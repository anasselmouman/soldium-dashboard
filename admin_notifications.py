"""Admin notification inbox — history, search, and read state."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from html import unescape
from typing import Any

from database_connector import get_db

logger = logging.getLogger("soldium.admin_notifications")

_schema_ensured = False
_schema_lock = asyncio.Lock()
_STRIP_TAGS = re.compile(r"<[^>]+>")

CATEGORY_META: dict[str, dict[str, str]] = {
    "manual_order": {
        "label": "طلب تنفيذ يدوي",
        "icon": "📋",
        "default_action": "/manual-orders",
    },
    "withdrawal": {
        "label": "طلب سحب",
        "icon": "💸",
        "default_action": "/withdrawals",
    },
    "withdrawal_referral": {
        "label": "سحب إحالة",
        "icon": "🎁",
        "default_action": "/withdrawals",
    },
    "deposit_recharge": {
        "label": "تعبئة اتصالات",
        "icon": "📱",
        "default_action": "/deposits",
    },
    "deposit_bank": {
        "label": "إيصال بنكي",
        "icon": "🏦",
        "default_action": "/deposits",
    },
    "system_alert": {
        "label": "تحذير نظام",
        "icon": "⚠️",
        "default_action": "/",
    },
    "provider_auth": {
        "label": "مزوّد API",
        "icon": "🔑",
        "default_action": "/providers",
    },
    "general": {
        "label": "عام",
        "icon": "🔔",
        "default_action": "/notifications",
    },
}

SEVERITY_META: dict[str, dict[str, str]] = {
    "critical": {"label": "حرج", "color": "red"},
    "warning": {"label": "تحذير", "color": "amber"},
    "info": {"label": "معلومة", "color": "sky"},
}


def strip_html_to_plain(html_text: str) -> str:
    text = _STRIP_TAGS.sub(" ", html_text or "")
    return " ".join(unescape(text).split())


async def ensure_admin_notifications_schema() -> None:
    global _schema_ensured
    if _schema_ensured:
        return
    async with _schema_lock:
        if _schema_ensured:
            return
        from db_schema import ensure_admin_notifications_table

        await ensure_admin_notifications_table()
        _schema_ensured = True


def _category_label(category: str) -> str:
    meta = CATEGORY_META.get(category) or CATEGORY_META["general"]
    return str(meta.get("label") or category)


def _category_icon(category: str) -> str:
    meta = CATEGORY_META.get(category) or CATEGORY_META["general"]
    return str(meta.get("icon") or "🔔")


def _action_url(category: str, entity_type: str | None, entity_id: str | None) -> str | None:
    if entity_type == "order" and entity_id:
        return f"/orders?highlight={entity_id}"
    if entity_type == "withdrawal" and entity_id:
        return "/withdrawals"
    if entity_type == "deposit" and entity_id:
        return "/deposits"
    if entity_type == "user" and entity_id:
        return f"/dashboard/users/{entity_id}"
    meta = CATEGORY_META.get(category) or CATEGORY_META["general"]
    return meta.get("default_action")


def _row_to_notification(row) -> dict[str, Any]:
    payload: dict[str, Any] | None = None
    raw = row["payload_json"]
    if raw:
        try:
            payload = json.loads(str(raw))
        except json.JSONDecodeError:
            payload = None
    category = str(row["category"])
    entity_type = row["entity_type"]
    entity_id = row["entity_id"]
    severity = str(row["severity"] or "info")
    return {
        "id": int(row["id"]),
        "category": category,
        "category_label": _category_label(category),
        "category_icon": _category_icon(category),
        "severity": severity,
        "severity_label": SEVERITY_META.get(severity, SEVERITY_META["info"])["label"],
        "title": str(row["title"]),
        "body_html": str(row["body_html"] or ""),
        "body_plain": str(row["body_plain"] or ""),
        "entity_type": entity_type,
        "entity_id": entity_id,
        "user_id": int(row["user_id"]) if row["user_id"] is not None else None,
        "source": str(row["source"] or "bot"),
        "channel": str(row["channel"] or "telegram"),
        "telegram_sent": bool(int(row["telegram_sent"] or 0)),
        "telegram_error": row["telegram_error"],
        "is_read": bool(int(row["is_read"] or 0)),
        "read_at": row["read_at"],
        "payload": payload,
        "created_at": str(row["created_at"]),
        "action_url": _action_url(
            category,
            str(entity_type) if entity_type is not None else None,
            str(entity_id) if entity_id is not None else None,
        ),
    }


def _build_list_filters(
    *,
    unread_only: bool = False,
    category: str | None = None,
    severity: str | None = None,
    search: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> tuple[str, list[Any]]:
    clauses = ["1=1"]
    params: list[Any] = []

    if unread_only:
        clauses.append("is_read = 0")
    if category:
        clauses.append("category = ?")
        params.append(category.strip().lower())
    if severity:
        clauses.append("severity = ?")
        params.append(severity.strip().lower())
    if from_date:
        clauses.append("date(created_at) >= date(?)")
        params.append(from_date)
    if to_date:
        clauses.append("date(created_at) <= date(?)")
        params.append(to_date)
    if search:
        q = f"%{search.strip()}%"
        clauses.append(
            """
            (
                title LIKE ?
                OR body_plain LIKE ?
                OR CAST(COALESCE(entity_id, '') AS TEXT) LIKE ?
                OR CAST(COALESCE(user_id, '') AS TEXT) LIKE ?
            )
            """
        )
        params.extend([q, q, q, q])

    return " AND ".join(clauses), params


async def record_admin_notification(
    *,
    category: str,
    title: str,
    body_html: str,
    severity: str = "info",
    entity_type: str | None = None,
    entity_id: str | None = None,
    user_id: int | None = None,
    source: str = "dashboard",
    channel: str = "telegram",
    telegram_sent: bool = False,
    telegram_error: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int | None:
    await ensure_admin_notifications_schema()
    plain = strip_html_to_plain(body_html) or (title or "").strip()
    payload_json = json.dumps(payload, ensure_ascii=False) if payload else None
    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO admin_notifications (
                category, severity, title, body_html, body_plain,
                entity_type, entity_id, user_id, source, channel,
                telegram_sent, telegram_error, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (category or "general").strip().lower(),
                (severity or "info").strip().lower(),
                (title or "إشعار").strip(),
                (body_html or "").strip(),
                plain,
                entity_type,
                str(entity_id) if entity_id is not None else None,
                user_id,
                (source or "dashboard").strip().lower(),
                (channel or "telegram").strip().lower(),
                1 if telegram_sent else 0,
                telegram_error,
                payload_json,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def list_admin_notifications(
    *,
    limit: int = 50,
    offset: int = 0,
    unread_only: bool = False,
    category: str | None = None,
    severity: str | None = None,
    search: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    await ensure_admin_notifications_schema()
    where_sql, params = _build_list_filters(
        unread_only=unread_only,
        category=category,
        severity=severity,
        search=search,
        from_date=from_date,
        to_date=to_date,
    )
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    async with get_db() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM admin_notifications WHERE {where_sql}",
            params,
        ) as cursor:
            total_row = await cursor.fetchone()
        total = int(total_row[0] if total_row else 0)

        async with db.execute(
            f"""
            SELECT id, category, severity, title, body_html, body_plain,
                   entity_type, entity_id, user_id, source, channel,
                   telegram_sent, telegram_error, is_read, read_at,
                   payload_json, created_at
            FROM admin_notifications
            WHERE {where_sql}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()

    items = [_row_to_notification(row) for row in rows]
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < total,
    }


async def get_admin_notification(notification_id: int) -> dict[str, Any] | None:
    await ensure_admin_notifications_schema()
    async with get_db() as db:
        async with db.execute(
            """
            SELECT id, category, severity, title, body_html, body_plain,
                   entity_type, entity_id, user_id, source, channel,
                   telegram_sent, telegram_error, is_read, read_at,
                   payload_json, created_at
            FROM admin_notifications
            WHERE id = ?
            """,
            (notification_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_notification(row) if row else None


async def mark_notification_read(notification_id: int) -> bool:
    await ensure_admin_notifications_schema()
    async with get_db() as db:
        cursor = await db.execute(
            """
            UPDATE admin_notifications
            SET is_read = 1, read_at = CURRENT_TIMESTAMP
            WHERE id = ? AND is_read = 0
            """,
            (notification_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def mark_all_notifications_read() -> int:
    await ensure_admin_notifications_schema()
    async with get_db() as db:
        cursor = await db.execute(
            """
            UPDATE admin_notifications
            SET is_read = 1, read_at = CURRENT_TIMESTAMP
            WHERE is_read = 0
            """
        )
        await db.commit()
        return cursor.rowcount


async def get_notifications_summary() -> dict[str, Any]:
    await ensure_admin_notifications_schema()
    async with get_db() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM admin_notifications WHERE is_read = 0"
        ) as cursor:
            unread_row = await cursor.fetchone()
        async with db.execute(
            """
            SELECT COUNT(*) FROM admin_notifications
            WHERE date(created_at) = date('now')
            """
        ) as cursor:
            today_row = await cursor.fetchone()
        async with db.execute(
            """
            SELECT COUNT(*) FROM admin_notifications
            WHERE is_read = 0 AND severity = 'critical'
            """
        ) as cursor:
            critical_row = await cursor.fetchone()
        async with db.execute(
            """
            SELECT category, COUNT(*) AS cnt
            FROM admin_notifications
            WHERE is_read = 0
            GROUP BY category
            ORDER BY cnt DESC
            """
        ) as cursor:
            cat_rows = await cursor.fetchall()

    by_category = [
        {
            "category": str(row["category"]),
            "label": _category_label(str(row["category"])),
            "icon": _category_icon(str(row["category"])),
            "count": int(row["cnt"]),
        }
        for row in cat_rows
    ]
    return {
        "unread_count": int(unread_row[0] if unread_row else 0),
        "today_count": int(today_row[0] if today_row else 0),
        "critical_unread": int(critical_row[0] if critical_row else 0),
        "unread_by_category": by_category,
        "categories": [
            {"key": key, **meta}
            for key, meta in CATEGORY_META.items()
        ],
        "severities": [
            {"key": key, **meta}
            for key, meta in SEVERITY_META.items()
        ],
    }
