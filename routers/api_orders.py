"""JSON API for order management."""

from __future__ import annotations



import sqlite3



from fastapi import APIRouter, HTTPException, Query



from admin_log import logger

from database_connector import DatabaseLockedError, DatabaseWriteError

from notifier import notify_order_status_changed

from orders import (

    OrderNotFoundError,

    OrderStatusError,

    list_orders,

    update_order_status,

)

from schemas import UpdateOrderStatusRequest

from utils.messages_ar import (

    DB_BUSY,

    LOAD_ORDERS_FAILED,

    UPDATE_ORDER_STATUS_FAILED,

)



router = APIRouter(prefix="/api/orders", tags=["orders"])





def _locked_response(exc: DatabaseLockedError) -> HTTPException:

    logger.warning("database locked during order action: %s", exc)

    return HTTPException(

        status_code=503,

        detail={"error": "database_locked", "message": DB_BUSY},

    )





@router.get("")

async def get_orders(

    page: int = Query(1, ge=1, description="رقم الصفحة"),

    limit: int = Query(50, ge=1, le=100, description="عدد النتائج في الصفحة"),

    status: str | None = Query(

        None,

        max_length=50,

        description="تصفية حسب الحالة",

    ),

    search: str | None = Query(

        None,

        max_length=100,

        description="بحث برقم الطلب لدى المزوّد أو معرّف المستخدم",

    ),

):

    try:

        return await list_orders(page=page, limit=limit, status=status, search=search)

    except FileNotFoundError as exc:

        raise HTTPException(status_code=503, detail=str(exc)) from exc

    except Exception as exc:

        raise HTTPException(

            status_code=503,

            detail=f"{LOAD_ORDERS_FAILED} ({exc})",

        ) from exc





@router.patch("/{order_id}/status")

async def patch_order_status(order_id: int, body: UpdateOrderStatusRequest):

    try:

        result = await update_order_status(order_id, body.status)

    except OrderNotFoundError as exc:

        raise HTTPException(status_code=404, detail=str(exc)) from exc

    except OrderStatusError as exc:

        raise HTTPException(status_code=400, detail=exc.message) from exc

    except DatabaseLockedError as exc:

        raise _locked_response(exc) from exc

    except (sqlite3.OperationalError, DatabaseWriteError) as exc:

        logger.exception("patch order status order_id=%s database error", order_id)

        raise HTTPException(status_code=500, detail=str(exc)) from exc

    except Exception as exc:

        logger.exception("patch order status order_id=%s failed", order_id)

        raise HTTPException(

            status_code=500,

            detail=f"{UPDATE_ORDER_STATUS_FAILED} ({exc})",

        ) from exc



    order = result["order"]

    if result.get("status_changed"):

        await notify_order_status_changed(

            int(order["user_id"]),

            order_id=order_id,

            provider_order_id=order.get("provider_order_id"),

            new_status=order["status"],

            refunded_dh=float(result.get("refunded_dh") or 0.0),

        )

    return result


