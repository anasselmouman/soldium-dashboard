"""Tests for scheduled / recurring orders."""
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
    QUANTITY_RANGE,
    STATUS_ACTIVE,
    STATUS_PAUSED,
    ScheduledOrderValidationError,
    create_scheduled_order,
    execute_scheduled_order,
    list_scheduled_orders,
    pause_scheduled_order,
    resolve_template_order,
    resume_scheduled_order,
)


def _create_test_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                balance REAL NOT NULL DEFAULT 1000,
                total_spent REAL NOT NULL DEFAULT 0,
                telegram_name TEXT
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
                provider_api_account TEXT NOT NULL DEFAULT 'default'
            );
            INSERT INTO users (user_id, balance) VALUES (999001, 500.0);
            INSERT INTO providers (slug, name, api_base_url, is_active)
            VALUES ('gozibra', 'Gozibra', 'https://example.test', 1);
            INSERT INTO provider_accounts
                (provider_slug, account_key, api_key_env, is_active)
            VALUES ('gozibra', 'default', 'TEST_PROVIDER_KEY', 1);
            INSERT INTO orders (
                user_id, service_name, service_id, link, quantity, amount, total_price,
                status, provider_order_id, catalog_id, external_service_id_snapshot
            ) VALUES (
                123, 'Test', 'svc-1', 'https://example.com/x', 50, 5.0, 5.0,
                'completed', 'PROV-999', 'svc-1', '1001'
            );
            INSERT INTO smm_services (
                service_id, catalog_id, local_item_id, external_service_id, provider_price_usd, local_price_dh,
                min_qty, max_qty, provider_slug, provider_api_account
            ) VALUES ('gozibra-1', 'svc-1', 'svc-1', '1001', 1.0, 14.0, 10, 10000, 'gozibra', 'default');
            """
        )
    finally:
        conn.close()


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "users.db"
    _create_test_db(db_path)
    monkeypatch.setattr(db_conn, "DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_ID", "999001")
    monkeypatch.setenv("TEST_PROVIDER_KEY", "test-key-not-placeholder")
    import config as dashboard_config
    import scheduled_orders as so_module
    from services.provider_registry import clear_provider_caches

    monkeypatch.setattr(dashboard_config, "ADMIN_TELEGRAM_ID", 999001)
    monkeypatch.setattr(so_module, "ADMIN_TELEGRAM_ID", 999001)
    clear_provider_caches()
    yield db_path
    clear_provider_caches()


def test_create_scheduled_order_fixed(temp_db: Path) -> None:
    async def _run() -> None:
        await ensure_scheduled_orders_tables()
        job = await create_scheduled_order(
            template_order_ref="PROV-999",
            quantity_mode=QUANTITY_FIXED,
            quantity_fixed=60,
            interval_days=2,
            name="Daily boost",
        )
        assert job["id"] >= 1
        assert job["status"] == STATUS_ACTIVE
        assert job["quantity_mode"] == QUANTITY_FIXED
        assert job["quantity_fixed"] == 60
        assert job["interval_days"] == 2
        assert job["user_id"] == 999001
        assert job["template_order_id"] == 1
        assert job["external_service_id"] == "1001"
        assert job["execution_generation"] == "gen1"

        jobs = await list_scheduled_orders()
        assert len(jobs) == 1

    asyncio.run(_run())


def test_create_scheduled_order_range_validation(temp_db: Path) -> None:
    async def _run() -> None:
        await ensure_scheduled_orders_tables()
        with pytest.raises(ScheduledOrderValidationError, match="الحد الأدنى"):
            await create_scheduled_order(
                template_order_ref="PROV-999",
                quantity_mode=QUANTITY_RANGE,
                quantity_min=200,
                quantity_max=100,
                interval_days=1,
            )

    asyncio.run(_run())


def test_pause_and_resume(temp_db: Path) -> None:
    async def _run() -> None:
        await ensure_scheduled_orders_tables()
        job = await create_scheduled_order(
            template_order_ref="PROV-999",
            quantity_mode=QUANTITY_FIXED,
            quantity_fixed=40,
            interval_days=1,
        )
        paused = await pause_scheduled_order(job["id"])
        assert paused["status"] == STATUS_PAUSED

        resumed = await resume_scheduled_order(job["id"])
        assert resumed["status"] == STATUS_ACTIVE

    asyncio.run(_run())


def test_execute_scheduled_order_success(temp_db: Path) -> None:
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
                    'gozibra', 'default', 'auto', '1',
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
            side_effect=AssertionError("Gen-1 must not live-lookup SKU")
        )
        submit = AsyncMock(return_value="prov-123")
        with patch("scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)), patch(
            "scheduled_orders._lookup_service_provider_meta",
            new=lookup,
        ), patch(
            "scheduled_orders.submit_provider_order",
            new=submit,
        ):
            result = await execute_scheduled_order(int(job_id))

        assert result["ok"] is True
        assert result["order_id"] >= 1
        assert result["quantity"] == 50
        lookup.assert_not_called()
        assert submit.await_args.kwargs["service_id"] == "1"

        conn = sqlite3.connect(temp_db)
        try:
            order = conn.execute(
                "SELECT user_id, quantity, provider_order_id FROM orders WHERE id = ?",
                (result["order_id"],),
            ).fetchone()
            snap = conn.execute(
                "SELECT catalog_id, external_service_id_snapshot FROM orders WHERE id = ?",
                (result["order_id"],),
            ).fetchone()
            balance = conn.execute(
                "SELECT balance FROM users WHERE user_id = 999001"
            ).fetchone()
            runs = conn.execute(
                "SELECT status FROM scheduled_order_runs WHERE scheduled_order_id = ?",
                (int(job_id),),
            ).fetchone()
        finally:
            conn.close()

        assert order is not None
        assert order[0] == 999001
        assert order[1] == 50
        assert order[2] == "prov-123"
        assert snap is not None
        assert snap[0] == "svc-1"
        assert snap[1] == "1"
        assert balance is not None
        assert balance[0] < 500.0
        assert runs is not None
        assert runs[0] == "success"

    asyncio.run(_run())


def test_execute_blocked_when_link_already_active(temp_db: Path) -> None:
    """Second materialization for the same link must fail while prior order is active."""

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
                    'gozibra', 'default', 'auto', '1',
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
        submit = AsyncMock(return_value="prov-first")
        with patch("scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)), patch(
            "scheduled_orders._lookup_service_provider_meta",
            new=AsyncMock(side_effect=AssertionError("gen1")),
        ), patch("scheduled_orders.submit_provider_order", new=submit):
            first = await execute_scheduled_order(int(job_id))
        assert first["ok"] is True

        conn = sqlite3.connect(temp_db)
        try:
            bal_before = conn.execute(
                "SELECT balance FROM users WHERE user_id = 999001"
            ).fetchone()[0]
            order_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            conn.execute(
                "UPDATE scheduled_orders SET next_run_at = ?, status = 'active' WHERE id = ?",
                (past, int(job_id)),
            )
            conn.commit()
        finally:
            conn.close()

        submit2 = AsyncMock(return_value="must-not-call")
        with patch("scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)), patch(
            "scheduled_orders._lookup_service_provider_meta",
            new=AsyncMock(side_effect=AssertionError("gen1")),
        ), patch("scheduled_orders.submit_provider_order", new=submit2):
            second = await execute_scheduled_order(int(job_id), force=True)

        assert second["ok"] is False
        assert "الرابط مشغول" in str(second.get("error") or "")
        submit2.assert_not_called()

        conn = sqlite3.connect(temp_db)
        try:
            bal_after = conn.execute(
                "SELECT balance FROM users WHERE user_id = 999001"
            ).fetchone()[0]
            assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == order_count
            assert bal_after == bal_before
        finally:
            conn.close()

    asyncio.run(_run())


def test_resolve_template_order_by_provider_id(temp_db: Path) -> None:
    async def _run() -> None:
        order = await resolve_template_order("PROV-999")
        assert order["id"] == 1
        assert order["provider_order_id"] == "PROV-999"
        assert order["service_id"] == "svc-1"

    asyncio.run(_run())
