"""Deposit queue queries and admin actions for the dashboard."""
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
from utils.deposit_ledger import ledger_method_for_approved_deposit
from utils.deposit_validation import validate_admin_deposit_amount
from utils.messages_ar import (
    amount_must_be_positive,
    deposit_already_processed,
    deposit_not_found,
    deposit_not_pending,
)
from utils.money import to_float

_PENDING_DEPOSITS_SQL = """
    SELECT
        d.id,
        d.user_id,
        d.amount,
        d.method,
        d.proof_file_id,
        d.status,
        u.telegram_name
    FROM deposits AS d
    INNER JOIN users AS u ON u.user_id = d.user_id
    WHERE d.status = 'pending'
    ORDER BY d.id ASC
"""

_DEPOSIT_BY_ID_SQL = """
    SELECT id, user_id, amount, method, proof_file_id, status
    FROM deposits
    WHERE id = ?
"""


class DepositNotFoundError(Exception):
    pass


class DepositNotPendingError(Exception):
    pass


class DepositValidationError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class DepositAlreadyProcessedError(Exception):
    pass


def _row_to_deposit(row: Any) -> dict[str, Any]:
    telegram_name = row["telegram_name"]
    user_id = int(row["user_id"])
    name = str(telegram_name).strip() if telegram_name else ""
    return {
        "id": int(row["id"]),
        "user_id": user_id,
        "telegram_name": name or None,
        "display_name": name if name else str(user_id),
        "amount": float(row["amount"]),
        "method": str(row["method"]),
        "proof_file_id": str(row["proof_file_id"]),
        "status": str(row["status"]),
    }


def _row_to_deposit_record(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "amount": float(row["amount"]),
        "method": str(row["method"]),
        "proof_file_id": str(row["proof_file_id"]),
        "status": str(row["status"]),
    }


async def get_pending_deposits() -> list[dict[str, Any]]:
    """All pending deposits with user display info."""
    async with get_db() as db:
        async with db.execute(_PENDING_DEPOSITS_SQL) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_deposit(row) for row in rows]


async def get_deposit(deposit_id: int) -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(_DEPOSIT_BY_ID_SQL, (deposit_id,)) as cursor:
            row = await cursor.fetchone()
    return _row_to_deposit_record(row) if row else None


async def _get_user_balance(db: aiosqlite.Connection, user_id: int) -> float:
    async with db.execute(
        "SELECT balance FROM users WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return float(row[0]) if row else 0.0


async def finalize_approved_deposit(
    deposit_id: int,
    user_id: int,
    amount: float,
    deposit_method: str,
) -> bool:
    """
    Atomic approval — mirrors soldium-bot/database.finalize_approved_deposit.
    Updates deposit, credits balance, inserts deposit_transactions row.
    """
    amount_money = to_float(amount)
    approved_status = f"approved:{amount_money}"
    ledger_method = ledger_method_for_approved_deposit(deposit_method)

    try:
        async with db_transaction() as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                (user_id,),
            )
            cursor = await db.execute(
                """
                UPDATE deposits
                SET amount = ?, status = ?
                WHERE id = ? AND user_id = ? AND status = 'pending'
                """,
                (amount_money, approved_status, deposit_id, user_id),
            )
            if cursor.rowcount == 0:
                return False

            user_cursor = await db.execute(
                "UPDATE users SET balance = ROUND(balance + ?, 6) WHERE user_id = ?",
                (amount_money, user_id),
            )
            if user_cursor.rowcount == 0:
                return False

            await db.execute(
                """
                INSERT INTO deposit_transactions (user_id, deposit_method, amount, status, deposit_id)
                VALUES (?, ?, ?, 'completed', ?)
                """,
                (user_id, ledger_method, amount_money, deposit_id),
            )
            await db.commit()
    except DatabaseLockedError:
        raise
    except (sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.error(
            "finalize_approved_deposit deposit_id=%s failed: %s",
            deposit_id,
            exc,
            exc_info=True,
        )
        raise

    return True


async def reject_pending_deposit(deposit_id: int) -> bool:
    """Reject only if still pending — mirrors update_pending_deposit_status."""
    try:
        async with db_transaction() as db:
            cursor = await db.execute(
                """
                UPDATE deposits
                SET status = 'rejected'
                WHERE id = ? AND status = 'pending'
                """,
                (deposit_id,),
            )
            updated = cursor.rowcount > 0
            await db.commit()
    except DatabaseLockedError:
        raise
    except (sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.error(
            "reject_pending_deposit deposit_id=%s failed: %s",
            deposit_id,
            exc,
            exc_info=True,
        )
        raise
    return updated


async def approve_deposit(deposit_id: int, amount_dh: float) -> dict[str, Any]:
    deposit = await get_deposit(deposit_id)
    if deposit is None:
        raise DepositNotFoundError(deposit_not_found(deposit_id))
    if deposit["status"] != "pending":
        raise DepositNotPendingError(
            deposit_not_pending(deposit_id, deposit["status"])
        )

    amount_money = to_float(amount_dh)
    if amount_money <= 0:
        raise DepositValidationError(amount_must_be_positive())

    validation_error = validate_admin_deposit_amount(deposit["method"], amount_money)
    if validation_error:
        raise DepositValidationError(validation_error)

    try:
        ok = await finalize_approved_deposit(
            deposit_id,
            deposit["user_id"],
            amount_money,
            deposit["method"],
        )
    except DatabaseLockedError as exc:
        logger.warning(
            "approve deposit_id=%s locked: %s", deposit_id, exc,
        )
        raise

    if not ok:
        raise DepositAlreadyProcessedError(deposit_already_processed(deposit_id))

    approved_status = f"approved:{amount_money}"
    async with get_db() as db:
        new_balance = await _get_user_balance(db, deposit["user_id"])

    logger.info(
        "APPROVE deposit_id=%s user_id=%s method=%s amount_dh=%s status=%s balance=%s",
        deposit_id,
        deposit["user_id"],
        deposit["method"],
        amount_money,
        approved_status,
        new_balance,
    )

    return {
        "ok": True,
        "deposit_id": deposit_id,
        "user_id": deposit["user_id"],
        "amount_dh": amount_money,
        "status": approved_status,
        "new_balance": new_balance,
    }


async def reject_deposit(deposit_id: int, *, reason: str | None = None) -> dict[str, Any]:
    deposit = await get_deposit(deposit_id)
    if deposit is None:
        raise DepositNotFoundError(deposit_not_found(deposit_id))
    if deposit["status"] != "pending":
        raise DepositNotPendingError(
            deposit_not_pending(deposit_id, deposit["status"])
        )

    try:
        ok = await reject_pending_deposit(deposit_id)
    except DatabaseLockedError as exc:
        logger.warning(
            "reject deposit_id=%s locked: %s", deposit_id, exc,
        )
        raise

    if not ok:
        raise DepositAlreadyProcessedError(deposit_already_processed(deposit_id))

    note = (reason or "").strip()
    logger.info(
        "REJECT deposit_id=%s user_id=%s method=%s reason=%s",
        deposit_id,
        deposit["user_id"],
        deposit["method"],
        note or "(none)",
    )

    return {
        "ok": True,
        "deposit_id": deposit_id,
        "user_id": deposit["user_id"],
        "status": "rejected",
        "reason_logged": note or None,
    }
