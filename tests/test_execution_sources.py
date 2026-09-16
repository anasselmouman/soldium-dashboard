# -*- coding: utf-8 -*-
"""Phase 3 — Catalog execution sources tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.errors import CatalogNotFoundError, CatalogValidationError
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "catalog_exec_test.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE smm_services (
                service_id TEXT PRIMARY KEY,
                name_ar TEXT NOT NULL DEFAULT 'legacy'
            );
            INSERT INTO smm_services(service_id, name_ar) VALUES ('legacy-1', 'قديم');
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                service_id TEXT NOT NULL,
                service_name TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO orders(id, service_id, service_name) VALUES (1, 'legacy-1', 'قديم');
            CREATE TABLE providers (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                api_base_url TEXT NOT NULL DEFAULT '',
                adapter_type TEXT NOT NULL DEFAULT 'gozibra_v2',
                is_active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO providers(slug, name, api_base_url) VALUES
              ('gozibra', 'Gozibra', 'https://example.test/api'),
              ('other', 'Provider B', 'https://other.test/api');
            CREATE TABLE provider_accounts (
                id INTEGER PRIMARY KEY,
                provider_slug TEXT NOT NULL,
                account_key TEXT NOT NULL DEFAULT 'default',
                api_key_env TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(provider_slug, account_key),
                FOREIGN KEY(provider_slug) REFERENCES providers(slug)
            );
            INSERT INTO provider_accounts(provider_slug, account_key, display_name) VALUES
              ('gozibra', 'account_2', 'الحساب 2'),
              ('gozibra', 'default', 'افتراضي'),
              ('other', 'account_1', 'الحساب 1');
            CREATE TABLE catalog_nodes (id TEXT PRIMARY KEY, title TEXT);
            INSERT INTO catalog_nodes(id, title) VALUES ('old', 'قديم');
            CREATE TABLE catalog_services (catalog_id TEXT PRIMARY KEY, name_ar TEXT);
            INSERT INTO catalog_services(catalog_id, name_ar) VALUES ('old-svc', 'قديم');
            CREATE TABLE service_provider_bindings (id INTEGER PRIMARY KEY);
            CREATE TABLE provider_inventory (id INTEGER PRIMARY KEY);
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def test_assign_source_becomes_active(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="متابعون اقتصادي")
        result = svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="account_2",
            external_service_id="1847",
        )
        assert result.unchanged is False
        assert result.current is not None
        assert result.current.status == "active"
        assert result.current.external_service_id == "1847"
        assert result.current.provider_slug == "gozibra"
        assert result.current.provider_account_key == "account_2"
        again = svc.get_service(created.id)
        assert again.current_source is not None
        assert again.current_source.external_service_id == "1847"


def test_service_identity_stable_after_source_change(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        node = svc.create_node(name_ar="إنستغرام")
        created = svc.create_service(
            name_ar="خدمة ثابتة", parent_entry_id=node.entry_id
        )
        sid = created.id
        path_before = list(created.location_path)
        svc.change_execution_source(
            sid,
            provider_slug="gozibra",
            provider_account_key="account_2",
            external_service_id="1847",
        )
        svc.change_execution_source(
            sid,
            provider_slug="other",
            provider_account_key="account_1",
            external_service_id="9281",
        )
        after = svc.get_service(sid)
        assert after.id == sid
        assert after.location_path == path_before
        assert after.current_source.external_service_id == "9281"


def test_location_unchanged_when_source_changes(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        a = svc.create_node(name_ar="أ")
        b = svc.create_node(name_ar="ب", parent_entry_id=a.entry_id)
        created = svc.create_service(name_ar="س", parent_entry_id=b.entry_id)
        parent_before = created.parent_entry_id
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="x1",
        )
        after = svc.get_service(created.id)
        assert after.parent_entry_id == parent_before
        assert after.location_path == ["أ", "ب"]


def test_history_preserved_on_change(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="تاريخ")
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="account_2",
            external_service_id="1847",
        )
        svc.change_execution_source(
            created.id,
            provider_slug="other",
            provider_account_key="account_1",
            external_service_id="9281",
        )
        history = svc.list_execution_source_history(created.id)
        assert len(history) == 2
        active = [h for h in history if h.status == "active"]
        past = [h for h in history if h.status == "historical"]
        assert len(active) == 1
        assert len(past) == 1
        assert active[0].external_service_id == "9281"
        assert past[0].external_service_id == "1847"
        assert past[0].ended_at is not None


def test_same_source_no_duplicate_history(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="نفس المصدر")
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="account_2",
            external_service_id="1847",
        )
        again = svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="account_2",
            external_service_id="1847",
        )
        assert again.unchanged is True
        assert again.message == "المصدر المحدد مستخدم بالفعل"
        assert len(svc.list_execution_source_history(created.id)) == 1


def test_validation_rejects_bad_refs(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="تحقق")
        with pytest.raises(CatalogNotFoundError):
            svc.change_execution_source(
                "svc_missing",
                provider_slug="gozibra",
                provider_account_key="account_2",
                external_service_id="1",
            )
        with pytest.raises(CatalogValidationError, match="المورد"):
            svc.change_execution_source(
                created.id,
                provider_slug="nope",
                provider_account_key="account_2",
                external_service_id="1",
            )
        with pytest.raises(CatalogValidationError, match="لا ينتمي|غير موجود"):
            svc.change_execution_source(
                created.id,
                provider_slug="gozibra",
                provider_account_key="account_1",  # belongs to other
                external_service_id="1",
            )
        with pytest.raises(CatalogValidationError, match="معرّف"):
            svc.change_execution_source(
                created.id,
                provider_slug="gozibra",
                provider_account_key="account_2",
                external_service_id="   ",
            )


def test_opaque_external_ids(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="opaque")
        for eid in ("1847", "service_1847", "abc-1847"):
            result = svc.change_execution_source(
                created.id,
                provider_slug="gozibra",
                provider_account_key="default",
                external_service_id=eid,
            )
            assert result.current.external_service_id == eid
            assert isinstance(result.current.external_service_id, str)


def test_one_active_source_constraint(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="قيود")
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="1",
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO soldium_catalog_execution_sources (
                    id, service_id, provider_slug, provider_account_key,
                    external_service_id, status
                ) VALUES ('src_dup', ?, 'other', 'account_1', '2', 'active')
                """,
                (created.id,),
            )


def test_source_filter_list(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        a = svc.create_service(name_ar="مع مصدر")
        svc.create_service(name_ar="بدون مصدر")
        svc.change_execution_source(
            a.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="99",
        )
        with_src, n1 = svc.list_services(source="assigned")
        without, n0 = svc.list_services(source="none")
        assert n1 == 1 and with_src[0].id == a.id
        assert n0 == 1 and without[0].name_ar == "بدون مصدر"


def test_isolation_unchanged(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="عزل")
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="x",
        )
        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM provider_accounts").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM catalog_services").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_execution_sources"
            ).fetchone()[0]
            == 1
        )


def test_api_routes_registered():
    from main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/soldium-catalog/services/{service_id}/execution-source" in paths
    assert (
        "/api/soldium-catalog/services/{service_id}/execution-source/history" in paths
    )


def test_ui_has_no_browser_dialogs():
    js = Path(__file__).resolve().parent.parent / "static" / "js" / "catalog_core_ui.js"
    text = js.read_text(encoding="utf-8")
    assert "prompt(" not in text
    assert "confirm(" not in text
    assert "alert(" not in text
    assert "مصدر التنفيذ" in text
    assert "تأكيد الاستبدال" in text or "تأكيد تغيير المصدر" in text
    assert "معرّف المزود" in text
    assert "استبدال مصدر التنفيذ" in text
