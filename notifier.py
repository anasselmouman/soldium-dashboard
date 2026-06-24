"""Send user-facing Telegram messages via the Bot API (dashboard → user)."""
from __future__ import annotations

import asyncio
import html
import logging
import os
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv

from database_connector import get_db
from utils.order_ref import display_order_ref_html

_BASE_DIR = Path(__file__).resolve().parent
load_dotenv(_BASE_DIR / ".env")
load_dotenv(_BASE_DIR.parent / "soldium-bot" / ".env")

logger = logging.getLogger("soldium.notifier")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
_API_BASE = "https://api.telegram.org/bot{token}"
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Matches soldium-bot/utils/smart_notifications.py
DISMISS_CALLBACK_PREFIX = "notify:dismiss:"
ANNOUNCE_DISMISS_CALLBACK_PREFIX = "announce:dismiss:"
DISMISS_BUTTON_TEXT = "✖️ إخفاء الإشعار"
ANNOUNCE_DISMISS_BUTTON_TEXT = "✖️ إخفاء الإعلان"


def _token_configured() -> bool:
    return bool(BOT_TOKEN) and "PASTE" not in BOT_TOKEN and "your_" not in BOT_TOKEN.lower()


def bot_token_configured() -> bool:
    """Whether Telegram Bot API can send messages."""
    return _token_configured()


def _api_url(method: str) -> str:
    return f"{_API_BASE.format(token=BOT_TOKEN)}/{method}"


def dismiss_callback_data(chat_id: int, message_id: int) -> str:
    return f"{DISMISS_CALLBACK_PREFIX}{chat_id}:{message_id}"


def build_dismiss_reply_markup(chat_id: int, message_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": DISMISS_BUTTON_TEXT,
                    "callback_data": dismiss_callback_data(chat_id, message_id),
                }
            ]
        ]
    }


def announce_dismiss_callback_data(announcement_id: int) -> str:
    return f"{ANNOUNCE_DISMISS_CALLBACK_PREFIX}{announcement_id}"


def build_announce_dismiss_reply_markup(announcement_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": ANNOUNCE_DISMISS_BUTTON_TEXT,
                    "callback_data": announce_dismiss_callback_data(announcement_id),
                }
            ]
        ]
    }


def format_timed_announcement_text(message_html: str) -> str:
    body = (message_html or "").strip()
    return f"<b>📢 SOLDIUM | إعلان مؤقت</b>\n\n{body}"


async def send_timed_announcement(
    user_id: int,
    announcement_id: int,
    message_html: str,
    *,
    auto_delete_seconds: int | None = None,
) -> bool:
    """Send a timed announcement — dismiss button only removes the chat message."""
    if not _token_configured():
        logger.warning(
            "BOT_TOKEN not configured — skipping timed announcement for user_id=%s",
            user_id,
        )
        return False

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
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            sent = await _telegram_post(session, "sendMessage", payload)
            if not sent:
                return False

            if auto_delete_seconds is not None:
                result = sent.get("result") or {}
                message_id = result.get("message_id")
                if message_id is not None:
                    from message_deletions import schedule_message_deletion

                    await schedule_message_deletion(
                        chat_id,
                        int(message_id),
                        auto_delete_seconds,
                    )

        logger.info(
            "Timed announcement id=%s sent to user_id=%s auto_delete=%s",
            announcement_id,
            user_id,
            auto_delete_seconds,
        )
        return True
    except aiohttp.ClientError as exc:
        logger.warning(
            "Telegram timed announcement failed for user_id=%s: %s",
            user_id,
            exc,
        )
        return False
    except Exception as exc:
        logger.warning(
            "Unexpected error sending timed announcement to user_id=%s: %s",
            user_id,
            exc,
            exc_info=True,
        )
        return False


def _format_dh(amount: float) -> str:
    text = f"{amount:.6f}".rstrip("0").rstrip(".")
    return text or "0"


async def register_pending_notifications_batch(
    items: list[tuple[int, int, int]],
) -> None:
    """Batch-register pending notifications — one DB connection per flush."""
    if not items:
        return
    try:
        async with get_db() as db:
            for user_id, _chat_id, _message_id in items:
                await db.execute(
                    "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                    (user_id,),
                )
            await db.executemany(
                """
                INSERT OR REPLACE INTO pending_notifications (user_id, chat_id, message_id)
                VALUES (?, ?, ?)
                """,
                items,
            )
            await db.commit()
    except Exception as exc:
        logger.warning("Failed to batch-register pending notifications: %s", exc)


async def _register_pending_notification(
    user_id: int,
    chat_id: int,
    message_id: int,
) -> None:
    """Mirror bot add_pending_notification — enables dismiss + auto-delete on activity."""
    try:
        async with get_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                (user_id,),
            )
            await db.execute(
                """
                INSERT OR REPLACE INTO pending_notifications (user_id, chat_id, message_id)
                VALUES (?, ?, ?)
                """,
                (user_id, chat_id, message_id),
            )
            await db.commit()
    except Exception as exc:
        logger.warning(
            "Failed to register pending notification user_id=%s message_id=%s: %s",
            user_id,
            message_id,
            exc,
        )


async def _telegram_post(
    session: aiohttp.ClientSession,
    method: str,
    payload: dict[str, Any],
    *,
    _rate_limit_attempt: int = 0,
) -> dict[str, Any] | None:
    async with session.post(_api_url(method), json=payload) as response:
        body = await response.json(content_type=None)

    if response.status == 429 and _rate_limit_attempt < 5:
        retry_after = 2.0
        if isinstance(body, dict):
            params = body.get("parameters") or {}
            if isinstance(params, dict) and params.get("retry_after"):
                retry_after = float(params["retry_after"])
        wait = min(max(retry_after, 0.5), 30.0)
        logger.warning(
            "Telegram rate limit on %s — waiting %.1fs",
            method,
            wait,
        )
        await asyncio.sleep(wait)
        return await _telegram_post(
            session,
            method,
            payload,
            _rate_limit_attempt=_rate_limit_attempt + 1,
        )

    if response.status == 200 and isinstance(body, dict) and body.get("ok"):
        return body
    description = body.get("description", body) if isinstance(body, dict) else body
    logger.warning("Telegram %s failed: %s", method, description)
    return None


async def delete_telegram_message(chat_id: int, message_id: int) -> bool:
    """Delete a message via Bot API. Returns True on success or if already gone."""
    if not _token_configured():
        return False
    payload = {"chat_id": chat_id, "message_id": message_id}
    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            result = await _telegram_post(session, "deleteMessage", payload)
        return result is not None
    except Exception as exc:
        logger.debug(
            "deleteMessage chat_id=%s message_id=%s: %s",
            chat_id,
            message_id,
            exc,
        )
        return False


async def send_telegram_notification(
    user_id: int,
    text: str,
    *,
    auto_delete_seconds: int | None = None,
) -> bool:
    """
    Send a smart notification: message + dismiss button + pending_notifications row.
    When auto_delete_seconds is set, skip pending_notifications and schedule deletion.
    Failures are logged; callers keep HTTP success.
    """
    if not _token_configured():
        logger.warning(
            "BOT_TOKEN not configured — skipping Telegram notification for user_id=%s",
            user_id,
        )
        return False

    chat_id = user_id
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            sent = await _telegram_post(session, "sendMessage", payload)
            if not sent:
                return False

            result = sent.get("result") or {}
            message_id = result.get("message_id")
            if message_id is None:
                logger.warning(
                    "Telegram sendMessage missing message_id for user_id=%s",
                    user_id,
                )
                return True

            message_id = int(message_id)
            markup = build_dismiss_reply_markup(chat_id, message_id)
            await _telegram_post(
                session,
                "editMessageReplyMarkup",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": markup,
                },
            )

        if auto_delete_seconds is not None:
            from message_deletions import schedule_message_deletion

            await schedule_message_deletion(chat_id, message_id, auto_delete_seconds)
        else:
            await _register_pending_notification(user_id, chat_id, message_id)

        logger.info(
            "Smart notification sent to user_id=%s (message_id=%s auto_delete=%s)",
            user_id,
            message_id,
            auto_delete_seconds,
        )
        return True
    except aiohttp.ClientError as exc:
        logger.warning(
            "Telegram request failed for user_id=%s: %s",
            user_id,
            exc,
        )
        return False
    except Exception as exc:
        logger.warning(
            "Unexpected error sending Telegram to user_id=%s: %s",
            user_id,
            exc,
            exc_info=True,
        )
        return False


def _escape(text: str | None, *, fallback: str) -> str:
    raw = (text or "").strip()
    return html.escape(raw if raw else fallback)


async def notify_deposit_approved(user_id: int, amount_dh: float) -> bool:
    amount = html.escape(_format_dh(amount_dh))
    text = (
        "✅ <b>تم قبول إيداعك!</b>\n"
        f"تمت إضافة <code>{amount} درهم</code> إلى رصيدك بنجاح."
    )
    return await send_telegram_notification(user_id, text)


async def notify_deposit_rejected(
    user_id: int,
    *,
    reason: str | None = None,
) -> bool:
    reason_html = _escape(reason, fallback="لم يُحدد سبب")
    text = (
        "❌ <b>تم رفض عملية الإيداع.</b>\n"
        f"السبب: {reason_html}\n"
        "يرجى مراجعة الدعم الفني إذا كان لديك استفسار."
    )
    return await send_telegram_notification(user_id, text)


async def notify_balance_adjusted(
    user_id: int,
    *,
    amount_dh: float,
    new_balance: float,
    reason: str | None = None,
) -> bool:
    amount = html.escape(_format_dh(amount_dh))
    balance = html.escape(_format_dh(new_balance))
    reason_html = _escape(reason, fallback="تعديل إداري")
    sign = "+" if amount_dh > 0 else ""
    text = (
        "🔔 <b>تحديث في الرصيد</b>\n"
        f"تم تعديل رصيدك بمقدار <code>{sign}{amount} درهم</code>.\n"
        f"السبب: {reason_html}\n"
        f"رصيدك الحالي: <code>{balance} درهم</code>."
    )
    return await send_telegram_notification(user_id, text)


async def notify_referral_level_changed(user_id: int, new_level: int) -> bool:
    level = html.escape(str(new_level))
    text = (
        "🎉 <b>ترقية مستوى الإحالة!</b>\n"
        f"تمت ترقية حسابك إلى المستوى <code>{level}</code>."
    )
    return await send_telegram_notification(user_id, text)



def _order_status_label_ar(status: str) -> str:
    from utils.order_status import normalize_order_status_key, status_label_ar

    return status_label_ar(normalize_order_status_key(status))


def _customer_order_display_ref(provider_order_id: str | None) -> str:
    return display_order_ref_html(provider_order_id)


async def notify_order_status_changed(
    user_id: int,
    *,
    provider_order_id: str | None,
    new_status: str,
    refunded_dh: float = 0.0,
) -> bool:
    display_id = _customer_order_display_ref(provider_order_id)
    status_ar = html.escape(_order_status_label_ar(new_status))
    status_key = new_status.strip().lower().replace("_", " ")
    lines = [
        "🔔 <b>تحديث في حالة الطلب</b>",
        f"رقم الطلب: <code>{display_id}</code>",
        f"الحالة الجديدة: <b>{status_ar}</b>",
    ]
    if status_key in {"canceled", "cancelled", "refunded", "failed"} or refunded_dh > 0:
        refund = html.escape(_format_dh(refunded_dh))
        lines.append(
            f"تم استرجاع <code>{refund} درهم</code> إلى رصيدك."
        )
    text = "\n".join(lines)
    return await send_telegram_notification(user_id, text)


async def notify_withdrawal_approved(
    user_id: int,
    *,
    amount_dh: float,
    method: str,
) -> bool:
    amount = html.escape(_format_dh(amount_dh))
    method_html = _escape(method, fallback="طريقة السحب")
    text = (
        "✅ <b>تم تنفيذ طلب السحب الخاص بك بنجاح</b>\n"
        f"المبلغ: <code>{amount} درهم</code>\n"
        f"الطريقة: <b>{method_html}</b>\n"
        "تم إرسال المبلغ إلى محفظتك/حسابك حسب البيانات التي قدّمتها."
    )
    return await send_telegram_notification(user_id, text)


async def notify_manual_order_completed(
    user_id: int,
    *,
    provider_order_id: str | None = None,
) -> bool:
    display_ref = _customer_order_display_ref(provider_order_id)
    text = (
        "<b>✅ SOLDIUM | تم تنفيذ طلبك</b>\n"
        f"تم تنفيذ الطلب <code>#{display_ref}</code> بنجاح.\n"
        "شكراً لثقتك بنا!"
    )
    return await send_telegram_notification(user_id, text)


async def notify_admin_system_alert(
    title: str,
    message: str,
    *,
    message_html: str | None = None,
) -> bool:
    """Send a critical dashboard alert to the admin Telegram chat."""
    from admin_notifications import record_admin_notification
    from config import ADMIN_TELEGRAM_ID

    title_clean = (title or "").strip() or "تحذير نظام"
    title_html = html.escape(title_clean)

    if not ADMIN_TELEGRAM_ID:
        logger.warning("ADMIN_ID not configured — skipping admin system alert")
        body_for_log = message_html or (message or "").strip()
        await record_admin_notification(
            category="system_alert",
            title=title_clean,
            body_html=body_for_log,
            severity="critical",
            source="dashboard",
            channel="dashboard",
            telegram_sent=False,
            telegram_error="ADMIN_ID not configured",
        )
        return False

    if message_html:
        text = f"<b>⚠️ SOLDIUM | {title_html}</b>\n\n{message_html}"
    else:
        body_html = html.escape((message or "").strip())
        text = f"<b>⚠️ SOLDIUM | {title_html}</b>\n\n{body_html}"
    sent = await send_telegram_notification(ADMIN_TELEGRAM_ID, text)
    await record_admin_notification(
        category="system_alert",
        title=title_clean,
        body_html=text,
        severity="critical",
        source="dashboard",
        channel="telegram",
        telegram_sent=sent,
        telegram_error=None if sent else "Telegram send failed",
    )
    return sent


async def notify_admin_direct_message(
    user_id: int,
    message_html: str,
    *,
    auto_delete_seconds: int | None = None,
) -> bool:
    """Admin-composed HTML message without order context."""
    body = (message_html or "").strip()
    text = f"<b>💬 SOLDIUM | رسالة من الإدارة</b>\n\n{body}"
    return await send_telegram_notification(
        user_id,
        text,
        auto_delete_seconds=auto_delete_seconds,
    )


async def notify_manual_order_customer_message(
    user_id: int,
    *,
    provider_order_id: str | None = None,
    message: str,
) -> bool:
    display_ref = _customer_order_display_ref(provider_order_id)
    body = _escape(message, fallback="")
    text = (
        "<b>💬 SOLDIUM | رسالة من الإدارة</b>\n"
        f"بخصوص طلبك <code>#{display_ref}</code>:\n\n"
        f"{body}"
    )
    return await send_telegram_notification(user_id, text)


async def notify_manual_order_rejected(
    user_id: int,
    *,
    provider_order_id: str | None = None,
    amount_dh: float,
    reason: str | None = None,
) -> bool:
    display_ref = _customer_order_display_ref(provider_order_id)
    amount = html.escape(_format_dh(amount_dh))
    lines = [
        "<b>⚠️ SOLDIUM | تم رفض الطلب</b>",
        f"تعذّر تنفيذ الطلب <code>#{display_ref}</code>.",
        f"تم إرجاع <b>{amount} درهم</b> إلى رصيدك.",
    ]
    reason_text = (reason or "").strip()
    if reason_text:
        lines.append(f"السبب: {_escape(reason_text, fallback='')}")
    lines.append("تواصل مع الدعم إذا احتجت مساعدة.")
    return await send_telegram_notification(user_id, "\n".join(lines))


async def notify_withdrawal_rejected(
    user_id: int,
    *,
    amount_dh: float,
    reason: str | None = None,
    withdrawal_type: str = "normal",
) -> bool:
    amount = html.escape(_format_dh(amount_dh))
    reason_html = _escape(reason, fallback="لم يُحدد سبب")
    balance_label = (
        "رصيد أرباح الإحالة"
        if (withdrawal_type or "").strip().lower() == "referral"
        else "رصيدك القابل للإنفاق"
    )
    text = (
        "❌ <b>تم رفض طلب السحب</b>\n"
        f"تمت إعادة <code>{amount} درهم</code> إلى {balance_label}.\n"
        f"السبب: {reason_html}"
    )
    return await send_telegram_notification(user_id, text)
