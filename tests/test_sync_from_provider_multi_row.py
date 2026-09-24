# -*- coding: utf-8 -*-
"""sync_from_provider duplicate-safe preparation (UNIQUE still present in prod)."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOT_ROOT = ROOT.parent / "soldium-bot"
# Dashboard first so `utils` / `smm_services` resolve to dashboard packages.
sys.path.insert(0, str(BOT_ROOT))
sys.path.insert(0, str(ROOT))

import database_connector  # noqa: E402
import smm_services  # noqa: E402


def _create_smm(conn: sqlite3.Connection, *, with_unique: bool) -> None:
    unique_clause = (
        ", UNIQUE(provider_slug, external_service_id)" if with_unique else ""
    )
    conn.execute(
        f"""
        CREATE TABLE smm_services (
            catalog_id TEXT PRIMARY KEY,
            external_service_id TEXT NOT NULL,
            provider_slug TEXT NOT NULL DEFAULT 'gozibra',
            category TEXT NOT NULL DEFAULT '',
            name_ar TEXT NOT NULL DEFAULT '',
            provider_price_usd REAL NOT NULL DEFAULT 0,
            local_price_dh REAL NOT NULL DEFAULT 0,
            min_qty INTEGER NOT NULL DEFAULT 1,
            max_qty INTEGER NOT NULL DEFAULT 1000000,
            is_active INTEGER NOT NULL DEFAULT 1,
            platform_key TEXT NOT NULL DEFAULT '',
            local_item_id TEXT NOT NULL DEFAULT '',
            platform_title TEXT NOT NULL DEFAULT '',
            fulfillment_mode TEXT NOT NULL DEFAULT 'auto',
            provider_api_account TEXT,
            provider_price_updated_at TEXT,
            service_id TEXT NOT NULL DEFAULT ''
            {unique_clause}
        )
        """
    )


def _insert(
    conn: sqlite3.Connection,
    *,
    catalog_id: str,
    external_service_id: str,
    provider_price_usd: float = 1.0,
    min_qty: int = 1,
    max_qty: int = 100,
    is_active: int = 1,
) -> None:
    conn.execute(
        """
        INSERT INTO smm_services (
            catalog_id, external_service_id, service_id, provider_slug,
            provider_price_usd, local_price_dh, min_qty, max_qty,
            is_active, platform_key, local_item_id, name_ar
        ) VALUES (?, ?, ?, 'gozibra', ?, 10.0, ?, ?, ?, 'telegram', ?, ?)
        """,
        (
            catalog_id,
            external_service_id,
            external_service_id,
            provider_price_usd,
            min_qty,
            max_qty,
            is_active,
            catalog_id,
            catalog_id,
        ),
    )


@pytest.fixture
def sync_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "sync_from_provider.db"
    monkeypatch.setattr(database_connector, "DB_PATH", str(path))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _create_smm(conn, with_unique=True)
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def sync_db_allow_dupes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "sync_from_provider_dupes.db"
    monkeypatch.setattr(database_connector, "DB_PATH", str(path))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _create_smm(conn, with_unique=False)
        conn.commit()
    finally:
        conn.close()
    return path


def _entry(service: int, rate: float = 2.5, min_q: int = 50, max_q: int = 5000) -> dict:
    return {
        "service": service,
        "rate": rate,
        "min": min_q,
        "max": max_q,
        "provider_slug": "gozibra",
        "api_account": "default",
        "name": f"Svc {service}",
        "category": "test",
    }


def test_sync_updates_existing_operational_row(sync_db: Path) -> None:
    conn = sqlite3.connect(sync_db)
    try:
        _insert(conn, catalog_id="4210", external_service_id="4210", provider_price_usd=1.0)
        conn.commit()
    finally:
        conn.close()

    stats = asyncio.run(smm_services.sync_from_provider([_entry(4210, rate=4.0)]))
    assert stats["updated"] == 1
    assert stats["inserted"] == 0
    assert stats["rows_touched"] == 1

    conn = sqlite3.connect(sync_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT provider_price_usd, min_qty, max_qty FROM smm_services WHERE catalog_id='4210'"
        ).fetchone()
        assert float(row["provider_price_usd"]) == 4.0
        assert int(row["min_qty"]) == 50
        assert int(row["max_qty"]) == 5000
        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == 1
    finally:
        conn.close()


def test_sync_inserts_inventory_stub_when_zero_matches(sync_db: Path) -> None:
    stats = asyncio.run(smm_services.sync_from_provider([_entry(9999, rate=1.25)]))
    assert stats["inserted"] == 1
    assert stats["updated"] == 0

    conn = sqlite3.connect(sync_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT catalog_id, is_active, provider_price_usd FROM smm_services"
        ).fetchone()
        assert row["catalog_id"] == "gozibra-9999"
        assert int(row["is_active"]) == 0
        assert float(row["provider_price_usd"]) == 1.25
    finally:
        conn.close()


def test_sync_updates_all_matching_rows_not_arbitrary_fetchone(
    sync_db_allow_dupes: Path,
) -> None:
    """When N rows share a provider SKU, every catalog_id is updated."""
    conn = sqlite3.connect(sync_db_allow_dupes)
    try:
        _insert(conn, catalog_id="4210", external_service_id="4210", provider_price_usd=1.0)
        _insert(
            conn,
            catalog_id="gozibra-4210",
            external_service_id="4210",
            provider_price_usd=1.0,
            is_active=0,
        )
        conn.commit()
    finally:
        conn.close()

    stats = asyncio.run(smm_services.sync_from_provider([_entry(4210, rate=7.7)]))
    assert stats["updated"] == 1
    assert stats["inserted"] == 0
    assert stats["rows_touched"] == 2

    conn = sqlite3.connect(sync_db_allow_dupes)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT catalog_id, provider_price_usd, min_qty, max_qty
            FROM smm_services
            WHERE provider_slug='gozibra' AND external_service_id='4210'
            ORDER BY catalog_id
            """
        ).fetchall()
        assert [r["catalog_id"] for r in rows] == ["4210", "gozibra-4210"]
        for row in rows:
            assert float(row["provider_price_usd"]) == 7.7
            assert int(row["min_qty"]) == 50
            assert int(row["max_qty"]) == 5000
    finally:
        conn.close()


def test_sync_does_not_insert_when_any_match_exists(sync_db_allow_dupes: Path) -> None:
    conn = sqlite3.connect(sync_db_allow_dupes)
    try:
        _insert(conn, catalog_id="4210", external_service_id="4210")
        _insert(conn, catalog_id="gozibra-4210", external_service_id="4210", is_active=0)
        conn.commit()
    finally:
        conn.close()

    stats = asyncio.run(smm_services.sync_from_provider([_entry(4210)]))
    assert stats["inserted"] == 0
    assert stats["updated"] == 1

    conn = sqlite3.connect(sync_db_allow_dupes)
    try:
        count = conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0]
        assert count == 2
    finally:
        conn.close()
