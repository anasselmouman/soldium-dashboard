"""Order listing and admin status updates for the dashboard."""
from __future__ import annotations

import sqlite3
from typing import Any

import aiosqlite

from admin_log import logger
from database_connector import (
    DatabaseLockedError,
    DatabaseWriteError,
    db_transaction,
    get_db,
)
from utils.messages_ar import order_not_found, order_status_unchanged, order_status_update_failed
from utils.money import to_float
from utils.order_status import normalize_order_status_key

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

_ORDER_STATUSES_NO_ADMIN_OVERRIDE = frozenset({"failed", "canceled", "refunded"})

_REFERRAL_RATES = {1: 0.10, 2: 0.15, 3: 0.20, 4: 0.25}


class OrderNotFoundError(Exception):
    pass


class OrderStatusError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _net_spent_amount(order_amount: float, refunded_amount: float | None) -> float:
    gross = to_float(order_amount)
    ref = to_float(refunded_amount)
    if ref < 0:
        ref = 0.0
    if ref > gross:
        ref = gross
    return max(0.0, gross - ref)


def _commission_rate(level: int) -> float:
    clamped = max(1, min(4, int(level)))
    return _REFERRAL_RATES.get(clamped, 0.10)


def _compute_commission(net_spent: float, level: int) -> float:
    return round(to_float(net_spent) * _commission_rate(level), 6)


def _row_to_order(row: aiosqlite.Row) -> dict[str, Any]:
    telegram_name = row["telegram_name"]
    name = str(telegram_name).strip() if telegram_name else ""
    provider_id = row["provider_order_id"]
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "telegram_name": name or None,
        "service_name": str(row["service_name"] or ""),
        "link": str(row["link"] or ""),
        "quantity": int(row["quantity"]),
        "amount": float(row["amount"] or 0.0),
        "status": str(row["status"]),
        "status_key": normalize_order_status_key(row["status"]),
        "provider_order_id": str(provider_id) if provider_id else None,
        "refunded_amount": float(row["refunded_amount"] or 0.0),
        "created_at": str(row["created_at"]),
    }


def _filter_clause(
    status: str | None,
    search: str | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if status and status.strip():
        key = normalize_order_status_key(status.strip())
        clauses.append("LOWER(REPLACE(o.status, '_', ' ')) = ?")
        params.append(key)

    if search and search.strip():
        term = search.strip()
        if term.isdigit():
            uid = int(term)
            clauses.append(
                "(o.user_id = ? OR o.provider_order_id LIKE ? OR CAST(o.id AS TEXT) = ?)"
            )
            params.extend([uid, f"%{term}%", term])
        else:
            like = f"%{term}%"
            clauses.append(
                "(o.provider_order_id LIKE ? OR CAST(o.user_id AS TEXT) LIKE ?)"
            )
            params.extend([like, like])

    if not clauses:
        return "", []
    return "WHERE " + " AND ".join(clauses), params


async def list_orders(
    *,
    page: int = 1,
    limit: int = DEFAULT_PAGE_SIZE,
    status: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    page = max(1, page)
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    offset = (page - 1) * limit
    where_sql, params = _filter_clause(status, search)

    async with get_db() as db:
        count_sql = f"""
            SELECT COUNT(*)
            FROM orders o
            {where_sql}
        """
        async with db.execute(count_sql, params) as cursor:
            total_row = await cursor.fetchone()
        total = int(total_row[0]) if total_row else 0

        list_sql = f"""
            SELECT
                o.id,
                o.user_id,
                u.telegram_name,
                o.service_name,
                o.link,
                o.quantity,
                o.amount,
                o.status,
                o.provider_order_id,
                COALESCE(o.refunded_amount, 0) AS refunded_amount,
                o.created_at
            FROM orders o
            INNER JOIN users u ON u.user_id = o.user_id
            {where_sql}
            ORDER BY o.id DESC
            LIMIT ? OFFSET ?
        """
        async with db.execute(list_sql, [*params, limit, offset]) as cursor:
            rows = await cursor.fetchall()

    orders = [_row_to_order(row) for row in rows]
    total_pages = max(1, (total + limit - 1) // limit) if total else 1
    return {
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": total_pages,
        "orders": orders,
    }


async def get_order(order_id: int) -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(
            """
            SELECT
                o.id,
                o.user_id,
                u.telegram_name,
                o.service_name,
                o.link,
                o.quantity,
                o.amount,
                o.status,
                o.provider_order_id,
                COALESCE(o.refunded_amount, 0) AS refunded_amount,
                o.created_at
            FROM orders o
            INNER JOIN users u ON u.user_id = o.user_id
            WHERE o.id = ?
            """,
            (order_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_order(row) if row else None


async def _credit_user_order_refund(
    db: aiosqlite.Connection,
    user_id: int,
    refund_amount: float,
) -> None:
    amount_money = to_float(refund_amount)
    if amount_money <= 0:
        return
    await db.execute(
        """
        UPDATE users
        SET
            balance = ROUND(balance + ?, 6),
            total_spent = CASE
                WHEN total_spent >= ? THEN ROUND(total_spent - ?, 6)
                ELSE 0
            END
        WHERE user_id = ?
        """,
        (amount_money, amount_money, amount_money, user_id),
    )


async def _reverse_referral_payout_in_tx(
    db: aiosqlite.Connection,
    order_id: int,
) -> None:
    async with db.execute(
        """
        SELECT COALESCE(o.referral_payout_done, 0) AS referral_payout_done,
               COALESCE(o.referral_commission_amount, 0) AS referral_commission_amount,
               u.referred_by AS referred_by
        FROM orders o
        JOIN users u ON u.user_id = o.user_id
        WHERE o.id = ?
        """,
        (order_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or not int(row["referral_payout_done"] or 0):
        return
    commission = to_float(row["referral_commission_amount"])
    referred_by = row["referred_by"]
    await db.execute(
        """
        UPDATE orders
        SET referral_payout_done = 0,
            referral_commission_amount = 0
        WHERE id = ?
        """,
        (order_id,),
    )
    if referred_by is not None and commission > 0:
        await db.execute(
            """
            UPDATE users
            SET referral_balance = ROUND(
                    CASE
                        WHEN COALESCE(referral_balance, 0) >= ? THEN COALESCE(referral_balance, 0) - ?
                        ELSE 0
                    END,
                    6
                ),
                referral_earned_total = CASE
                    WHEN COALESCE(referral_earned_total, 0) >= ? THEN ROUND(referral_earned_total - ?, 6)
                    ELSE 0
                END
            WHERE user_id = ?
            """,
            (commission, commission, commission, commission, int(referred_by)),
        )


async def _try_apply_referral_payout(order_id: int) -> None:
    """Post-commit referral commission (mirrors soldium-bot try_apply_referral_payout_for_order)."""
    try:
        async with db_transaction() as db:
            async with db.execute(
                """
                SELECT o.id, o.amount, o.status,
                       COALESCE(o.refunded_amount, 0) AS refunded_amount,
                       COALESCE(o.referral_payout_done, 0) AS referral_payout_done,
                       u.referred_by AS referred_by
                FROM orders o
                JOIN users u ON u.user_id = o.user_id
                WHERE o.id = ?
                """,
                (order_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return
            if int(row["referral_payout_done"] or 0):
                return

            status_key = normalize_order_status_key(row["status"])
            if status_key not in {"completed", "partial"}:
                return

            net = _net_spent_amount(
                float(row["amount"]),
                float(row["refunded_amount"] or 0),
            )
            referred_by = row["referred_by"]

            if net <= 0 or referred_by is None:
                await db.execute(
                    """
                    UPDATE orders
                    SET referral_payout_done = 1,
                        referral_commission_amount = 0
                    WHERE id = ?
                    """,
                    (order_id,),
                )
                return

            async with db.execute(
                "SELECT referral_level FROM users WHERE user_id = ?",
                (int(referred_by),),
            ) as cursor:
                ref = await cursor.fetchone()
            if ref is None:
                await db.execute(
                    """
                    UPDATE orders
                    SET referral_payout_done = 1,
                        referral_commission_amount = 0
                    WHERE id = ?
                    """,
                    (order_id,),
                )
                return

            referrer_level = max(1, int(ref["referral_level"] or 1))
            commission = _compute_commission(net, referrer_level)
            if commission <= 0:
                await db.execute(
                    """
                    UPDATE orders
                    SET referral_payout_done = 1,
                        referral_commission_amount = 0
                    WHERE id = ?
                    """,
                    (order_id,),
                )
                return

            referrer_id = int(referred_by)
            await db.execute(
                """
                UPDATE users
                SET referral_balance = ROUND(COALESCE(referral_balance, 0) + ?, 6),
                    referral_earned_total = ROUND(COALESCE(referral_earned_total, 0) + ?, 6)
                WHERE user_id = ?
                """,
                (commission, commission, referrer_id),
            )
            await db.execute(
                """
                UPDATE orders
                SET referral_payout_done = 1,
                    referral_commission_amount = ?
                WHERE id = ?
                """,
                (commission, order_id),
            )
    except (DatabaseLockedError, sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.warning(
            "referral payout for order_id=%s skipped: %s",
            order_id,
            exc,
        )


async def update_order_status(order_id: int, new_status: str) -> dict[str, Any]:
    """
    Admin status change with atomic refund on canceled/refunded/failed.
    Mirrors soldium-bot/database.set_order_status_by_admin.
    """
    status_text = str(new_status or "").strip()
    if not status_text:
        raise OrderStatusError(order_status_update_failed())

    new_key = normalize_order_status_key(status_text)
    stored_status = status_text.lower().replace("_", " ")

    existing = await get_order(order_id)
    if existing is None:
        raise OrderNotFoundError(order_not_found(order_id))

    current_key = normalize_order_status_key(existing["status"])
    if new_key == current_key:
        return {
            "ok": True,
            "order": existing,
            "refunded_dh": 0.0,
            "status_changed": False,
        }

    if current_key in _ORDER_STATUSES_NO_ADMIN_OVERRIDE:
        raise OrderStatusError(order_status_unchanged(order_id))

    refunded_dh = 0.0

    try:
        async with db_transaction() as db:
            async with db.execute(
                """
                SELECT user_id, amount, status,
                       COALESCE(refunded_amount, 0) AS refunded_amount,
                       COALESCE(referral_payout_done, 0) AS referral_payout_done
                FROM orders
                WHERE id = ?
                """,
                (order_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise OrderNotFoundError(order_not_found(order_id))

            order_amount = float(row["amount"])
            refunded_already = float(row["refunded_amount"])
            user_id = int(row["user_id"])
            had_referral_payout = bool(int(row["referral_payout_done"] or 0))

            if new_key in {"failed", "canceled", "refunded"}:
                refund_due = max(0.0, order_amount - refunded_already)
                order_cursor = await db.execute(
                    """
                    UPDATE orders
                    SET status = ?,
                        refunded_amount = ROUND(COALESCE(amount, 0), 6)
                    WHERE id = ?
                      AND LOWER(REPLACE(status, '_', ' ')) NOT IN ('failed', 'canceled', 'refunded')
                    """,
                    (stored_status, order_id),
                )
                if order_cursor.rowcount == 0:
                    raise OrderStatusError(order_status_update_failed())
                await _credit_user_order_refund(db, user_id, refund_due)
                refunded_dh = refund_due
                if had_referral_payout:
                    await _reverse_referral_payout_in_tx(db, order_id)

            elif new_key == "partial":
                if current_key in {"failed", "canceled", "refunded", "completed"}:
                    raise OrderStatusError(order_status_update_failed())
                if refunded_already <= 0 and current_key != "partial":
                    raise OrderStatusError(order_status_update_failed())
                order_cursor = await db.execute(
                    """
                    UPDATE orders
                    SET status = ?
                    WHERE id = ?
                      AND LOWER(REPLACE(status, '_', ' ')) NOT IN ('failed', 'canceled', 'refunded')
                    """,
                    (stored_status, order_id),
                )
                if order_cursor.rowcount == 0:
                    raise OrderStatusError(order_status_update_failed())

            elif new_key == "completed":
                if current_key in {"failed", "canceled", "refunded", "completed"}:
                    raise OrderStatusError(order_status_update_failed())
                order_cursor = await db.execute(
                    """
                    UPDATE orders
                    SET status = ?
                    WHERE id = ?
                      AND LOWER(REPLACE(status, '_', ' ')) NOT IN (
                          'failed', 'canceled', 'refunded', 'completed'
                      )
                    """,
                    (stored_status, order_id),
                )
                if order_cursor.rowcount == 0:
                    raise OrderStatusError(order_status_update_failed())

            else:
                order_cursor = await db.execute(
                    """
                    UPDATE orders
                    SET status = ?
                    WHERE id = ?
                      AND LOWER(REPLACE(status, '_', ' ')) NOT IN ('failed', 'canceled', 'refunded')
                    """,
                    (stored_status, order_id),
                )
                if order_cursor.rowcount == 0:
                    raise OrderStatusError(order_status_update_failed())

    except DatabaseLockedError:
        raise
    except OrderNotFoundError:
        raise
    except OrderStatusError:
        raise
    except (sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.error(
            "update_order_status order_id=%s failed: %s",
            order_id,
            exc,
            exc_info=True,
        )
        raise

    if new_key == "completed":
        await _try_apply_referral_payout(order_id)
    elif new_key == "partial":
        await _try_apply_referral_payout(order_id)

    updated = await get_order(order_id)
    assert updated is not None

    logger.info(
        "ORDER_STATUS order_id=%s user_id=%s old=%s new=%s refunded_dh=%s",
        order_id,
        updated["user_id"],
        existing["status"],
        updated["status"],
        refunded_dh,
    )
    return {
        "ok": True,
        "order": updated,
        "refunded_dh": refunded_dh,
        "status_changed": True,
        "previous_status": existing["status"],
    }
