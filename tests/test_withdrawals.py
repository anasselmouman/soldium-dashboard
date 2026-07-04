"""Tests for pending withdrawal queue API."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

import database_connector as db_conn
from withdrawals import get_pending_withdrawals


def _create_test_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                telegram_name TEXT
            );
            CREATE TABLE withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                method TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                withdrawal_type TEXT NOT NULL DEFAULT 'normal',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO users (user_id, telegram_name) VALUES (?, ?)",
            (1001, "Test User"),
        )
        conn.execute(
            """
            INSERT INTO withdrawals (user_id, amount, method, status, withdrawal_type)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1001, 50.0, "CashPlus", "pending", "normal"),
        )
        conn.execute(
            """
            INSERT INTO withdrawals (user_id, amount, method, status, withdrawal_type)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1001, 20.0, "PayPal", "completed", "referral"),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def withdrawals_db(tmp_path, monkeypatch):
    db_path = tmp_path / "withdrawals.db"
    _create_test_db(db_path)
    monkeypatch.setattr(db_conn, "DB_PATH", db_path)
    return db_path


def test_get_pending_withdrawals_returns_only_pending(withdrawals_db):
    async def _run():
        items = await get_pending_withdrawals()
        assert len(items) == 1
        assert items[0]["id"] == 1
        assert items[0]["amount"] == 50.0
        assert items[0]["withdrawal_type"] == "normal"
        assert items[0]["display_name"] == "Test User"

    asyncio.run(_run())
