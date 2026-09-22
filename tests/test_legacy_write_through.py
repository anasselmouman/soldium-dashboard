# -*- coding: utf-8 -*-
"""Catalog Admin → Legacy smm_services write-through tests."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from catalog_core.db import catalog_transaction
from catalog_core.errors import CatalogConflictError
from catalog_core.legacy_migration import LEGACY_SERVICE_BRIDGE_TABLE
from catalog_core.legacy_write_through import (
    STATUS_TO_IS_ACTIVE,
    millimes_to_local_price_dh,
)
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService

ROOT = Path(__file__).resolve().parents[1]
BOT_ROOT = ROOT.parent / "soldium-bot"


def _seed_bridged_world(conn: sqlite3.Connection) -> tuple[str, str]:
    """Create providers, one legacy row, one Catalog service, and a bridge."""
    ensure_soldium_catalog_schema(conn)
    for account in ("default", "instagram", "facebook", "tiktok"):
        conn.execute(
            """
            INSERT OR IGNORE INTO provider_accounts(provider_slug, account_key, display_name)
            VALUES ('gozibra', ?, ?)
            """,
            (account, account.title()),
        )

    legacy_id = "wt-1154"
    conn.execute(
        """
        INSERT INTO smm_services (
            service_id, catalog_id, local_item_id, name_ar, local_price_dh,
            min_qty, max_qty, category, platform_key, platform_title,
            section_key, section_title, subsection_key, subsection_title,
            external_service_id, provider_slug, provider_api_account,
            provider_price_usd, fulfillment_mode, is_active
        ) VALUES (
            '1154', ?, ?, 'اسم قديم', 3.0,
            100, 200000, 'default', 'instagram', 'Instagram',
            'likes', 'Likes', '', '',
            '1154', 'gozibra', 'instagram',
            0.5, 'auto', 1
        )
        """,
        (legacy_id, legacy_id),
    )

    svc = CatalogCoreService(conn)
    created = svc.create_service(
        name_ar="اسم قديم",
        status="active",
        min_quantity=100,
        max_quantity=200000,
        fulfillment_mode="auto",
        service_type="likes",
        ordering_mode="quantity_based",
    )
    sid = created.id
    svc.change_price(sid, amount_dh="3", pricing_mode="per_1000")
    svc.change_execution_source(
        sid,
        provider_slug="gozibra",
        provider_account_key="instagram",
        external_service_id="1154",
    )

    conn.execute(
        f"""
        INSERT INTO {LEGACY_SERVICE_BRIDGE_TABLE} (
            legacy_catalog_id, legacy_local_item_id, legacy_service_id,
            soldium_service_id, provider_slug, external_service_id,
            provider_api_account, legacy_fulfillment_mode,
            classification, review_codes, migration_batch_id
        ) VALUES (?, ?, ?, ?, 'gozibra', '1154', 'instagram', 'auto',
                  'SAFE', '[]', 'test-wt')
        """,
        (legacy_id, legacy_id, "1154", sid),
    )
    return sid, legacy_id


@pytest.fixture
def wt_db(tmp_path: Path) -> Path:
    path = tmp_path / "write_through.db"
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


def _legacy(conn: sqlite3.Connection, legacy_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM smm_services WHERE catalog_id = ?", (legacy_id,)
    ).fetchone()
    assert row is not None
    return row


def test_status_mapping_documented():
    assert STATUS_TO_IS_ACTIVE == {"active": 1, "archived": 0, "draft": 0}


def test_millimes_to_dh_no_float_drift():
    assert millimes_to_local_price_dh(3000) == 3.0
    assert millimes_to_local_price_dh(1250) == 1.25
    assert millimes_to_local_price_dh(1) == 0.001


def test_A_edit_name_writes_legacy(wt_db: Path):
    with catalog_transaction(wt_db) as conn:
        sid, lid = _seed_bridged_world(conn)
        result = CatalogCoreService(conn).update_service(sid, name_ar="اسم جديد من الكتالوج")
        assert result.legacy_write_through["applied"] is True
        assert _legacy(conn, lid)["name_ar"] == "اسم جديد من الكتالوج"


def test_B_min_max_writes_legacy(wt_db: Path):
    with catalog_transaction(wt_db) as conn:
        sid, lid = _seed_bridged_world(conn)
        CatalogCoreService(conn).update_service(sid, min_quantity=50, max_quantity=9999)
        row = _legacy(conn, lid)
        assert row["min_qty"] == 50
        assert row["max_qty"] == 9999


def test_C_price_writes_legacy(wt_db: Path):
    with catalog_transaction(wt_db) as conn:
        sid, lid = _seed_bridged_world(conn)
        price = CatalogCoreService(conn).change_price(
            sid, amount_dh="12.5", pricing_mode="per_1000"
        )
        assert price.unchanged is False
        assert price.legacy_write_through["applied"] is True
        assert float(_legacy(conn, lid)["local_price_dh"]) == 12.5
        active = CatalogCoreService(conn).get_price(sid)
        assert active is not None
        assert active.amount_millimes == 12500


def test_D_execution_source_writes_legacy(wt_db: Path):
    with catalog_transaction(wt_db) as conn:
        sid, lid = _seed_bridged_world(conn)
        src = CatalogCoreService(conn).change_execution_source(
            sid,
            provider_slug="gozibra",
            provider_account_key="facebook",
            external_service_id="9999",
        )
        assert src.legacy_write_through["applied"] is True
        row = _legacy(conn, lid)
        assert row["provider_slug"] == "gozibra"
        assert row["provider_api_account"] == "facebook"
        assert str(row["external_service_id"]) == "9999"


def test_E_fulfillment_writes_legacy(wt_db: Path):
    with catalog_transaction(wt_db) as conn:
        sid, lid = _seed_bridged_world(conn)
        CatalogCoreService(conn).update_service(sid, fulfillment_mode="admin")
        assert _legacy(conn, lid)["fulfillment_mode"] == "admin"


def test_F_archive_deactivates_legacy(wt_db: Path):
    with catalog_transaction(wt_db) as conn:
        sid, lid = _seed_bridged_world(conn)
        CatalogCoreService(conn).archive_service(sid)
        assert _legacy(conn, lid)["is_active"] == 0


def test_G_reactivate_activates_legacy(wt_db: Path):
    with catalog_transaction(wt_db) as conn:
        sid, lid = _seed_bridged_world(conn)
        CatalogCoreService(conn).archive_service(sid)
        CatalogCoreService(conn).restore_service(sid, status="active")
        assert _legacy(conn, lid)["is_active"] == 1


def test_draft_does_not_activate_telegram(wt_db: Path):
    with catalog_transaction(wt_db) as conn:
        sid, lid = _seed_bridged_world(conn)
        CatalogCoreService(conn).update_service(sid, status="draft")
        assert _legacy(conn, lid)["is_active"] == 0


def test_H_missing_bridge_no_legacy_created(wt_db: Path):
    with catalog_transaction(wt_db) as conn:
        ensure_soldium_catalog_schema(conn)
        for account in ("default", "instagram"):
            conn.execute(
                """
                INSERT OR IGNORE INTO provider_accounts(provider_slug, account_key)
                VALUES ('gozibra', ?)
                """,
                (account,),
            )
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="كتالوج فقط", status="active")
        updated = svc.update_service(created.id, name_ar="كتالوج فقط محدث")
        assert updated.legacy_write_through["outcome"] == "skipped_no_bridge"
        assert updated.legacy_write_through["applied"] is False
        count = conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0]
        assert count == 0
        price = svc.change_price(created.id, amount_dh="9", pricing_mode="per_1000")
        assert price.legacy_write_through["outcome"] == "skipped_no_bridge"
        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == 0


def test_I_transaction_rollback_on_broken_bridge(wt_db: Path):
    with catalog_transaction(wt_db) as conn:
        sid, lid = _seed_bridged_world(conn)

    with pytest.raises(CatalogConflictError):
        with catalog_transaction(wt_db) as conn:
            conn.execute("DELETE FROM smm_services WHERE catalog_id = ?", (lid,))
            CatalogCoreService(conn).update_service(sid, name_ar="يجب أن يفشل")

    conn = sqlite3.connect(wt_db)
    conn.row_factory = sqlite3.Row
    try:
        name = conn.execute(
            "SELECT name_ar FROM soldium_catalog_services WHERE id = ?", (sid,)
        ).fetchone()["name_ar"]
        assert name == "اسم قديم"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM smm_services WHERE catalog_id = ?", (lid,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT name_ar FROM smm_services WHERE catalog_id = ?", (lid,)
            ).fetchone()["name_ar"]
            == "اسم قديم"
        )
    finally:
        conn.close()


def test_J_gozibra_account_from_catalog_source_exact(wt_db: Path):
    """Catalog-root account is authoritative — no discovery overwrite."""
    with catalog_transaction(wt_db) as conn:
        sid, lid = _seed_bridged_world(conn)
        for account in ("facebook", "tiktok", "default"):
            CatalogCoreService(conn).change_execution_source(
                sid,
                provider_slug="gozibra",
                provider_account_key=account,
                external_service_id="4242",
            )
            row = _legacy(conn, lid)
            assert row["provider_api_account"] == account
            assert str(row["external_service_id"]) == "4242"
            # Price change must not touch routing columns.
            CatalogCoreService(conn).change_price(
                sid, amount_dh="7.7", pricing_mode="per_1000"
            )
            row2 = _legacy(conn, lid)
            assert row2["provider_api_account"] == account
            assert str(row2["external_service_id"]) == "4242"
            assert float(row2["local_price_dh"]) == 7.7


@pytest.mark.skipif(not BOT_ROOT.is_dir(), reason="soldium-bot sibling missing")
def test_e2e_catalog_price_to_telegram_loader(wt_db: Path):
    """Catalog mutation → smm_services → bot services_catalog_db loader (subprocess)."""
    with catalog_transaction(wt_db) as conn:
        sid, lid = _seed_bridged_world(conn)
        CatalogCoreService(conn).change_price(
            sid, amount_dh="19.5", pricing_mode="per_1000"
        )
        CatalogCoreService(conn).change_execution_source(
            sid,
            provider_slug="gozibra",
            provider_account_key="tiktok",
            external_service_id="7777",
        )

    # Subprocess keeps bot imports isolated from dashboard ``config``/``main``.
    script = f"""
import json, sys
from pathlib import Path
sys.path.insert(0, {str(BOT_ROOT.resolve())!r})
import services_catalog_db as scdb
scdb.DB_PATH = Path({str(Path(wt_db).resolve())!r})
tree = scdb.build_services_dict_from_db()
lid = {lid!r}
found = None
for platform in tree.values():
    for section in (platform.get("sections") or {{}}).values():
        for item in section.get("items") or []:
            if str(item.get("catalog_id")) == lid or str(item.get("id")) == lid:
                found = item
                break
        if found:
            break
    if found:
        break
assert found is not None, "Telegram loader miss"
assert float(found["price"]) == 19.5
assert found.get("provider_account") == "tiktok"
assert found.get("provider_slug") == "gozibra"
assert str(found.get("external_service_id_text") or "") == "7777"
print(json.dumps({{
    "price": float(found["price"]),
    "provider_account": found.get("provider_account"),
    "provider_slug": found.get("provider_slug"),
    "external_service_id_text": found.get("external_service_id_text"),
}}))
"""
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(BOT_ROOT),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    import json

    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["price"] == 19.5
    assert payload["provider_account"] == "tiktok"
