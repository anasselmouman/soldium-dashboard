# -*- coding: utf-8 -*-
"""Phase 8D — real legacy migration importer tests (isolated DBs only)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from catalog_core.legacy_migration import (
    compute_plan_fingerprint,
    plan_legacy_migration_at_path,
    planned_soldium_service_id,
)
from catalog_core.legacy_migration_import import (
    APPROVED_PLAN_FINGERPRINT,
    LegacyMigrationImportError,
    _import_one_service,
    _verify_existing_service,
    execute_legacy_migration,
    reconcile_migration,
    run_preflight,
)
from catalog_core.legacy_migration_import import execute_legacy_migration as _exec
from catalog_core.schema import SOLDIUM_CATALOG_SCHEMA_VERSION, ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService


def _base(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE providers (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO providers(slug, name) VALUES ('gozibra', 'Gozibra');
        CREATE TABLE provider_accounts (
            id INTEGER PRIMARY KEY,
            provider_slug TEXT NOT NULL,
            account_key TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            UNIQUE(provider_slug, account_key)
        );
        INSERT INTO provider_accounts(provider_slug, account_key, display_name)
        VALUES ('gozibra', 'tiktok', 'TikTok');
        CREATE TABLE orders (id INTEGER PRIMARY KEY, service_id TEXT NOT NULL DEFAULT '');
        INSERT INTO orders(id, service_id) VALUES (1, 'keep');
        CREATE TABLE smm_services (
            catalog_id TEXT PRIMARY KEY,
            external_service_id TEXT NOT NULL,
            provider_slug TEXT NOT NULL DEFAULT 'gozibra',
            category TEXT NOT NULL DEFAULT '',
            name_ar TEXT NOT NULL DEFAULT '',
            provider_price_usd REAL NOT NULL DEFAULT 0,
            local_price_dh REAL NOT NULL DEFAULT 0,
            min_qty INTEGER NOT NULL DEFAULT 1,
            max_qty INTEGER NOT NULL DEFAULT 1000000,
            is_active INTEGER NOT NULL DEFAULT 1,
            platform_key TEXT NOT NULL DEFAULT '',
            section_key TEXT,
            subsection_key TEXT,
            local_item_id TEXT NOT NULL DEFAULT '',
            platform_title TEXT NOT NULL DEFAULT '',
            section_title TEXT,
            subsection_title TEXT,
            fulfillment_mode TEXT NOT NULL DEFAULT 'auto',
            provider_api_account TEXT,
            provider_price_updated_at TEXT,
            service_id TEXT NOT NULL DEFAULT ''
        );
        """
    )
    ensure_soldium_catalog_schema(conn)


def _ins(
    conn: sqlite3.Connection,
    *,
    catalog_id: str,
    name_ar: str = "خدمة",
    price: float = 1.5,
    min_qty: int = 10,
    max_qty: int = 1000,
    platform_key: str = "tiktok",
    platform_title: str = "تيك",
    section_key: str | None = "likes",
    section_title: str | None = "لايك",
    subsection_key: str | None = None,
    subsection_title: str | None = None,
    category: str = "x",
    account: str | None = "tiktok",
    external: str | None = None,
    fulfillment: str = "auto",
    provider_price_usd: float = 9.99,
) -> None:
    ext = external or catalog_id
    conn.execute(
        """
        INSERT INTO smm_services (
            catalog_id, external_service_id, provider_slug, category, name_ar,
            provider_price_usd, local_price_dh, min_qty, max_qty, is_active,
            platform_key, section_key, subsection_key, local_item_id,
            platform_title, section_title, subsection_title,
            fulfillment_mode, provider_api_account, service_id
        ) VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            catalog_id,
            ext,
            "gozibra",
            category,
            name_ar,
            provider_price_usd,
            price,
            min_qty,
            max_qty,
            platform_key,
            section_key,
            subsection_key,
            catalog_id,
            platform_title,
            section_title,
            subsection_title,
            fulfillment,
            account,
            ext,
        ),
    )


@pytest.fixture
def mig_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Small fixture — bypass approved fingerprint (test-only)."""
    path = tmp_path / "mig.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _base(conn)
        _ins(conn, catalog_id="1001", name_ar="آمن", price=1.5)
        _ins(
            conn,
            catalog_id="1002",
            name_ar="منسّق",
            price=24.8791,
            subsection_key="geo",
            subsection_title="دول",
        )
        _ins(conn, catalog_id="2001", name_ar="بدون حساب", account=None, price=2.0)
        _ins(
            conn,
            catalog_id="3001",
            name_ar="وحدة",
            category="per_unit",
            price=42.0,
            min_qty=1,
            max_qty=1,
            fulfillment="admin",
            platform_key="subscriptions",
            platform_title="اشتراكات",
            section_key="iptv",
            section_title="IPTV",
        )
        _ins(conn, catalog_id="4001", name_ar="سنتينل", max_qty=2147483647, price=3.0)
        conn.commit()
    finally:
        conn.close()

    # Patch approved constants for this isolated DB
    import catalog_core.legacy_migration_import as imp

    report = plan_legacy_migration_at_path(path)
    monkeypatch.setattr(imp, "APPROVED_PLAN_FINGERPRINT", compute_plan_fingerprint(report))
    monkeypatch.setattr(imp, "APPROVED_CANDIDATE_COUNT", report.candidate_count)
    monkeypatch.setattr(imp, "APPROVED_SAFE_COUNT", report.safe_count)
    monkeypatch.setattr(imp, "APPROVED_REVIEW_COUNT", report.review_count)
    monkeypatch.setattr(imp, "APPROVED_BLOCKED_COUNT", report.blocked_count)
    monkeypatch.setattr(imp, "APPROVED_NODE_COUNT", report.planned_node_count)
    monkeypatch.setattr(
        imp, "APPROVED_EXEC_SOURCE_COUNT", report.planned_execution_source_count
    )
    monkeypatch.setattr(
        imp,
        "APPROVED_SMM_SERVICES_COUNT",
        report.candidate_count + report.inactive_inventory_count,
    )
    # fixture has 5 active + 0 inactive inventory matching filter; inactive_inventory includes non-candidates
    # smm total = all rows inserted = 5
    monkeypatch.setattr(imp, "APPROVED_SMM_SERVICES_COUNT", 5)
    monkeypatch.setattr(imp, "APPROVED_ORDERS_COUNT", 1)
    return path


def test_execute_creates_identity_structure_prices_sources(mig_db: Path):
    result = execute_legacy_migration(mig_db)
    assert result.verdict.startswith("MIGRATION SUCCESSFUL"), result.errors
    conn = sqlite3.connect(mig_db)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM soldium_catalog_services").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM soldium_catalog_nodes").fetchone()[0] >= 2
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_entries WHERE entry_type='service'"
            ).fetchone()[0]
            == 5
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_prices WHERE status='active'"
            ).fetchone()[0]
            == 5
        )
        # 1002 normalized
        sid = planned_soldium_service_id("1002")
        millimes = conn.execute(
            "SELECT amount_millimes FROM soldium_catalog_prices WHERE service_id=? AND status='active'",
            (sid,),
        ).fetchone()[0]
        assert millimes == 24879
        # missing account: no source
        sid_miss = planned_soldium_service_id("2001")
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_execution_sources WHERE service_id=? AND status='active'",
                (sid_miss,),
            ).fetchone()[0]
            == 0
        )
        # safe has source
        sid_ok = planned_soldium_service_id("1001")
        row = conn.execute(
            "SELECT provider_slug, provider_account_key, external_service_id FROM soldium_catalog_execution_sources WHERE service_id=? AND status='active'",
            (sid_ok,),
        ).fetchone()
        assert row["external_service_id"] == "1001"
        assert row["provider_account_key"] == "tiktok"
        # bridge review + fulfillment
        br = conn.execute(
            "SELECT classification, review_codes, legacy_fulfillment_mode, provider_api_account FROM soldium_catalog_legacy_bridge WHERE legacy_catalog_id='3001'"
        ).fetchone()
        assert br["legacy_fulfillment_mode"] == "admin"
        codes = json.loads(br["review_codes"])
        assert "per_unit" in codes and "fulfillment_admin" in codes
        # no publication / mapping
        assert conn.execute("SELECT COUNT(*) FROM soldium_catalog_publications").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM soldium_provider_service_mappings").fetchone()[0]
            == 0
        )
        # cost not retail
        price = conn.execute(
            "SELECT amount_millimes FROM soldium_catalog_prices WHERE service_id=?",
            (sid_ok,),
        ).fetchone()[0]
        assert price == 1500  # 1.5 DH — not provider_price_usd
        # legacy untouched
        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == 5
        assert conn.execute(
            "SELECT local_price_dh FROM smm_services WHERE catalog_id='1002'"
        ).fetchone()[0] == pytest.approx(24.8791)
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert (
            conn.execute("SELECT value FROM soldium_catalog_schema_meta WHERE key='schema_version'")
            .fetchone()[0]
            == SOLDIUM_CATALOG_SCHEMA_VERSION
        )
    finally:
        conn.close()


def test_uuid5_deterministic(mig_db: Path):
    assert planned_soldium_service_id("1001") == planned_soldium_service_id("1001")
    execute_legacy_migration(mig_db)
    conn = sqlite3.connect(mig_db)
    try:
        sid = conn.execute(
            "SELECT soldium_service_id FROM soldium_catalog_legacy_bridge WHERE legacy_catalog_id='1001'"
        ).fetchone()[0]
        assert sid == planned_soldium_service_id("1001")
    finally:
        conn.close()


def test_rerun_idempotent(mig_db: Path):
    r1 = execute_legacy_migration(mig_db)
    assert r1.verdict.startswith("MIGRATION SUCCESSFUL")
    conn = sqlite3.connect(mig_db)
    before = conn.execute("SELECT COUNT(*) FROM soldium_catalog_services").fetchone()[0]
    conn.close()
    r2 = execute_legacy_migration(mig_db)
    assert r2.verdict.startswith("MIGRATION SUCCESSFUL"), r2.errors
    conn = sqlite3.connect(mig_db)
    after = conn.execute("SELECT COUNT(*) FROM soldium_catalog_services").fetchone()[0]
    bridges = conn.execute("SELECT COUNT(*) FROM soldium_catalog_legacy_bridge").fetchone()[0]
    conn.close()
    assert before == after == bridges == 5


def test_price_conflict_fails_closed(mig_db: Path):
    assert execute_legacy_migration(mig_db).verdict.startswith("MIGRATION SUCCESSFUL")
    conn = sqlite3.connect(mig_db)
    sid = planned_soldium_service_id("1001")
    conn.execute(
        "UPDATE soldium_catalog_prices SET amount_millimes=999 WHERE service_id=? AND status='active'",
        (sid,),
    )
    conn.commit()
    conn.close()
    report = plan_legacy_migration_at_path(mig_db)
    planned = next(s for s in report.services if s.legacy_catalog_id == "1001")
    conn = sqlite3.connect(mig_db)
    conn.row_factory = sqlite3.Row
    with pytest.raises(LegacyMigrationImportError, match="price"):
        _verify_existing_service(conn, planned)
    conn.close()


def test_bridge_missing_service_fails(mig_db: Path):
    assert execute_legacy_migration(mig_db).verdict.startswith("MIGRATION SUCCESSFUL")
    conn = sqlite3.connect(mig_db)
    sid = planned_soldium_service_id("1001")
    # Delete price/entry/source first due to FKs? publications none; disable FK for test teardown of service
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM soldium_catalog_execution_sources WHERE service_id=?", (sid,))
    conn.execute("DELETE FROM soldium_catalog_prices WHERE service_id=?", (sid,))
    conn.execute("DELETE FROM soldium_catalog_entries WHERE service_id=?", (sid,))
    conn.execute("DELETE FROM soldium_catalog_services WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    report = plan_legacy_migration_at_path(mig_db)
    planned = next(s for s in report.services if s.legacy_catalog_id == "1001")
    conn = sqlite3.connect(mig_db)
    conn.row_factory = sqlite3.Row
    with pytest.raises(LegacyMigrationImportError, match="missing service"):
        _verify_existing_service(conn, planned)
    conn.close()


def test_zero_normalized_price_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "zero.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _base(conn)
        _ins(conn, catalog_id="9001", name_ar="صفر", price=0.0001)  # → 0.000 millimes
        conn.commit()
    finally:
        conn.close()
    import catalog_core.legacy_migration_import as imp

    report = plan_legacy_migration_at_path(path)
    monkeypatch.setattr(imp, "APPROVED_PLAN_FINGERPRINT", compute_plan_fingerprint(report))
    monkeypatch.setattr(imp, "APPROVED_CANDIDATE_COUNT", report.candidate_count)
    monkeypatch.setattr(imp, "APPROVED_SAFE_COUNT", report.safe_count)
    monkeypatch.setattr(imp, "APPROVED_REVIEW_COUNT", report.review_count)
    monkeypatch.setattr(imp, "APPROVED_BLOCKED_COUNT", report.blocked_count)
    monkeypatch.setattr(imp, "APPROVED_NODE_COUNT", report.planned_node_count)
    monkeypatch.setattr(
        imp, "APPROVED_EXEC_SOURCE_COUNT", report.planned_execution_source_count
    )
    monkeypatch.setattr(imp, "APPROVED_SMM_SERVICES_COUNT", 1)
    monkeypatch.setattr(imp, "APPROVED_ORDERS_COUNT", 1)
    pre = run_preflight(path)
    assert pre.ok is False
    assert pre.stop_reason and "millimes" in pre.stop_reason.lower() or "zero" in (
        pre.stop_reason or ""
    ).lower() or "price" in (pre.stop_reason or "").lower()


def test_fingerprint_mismatch_stops(mig_db: Path, monkeypatch: pytest.MonkeyPatch):
    import catalog_core.legacy_migration_import as imp

    monkeypatch.setattr(imp, "APPROVED_PLAN_FINGERPRINT", "deadbeef" * 8)
    result = execute_legacy_migration(mig_db)
    assert result.verdict == "MIGRATION STOPPED — CONFLICT DETECTED"


def test_transaction_rollback_on_failure(mig_db: Path, monkeypatch: pytest.MonkeyPatch):
    import catalog_core.legacy_migration_import as imp

    real_import = imp._import_one_service
    calls = {"n": 0}

    def boom(conn, planned, key_to_entry, migration_batch_id):
        calls["n"] += 1
        if planned.legacy_catalog_id == "4001":
            raise LegacyMigrationImportError("boom", code="test")
        return real_import(conn, planned, key_to_entry, migration_batch_id)

    monkeypatch.setattr(imp, "_import_one_service", boom)
    result = execute_legacy_migration(mig_db)
    assert result.verdict == "MIGRATION FAILED — ROLLED BACK"
    conn = sqlite3.connect(mig_db)
    # Entire import transaction rolled back — no service bridges
    assert conn.execute("SELECT COUNT(*) FROM soldium_catalog_legacy_bridge").fetchone()[0] == 0
    # Nodes may also roll back with same transaction — check services
    assert conn.execute("SELECT COUNT(*) FROM soldium_catalog_services").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == 5
    conn.close()
