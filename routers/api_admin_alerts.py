"""JSON API for admin system alerts."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from admin_alerts import dismiss_alert, list_open_alerts, scan_all_alerts

router = APIRouter(prefix="/api/admin-alerts", tags=["admin-alerts"])


@router.get("")
async def get_admin_alerts(limit: int = 50):
    try:
        alerts = await list_open_alerts(limit=limit)
        return {"ok": True, "alerts": alerts, "count": len(alerts)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/scan")
async def trigger_alert_scan():
    try:
        result = await scan_all_alerts()
        alerts = await list_open_alerts(limit=50)
        return {"ok": True, "scan": result, "alerts": alerts}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/{alert_id}/dismiss")
async def dismiss_admin_alert(alert_id: int):
    try:
        ok = await dismiss_alert(alert_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="التحذير غير موجود أو مُعالَج مسبقاً.")
    return {"ok": True, "alert_id": alert_id}
