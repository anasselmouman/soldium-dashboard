"""Schedule and process automatic Telegram message deletions."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from database_connector import get_db

logger = logging.getLogger("soldium.message_deletions")

POLL_INTERVAL_SECONDS = 5.0
BATCH_SIZE = 50

MIN_AUTO_DELETE_SECONDS = 10
MAX_AUTO_DELETE_SECONDS = 7 * 24 * 3600

SCHEDULED_DELETIONS_DDL = """
CREATE TABLE IF NOT EXISTS scheduled_message_deletions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    delete_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

SCHEDULED_DELETIONS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_scheduled_deletions_delete_at
ON scheduled_message_deletions (delete_at);
"""


class AutoDeleteValidationError(ValueError):
    pass


def validate_auto_delete_seconds(value: int | None) -> int | None:
    if value is None:
        return None
    seconds = int(value)
    if seconds < MIN_AUTO_DELETE_SECONDS:
        raise AutoDeleteValidationError(
            f"مدة الحذف التلقائي يجب أن تكون {MIN_AUTO_DELETE_SECONDS} ثانية على الأقل.",
        )
    if seconds > MAX_AUTO_DELETE_SECONDS:
        raise AutoDeleteValidationError(
            "مدة الحذف التلقائي لا يمكن أن تتجاوز 7 أيام.",
        )
    return seconds


def _utc_delete_at_str(seconds_from_now: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def ensure_scheduled_deletions_table() -> None:
    async with get_db() as db:
        await db.execute(SCHEDULED_DELETIONS_DDL)
        await db.execute(SCHEDULED_DELETIONS_INDEX)
        await db.commit()


async def schedule_message_deletion(chat_id: int, message_id: int, seconds: int) -> None:
    delete_at = _utc_delete_at_str(seconds)
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO scheduled_message_deletions (chat_id, message_id, delete_at)
            VALUES (?, ?, ?)
            """,
            (chat_id, message_id, delete_at),
        )
        await db.commit()


async def schedule_message_deletions_batch(
    items: list[tuple[int, int, int]],
) -> None:
    """Each item: (chat_id, message_id, seconds_from_now)."""
    if not items:
        return
    rows = [
        (chat_id, message_id, _utc_delete_at_str(seconds))
        for chat_id, message_id, seconds in items
    ]
    async with get_db() as db:
        await db.executemany(
            """
            INSERT INTO scheduled_message_deletions (chat_id, message_id, delete_at)
            VALUES (?, ?, ?)
            """,
            rows,
        )
        await db.commit()


async def _fetch_due_deletions(limit: int) -> list[tuple[int, int, int]]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with get_db() as db:
        async with db.execute(
            """
            SELECT id, chat_id, message_id
            FROM scheduled_message_deletions
            WHERE delete_at <= ?
            ORDER BY delete_at ASC
            LIMIT ?
            """,
            (now, limit),
        ) as cursor:
            rows = await cursor.fetchall()
    return [(int(row[0]), int(row[1]), int(row[2])) for row in rows]


async def _remove_deletion(row_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            "DELETE FROM scheduled_message_deletions WHERE id = ?",
            (row_id,),
        )
        await db.commit()


async def process_due_deletions_once() -> int:
    from notifier import delete_telegram_message

    due = await _fetch_due_deletions(BATCH_SIZE)
    processed = 0
    for row_id, chat_id, message_id in due:
        await delete_telegram_message(chat_id, message_id)
        await _remove_deletion(row_id)
        processed += 1
    return processed


async def run_deletion_worker() -> None:
    logger.info("Message deletion worker started")
    while True:
        try:
            count = await process_due_deletions_once()
            if count:
                logger.info("Auto-deleted %s scheduled message(s)", count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Message deletion worker error: %s", exc)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
