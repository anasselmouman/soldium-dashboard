"""JSON API for mass Telegram broadcasts."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from admin_log import logger
from broadcast import BroadcastValidationError, send_broadcast
from schemas import BroadcastRequest
from utils.messages_ar import BROADCAST_FAILED

router = APIRouter(prefix="/api/broadcast", tags=["broadcast"])


@router.post("")
async def broadcast_to_all(body: BroadcastRequest):
    try:
        result = await send_broadcast(body.message_html)
    except BroadcastValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("broadcast failed")
        raise HTTPException(
            status_code=500,
            detail=f"{BROADCAST_FAILED} ({exc})",
        ) from exc

    return {
        "ok": True,
        "message": "اكتمل إرسال البث.",
        "total_users": result["total_users"],
        "sent": result["sent"],
        "failed": result["failed"],
    }
