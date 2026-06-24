"""Background runners for broadcast jobs."""
from __future__ import annotations

import asyncio

from admin_log import logger
from broadcast import send_broadcast, send_private_messages
from broadcast_jobs import complete_job, fail_job, release_job_if_still_running, update_job_progress
from timed_announcements import deliver_timed_announcement_to_users, prepare_timed_announcement


def _make_progress_updater(job_id: str):
    async def _on_progress(sent: int, failed: int) -> None:
        await update_job_progress(job_id, sent=sent, failed=failed)

    return _on_progress


async def run_mass_broadcast_job(
    job_id: str,
    message_html: str,
    *,
    auto_delete_seconds: int | None,
) -> None:
    on_progress = _make_progress_updater(job_id)
    try:
        result = await send_broadcast(
            message_html,
            auto_delete_seconds=auto_delete_seconds,
            on_progress=on_progress,
        )
        sent = int(result["sent"])
        total = int(result["total_users"])
        failed = int(result["failed"])
        msg = f"تم إرسال البث إلى {sent} مستخدم من أصل {total}."
        if failed:
            msg += f" ({failed} فشل)"
        await complete_job(job_id, result=result, message=msg)
    except asyncio.CancelledError:
        await fail_job(job_id, error="أُلغيت مهمة الإرسال.")
        raise
    except Exception as exc:
        logger.exception("mass broadcast job %s failed", job_id)
        await fail_job(job_id, error=str(exc))
    finally:
        await release_job_if_still_running(job_id)


async def run_private_broadcast_job(
    job_id: str,
    user_ids: list[int],
    message_html: str,
    *,
    auto_delete_seconds: int | None,
) -> None:
    on_progress = _make_progress_updater(job_id)
    try:
        result = await send_private_messages(
            user_ids,
            message_html,
            auto_delete_seconds=auto_delete_seconds,
            on_progress=on_progress,
        )
        sent = int(result["sent"])
        total = int(result["total_recipients"])
        failed = int(result["failed"])
        msg = f"تم إرسال الرسالة إلى {sent} مستخدم من أصل {total}."
        if failed:
            msg += f" ({failed} فشل)"
        await complete_job(job_id, result=result, message=msg)
    except asyncio.CancelledError:
        await fail_job(job_id, error="أُلغيت مهمة الإرسال.")
        raise
    except Exception as exc:
        logger.exception("private broadcast job %s failed", job_id)
        await fail_job(job_id, error=str(exc))
    finally:
        await release_job_if_still_running(job_id)


async def run_timed_announcement_job(
    job_id: str,
    announcement_id: int,
    message_html: str,
    *,
    auto_delete_seconds: int | None,
) -> None:
    on_progress = _make_progress_updater(job_id)
    try:
        result = await deliver_timed_announcement_to_users(
            announcement_id,
            message_html,
            auto_delete_seconds=auto_delete_seconds,
            on_progress=on_progress,
        )
        sent = int(result["sent"])
        total = int(result["total_users"])
        failed = int(result["failed"])
        msg = f"تم إطلاق الإعلان #{announcement_id} — وصل إلى {sent} من {total}."
        if failed:
            msg += f" ({failed} فشل)"
        await complete_job(job_id, result=result, message=msg)
    except asyncio.CancelledError:
        await fail_job(job_id, error="أُلغيت مهمة الإرسال.")
        raise
    except Exception as exc:
        logger.exception("timed announcement job %s failed", job_id)
        await fail_job(job_id, error=str(exc))
    finally:
        await release_job_if_still_running(job_id)


async def start_timed_announcement_job(
    message_html: str,
    ends_at: str,
    *,
    auto_delete_seconds: int | None,
) -> tuple[str, dict]:
    """Validate, persist announcement, create job — delivery runs separately."""
    from broadcast_jobs import create_job

    prepared = await prepare_timed_announcement(
        message_html,
        ends_at,
        auto_delete_seconds=auto_delete_seconds,
    )
    user_ids = prepared["user_ids"]
    announcement = prepared["announcement"]
    announcement_id = int(announcement["id"])

    job_id = await create_job(
        kind="timed",
        total=len(user_ids),
        meta={"announcement_id": announcement_id},
    )
    return job_id, {
        "announcement": announcement,
        "total_users": len(user_ids),
        "announcement_id": announcement_id,
        "message_html": prepared["message_html"],
        "auto_delete_seconds": prepared["auto_delete_seconds"],
    }
