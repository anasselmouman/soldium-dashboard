"""Tests for admin alert detection and persistence."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

import database_connector as db_conn
from admin_alerts import (
    _scan_old_deposits,
    dismiss_alert,
    list_open_alerts,
    scan_all_alerts,
)
from db_schema import ADMIN_ALERTS_DDL, ADMIN_ALERTS_INDEX


def _create_test_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                balance REAL NOT NULL DEFAULT 0,
                referred_by INTEGER
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                service_name TEXT NOT NULL DEFAULT '',
                service_id TEXT NOT NULL DEFAULT '',
                link TEXT NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL DEFAULT 1,
                amount REAL NOT NULL DEFAULT 0,
                total_price REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                provider_order_id TEXT,
                fulfillment_mode TEXT NOT NULL DEFAULT 'auto',
                provider_slug TEXT NOT NULL DEFAULT 'gozibra',
                api_account TEXT NOT NULL DEFAULT 'default',
                start_count INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                status_changed_at TEXT
            );
            CREATE TABLE deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                method TEXT NOT NULL DEFAULT 'bank',
                proof_file_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE deposit_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                deposit_method TEXT NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'completed',
                deposit_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE smm_services (
                service_id TEXT PRIMARY KEY,
                catalog_id TEXT,
                local_item_id TEXT,
                platform_title TEXT,
                platform_key TEXT,
                provider_slug TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                provider_price_updated_at TEXT
            );
            CREATE TABLE providers (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO providers (slug, name) VALUES ('gozibra', 'Gozibra');
            """
        )
        conn.executescript(ADMIN_ALERTS_DDL)
        conn.executescript(ADMIN_ALERTS_INDEX)
        conn.execute("INSERT INTO users (user_id) VALUES (1)")
        conn.execute(
            """
            INSERT INTO orders (
                user_id, service_name, service_id, link, quantity, amount, total_price,
                status, provider_order_id, provider_slug, api_account,
                created_at, status_changed_at
            )
            VALUES (1, 'متابعين', 's1', 'https://example.com', 1000, 50, 50,
                    'in progress', 'PO-100', 'gozibra', 'default',
                    datetime('now', '-30 hours'), datetime('now', '-30 hours'))
            """
        )
        conn.execute(
            """
            INSERT INTO deposits (user_id, amount, method, proof_file_id, status)
            VALUES (1, 100.0, 'bank', 'proof-pending-1', 'pending')
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def alerts_db(tmp_path, monkeypatch):
    db_path = tmp_path / "alerts.db"
    _create_test_db(db_path)
    monkeypatch.setattr(db_conn, "DB_PATH", db_path)
    return db_path


def test_deposits_schema_has_no_created_at(alerts_db):
    conn = sqlite3.connect(alerts_db)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(deposits)")}
    finally:
        conn.close()
    assert "created_at" not in cols
    assert cols >= {"id", "user_id", "amount", "method", "proof_file_id", "status"}


def test_scan_old_deposits_does_not_query_missing_created_at(alerts_db):
    async def _run():
        candidates = await _scan_old_deposits()
        assert candidates == []

    asyncio.run(_run())


def test_scan_detects_stuck_execution_order(alerts_db, monkeypatch):
    monkeypatch.setattr("admin_alerts.ALERT_TELEGRAM_ON_CRITICAL", False)

    async def _empty_provider_scan():
        return []

    monkeypatch.setattr("admin_alerts._scan_low_provider_balance", _empty_provider_scan)
    monkeypatch.setattr("admin_alerts._scan_low_margin_services", _empty_provider_scan)

    async def _run():
        result = await scan_all_alerts()
        assert result["admin_alerts_count"] >= 1
        alerts = await list_open_alerts()
        types = {a["alert_type"] for a in alerts}
        assert "stuck_execution" in types
        assert "old_deposit" not in types

    asyncio.run(_run())


def test_dismiss_alert_hides_from_open_list(alerts_db, monkeypatch):
    monkeypatch.setattr("admin_alerts.ALERT_TELEGRAM_ON_CRITICAL", False)

    async def _empty_provider_scan():
        return []

    monkeypatch.setattr("admin_alerts._scan_low_provider_balance", _empty_provider_scan)
    monkeypatch.setattr("admin_alerts._scan_low_margin_services", _empty_provider_scan)

    async def _run():
        await scan_all_alerts()
        alerts = await list_open_alerts()
        assert alerts
        alert_id = alerts[0]["id"]
        ok = await dismiss_alert(alert_id)
        assert ok is True
        remaining = await list_open_alerts()
        assert all(item["id"] != alert_id for item in remaining)

    asyncio.run(_run())
