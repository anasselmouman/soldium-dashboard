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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE smm_services (
                service_id TEXT PRIMARY KEY,
                catalog_id TEXT,
                local_item_id TEXT,
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
            INSERT INTO orders (
                user_id, service_name, service_id, link, quantity, amount, total_price,
                status, provider_order_id
            ) VALUES (123, 'Test', 'svc-1', 'https://example.com/x', 50, 5.0, 5.0, 'completed', 'PROV-999');
            INSERT INTO smm_services (
                service_id, catalog_id, local_item_id, provider_price_usd, local_price_dh,
                min_qty, max_qty, provider_slug, provider_api_account
            ) VALUES ('gozibra-1', 'svc-1', 'svc-1', 1.0, 14.0, 10, 10000, 'gozibra', 'default');
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
    import config as dashboard_config
    import scheduled_orders as so_module

    monkeypatch.setattr(dashboard_config, "ADMIN_TELEGRAM_ID", 999001)
    monkeypatch.setattr(so_module, "ADMIN_TELEGRAM_ID", 999001)
    yield db_path


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
                    quantity_mode, quantity_fixed, interval_days, next_run_at, status
                ) VALUES (1, 999001, 'svc-1', 'Test', 'https://example.com/x',
                          'fixed', 50, 1, ?, 'active')
                """,
                (past,),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        mock_svc = {
            "local_price_dh": 14.0,
            "provider_price_usd": 1.0,
            "price_per_unit": False,
        }

        with patch("scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)), patch(
            "scheduled_orders._lookup_service_provider_meta",
            new=AsyncMock(
                return_value={
                    "external_service_id": "1",
                    "provider_slug": "gozibra",
                    "account_key": "default",
                }
            ),
        ), patch(
            "scheduled_orders.submit_provider_order",
            new=AsyncMock(return_value="prov-123"),
        ):
            result = await execute_scheduled_order(int(job_id))

        assert result["ok"] is True
        assert result["order_id"] >= 1
        assert result["quantity"] == 50

        conn = sqlite3.connect(temp_db)
        try:
            order = conn.execute(
                "SELECT user_id, quantity, provider_order_id FROM orders WHERE id = ?",
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
        assert balance is not None
        assert balance[0] < 500.0
        assert runs is not None
        assert runs[0] == "success"

    asyncio.run(_run())


def test_resolve_template_order_by_provider_id(temp_db: Path) -> None:
    async def _run() -> None:
        order = await resolve_template_order("PROV-999")
        assert order["id"] == 1
        assert order["provider_order_id"] == "PROV-999"
        assert order["service_id"] == "svc-1"

    asyncio.run(_run())
