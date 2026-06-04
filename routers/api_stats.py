"""JSON API for dashboard summary statistics."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from stats import get_dashboard_stats
from utils.messages_ar import LOAD_STATS_FAILED

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("")
async def get_stats():
    try:
        return await get_dashboard_stats()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{LOAD_STATS_FAILED} ({exc})",
        ) from exc
