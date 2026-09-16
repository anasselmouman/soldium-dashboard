# -*- coding: utf-8 -*-
"""Phase 9B.8 — Scheduled Gen-1 snapshot independence tests."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import database_connector as db_conn
from db_schema import ensure_scheduled_orders_tables
from scheduled_orders import (
    QUANTITY_FIXED,
    ScheduledOrderValidationError,
    create_scheduled_order,
    execute_scheduled_order,
)
from services.provider_registry import clear_provider_caches
from utils.order_execution_identity import is_gen1_scheduled_job


def _seed_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                balance REAL NOT NULL DEFAULT 1000,
                total_spent REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE providers (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                api_base_url TEXT NOT NULL DEFAULT 'https://example.test',
                adapter_type TEXT NOT NULL DEFAULT 'gozibra_v2',
                is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE provider_accounts (
                id INTEGER PRIMARY KEY,
                provider_slug TEXT NOT NULL,
                account_key TEXT NOT NULL,
                api_key_env TEXT NOT NULL DEFAULT 'TEST_PROVIDER_KEY',
                is_active INTEGER NOT NULL DEFAULT 1,
                display_name TEXT NOT NULL DEFAULT '',
                UNIQUE(provider_slug, account_key)
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                service_name TEXT NOT NULL DEFAULT 'Test Service',
                service_id TEXT NOT NULL DEFAULT 'svc-1',
                link TEXT NOT NULL DEFAULT 'https://example.com/post',
                quantity INTEGER NOT NULL DEFAULT 100,
                amount REAL NOT NULL DEFAULT 10,
                total_price REAL NOT NULL DEFAULT 10,
                status TEXT NOT NULL DEFAULT 'completed',
                provider_order_id TEXT,
                fulfillment_mode TEXT NOT NULL DEFAULT 'auto',
                provider_slug TEXT NOT NULL DEFAULT 'gozibra',
                api_account TEXT NOT NULL DEFAULT 'default',
                provider_cost_dh REAL NOT NULL DEFAULT 0,
                catalog_id TEXT,
                external_service_id_snapshot TEXT,
                normalized_link TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_active_normalized_link
            ON orders (normalized_link)
            WHERE normalized_link IS NOT NULL
              AND TRIM(normalized_link) != ''
              AND LOWER(REPLACE(status, '_', ' ')) IN (
                  'pending', 'pending admin', 'submitted', 'in progress', 'processing'
              );
            CREATE TABLE smm_services (
                service_id TEXT PRIMARY KEY,
                catalog_id TEXT,
                local_item_id TEXT,
                external_service_id TEXT,
                name_ar TEXT NOT NULL DEFAULT '',
                provider_price_usd REAL NOT NULL DEFAULT 1,
                local_price_dh REAL NOT NULL DEFAULT 14,
                min_qty INTEGER NOT NULL DEFAULT 10,
                max_qty INTEGER NOT NULL DEFAULT 10000,
                is_active INTEGER NOT NULL DEFAULT 1,
                provider_slug TEXT NOT NULL DEFAULT 'gozibra',
                provider_api_account TEXT NOT NULL DEFAULT 'default',
                fulfillment_mode TEXT NOT NULL DEFAULT 'auto'
            );
            INSERT INTO users (user_id, balance) VALUES (999001, 500.0);
            INSERT INTO providers (slug, name, api_base_url, is_active)
            VALUES ('gozibra', 'Gozibra', 'https://example.test', 1);
            INSERT INTO provider_accounts
                (provider_slug, account_key, api_key_env, is_active)
            VALUES ('gozibra', 'default', 'TEST_PROVIDER_KEY', 1);
            INSERT INTO orders (
                user_id, service_name, service_id, link, quantity, amount, total_price,
                status, provider_order_id, fulfillment_mode, provider_slug, api_account,
                catalog_id, external_service_id_snapshot
            ) VALUES (
                123, 'Test', 'svc-1', 'https://example.com/x', 50, 5.0, 5.0,
                'completed', 'PROV-999', 'auto', 'gozibra', 'default',
                'svc-1', '12345'
            );
            INSERT INTO smm_services (
                service_id, catalog_id, local_item_id, external_service_id,
                provider_price_usd, local_price_dh, min_qty, max_qty,
                provider_slug, provider_api_account, fulfillment_mode
            ) VALUES (
                'gozibra-1', 'svc-1', 'svc-1', '12345',
                1.0, 14.0, 10, 10000, 'gozibra', 'default', 'auto'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "users.db"
    _seed_db(db_path)
    monkeypatch.setattr(db_conn, "DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_ID", "999001")
    monkeypatch.setenv("TEST_PROVIDER_KEY", "test-key-not-placeholder")
    import config as dashboard_config
    import scheduled_orders as so_module

    monkeypatch.setattr(dashboard_config, "ADMIN_TELEGRAM_ID", 999001)
    monkeypatch.setattr(so_module, "ADMIN_TELEGRAM_ID", 999001)
    clear_provider_caches()
    yield db_path
    clear_provider_caches()


def test_create_freezes_execution_fulfillment_target(temp_db: Path) -> None:
    async def _run() -> None:
        await ensure_scheduled_orders_tables()
        job = await create_scheduled_order(
            template_order_ref="PROV-999",
            quantity_mode=QUANTITY_FIXED,
            quantity_fixed=60,
            interval_days=1,
        )
        assert is_gen1_scheduled_job(job)
        assert job["execution_generation"] == "gen1"
        assert job["external_service_id"] == "12345"
        assert job["fulfillment_mode"] == "auto"
        assert job["link"] == "https://example.com/x"
        assert job["provider_slug"] == "gozibra"
        assert job["api_account"] == "default"

    asyncio.run(_run())


def test_gen1_materialize_ignores_legacy_sku_mutation(temp_db: Path) -> None:
    async def _run() -> None:
        await ensure_scheduled_orders_tables()
        job = await create_scheduled_order(
            template_order_ref="PROV-999",
            quantity_mode=QUANTITY_FIXED,
            quantity_fixed=50,
            interval_days=1,
        )
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                "UPDATE scheduled_orders SET next_run_at = ? WHERE id = ?",
                (past, job["id"]),
            )
            # Poison Legacy SKU + account + fulfillment after freeze.
            conn.execute(
                """
                UPDATE smm_services
                SET external_service_id = '99999',
                    provider_api_account = 'tiktok',
                    fulfillment_mode = 'admin'
                WHERE catalog_id = 'svc-1'
                """
            )
            conn.commit()
        finally:
            conn.close()

        mock_svc = {
            "catalog_id": "svc-1",
            "local_price_dh": 14.0,
            "provider_price_usd": 1.0,
            "price_per_unit": False,
        }
        lookup = AsyncMock(
            return_value={
                "external_service_id": "99999",
                "provider_slug": "gozibra",
                "account_key": "tiktok",
            }
        )
        submit = AsyncMock(return_value="prov-gen1")

        with patch("scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)), patch(
            "scheduled_orders._lookup_service_provider_meta", new=lookup
        ), patch("scheduled_orders.submit_provider_order", new=submit):
            result = await execute_scheduled_order(int(job["id"]))

        assert result["ok"] is True
        lookup.assert_not_called()
        submit.assert_awaited()
        kwargs = submit.await_args.kwargs
        assert kwargs["service_id"] == "12345"
        assert kwargs["account_key"] == "default"
        assert kwargs["provider_slug"] == "gozibra"
        assert kwargs["link"] == "https://example.com/x"

        conn = sqlite3.connect(temp_db)
        try:
            order = conn.execute(
                """
                SELECT external_service_id_snapshot, fulfillment_mode, api_account, link
                FROM orders WHERE id = ?
                """,
                (result["order_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert order[0] == "12345"
        assert order[1] == "auto"
        assert order[2] == "default"
        assert order[3] == "https://example.com/x"

    asyncio.run(_run())


def test_gen0_execution_fails_closed_no_live_lookup(temp_db: Path) -> None:
    """Phase 9O: Gen-0 scheduled materialize must not rescue via live smm_services."""

    async def _run() -> None:
        await ensure_scheduled_orders_tables()
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = sqlite3.connect(temp_db)
        try:
            job_id = conn.execute(
                """
                INSERT INTO scheduled_orders (
                    template_order_id, user_id, service_id, service_name, link,
                    provider_slug, api_account, fulfillment_mode, external_service_id,
                    quantity_mode, quantity_fixed, interval_days, next_run_at, status
                ) VALUES (
                    1, 999001, 'svc-1', 'Test', 'https://example.com/x',
                    'gozibra', 'default', 'auto', NULL,
                    'fixed', 50, 1, ?, 'active'
                )
                """,
                (past,),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        mock_svc = {
            "catalog_id": "svc-1",
            "local_price_dh": 14.0,
            "provider_price_usd": 1.0,
            "price_per_unit": False,
        }
        lookup = AsyncMock(
            return_value={
                "external_service_id": "1001",
                "provider_slug": "gozibra",
                "account_key": "default",
            }
        )
        submit = AsyncMock(return_value="prov-gen0")
        with patch("scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)), patch(
            "scheduled_orders._lookup_service_provider_meta", new=lookup
        ), patch("scheduled_orders.submit_provider_order", new=submit):
            result = await execute_scheduled_order(int(job_id))

        assert result["ok"] is False
        assert "GEN0_EXECUTION_IDENTITY_MISSING" in str(result.get("error") or "")
        lookup.assert_not_called()
        submit.assert_not_called()

        conn = sqlite3.connect(temp_db)
        try:
            order_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            # Only the seed template order (id=1); no Gen-0 materialization.
            assert order_count == 1
        finally:
            conn.close()

    asyncio.run(_run())


def test_gen1_missing_frozen_identity_fails_closed(temp_db: Path) -> None:
    async def _run() -> None:
        await ensure_scheduled_orders_tables()
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = sqlite3.connect(temp_db)
        try:
            # Corrupt Gen-1 row: marker present but empty provider_slug
            job_id = conn.execute(
                """
                INSERT INTO scheduled_orders (
                    template_order_id, user_id, service_id, service_name, link,
                    provider_slug, api_account, fulfillment_mode, external_service_id,
                    quantity_mode, quantity_fixed, interval_days, next_run_at, status
                ) VALUES (
                    1, 999001, 'svc-1', 'Test', 'https://example.com/x',
                    '', 'default', 'auto', '12345',
                    'fixed', 50, 1, ?, 'active'
                )
                """,
                (past,),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        mock_svc = {
            "catalog_id": "svc-1",
            "local_price_dh": 14.0,
            "provider_price_usd": 1.0,
            "price_per_unit": False,
        }
        with patch("scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)), patch(
            "scheduled_orders._lookup_service_provider_meta",
            new=AsyncMock(side_effect=AssertionError("must not lookup")),
        ), patch(
            "scheduled_orders.submit_provider_order",
            new=AsyncMock(side_effect=AssertionError("must not submit")),
        ):
            result = await execute_scheduled_order(int(job_id))
        assert result["ok"] is False

    asyncio.run(_run())


def test_gen1_malformed_external_fails_closed(temp_db: Path) -> None:
    async def _run() -> None:
        await ensure_scheduled_orders_tables()
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = sqlite3.connect(temp_db)
        try:
            job_id = conn.execute(
                """
                INSERT INTO scheduled_orders (
                    template_order_id, user_id, service_id, service_name, link,
                    provider_slug, api_account, fulfillment_mode, external_service_id,
                    quantity_mode, quantity_fixed, interval_days, next_run_at, status
                ) VALUES (
                    1, 999001, 'svc-1', 'Test', 'https://example.com/x',
                    'gozibra', 'default', 'auto', '0',
                    'fixed', 50, 1, ?, 'active'
                )
                """,
                (past,),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        mock_svc = {
            "catalog_id": "svc-1",
            "local_price_dh": 14.0,
            "provider_price_usd": 1.0,
            "price_per_unit": False,
        }
        with patch("scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)), patch(
            "scheduled_orders.submit_provider_order",
            new=AsyncMock(side_effect=AssertionError("must not submit")),
        ):
            result = await execute_scheduled_order(int(job_id))
        assert result["ok"] is False

    asyncio.run(_run())


def test_gen1_invalid_frozen_account_fails_closed(temp_db: Path) -> None:
    async def _run() -> None:
        await ensure_scheduled_orders_tables()
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = sqlite3.connect(temp_db)
        try:
            job_id = conn.execute(
                """
                INSERT INTO scheduled_orders (
                    template_order_id, user_id, service_id, service_name, link,
                    provider_slug, api_account, fulfillment_mode, external_service_id,
                    quantity_mode, quantity_fixed, interval_days, next_run_at, status
                ) VALUES (
                    1, 999001, 'svc-1', 'Test', 'https://example.com/x',
                    'gozibra', 'missing-account', 'auto', '12345',
                    'fixed', 50, 1, ?, 'active'
                )
                """,
                (past,),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()
        clear_provider_caches()

        mock_svc = {
            "catalog_id": "svc-1",
            "local_price_dh": 14.0,
            "provider_price_usd": 1.0,
            "price_per_unit": False,
        }
        with patch("scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)), patch(
            "scheduled_orders.submit_provider_order",
            new=AsyncMock(side_effect=AssertionError("must not submit")),
        ):
            result = await execute_scheduled_order(int(job_id))
        assert result["ok"] is False

    asyncio.run(_run())


def test_end_to_end_storefront_intent_to_schedule_to_order(temp_db: Path) -> None:
    """Isolated chain: frozen identity flows create → materialize → 8G order."""

    async def _run() -> None:
        await ensure_scheduled_orders_tables()
        # Simulate Order Intent / Gen-1 template already snapshotted.
        job = await create_scheduled_order(
            template_order_ref="PROV-999",
            quantity_mode=QUANTITY_FIXED,
            quantity_fixed=50,
            interval_days=1,
        )
        assert job["external_service_id"] == "12345"

        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                "UPDATE scheduled_orders SET next_run_at = ? WHERE id = ?",
                (past, job["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        mock_svc = {
            "catalog_id": "svc-1",
            "local_price_dh": 14.0,
            "provider_price_usd": 1.0,
            "price_per_unit": False,
        }
        submit = AsyncMock(return_value="wire-ok")
        with patch("scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)), patch(
            "scheduled_orders._lookup_service_provider_meta",
            new=AsyncMock(side_effect=AssertionError("gen1 must not lookup")),
        ), patch("scheduled_orders.submit_provider_order", new=submit):
            result = await execute_scheduled_order(int(job["id"]))

        assert result["ok"] is True
        assert submit.await_args.kwargs["service_id"] == "12345"
        assert submit.await_args.kwargs["provider_slug"] == "gozibra"
        assert submit.await_args.kwargs["account_key"] == "default"

        # Retry uses same frozen identity. Free the link first: active-link
        # protection blocks a second provider order while the prior run is active.
        past2 = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                """
                UPDATE orders SET status = 'completed'
                WHERE link = 'https://example.com/x'
                  AND LOWER(REPLACE(status, '_', ' ')) IN (
                      'pending', 'pending admin', 'submitted',
                      'in progress', 'processing'
                  )
                """
            )
            conn.execute(
                "UPDATE scheduled_orders SET next_run_at = ?, status = 'active' WHERE id = ?",
                (past2, job["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        submit2 = AsyncMock(return_value="wire-retry")
        with patch("scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)), patch(
            "scheduled_orders._lookup_service_provider_meta",
            new=AsyncMock(side_effect=AssertionError("retry must not lookup")),
        ), patch("scheduled_orders.submit_provider_order", new=submit2):
            result2 = await execute_scheduled_order(int(job["id"]), force=True)

        assert result2["ok"] is True
        assert submit2.await_args.kwargs["service_id"] == "12345"

    asyncio.run(_run())


def test_create_rejects_when_execution_identity_unresolvable(temp_db: Path) -> None:
    async def _run() -> None:
        await ensure_scheduled_orders_tables()
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                """
                UPDATE orders SET external_service_id_snapshot = NULL,
                    service_id = 'missing-svc' WHERE id = 1
                """
            )
            conn.execute("DELETE FROM smm_services")
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(ScheduledOrderValidationError, match="تجميد"):
            await create_scheduled_order(
                template_order_ref="PROV-999",
                quantity_mode=QUANTITY_FIXED,
                quantity_fixed=40,
                interval_days=1,
            )

    asyncio.run(_run())
