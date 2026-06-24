"""Pending manual-fulfillment order queue and admin actions for the dashboard."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from admin_log import logger
from database_connector import (
    DatabaseLockedError,
    DatabaseWriteError,
    db_transaction,
    get_db,
)
from orders import (
    OrderNotFoundError,
    OrderStatusError,
    get_order,
    update_order_status,
)
from utils.messages_ar import (
    manual_order_already_processed,
    manual_order_not_found,
    manual_order_not_pending,
)
from utils.money import to_float
from utils.order_status import normalize_order_status_key
from services.smm_provider import (
    ProviderUnavailableError,
    submit_provider_order,
)
from services.provider_registry import get_default_provider_slug

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

_PENDING_MANUAL_ORDERS_SQL = """
    SELECT
        o.id,
        o.user_id,
        u.telegram_name,
        u.balance,
        u.total_spent,
        o.service_name,
        o.service_id,
        o.link,
        o.quantity,
        o.amount,
        o.status,
        COALESCE(o.fulfillment_mode, 'auto') AS fulfillment_mode,
        o.api_account,
        o.provider_order_id,
        o.created_at,
        (
            SELECT COUNT(*)
            FROM orders o2
            WHERE o2.user_id = o.user_id
        ) AS user_orders_count
    FROM orders AS o
    INNER JOIN users AS u ON u.user_id = o.user_id
    WHERE LOWER(REPLACE(o.status, '_', ' ')) = 'pending admin'
      AND COALESCE(o.fulfillment_mode, 'auto') = 'admin'
    ORDER BY o.id ASC
"""

_MANUAL_ORDER_BY_ID_SQL = """
    SELECT
        o.id,
        o.user_id,
        u.telegram_name,
        u.balance,
        u.total_spent,
        o.service_name,
        o.service_id,
        o.link,
        o.quantity,
        o.amount,
        o.status,
        COALESCE(o.fulfillment_mode, 'auto') AS fulfillment_mode,
        o.api_account,
        o.provider_order_id,
        COALESCE(o.refunded_amount, 0) AS refunded_amount,
        o.status_note,
        o.created_at,
        (
            SELECT COUNT(*)
            FROM orders o2
            WHERE o2.user_id = o.user_id
        ) AS user_orders_count
    FROM orders AS o
    INNER JOIN users AS u ON u.user_id = o.user_id
    WHERE o.id = ?
      AND COALESCE(o.fulfillment_mode, 'auto') = 'admin'
"""

_HISTORY_STATUSES = frozenset({"completed", "canceled", "failed"})


class ManualOrderNotFoundError(Exception):
    pass


class ManualOrderNotPendingError(Exception):
    pass


class ManualOrderAlreadyProcessedError(Exception):
    pass


def _parse_created_at(created_at: str | None) -> datetime | None:
    if not created_at:
        return None
    text = str(created_at).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _waiting_seconds(created_at: str | None) -> int:
    dt = _parse_created_at(created_at)
    if dt is None:
        return 0
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int((now - dt).total_seconds()))


def _waiting_label(seconds: int) -> str:
    if seconds < 60:
        return "أقل من دقيقة"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} د"
    hours = minutes // 60
    rem_min = minutes % 60
    if rem_min:
        return f"{hours}س {rem_min}د"
    return f"{hours}س"


def _row_to_manual_order(row: aiosqlite.Row, *, include_note: bool = False) -> dict[str, Any]:
    telegram_name = row["telegram_name"]
    name = str(telegram_name).strip() if telegram_name else ""
    waiting = _waiting_seconds(row["created_at"])
    item: dict[str, Any] = {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "telegram_name": name or None,
        "display_name": name if name else str(row["user_id"]),
        "user_balance": float(row["balance"] or 0.0),
        "user_total_spent": float(row["total_spent"] or 0.0),
        "user_orders_count": int(row["user_orders_count"] or 0),
        "service_name": str(row["service_name"] or ""),
        "service_id": str(row["service_id"] or ""),
        "link": str(row["link"] or ""),
        "quantity": int(row["quantity"]),
        "amount": float(row["amount"] or 0.0),
        "status": str(row["status"]),
        "status_key": normalize_order_status_key(row["status"]),
        "fulfillment_mode": str(row["fulfillment_mode"] or "admin"),
        "api_account": str(row["api_account"] or "admin"),
        "provider_order_id": (
            str(row["provider_order_id"]) if row["provider_order_id"] else None
        ),
        "created_at": str(row["created_at"]),
        "waiting_seconds": waiting,
        "waiting_label": _waiting_label(waiting),
    }
    if include_note:
        note = row["status_note"]
        item["status_note"] = str(note) if note is not None else None
        item["refunded_amount"] = float(row["refunded_amount"] or 0.0)
    return item


def _resolve_api_account(api_account: str | None) -> str:
    account = str(api_account or "default").strip().lower()
    if account == "admin" or not account:
        return "default"
    return account


async def _persist_provider_order_id(order_id: int, provider_ref: str) -> None:
    ref = str(provider_ref or "").strip()
    if not ref:
        return
    async with db_transaction() as db:
        await db.execute(
            "UPDATE orders SET provider_order_id = ? WHERE id = ?",
            (ref, order_id),
        )


async def _lookup_service_provider_meta(catalog_item_id: str) -> dict[str, str] | None:
    item_id = str(catalog_item_id or "").strip()
    if not item_id:
        return None
    async with get_db() as db:
        try:
            async with db.execute(
                """
                SELECT external_service_id, provider_slug, provider_api_account
                FROM smm_services
                WHERE catalog_id = ? OR local_item_id = ?
                LIMIT 1
                """,
                (item_id, item_id),
            ) as cursor:
                row = await cursor.fetchone()
        except sqlite3.OperationalError:
            async with db.execute(
                """
                SELECT service_id AS external_service_id, provider_slug, provider_api_account
                FROM smm_services
                WHERE service_id = ? OR local_item_id = ?
                LIMIT 1
                """,
                (item_id, item_id),
            ) as cursor:
                row = await cursor.fetchone()
    if row is None:
        return None
    external_id = str(row["external_service_id"] or "").strip()
    if not external_id.isdigit():
        return None
    slug = str(row["provider_slug"] or get_default_provider_slug()).strip().lower()
    account = str(row["provider_api_account"] or "default").strip().lower() or "default"
    return {
        "external_service_id": external_id,
        "provider_slug": slug,
        "account_key": account,
    }


async def ensure_provider_order_ref(order: dict[str, Any]) -> str | None:
    """
    Return distributor order id for a manual order.
    If missing locally, submit once to the distributor API and persist the ref.
    """
    existing = str(order.get("provider_order_id") or "").strip()
    if existing:
        return existing

    order_id = int(order["id"])
    catalog_item_id = str(order.get("service_id") or "").strip()
    link = str(order.get("link") or "").strip()
    if not catalog_item_id or not link:
        logger.warning(
            "ensure_provider_order_ref missing service/link order_id=%s",
            order_id,
        )
        return None

    meta = await _lookup_service_provider_meta(catalog_item_id)
    if meta is None:
        logger.warning(
            "ensure_provider_order_ref catalog lookup failed order_id=%s item=%s",
            order_id,
            catalog_item_id,
        )
        return None

    provider_slug = str(order.get("provider_slug") or meta["provider_slug"]).strip().lower()
    account = _resolve_api_account(order.get("api_account") or meta["account_key"])

    try:
        provider_ref = await submit_provider_order(
            provider_slug=provider_slug,
            account_key=account,
            service_id=int(meta["external_service_id"]),
            link=link,
            quantity=int(order.get("quantity") or 1),
        )
    except ProviderUnavailableError as exc:
        logger.warning(
            "ensure_provider_order_ref submit failed order_id=%s: %s",
            order_id,
            exc,
        )
        return None
    except Exception as exc:
        logger.exception(
            "ensure_provider_order_ref unexpected error order_id=%s",
            order_id,
        )
        return None

    await _persist_provider_order_id(order_id, provider_ref)
    order["provider_order_id"] = provider_ref
    logger.info(
        "ASSIGN_PROVIDER_ORDER_REF order_id=%s provider_ref=%s",
        order_id,
        provider_ref,
    )
    return provider_ref


def _is_pending_manual(order: dict[str, Any]) -> bool:
    return normalize_order_status_key(order["status"]) == "pending admin"


async def get_pending_manual_orders() -> list[dict[str, Any]]:
    async with get_db() as db:
        async with db.execute(_PENDING_MANUAL_ORDERS_SQL) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_manual_order(row) for row in rows]


async def get_pending_manual_orders_summary() -> dict[str, Any]:
    orders = await get_pending_manual_orders()
    total_amount = sum(to_float(o["amount"]) for o in orders)
    oldest_waiting = max((o["waiting_seconds"] for o in orders), default=0)
    return {
        "count": len(orders),
        "total_amount_dh": round(total_amount, 2),
        "oldest_waiting_seconds": oldest_waiting,
        "orders": orders,
    }


async def get_manual_order(order_id: int) -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(_MANUAL_ORDER_BY_ID_SQL, (order_id,)) as cursor:
            row = await cursor.fetchone()
    return _row_to_manual_order(row, include_note=True) if row else None


async def _require_pending_manual(order_id: int) -> dict[str, Any]:
    order = await get_manual_order(order_id)
    if order is None:
        raise ManualOrderNotFoundError(manual_order_not_found(order_id))
    if not _is_pending_manual(order):
        raise ManualOrderNotPendingError(
            manual_order_not_pending(order_id, order["status"]),
        )
    return order


async def _save_status_note(order_id: int, note: str) -> None:
    text = note.strip()
    if not text:
        return
    try:
        async with db_transaction() as db:
            await db.execute(
                "UPDATE orders SET status_note = ? WHERE id = ?",
                (text, order_id),
            )
    except (DatabaseLockedError, sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.warning("save status_note order_id=%s failed: %s", order_id, exc)


async def complete_manual_order(order_id: int) -> dict[str, Any]:
    await _require_pending_manual(order_id)
    try:
        result = await update_order_status(order_id, "completed")
    except OrderNotFoundError as exc:
        raise ManualOrderNotFoundError(str(exc)) from exc
    except OrderStatusError as exc:
        raise ManualOrderAlreadyProcessedError(manual_order_already_processed(order_id)) from exc

    if not result.get("status_changed"):
        raise ManualOrderAlreadyProcessedError(manual_order_already_processed(order_id))

    updated = await get_manual_order(order_id)
    assert updated is not None
    logger.info(
        "COMPLETE_MANUAL_ORDER order_id=%s user_id=%s amount=%s",
        order_id,
        updated["user_id"],
        updated["amount"],
    )
    return {
        "ok": True,
        "order": updated,
        "refunded_dh": 0.0,
    }


async def reject_manual_order(
    order_id: int,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    pending = await _require_pending_manual(order_id)
    try:
        result = await update_order_status(order_id, "canceled")
    except OrderNotFoundError as exc:
        raise ManualOrderNotFoundError(str(exc)) from exc
    except OrderStatusError as exc:
        raise ManualOrderAlreadyProcessedError(manual_order_already_processed(order_id)) from exc

    if not result.get("status_changed"):
        raise ManualOrderAlreadyProcessedError(manual_order_already_processed(order_id))

    note = (reason or "").strip()
    if note:
        await _save_status_note(order_id, note)

    updated = await get_manual_order(order_id)
    assert updated is not None
    refunded = float(result.get("refunded_dh") or pending["amount"])
    logger.info(
        "REJECT_MANUAL_ORDER order_id=%s user_id=%s amount=%s refunded_dh=%s reason=%s",
        order_id,
        updated["user_id"],
        updated["amount"],
        refunded,
        note or "(none)",
    )
    return {
        "ok": True,
        "order": updated,
        "refunded_dh": refunded,
        "reason_logged": note or None,
    }


def _history_filter_clause(
    *,
    status: str | None,
    search: str | None,
    from_date: str | None,
    to_date: str | None,
) -> tuple[str, list[Any]]:
    clauses = [
        "COALESCE(o.fulfillment_mode, 'auto') = 'admin'",
        "LOWER(REPLACE(o.status, '_', ' ')) IN ('completed', 'canceled', 'failed')",
    ]
    params: list[Any] = []

    if status and status.strip():
        key = normalize_order_status_key(status.strip())
        if key in _HISTORY_STATUSES:
            clauses.append("LOWER(REPLACE(o.status, '_', ' ')) = ?")
            params.append(key)

    if search and search.strip():
        term = search.strip()
        if term.isdigit():
            uid = int(term)
            clauses.append("(o.user_id = ? OR o.provider_order_id LIKE ?)")
            params.extend([uid, f"%{term}%"])
        else:
            like = f"%{term}%"
            clauses.append(
                "(o.service_name LIKE ? OR o.provider_order_id LIKE ? OR CAST(o.user_id AS TEXT) LIKE ?)"
            )
            params.extend([like, like, like])

    if from_date and from_date.strip():
        clauses.append("date(o.created_at) >= date(?)")
        params.append(from_date.strip())

    if to_date and to_date.strip():
        clauses.append("date(o.created_at) <= date(?)")
        params.append(to_date.strip())

    return "WHERE " + " AND ".join(clauses), params


async def list_manual_order_history(
    *,
    page: int = 1,
    limit: int = DEFAULT_PAGE_SIZE,
    status: str | None = None,
    search: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    page = max(1, page)
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    offset = (page - 1) * limit
    where_sql, params = _history_filter_clause(
        status=status,
        search=search,
        from_date=from_date,
        to_date=to_date,
    )

    async with get_db() as db:
        count_sql = f"SELECT COUNT(*) FROM orders o {where_sql}"
        async with db.execute(count_sql, params) as cursor:
            total_row = await cursor.fetchone()
        total = int(total_row[0]) if total_row else 0

        list_sql = f"""
            SELECT
                o.id,
                o.user_id,
                u.telegram_name,
                u.balance,
                u.total_spent,
                o.service_name,
                o.service_id,
                o.link,
                o.quantity,
                o.amount,
                o.status,
                COALESCE(o.fulfillment_mode, 'auto') AS fulfillment_mode,
                o.api_account,
                o.provider_order_id,
                COALESCE(o.refunded_amount, 0) AS refunded_amount,
                o.status_note,
                o.created_at,
                (
                    SELECT COUNT(*)
                    FROM orders o2
                    WHERE o2.user_id = o.user_id
                ) AS user_orders_count
            FROM orders o
            INNER JOIN users u ON u.user_id = o.user_id
            {where_sql}
            ORDER BY o.id DESC
            LIMIT ? OFFSET ?
        """
        async with db.execute(list_sql, [*params, limit, offset]) as cursor:
            rows = await cursor.fetchall()

    orders = [_row_to_manual_order(row, include_note=True) for row in rows]
    total_pages = max(1, (total + limit - 1) // limit) if total else 1
    return {
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": total_pages,
        "orders": orders,
    }
