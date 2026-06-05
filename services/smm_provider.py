"""SMM provider API client (balance, etc.)."""

from __future__ import annotations



import asyncio

import json

import logging

from typing import Any



import aiohttp



from config import API_URL, SMM_API_KEYS, SMM_KEY_DEFAULT

from utils.provider_parse import normalize_provider_services_list



logger = logging.getLogger("soldium.provider")

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)



BREAKDOWN_ACCOUNTS: tuple[str, ...] = ("tiktok", "instagram", "facebook", "default")





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





def _any_smm_key_configured() -> bool:

    return any(_key_valid(key) for key in SMM_API_KEYS.values())





def _default_key_configured() -> bool:

    return _key_valid(SMM_KEY_DEFAULT)





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

        raise ProviderUnavailableError(

            f"استجابة غير متوقعة من المزوّد (رمز {status})",

        )



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

    account: str,

    api_key: str,

) -> float:

    if not _key_valid(api_key):

        logger.warning("SMM key missing or placeholder for account=%s", account)

        return 0.0



    payload = {"key": api_key, "action": "balance"}

    try:

        async with session.post(API_URL, data=payload) as response:

            data = await _parse_response(response, action="balance")

        return _balance_from_payload(data)

    except Exception as exc:

        logger.warning(

            "Provider balance fetch failed account=%s: %s",

            account,

            exc,

        )

        return 0.0





async def fetch_provider_balance() -> dict[str, Any]:

    """

    Fetch balance for all SMM accounts concurrently (action=balance).

    Returns total_usd, per-account breakdown, and ok/error.

    """

    if not _any_smm_key_configured():

        return {

            "ok": False,

            "error": "لم يتم ضبط مفاتيح SMM_KEY_* في ملف البيئة.",

        }



    try:

        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:

            results = await asyncio.gather(

                *[

                    _fetch_account_balance(session, account, SMM_API_KEYS[account])

                    for account in BREAKDOWN_ACCOUNTS

                ],

            )

    except Exception as exc:

        logger.exception("Provider balance concurrent fetch failed")

        return {"ok": False, "error": f"خطأ غير متوقع: {exc}"}



    breakdown = {

        account: float(balance)

        for account, balance in zip(BREAKDOWN_ACCOUNTS, results, strict=True)

    }

    total_usd = round(sum(breakdown.values()), 4)



    return {

        "ok": True,

        "total_usd": total_usd,

        "breakdown": breakdown,

        "currency": "USD",

        "balance": total_usd,

    }





async def fetch_provider_services() -> dict[str, Any]:

    """Standard SMM API services list (action=services) via default account key."""

    if not _default_key_configured():

        return {

            "ok": False,

            "error": "لم يتم ضبط SMM_KEY_DEFAULT في ملف البيئة.",

            "services": [],

        }



    payload = {"key": SMM_KEY_DEFAULT, "action": "services"}



    try:

        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:

            async with session.post(API_URL, data=payload) as response:

                data = await _parse_response(response, action="services")

    except ProviderAuthError as exc:

        logger.warning("Provider services auth failed: %s", exc)

        return {"ok": False, "error": str(exc), "services": []}

    except ProviderUnavailableError as exc:

        logger.warning("Provider services unavailable: %s", exc)

        return {"ok": False, "error": str(exc), "services": []}

    except aiohttp.ClientError as exc:

        logger.warning("Provider services network error: %s", exc)

        return {

            "ok": False,

            "error": "تعذّر الاتصال بمزوّد الخدمة.",

            "services": [],

        }

    except Exception as exc:

        logger.exception("Provider services unexpected error")

        return {"ok": False, "error": f"خطأ غير متوقع: {exc}", "services": []}



    services = normalize_provider_services_list(data)

    if not services:

        return {

            "ok": False,

            "error": "صيغة قائمة الخدمات غير متوقعة من المزوّد.",

            "services": [],

        }



    return {"ok": True, "services": services, "count": len(services)}


async def submit_provider_order(
    *,
    api_key: str,
    service_id: int,
    link: str,
    quantity: int,
) -> str:
    """Submit order to distributor API; returns distributor order id."""
    if not _key_valid(api_key):
        raise ProviderUnavailableError("مفتاح API غير مضبوط.")

    payload = {
        "key": api_key,
        "action": "add",
        "service": service_id,
        "link": link,
        "quantity": quantity,
    }

    async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        async with session.post(API_URL, data=payload) as response:
            data = await _parse_response(response, action="add")

    if not isinstance(data, dict):
        raise ProviderUnavailableError("صيغة استجابة إنشاء الطلب غير متوقعة.")

    provider_ref = str(data.get("order") or "").strip()
    if not provider_ref:
        raise ProviderUnavailableError("لم يُرجع الموزّد رقم الطلب.")

    return provider_ref

