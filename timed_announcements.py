"""Timed announcements — broadcast on launch and on every /start while active."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from admin_log import logger
from broadcast import BroadcastValidationError, list_all_user_ids
from broadcast_engine import deliver_timed_announcements_parallel
from database_connector import get_db
from notifier import bot_token_configured

STATUS_ACTIVE = "active"
STATUS_STOPPED = "stopped"
STATUS_EXPIRED = "expired"


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_ends_at(value: str) -> datetime:
    raw = (value or "").strip()
    if not raw:
        raise BroadcastValidationError("حدّد تاريخ ووقت انتهاء الإعلان.")
    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise BroadcastValidationError("صيغة تاريخ الانتهاء غير صالحة.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _expire_past_announcements(db) -> None:
    now = _utc_now_str()
    await db.execute(
        """
        UPDATE timed_announcements
        SET status = ?
        WHERE status = ? AND ends_at <= ?
        """,
        (STATUS_EXPIRED, STATUS_ACTIVE, now),
    )


def _row_to_announcement(row) -> dict[str, Any]:
    auto_delete = row["auto_delete_seconds"]
    return {
        "id": int(row["id"]),
        "message_html": str(row["message_html"]),
        "ends_at": str(row["ends_at"]),
        "status": str(row["status"]),
        "created_at": str(row["created_at"]),
        "launched_at": str(row["launched_at"]) if row["launched_at"] else None,
        "stopped_at": str(row["stopped_at"]) if row["stopped_at"] else None,
        "auto_delete_seconds": int(auto_delete) if auto_delete is not None else None,
    }


def _strip_html_preview(html: str, max_len: int = 100) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


async def list_active_timed_announcements() -> list[dict[str, Any]]:
    """All currently active, non-expired announcements (multiple allowed)."""
    now = _utc_now_str()
    async with get_db() as db:
        await _expire_past_announcements(db)
        await db.commit()
        async with db.execute(
            """
            SELECT ta.id, ta.message_html, ta.ends_at, ta.launched_at
            FROM timed_announcements ta
            WHERE ta.status = ?
              AND ta.ends_at > ?
            ORDER BY ta.id ASC
            """,
            (STATUS_ACTIVE, now),
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "ends_at": str(row["ends_at"]),
            "launched_at": str(row["launched_at"]) if row["launched_at"] else None,
            "preview": _strip_html_preview(str(row["message_html"])),
        }
        for row in rows
    ]


async def count_active_timed_announcements() -> int:
    items = await list_active_timed_announcements()
    return len(items)


async def list_timed_announcements(*, limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    async with get_db() as db:
        await _expire_past_announcements(db)
        await db.commit()
        async with db.execute(
            """
            SELECT ta.*
            FROM timed_announcements ta
            ORDER BY ta.id DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_announcement(row) for row in rows]


async def get_timed_announcement(announcement_id: int) -> dict[str, Any] | None:
    async with get_db() as db:
        await _expire_past_announcements(db)
        await db.commit()
        async with db.execute(
            "SELECT ta.* FROM timed_announcements ta WHERE ta.id = ?",
            (announcement_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_announcement(row) if row else None


async def prepare_timed_announcement(
    message_html: str,
    ends_at: str,
    *,
    auto_delete_seconds: int | None = None,
) -> dict[str, Any]:
    """Validate and persist announcement; return metadata for background delivery."""
    text = (message_html or "").strip()
    if not text:
        raise BroadcastValidationError("لا يمكن إرسال رسالة فارغة.")

    if not bot_token_configured():
        raise BroadcastValidationError(
            "لم يتم ضبط BOT_TOKEN — تعذّر إرسال الرسائل عبر تيليغرام.",
        )

    from message_deletions import validate_auto_delete_seconds

    try:
        auto_delete_seconds = validate_auto_delete_seconds(auto_delete_seconds)
    except ValueError as exc:
        raise BroadcastValidationError(str(exc)) from exc

    ends_dt = _parse_ends_at(ends_at)
    now_dt = datetime.now(timezone.utc)
    if ends_dt <= now_dt:
        raise BroadcastValidationError("وقت الانتهاء يجب أن يكون في المستقبل.")

    ends_at_str = ends_dt.strftime("%Y-%m-%d %H:%M:%S")
    launched_at = _utc_now_str()

    async with get_db() as db:
        await _expire_past_announcements(db)
        cursor = await db.execute(
            """
            INSERT INTO timed_announcements (message_html, ends_at, status, launched_at, auto_delete_seconds)
            VALUES (?, ?, ?, ?, ?)
            """,
            (text, ends_at_str, STATUS_ACTIVE, launched_at, auto_delete_seconds),
        )
        announcement_id = int(cursor.lastrowid)
        await db.commit()

    user_ids = await list_all_user_ids()
    announcement = await get_timed_announcement(announcement_id)
    return {
        "announcement": announcement,
        "user_ids": user_ids,
        "message_html": text,
        "auto_delete_seconds": auto_delete_seconds,
    }


async def deliver_timed_announcement_to_users(
    announcement_id: int,
    message_html: str,
    *,
    auto_delete_seconds: int | None = None,
    on_progress=None,
) -> dict[str, Any]:
    user_ids = await list_all_user_ids()
    total = len(user_ids)
    sent, failed = await deliver_timed_announcements_parallel(
        user_ids,
        announcement_id=announcement_id,
        message_html=message_html,
        auto_delete_seconds=auto_delete_seconds,
        on_progress=on_progress,
    )

    logger.info(
        "TIMED_ANNOUNCEMENT delivered id=%s total=%s sent=%s failed=%s",
        announcement_id,
        total,
        sent,
        failed,
    )

    announcement = await get_timed_announcement(announcement_id)
    return {
        "ok": True,
        "announcement": announcement,
        "total_users": total,
        "sent": sent,
        "failed": failed,
    }


async def launch_timed_announcement(
    message_html: str,
    ends_at: str,
    *,
    auto_delete_seconds: int | None = None,
    on_progress=None,
) -> dict[str, Any]:
    """Synchronous launch (used by background job runner after prepare)."""
    prepared = await prepare_timed_announcement(
        message_html,
        ends_at,
        auto_delete_seconds=auto_delete_seconds,
    )
    announcement = prepared["announcement"]
    announcement_id = int(announcement["id"])
    return await deliver_timed_announcement_to_users(
        announcement_id,
        prepared["message_html"],
        auto_delete_seconds=prepared["auto_delete_seconds"],
        on_progress=on_progress,
    )


async def stop_timed_announcement(announcement_id: int) -> dict[str, Any]:
    now = _utc_now_str()
    async with get_db() as db:
        await _expire_past_announcements(db)
        async with db.execute(
            "SELECT id, status FROM timed_announcements WHERE id = ?",
            (announcement_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise BroadcastValidationError("الإعلان المؤقت غير موجود.")
        if str(row["status"]) != STATUS_ACTIVE:
            raise BroadcastValidationError("الإعلان ليس نشطاً — لا يمكن إيقافه.")

        await db.execute(
            """
            UPDATE timed_announcements
            SET status = ?, stopped_at = ?
            WHERE id = ?
            """,
            (STATUS_STOPPED, now, announcement_id),
        )
        await db.commit()

    logger.info("TIMED_ANNOUNCEMENT stopped id=%s", announcement_id)
    announcement = await get_timed_announcement(announcement_id)
    return {"ok": True, "announcement": announcement}
