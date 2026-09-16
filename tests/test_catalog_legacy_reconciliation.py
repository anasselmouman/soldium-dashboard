# -*- coding: utf-8 -*-
"""Phase 8E — read-only post-migration reconciliation tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from catalog_core.legacy_migration import (
    compute_plan_fingerprint,
    plan_legacy_migration_at_path,
)
from catalog_core.legacy_migration_import import execute_legacy_migration
from catalog_core.legacy_reconciliation import (
    reconcile_legacy_migration_at_path,
    reconcile_twice,
)
from catalog_core.schema import ensure_soldium_catalog_schema


def _base(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE providers (slug TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '');
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


def _ins(conn, **kw):
    catalog_id = kw["catalog_id"]
    conn.execute(
        """
        INSERT INTO smm_services (
            catalog_id, external_service_id, provider_slug, category, name_ar,
            provider_price_usd, local_price_dh, min_qty, max_qty, is_active,
            platform_key, section_key, subsection_key, local_item_id,
            platform_title, section_title, subsection_title,
            fulfillment_mode, provider_api_account, service_id
        ) VALUES (?,?,?,?,?,0.5,?,?,?,1,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            catalog_id,
            kw.get("external", catalog_id),
            "gozibra",
            kw.get("category", "x"),
            kw.get("name_ar", "خدمة"),
            kw.get("price", 1.5),
            kw.get("min_qty", 10),
            kw.get("max_qty", 1000),
            kw.get("platform_key", "tiktok"),
            kw.get("section_key", "likes"),
            kw.get("subsection_key"),
            catalog_id,
            kw.get("platform_title", "تيك"),
            kw.get("section_title", "لايك"),
            kw.get("subsection_title"),
            kw.get("fulfillment", "auto"),
            kw.get("account", "tiktok"),
            kw.get("external", catalog_id),
        ),
    )


@pytest.fixture
def recon_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "recon.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _base(conn)
        _ins(conn, catalog_id="1001", name_ar="آمن", price=1.5)
        _ins(conn, catalog_id="1002", name_ar="منسّق", price=24.8791)
        _ins(conn, catalog_id="2001", name_ar="بدون حساب", account=None, price=2.0)
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
    monkeypatch.setattr(imp, "APPROVED_SMM_SERVICES_COUNT", 3)
    monkeypatch.setattr(imp, "APPROVED_ORDERS_COUNT", 1)

    result = execute_legacy_migration(path)
    assert result.verdict.startswith("MIGRATION SUCCESSFUL"), result.errors
    return path


def test_reconciliation_passes_after_import(recon_db: Path):
    report = reconcile_legacy_migration_at_path(
        recon_db, expect_approved_baseline=False
    )
    assert report.verdict == "RECONCILIATION PASSED", (
        report.hard_stop_reasons,
        report.mismatches[:5],
    )
    assert report.sections["service_identity"]["ok"] is True
    assert report.sections["prices"]["ok"] is True
    assert report.sections["execution_sources"]["ok"] is True
    assert report.sections["readiness"]["ready"] == 2
    assert report.sections["readiness"]["needs_review"] == 1
    assert report.sections["publication_mapping_isolation"]["publications"] == 0


def test_reconciliation_deterministic(recon_db: Path):
    r1, r2, same = reconcile_twice(recon_db, expect_approved_baseline=False)
    assert same is True
    assert r1.content_fingerprint == r2.content_fingerprint
    assert r1.verdict == r2.verdict


def test_reconciliation_detects_price_tamper(recon_db: Path):
    conn = sqlite3.connect(recon_db)
    row = conn.execute(
        "SELECT id FROM soldium_catalog_prices WHERE status='active' LIMIT 1"
    ).fetchone()
    assert row is not None
    conn.execute(
        "UPDATE soldium_catalog_prices SET amount_millimes=1 WHERE id=?",
        (row[0],),
    )
    conn.commit()
    conn.close()
    report = reconcile_legacy_migration_at_path(
        recon_db, expect_approved_baseline=False
    )
    assert report.verdict == "RECONCILIATION FAILED — HARD STOP"
    assert any(
        m.entity.startswith("price:")
        or "millimes" in (m.why_it_matters or "").lower()
        or "price" in (m.why_it_matters or "").lower()
        for m in report.mismatches
    )


def test_reconciliation_readonly_no_count_change(recon_db: Path):
    conn = sqlite3.connect(recon_db)
    before = {
        "svc": conn.execute("SELECT COUNT(*) FROM soldium_catalog_services").fetchone()[0],
        "smm": conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0],
        "ord": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        "pub": conn.execute("SELECT COUNT(*) FROM soldium_catalog_publications").fetchone()[0],
    }
    conn.close()
    reconcile_legacy_migration_at_path(recon_db, expect_approved_baseline=False)
    conn = sqlite3.connect(recon_db)
    after = {
        "svc": conn.execute("SELECT COUNT(*) FROM soldium_catalog_services").fetchone()[0],
        "smm": conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0],
        "ord": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        "pub": conn.execute("SELECT COUNT(*) FROM soldium_catalog_publications").fetchone()[0],
    }
    conn.close()
    assert before == after
