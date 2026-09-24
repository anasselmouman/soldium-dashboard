# -*- coding: utf-8 -*-
"""Regression: execution-source UI success path must not surface false ISE."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from catalog_core.db import catalog_transaction
from catalog_core.errors import CatalogValidationError
from catalog_core.legacy_migration import LEGACY_SERVICE_BRIDGE_TABLE
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from workspaces import catalog_ui_asset_version, page_context

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "static" / "js" / "catalog_core_ui.js"


def _js() -> str:
    return JS.read_text(encoding="utf-8")


def test_ui_confirm_isolates_aftersave_from_replace_success():
    """After HTTP 200, refresh failures must not present as replace ISE."""
    text = _js()
    assert "تم تغيير مصدر التنفيذ، لكن تعذر تحديث الشاشة" in text
    src = text.split("تأكيد الاستبدال", 1)[1]
    confirm_block = src.split("openMove", 1)[0]
    assert "execution-source" in confirm_block
    assert "return false" in confirm_block
    assert "refreshErr" in confirm_block
    assert "httpErrorMessage" in text
    assert "internal server error" in text.lower()


def test_ui_refresh_after_does_not_require_list_load_success():
    text = _js()
    assert "ignore list refresh errors" in text
    assert "await openDetails(serviceId)" in text


def test_ui_json_helper_does_not_prefer_bare_status_text_ise():
    text = _js()
    json_fn = text.split("async function json", 1)[1].split("function alertBox", 1)[0]
    assert "|| res.statusText" not in json_fn
    assert "httpErrorMessage" in text


def test_catalog_templates_cache_bust_catalog_ui_js():
    for name in (
        "catalog_services.html",
        "catalog_structure.html",
        "catalog_review.html",
    ):
        html = (ROOT / "templates" / "workspaces" / name).read_text(encoding="utf-8")
        assert "catalog_core_ui.js?v={{ catalog_ui_asset_v }}" in html


def test_page_context_includes_asset_version():
    ctx = page_context(active_nav="services", page_title="t", page_heading="h")
    assert "catalog_ui_asset_v" in ctx
    assert ctx["catalog_ui_asset_v"] == catalog_ui_asset_version()
    assert re.fullmatch(r"\d+-\d+", ctx["catalog_ui_asset_v"])


@pytest.fixture
def shared_external_db(tmp_path: Path) -> Path:
    path = tmp_path / "exec_src_ui_shared.db"
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
            CREATE INDEX idx_smm_services_provider_external
            ON smm_services (provider_slug, external_service_id);
            CREATE TABLE orders (id INTEGER PRIMARY KEY);
            CREATE TABLE providers (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                api_base_url TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1
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
            VALUES ('gozibra', 'facebook', 'FB'), ('gozibra', 'instagram', 'IG'),
                   ('gozibra', 'default', 'Default');
            CREATE TABLE catalog_services (catalog_id TEXT PRIMARY KEY, name_ar TEXT);
            CREATE TABLE catalog_nodes (id TEXT PRIMARY KEY);
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _bridge(conn, soldium_id: str, legacy_id: str, *, external: str, account: str) -> None:
    conn.execute(
        f"""
        INSERT INTO {LEGACY_SERVICE_BRIDGE_TABLE} (
            legacy_catalog_id, legacy_local_item_id, legacy_service_id,
            soldium_service_id, provider_slug, external_service_id,
            provider_api_account, legacy_fulfillment_mode,
            classification, review_codes, migration_batch_id
        ) VALUES (?, ?, ?, ?, 'gozibra', ?, ?, 'auto',
                  'SAFE', '[]', 'test-ui-src')
        """,
        (legacy_id, legacy_id, legacy_id, soldium_id, external, account),
    )


def test_occupied_external_replace_api_success_and_isolation(shared_external_db: Path):
    """A→occupied external succeeds; only A's legacy row changes; same-source unchanged."""
    with catalog_transaction(shared_external_db) as conn:
        conn.execute(
            """
            INSERT INTO smm_services (
                service_id, catalog_id, local_item_id, name_ar, local_price_dh,
                min_qty, max_qty, external_service_id, provider_slug,
                provider_api_account, is_active
            ) VALUES (
                '1154', '1154', '1154', 'Owner 1154', 5.0,
                10, 1000, '1154', 'gozibra', 'instagram', 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO smm_services (
                service_id, catalog_id, local_item_id, name_ar, local_price_dh,
                min_qty, max_qty, external_service_id, provider_slug,
                provider_api_account, is_active
            ) VALUES (
                '4210', '4210', '4210', 'Pages followers', 12.0,
                10, 1000000, '4210', 'gozibra', 'facebook', 1
            )
            """
        )
        svc = CatalogCoreService(conn)
        a = svc.create_service(name_ar="Pages followers", status="active")
        b = svc.create_service(name_ar="Owner 1154", status="active")
        _bridge(conn, a.id, "4210", external="4210", account="facebook")
        _bridge(conn, b.id, "1154", external="1154", account="instagram")
        svc.change_execution_source(
            a.id,
            provider_slug="gozibra",
            provider_account_key="facebook",
            external_service_id="4210",
        )
        svc.change_execution_source(
            b.id,
            provider_slug="gozibra",
            provider_account_key="instagram",
            external_service_id="1154",
        )
        sid_a = a.id

        free = svc.change_execution_source(
            sid_a,
            provider_slug="gozibra",
            provider_account_key="facebook",
            external_service_id="8888",
        )
        assert free.unchanged is False
        assert free.current.external_service_id == "8888"

        same = svc.change_execution_source(
            sid_a,
            provider_slug="gozibra",
            provider_account_key="facebook",
            external_service_id="8888",
        )
        assert same.unchanged is True

        occupied = svc.change_execution_source(
            sid_a,
            provider_slug="gozibra",
            provider_account_key="facebook",
            external_service_id="1154",
        )
        assert occupied.unchanged is False
        assert occupied.current.external_service_id == "1154"
        assert occupied.legacy_write_through["applied"] is True
        assert occupied.message == "تم تغيير مصدر التنفيذ"

        row_a = conn.execute(
            "SELECT external_service_id, provider_api_account FROM smm_services WHERE catalog_id='4210'"
        ).fetchone()
        row_b = conn.execute(
            "SELECT external_service_id, provider_api_account FROM smm_services WHERE catalog_id='1154'"
        ).fetchone()
        assert str(row_a["external_service_id"]) == "1154"
        assert str(row_a["provider_api_account"]) == "facebook"
        assert str(row_b["external_service_id"]) == "1154"
        assert str(row_b["provider_api_account"]) == "instagram"


def test_api_validation_error_is_readable(shared_external_db: Path):
    with catalog_transaction(shared_external_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="x", status="active")
        with pytest.raises(CatalogValidationError) as ei:
            svc.change_execution_source(
                created.id,
                provider_slug="missing-provider",
                provider_account_key="facebook",
                external_service_id="1",
            )
        assert "Internal Server Error" not in str(ei.value)
