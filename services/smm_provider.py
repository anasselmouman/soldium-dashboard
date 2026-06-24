"""SMM provider API client — multi-provider via provider_registry."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from services.provider_registry import (
    get_default_provider_slug,
    get_provider_account_record,
    get_provider_record,
    list_active_provider_accounts,
    resolve_api_key,
)
from utils.provider_parse import normalize_provider_services_list

logger = logging.getLogger("soldium.provider")

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=120)


class ProviderAuthError(Exception):
    pass


class ProviderUnavailableError(Exception):
    pass


def _key_valid(api_key: str) -> bool:
    key = (api_key or "").strip()
    if not key:
        return False
    lowered = key.lower()
    return "your_" not in lowered and "paste" not in lowered


async def _parse_response(
    response: aiohttp.ClientResponse,
    *,
    action: str,
) -> dict[str, Any] | list[Any]:
    status = response.status
    raw = await response.read()
    data: dict[str, Any] | list[Any] | None = None
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None

    if status == 401:
        err = "مفتاح API غير صالح"
        if isinstance(data, dict) and data.get("error"):
            err = str(data["error"])
        raise ProviderAuthError(err)

    if status >= 400:
        raise ProviderUnavailableError(f"استجابة غير متوقعة من المزوّد (رمز {status})")

    if data is None:
        raise ProviderUnavailableError("استجابة فارغة من مزوّد الخدمة.")

    if isinstance(data, dict) and data.get("error"):
        raise ProviderUnavailableError(str(data["error"]))

    return data


def _balance_from_payload(data: dict[str, Any] | list[Any] | None) -> float:
    if not isinstance(data, dict):
        return 0.0
    try:
        return float(data.get("balance", 0))
    except (TypeError, ValueError):
        return 0.0


async def _fetch_account_balance(
    session: aiohttp.ClientSession,
    provider_slug: str,
    account: str,
) -> tuple[str, str, float, str | None]:
    slug = str(provider_slug).strip().lower()
    try:
        provider = get_provider_record(slug)
        if provider is None or not provider.is_active:
            return slug, account, 0.0, "مزوّد غير نشط"
        api_key = resolve_api_key(slug, account)
    except Exception as exc:
        return slug, account, 0.0, str(exc)

    payload = {"key": api_key, "action": "balance"}
    try:
        async with session.post(provider.api_base_url, data=payload) as response:
            data = await _parse_response(response, action="balance")
        return slug, account, _balance_from_payload(data), None
    except Exception as exc:
        logger.warning("Provider balance fetch failed %s/%s: %s", slug, account, exc)
        return slug, account, 0.0, str(exc)


async def fetch_provider_balance() -> dict[str, Any]:
    pairs = list_active_provider_accounts()
    if not pairs:
        return {
            "ok": False,
            "error": "لا توجد حسابات مزوّد نشطة في قاعدة البيانات.",
        }

    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            results = await asyncio.gather(
                *[_fetch_account_balance(session, slug, account) for slug, account in pairs],
            )
    except Exception as exc:
        logger.exception("Provider balance concurrent fetch failed")
        return {"ok": False, "error": f"خطأ غير متوقع: {exc}"}

    breakdown: dict[str, float] = {}
    accounts: list[dict[str, Any]] = []
    errors: list[str] = []
    for slug, account, balance, err in results:
        label = f"{slug}/{account}"
        breakdown[label] = float(balance)
        account_rec = get_provider_account_record(slug, account)
        provider_rec = get_provider_record(slug)
        display_name = account_rec.display_name if account_rec else account
        provider_name = provider_rec.name if provider_rec else slug
        accounts.append(
            {
                "provider_slug": slug,
                "provider_name": provider_name,
                "account_key": account,
                "display_name": display_name,
                "balance_usd": float(balance),
                "ok": err is None,
                "error": err,
            }
        )
        if err:
            errors.append(f"{display_name} ({provider_name}): {err}")

    total_usd = round(sum(breakdown.values()), 4)
    ok = any(item["balance_usd"] > 0 for item in accounts) or not errors

    return {
        "ok": ok,
        "total_usd": total_usd,
        "breakdown": breakdown,
        "accounts": accounts,
        "currency": "USD",
        "balance": total_usd,
        "errors": errors,
    }


async def fetch_provider_services(
    *,
    provider_slug: str | None = None,
) -> dict[str, Any]:
    """Services list per provider account; merged by (provider_slug, service_id)."""
    pairs = list_active_provider_accounts(provider_slug)
    if not pairs:
        return {
            "ok": False,
            "error": "لا توجد حسابات مزوّد نشطة.",
            "services": [],
        }

    merged: dict[tuple[str, int], dict[str, Any]] = {}
    errors: list[str] = []

    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            for slug, account in pairs:
                try:
                    provider = get_provider_record(slug)
                    if provider is None:
                        continue
                    api_key = resolve_api_key(slug, account)
                except Exception as exc:
                    errors.append(f"{slug}/{account}: {exc}")
                    continue
                payload = {"key": api_key, "action": "services"}
                try:
                    async with session.post(provider.api_base_url, data=payload) as response:
                        data = await _parse_response(response, action="services")
                except Exception as exc:
                    errors.append(f"{slug}/{account}: {exc}")
                    continue
                services = normalize_provider_services_list(data)
                for entry in services:
                    if not isinstance(entry, dict):
                        continue
                    try:
                        sid = int(str(entry.get("service")))
                    except (TypeError, ValueError):
                        continue
                    if sid > 0:
                        tagged = dict(entry)
                        tagged["provider_slug"] = slug
                        tagged["api_account"] = account
                        merged[(slug, sid)] = tagged
    except Exception as exc:
        logger.exception("Provider services concurrent fetch failed")
        return {"ok": False, "error": f"خطأ غير متوقع: {exc}", "services": []}

    services_list = list(merged.values())
    if not services_list:
        err = "; ".join(errors) if errors else "لم تُرجع الحسابات أي خدمات."
        return {"ok": False, "error": err, "services": []}

    return {"ok": True, "services": services_list, "count": len(services_list)}


async def submit_provider_order(
    *,
    provider_slug: str,
    account_key: str,
    service_id: int,
    link: str,
    quantity: int,
) -> str:
    slug = str(provider_slug or get_default_provider_slug()).strip().lower()
    account = str(account_key or "default").strip().lower() or "default"
    provider = get_provider_record(slug)
    if provider is None or not provider.is_active:
        raise ProviderUnavailableError(f"المزوّد {slug} غير نشط.")
    api_key = resolve_api_key(slug, account)

    payload = {
        "key": api_key,
        "action": "add",
        "service": service_id,
        "link": link,
        "quantity": quantity,
    }

    async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        async with session.post(provider.api_base_url, data=payload) as response:
            data = await _parse_response(response, action="add")

    if not isinstance(data, dict):
        raise ProviderUnavailableError("صيغة استجابة إنشاء الطلب غير متوقعة.")

    provider_ref = str(data.get("order") or "").strip()
    if not provider_ref:
        raise ProviderUnavailableError("لم يُرجع الموزّد رقم الطلب.")

    return provider_ref
