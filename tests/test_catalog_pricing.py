# -*- coding: utf-8 -*-
"""Phase 4B — Catalog pricing tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.errors import CatalogValidationError
from catalog_core.pricing import dh_to_millimes, format_dh_amount, format_price_display
from catalog_core.schema import SOLDIUM_CATALOG_SCHEMA_VERSION, ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "catalog_pricing_test.db"
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
            INSERT INTO catalog_services VALUES ('old', 'قديم');
            CREATE TABLE price_rules (id INTEGER PRIMARY KEY);
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def test_money_conversion_exact():
    assert dh_to_millimes("0.01") == 10
    assert dh_to_millimes("0.14") == 140
    assert dh_to_millimes("1.48") == 1480
    assert dh_to_millimes("2") == 2000
    assert dh_to_millimes("2.5") == 2500
    assert dh_to_millimes("12") == 12000
    assert format_dh_amount(10) == "0.01"
    assert format_dh_amount(1480) == "1.48"
    assert format_dh_amount(2000) == "2"
    assert "1000" in format_price_display(2000, "per_1000")


def test_invalid_money_inputs():
    with pytest.raises(CatalogValidationError):
        dh_to_millimes("-1")
    with pytest.raises(CatalogValidationError):
        dh_to_millimes("abc")
    with pytest.raises(CatalogValidationError):
        dh_to_millimes("0.0001")  # finer than millime
    with pytest.raises(CatalogValidationError, match="صفر"):
        dh_to_millimes("0")
    with pytest.raises(CatalogValidationError, match="صفر"):
        dh_to_millimes("0.000")


def test_create_price(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="خدمة تسعير")
        result = svc.change_price(
            created.id, amount_dh="2", pricing_mode="per_1000", currency="MAD"
        )
        assert result.unchanged is False
        assert result.current is not None
        assert result.current.amount_millimes == 2000
        assert result.current.pricing_mode == "per_1000"
        assert result.current.currency == "MAD"
        assert result.current.status == "active"
        again = svc.get_service(created.id)
        assert again.current_price is not None
        assert again.current_price.to_dict()["display_ar"]


def test_missing_price_supported(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="بدون سعر")
        assert created.current_price is None
        assert created.to_dict()["has_price"] is False
        assert svc.get_price(created.id) is None


def test_price_change_history(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="تاريخ أسعار")
        svc.change_price(created.id, amount_dh="2", pricing_mode="per_1000")
        svc.change_price(created.id, amount_dh="2.5", pricing_mode="per_1000")
        svc.change_price(created.id, amount_dh="3", pricing_mode="per_1000")
        history = svc.list_price_history(created.id)
        assert len(history) == 3
        active = [h for h in history if h.status == "active"]
        past = [h for h in history if h.status == "historical"]
        assert len(active) == 1 and active[0].amount_millimes == 3000
        assert len(past) == 2
        assert all(p.effective_to for p in past)


def test_same_price_no_duplicate(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="نفس السعر")
        svc.change_price(created.id, amount_dh="1.48", pricing_mode="per_unit")
        again = svc.change_price(created.id, amount_dh="1.48", pricing_mode="per_unit")
        assert again.unchanged is True
        assert again.message == "السعر الحالي مطابق بالفعل"
        assert len(svc.list_price_history(created.id)) == 1


def test_one_active_price_constraint(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="قيد")
        svc.change_price(created.id, amount_dh="1", pricing_mode="per_1000")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO soldium_catalog_prices (
                    id, service_id, amount_millimes, currency, pricing_mode, status
                ) VALUES ('prc_dup', ?, 2000, 'MAD', 'per_1000', 'active')
                """,
                (created.id,),
            )


def test_price_independent_of_source_and_location(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        a = svc.create_node(name_ar="أ")
        b = svc.create_node(name_ar="ب")
        created = svc.create_service(name_ar="مستقل", parent_entry_id=a.entry_id)
        sid = created.id
        svc.change_price(sid, amount_dh="2", pricing_mode="per_1000")
        svc.change_execution_source(
            sid,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="1847",
        )
        after_source = svc.get_service(sid)
        assert after_source.current_price.amount_millimes == 2000
        assert after_source.current_source.external_service_id == "1847"

        svc.move_service(sid, new_parent_entry_id=b.entry_id)
        after_move = svc.get_service(sid)
        assert after_move.parent_entry_id == b.entry_id
        assert after_move.current_price.amount_millimes == 2000
        assert after_move.id == sid

        svc.change_price(sid, amount_dh="3", pricing_mode="per_1000")
        after_price = svc.get_service(sid)
        assert after_price.current_source.external_service_id == "1847"
        assert after_price.parent_entry_id == b.entry_id
        assert after_price.current_price.amount_millimes == 3000


def test_validation_rejects_bad_mode_currency_service(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="تحقق")
        with pytest.raises(CatalogValidationError, match="طريقة التسعير"):
            svc.change_price(created.id, amount_dh="1", pricing_mode="per_thousand")
        with pytest.raises(CatalogValidationError, match="العملة"):
            svc.change_price(
                created.id, amount_dh="1", pricing_mode="per_1000", currency="USD"
            )
        with pytest.raises(Exception):
            svc.change_price("svc_missing", amount_dh="1", pricing_mode="per_1000")


def test_price_filters(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        a = svc.create_service(name_ar="مسعّرة")
        svc.create_service(name_ar="بلا سعر")
        svc.change_price(a.id, amount_dh="0.01", pricing_mode="fixed_package")
        with_price, n1 = svc.list_services(price="assigned")
        none_price, n0 = svc.list_services(price="none")
        by_mode, n2 = svc.list_services(pricing_mode="fixed_package")
        assert n1 == 1 and with_price[0].id == a.id
        assert n0 == 1 and none_price[0].name_ar == "بلا سعر"
        assert n2 == 1


def test_isolation_legacy(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="عزل")
        svc.change_price(created.id, amount_dh="2", pricing_mode="per_1000")
        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM price_rules").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM soldium_catalog_prices").fetchone()[0]
            == 1
        )
        ver = conn.execute(
            "SELECT value FROM soldium_catalog_schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert ver == "10"
        assert SOLDIUM_CATALOG_SCHEMA_VERSION == "10"


def test_api_price_routes():
    from main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/soldium-catalog/services/{service_id}/price" in paths
    assert "/api/soldium-catalog/services/{service_id}/price/history" in paths


def test_ui_no_browser_dialogs_and_pricing_copy():
    js = Path(__file__).resolve().parent.parent / "static" / "js" / "catalog_core_ui.js"
    text = js.read_text(encoding="utf-8")
    assert "prompt(" not in text
    assert "confirm(" not in text
    assert "alert(" not in text
    assert "تعديل السعر" in text
    assert "سجل الأسعار" in text
    assert "بدون سعر" in text
