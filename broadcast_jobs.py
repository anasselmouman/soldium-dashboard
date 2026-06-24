"""In-memory tracking for background broadcast jobs."""
from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

JOB_TTL_SECONDS = 3600
_EXCLUSIVE_KINDS = frozenset({"mass", "timed"})


class BroadcastJobBusyError(Exception):
    """Raised when a mass/timed job is already running."""

    def __init__(self, message: str = "يوجد بث أو إعلان قيد الإرسال حالياً. انتظر اكتماله.") -> None:
        self.message = message
        super().__init__(message)


_jobs: dict[str, dict[str, Any]] = {}
_lock = asyncio.Lock()
_running_exclusive: set[str] = set()


def _purge_stale_locked() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    stale = [
        job_id
        for job_id, job in _jobs.items()
        if job.get("updated_at", 0) < cutoff and job.get("status") != "running"
    ]
    for job_id in stale:
        _jobs.pop(job_id, None)


async def create_job(
    *,
    kind: str,
    total: int,
    meta: dict[str, Any] | None = None,
) -> str:
    async with _lock:
        if kind in _EXCLUSIVE_KINDS and _running_exclusive:
            raise BroadcastJobBusyError()

        _purge_stale_locked()
        job_id = secrets.token_urlsafe(12)
        now = time.time()
        _jobs[job_id] = {
            "job_id": job_id,
            "kind": kind,
            "status": "running",
            "total": max(0, int(total)),
            "sent": 0,
            "failed": 0,
            "message": "جاري الإرسال…",
            "meta": dict(meta or {}),
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        if kind in _EXCLUSIVE_KINDS:
            _running_exclusive.add(kind)
        return job_id


async def update_job_progress(job_id: str, *, sent: int, failed: int) -> None:
    async with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["sent"] = sent
        job["failed"] = failed
        job["updated_at"] = time.time()


async def complete_job(job_id: str, *, result: dict[str, Any], message: str) -> None:
    async with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["status"] = "completed"
        job["result"] = result
        job["message"] = message
        job["updated_at"] = time.time()
        kind = job.get("kind")
        if kind in _EXCLUSIVE_KINDS:
            _running_exclusive.discard(kind)


async def fail_job(job_id: str, *, error: str) -> None:
    async with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["status"] = "failed"
        job["error"] = error
        job["message"] = error
        job["updated_at"] = time.time()
        kind = job.get("kind")
        if kind in _EXCLUSIVE_KINDS:
            _running_exclusive.discard(kind)


async def release_job_if_still_running(job_id: str) -> None:
    """Safety net — free exclusive lock if a background task died unexpectedly."""
    async with _lock:
        job = _jobs.get(job_id)
        if job is None or job.get("status") != "running":
            return
        job["status"] = "failed"
        job["error"] = "توقفت مهمة الإرسال بشكل غير متوقع."
        job["message"] = job["error"]
        job["updated_at"] = time.time()
        kind = job.get("kind")
        if kind in _EXCLUSIVE_KINDS:
            _running_exclusive.discard(kind)


async def get_job(job_id: str) -> dict[str, Any] | None:
    async with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        return dict(job)
