"""Tests for financial analytics calculations."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import date
from pathlib import Path

import pytest

import database_connector as db_conn
from analytics import get_liquidity_metrics, get_profit_chart
from db_schema import SMM_SERVICES_DDL
from settings import PARTIAL_PROVIDER_USD_TO_DH, SERVICE_USD_TO_DH_MULTIPLIER
from smm_services import DEFAULT_MARKUP_MULTIPLIER


def _create_test_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                balance REAL NOT NULL DEFAULT 0,
                total_spent REAL NOT NULL DEFAULT 0,
                referral_balance REAL NOT NULL DEFAULT 0,
                referral_earned_total REAL NOT NULL DEFAULT 0,
                telegram_name TEXT
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                service_name TEXT NOT NULL DEFAULT '',
                service_id TEXT NOT NULL,
                link TEXT NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                total_price REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                refunded_amount REAL NOT NULL DEFAULT 0,
                referral_commission_amount REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE deposit_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL,
                deposit_method TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT
            );
            CREATE TABLE refund_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                refund_type TEXT NOT NULL,
                refund_amount_dh REAL NOT NULL,
                actual_provider_usd REAL,
                final_customer_price_dh REAL,
                payload_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.executescript(SMM_SERVICES_DDL)
    finally:
        conn.close()


def _insert_service(
    conn: sqlite3.Connection,
    *,
    service_id: str,
    category: str = "",
    provider_price_usd: float = 0,
    local_price_dh: float = 0,
    local_item_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO smm_services (
            service_id, category, name_ar, provider_price_usd,
            local_price_dh, min_qty, max_qty, is_active,
            platform_key, local_item_id, platform_title
        ) VALUES (?, ?, 'test', ?, ?, 1, 1000, 1, 'test', ?, 'Test')
        """,
        (
            service_id,
            category,
            provider_price_usd,
            local_price_dh,
            local_item_id or service_id,
        ),
    )


def _insert_order(
    conn: sqlite3.Connection,
    *,
    service_id: str,
    quantity: int,
    amount: float,
    status: str = "completed",
    referral_commission: float = 0,
    refunded_amount: float = 0,
    day: str | None = None,
) -> int:
    created_at = day or date.today().isoformat()
    cursor = conn.execute(
        """
        INSERT INTO orders (
            user_id, service_name, service_id, link, quantity, amount,
            total_price, status, referral_commission_amount, refunded_amount,
            created_at
        ) VALUES (1, 'svc', ?, 'http://x', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            service_id,
            quantity,
            amount,
            amount,
            status,
            referral_commission,
            refunded_amount,
            created_at,
        ),
    )
    return int(cursor.lastrowid)


def _today_chart_index(chart: dict) -> int:
    today = date.today().isoformat()
    return chart["dates"].index(today)


@pytest.fixture
def analytics_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "analytics_test.db"
    _create_test_db(db_path)
    monkeypatch.setattr(db_conn, "DB_PATH", str(db_path))
    conn = sqlite3.connect(db_path)
    yield conn
    conn.close()


def test_profit_standard_service_provider_cost(analytics_db: sqlite3.Connection) -> None:
    async def _run() -> None:
        mult = SERVICE_USD_TO_DH_MULTIPLIER
        _insert_service(
            analytics_db,
            service_id="1001",
            provider_price_usd=1.0,
        )
        _insert_order(
            analytics_db,
            service_id="1001",
            quantity=1000,
            amount=20.0,
        )
        analytics_db.commit()

        chart = await get_profit_chart()
        idx = _today_chart_index(chart)

        expected_cost = round(1.0 * mult, 2)
        assert chart["costs"][idx] == expected_cost
        assert chart["sales"][idx] == 20.0
        assert chart["net_profit"][idx] == round(20.0 - expected_cost, 2)
        assert chart["totals"]["sales_dh"] == 20.0

    asyncio.run(_run())


def test_profit_per_unit_service_cost(analytics_db: sqlite3.Connection) -> None:
    async def _run() -> None:
        mult = SERVICE_USD_TO_DH_MULTIPLIER
        _insert_service(
            analytics_db,
            service_id="2001",
            category="per_unit",
            provider_price_usd=2.0,
        )
        _insert_order(
            analytics_db,
            service_id="2001",
            quantity=3,
            amount=90.0,
        )
        analytics_db.commit()

        chart = await get_profit_chart()
        idx = _today_chart_index(chart)

        expected_cost = round(3 * 2.0 * mult, 2)
        assert chart["costs"][idx] == expected_cost
        assert chart["net_profit"][idx] == round(90.0 - expected_cost, 2)

    asyncio.run(_run())


def test_profit_per_unit_fallback_uses_markup_not_retail(
    analytics_db: sqlite3.Connection,
) -> None:
    async def _run() -> None:
        mult = SERVICE_USD_TO_DH_MULTIPLIER
        markup = DEFAULT_MARKUP_MULTIPLIER
        local_price = 42.0
        _insert_service(
            analytics_db,
            service_id="3001",
            category="per_unit",
            provider_price_usd=0,
            local_price_dh=local_price,
        )
        _insert_order(
            analytics_db,
            service_id="3001",
            quantity=2,
            amount=84.0,
        )
        analytics_db.commit()

        chart = await get_profit_chart()
        idx = _today_chart_index(chart)

        expected_cost = round(2 * local_price * (mult / markup), 2)
        assert chart["costs"][idx] == expected_cost
        assert chart["sales"][idx] == 84.0
        assert chart["costs"][idx] < chart["sales"][idx]

    asyncio.run(_run())


def test_profit_excludes_canceled_and_pending_orders(
    analytics_db: sqlite3.Connection,
) -> None:
    async def _run() -> None:
        _insert_service(analytics_db, service_id="4001", provider_price_usd=1.0)
        _insert_order(
            analytics_db,
            service_id="4001",
            quantity=1000,
            amount=50.0,
            status="canceled",
        )
        _insert_order(
            analytics_db,
            service_id="4001",
            quantity=1000,
            amount=30.0,
            status="pending",
        )
        _insert_order(
            analytics_db,
            service_id="4001",
            quantity=1000,
            amount=20.0,
            status="completed",
        )
        analytics_db.commit()

        chart = await get_profit_chart()
        idx = _today_chart_index(chart)

        assert chart["sales"][idx] == 20.0

    asyncio.run(_run())


def test_profit_partial_scales_provider_cost(analytics_db: sqlite3.Connection) -> None:
    async def _run() -> None:
        mult = SERVICE_USD_TO_DH_MULTIPLIER
        _insert_service(analytics_db, service_id="4501", provider_price_usd=1.0)
        _insert_order(
            analytics_db,
            service_id="4501",
            quantity=1000,
            amount=100.0,
            status="partial",
            refunded_amount=60.0,
        )
        analytics_db.commit()

        chart = await get_profit_chart()
        idx = _today_chart_index(chart)

        full_cost = round(1.0 * mult, 2)
        expected_cost = round(full_cost * 0.4, 2)
        assert chart["sales"][idx] == 40.0
        assert chart["costs"][idx] == expected_cost
        assert chart["costs"][idx] < chart["sales"][idx]

    asyncio.run(_run())


def test_profit_partial_uses_audit_log_provider_usd(
    analytics_db: sqlite3.Connection,
) -> None:
    async def _run() -> None:
        _insert_service(analytics_db, service_id="4601", provider_price_usd=5.0)
        order_id = _insert_order(
            analytics_db,
            service_id="4601",
            quantity=1000,
            amount=100.0,
            status="partial",
            refunded_amount=50.0,
        )
        analytics_db.execute(
            """
            INSERT INTO refund_audit_log (
                order_id, user_id, refund_type, refund_amount_dh, actual_provider_usd
            ) VALUES (?, 1, 'partial', 50.0, 2.0)
            """,
            (order_id,),
        )
        analytics_db.commit()

        chart = await get_profit_chart()
        idx = _today_chart_index(chart)

        expected_cost = round(2.0 * PARTIAL_PROVIDER_USD_TO_DH, 2)
        assert chart["costs"][idx] == expected_cost
        assert chart["sales"][idx] == 50.0

    asyncio.run(_run())


def test_profit_joins_local_item_id_without_duplication(
    analytics_db: sqlite3.Connection,
) -> None:
    async def _run() -> None:
        mult = SERVICE_USD_TO_DH_MULTIPLIER
        _insert_service(
            analytics_db,
            service_id="5001",
            provider_price_usd=1.0,
            local_item_id="local-abc",
        )
        _insert_service(
            analytics_db,
            service_id="5002",
            provider_price_usd=9.0,
            local_item_id="local-abc",
        )
        _insert_order(
            analytics_db,
            service_id="local-abc",
            quantity=1000,
            amount=25.0,
        )
        analytics_db.commit()

        chart = await get_profit_chart()
        idx = _today_chart_index(chart)

        assert chart["costs"][idx] == round(1.0 * mult, 2)
        assert chart["sales"][idx] == 25.0

    asyncio.run(_run())


def test_profit_excludes_referral_from_costs(analytics_db: sqlite3.Connection) -> None:
    async def _run() -> None:
        mult = SERVICE_USD_TO_DH_MULTIPLIER
        _insert_service(analytics_db, service_id="6001", provider_price_usd=1.0)
        analytics_db.execute(
            """
            INSERT INTO orders (
                user_id, service_id, quantity, amount, status, referral_commission_amount
            ) VALUES (1, '6001', 1000, 50.0, 'completed', 10.0)
            """
        )
        analytics_db.commit()

        chart = await get_profit_chart()
        idx = _today_chart_index(chart)

        expected_cost = round(1.0 * mult, 2)
        assert chart["costs"][idx] == expected_cost
        assert chart["sales"][idx] == 50.0
        assert chart["net_profit"][idx] == round(50.0 - expected_cost, 2)
        assert chart["totals"]["net_profit_dh"] == round(
            chart["totals"]["sales_dh"] - chart["totals"]["costs_dh"], 2
        )

    asyncio.run(_run())


def test_liquidity_free_money_formula(analytics_db: sqlite3.Connection) -> None:
    async def _run() -> None:
        analytics_db.executemany(
            "INSERT INTO deposit_transactions (user_id, amount, status) VALUES (?, ?, 'completed')",
            [(1, 1000.0), (2, 500.0)],
        )
        analytics_db.executemany(
            "INSERT INTO withdrawals (user_id, amount, status) VALUES (?, ?, 'completed')",
            [(1, 200.0)],
        )
        analytics_db.executemany(
            """
            INSERT INTO users (user_id, balance, referral_balance, total_spent)
            VALUES (?, ?, ?, 0)
            """,
            [(1, 300.0, 50.0), (2, 100.0, 0.0)],
        )
        analytics_db.execute(
            """
            INSERT INTO orders (
                user_id, service_id, quantity, amount, status, referral_commission_amount
            ) VALUES (1, 'svc', 100, 900.0, 'completed', 50.0)
            """
        )
        analytics_db.commit()

        metrics = await get_liquidity_metrics()

        assert metrics["total_deposited_dh"] == 1500.0
        assert metrics["total_withdrawn_dh"] == 200.0
        assert metrics["total_liabilities_dh"] == 450.0
        assert metrics["total_free_money_dh"] == 850.0

    asyncio.run(_run())


def test_liquidity_open_orders_in_liabilities(analytics_db: sqlite3.Connection) -> None:
    async def _run() -> None:
        analytics_db.execute(
            "INSERT INTO deposit_transactions (user_id, amount, status) VALUES (1, 1000.0, 'completed')"
        )
        analytics_db.execute(
            "INSERT INTO users (user_id, balance, referral_balance, total_spent) VALUES (1, 700.0, 0.0, 0)"
        )
        analytics_db.execute(
            """
            INSERT INTO orders (user_id, service_id, quantity, amount, status)
            VALUES (1, 'svc', 100, 300.0, 'pending')
            """
        )
        analytics_db.commit()

        metrics = await get_liquidity_metrics()

        assert metrics["total_deposited_dh"] == 1000.0
        assert metrics["total_liabilities_dh"] == 1000.0
        assert metrics["total_free_money_dh"] == 0.0

    asyncio.run(_run())

