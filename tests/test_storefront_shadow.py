# -*- coding: utf-8 -*-
"""Phase 9B.3 — Storefront shadow comparison tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.legacy_migration import LEGACY_SERVICE_BRIDGE_TABLE
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_shadow import (
    compare_storefronts,
    load_catalog_snapshots,
    load_legacy_snapshots,
)


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "storefront_shadow.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE smm_services (
                service_id TEXT NOT NULL,
                catalog_id TEXT PRIMARY KEY,
                local_item_id TEXT,
                name_ar TEXT NOT NULL DEFAULT '',
                local_price_dh REAL NOT NULL DEFAULT 0,
                min_qty INTEGER NOT NULL DEFAULT 1,
                max_qty INTEGER NOT NULL DEFAULT 1000,
                category TEXT NOT NULL DEFAULT '',
                platform_key TEXT NOT NULL DEFAULT '',
                platform_title TEXT NOT NULL DEFAULT '',
                section_key TEXT NOT NULL DEFAULT '',
                section_title TEXT NOT NULL DEFAULT '',
                subsection_key TEXT NOT NULL DEFAULT '',
                subsection_title TEXT NOT NULL DEFAULT '',
                external_service_id TEXT NOT NULL DEFAULT '',
                provider_slug TEXT NOT NULL DEFAULT 'gozibra',
                provider_api_account TEXT NOT NULL DEFAULT 'default',
                provider_price_usd REAL NOT NULL DEFAULT 0,
                fulfillment_mode TEXT NOT NULL DEFAULT 'auto',
                is_active INTEGER NOT NULL DEFAULT 1
            );
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
            VALUES ('gozibra', 'default', 'Main');
            INSERT INTO provider_accounts(provider_slug, account_key, display_name)
            VALUES ('other', 'main', 'Main');
            CREATE TABLE catalog_services (catalog_id TEXT PRIMARY KEY, name_ar TEXT);
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


def _insert_legacy(
    conn: sqlite3.Connection,
    *,
    catalog_id: str,
    name: str,
    price_dh: float = 2.0,
    min_qty: int = 10,
    max_qty: int = 5000,
    category: str = "followers",
    platform_key: str = "tiktok",
    section_key: str = "likes",
    subsection_key: str = "",
    external_service_id: str = "123",
    provider_slug: str = "gozibra",
    provider_api_account: str = "default",
    is_active: int = 1,
) -> None:
    conn.execute(
        """
        INSERT INTO smm_services (
            service_id, catalog_id, local_item_id, name_ar, local_price_dh,
            min_qty, max_qty, category, platform_key, platform_title,
            section_key, section_title, subsection_key, subsection_title,
            external_service_id, provider_slug, provider_api_account,
            fulfillment_mode, is_active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'auto', ?)
        """,
        (
            catalog_id,
            catalog_id,
            catalog_id,
            name,
            price_dh,
            min_qty,
            max_qty,
            category,
            platform_key,
            platform_key,
            section_key,
            section_key,
            subsection_key,
            subsection_key,
            external_service_id,
            provider_slug,
            provider_api_account,
            is_active,
        ),
    )


def _place(core: CatalogCoreService, service_id: str, path_names: list[str]) -> None:
    parent = None
    for name in path_names:
        node = core.create_node(name_ar=name, parent_entry_id=parent)
        parent = node.entry_id
    core.move_service(service_id, new_parent_entry_id=parent)


def _ready_publish(
    core: CatalogCoreService,
    pub: CatalogPublicationService,
    *,
    name: str,
    external_id: str = "123",
    amount_dh: str = "2",
    pricing_mode: str = "per_1000",
    min_quantity: int = 10,
    max_quantity: int = 5000,
    path: list[str] | None = None,
    provider_slug: str = "gozibra",
    provider_account_key: str = "default",
):
    svc = core.create_service(
        name_ar=name,
        note_ar="",
        service_type="followers",
        ordering_mode="quantity_based",
        min_quantity=min_quantity,
        max_quantity=max_quantity,
        status="active",
        fulfillment_mode="auto",
        target_platform_key="tiktok",
        target_section_key="likes",
    )
    core.change_execution_source(
        svc.id,
        provider_slug=provider_slug,
        provider_account_key=provider_account_key,
        external_service_id=external_id,
    )
    core.change_price(
        svc.id, amount_dh=amount_dh, pricing_mode=pricing_mode, currency="MAD"
    )
    _place(core, svc.id, path or ["تيك توك", "لايك"])
    pub.publish(svc.id, published_by="admin")
    return core.get_service(svc.id)


def _bridge(conn: sqlite3.Connection, legacy_catalog_id: str, soldium_service_id: str) -> None:
    conn.execute(
        f"""
        INSERT INTO {LEGACY_SERVICE_BRIDGE_TABLE} (
            legacy_catalog_id, legacy_local_item_id, legacy_service_id,
            soldium_service_id, classification, review_codes, migration_batch_id
        ) VALUES (?, ?, ?, ?, 'SAFE', '[]', 'test_batch')
        """,
        (legacy_catalog_id, legacy_catalog_id, legacy_catalog_id, soldium_service_id),
    )


def _codes(row) -> set[str]:
    return {d.code for d in row.differences}


def test_empty_catalog_pre_publication_baseline(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        _insert_legacy(conn, catalog_id="leg_a", name="خدمة أ")
        _insert_legacy(conn, catalog_id="leg_b", name="خدمة ب")
        report = compare_storefronts(conn, generated_at="2026-01-01T00:00:00+00:00")
        assert report.baseline_state == "PRE_PUBLICATION_BASELINE"
        assert report.legacy_count == 2
        assert report.catalog_count == 0
        assert report.legacy_only_count == 2
        assert report.baseline_count == 2
        assert report.dangerous_count == 0
        assert load_catalog_snapshots(conn) == []
        assert len(load_legacy_snapshots(conn)) == 2


def test_perfect_match_correlated_review_only_placement(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_publish(
            core,
            pub,
            name="خدمة متطابقة",
            external_id="123",
            amount_dh="2",
            min_quantity=10,
            max_quantity=5000,
        )
        _insert_legacy(
            conn,
            catalog_id="leg_match",
            name="خدمة متطابقة",
            price_dh=2.0,
            min_qty=10,
            max_qty=5000,
            external_service_id="123",
            category="followers",
        )
        _bridge(conn, "leg_match", svc.id)
        report = compare_storefronts(conn, generated_at="t0")
        assert report.catalog_count == 1
        assert report.correlated_count == 1
        row = report.rows[0]
        assert row.correlation == "correlated"
        codes = _codes(row)
        assert "price_equal" in codes
        assert "min_equal" in codes
        assert "max_equal" in codes
        assert "execution_equal" in codes
        assert "name_equal" in codes
        assert "placement_comparison_indeterminate" in codes
        # No dangerous price/qty/execution diffs
        assert row.severity in {"review", "equal", "info"}
        assert "price_changed" not in codes
        assert "execution_changed" not in codes


def test_price_min_max_name_execution_mismatches(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_publish(
            core,
            pub,
            name="كتالوج",
            external_id="111",
            amount_dh="3",
            min_quantity=20,
            max_quantity=100,
        )
        _insert_legacy(
            conn,
            catalog_id="leg_diff",
            name="ليجاسي",
            price_dh=2.0,
            min_qty=10,
            max_qty=200,
            external_service_id="999",
        )
        _bridge(conn, "leg_diff", svc.id)
        report = compare_storefronts(conn, generated_at="t1")
        row = next(r for r in report.rows if r.correlation == "correlated")
        codes = _codes(row)
        assert "price_changed" in codes
        assert "min_changed" in codes
        assert "max_changed" in codes
        assert "name_changed" in codes
        assert "execution_changed" in codes
        assert row.severity == "dangerous"


def test_execution_live_catalog_and_legacy_write_through(catalog_db: Path):
    """Customer Catalog follows live source; Legacy write-through stays aligned."""
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_publish(core, pub, name="Exec", external_id="123")
        _insert_legacy(
            conn,
            catalog_id="leg_exec",
            name="Exec",
            external_service_id="123",
            price_dh=2.0,
        )
        _bridge(conn, "leg_exec", svc.id)
        core.change_execution_source(
            svc.id,
            provider_slug="other",
            provider_account_key="main",
            external_service_id="456",
        )
        catalog = load_catalog_snapshots(conn)[0]
        assert catalog.execution is not None
        assert catalog.execution.external_service_id == "456"
        leg = conn.execute(
            "SELECT external_service_id, provider_api_account FROM smm_services "
            "WHERE catalog_id='leg_exec'"
        ).fetchone()
        assert str(leg["external_service_id"]) == "456"
        assert str(leg["provider_api_account"]) == "main"
        report = compare_storefronts(conn, generated_at="t2")
        row = next(r for r in report.rows if r.correlation == "correlated")
        assert "execution_changed" not in _codes(row)


def test_catalog_only_and_legacy_only_when_published(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        orphan = _ready_publish(core, pub, name="منشور فقط", external_id="77")
        _insert_legacy(conn, catalog_id="leg_only", name="ليجاسي فقط")
        # No bridge for either → legacy_only + catalog_only; not pre-publication
        report = compare_storefronts(conn, generated_at="t3")
        assert report.baseline_state == "PARTIAL_PUBLICATION_PILOT"
        assert report.legacy_only_count == 1
        assert report.catalog_only_count == 1
        # Legacy-only outside pilot scope is baseline, not dangerous.
        assert report.baseline_count == 1
        assert report.dangerous_count >= 1  # catalog_only
        leg_row = next(r for r in report.rows if r.correlation == "legacy_only")
        assert "PILOT_PUBLICATION_SCOPE" in leg_row.notes
        assert orphan.id.startswith("svc_")


def test_fixed_package_unsupported(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_publish(
            core,
            pub,
            name="باقة",
            external_id="88",
            amount_dh="5",
            pricing_mode="fixed_package",
            min_quantity=1,
            max_quantity=1,
        )
        _insert_legacy(
            conn,
            catalog_id="leg_pkg",
            name="باقة",
            price_dh=5.0,
            min_qty=1,
            max_qty=1,
            external_service_id="88",
        )
        _bridge(conn, "leg_pkg", svc.id)
        report = compare_storefronts(conn, generated_at="t4")
        row = next(r for r in report.rows if r.correlation == "correlated")
        assert "unsupported_pricing_mode" in _codes(row)
        assert "legacy_orderable_catalog_not_orderable" in _codes(row)
        assert row.severity == "dangerous"


def test_external_id_not_identity(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_publish(core, pub, name="A", external_id="555")
        # Different legacy id, same external id — no bridge → not correlated
        _insert_legacy(
            conn, catalog_id="other_leg", name="B", external_service_id="555"
        )
        report = compare_storefronts(conn, generated_at="t5")
        assert report.correlated_count == 0
        assert report.catalog_only_count == 1
        assert report.legacy_only_count == 1
        assert svc.id not in {
            r.legacy_identity for r in report.rows if r.legacy_identity
        }


def test_legacy_poison_does_not_change_catalog_side(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_publish(core, pub, name="اسم منشور", external_id="1", amount_dh="2")
        _insert_legacy(
            conn, catalog_id="leg_p", name="اسم منشور", price_dh=2.0, external_service_id="1"
        )
        _bridge(conn, "leg_p", svc.id)
        before = load_catalog_snapshots(conn)[0]
        conn.execute(
            "UPDATE smm_services SET name_ar=?, local_price_dh=? WHERE catalog_id=?",
            ("POISON", 99.0, "leg_p"),
        )
        after = load_catalog_snapshots(conn)[0]
        assert after.name == before.name == "اسم منشور"
        assert after.amount_millimes == before.amount_millimes == 2000


def test_determinism_fingerprint(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        _insert_legacy(conn, catalog_id="leg_d", name="د")
        a = compare_storefronts(conn, generated_at="2026-01-01T00:00:00+00:00")
        b = compare_storefronts(conn, generated_at="2026-01-02T00:00:00+00:00")
        assert a.fingerprint == b.fingerprint
        assert a.generated_at != b.generated_at


def test_inactive_legacy_excluded(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        _insert_legacy(conn, catalog_id="inactive", name="خامل", is_active=0)
        _insert_legacy(
            conn, catalog_id="noplat", name="بدون منصة", platform_key="", is_active=1
        )
        assert load_legacy_snapshots(conn) == []
