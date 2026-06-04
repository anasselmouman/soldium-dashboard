"""User listing and admin actions for the dashboard."""
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
from utils.messages_ar import (
    balance_adjust_failed,
    balance_adjust_zero,
    user_not_found,
)
from utils.money import to_float

MIN_REFERRAL_LEVEL = 1
MAX_REFERRAL_LEVEL = 4
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


class UserNotFoundError(Exception):
    pass


class UserValidationError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _row_to_user(row: aiosqlite.Row) -> dict[str, Any]:
    telegram_name = row["telegram_name"]
    name = str(telegram_name).strip() if telegram_name else ""
    return {
        "user_id": int(row["user_id"]),
        "telegram_name": name or None,
        "balance": float(row["balance"] or 0.0),
        "total_spent": float(row["total_spent"] or 0.0),
        "referral_level": int(row["referral_level"] or 1),
        "referral_balance": float(row["referral_balance"] or 0.0),
    }


def _search_clause(search: str | None) -> tuple[str, list[Any]]:
    if not search or not search.strip():
        return "", []
    term = search.strip()
    if term.isdigit():
        uid = int(term)
        return (
            "WHERE user_id = ? OR telegram_name LIKE ?",
            [uid, f"%{term}%"],
        )
    like = f"%{term}%"
    return (
        "WHERE telegram_name LIKE ? OR CAST(user_id AS TEXT) LIKE ?",
        [like, like],
    )


async def list_users(
    *,
    page: int = 1,
    limit: int = DEFAULT_PAGE_SIZE,
    search: str | None = None,
) -> dict[str, Any]:
    page = max(1, page)
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    offset = (page - 1) * limit
    where_sql, params = _search_clause(search)

    async with get_db() as db:
        count_sql = f"SELECT COUNT(*) FROM users {where_sql}"
        async with db.execute(count_sql, params) as cursor:
            total_row = await cursor.fetchone()
        total = int(total_row[0]) if total_row else 0

        list_sql = f"""
            SELECT user_id, telegram_name, balance, total_spent,
                   referral_level, referral_balance
            FROM users
            {where_sql}
            ORDER BY user_id DESC
            LIMIT ? OFFSET ?
        """
        async with db.execute(list_sql, [*params, limit, offset]) as cursor:
            rows = await cursor.fetchall()

    users = [_row_to_user(row) for row in rows]
    total_pages = max(1, (total + limit - 1) // limit) if total else 1
    return {
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": total_pages,
        "users": users,
    }


def _compute_trust_status(
    *,
    pending_withdrawals: int,
    rejected_withdrawals: int,
    failed_or_canceled_orders: int,
    total_orders: int,
) -> dict[str, str]:
    """Heuristic trust label for admin audit (not a legal determination)."""
    if rejected_withdrawals >= 3 or (
        total_orders >= 5
        and failed_or_canceled_orders / total_orders >= 0.5
    ):
        return {
            "key": "suspicious",
            "label": "مشبوه — مراجعة عاجلة",
            "color": "red",
        }
    if pending_withdrawals >= 2 or rejected_withdrawals >= 1:
        return {
            "key": "review",
            "label": "يحتاج مراجعة مالية",
            "color": "amber",
        }
    return {
        "key": "trusted",
        "label": "موثوق — نشط",
        "color": "green",
    }


async def get_user(user_id: int) -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(
            """
            SELECT user_id, telegram_name, balance, total_spent,
                   referral_level, referral_balance
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_user(row) if row else None


async def adjust_user_balance(
    user_id: int,
    amount_dh: float,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """
    Atomic balance adjustment — mirrors SOLDUIM/database.update_balance.
  Positive amount credits; negative debits (and increases total_spent).
    """
    amount_money = to_float(amount_dh)
    if amount_money == 0:
        raise UserValidationError(balance_adjust_zero())

    user = await get_user(user_id)
    if user is None:
        raise UserNotFoundError(user_not_found(user_id))

    try:
        async with db_transaction() as db:
            cursor = await db.execute(
                """
                UPDATE users
                SET
                    balance = ROUND(balance + ?, 6),
                    total_spent = CASE
                        WHEN ? < 0 THEN ROUND(total_spent + ABS(?), 6)
                        ELSE total_spent
                    END
                WHERE user_id = ?
                  AND (balance + ?) >= 0
                """,
                (
                    amount_money,
                    amount_money,
                    amount_money,
                    user_id,
                    amount_money,
                ),
            )
            if cursor.rowcount == 0:
                raise UserValidationError(balance_adjust_failed())
            await db.commit()
    except DatabaseLockedError:
        raise
    except (sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.error(
            "adjust_user_balance user_id=%s failed: %s",
            user_id,
            exc,
            exc_info=True,
        )
        raise

    updated = await get_user(user_id)
    assert updated is not None
    note = (reason or "").strip()
    logger.info(
        "ADJUST_BALANCE user_id=%s amount_dh=%s new_balance=%s reason=%s",
        user_id,
        amount_money,
        updated["balance"],
        note or "(none)",
    )
    return {
        "ok": True,
        "user_id": user_id,
        "adjustment_dh": amount_money,
        "balance": updated["balance"],
        "total_spent": updated["total_spent"],
    }


async def change_user_referral_level(user_id: int, new_level: int) -> dict[str, Any]:
    level = max(MIN_REFERRAL_LEVEL, min(MAX_REFERRAL_LEVEL, int(new_level)))

    user = await get_user(user_id)
    if user is None:
        raise UserNotFoundError(user_not_found(user_id))

    try:
        async with db_transaction() as db:
            cursor = await db.execute(
                "UPDATE users SET referral_level = ? WHERE user_id = ?",
                (level, user_id),
            )
            if cursor.rowcount == 0:
                raise UserNotFoundError(user_not_found(user_id))
            await db.commit()
    except DatabaseLockedError:
        raise
    except (sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.error(
            "change_user_referral_level user_id=%s failed: %s",
            user_id,
            exc,
            exc_info=True,
        )
        raise

    logger.info(
        "CHANGE_REFERRAL_LEVEL user_id=%s old_level=%s new_level=%s",
        user_id,
        user["referral_level"],
        level,
    )
    return {
        "ok": True,
        "user_id": user_id,
        "referral_level": level,
    }


async def get_user_full_logs(user_id: int) -> dict[str, Any] | None:
    """
    Comprehensive audit bundle for the 360° user profile page.
    """
    user = await get_user(user_id)
    if user is None:
        return None

    async with get_db() as db:
        async with db.execute(
            """
            SELECT MIN(ts) AS joined_at
            FROM (
                SELECT MIN(created_at) AS ts
                FROM deposit_transactions
                WHERE user_id = ?
                UNION ALL
                SELECT MIN(created_at) AS ts
                FROM orders
                WHERE user_id = ?
                UNION ALL
                SELECT MIN(created_at) AS ts
                FROM withdrawals
                WHERE user_id = ?
            )
            WHERE ts IS NOT NULL
            """,
            (user_id, user_id, user_id),
        ) as cursor:
            joined_row = await cursor.fetchone()

        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE referred_by = ?",
            (user_id,),
        ) as cursor:
            referrals_row = await cursor.fetchone()

        async with db.execute(
            """
            SELECT id, deposit_method, amount, status, deposit_id, created_at
            FROM deposit_transactions
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (user_id,),
        ) as cursor:
            deposit_tx_rows = await cursor.fetchall()

        async with db.execute(
            """
            SELECT id, amount, method, proof_file_id, status
            FROM deposits
            WHERE user_id = ?
            ORDER BY id DESC
            """,
            (user_id,),
        ) as cursor:
            deposit_req_rows = await cursor.fetchall()

        async with db.execute(
            """
            SELECT
                w.id,
                w.amount,
                w.method,
                w.details_json,
                w.status,
                w.withdrawal_type,
                w.created_at,
                w.updated_at
            FROM withdrawals AS w
            WHERE w.user_id = ?
            ORDER BY w.id DESC
            """,
            (user_id,),
        ) as cursor:
            withdrawal_rows = await cursor.fetchall()

        async with db.execute(
            """
            SELECT
                o.id,
                o.service_name,
                o.link,
                o.quantity,
                o.amount,
                o.status,
                o.provider_order_id,
                COALESCE(o.refunded_amount, 0) AS refunded_amount,
                o.created_at
            FROM orders AS o
            WHERE o.user_id = ?
            ORDER BY o.id DESC
            """,
            (user_id,),
        ) as cursor:
            order_rows = await cursor.fetchall()

        async with db.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected_count
            FROM withdrawals
            WHERE user_id = ?
            """,
            (user_id,),
        ) as cursor:
            withdraw_stats_row = await cursor.fetchone()

        async with db.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(
                       CASE
                           WHEN LOWER(REPLACE(status, '_', ' ')) IN (
                               'failed', 'canceled', 'refunded'
                           ) THEN 1
                           ELSE 0
                       END
                   ) AS bad_count
            FROM orders
            WHERE user_id = ?
            """,
            (user_id,),
        ) as cursor:
            order_stats_row = await cursor.fetchone()

    joined_at = None
    if joined_row and joined_row[0]:
        joined_at = str(joined_row[0])

    referrals_count = int(referrals_row[0]) if referrals_row else 0

    deposits: list[dict[str, Any]] = []
    for row in deposit_tx_rows:
        deposits.append(
            {
                "id": int(row["id"]),
                "record_type": "ledger",
                "deposit_method": str(row["deposit_method"] or ""),
                "amount": float(row["amount"] or 0.0),
                "status": str(row["status"] or ""),
                "deposit_id": int(row["deposit_id"]) if row["deposit_id"] else None,
                "created_at": str(row["created_at"] or ""),
            }
        )
    for row in deposit_req_rows:
        deposits.append(
            {
                "id": int(row["id"]),
                "record_type": "request",
                "deposit_method": str(row["method"] or ""),
                "amount": float(row["amount"] or 0.0),
                "status": str(row["status"] or ""),
                "proof_file_id": str(row["proof_file_id"] or ""),
                "deposit_id": int(row["id"]),
                "created_at": None,
            }
        )
    deposits.sort(
        key=lambda item: (
            item.get("created_at") or "",
            item.get("id") or 0,
        ),
        reverse=True,
    )

    withdrawals: list[dict[str, Any]] = []
    for row in withdrawal_rows:
        wtype = str(row["withdrawal_type"] or "normal").strip().lower()
        withdrawals.append(
            {
                "id": int(row["id"]),
                "amount": float(row["amount"] or 0.0),
                "method": str(row["method"] or ""),
                "status": str(row["status"] or ""),
                "withdrawal_type": "referral" if wtype == "referral" else "normal",
                "created_at": str(row["created_at"] or ""),
                "updated_at": str(row["updated_at"]) if row["updated_at"] else None,
            }
        )

    orders: list[dict[str, Any]] = []
    for row in order_rows:
        orders.append(
            {
                "id": int(row["id"]),
                "service_name": str(row["service_name"] or ""),
                "link": str(row["link"] or ""),
                "quantity": int(row["quantity"] or 0),
                "amount": float(row["amount"] or 0.0),
                "status": str(row["status"] or ""),
                "provider_order_id": str(row["provider_order_id"])
                if row["provider_order_id"]
                else None,
                "refunded_amount": float(row["refunded_amount"] or 0.0),
                "created_at": str(row["created_at"] or ""),
            }
        )

    pending_withdrawals = int(withdraw_stats_row[0] or 0) if withdraw_stats_row else 0
    rejected_withdrawals = int(withdraw_stats_row[1] or 0) if withdraw_stats_row else 0
    total_orders = int(order_stats_row[0] or 0) if order_stats_row else 0
    bad_orders = int(order_stats_row[1] or 0) if order_stats_row else 0

    trust = _compute_trust_status(
        pending_withdrawals=pending_withdrawals,
        rejected_withdrawals=rejected_withdrawals,
        failed_or_canceled_orders=bad_orders,
        total_orders=total_orders,
    )

    return {
        "user_info": {
            **user,
            "joined_at": joined_at,
            "trust_status": trust,
            "referrals_count": referrals_count,
        },
        "deposits": deposits,
        "withdrawals": withdrawals,
        "orders": orders,
        "referrals": {"count": referrals_count},
    }
