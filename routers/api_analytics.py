"""JSON API for the analytics & statistics hub."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from analytics import (
    get_analytics_summary,
    get_cashflow_chart,
    get_gateways_chart,
    get_leaderboards,
    get_liquidity_metrics,
    get_orders_status_chart,
    get_profit_chart,
)
from utils.messages_ar import LOAD_ANALYTICS_FAILED

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _analytics_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(
        status_code=503,
        detail=f"{LOAD_ANALYTICS_FAILED} ({exc})",
    )


@router.get("/summary")
async def analytics_summary():
    try:
        return await get_analytics_summary()
    except Exception as exc:
        raise _analytics_error(exc) from exc


@router.get("/charts/cashflow")
async def cashflow_chart():
    try:
        return await get_cashflow_chart()
    except Exception as exc:
        raise _analytics_error(exc) from exc


@router.get("/charts/gateways")
async def gateways_chart():
    try:
        return await get_gateways_chart()
    except Exception as exc:
        raise _analytics_error(exc) from exc


@router.get("/charts/profit")
async def profit_chart():
    try:
        return await get_profit_chart()
    except Exception as exc:
        raise _analytics_error(exc) from exc


@router.get("/charts/orders-status")
async def orders_status_chart():
    try:
        return await get_orders_status_chart()
    except Exception as exc:
        raise _analytics_error(exc) from exc


@router.get("/leaderboards")
async def leaderboards():
    try:
        return await get_leaderboards()
    except Exception as exc:
        raise _analytics_error(exc) from exc


@router.get("/liquidity")
async def liquidity_metrics():
    """Legacy full liquidity bundle (kept for compatibility)."""
    try:
        return await get_liquidity_metrics()
    except Exception as exc:
        raise _analytics_error(exc) from exc
