# -*- coding: utf-8 -*-
"""Catalog Admin delete/archive service tests."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from catalog_core.db import catalog_transaction
from catalog_core.errors import CatalogValidationError
from catalog_core.legacy_migration import LEGACY_SERVICE_BRIDGE_TABLE
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService

ROOT = Path(__file__).resolve().parents[1]
BOT_ROOT = ROOT.parent / "soldium-bot"


def _seed(conn: sqlite3.Connection) -> tuple[str, str]:
    ensure_soldium_catalog_schema(conn)
    for account in ("default", "instagram", "facebook"):
        conn.execute(
            """
            INSERT OR IGNORE INTO provider_accounts(provider_slug, account_key, display_name)
            VALUES ('gozibra', ?, ?)
            """,
            (account, account),
        )

    legacy_id = "del-2001"
    conn.execute(
        """
        INSERT INTO smm_services (
            service_id, catalog_id, local_item_id, name_ar, local_price_dh,
            min_qty, max_qty, category, platform_key, platform_title,
            section_key, section_title, subsection_key, subsection_title,
            external_service_id, provider_slug, provider_api_account,
            provider_price_usd, fulfillment_mode, is_active
        ) VALUES (
            '2001', ?, ?, 'خدمة للحذف', 5.0,
            10, 1000, 'default', 'instagram', 'Instagram',
            'likes', 'Likes', '', '',
            '2001', 'gozibra', 'instagram',
            0.1, 'auto', 1
        )
        """,
        (legacy_id, legacy_id),
    )
    conn.execute(
        """
        INSERT INTO orders(id, service_id, catalog_id, service_name, amount)
        VALUES (1, ?, ?, 'خدمة للحذف', 5.0)
        """,
        (legacy_id, legacy_id),
    )

    core = CatalogCoreService(conn)
    created = core.create_service(
        name_ar="خدمة للحذف",
        status="active",
        min_quantity=10,
        max_quantity=1000,
        fulfillment_mode="auto",
        service_type="likes",
        ordering_mode="quantity_based",
        target_platform_key="instagram",
        target_section_key="likes",
        target_link_type="url",
    )
    sid = created.id
    core.change_price(sid, amount_dh="5", pricing_mode="per_1000")
    core.change_execution_source(
        sid,
        provider_slug="gozibra",
        provider_account_key="instagram",
        external_service_id="2001",
    )
    # Historical price + source already exist; add another price for history depth.
    core.change_price(sid, amount_dh="6", pricing_mode="per_1000")
    CatalogPublicationService(conn).publish(sid, published_by="test")

    conn.execute(
        f"""
        INSERT INTO {LEGACY_SERVICE_BRIDGE_TABLE} (
            legacy_catalog_id, legacy_local_item_id, legacy_service_id,
            soldium_service_id, provider_slug, external_service_id,
            provider_api_account, legacy_fulfillment_mode,
            classification, review_codes, migration_batch_id
        ) VALUES (?, ?, ?, ?, 'gozibra', '2001', 'instagram', 'auto',
                  'SAFE', '[]', 'test-del')
        """,
        (legacy_id, legacy_id, "2001", sid),
    )
    return sid, legacy_id


@pytest.fixture
def del_db(tmp_path: Path) -> Path:
    path = tmp_path / "delete_service.db"
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
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                service_id TEXT NOT NULL,
                catalog_id TEXT,
                service_name TEXT NOT NULL DEFAULT '',
                amount REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE scheduled_orders (
                id INTEGER PRIMARY KEY,
                service_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE providers (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                api_base_url TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO providers(slug, name, api_base_url)
            VALUES ('gozibra', 'Gozibra', 'https://example.test');
            CREATE TABLE provider_accounts (
                id INTEGER PRIMARY KEY,
                provider_slug TEXT NOT NULL,
                account_key TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                UNIQUE(provider_slug, account_key)
            );
            CREATE TABLE catalog_services (catalog_id TEXT PRIMARY KEY, name_ar TEXT);
            CREATE TABLE catalog_nodes (id TEXT PRIMARY KEY);
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def test_1_delete_archives_active_service(del_db: Path):
    with catalog_transaction(del_db) as conn:
        sid, lid = _seed(conn)
        result = CatalogCoreService(conn).delete_service(sid)
        assert result.physical_delete is False
        assert result.mode == "archive"
        assert result.service.status == "archived"
        assert conn.execute(
            "SELECT status FROM soldium_catalog_services WHERE id=?", (sid,)
        ).fetchone()[0] == "archived"
        assert conn.execute(
            "SELECT is_active FROM smm_services WHERE catalog_id=?", (lid,)
        ).fetchone()[0] == 0


def test_2_api_routes_registered():
    from main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/soldium-catalog/services/{service_id}/delete" in paths
    assert "/api/soldium-catalog/services/{service_id}/delete-preview" in paths


def test_3_4_catalog_and_legacy(del_db: Path):
    with catalog_transaction(del_db) as conn:
        sid, lid = _seed(conn)
        CatalogCoreService(conn).delete_service(sid)
        assert (
            conn.execute(
                "SELECT status FROM soldium_catalog_services WHERE id=?", (sid,)
            ).fetchone()[0]
            == "archived"
        )
        assert (
            conn.execute(
                "SELECT is_active FROM smm_services WHERE catalog_id=?", (lid,)
            ).fetchone()[0]
            == 0
        )


@pytest.mark.skipif(not BOT_ROOT.is_dir(), reason="soldium-bot sibling missing")
def test_5_telegram_loader_hides_service(del_db: Path):
    with catalog_transaction(del_db) as conn:
        sid, lid = _seed(conn)
        CatalogCoreService(conn).delete_service(sid)

    script = f"""
import json, sys
from pathlib import Path
sys.path.insert(0, {str(BOT_ROOT.resolve())!r})
import services_catalog_db as scdb
scdb.DB_PATH = Path({str(Path(del_db).resolve())!r})
tree = scdb.build_services_dict_from_db()
lid = {lid!r}
found = False
for platform in tree.values():
    for section in (platform.get("sections") or {{}}).values():
        for item in section.get("items") or []:
            if str(item.get("catalog_id")) == lid or str(item.get("id")) == lid:
                found = True
print(json.dumps({{"found": found}}))
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(BOT_ROOT),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["found"] is False


def test_6_7_8_9_history_preserved(del_db: Path):
    with catalog_transaction(del_db) as conn:
        sid, lid = _seed(conn)
        orders_before = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        prices_before = conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_prices WHERE service_id=?", (sid,)
        ).fetchone()[0]
        sources_before = conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_execution_sources WHERE service_id=?",
            (sid,),
        ).fetchone()[0]
        pubs_before = conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_publications WHERE service_id=?",
            (sid,),
        ).fetchone()[0]
        order_row = dict(
            conn.execute("SELECT * FROM orders WHERE id=1").fetchone()
        )

        CatalogCoreService(conn).delete_service(sid)

        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == orders_before
        assert dict(conn.execute("SELECT * FROM orders WHERE id=1").fetchone()) == order_row
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_prices WHERE service_id=?", (sid,)
            ).fetchone()[0]
            == prices_before
        )
        assert prices_before >= 2
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_execution_sources WHERE service_id=?",
                (sid,),
            ).fetchone()[0]
            == sources_before
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_publications WHERE service_id=?",
                (sid,),
            ).fetchone()[0]
            == pubs_before
        )
        assert pubs_before >= 1
        # Bridge and legacy row remain
        assert (
            conn.execute(
                f"SELECT COUNT(*) FROM {LEGACY_SERVICE_BRIDGE_TABLE} WHERE soldium_service_id=?",
                (sid,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM smm_services WHERE catalog_id=?", (lid,)
            ).fetchone()[0]
            == 1
        )


def test_10_missing_bridge_still_archives(del_db: Path):
    with catalog_transaction(del_db) as conn:
        ensure_soldium_catalog_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO provider_accounts(provider_slug, account_key) "
            "VALUES ('gozibra', 'default')"
        )
        core = CatalogCoreService(conn)
        created = core.create_service(name_ar="كتالوج فقط", status="active")
        result = core.delete_service(created.id)
        assert result.service.status == "archived"
        assert result.service.legacy_write_through["outcome"] == "skipped_no_bridge"
        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == 0


def test_11_rollback_when_legacy_row_missing(del_db: Path):
    with catalog_transaction(del_db) as conn:
        sid, lid = _seed(conn)

    with pytest.raises(Exception):
        with catalog_transaction(del_db) as conn:
            conn.execute("DELETE FROM smm_services WHERE catalog_id=?", (lid,))
            CatalogCoreService(conn).delete_service(sid)

    conn = sqlite3.connect(del_db)
    conn.row_factory = sqlite3.Row
    try:
        assert (
            conn.execute(
                "SELECT status FROM soldium_catalog_services WHERE id=?", (sid,)
            ).fetchone()[0]
            == "active"
        )
        assert (
            conn.execute(
                "SELECT is_active FROM smm_services WHERE catalog_id=?", (lid,)
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_12_already_archived(del_db: Path):
    with catalog_transaction(del_db) as conn:
        sid, _lid = _seed(conn)
        CatalogCoreService(conn).delete_service(sid)
        with pytest.raises(CatalogValidationError, match="محذوفة بالفعل"):
            CatalogCoreService(conn).delete_service(sid)


def test_13_restore_reactivates_legacy(del_db: Path):
    with catalog_transaction(del_db) as conn:
        sid, lid = _seed(conn)
        CatalogCoreService(conn).delete_service(sid)
        restored = CatalogCoreService(conn).restore_service(sid, status="active")
        assert restored.status == "active"
        assert (
            conn.execute(
                "SELECT is_active FROM smm_services WHERE catalog_id=?", (lid,)
            ).fetchone()[0]
            == 1
        )


def test_preview_lists_dependencies(del_db: Path):
    with catalog_transaction(del_db) as conn:
        sid, _lid = _seed(conn)
        preview = CatalogCoreService(conn).preview_service_delete(sid)
        assert preview["physical_delete"] is False
        assert preview["mode"] == "archive"
        assert preview["orders_count"] >= 1
        assert preview["has_legacy_bridge"] is True
        assert preview["publication_records_count"] >= 1
        assert preview["price_records_count"] >= 2
        assert any("طلبات" in w for w in preview["warnings"])


def test_ui_declares_delete_action():
    js = (ROOT / "static" / "js" / "catalog_core_ui.js").read_text(encoding="utf-8")
    assert "حذف الخدمة" in js
    assert "/delete-preview" in js
    assert "/delete" in js
    assert "btn-confirm-delete" in js
