"""JSON API for mass Telegram broadcasts and private messages."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from admin_log import logger
from broadcast import BroadcastValidationError, send_broadcast, send_private_messages
from schemas import BroadcastRequest, LaunchTimedAnnouncementRequest, PrivateMessageRequest
from timed_announcements import (
    launch_timed_announcement,
    list_active_timed_announcements,
    list_timed_announcements,
    stop_timed_announcement,
)
from utils.messages_ar import (
    BROADCAST_FAILED,
    PRIVATE_MESSAGE_FAILED,
    STOP_TIMED_ANNOUNCEMENT_FAILED,
    TIMED_ANNOUNCEMENT_FAILED,
)

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


@router.post("/private")
async def broadcast_private(body: PrivateMessageRequest):
    unique_ids = list(dict.fromkeys(body.user_ids))
    try:
        result = await send_private_messages(unique_ids, body.message_html)
    except BroadcastValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("private message failed")
        raise HTTPException(
            status_code=500,
            detail=f"{PRIVATE_MESSAGE_FAILED} ({exc})",
        ) from exc

    return {
        "ok": True,
        "message": "اكتمل إرسال الرسالة الخاصة.",
        "total_recipients": result["total_recipients"],
        "sent": result["sent"],
        "failed": result["failed"],
        "invalid_user_ids": result["invalid_user_ids"],
    }


@router.get("/timed")
async def get_timed_announcements():
    try:
        announcements = await list_timed_announcements()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("list timed announcements failed")
        raise HTTPException(
            status_code=500,
            detail=f"{TIMED_ANNOUNCEMENT_FAILED} ({exc})",
        ) from exc
    return {"ok": True, "announcements": announcements}


@router.get("/timed/active")
async def get_active_timed_announcements():
    try:
        announcements = await list_active_timed_announcements()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("list active timed announcements failed")
        raise HTTPException(
            status_code=500,
            detail=f"{TIMED_ANNOUNCEMENT_FAILED} ({exc})",
        ) from exc
    return {
        "ok": True,
        "count": len(announcements),
        "announcements": announcements,
    }


@router.post("/timed")
async def create_timed_announcement(body: LaunchTimedAnnouncementRequest):
    try:
        result = await launch_timed_announcement(body.message_html, body.ends_at)
    except BroadcastValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("launch timed announcement failed")
        raise HTTPException(
            status_code=500,
            detail=f"{TIMED_ANNOUNCEMENT_FAILED} ({exc})",
        ) from exc

    return {
        "ok": True,
        "message": "تم إطلاق الإعلان المؤقت.",
        "announcement": result["announcement"],
        "total_users": result["total_users"],
        "sent": result["sent"],
        "failed": result["failed"],
    }


@router.post("/timed/{announcement_id}/stop")
async def stop_timed_announcement_endpoint(announcement_id: int):
    try:
        result = await stop_timed_announcement(announcement_id)
    except BroadcastValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("stop timed announcement id=%s failed", announcement_id)
        raise HTTPException(
            status_code=500,
            detail=f"{STOP_TIMED_ANNOUNCEMENT_FAILED} ({exc})",
        ) from exc

    return {
        "ok": True,
        "message": "تم إيقاف الإعلان المؤقت.",
        "announcement": result["announcement"],
    }
