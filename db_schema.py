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


TIMED_ANNOUNCEMENTS_DDL = """
CREATE TABLE IF NOT EXISTS timed_announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_html TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    launched_at TEXT,
    stopped_at TEXT
);
"""

TIMED_ANNOUNCEMENT_DISMISSALS_DDL = """
CREATE TABLE IF NOT EXISTS timed_announcement_dismissals (
    announcement_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    dismissed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (announcement_id, user_id),
    FOREIGN KEY (announcement_id) REFERENCES timed_announcements(id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
"""

TIMED_ANNOUNCEMENTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_timed_announcements_status_ends
ON timed_announcements (status, ends_at);
"""


async def ensure_smm_services_table() -> None:
    async with get_db() as db:
        await db.execute(SMM_SERVICES_DDL)
        await db.execute(SMM_SERVICES_INDEX)
        await db.commit()


async def ensure_timed_announcements_tables() -> None:
    async with get_db() as db:
        await db.execute(TIMED_ANNOUNCEMENTS_DDL)
        await db.execute(TIMED_ANNOUNCEMENT_DISMISSALS_DDL)
        await db.execute(TIMED_ANNOUNCEMENTS_INDEX)
        await db.commit()
