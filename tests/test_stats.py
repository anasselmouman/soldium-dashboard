"""Tests for dashboard summary statistics."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

import database_connector as db_conn
from stats import get_dashboard_stats


def _create_stats_test_db(path: Path) -> None:
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
                referral_level INTEGER NOT NULL DEFAULT 1,
                referred_by INTEGER,
                telegram_name TEXT
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                fulfillment_mode TEXT NOT NULL DEFAULT 'auto',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
                amount REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'completed',
                deposit_method TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT
            );
            """
        )
        # شبحان
        conn.execute("INSERT INTO users (user_id) VALUES (1)")
        conn.execute("INSERT INTO users (user_id) VALUES (2)")
        # نشط بطلب
        conn.execute("INSERT INTO users (user_id) VALUES (3)")
        conn.execute(
            "INSERT INTO orders (user_id, amount, status) VALUES (3, 10.0, 'completed')"
        )
        # نشط كمُحيل
        conn.execute("INSERT INTO users (user_id) VALUES (4)")
        conn.execute("INSERT INTO users (user_id, referred_by) VALUES (5, 4)")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def stats_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "users.db"
    _create_stats_test_db(db_path)
    monkeypatch.setattr(db_conn, "DB_PATH", db_path)

    async def _empty_timed():
        return []

    async def _empty_alerts(*_args, **_kwargs):
        return []

    async def _empty_counts():
        return {"admin_alerts_count": 0, "admin_alerts_critical": 0}

    async def _empty_notif():
        return {"unread_count": 0, "today_count": 0}

    async def _fail_profit():
        raise RuntimeError("skip profit in test")

    monkeypatch.setattr("stats.list_active_timed_announcements", _empty_timed)
    monkeypatch.setattr("stats.list_open_alerts", _empty_alerts)
    monkeypatch.setattr("stats.count_open_alerts", _empty_counts)
    monkeypatch.setattr("stats.get_notifications_summary", _empty_notif)
    monkeypatch.setattr("stats.get_profit_chart", _fail_profit)
    return db_path


def test_active_users_excludes_start_only_accounts(stats_db: Path) -> None:
    del stats_db
    result = asyncio.run(get_dashboard_stats())
    assert result["total_users"] == 5
    assert result["active_users"] == 2
