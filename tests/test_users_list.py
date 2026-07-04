"""Tests for user listing with activity classification."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

import database_connector as db_conn
from users import list_users


def _create_users_test_db(path: Path) -> None:
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
                status TEXT NOT NULL DEFAULT 'pending'
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
                deposit_method TEXT
            );
            CREATE TABLE withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            );
            """
        )
        conn.execute("INSERT INTO users (user_id) VALUES (1)")
        conn.execute("INSERT INTO users (user_id) VALUES (2)")
        conn.execute("INSERT INTO users (user_id, balance) VALUES (3, 5.0)")
        conn.execute(
            "INSERT INTO orders (user_id, amount, status) VALUES (3, 5.0, 'completed')"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def users_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "users.db"
    _create_users_test_db(db_path)
    monkeypatch.setattr(db_conn, "DB_PATH", db_path)
    return db_path


def test_list_users_marks_activity_and_sorts_active_first(users_db: Path) -> None:
    del users_db
    result = asyncio.run(list_users(page=1, limit=10))
    assert result["total_users"] == 3
    assert result["active_users"] == 1
    assert len(result["users"]) == 3
    assert result["users"][0]["user_id"] == 3
    assert result["users"][0]["is_active"] is True
    assert result["users"][1]["is_active"] is False


def test_list_users_active_only_filter(users_db: Path) -> None:
    del users_db
    result = asyncio.run(list_users(page=1, limit=10, active_only=True))
    assert result["total"] == 1
    assert result["active_only"] is True
    assert result["users"][0]["user_id"] == 3
