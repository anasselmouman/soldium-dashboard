"""JSON API for scheduled / recurring orders."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Query

from admin_log import logger
from database_connector import DatabaseLockedError, DatabaseWriteError
from scheduled_orders import (
    ScheduledOrderNotFoundError,
    ScheduledOrderValidationError,
    create_scheduled_order,
    delete_scheduled_order,
    execute_scheduled_order,
    get_scheduled_order,
    list_scheduled_order_runs,
    list_scheduled_orders,
    pause_scheduled_order,
    resume_scheduled_order,
    search_template_orders,
    stop_scheduled_order,
)
from schemas import CreateScheduledOrderRequest
from utils.messages_ar import DB_BUSY

router = APIRouter(prefix="/api/scheduled-orders", tags=["scheduled-orders"])


def _locked_response(exc: DatabaseLockedError) -> HTTPException:
    logger.warning("database locked during scheduled order action: %s", exc)
    return HTTPException(
        status_code=503,
        detail={"error": "database_locked", "message": DB_BUSY},
    )


@router.get("")
async def get_scheduled_orders(status: str | None = Query(None, max_length=20)):
    try:
        jobs = await list_scheduled_orders(status=status)
        return {"jobs": jobs, "total": len(jobs)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"تعذّر تحميل الطلبات المجدولة ({exc})",
        ) from exc


@router.get("/template-search")
async def search_template_orders_endpoint(
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(10, ge=1, le=20),
):
    try:
        orders = await search_template_orders(q, limit=limit)
        return {"query": q, "orders": orders, "total": len(orders)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"تعذّر البحث عن الطلبات ({exc})",
        ) from exc


@router.get("/{job_id}")
async def get_scheduled_order_detail(job_id: int):
    try:
        job = await get_scheduled_order(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if job is None:
        raise HTTPException(status_code=404, detail="المهمة غير موجودة.")
    runs = await list_scheduled_order_runs(job_id)
    return {"job": job, "runs": runs}


@router.get("/{job_id}/runs")
async def get_scheduled_order_runs(job_id: int, limit: int = Query(20, ge=1, le=100)):
    job = await get_scheduled_order(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="المهمة غير موجودة.")
    runs = await list_scheduled_order_runs(job_id, limit=limit)
    return {"job_id": job_id, "runs": runs}


@router.post("")
async def create_scheduled_order_endpoint(body: CreateScheduledOrderRequest):
    if not body.template_order_ref and body.template_order_id is None:
        raise HTTPException(
            status_code=422,
            detail="اختر طلباً مرجعياً (معرف المزوّد أو رقم داخلي).",
        )
    try:
        job = await create_scheduled_order(
            template_order_ref=body.template_order_ref,
            template_order_id=body.template_order_id,
            quantity_mode=body.quantity_mode,
            quantity_fixed=body.quantity_fixed,
            quantity_min=body.quantity_min,
            quantity_max=body.quantity_max,
            interval_days=body.interval_days,
            first_run_at=body.first_run_at,
            name=body.name,
        )
    except ScheduledOrderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DatabaseLockedError as exc:
        raise _locked_response(exc) from exc
    except (sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.exception("create scheduled order database error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("create scheduled order failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "job": job}


@router.post("/{job_id}/pause")
async def pause_scheduled_order_endpoint(job_id: int):
    try:
        job = await pause_scheduled_order(job_id)
    except ScheduledOrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScheduledOrderValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DatabaseLockedError as exc:
        raise _locked_response(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "job": job}


@router.post("/{job_id}/resume")
async def resume_scheduled_order_endpoint(job_id: int):
    try:
        job = await resume_scheduled_order(job_id)
    except ScheduledOrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScheduledOrderValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DatabaseLockedError as exc:
        raise _locked_response(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "job": job}


@router.post("/{job_id}/stop")
async def stop_scheduled_order_endpoint(job_id: int):
    try:
        job = await stop_scheduled_order(job_id)
    except ScheduledOrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DatabaseLockedError as exc:
        raise _locked_response(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "job": job}


@router.post("/{job_id}/run-now")
async def run_scheduled_order_now_endpoint(job_id: int):
    try:
        result = await execute_scheduled_order(job_id, force=True)
    except ScheduledOrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScheduledOrderValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DatabaseLockedError as exc:
        raise _locked_response(exc) from exc
    except (sqlite3.OperationalError, DatabaseWriteError) as exc:
        logger.exception("run-now scheduled order_id=%s database error", job_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("run-now scheduled order_id=%s failed", job_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@router.delete("/{job_id}")
async def delete_scheduled_order_endpoint(job_id: int):
    try:
        await delete_scheduled_order(job_id)
    except ScheduledOrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DatabaseLockedError as exc:
        raise _locked_response(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "deleted_id": job_id}
