"""Mass Telegram broadcast to all registered users."""
from __future__ import annotations

import asyncio

from admin_log import logger
from database_connector import get_db
from notifier import bot_token_configured, send_telegram_notification

BROADCAST_DELAY_SECONDS = 0.05
2066400785

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
