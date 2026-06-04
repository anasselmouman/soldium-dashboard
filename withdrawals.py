"""Pending withdrawal queue and admin actions for the dashboard."""
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
    withdrawal_already_processed,
    withdrawal_not_found,
    withdrawal_not_pending,
)
from utils.money import to_float
from utils.withdraw_details import (
    format_details_lines,
    safe_withdraw_details,
    withdrawal_type_label,
)

_PENDING_WITHDRAWALS_SQL = """
    SELECT
        w.id,
        w.user_id,
        u.telegram_name,
        w.amount,
        w.method,
        w.details_json,
        w.status,
        w.withdrawal_type,
        w.created_at
    FROM withdrawals AS w
    INNER JOIN users AS u ON u.user_id = w.user_id
    WHERE w.status = 'pending'
    ORDER BY w.id ASC
"""

_WITHDRAWAL_BY_ID_SQL = """
    SELECT
        w.id,
        w.user_id,
        u.telegram_name,
        w.amount,
        w.method,
        w.details_json,
        w.status,
        w.withdrawal_type,
        w.created_at
    FROM withdrawals AS w
    INNER JOIN users AS u ON u.user_id = w.user_id
    WHERE w.id = ?
"""


class WithdrawalNotFoundError(Exception):
    pass


class WithdrawalNotPendingError(Exception):
    pass


class WithdrawalAlreadyProcessedError(Exception):
    pass


def _normalize_withdrawal_type(raw: str | None) -> str:
    value = (raw or "normal").strip().lower()
    return "referral" if value == "referral" else "normal"


def _row_to_withdrawal(row: aiosqlite.Row) -> dict[str, Any]:
    telegram_name = row["telegram_name"]
    name = str(telegram_name).strip() if telegram_name else ""
    details_raw = str(row["details_json"] or "{}")
    details = safe_withdraw_details(details_raw)
    wtype = _normalize_withdrawal_type(str(row["withdrawal_type"]))
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "telegram_name": name or None,
        "display_name": name if name else str(row["user_id"]),
        "amount": float(row["amount"]),
        "method": str(row["method"]),
        "details_json": details_raw,
        "details": details,
        "details_lines": format_details_lines(details),
        "status": str(row["status"]),
        "withdrawal_type": wtype,
        "withdrawal_type_label": withdrawal_type_label(wtype),
        "created_at": str(row["created_at"]),
    }


async def get_pending_withdrawals() -> list[dict[str, Any]]:
    async with get_db() as db:
        async with db.execute(_PENDING_WITHDRAWALS_SQL) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_withdrawal(row) for row in rows]


async def get_withdrawal(withdrawal_id: int) -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(_WITHDRAWAL_BY_ID_SQL, (withdrawal_id,)) as cursor:
            row = await cursor.fetchone()
    return _row_to_withdrawal(row) if row else None


async def approve_withdrawal(withdrawal_id: int) -> dict[str, Any]:
    """Mark pending withdrawal completed — balance already held at creation."""
    withdrawal = await get_withdrawal(withdrawal_id)
    if withdrawal is None:
        raise WithdrawalNotFoundError(withdrawal_not_found(withdrawal_id))
    if withdrawal["status"] != "pending":
        raise WithdrawalNotPendingError(
            withdrawal_not_pending(withdrawal_id, withdrawal["status"]),
        )

    try:
        async with db_transaction() as db:
            cursor = await db.execute(
                """
                UPDATE withdrawals
                SET status = 'completed',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND status = 'pending'
                """,
                (withdrawal_id,),
            )
            if cursor.rowcount == 0:
                raise WithdrawalAlreadyProcessedError(
                    withdrawal_already_processed(withdrawal_id),
                )
    except DatabaseLockedError:
        raise
    except WithdrawalAlreadyProcessedError:
        raise
    except (sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.error(
            "approve_withdrawal id=%s failed: %s",
            withdrawal_id,
            exc,
            exc_info=True,
        )
        raise

    updated = await get_withdrawal(withdrawal_id)
    assert updated is not None

    logger.info(
        "APPROVE_WITHDRAWAL id=%s user_id=%s amount=%s method=%s type=%s",
        withdrawal_id,
        updated["user_id"],
        updated["amount"],
        updated["method"],
        updated["withdrawal_type"],
    )
    return {
        "ok": True,
        "withdrawal": updated,
    }


async def reject_withdrawal(
    withdrawal_id: int,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """
    Reject pending withdrawal and refund held amount atomically.
    Mirrors SOLDUIM/database.reject_withdrawal_by_admin.
    """
    withdrawal = await get_withdrawal(withdrawal_id)
    if withdrawal is None:
        raise WithdrawalNotFoundError(withdrawal_not_found(withdrawal_id))
    if withdrawal["status"] != "pending":
        raise WithdrawalNotPendingError(
            withdrawal_not_pending(withdrawal_id, withdrawal["status"]),
        )

    user_id = int(withdrawal["user_id"])
    amount_money = to_float(withdrawal["amount"])
    wtype = withdrawal["withdrawal_type"]
    refund_column = "referral_balance" if wtype == "referral" else "balance"

    try:
        async with db_transaction() as db:
            reject_cursor = await db.execute(
                """
                UPDATE withdrawals
                SET status = 'rejected',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND status = 'pending'
                """,
                (withdrawal_id,),
            )
            if reject_cursor.rowcount == 0:
                raise WithdrawalAlreadyProcessedError(
                    withdrawal_already_processed(withdrawal_id),
                )

            await db.execute(
                f"""
                UPDATE users
                SET {refund_column} = ROUND(COALESCE({refund_column}, 0) + ?, 6)
                WHERE user_id = ?
                """,
                (amount_money, user_id),
            )
    except DatabaseLockedError:
        raise
    except WithdrawalAlreadyProcessedError:
        raise
    except (sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.error(
            "reject_withdrawal id=%s failed: %s",
            withdrawal_id,
            exc,
            exc_info=True,
        )
        raise

    updated = await get_withdrawal(withdrawal_id)
    assert updated is not None

    note = (reason or "").strip()
    logger.info(
        "REJECT_WITHDRAWAL id=%s user_id=%s amount=%s refund_to=%s reason=%s",
        withdrawal_id,
        user_id,
        amount_money,
        refund_column,
        note or "(none)",
    )
    return {
        "ok": True,
        "withdrawal": updated,
        "refunded_dh": amount_money,
        "refund_target": refund_column,
        "reason_logged": note or None,
    }
