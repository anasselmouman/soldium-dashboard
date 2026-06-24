"""Test that profit chart prefers stored provider_cost_dh snapshot."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import date
from pathlib import Path

import database_connector as db_conn
from analytics import get_profit_chart
from db_schema import SMM_SERVICES_DDL


def test_profit_uses_order_cost_snapshot(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "snapshot.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0, total_spent REAL DEFAULT 0,
            referral_balance REAL DEFAULT 0, referral_earned_total REAL DEFAULT 0);
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, service_name TEXT, service_id TEXT,
            link TEXT, quantity INTEGER, amount REAL, total_price REAL, status TEXT,
            refunded_amount REAL DEFAULT 0, referral_commission_amount REAL DEFAULT 0,
            provider_cost_dh REAL DEFAULT 0, provider_slug TEXT NOT NULL DEFAULT 'gozibra',
            created_at TEXT
        );
        CREATE TABLE refund_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, user_id INTEGER,
            refund_type TEXT, refund_amount_dh REAL, actual_provider_usd REAL
        );
        """
    )
    conn.executescript(SMM_SERVICES_DDL)
    conn.execute(
        "ALTER TABLE smm_services ADD COLUMN catalog_id TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE smm_services ADD COLUMN external_service_id TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        """
        INSERT INTO smm_services (
            service_id, catalog_id, external_service_id, category, name_ar, provider_price_usd, local_price_dh,
            min_qty, max_qty, is_active, platform_key, local_item_id, platform_title, provider_slug
        ) VALUES ('9001', '9001', '9001', '', 'test', 9.0, 50.0, 1, 1000, 1, 'test', '9001', 'T', 'gozibra')
        """
    )
    today = date.today().isoformat()
    conn.execute(
        """
        INSERT INTO orders (
            user_id, service_name, service_id, link, quantity, amount, total_price,
            status, provider_cost_dh, created_at
        ) VALUES (1, 'svc', '9001', 'x', 1000, 40.0, 40.0, 'completed', 5.0, ?)
        """,
        (today,),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db_conn, "DB_PATH", str(db_path))

    async def _run() -> None:
        chart = await get_profit_chart()
        idx = chart["dates"].index(today)
        assert chart["costs"][idx] == 5.0
        assert chart["net_profit"][idx] == 35.0

    asyncio.run(_run())
