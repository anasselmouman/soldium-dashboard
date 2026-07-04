"""Tests for minimum retail margin alerts."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

import database_connector as db_conn
from admin_alerts import list_open_alerts, scan_all_alerts
from db_schema import ADMIN_ALERTS_DDL, ADMIN_ALERTS_INDEX
from utils.order_economics import is_retail_below_minimum_margin, minimum_retail_price_dh


def test_minimum_retail_price() -> None:
    assert minimum_retail_price_dh(1.0, usd_to_dh=14) == 14.0
    assert is_retail_below_minimum_margin(1.0, 10.0, usd_to_dh=14) is True
    assert is_retail_below_minimum_margin(1.0, 14.0, usd_to_dh=14) is False
    assert is_retail_below_minimum_margin(1.0, 15.0, usd_to_dh=14) is False
    assert is_retail_below_minimum_margin(0.0, 5.0, usd_to_dh=14) is False


def _create_margin_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE smm_services (
                service_id TEXT PRIMARY KEY,
                catalog_id TEXT,
                name_ar TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                platform_title TEXT,
                platform_key TEXT,
                provider_slug TEXT,
                provider_price_usd REAL NOT NULL DEFAULT 0,
                local_price_dh REAL NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        conn.executescript(ADMIN_ALERTS_DDL)
        conn.executescript(ADMIN_ALERTS_INDEX)
        conn.execute(
            """
            INSERT INTO smm_services (
                service_id, catalog_id, name_ar, category,
                platform_title, platform_key, provider_slug,
                provider_price_usd, local_price_dh, is_active
            )
            VALUES (
                'svc-low', 'cat-1', 'متابعين', '',
                'إنستغرام', 'instagram', 'gozibra',
                1.0, 10.0, 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO smm_services (
                service_id, catalog_id, name_ar, category,
                platform_title, platform_key, provider_slug,
                provider_price_usd, local_price_dh, is_active
            )
            VALUES (
                'svc-ok', 'cat-2', 'لايكات', '',
                'تيك توك', 'tiktok', 'gozibra',
                1.0, 15.0, 1
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def margin_db(tmp_path, monkeypatch):
    db_path = tmp_path / "margin.db"
    _create_margin_db(db_path)
    monkeypatch.setattr(db_conn, "DB_PATH", db_path)
    monkeypatch.setattr("admin_alerts.ALERT_TELEGRAM_ON_CRITICAL", False)
    monkeypatch.setattr("admin_alerts.ALERT_TELEGRAM_ON_LOW_MARGIN", False)
    return db_path


def test_scan_detects_low_margin_service(margin_db, monkeypatch):
    async def _empty_provider_scan():
        return []

    monkeypatch.setattr("admin_alerts._scan_low_provider_balance", _empty_provider_scan)

    async def _run():
        await scan_all_alerts()
        alerts = await list_open_alerts()
        low_margin = [a for a in alerts if a["alert_type"] == "low_margin"]
        assert len(low_margin) == 1
        assert low_margin[0]["entity_id"] == "svc-low"
        assert "10.00" in low_margin[0]["message"]
        assert "14.00" in low_margin[0]["message"]
        payload = low_margin[0]["payload"] or {}
        assert payload.get("message_html")
        assert "<code>cat-1</code>" in payload["message_html"]

    asyncio.run(_run())
