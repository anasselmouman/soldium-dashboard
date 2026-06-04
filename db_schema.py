"""Shared SQLite schema helpers for the admin dashboard."""
from __future__ import annotations

from database_connector import get_db

SMM_SERVICES_DDL = """
CREATE TABLE IF NOT EXISTS smm_services (
    service_id TEXT PRIMARY KEY,
    category TEXT NOT NULL DEFAULT '',
    name_ar TEXT NOT NULL DEFAULT '',
    provider_price_usd REAL NOT NULL DEFAULT 0,
    local_price_dh REAL NOT NULL DEFAULT 0,
    min_qty INTEGER NOT NULL DEFAULT 1,
    max_qty INTEGER NOT NULL DEFAULT 1000000,
    is_active INTEGER NOT NULL DEFAULT 1,
    platform_key TEXT NOT NULL DEFAULT '',
    section_key TEXT,
    subsection_key TEXT,
    local_item_id TEXT NOT NULL DEFAULT '',
    platform_title TEXT NOT NULL DEFAULT '',
    section_title TEXT,
    subsection_title TEXT
);
"""

SMM_SERVICES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_smm_services_active
ON smm_services (is_active, platform_key);
"""


async def ensure_smm_services_table() -> None:
    async with get_db() as db:
        await db.execute(SMM_SERVICES_DDL)
        await db.execute(SMM_SERVICES_INDEX)
        await db.commit()
