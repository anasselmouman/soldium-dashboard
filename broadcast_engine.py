"""Fast, safe parallel Telegram delivery for broadcasts."""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from notifier import (
    _REQUEST_TIMEOUT,
    _telegram_post,
    build_announce_dismiss_reply_markup,
    build_dismiss_reply_markup,
    format_timed_announcement_text,
)

logger = logging.getLogger("soldium.broadcast_engine")

BROADCAST_CONCURRENCY = max(1, min(int(os.getenv("BROADCAST_CONCURRENCY", "6")), 12))
DB_FLUSH_EVERY = 40

ProgressCallback = Callable[[int, int], Awaitable[None]]


@dataclass(frozen=True)
class _SendOutcome:
    ok: bool
    user_id: int
    chat_id: int
    message_id: int | None


async def _send_notification_message(
    session: aiohttp.ClientSession,
    user_id: int,
    text: str,
) -> _SendOutcome:
    chat_id = user_id
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        sent = await _telegram_post(session, "sendMessage", payload)
        if not sent:
            return _SendOutcome(False, user_id, chat_id, None)

        result = sent.get("result") or {}
        message_id = result.get("message_id")
        if message_id is None:
            return _SendOutcome(True, user_id, chat_id, None)

        message_id = int(message_id)
        await _telegram_post(
            session,
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": build_dismiss_reply_markup(chat_id, message_id),
            },
        )
        return _SendOutcome(True, user_id, chat_id, message_id)
    except aiohttp.ClientError as exc:
        logger.warning("broadcast send failed user_id=%s: %s", user_id, exc)
        return _SendOutcome(False, user_id, chat_id, None)


async def _send_timed_announcement_message(
    session: aiohttp.ClientSession,
    user_id: int,
    *,
    announcement_id: int,
    message_html: str,
) -> _SendOutcome:
    chat_id = user_id
    text = format_timed_announcement_text(message_html)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": build_announce_dismiss_reply_markup(announcement_id),
    }
    try:
        sent = await _telegram_post(session, "sendMessage", payload)
        if not sent:
            return _SendOutcome(False, user_id, chat_id, None)
        result = sent.get("result") or {}
        message_id = result.get("message_id")
        return _SendOutcome(
            True,
            user_id,
            chat_id,
            int(message_id) if message_id is not None else None,
        )
    except aiohttp.ClientError as exc:
        logger.warning(
            "timed announcement send failed user_id=%s id=%s: %s",
            user_id,
            announcement_id,
            exc,
        )
        return _SendOutcome(False, user_id, chat_id, None)


async def _flush_pending_notifications(
    items: list[tuple[int, int, int]],
) -> None:
    if not items:
        return
    from notifier import register_pending_notifications_batch

    await register_pending_notifications_batch(items)


async def _flush_scheduled_deletions(
    items: list[tuple[int, int, int]],
) -> None:
    if not items:
        return
    from message_deletions import schedule_message_deletions_batch

    await schedule_message_deletions_batch(items)


async def _run_parallel_delivery(
    user_ids: list[int],
    send_fn,
    *,
    auto_delete_seconds: int | None,
    on_progress: ProgressCallback | None,
) -> tuple[int, int]:
    if not user_ids:
        return 0, 0

    semaphore = asyncio.Semaphore(BROADCAST_CONCURRENCY)
    sent = 0
    failed = 0
    progress_lock = asyncio.Lock()
    pending_notifications: list[tuple[int, int, int]] = []
    scheduled_deletions: list[tuple[int, int, int]] = []
    db_lock = asyncio.Lock()

    async def _maybe_flush_db() -> None:
        async with db_lock:
            if auto_delete_seconds is not None:
                if len(scheduled_deletions) >= DB_FLUSH_EVERY:
                    batch = scheduled_deletions[:]
                    scheduled_deletions.clear()
                    await _flush_scheduled_deletions(batch)
            elif len(pending_notifications) >= DB_FLUSH_EVERY:
                batch = pending_notifications[:]
                pending_notifications.clear()
                await _flush_pending_notifications(batch)

    async def _record_progress(ok: bool) -> None:
        nonlocal sent, failed
        async with progress_lock:
            if ok:
                sent += 1
            else:
                failed += 1
            if on_progress is not None:
                await on_progress(sent, failed)

    async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        async def _send_one(user_id: int) -> None:
            async with semaphore:
                outcome = await send_fn(session, user_id)

            if outcome.ok and outcome.message_id is not None:
                async with db_lock:
                    if auto_delete_seconds is not None:
                        scheduled_deletions.append(
                            (outcome.chat_id, outcome.message_id, auto_delete_seconds),
                        )
                    else:
                        pending_notifications.append(
                            (outcome.user_id, outcome.chat_id, outcome.message_id),
                        )
                await _maybe_flush_db()

            await _record_progress(outcome.ok)

        await asyncio.gather(*(_send_one(uid) for uid in user_ids))

    async with db_lock:
        if auto_delete_seconds is not None:
            await _flush_scheduled_deletions(scheduled_deletions)
        else:
            await _flush_pending_notifications(pending_notifications)

    return sent, failed


async def deliver_messages_parallel(
    user_ids: list[int],
    text: str,
    *,
    auto_delete_seconds: int | None = None,
    on_progress: ProgressCallback | None = None,
) -> tuple[int, int]:
    async def _send(session: aiohttp.ClientSession, user_id: int) -> _SendOutcome:
        return await _send_notification_message(session, user_id, text)

    return await _run_parallel_delivery(
        user_ids,
        _send,
        auto_delete_seconds=auto_delete_seconds,
        on_progress=on_progress,
    )


async def deliver_timed_announcements_parallel(
    user_ids: list[int],
    *,
    announcement_id: int,
    message_html: str,
    auto_delete_seconds: int | None = None,
    on_progress: ProgressCallback | None = None,
) -> tuple[int, int]:
    async def _send(session: aiohttp.ClientSession, user_id: int) -> _SendOutcome:
        return await _send_timed_announcement_message(
            session,
            user_id,
            announcement_id=announcement_id,
            message_html=message_html,
        )

    return await _run_parallel_delivery(
        user_ids,
        _send,
        auto_delete_seconds=auto_delete_seconds,
        on_progress=on_progress,
    )
