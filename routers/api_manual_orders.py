"""JSON API for manual-fulfillment order management."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Query

from admin_log import logger
from database_connector import DatabaseLockedError, DatabaseWriteError
from manual_orders import (
    ManualOrderAlreadyProcessedError,
    ManualOrderNotFoundError,
    ManualOrderNotPendingError,
    complete_manual_order,
    ensure_provider_order_ref,
    get_manual_order,
    get_pending_manual_orders_summary,
    list_manual_order_history,
    reject_manual_order,
)
from notifier import (
    bot_token_configured,
    notify_manual_order_completed,
    notify_manual_order_customer_message,
    notify_manual_order_rejected,
)
from schemas import RejectManualOrderRequest, SendManualOrderNotifyRequest
from utils.messages_ar import (
    BOT_TOKEN_NOT_CONFIGURED,
    COMPLETE_MANUAL_ORDER_FAILED,
    DB_BUSY,
    LOAD_MANUAL_ORDER_HISTORY_FAILED,
    LOAD_MANUAL_ORDERS_FAILED,
    MANUAL_ORDER_REF_UNAVAILABLE,
    REJECT_MANUAL_ORDER_FAILED,
    SEND_MANUAL_ORDER_NOTIFY_FAILED,
)

router = APIRouter(prefix="/api/manual-orders", tags=["manual-orders"])


def _locked_response(exc: DatabaseLockedError) -> HTTPException:
    logger.warning("database locked during manual order action: %s", exc)
    return HTTPException(
        status_code=503,
        detail={"error": "database_locked", "message": DB_BUSY},
    )


@router.get("/pending")
async def list_pending_manual_orders():
    try:
        return await get_pending_manual_orders_summary()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{LOAD_MANUAL_ORDERS_FAILED} ({exc})",
        ) from exc


@router.get("/history")
async def list_manual_orders_history(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    status: str | None = Query(None),
    search: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
):
    try:
        return await list_manual_order_history(
            page=page,
            limit=limit,
            status=status,
            search=search,
            from_date=from_date,
            to_date=to_date,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{LOAD_MANUAL_ORDER_HISTORY_FAILED} ({exc})",
        ) from exc


@router.get("/{order_id}")
async def get_manual_order_detail(order_id: int):
    try:
        order = await get_manual_order(order_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{LOAD_MANUAL_ORDERS_FAILED} ({exc})",
        ) from exc

    if order is None:
        raise HTTPException(status_code=404, detail="الطلب غير موجود.")
    return order


@router.post("/{order_id}/complete")
async def complete_manual_order_endpoint(order_id: int):
    try:
        result = await complete_manual_order(order_id)
    except ManualOrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ManualOrderNotPendingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ManualOrderAlreadyProcessedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DatabaseLockedError as exc:
        raise _locked_response(exc) from exc
    except (sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.exception("complete manual order_id=%s database error", order_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("complete manual order_id=%s failed", order_id)
        raise HTTPException(
            status_code=500,
            detail=f"{COMPLETE_MANUAL_ORDER_FAILED} ({exc})",
        ) from exc

    order = result["order"]
    provider_ref = await ensure_provider_order_ref(order)
    if not provider_ref:
        raise HTTPException(status_code=503, detail=MANUAL_ORDER_REF_UNAVAILABLE)
    await notify_manual_order_completed(
        int(order["user_id"]),
        provider_order_id=provider_ref,
    )
    return result


@router.post("/{order_id}/reject")
async def reject_manual_order_endpoint(
    order_id: int,
    body: RejectManualOrderRequest,
):
    try:
        result = await reject_manual_order(order_id, reason=body.reason)
    except ManualOrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ManualOrderNotPendingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ManualOrderAlreadyProcessedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DatabaseLockedError as exc:
        raise _locked_response(exc) from exc
    except (sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.exception("reject manual order_id=%s database error", order_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("reject manual order_id=%s failed", order_id)
        raise HTTPException(
            status_code=500,
            detail=f"{REJECT_MANUAL_ORDER_FAILED} ({exc})",
        ) from exc

    order = result["order"]
    provider_ref = await ensure_provider_order_ref(order)
    if not provider_ref:
        raise HTTPException(status_code=503, detail=MANUAL_ORDER_REF_UNAVAILABLE)
    await notify_manual_order_rejected(
        int(order["user_id"]),
        provider_order_id=provider_ref,
        amount_dh=float(result.get("refunded_dh") or order["amount"]),
        reason=body.reason,
    )
    return result


@router.post("/{order_id}/notify")
async def notify_manual_order_customer_endpoint(
    order_id: int,
    body: SendManualOrderNotifyRequest,
):
    if not bot_token_configured():
        raise HTTPException(status_code=503, detail=BOT_TOKEN_NOT_CONFIGURED)

    try:
        order = await get_manual_order(order_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{LOAD_MANUAL_ORDERS_FAILED} ({exc})",
        ) from exc

    if order is None:
        raise HTTPException(status_code=404, detail="الطلب غير موجود.")

    user_id = int(order["user_id"])
    message = body.message.strip()

    provider_ref = await ensure_provider_order_ref(order)
    if not provider_ref:
        raise HTTPException(status_code=503, detail=MANUAL_ORDER_REF_UNAVAILABLE)

    try:
        sent = await notify_manual_order_customer_message(
            user_id,
            provider_order_id=provider_ref,
            message=message,
        )
    except Exception as exc:
        logger.exception("notify manual order_id=%s user_id=%s failed", order_id, user_id)
        raise HTTPException(
            status_code=500,
            detail=f"{SEND_MANUAL_ORDER_NOTIFY_FAILED} ({exc})",
        ) from exc

    logger.info(
        "NOTIFY_MANUAL_ORDER order_id=%s user_id=%s sent=%s",
        order_id,
        user_id,
        sent,
    )
    return {
        "ok": True,
        "order_id": order_id,
        "user_id": user_id,
        "notification_sent": sent,
    }
