# -*- coding: utf-8 -*-
"""Phase 4A — Catalog commercial profile tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.errors import CatalogValidationError
from catalog_core.schema import SOLDIUM_CATALOG_SCHEMA_VERSION, ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "catalog_commercial_test.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE smm_services (
                service_id TEXT PRIMARY KEY,
                name_ar TEXT NOT NULL DEFAULT 'legacy',
                min_qty INTEGER NOT NULL DEFAULT 1,
                max_qty INTEGER NOT NULL DEFAULT 1000
            );
            INSERT INTO smm_services(service_id, name_ar) VALUES ('legacy-1', 'قديم');
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                service_id TEXT NOT NULL
            );
            INSERT INTO orders(id, service_id) VALUES (1, 'legacy-1');
            CREATE TABLE providers (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                api_base_url TEXT NOT NULL DEFAULT '',
                adapter_type TEXT NOT NULL DEFAULT 'gozibra_v2',
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
            INSERT INTO provider_accounts(provider_slug, account_key, display_name)
            VALUES ('gozibra', 'default', 'افتراضي');
            CREATE TABLE catalog_services (catalog_id TEXT PRIMARY KEY, name_ar TEXT);
            INSERT INTO catalog_services(catalog_id, name_ar) VALUES ('old', 'قديم');
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def test_create_commercial_profile(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(
            name_ar="متابعون اقتصادي",
            note_ar="وصف للعميل",
            service_type="followers",
            ordering_mode="quantity_based",
            min_quantity=100,
            max_quantity=100000,
            status="draft",
        )
        assert created.name_ar == "متابعون اقتصادي"
        assert created.service_type == "followers"
        assert created.ordering_mode == "quantity_based"
        assert created.min_quantity == 100
        assert created.max_quantity == 100000
        assert created.to_dict()["service_type_label_ar"] == "متابعون"
        assert created.to_dict()["ordering_mode_label_ar"] == "حسب الكمية"


def test_update_preserves_identity_placement_source(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        node = svc.create_node(name_ar="إنستغرام")
        created = svc.create_service(
            name_ar="خدمة",
            parent_entry_id=node.entry_id,
            service_type="likes",
            min_quantity=10,
            max_quantity=500,
        )
        sid = created.id
        parent = created.parent_entry_id
        path = list(created.location_path)
        svc.change_execution_source(
            sid,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="1847",
        )
        updated = svc.update_service(
            sid,
            name_ar="خدمة محدثة",
            service_type="followers",
            min_quantity=50,
            max_quantity=9000,
            note_ar="ملاحظة",
        )
        assert updated.id == sid
        assert updated.parent_entry_id == parent
        assert updated.location_path == path
        assert updated.current_source is not None
        assert updated.current_source.external_service_id == "1847"
        assert updated.service_type == "followers"
        assert updated.min_quantity == 50


def test_empty_name_rejected(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        with pytest.raises(CatalogValidationError, match="اسم"):
            svc.create_service(name_ar="   ")
        created = svc.create_service(name_ar="صالحة")
        with pytest.raises(CatalogValidationError, match="اسم"):
            svc.update_service(created.id, name_ar="  ")


def test_invalid_service_type(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        with pytest.raises(CatalogValidationError, match="نوع"):
            svc.create_service(name_ar="س", service_type="followers_xxx")


def test_type_change_does_not_move_placement(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        a = svc.create_node(name_ar="أ")
        created = svc.create_service(
            name_ar="س", parent_entry_id=a.entry_id, service_type="views"
        )
        before = created.parent_entry_id
        after = svc.update_service(created.id, service_type="comments")
        assert after.parent_entry_id == before
        assert after.location_path == ["أ"]


def test_quantity_validation(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        with pytest.raises(CatalogValidationError, match="سالب"):
            svc.create_service(name_ar="س", min_quantity=-1, max_quantity=10)
        with pytest.raises(CatalogValidationError, match="يتجاوز"):
            svc.create_service(name_ar="س", min_quantity=100, max_quantity=50)
        ok = svc.create_service(name_ar="س", min_quantity=0, max_quantity=0)
        assert ok.min_quantity == 0 and ok.max_quantity == 0


def test_ordering_mode_validation(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        with pytest.raises(CatalogValidationError, match="طريقة الطلب"):
            svc.create_service(name_ar="س", ordering_mode="auto")
        pkg = svc.create_service(
            name_ar="باقة",
            ordering_mode="package_based",
            min_quantity=1,
            max_quantity=1,
        )
        assert pkg.ordering_mode == "package_based"


def test_status_lifecycle_preserved(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="دورة", status="draft")
        active = svc.update_service(created.id, status="active")
        assert active.status == "active"
        archived = svc.archive_service(created.id)
        assert archived.status == "archived"
        restored = svc.restore_service(created.id, status="draft")
        assert restored.status == "draft"


def test_commercial_change_does_not_alter_source(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="عزل مصدر")
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="x1",
        )
        hist_before = len(svc.list_execution_source_history(created.id))
        svc.update_service(created.id, note_ar="تغيير تجاري", min_quantity=20)
        hist_after = svc.list_execution_source_history(created.id)
        assert len(hist_after) == hist_before == 1
        assert hist_after[0].status == "active"
        assert hist_after[0].external_service_id == "x1"


def test_source_change_does_not_alter_commercial(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(
            name_ar="عزل تجاري",
            service_type="shares",
            min_quantity=5,
            max_quantity=55,
            note_ar="ثابت",
        )
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="z9",
        )
        after = svc.get_service(created.id)
        assert after.service_type == "shares"
        assert after.min_quantity == 5
        assert after.max_quantity == 55
        assert after.note_ar == "ثابت"


def test_filters_by_type_and_ordering(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        svc.create_service(name_ar="أ", service_type="likes", ordering_mode="quantity_based")
        svc.create_service(name_ar="ب", service_type="views", ordering_mode="package_based")
        likes, n = svc.list_services(service_type="likes")
        assert n == 1 and likes[0].name_ar == "أ"
        pkgs, n2 = svc.list_services(ordering_mode="package_based")
        assert n2 == 1 and pkgs[0].name_ar == "ب"


def test_schema_version_and_isolation(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        assert SOLDIUM_CATALOG_SCHEMA_VERSION == "10"
        ver = conn.execute(
            "SELECT value FROM soldium_catalog_schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert ver == "10"
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(soldium_catalog_services)").fetchall()
        }
        assert {"service_type", "ordering_mode", "min_quantity", "max_quantity"} <= cols
        CatalogCoreService(conn).create_service(name_ar="جديدة", service_type="other")
        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM catalog_services").fetchone()[0] == 1


def test_additive_migration_on_legacy_table(tmp_path: Path):
    """Simulate Phase 2 table without commercial columns, then upgrade."""
    path = tmp_path / "upgrade.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE soldium_catalog_services (
                id TEXT PRIMARY KEY,
                name_ar TEXT NOT NULL DEFAULT '',
                note_ar TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE soldium_catalog_schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO soldium_catalog_schema_meta(key, value) VALUES ('schema_version', '2');
            INSERT INTO soldium_catalog_services(id, name_ar) VALUES ('svc_old', 'قديمة');
            """
        )
        conn.commit()
    finally:
        conn.close()

    up = sqlite3.connect(path)
    try:
        ensure_soldium_catalog_schema(up)
        up.commit()
    finally:
        up.close()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(soldium_catalog_services)").fetchall()
        }
        assert "min_quantity" in cols
        row = conn.execute(
            "SELECT min_quantity, max_quantity, service_type FROM soldium_catalog_services WHERE id='svc_old'"
        ).fetchone()
        assert int(row["min_quantity"]) == 1
        assert int(row["max_quantity"]) == 1000000
        assert row["service_type"] == "other"
        ver = conn.execute(
            "SELECT value FROM soldium_catalog_schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert ver == "10"
    finally:
        conn.close()


def test_api_commercial_options_route():
    from main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/soldium-catalog/commercial-options" in paths
