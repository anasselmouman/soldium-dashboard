"""JSON API for user and referral management."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Query

from admin_log import logger
from database_connector import DatabaseLockedError, DatabaseWriteError
from notifier import notify_balance_adjusted, notify_referral_level_changed
from schemas import AdjustBalanceRequest, ChangeReferralLevelRequest
from users import (
    UserNotFoundError,
    UserValidationError,
    adjust_user_balance,
    change_user_referral_level,
    get_user_full_logs,
    list_users,
)
from utils.messages_ar import (
    ADJUST_BALANCE_FAILED,
    CHANGE_REFERRAL_LEVEL_FAILED,
    DB_BUSY,
    LOAD_USER_PROFILE_FAILED,
    LOAD_USERS_FAILED,
    user_not_found,
)

router = APIRouter(prefix="/api/users", tags=["users"])


def _locked_response(exc: DatabaseLockedError) -> HTTPException:
    logger.warning("database locked during user action: %s", exc)
    return HTTPException(
        status_code=503,
        detail={"error": "database_locked", "message": DB_BUSY},
    )


@router.get("")
async def get_users(
    page: int = Query(1, ge=1, description="رقم الصفحة"),
    limit: int = Query(25, ge=1, le=100, description="عدد النتائج في الصفحة"),
    search: str | None = Query(
        None,
        max_length=100,
        description="بحث بمعرّف تيليغرام أو اسم المستخدم",
    ),
):
    try:
        return await list_users(page=page, limit=limit, search=search)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{LOAD_USERS_FAILED} ({exc})",
        ) from exc


@router.get("/{user_id}/full-logs")
async def get_user_full_logs_endpoint(user_id: int):
    try:
        payload = await get_user_full_logs(user_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{LOAD_USER_PROFILE_FAILED} ({exc})",
        ) from exc

    if payload is None:
        raise HTTPException(status_code=404, detail=user_not_found(user_id))

    return payload


@router.post("/{user_id}/adjust-balance")
async def adjust_balance_endpoint(user_id: int, body: AdjustBalanceRequest):
    try:
        result = await adjust_user_balance(
            user_id,
            body.amount_dh,
            reason=body.reason,
        )
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UserValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except DatabaseLockedError as exc:
        raise _locked_response(exc) from exc
    except (sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.exception("adjust balance user_id=%s database error", user_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("adjust balance user_id=%s failed", user_id)
        raise HTTPException(
            status_code=500,
            detail=f"{ADJUST_BALANCE_FAILED} ({exc})",
        ) from exc

    await notify_balance_adjusted(
        user_id,
        amount_dh=result["adjustment_dh"],
        new_balance=result["balance"],
        reason=body.reason,
    )
    return result


@router.post("/{user_id}/change-referral-level")
async def change_referral_level_endpoint(
    user_id: int,
    body: ChangeReferralLevelRequest,
):
    try:
        result = await change_user_referral_level(user_id, body.new_level)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DatabaseLockedError as exc:
        raise _locked_response(exc) from exc
    except (sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.exception(
            "change referral level user_id=%s database error", user_id,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("change referral level user_id=%s failed", user_id)
        raise HTTPException(
            status_code=500,
            detail=f"{CHANGE_REFERRAL_LEVEL_FAILED} ({exc})",
        ) from exc

    await notify_referral_level_changed(user_id, result["referral_level"])
    return result
