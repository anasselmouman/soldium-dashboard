"""JSON API for deposit management."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException

from admin_log import logger
from database_connector import DatabaseLockedError, DatabaseWriteError
from deposits import (
    DepositAlreadyProcessedError,
    DepositNotFoundError,
    DepositNotPendingError,
    DepositValidationError,
    approve_deposit,
    get_pending_deposits,
    reject_deposit,
)
from notifier import notify_deposit_approved, notify_deposit_rejected
from schemas import ApproveDepositRequest, RejectDepositRequest
from utils.messages_ar import (
    APPROVE_FAILED,
    DB_BUSY,
    LOAD_DEPOSITS_FAILED,
    REJECT_FAILED,
)

router = APIRouter(prefix="/api/deposits", tags=["deposits"])


def _locked_response(exc: DatabaseLockedError) -> HTTPException:
    logger.warning("database locked during deposit action: %s", exc)
    return HTTPException(
        status_code=503,
        detail={
            "error": "database_locked",
            "message": DB_BUSY,
        },
    )


@router.get("/pending")
async def list_pending_deposits():
    """Pending deposit queue joined with user names."""
    try:
        items = await get_pending_deposits()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{LOAD_DEPOSITS_FAILED} ({exc})",
        ) from exc

    return {"count": len(items), "deposits": items}


@router.post("/{deposit_id}/approve")
async def approve_deposit_endpoint(deposit_id: int, body: ApproveDepositRequest):
    try:
        result = await approve_deposit(deposit_id, body.amount_dh)
    except DepositNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DepositNotPendingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DepositValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except DepositAlreadyProcessedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DatabaseLockedError as exc:
        raise _locked_response(exc) from exc
    except (sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.exception("approve deposit_id=%s database error", deposit_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("approve deposit_id=%s failed", deposit_id)
        raise HTTPException(
            status_code=500,
            detail=f"{APPROVE_FAILED} ({exc})",
        ) from exc

    await notify_deposit_approved(
        result["user_id"],
        result["amount_dh"],
    )
    return result


@router.post("/{deposit_id}/reject")
async def reject_deposit_endpoint(deposit_id: int, body: RejectDepositRequest):
    try:
        result = await reject_deposit(deposit_id, reason=body.reason)
    except DepositNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DepositNotPendingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DepositAlreadyProcessedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DatabaseLockedError as exc:
        raise _locked_response(exc) from exc
    except (sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.exception("reject deposit_id=%s database error", deposit_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("reject deposit_id=%s failed", deposit_id)
        raise HTTPException(
            status_code=500,
            detail=f"{REJECT_FAILED} ({exc})",
        ) from exc

    await notify_deposit_rejected(
        result["user_id"],
        reason=body.reason,
    )
    return result
