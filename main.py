"""
Soldium Admin Dashboard — FastAPI entry point.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from admin_log import setup_admin_logging
from database_connector import DB_PATH, count_users
from db_schema import ensure_smm_services_table
from middleware.auth import AdminAuthMiddleware
from routers import (
    api_analytics,
    api_broadcast,
    api_deposits,
    api_system,
    api_services,
    api_orders,
    api_provider,
    api_stats,
    api_users,
    api_withdrawals,
    auth_api,
    web,
)
from utils.messages_ar import HEALTH_DB_QUERY_FAILED

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_ASSETS_DIR = Path(__file__).resolve().parent / "assets"


_db_log = logging.getLogger("soldium.db")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_admin_logging()
    _db_log.info("Dashboard database path: %s", DB_PATH)
    try:
        await ensure_smm_services_table()
    except Exception as exc:
        _db_log.warning("smm_services schema init failed: %s", exc)
    yield


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
app.include_router(api_analytics.router)
app.include_router(api_provider.router)
app.include_router(api_services.router)
app.include_router(api_deposits.router)
app.include_router(api_users.router)
app.include_router(api_orders.router)
app.include_router(api_withdrawals.router)
app.include_router(api_broadcast.router)
app.include_router(api_system.router)


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
