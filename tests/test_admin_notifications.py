"""Tests for admin notification inbox."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

import database_connector as db_conn
from admin_notifications import (
    get_notifications_summary,
    list_admin_notifications,
    mark_notification_read,
    record_admin_notification,
)
from db_schema import ADMIN_NOTIFICATIONS_DDL, ADMIN_NOTIFICATIONS_INDEXES


def _create_test_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(ADMIN_NOTIFICATIONS_DDL)
        conn.executescript(ADMIN_NOTIFICATIONS_INDEXES)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def notifications_db(tmp_path, monkeypatch):
    db_path = tmp_path / "notifications.db"
    _create_test_db(db_path)
    monkeypatch.setattr(db_conn, "DB_PATH", db_path)
    return db_path


def test_record_list_and_mark_read(notifications_db):
    async def _run():
        nid = await record_admin_notification(
            category="manual_order",
            title="طلب يدوي",
            body_html="<b>اختبار</b> طلب",
            severity="warning",
            entity_type="order",
            entity_id="42",
            user_id=1001,
            telegram_sent=True,
        )
        assert nid is not None

        result = await list_admin_notifications(unread_only=True)
        assert result["total"] == 1
        assert result["items"][0]["category"] == "manual_order"
        assert result["items"][0]["action_url"] == "/orders?highlight=42"

        summary = await get_notifications_summary()
        assert summary["unread_count"] == 1

        ok = await mark_notification_read(nid)
        assert ok is True
        summary2 = await get_notifications_summary()
        assert summary2["unread_count"] == 0

    asyncio.run(_run())


def test_search_notifications(notifications_db):
    async def _run():
        await record_admin_notification(
            category="deposit_bank",
            title="إيصال شحن",
            body_html="مستخدم 555",
            user_id=555,
            telegram_sent=True,
        )
        found = await list_admin_notifications(search="555")
        assert found["total"] == 1

    asyncio.run(_run())
