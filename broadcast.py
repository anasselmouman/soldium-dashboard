"""Telegram broadcast and targeted private messaging."""
from __future__ import annotations

import asyncio

from admin_log import logger
from database_connector import get_db
from notifier import (
    bot_token_configured,
    notify_admin_direct_message,
    send_telegram_notification,
)

BROADCAST_DELAY_SECONDS = 0.05
MAX_PRIVATE_RECIPIENTS = 100

class BroadcastValidationError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


async def list_all_user_ids() -> list[int]:
    async with get_db() as db:
        async with db.execute("SELECT user_id FROM users ORDER BY user_id") as cursor:
            rows = await cursor.fetchall()
    return [int(row[0]) for row in rows]


async def send_broadcast(message_html: str) -> dict[str, int | bool]:
    text = (message_html or "").strip()
    if not text:
        raise BroadcastValidationError("لا يمكن إرسال رسالة فارغة.")

    if not bot_token_configured():
        raise BroadcastValidationError(
            "لم يتم ضبط BOT_TOKEN — تعذّر إرسال الرسائل عبر تيليغرام.",
        )

    user_ids = await list_all_user_ids()
    total = len(user_ids)
    sent = 0
    failed = 0

    for user_id in user_ids:
        try:
            ok = await send_telegram_notification(user_id, text)
        except Exception as exc:
            logger.warning("broadcast user_id=%s unexpected error: %s", user_id, exc)
            ok = False
        if ok:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY_SECONDS)

    logger.info(
        "BROADCAST completed total_users=%s sent=%s failed=%s",
        total,
        sent,
        failed,
    )
    return {
        "ok": True,
        "total_users": total,
        "sent": sent,
        "failed": failed,
    }


async def resolve_user_ids(user_ids: list[int]) -> tuple[list[int], list[int]]:
    """Return (valid_ids, invalid_ids) after deduplication and DB lookup."""
    unique: list[int] = list(dict.fromkeys(user_ids))
    if not unique:
        return [], []

    placeholders = ",".join("?" for _ in unique)
    async with get_db() as db:
        async with db.execute(
            f"SELECT user_id FROM users WHERE user_id IN ({placeholders})",
            unique,
        ) as cursor:
            rows = await cursor.fetchall()

    found = {int(row[0]) for row in rows}
    valid = [uid for uid in unique if uid in found]
    invalid = [uid for uid in unique if uid not in found]
    return valid, invalid


async def send_private_messages(user_ids: list[int], message_html: str) -> dict[str, int | bool | list[int]]:
    text = (message_html or "").strip()
    if not text:
        raise BroadcastValidationError("لا يمكن إرسال رسالة فارغة.")

    if not bot_token_configured():
        raise BroadcastValidationError(
            "لم يتم ضبط BOT_TOKEN — تعذّر إرسال الرسائل عبر تيليغرام.",
        )

    if len(user_ids) > MAX_PRIVATE_RECIPIENTS:
        raise BroadcastValidationError(
            f"الحد الأقصى للمستلمين في رسالة واحدة هو {MAX_PRIVATE_RECIPIENTS}.",
        )

    valid_ids, invalid_ids = await resolve_user_ids(user_ids)
    if not valid_ids:
        raise BroadcastValidationError("لا يوجد أي مستلم صالح من بين المعرّفات المحددة.")

    total = len(valid_ids)
    sent = 0
    failed = 0

    for user_id in valid_ids:
        try:
            ok = await notify_admin_direct_message(user_id, text)
        except Exception as exc:
            logger.warning("private message user_id=%s unexpected error: %s", user_id, exc)
            ok = False
        if ok:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY_SECONDS)

    logger.info(
        "PRIVATE_MESSAGE completed recipients=%s sent=%s failed=%s invalid=%s",
        total,
        sent,
        failed,
        len(invalid_ids),
    )
    return {
        "ok": True,
        "total_recipients": total,
        "sent": sent,
        "failed": failed,
        "invalid_user_ids": invalid_ids,
    }
