# -*- coding: utf-8 -*-
"""Phase 5 — Catalog service readiness tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.models import CatalogPrice, CatalogService, ExecutionSource
from catalog_core.readiness import (
    check_basic_service,
    check_catalog_placement,
    check_commercial_profile,
    check_execution_source,
    check_price,
    evaluate_service_readiness,
)
from catalog_core.repository import CatalogRepository
from catalog_core.schema import SOLDIUM_CATALOG_SCHEMA_VERSION, ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "catalog_readiness_test.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE smm_services (
                service_id TEXT PRIMARY KEY,
                name_ar TEXT NOT NULL DEFAULT 'legacy',
                local_price_dh REAL NOT NULL DEFAULT 0
            );
            INSERT INTO smm_services(service_id, name_ar) VALUES ('legacy-1', 'قديم');
            CREATE TABLE orders (id INTEGER PRIMARY KEY, service_id TEXT NOT NULL);
            INSERT INTO orders(id, service_id) VALUES (1, 'legacy-1');
            CREATE TABLE providers (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                api_base_url TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO providers(slug, name, api_base_url)
            VALUES ('gozibra', 'Gozibra', 'https://example.test');
            INSERT INTO providers(slug, name, api_base_url)
            VALUES ('other', 'Other', 'https://other.test');
            CREATE TABLE provider_accounts (
                id INTEGER PRIMARY KEY,
                provider_slug TEXT NOT NULL,
                account_key TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                UNIQUE(provider_slug, account_key)
            );
            INSERT INTO provider_accounts(provider_slug, account_key, display_name)
            VALUES ('gozibra', 'default', 'افتراضي');
            INSERT INTO provider_accounts(provider_slug, account_key, display_name)
            VALUES ('other', 'main', 'رئيسي');
            CREATE TABLE catalog_services (catalog_id TEXT PRIMARY KEY, name_ar TEXT);
            INSERT INTO catalog_services VALUES ('old', 'قديم');
            CREATE TABLE catalog_nodes (id TEXT PRIMARY KEY);
            CREATE TABLE service_provider_bindings (id INTEGER PRIMARY KEY);
            CREATE TABLE price_rules (id INTEGER PRIMARY KEY);
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _make_ready(svc: CatalogCoreService, name: str = "خدمة جاهزة") -> CatalogService:
    created = svc.create_service(
        name_ar=name,
        service_type="followers",
        ordering_mode="quantity_based",
        min_quantity=100,
        max_quantity=10000,
        status="active",
        fulfillment_mode="auto",
        target_platform_key="instagram",
        target_section_key="followers",
    )
    svc.change_execution_source(
        created.id,
        provider_slug="gozibra",
        provider_account_key="default",
        external_service_id="12345",
    )
    svc.change_price(
        created.id, amount_dh="2", pricing_mode="per_1000", currency="MAD"
    )
    return svc.get_service(created.id)


# ── BASIC ──────────────────────────────────────────────


def test_basic_valid_service(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="أساسية")
        result = check_basic_service(created)
        assert result.ok is True


def test_basic_missing_name(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="مؤقت")
        conn.execute(
            "UPDATE soldium_catalog_services SET name_ar = '' WHERE id = ?",
            (created.id,),
        )
        row = CatalogRepository(conn).get_service(created.id)
        result = check_basic_service(row)
        assert result.ok is False
        assert any(i.code == "missing_name" for i in result.issues)


def test_basic_archived_not_ready(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        ready = _make_ready(svc)
        svc.archive_service(ready.id)
        result = svc.get_service_readiness(ready.id)
        assert result.ready is False
        assert result.state == "needs_review"
        assert any(i.code == "service_archived" for i in result.issues)
        assert "مؤرشفة" in result.issues[0].message or any(
            "مؤرشفة" in i.message for i in result.issues
        )


# ── PLACEMENT ──────────────────────────────────────────


def test_placement_valid(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="بمكان")
        result = check_catalog_placement(svc.repo, created)
        assert result.ok is True


def test_placement_missing(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="بلا مكان")
        entry = svc.repo.get_entry_for_service(created.id)
        assert entry is not None
        conn.execute(
            "DELETE FROM soldium_catalog_entries WHERE id = ?", (entry.id,)
        )
        result = check_catalog_placement(svc.repo, created)
        assert result.ok is False
        assert any(i.code == "missing_placement" for i in result.issues)


def test_placement_broken_parent(catalog_db: Path):
    """Broken parent is detected even when the DB FK would normally prevent it."""
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        node = svc.create_node(name_ar="قسم")
        created = svc.create_service(name_ar="تحت قسم", parent_entry_id=node.entry_id)
        entry = svc.repo.get_entry_for_service(created.id)
        assert entry is not None

        # Simulate a corrupt placement row without fighting live FK enforcement.
        class _RepoProxy:
            def __init__(self, repo, entry_id, broken_parent_id):
                self._repo = repo
                self._entry_id = entry_id
                self._broken_parent_id = broken_parent_id

            def get_entry_for_service(self, service_id):
                e = self._repo.get_entry_for_service(service_id)
                if e and e.id == self._entry_id:
                    e.parent_entry_id = self._broken_parent_id
                return e

            def get_entry(self, entry_id):
                if entry_id == self._broken_parent_id:
                    return None
                return self._repo.get_entry(entry_id)

            def __getattr__(self, name):
                return getattr(self._repo, name)

        proxy = _RepoProxy(svc.repo, entry.id, "entry_missing_parent")
        result = check_catalog_placement(proxy, created)
        assert result.ok is False
        assert any(i.code == "broken_parent" for i in result.issues)


def test_commercial_valid(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(
            name_ar="تجاري",
            service_type="likes",
            ordering_mode="quantity_based",
            min_quantity=10,
            max_quantity=1000,
        )
        assert check_commercial_profile(created).ok is True


def test_commercial_invalid_type(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="نوع خاطئ")
        conn.execute(
            "UPDATE soldium_catalog_services SET service_type = ? WHERE id = ?",
            ("not_a_real_type", created.id),
        )
        row = CatalogRepository(conn).get_service(created.id)
        result = check_commercial_profile(row)
        assert result.ok is False
        assert any(i.code == "invalid_service_type" for i in result.issues)


def test_commercial_invalid_ordering_mode():
    svc = CatalogService(
        id="s1",
        name_ar="طلب خاطئ",
        note_ar="",
        status="active",
        service_type="other",
        ordering_mode="bogus_mode",
        min_quantity=1,
        max_quantity=10,
        created_at="",
        updated_at="",
    )
    result = check_commercial_profile(svc)
    assert result.ok is False
    assert any(i.code == "invalid_ordering_mode" for i in result.issues)


def test_commercial_valid_quantity_range(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(
            name_ar="كمية صالحة", min_quantity=0, max_quantity=500
        )
        assert check_commercial_profile(created).ok is True


def test_commercial_invalid_quantity_range(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="كمية خاطئة")
        conn.execute(
            "UPDATE soldium_catalog_services SET min_quantity = 100, max_quantity = 10 WHERE id = ?",
            (created.id,),
        )
        row = CatalogRepository(conn).get_service(created.id)
        result = check_commercial_profile(row)
        assert result.ok is False
        assert any(i.code == "invalid_commercial_quantities" for i in result.issues)


# ── SOURCE ─────────────────────────────────────────────


def test_source_active_exists(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="بمصدر")
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="99",
        )
        src = svc.repo.get_active_execution_source(created.id)
        result = check_execution_source(svc.repo, created, source=src)
        assert result.ok is True


def test_source_missing(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="بلا مصدر")
        result = check_execution_source(svc.repo, created)
        assert result.ok is False
        assert any(i.code == "missing_execution_source" for i in result.issues)


def test_source_invalid_provider(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="مورد خاطئ")
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="1",
        )
        conn.execute(
            "UPDATE soldium_catalog_execution_sources SET provider_slug = ? WHERE service_id = ? AND status = 'active'",
            ("ghost-provider", created.id),
        )
        src = svc.repo.get_active_execution_source(created.id)
        result = check_execution_source(svc.repo, created, source=src)
        assert result.ok is False
        assert any(i.code == "invalid_provider" for i in result.issues)


def test_source_invalid_account(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="حساب خاطئ")
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="1",
        )
        conn.execute(
            "UPDATE soldium_catalog_execution_sources SET provider_account_key = ? WHERE service_id = ? AND status = 'active'",
            ("missing-account", created.id),
        )
        src = svc.repo.get_active_execution_source(created.id)
        result = check_execution_source(svc.repo, created, source=src)
        assert result.ok is False
        assert any(i.code == "invalid_provider_account" for i in result.issues)


def test_source_provider_account_mismatch(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="عدم تطابق")
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="1",
        )
        # account_key "main" belongs to provider "other", not gozibra
        conn.execute(
            "UPDATE soldium_catalog_execution_sources SET provider_account_key = ? WHERE service_id = ? AND status = 'active'",
            ("main", created.id),
        )
        src = svc.repo.get_active_execution_source(created.id)
        result = check_execution_source(svc.repo, created, source=src)
        assert result.ok is False
        assert any(i.code == "provider_account_mismatch" for i in result.issues)


def test_source_empty_external_id(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="معرف فارغ")
        src = ExecutionSource(
            id="src1",
            service_id=created.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="   ",
            status="active",
        )
        result = check_execution_source(svc.repo, created, source=src)
        assert result.ok is False
        assert any(i.code == "empty_external_service_id" for i in result.issues)


# ── PRICE ──────────────────────────────────────────────


def test_price_valid(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="بسعر")
        svc.change_price(created.id, amount_dh="1.5", pricing_mode="per_1000")
        price = svc.repo.get_active_price(created.id)
        assert check_price(created, price=price).ok is True


def test_price_missing(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="بلا سعر")
        result = check_price(created, price=None)
        assert result.ok is False
        assert any(i.code == "missing_price" for i in result.issues)


def test_price_invalid_amount():
    price = CatalogPrice(
        id="p1",
        service_id="s1",
        amount_millimes=0,
        pricing_mode="per_1000",
        currency="MAD",
        status="active",
        effective_from="2026-01-01",
        effective_to=None,
    )
    svc = CatalogService(
        id="s1",
        name_ar="x",
        note_ar="",
        status="active",
        service_type="other",
        ordering_mode="quantity_based",
        min_quantity=1,
        max_quantity=10,
        created_at="",
        updated_at="",
    )
    result = check_price(svc, price=price)
    assert result.ok is False
    assert any(i.code == "invalid_price_amount" for i in result.issues)


def test_price_invalid_pricing_mode():
    price = CatalogPrice(
        id="p1",
        service_id="s1",
        amount_millimes=1000,
        pricing_mode="weird_mode",
        currency="MAD",
        status="active",
        effective_from="2026-01-01",
        effective_to=None,
    )
    svc = CatalogService(
        id="s1",
        name_ar="x",
        note_ar="",
        status="active",
        service_type="other",
        ordering_mode="quantity_based",
        min_quantity=1,
        max_quantity=10,
        created_at="",
        updated_at="",
    )
    result = check_price(svc, price=price)
    assert result.ok is False
    assert any(i.code == "invalid_pricing_mode" for i in result.issues)


# ── COMBINATION ────────────────────────────────────────


def test_all_valid_ready(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        ready = _make_ready(svc)
        result = svc.get_service_readiness(ready.id)
        assert result.ready is True
        assert result.state == "ready"
        assert result.state_label_ar == "جاهزة"
        assert result.issues == []
        assert all(c.ok for c in result.checks)


def test_missing_source_needs_review(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="ناقصة مصدر", status="active")
        svc.change_price(created.id, amount_dh="2", pricing_mode="per_1000")
        result = svc.get_service_readiness(created.id)
        assert result.ready is False
        assert any(i.code == "missing_execution_source" for i in result.issues)


def test_missing_price_needs_review(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="ناقصة سعر", status="active")
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="55",
        )
        result = svc.get_service_readiness(created.id)
        assert result.ready is False
        assert any(i.code == "missing_price" for i in result.issues)


def test_multiple_issues_returned(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="عدة مشاكل", status="active")
        result = svc.get_service_readiness(created.id)
        codes = {i.code for i in result.issues}
        assert "missing_execution_source" in codes
        assert "missing_price" in codes
        assert "missing_target_platform_key" in codes or "missing_target_policy" in codes
        assert len(result.issues) >= 3


def test_missing_target_needs_review(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(
            name_ar="بلا هدف",
            status="active",
            fulfillment_mode="auto",
        )
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="55",
        )
        svc.change_price(created.id, amount_dh="2", pricing_mode="per_1000")
        result = svc.get_service_readiness(created.id)
        assert result.ready is False
        assert any(
            i.fix_action == "target" for i in result.issues
        )


def test_archived_not_ready_even_if_complete(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        ready = _make_ready(svc)
        svc.archive_service(ready.id)
        result = svc.get_service_readiness(ready.id)
        assert result.ready is False
        assert result.state == "needs_review"


# ── ISOLATION / READ-ONLY ───────────────────────────────


def test_readiness_does_not_modify_data(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        ready = _make_ready(svc, "عزل")
        before_svc = conn.execute(
            "SELECT name_ar, status, updated_at FROM soldium_catalog_services WHERE id = ?",
            (ready.id,),
        ).fetchone()
        before_src = conn.execute(
            "SELECT COUNT(*), MAX(id) FROM soldium_catalog_execution_sources WHERE service_id = ?",
            (ready.id,),
        ).fetchone()
        before_price = conn.execute(
            "SELECT COUNT(*), MAX(id) FROM soldium_catalog_prices WHERE service_id = ?",
            (ready.id,),
        ).fetchone()
        before_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        before_smm = conn.execute(
            "SELECT name_ar FROM smm_services WHERE service_id = 'legacy-1'"
        ).fetchone()[0]

        for _ in range(3):
            svc.get_service_readiness(ready.id)
            evaluate_service_readiness(svc.repo, svc.get_service(ready.id))

        after_svc = conn.execute(
            "SELECT name_ar, status, updated_at FROM soldium_catalog_services WHERE id = ?",
            (ready.id,),
        ).fetchone()
        after_src = conn.execute(
            "SELECT COUNT(*), MAX(id) FROM soldium_catalog_execution_sources WHERE service_id = ?",
            (ready.id,),
        ).fetchone()
        after_price = conn.execute(
            "SELECT COUNT(*), MAX(id) FROM soldium_catalog_prices WHERE service_id = ?",
            (ready.id,),
        ).fetchone()
        assert tuple(before_svc) == tuple(after_svc)
        assert tuple(before_src) == tuple(after_src)
        assert tuple(before_price) == tuple(after_price)
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == before_orders
        assert (
            conn.execute(
                "SELECT name_ar FROM smm_services WHERE service_id = 'legacy-1'"
            ).fetchone()[0]
            == before_smm
        )


def test_no_readiness_table_or_override(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "soldium_catalog_readiness" not in tables
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(soldium_catalog_services)").fetchall()
        }
        assert "readiness" not in cols
        assert "is_ready" not in cols
        ver = conn.execute(
            "SELECT value FROM soldium_catalog_schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert ver == SOLDIUM_CATALOG_SCHEMA_VERSION


def test_list_filter_and_review_summary(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        _make_ready(svc, "جاهزة 1")
        svc.create_service(name_ar="تحتاج مراجعة")
        ready_items, ready_total = svc.list_services(readiness="ready")
        needs_items, needs_total = svc.list_services(readiness="needs_review")
        assert ready_total >= 1
        assert all(s.readiness and s.readiness["ready"] for s in ready_items)
        assert needs_total >= 1
        assert all(s.readiness and not s.readiness["ready"] for s in needs_items)
        summary = svc.review_summary()
        assert summary["total_services"] >= 2
        assert summary["ready"] >= 1
        assert summary["needs_review"] >= 1
        assert summary["without_source"] >= 1
        assert summary["without_price"] >= 1


def test_api_readiness_routes():
    from main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/soldium-catalog/services/{service_id}/readiness" in paths
    assert "/api/soldium-catalog/review" in paths
    assert "/api/soldium-catalog/review/summary" in paths


def test_ui_readiness_arabic_and_no_browser_dialogs():
    root = Path(__file__).resolve().parent.parent
    js = (root / "static" / "js" / "catalog_core_ui.js").read_text(encoding="utf-8")
    services_html = (root / "templates" / "workspaces" / "catalog_services.html").read_text(
        encoding="utf-8"
    )
    review_html = (root / "templates" / "workspaces" / "catalog_review.html").read_text(
        encoding="utf-8"
    )
    assert "جاهزية الخدمة" in js
    assert "تحتاج مراجعة" in js
    assert "جاهزة" in js
    assert "mountReview" in js
    assert "svc-readiness" in services_html
    assert "المراجعة الإدارية" in review_html
    assert "prompt(" not in js
    assert "confirm(" not in js
    assert "alert(" not in js


def test_legacy_tables_untouched(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        _make_ready(svc)
        svc.review_summary()
        assert conn.execute(
            "SELECT name_ar FROM catalog_services WHERE catalog_id = 'old'"
        ).fetchone()[0] == "قديم"
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM provider_accounts").fetchone()[0] == 2
        assert conn.execute(
            "SELECT name_ar FROM smm_services WHERE service_id = 'legacy-1'"
        ).fetchone()[0] == "قديم"
