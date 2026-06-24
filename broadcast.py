"""Telegram broadcast and targeted private messaging."""
from __future__ import annotations

from admin_log import logger
from broadcast_engine import deliver_messages_parallel
from database_connector import get_db
from message_deletions import validate_auto_delete_seconds
from notifier import bot_token_configured

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


def _validate_auto_delete(auto_delete_seconds: int | None) -> int | None:
    try:
        return validate_auto_delete_seconds(auto_delete_seconds)
    except ValueError as exc:
        raise BroadcastValidationError(str(exc)) from exc


def _admin_message_text(message_html: str) -> str:
    body = (message_html or "").strip()
    return f"<b>💬 SOLDIUM | رسالة من الإدارة</b>\n\n{body}"


async def send_broadcast(
    message_html: str,
    *,
    auto_delete_seconds: int | None = None,
    on_progress=None,
) -> dict[str, int | bool]:
    text = (message_html or "").strip()
    if not text:
        raise BroadcastValidationError("لا يمكن إرسال رسالة فارغة.")

    if not bot_token_configured():
        raise BroadcastValidationError(
            "لم يتم ضبط BOT_TOKEN — تعذّر إرسال الرسائل عبر تيليغرام.",
        )

    auto_delete_seconds = _validate_auto_delete(auto_delete_seconds)
    user_ids = await list_all_user_ids()
    total = len(user_ids)

    sent, failed = await deliver_messages_parallel(
        user_ids,
        text,
        auto_delete_seconds=auto_delete_seconds,
        on_progress=on_progress,
    )

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


async def send_private_messages(
    user_ids: list[int],
    message_html: str,
    *,
    auto_delete_seconds: int | None = None,
    on_progress=None,
) -> dict[str, int | bool | list[int]]:
    text = (message_html or "").strip()
    if not text:
        raise BroadcastValidationError("لا يمكن إرسال رسالة فارغة.")

    if not bot_token_configured():
        raise BroadcastValidationError(
            "لم يتم ضبط BOT_TOKEN — تعذّر إرسال الرسائل عبر تيليغرام.",
        )

    auto_delete_seconds = _validate_auto_delete(auto_delete_seconds)

    if len(user_ids) > MAX_PRIVATE_RECIPIENTS:
        raise BroadcastValidationError(
            f"الحد الأقصى للمستلمين في رسالة واحدة هو {MAX_PRIVATE_RECIPIENTS}.",
        )

    valid_ids, invalid_ids = await resolve_user_ids(user_ids)
    if not valid_ids:
        raise BroadcastValidationError("لا يوجد أي مستلم صالح من بين المعرّفات المحددة.")

    total = len(valid_ids)
    sent, failed = await deliver_messages_parallel(
        valid_ids,
        _admin_message_text(text),
        auto_delete_seconds=auto_delete_seconds,
        on_progress=on_progress,
    )

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
