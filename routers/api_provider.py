"""Provider (SMM) integration API."""
from __future__ import annotations

from fastapi import APIRouter

from services.smm_provider import fetch_provider_balance

router = APIRouter(prefix="/api/provider", tags=["provider"])


@router.get("/balance")
async def provider_balance():
    return await fetch_provider_balance()
