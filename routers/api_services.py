"""Service catalog API — smm_services SQLite table."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

import smm_services as catalog
from admin_log import logger
from schemas import PatchServiceRequest
from services.smm_provider import fetch_provider_services
from utils.messages_ar import (
    LOAD_SERVICES_CATALOG_FAILED,
    SAVE_SERVICES_CATALOG_FAILED,
    SYNC_PROVIDER_SERVICES_FAILED,
    service_not_found,
)

router = APIRouter(prefix="/api", tags=["services"])


@router.get("/services/platforms")
async def get_service_platforms(
    bot_only: bool = Query(default=False, description="عرض منصات البوت فقط"),
):
    try:
        platforms = await catalog.list_platforms(bot_only=bot_only)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("list service platforms failed")
        raise HTTPException(
            status_code=503,
            detail=f"{LOAD_SERVICES_CATALOG_FAILED} ({exc})",
        ) from exc

    return {"ok": True, "platforms": platforms, "count": len(platforms)}


@router.get("/services/categories")
async def get_service_categories():
    try:
        categories = await catalog.list_categories()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("list service categories failed")
        raise HTTPException(
            status_code=503,
            detail=f"{LOAD_SERVICES_CATALOG_FAILED} ({exc})",
        ) from exc

    return {"ok": True, "categories": categories, "count": len(categories)}


@router.get("/services")
async def list_services(
    platform: str | None = Query(default=None, description="تصفية حسب المنصة"),
    provider: str | None = Query(default=None, description="تصفية حسب المزوّد"),
    category: str | None = Query(default=None, description="تصفية حسب التصنيف"),
    search: str | None = Query(default=None, description="بحث في الاسم أو المعرّف"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    bot_only: bool = Query(default=False, description="خدمات البوت النشطة فقط"),
):
    try:
        result = await catalog.list_services_paginated(
            category=category,
            platform=platform,
            provider=provider,
            search=search,
            page=page,
            limit=limit,
            bot_only=bot_only,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("list services paginated failed")
        raise HTTPException(
            status_code=503,
            detail=f"{LOAD_SERVICES_CATALOG_FAILED} ({exc})",
        ) from exc

    return {
        "ok": True,
        "services": result["services"],
        "total_pages": result["total_pages"],
        "current_page": result["current_page"],
        "total_items": result["total_items"],
        "active_count": result["active_count"],
        "pending_count": result["pending_count"],
        "platform_count": result["platform_count"],
        "limit": result["limit"],
        "bot_only": result["bot_only"],
    }


@router.patch("/services/{service_id}")
async def patch_service(service_id: str, body: PatchServiceRequest):
    if (
        body.name_ar is None
        and body.local_price_dh is None
        and body.is_active is None
    ):
        raise HTTPException(status_code=400, detail="لا توجد حقول للتحديث.")

    try:
        updated = await catalog.update_service(
            service_id,
            name_ar=body.name_ar,
            local_price_dh=body.local_price_dh,
            is_active=body.is_active,
        )
    except Exception as exc:
        logger.exception("patch service failed service_id=%s", service_id)
        raise HTTPException(
            status_code=500,
            detail=f"{SAVE_SERVICES_CATALOG_FAILED} ({exc})",
        ) from exc

    if not updated:
        raise HTTPException(status_code=404, detail=service_not_found(service_id))

    row = await catalog.get_service(service_id)
    return {
        "ok": True,
        "message": "تم حفظ التعديلات.",
        "item": row,
    }


@router.post("/services/sync")
async def sync_services_with_provider(
    provider: str | None = Query(default=None, description="مزامنة مزوّد محدد فقط"),
):
    try:
        stats = await catalog.sync_catalog_with_providers(provider_slug=provider)
    except Exception as exc:
        logger.exception("sync provider services failed")
        raise HTTPException(
            status_code=503,
            detail=f"{SYNC_PROVIDER_SERVICES_FAILED} ({exc})",
        ) from exc

    if not stats.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=stats.get("error") or SYNC_PROVIDER_SERVICES_FAILED,
        )

    try:
        platforms = await catalog.list_platforms(bot_only=False)
    except Exception as exc:
        logger.exception("sync merge into db failed")
        raise HTTPException(
            status_code=500,
            detail=f"{SYNC_PROVIDER_SERVICES_FAILED} ({exc})",
        ) from exc

    return {
        "ok": True,
        "message": (
            f"تمت المزامنة: {stats['updated']} محدّثة، "
            f"{stats['inserted']} جديدة بانتظار المراجعة، "
            f"{stats.get('rates_applied', 0)} سعر مزوّد، "
            f"{stats.get('price_sync_updated', 0)} سعر كتالوج محدّث."
        ),
        "sync_stats": stats,
        "platforms": platforms,
    }


@router.get("/provider/services")
async def get_provider_services():
    """Raw provider list (debug / optional UI)."""
    try:
        return await fetch_provider_services()
    except Exception as exc:
        logger.exception("fetch provider services failed")
        raise HTTPException(
            status_code=503,
            detail=f"{SYNC_PROVIDER_SERVICES_FAILED} ({exc})",
        ) from exc
