"""JSON API for withdrawal management."""

from __future__ import annotations



import sqlite3



from fastapi import APIRouter, HTTPException



from admin_log import logger

from database_connector import DatabaseLockedError, DatabaseWriteError

from notifier import notify_withdrawal_approved, notify_withdrawal_rejected

from schemas import RejectWithdrawalRequest

from utils.messages_ar import (

    APPROVE_WITHDRAWAL_FAILED,

    DB_BUSY,

    LOAD_WITHDRAWALS_FAILED,

    REJECT_WITHDRAWAL_FAILED,

)

from withdrawals import (

    WithdrawalAlreadyProcessedError,

    WithdrawalNotFoundError,

    WithdrawalNotPendingError,

    approve_withdrawal,

    get_pending_withdrawals,

    reject_withdrawal,

)



router = APIRouter(prefix="/api/withdrawals", tags=["withdrawals"])





def _locked_response(exc: DatabaseLockedError) -> HTTPException:

    logger.warning("database locked during withdrawal action: %s", exc)

    return HTTPException(

        status_code=503,

        detail={"error": "database_locked", "message": DB_BUSY},

    )





@router.get("/pending")

async def list_pending_withdrawals():

    try:

        items = await get_pending_withdrawals()

    except FileNotFoundError as exc:

        raise HTTPException(status_code=503, detail=str(exc)) from exc

    except Exception as exc:

        raise HTTPException(

            status_code=503,

            detail=f"{LOAD_WITHDRAWALS_FAILED} ({exc})",

        ) from exc



    return {"count": len(items), "withdrawals": items}





@router.post("/{withdrawal_id}/approve")

async def approve_withdrawal_endpoint(withdrawal_id: int):

    try:

        result = await approve_withdrawal(withdrawal_id)

    except WithdrawalNotFoundError as exc:

        raise HTTPException(status_code=404, detail=str(exc)) from exc

    except WithdrawalNotPendingError as exc:

        raise HTTPException(status_code=409, detail=str(exc)) from exc

    except WithdrawalAlreadyProcessedError as exc:

        raise HTTPException(status_code=409, detail=str(exc)) from exc

    except DatabaseLockedError as exc:

        raise _locked_response(exc) from exc

    except (sqlite3.OperationalError, DatabaseWriteError) as exc:

        logger.exception("approve withdrawal_id=%s database error", withdrawal_id)

        raise HTTPException(status_code=500, detail=str(exc)) from exc

    except Exception as exc:

        logger.exception("approve withdrawal_id=%s failed", withdrawal_id)

        raise HTTPException(

            status_code=500,

            detail=f"{APPROVE_WITHDRAWAL_FAILED} ({exc})",

        ) from exc



    withdrawal = result["withdrawal"]

    await notify_withdrawal_approved(

        int(withdrawal["user_id"]),

        amount_dh=float(withdrawal["amount"]),

        method=str(withdrawal["method"]),

    )

    return result





@router.post("/{withdrawal_id}/reject")

async def reject_withdrawal_endpoint(

    withdrawal_id: int,

    body: RejectWithdrawalRequest,

):

    try:

        result = await reject_withdrawal(withdrawal_id, reason=body.reason)

    except WithdrawalNotFoundError as exc:

        raise HTTPException(status_code=404, detail=str(exc)) from exc

    except WithdrawalNotPendingError as exc:

        raise HTTPException(status_code=409, detail=str(exc)) from exc

    except WithdrawalAlreadyProcessedError as exc:

        raise HTTPException(status_code=409, detail=str(exc)) from exc

    except DatabaseLockedError as exc:

        raise _locked_response(exc) from exc

    except (sqlite3.OperationalError, DatabaseWriteError) as exc:

        logger.exception("reject withdrawal_id=%s database error", withdrawal_id)

        raise HTTPException(status_code=500, detail=str(exc)) from exc

    except Exception as exc:

        logger.exception("reject withdrawal_id=%s failed", withdrawal_id)

        raise HTTPException(

            status_code=500,

            detail=f"{REJECT_WITHDRAWAL_FAILED} ({exc})",

        ) from exc



    withdrawal = result["withdrawal"]

    await notify_withdrawal_rejected(

        int(withdrawal["user_id"]),

        amount_dh=float(result.get("refunded_dh") or withdrawal["amount"]),

        reason=body.reason,

        withdrawal_type=str(withdrawal["withdrawal_type"]),

    )

    return result


