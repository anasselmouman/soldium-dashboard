"""
Soldium Admin Dashboard — FastAPI entry point.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from admin_log import setup_admin_logging
from database_connector import DB_PATH, count_users
from db_schema import (
    ensure_admin_alerts_table,
    ensure_admin_notifications_table,
    ensure_smm_services_table,
    ensure_timed_announcements_tables,
)
from message_deletions import ensure_scheduled_deletions_table, run_deletion_worker
from middleware.auth import AdminAuthMiddleware
from routers import (
    api_admin_alerts,
    api_admin_notifications,
    api_analytics,
    api_broadcast,
    api_deposits,
    api_manual_orders,
    api_services,
    api_orders,
    api_provider,
    api_providers,
    api_stats,
    api_users,
    api_withdrawals,
    auth_api,
    web,
)
from utils.messages_ar import HEALTH_DB_QUERY_FAILED

import smm_services as catalog

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_ASSETS_DIR = Path(__file__).resolve().parent / "assets"


_db_log = logging.getLogger("soldium.db")
_startup_log = logging.getLogger("soldium.startup")


def _providers_configured() -> bool:
    try:
        from services.provider_registry import list_active_provider_accounts

        return bool(list_active_provider_accounts())
    except Exception:
        return False


async def _maybe_backfill_provider_rates() -> None:
    """Sync provider USD rates when most catalog rows lack pricing (background)."""
    if not _providers_configured():
        return
    try:
        zero_count, total = await catalog.count_services_missing_provider_rate()
    except Exception as exc:
        _startup_log.warning("Provider rate check skipped: %s", exc)
        return
    if total <= 0 or zero_count / total <= 0.5:
        return
    try:
        stats = await asyncio.wait_for(
            catalog.sync_catalog_with_providers(),
            timeout=180.0,
        )
        if not stats.get("ok"):
            _startup_log.warning(
                "Provider rate backfill skipped: %s",
                stats.get("error") or "unknown error",
            )
            return
        _startup_log.info("Provider rate backfill on startup: %s", stats)
    except asyncio.TimeoutError:
        _startup_log.warning("Provider rate backfill timed out")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _startup_log.warning("Provider rate backfill failed: %s", exc)


async def _admin_alerts_bootstrap() -> None:
    """Initial scan after startup (background)."""
    try:
        from admin_alerts import ensure_admin_alerts_schema, scan_all_alerts

        await ensure_admin_alerts_schema()
        await scan_all_alerts()
    except Exception as exc:
        _startup_log.warning("Admin alerts initial scan failed: %s", exc)


async def _run_schema_migrations() -> None:
    """Run each schema migration independently so one failure does not skip the rest."""
    migrations: tuple[tuple[str, object], ...] = (
        ("smm_services", ensure_smm_services_table),
        ("timed_announcements", ensure_timed_announcements_tables),
        ("scheduled_deletions", ensure_scheduled_deletions_table),
        ("admin_alerts", ensure_admin_alerts_table),
        ("admin_notifications", ensure_admin_notifications_table),
    )
    for name, migrate in migrations:
        try:
            await migrate()
        except Exception as exc:
            _startup_log.warning("Schema migration %s failed: %s", name, exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_admin_logging()
    _db_log.info("Dashboard database path: %s", DB_PATH)
    backfill_task: asyncio.Task[None] | None = None
    deletion_task: asyncio.Task[None] | None = None
    alerts_task: asyncio.Task[None] | None = None
    alerts_bootstrap: asyncio.Task[None] | None = None
    await _run_schema_migrations()
    try:
        backfill_task = asyncio.create_task(_maybe_backfill_provider_rates())
        deletion_task = asyncio.create_task(run_deletion_worker())
        alerts_bootstrap = asyncio.create_task(_admin_alerts_bootstrap())
        from admin_alerts import run_alert_scan_loop

        alerts_task = asyncio.create_task(run_alert_scan_loop())
    except Exception as exc:
        _startup_log.warning("Dashboard background workers failed to start: %s", exc)
    yield
    if alerts_task is not None:
        alerts_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await alerts_task
    if alerts_bootstrap is not None:
        alerts_bootstrap.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await alerts_bootstrap
    if deletion_task is not None:
        deletion_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await deletion_task
    if backfill_task is not None:
        backfill_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await backfill_task


app = FastAPI(
    title="لوحة تحكم سولديوم",
    description="واجهة إدارة بوت سولديوم على تيليغرام",
    version="0.1.0",
    lifespan=lifespan,
)

if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

if _ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_ASSETS_DIR)), name="assets")

app.add_middleware(AdminAuthMiddleware)

app.include_router(auth_api.router)
app.include_router(web.router)
app.include_router(api_stats.router)
app.include_router(api_admin_alerts.router)
app.include_router(api_admin_notifications.router)
app.include_router(api_analytics.router)
app.include_router(api_provider.router)
app.include_router(api_providers.router)
app.include_router(api_services.router)
app.include_router(api_deposits.router)
app.include_router(api_users.router)
app.include_router(api_orders.router)
app.include_router(api_withdrawals.router)
app.include_router(api_manual_orders.router)
app.include_router(api_broadcast.router)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request, _exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": "بيانات الطلب غير صالحة."},
    )


@app.get("/api/health")
async def health():
    """
    Liveness check plus shared-database connectivity.
    Runs SELECT COUNT(*) FROM users on the bot's users.db.
    """
    try:
        user_count = await count_users()
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail={"status": "error", "message": str(exc), "db_path": str(DB_PATH)},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "message": f"{HEALTH_DB_QUERY_FAILED}: {exc}",
                "db_path": str(DB_PATH),
            },
        ) from exc

    return {
        "status": "ok",
        "message": "الخدمة تعمل بشكل طبيعي",
        "database": str(DB_PATH),
        "users_count": user_count,
    }
