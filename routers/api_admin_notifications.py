"""JSON API for admin notification inbox."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from admin_notifications import (
    get_admin_notification,
    get_notifications_summary,
    list_admin_notifications,
    mark_all_notifications_read,
    mark_notification_read,
)

router = APIRouter(prefix="/api/admin-notifications", tags=["admin-notifications"])


@router.get("")
async def get_notifications(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    category: str | None = Query(None),
    severity: str | None = Query(None),
    search: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
):
    try:
        result = await list_admin_notifications(
            limit=limit,
            offset=offset,
            unread_only=unread_only,
            category=category,
            severity=severity,
            search=search,
            from_date=from_date,
            to_date=to_date,
        )
        return {"ok": True, **result}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/read-all")
async def read_all_notifications():
    try:
        updated = await mark_all_notifications_read()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "updated": updated}


@router.get("/{notification_id}")
async def get_notification(notification_id: int):
    try:
        item = await get_admin_notification(notification_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="الإشعار غير موجود.")
    return {"ok": True, "notification": item}


@router.post("/{notification_id}/read")
async def read_notification(notification_id: int):
    try:
        ok = await mark_notification_read(notification_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        item = await get_admin_notification(notification_id)
        if item is None:
            raise HTTPException(status_code=404, detail="الإشعار غير موجود.")
    return {"ok": True, "notification_id": notification_id}
