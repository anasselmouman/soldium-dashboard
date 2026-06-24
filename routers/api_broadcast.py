"""JSON API for mass Telegram broadcasts and private messages."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException

from admin_log import logger
from notifier import bot_token_configured
from broadcast import BroadcastValidationError, list_all_user_ids
from broadcast_jobs import BroadcastJobBusyError, create_job, get_job
from broadcast_tasks import (
    run_mass_broadcast_job,
    run_private_broadcast_job,
    run_timed_announcement_job,
    start_timed_announcement_job,
)
from schemas import BroadcastRequest, LaunchTimedAnnouncementRequest, PrivateMessageRequest
from timed_announcements import (
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


def _job_busy_response(exc: BroadcastJobBusyError) -> HTTPException:
    return HTTPException(status_code=409, detail=exc.message)


@router.get("/jobs/{job_id}")
async def get_broadcast_job(job_id: str):
    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="مهمة البث غير موجودة أو انتهت صلاحيتها.")
    return {"ok": True, **job}


@router.post("")
async def broadcast_to_all(body: BroadcastRequest, background_tasks: BackgroundTasks):
    try:
        if not bot_token_configured():
            raise BroadcastValidationError(
                "لم يتم ضبط BOT_TOKEN — تعذّر إرسال الرسائل عبر تيليغرام.",
            )
        user_ids = await list_all_user_ids()
        job_id = await create_job(kind="mass", total=len(user_ids))
    except BroadcastValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except BroadcastJobBusyError as exc:
        raise _job_busy_response(exc) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("broadcast job setup failed")
        raise HTTPException(
            status_code=500,
            detail=f"{BROADCAST_FAILED} ({exc})",
        ) from exc

    background_tasks.add_task(
        run_mass_broadcast_job,
        job_id,
        body.message_html,
        auto_delete_seconds=body.auto_delete_seconds,
    )

    return {
        "ok": True,
        "job_id": job_id,
        "status": "running",
        "message": "بدأ البث في الخلفية.",
        "total_users": len(user_ids),
    }


@router.post("/private")
async def broadcast_private(body: PrivateMessageRequest, background_tasks: BackgroundTasks):
    unique_ids = list(dict.fromkeys(body.user_ids))
    try:
        from broadcast import resolve_user_ids

        valid_ids, invalid_ids = await resolve_user_ids(unique_ids)
        if not valid_ids:
            raise BroadcastValidationError(
                "لا يوجد أي مستلم صالح من بين المعرّفات المحددة.",
            )
        if len(unique_ids) > 100:
            raise BroadcastValidationError(
                "الحد الأقصى للمستلمين في رسالة واحدة هو 100.",
            )

        job_id = await create_job(
            kind="private",
            total=len(valid_ids),
            meta={"invalid_user_ids": invalid_ids},
        )
    except BroadcastValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except BroadcastJobBusyError as exc:
        raise _job_busy_response(exc) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("private message job setup failed")
        raise HTTPException(
            status_code=500,
            detail=f"{PRIVATE_MESSAGE_FAILED} ({exc})",
        ) from exc

    background_tasks.add_task(
        run_private_broadcast_job,
        job_id,
        unique_ids,
        body.message_html,
        auto_delete_seconds=body.auto_delete_seconds,
    )

    return {
        "ok": True,
        "job_id": job_id,
        "status": "running",
        "message": "بدأ إرسال الرسائل في الخلفية.",
        "total_recipients": len(valid_ids),
        "invalid_user_ids": invalid_ids,
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
async def create_timed_announcement(
    body: LaunchTimedAnnouncementRequest,
    background_tasks: BackgroundTasks,
):
    try:
        job_id, prepared = await start_timed_announcement_job(
            body.message_html,
            body.ends_at,
            auto_delete_seconds=body.auto_delete_seconds,
        )
    except BroadcastValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except BroadcastJobBusyError as exc:
        raise _job_busy_response(exc) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("launch timed announcement failed")
        raise HTTPException(
            status_code=500,
            detail=f"{TIMED_ANNOUNCEMENT_FAILED} ({exc})",
        ) from exc

    background_tasks.add_task(
        run_timed_announcement_job,
        job_id,
        prepared["announcement_id"],
        prepared["message_html"],
        auto_delete_seconds=prepared["auto_delete_seconds"],
    )

    return {
        "ok": True,
        "job_id": job_id,
        "status": "running",
        "message": "بدأ إطلاق الإعلان والإرسال في الخلفية.",
        "announcement": prepared["announcement"],
        "total_users": prepared["total_users"],
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
