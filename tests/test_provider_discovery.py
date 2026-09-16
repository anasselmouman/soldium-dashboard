# -*- coding: utf-8 -*-
"""Phase 6A — Provider Catalog discovery (read-only) tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.provider_discovery import (
    discover_provider_catalog,
    normalize_provider_catalog_item,
)
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from services.provider_registry import ProviderAccountRecord, ProviderRecord
from services.smm_provider import (
    ProviderAuthError,
    ProviderMalformedResponseError,
    ProviderUnavailableError,
)
from utils.provider_parse import parse_provider_external_service_id


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "catalog_discovery_test.db"
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
                adapter_type TEXT NOT NULL DEFAULT 'gozibra_v2',
                is_active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO providers(slug, name, api_base_url, adapter_type)
            VALUES ('gozibra', 'Gozibra', 'https://example.test/api/v2', 'gozibra_v2');
            INSERT INTO providers(slug, name, api_base_url, adapter_type)
            VALUES ('other', 'Other', 'https://other.test/api/v2', 'gozibra_v2');
            CREATE TABLE provider_accounts (
                id INTEGER PRIMARY KEY,
                provider_slug TEXT NOT NULL,
                account_key TEXT NOT NULL,
                api_key_env TEXT NOT NULL DEFAULT 'SMM_KEY_DEFAULT',
                display_name TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(provider_slug, account_key)
            );
            INSERT INTO provider_accounts(provider_slug, account_key, api_key_env, display_name)
            VALUES
              ('gozibra', 'default', 'SMM_KEY_DEFAULT', 'افتراضي'),
              ('gozibra', 'instagram', 'SMM_KEY_INSTAGRAM', 'انستغرام'),
              ('other', 'main', 'SMM_KEY_OTHER', 'رئيسي');
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


def _provider(slug: str = "gozibra", *, active: bool = True, adapter: str = "gozibra_v2"):
    return ProviderRecord(
        slug=slug,
        name=slug.title(),
        api_base_url="https://example.test/api/v2",
        adapter_type=adapter,
        is_active=active,
    )


def _account(slug: str = "gozibra", key: str = "default", *, active: bool = True):
    return ProviderAccountRecord(
        provider_slug=slug,
        account_key=key,
        api_key_env="SMM_KEY_DEFAULT",
        is_active=active,
        display_name=key,
    )


def _patch_registry(monkeypatch, *, provider=None, account=None):
    monkeypatch.setattr(
        "catalog_core.provider_discovery.get_provider_record",
        lambda slug: provider if provider and provider.slug == slug else None,
    )
    monkeypatch.setattr(
        "catalog_core.provider_discovery.get_provider_account_record",
        lambda slug, key: (
            account
            if account and account.provider_slug == slug and account.account_key == key
            else None
        ),
    )


def test_discovery_success(monkeypatch):
    _patch_registry(monkeypatch, provider=_provider(), account=_account())
    monkeypatch.setattr(
        "catalog_core.provider_discovery.fetch_provider_account_services",
        AsyncMock(
            return_value=[
                {
                    "service": "12345",
                    "name": "Instagram Followers",
                    "category": "Instagram",
                    "type": "Default",
                    "rate": "0.50",
                    "min": "10",
                    "max": "10000",
                    "refill": "1",
                    "cancel": "0",
                },
                {
                    "service": "abc-123",
                    "name": "Opaque ID Service",
                    "rate": "1.2",
                },
            ]
        ),
    )
    result = asyncio.run(
        discover_provider_catalog(provider_slug="gozibra", account_key="default")
    )
    assert result.ok is True
    assert result.error is None
    assert result.item_count == 2
    ids = {i.external_service_id for i in result.items}
    assert ids == {"12345", "abc-123"}
    assert all(isinstance(i.external_service_id, str) for i in result.items)
    first = next(i for i in result.items if i.external_service_id == "12345")
    assert first.provider_service_name == "Instagram Followers"
    assert first.provider_category == "Instagram"
    assert first.provider_type == "Default"
    assert first.min_quantity == 10
    assert first.max_quantity == 10000
    assert first.provider_rate == 0.5
    assert first.refill is True
    assert first.cancel is False
    payload = result.to_dict()
    assert payload["operation"] == "discovery"
    assert "api_key" not in str(payload).lower()


def test_discovery_multiple_accounts(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def _fetch(*, provider_slug: str, account_key: str) -> list[dict[str, Any]]:
        calls.append((provider_slug, account_key))
        return [{"service": f"{account_key}-1", "name": account_key}]

    def get_provider(slug: str):
        return _provider(slug) if slug == "gozibra" else None

    def get_account(slug: str, key: str):
        if slug != "gozibra":
            return None
        if key in {"default", "instagram"}:
            return _account(slug, key)
        return None

    monkeypatch.setattr(
        "catalog_core.provider_discovery.get_provider_record", get_provider
    )
    monkeypatch.setattr(
        "catalog_core.provider_discovery.get_provider_account_record", get_account
    )
    monkeypatch.setattr(
        "catalog_core.provider_discovery.fetch_provider_account_services",
        _fetch,
    )

    r1 = asyncio.run(
        discover_provider_catalog(provider_slug="gozibra", account_key="default")
    )
    r2 = asyncio.run(
        discover_provider_catalog(provider_slug="gozibra", account_key="instagram")
    )
    assert r1.ok and r2.ok
    assert r1.items[0].provider_account_key == "default"
    assert r2.items[0].provider_account_key == "instagram"
    assert calls == [("gozibra", "default"), ("gozibra", "instagram")]


def test_external_service_id_remains_string():
    item = normalize_provider_catalog_item(
        {"service": "001234", "name": "Leading zeros"},
        provider_slug="gozibra",
        provider_account_key="default",
    )
    assert item is not None
    assert item.external_service_id == "001234"
    assert isinstance(item.external_service_id, str)
    assert parse_provider_external_service_id({"service": "service_789"}) == "service_789"


def test_empty_catalog_is_success(monkeypatch):
    _patch_registry(monkeypatch, provider=_provider(), account=_account())
    monkeypatch.setattr(
        "catalog_core.provider_discovery.fetch_provider_account_services",
        AsyncMock(return_value=[]),
    )
    result = asyncio.run(
        discover_provider_catalog(provider_slug="gozibra", account_key="default")
    )
    assert result.ok is True
    assert result.item_count == 0
    assert result.error is None


def test_auth_config_failure(monkeypatch):
    _patch_registry(monkeypatch, provider=_provider(), account=_account())
    monkeypatch.setattr(
        "catalog_core.provider_discovery.fetch_provider_account_services",
        AsyncMock(side_effect=ProviderAuthError("مفتاح API غير صالح")),
    )
    result = asyncio.run(
        discover_provider_catalog(provider_slug="gozibra", account_key="default")
    )
    assert result.ok is False
    assert result.item_count == 0
    assert result.error is not None
    assert result.error.code == "auth_config"


def test_missing_env_key_failure(monkeypatch):
    _patch_registry(monkeypatch, provider=_provider(), account=_account())
    monkeypatch.setattr(
        "catalog_core.provider_discovery.fetch_provider_account_services",
        AsyncMock(
            side_effect=RuntimeError(
                "Environment variable SMM_KEY_DEFAULT is not set"
            )
        ),
    )
    result = asyncio.run(
        discover_provider_catalog(provider_slug="gozibra", account_key="default")
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "auth_config"
    assert "SMM_KEY" not in (result.error.detail or "")
    assert "api_key" not in (result.error.detail or "").lower()


def test_api_failure(monkeypatch):
    _patch_registry(monkeypatch, provider=_provider(), account=_account())
    monkeypatch.setattr(
        "catalog_core.provider_discovery.fetch_provider_account_services",
        AsyncMock(
            side_effect=ProviderUnavailableError(
                "استجابة غير متوقعة من المزوّد (رمز 500)"
            )
        ),
    )
    result = asyncio.run(
        discover_provider_catalog(provider_slug="gozibra", account_key="default")
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "api_failure"
    assert result.item_count == 0


def test_malformed_response(monkeypatch):
    _patch_registry(monkeypatch, provider=_provider(), account=_account())
    monkeypatch.setattr(
        "catalog_core.provider_discovery.fetch_provider_account_services",
        AsyncMock(side_effect=ProviderMalformedResponseError("bad json")),
    )
    result = asyncio.run(
        discover_provider_catalog(provider_slug="gozibra", account_key="default")
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "malformed"


def test_malformed_unparseable_payload(monkeypatch):
    _patch_registry(monkeypatch, provider=_provider(), account=_account())
    monkeypatch.setattr(
        "catalog_core.provider_discovery.fetch_provider_account_services",
        AsyncMock(return_value={"unexpected": True}),
    )
    result = asyncio.run(
        discover_provider_catalog(provider_slug="gozibra", account_key="default")
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "malformed"


def test_timeout_failure(monkeypatch):
    _patch_registry(monkeypatch, provider=_provider(), account=_account())
    monkeypatch.setattr(
        "catalog_core.provider_discovery.fetch_provider_account_services",
        AsyncMock(
            side_effect=ProviderUnavailableError("انتهت مهلة الاتصال بمزوّد الخدمة.")
        ),
    )
    result = asyncio.run(
        discover_provider_catalog(provider_slug="gozibra", account_key="default")
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "timeout"


def test_unsupported_adapter(monkeypatch):
    _patch_registry(
        monkeypatch,
        provider=_provider(adapter="custom_xml"),
        account=_account(),
    )
    result = asyncio.run(
        discover_provider_catalog(provider_slug="gozibra", account_key="default")
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "unsupported"


def test_discovery_does_not_mutate_database(catalog_db: Path, monkeypatch):
    _patch_registry(monkeypatch, provider=_provider(), account=_account())
    monkeypatch.setattr(
        "catalog_core.provider_discovery.fetch_provider_account_services",
        AsyncMock(
            return_value=[
                {"service": "999", "name": "Should Not Persist", "rate": "3"},
            ]
        ),
    )
    with catalog_transaction(catalog_db) as conn:
        before = {
            "services": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_services"
            ).fetchone()[0],
            "sources": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_execution_sources"
            ).fetchone()[0],
            "prices": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_prices"
            ).fetchone()[0],
            "smm": conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0],
            "orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            "accounts": conn.execute(
                "SELECT COUNT(*) FROM provider_accounts"
            ).fetchone()[0],
            "legacy": conn.execute(
                "SELECT name_ar FROM catalog_services WHERE catalog_id='old'"
            ).fetchone()[0],
        }
        svc = CatalogCoreService(conn)
        existing = svc.create_service(name_ar="موجودة مسبقاً")
        svc.change_execution_source(
            existing.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="keep-me",
        )
        svc.change_price(existing.id, amount_dh="2", pricing_mode="per_1000")

    result = asyncio.run(
        discover_provider_catalog(provider_slug="gozibra", account_key="default")
    )
    assert result.ok is True

    with catalog_transaction(catalog_db) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM soldium_catalog_services").fetchone()[0]
            == before["services"] + 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_execution_sources"
            ).fetchone()[0]
            == before["sources"] + 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM soldium_catalog_prices").fetchone()[0]
            == before["prices"] + 1
        )
        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == before[
            "smm"
        ]
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == before[
            "orders"
        ]
        assert (
            conn.execute("SELECT COUNT(*) FROM provider_accounts").fetchone()[0]
            == before["accounts"]
        )
        assert (
            conn.execute(
                "SELECT name_ar FROM catalog_services WHERE catalog_id='old'"
            ).fetchone()[0]
            == before["legacy"]
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_execution_sources WHERE external_service_id = '999'"
            ).fetchone()[0]
            == 0
        )


def test_api_discovery_route_registered():
    from main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/soldium-catalog/provider-discovery" in paths


def test_no_schema_change_for_discovery():
    from catalog_core.schema import SOLDIUM_CATALOG_SCHEMA_VERSION

    assert SOLDIUM_CATALOG_SCHEMA_VERSION == "10"
