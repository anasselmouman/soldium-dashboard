# -*- coding: utf-8 -*-
"""Phase 8G — order execution snapshot protection tests (isolated DB)."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from utils.order_execution_identity import (
    InvalidProviderExternalServiceId,
    encode_provider_external_service_id_for_wire,
    is_gen1_execution_order,
    is_seed_demo_service_id,
)


def test_encode_provider_external_rejects_invalid():
    with pytest.raises(InvalidProviderExternalServiceId):
        encode_provider_external_service_id_for_wire("abc123")
    with pytest.raises(InvalidProviderExternalServiceId):
        encode_provider_external_service_id_for_wire("123-foo")
    with pytest.raises(InvalidProviderExternalServiceId):
        encode_provider_external_service_id_for_wire("")
    with pytest.raises(InvalidProviderExternalServiceId):
        encode_provider_external_service_id_for_wire(0)
    with pytest.raises(InvalidProviderExternalServiceId):
        encode_provider_external_service_id_for_wire("0")
    assert encode_provider_external_service_id_for_wire("123") == "123"
    assert encode_provider_external_service_id_for_wire("00123") == "00123"


def test_seed_demo_detection():
    assert is_seed_demo_service_id("seed_done_0")
    assert is_seed_demo_service_id("extra_laila")
    assert not is_seed_demo_service_id("4210")


def test_gen1_detection():
    assert is_gen1_execution_order({"external_service_id_snapshot": "999"})
    assert not is_gen1_execution_order({"external_service_id_snapshot": None})
    assert not is_gen1_execution_order({})


@pytest.fixture
def order_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "orders8g.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            balance REAL NOT NULL DEFAULT 1000,
            total_spent REAL NOT NULL DEFAULT 0,
            telegram_name TEXT
        );
        INSERT INTO users(user_id, balance, telegram_name) VALUES (1, 1000, 't');
        CREATE TABLE providers (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            api_base_url TEXT NOT NULL DEFAULT 'https://example.test',
            adapter_type TEXT NOT NULL DEFAULT 'gozibra_v2'
        );
        INSERT INTO providers(slug, name) VALUES ('gozibra', 'G');
        CREATE TABLE provider_accounts (
            provider_slug TEXT NOT NULL,
            account_key TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            api_key_env TEXT NOT NULL DEFAULT 'GOZIBRA_API_KEY',
            is_active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(provider_slug, account_key)
        );
        INSERT INTO provider_accounts(provider_slug, account_key, api_key_env)
        VALUES ('gozibra', 'default', 'GOZIBRA_API_KEY');
        CREATE TABLE smm_services (
            catalog_id TEXT PRIMARY KEY,
            external_service_id TEXT NOT NULL,
            service_id TEXT NOT NULL DEFAULT '',
            local_item_id TEXT NOT NULL DEFAULT '',
            provider_slug TEXT NOT NULL DEFAULT 'gozibra',
            provider_api_account TEXT,
            name_ar TEXT NOT NULL DEFAULT '',
            local_price_dh REAL NOT NULL DEFAULT 1,
            provider_price_usd REAL NOT NULL DEFAULT 0.1,
            min_qty INTEGER NOT NULL DEFAULT 1,
            max_qty INTEGER NOT NULL DEFAULT 10000,
            is_active INTEGER NOT NULL DEFAULT 1,
            platform_key TEXT NOT NULL DEFAULT 'tiktok',
            section_key TEXT,
            subsection_key TEXT,
            category TEXT NOT NULL DEFAULT '',
            fulfillment_mode TEXT NOT NULL DEFAULT 'auto'
        );
        INSERT INTO smm_services (
            catalog_id, external_service_id, service_id, local_item_id,
            provider_api_account, name_ar, local_price_dh
        ) VALUES (
            'cat-100', '555', '100', '100',
            'default', 'خدمة', 10.0
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            service_id TEXT NOT NULL,
            link TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            total_price REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            provider_order_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            service_name TEXT NOT NULL DEFAULT '',
            amount REAL NOT NULL DEFAULT 0.0,
            api_account TEXT NOT NULL DEFAULT 'default',
            fulfillment_mode TEXT NOT NULL DEFAULT 'auto',
            provider_cost_dh REAL NOT NULL DEFAULT 0,
            provider_slug TEXT NOT NULL DEFAULT 'gozibra',
            catalog_id TEXT,
            external_service_id_snapshot TEXT,
            refunded_amount REAL NOT NULL DEFAULT 0,
            status_note TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("SOLDIUM_DB_PATH", str(path))
    import database_connector as dc

    monkeypatch.setattr(dc, "DB_PATH", path)
    return path


def test_ensure_gen1_uses_snapshot_not_live(order_db: Path, monkeypatch):
    conn = sqlite3.connect(order_db)
    conn.execute(
        """
        INSERT INTO orders (
            user_id, service_name, service_id, link, quantity, amount, total_price,
            status, api_account, provider_slug, fulfillment_mode,
            catalog_id, external_service_id_snapshot
        ) VALUES (
            1, 'خدمة', '100', 'https://x.test', 10, 1.0, 1.0,
            'pending_admin', 'default', 'gozibra', 'admin',
            'cat-100', '777'
        )
        """
    )
    conn.execute(
        "UPDATE smm_services SET external_service_id='999', provider_api_account='tiktok' WHERE catalog_id='cat-100'"
    )
    conn.commit()
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    conn.close()

    captured: dict = {}

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return "REF-GEN1"

    monkeypatch.setenv("GOZIBRA_API_KEY", "test-key-not-placeholder")

    async def _run():
        with patch("manual_orders.submit_provider_order", AsyncMock(side_effect=fake_submit)):
            from manual_orders import ensure_provider_order_ref, get_manual_order

            order = await get_manual_order(oid)
            assert order is not None
            assert order["external_service_id_snapshot"] == "777"
            ref = await ensure_provider_order_ref(order)
            assert ref == "REF-GEN1"
            assert captured["service_id"] == "777"
            assert captured["account_key"] == "default"
            assert captured["provider_slug"] == "gozibra"

    asyncio.run(_run())


def test_ensure_gen0_missing_ref_needs_review(order_db: Path):
    conn = sqlite3.connect(order_db)
    conn.execute(
        """
        INSERT INTO orders (
            user_id, service_name, service_id, link, quantity, amount, total_price,
            status, api_account, provider_slug, fulfillment_mode
        ) VALUES (
            1, 'خدمة', '100', 'https://x.test', 10, 1.0, 1.0,
            'pending_admin', 'default', 'gozibra', 'admin'
        )
        """
    )
    conn.commit()
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    conn.close()

    async def _run():
        from manual_orders import ensure_provider_order_ref, get_manual_order

        order = await get_manual_order(oid)
        assert order["external_service_id_snapshot"] is None
        with patch("manual_orders.submit_provider_order", AsyncMock()) as mock_sub:
            ref = await ensure_provider_order_ref(order)
            assert ref is None
            mock_sub.assert_not_called()

    asyncio.run(_run())


def test_ensure_seed_blocked(order_db: Path):
    conn = sqlite3.connect(order_db)
    conn.execute(
        """
        INSERT INTO orders (
            user_id, service_name, service_id, link, quantity, amount, total_price,
            status, api_account, provider_slug, fulfillment_mode,
            external_service_id_snapshot
        ) VALUES (
            1, 'demo', 'seed_done_0', 'https://demo', 10, 1.0, 1.0,
            'pending_admin', 'default', 'gozibra', 'admin',
            '777'
        )
        """
    )
    conn.commit()
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    conn.close()

    async def _run():
        from manual_orders import ensure_provider_order_ref, get_manual_order

        order = await get_manual_order(oid)
        with patch("manual_orders.submit_provider_order", AsyncMock()) as mock_sub:
            assert await ensure_provider_order_ref(order) is None
            mock_sub.assert_not_called()

    asyncio.run(_run())


def test_ensure_gen1_invalid_snapshot_fails_closed(order_db: Path):
    conn = sqlite3.connect(order_db)
    conn.execute(
        """
        INSERT INTO orders (
            user_id, service_name, service_id, link, quantity, amount, total_price,
            status, api_account, provider_slug, fulfillment_mode,
            external_service_id_snapshot
        ) VALUES (
            1, 'خدمة', '100', 'https://x.test', 10, 1.0, 1.0,
            'pending_admin', 'default', 'gozibra', 'admin',
            'abc123'
        )
        """
    )
    conn.commit()
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    conn.close()

    async def _run():
        from manual_orders import ensure_provider_order_ref, get_manual_order

        order = await get_manual_order(oid)
        with patch("manual_orders.submit_provider_order", AsyncMock()) as mock_sub:
            assert await ensure_provider_order_ref(order) is None
            mock_sub.assert_not_called()

    asyncio.run(_run())


def test_ensure_gen0_with_provider_ref_tracking_only(order_db: Path):
    conn = sqlite3.connect(order_db)
    conn.execute(
        """
        INSERT INTO orders (
            user_id, service_name, service_id, link, quantity, amount, total_price,
            status, api_account, provider_slug, fulfillment_mode, provider_order_id
        ) VALUES (
            1, 'خدمة', '100', 'https://x.test', 10, 1.0, 1.0,
            'pending_admin', 'default', 'gozibra', 'admin', 'EXISTING'
        )
        """
    )
    conn.commit()
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    conn.close()

    async def _run():
        from manual_orders import ensure_provider_order_ref, get_manual_order

        order = await get_manual_order(oid)
        with patch("manual_orders.submit_provider_order", AsyncMock()) as mock_sub:
            assert await ensure_provider_order_ref(order) == "EXISTING"
            mock_sub.assert_not_called()

    asyncio.run(_run())


def test_submit_provider_order_rejects_invalid_no_zero(order_db: Path, monkeypatch):
    from services.smm_provider import ProviderUnavailableError, submit_provider_order

    monkeypatch.setenv("GOZIBRA_API_KEY", "test-key-not-placeholder")

    async def _run():
        with pytest.raises(ProviderUnavailableError):
            await submit_provider_order(
                provider_slug="gozibra",
                account_key="default",
                service_id="abc",
                link="https://x",
                quantity=1,
            )

    asyncio.run(_run())
