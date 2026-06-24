"""Provider management API."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

import providers_admin as admin
from admin_log import logger
from schemas import (
    CreateProviderAccountRequest,
    CreateProviderRequest,
    PatchProviderAccountRequest,
    PatchProviderRequest,
)

router = APIRouter(prefix="/api/providers", tags=["providers"])


@router.get("")
async def list_providers():
    try:
        providers = await admin.list_providers_with_accounts()
    except Exception as exc:
        logger.exception("list providers failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "providers": providers, "count": len(providers)}


@router.get("/options")
async def provider_options():
    try:
        options = await admin.list_provider_options()
    except Exception as exc:
        logger.exception("provider options failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "providers": options}


@router.post("")
async def create_provider(body: CreateProviderRequest):
    try:
        item = await admin.create_provider(
            slug=body.slug,
            name=body.name,
            api_base_url=body.api_base_url,
            adapter_type=body.adapter_type,
            is_active=body.is_active,
        )
    except admin.ProviderAdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("create provider failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@router.patch("/{slug}")
async def patch_provider(slug: str, body: PatchProviderRequest):
    if body.name is None and body.api_base_url is None and body.adapter_type is None and body.is_active is None:
        raise HTTPException(status_code=400, detail="لا توجد حقول للتحديث.")
    try:
        item = await admin.update_provider(
            slug,
            name=body.name,
            api_base_url=body.api_base_url,
            adapter_type=body.adapter_type,
            is_active=body.is_active,
        )
    except admin.ProviderAdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("patch provider failed slug=%s", slug)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@router.post("/{slug}/accounts")
async def create_account(slug: str, body: CreateProviderAccountRequest):
    try:
        item = await admin.create_provider_account(
            slug,
            account_key=body.account_key,
            api_key_env=body.api_key_env,
            display_name=body.display_name,
            is_active=body.is_active,
        )
    except admin.ProviderAdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("create provider account failed slug=%s", slug)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@router.patch("/{slug}/accounts/{account_key}")
async def patch_account(slug: str, account_key: str, body: PatchProviderAccountRequest):
    if body.api_key_env is None and body.display_name is None and body.is_active is None:
        raise HTTPException(status_code=400, detail="لا توجد حقول للتحديث.")
    try:
        item = await admin.update_provider_account(
            slug,
            account_key,
            api_key_env=body.api_key_env,
            display_name=body.display_name,
            is_active=body.is_active,
        )
    except admin.ProviderAdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("patch provider account failed %s/%s", slug, account_key)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "item": item}
