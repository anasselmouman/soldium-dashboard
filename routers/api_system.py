"""System maintenance endpoints (dangerous operations)."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Header, HTTPException, Query, status

from config import RESET_TEST_DATA_TOKEN
from database_connector import DatabaseLockedError, DatabaseWriteError, db_transaction

router = APIRouter(prefix="/api/system", tags=["system"])

_RESET_CONFIRM_PHRASE = "RESET_TEST_DATA"


def _reset_auth_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="غير مصرح بتنفيذ تصفير بيانات الاختبار.",
    )


@router.delete("/reset-test-data")
async def reset_test_data(
    confirm: str = Query(..., min_length=1, description="Must equal RESET_TEST_DATA"),
    x_reset_token: str = Header(default="", alias="X-Reset-Token"),
):
    """
    DANGEROUS: clear test financial data while keeping user identities and service catalog.
    """
    if confirm != _RESET_CONFIRM_PHRASE:
        raise HTTPException(status_code=400, detail="عبارة التأكيد غير صحيحة.")

    if not RESET_TEST_DATA_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="RESET_TEST_DATA_TOKEN غير مضبوط في البيئة.",
        )

    if not secrets.compare_digest((x_reset_token or "").strip(), RESET_TEST_DATA_TOKEN):
        raise _reset_auth_error()

    try:
        async with db_transaction() as db:
            await db.execute("DELETE FROM deposit_transactions")
            await db.execute("DELETE FROM withdrawals")
            await db.execute("DELETE FROM orders")
            await db.execute(
                """
                UPDATE users
                SET
                    balance = 0,
                    total_spent = 0,
                    referral_balance = 0,
                    referral_earned_total = 0
                """
            )
    except DatabaseLockedError as exc:
        raise HTTPException(status_code=423, detail=f"قاعدة البيانات مشغولة حالياً: {exc}") from exc
    except DatabaseWriteError as exc:
        raise HTTPException(status_code=500, detail=f"فشل تصفير البيانات: {exc}") from exc

    return {
        "ok": True,
        "message": "تم تصفير بيانات الاختبار بنجاح.",
        "reset": {
            "cleared_tables": ["deposit_transactions", "withdrawals", "orders"],
            "reset_users_fields": [
                "balance",
                "total_spent",
                "referral_balance",
                "referral_earned_total",
            ],
        },
    }
