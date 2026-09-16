# -*- coding: utf-8 -*-
"""Phase 6A — read-only Provider Catalog discovery (normalized, no persistence).

Reuses the existing PerfectPanel / SMM API v2 client (`services.smm_provider`)
and provider registry. Does not create SOLDIUM Catalog entities and does not
write provider data to the database.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from services.provider_registry import (
    GOZIBRA_ADAPTER,
    get_provider_account_record,
    get_provider_record,
)
from services.smm_provider import (
    ProviderAuthError,
    ProviderMalformedResponseError,
    ProviderUnavailableError,
    fetch_provider_account_services,
)
from utils.provider_parse import (
    normalize_provider_services_list,
    parse_float_loose,
    parse_provider_external_service_id,
    parse_provider_rate,
)

logger = logging.getLogger("soldium.catalog.provider_discovery")

DiscoveryErrorCode = Literal[
    "auth_config",
    "api_failure",
    "timeout",
    "network",
    "malformed",
    "unsupported",
    "not_found",
]

# Adapter types known to support read-only `action=services`.
_SUPPORTED_ADAPTERS = frozenset(
    {GOZIBRA_ADAPTER, "gozibra_v2", "perfectpanel", "smm_v2"}
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|password|secret|authorization)\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+[a-z0-9\-._~+/]+=*"),
    re.compile(r"(?i)environment variable\s+\S+"),
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_error_text(text: str) -> str:
    """Strip credential-like fragments from provider/exception text."""
    cleaned = str(text or "").strip()
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[محذوف]", cleaned)
    if len(cleaned) > 240:
        cleaned = cleaned[:240] + "…"
    return cleaned


def _parse_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    parsed = parse_float_loose(value)
    if parsed is None:
        return None
    try:
        return int(parsed)
    except (TypeError, ValueError):
        return None


def _parse_optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


@dataclass
class ProviderCatalogItem:
    """Normalized provider-side service (NOT a SOLDIUM Catalog service)."""

    provider_slug: str
    provider_account_key: str
    external_service_id: str
    provider_service_name: str | None = None
    provider_category: str | None = None
    provider_type: str | None = None
    provider_description: str | None = None
    min_quantity: int | None = None
    max_quantity: int | None = None
    provider_rate: float | None = None
    refill: bool | None = None
    cancel: bool | None = None
    dripfeed: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_slug": self.provider_slug,
            "provider_account_key": self.provider_account_key,
            "external_service_id": self.external_service_id,
            "provider_service_name": self.provider_service_name,
            "provider_category": self.provider_category,
            "provider_type": self.provider_type,
            "provider_description": self.provider_description,
            "min_quantity": self.min_quantity,
            "max_quantity": self.max_quantity,
            "provider_rate": self.provider_rate,
            "refill": self.refill,
            "cancel": self.cancel,
            "dripfeed": self.dripfeed,
        }


@dataclass
class ProviderDiscoveryError:
    code: DiscoveryErrorCode
    message_ar: str
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message_ar": self.message_ar,
            "detail": self.detail,
        }


@dataclass
class ProviderDiscoveryResult:
    provider_slug: str
    provider_name: str
    provider_account_key: str
    account_display_name: str
    discovered_at: str
    ok: bool
    item_count: int
    items: list[ProviderCatalogItem] = field(default_factory=list)
    error: ProviderDiscoveryError | None = None
    skipped_without_id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_slug": self.provider_slug,
            "provider_name": self.provider_name,
            "provider_account_key": self.provider_account_key,
            "account_display_name": self.account_display_name,
            "discovered_at": self.discovered_at,
            "ok": self.ok,
            "item_count": self.item_count,
            "items": [i.to_dict() for i in self.items],
            "skipped_without_id": self.skipped_without_id,
            "error": self.error.to_dict() if self.error else None,
            "operation": "discovery",
        }


def normalize_provider_catalog_item(
    entry: dict[str, Any],
    *,
    provider_slug: str,
    provider_account_key: str,
) -> ProviderCatalogItem | None:
    external_id = parse_provider_external_service_id(entry)
    if not external_id:
        return None

    name = entry.get("name") or entry.get("title") or entry.get("service_name")
    category = entry.get("category")
    stype = entry.get("type") or entry.get("service_type")
    description = entry.get("description") or entry.get("desc") or entry.get("note")

    return ProviderCatalogItem(
        provider_slug=provider_slug,
        provider_account_key=provider_account_key,
        external_service_id=external_id,
        provider_service_name=str(name).strip() if name not in (None, "") else None,
        provider_category=str(category).strip() if category not in (None, "") else None,
        provider_type=str(stype).strip() if stype not in (None, "") else None,
        provider_description=(
            str(description).strip() if description not in (None, "") else None
        ),
        min_quantity=_parse_optional_int(entry.get("min") or entry.get("min_quantity")),
        max_quantity=_parse_optional_int(entry.get("max") or entry.get("max_quantity")),
        provider_rate=parse_provider_rate(entry),
        refill=_parse_optional_bool(entry.get("refill")),
        cancel=_parse_optional_bool(entry.get("cancel")),
        dripfeed=_parse_optional_bool(
            entry.get("dripfeed") or entry.get("drip_feed") or entry.get("drip-feed")
        ),
    )


def _failure(
    *,
    provider_slug: str,
    provider_name: str,
    account_key: str,
    account_display: str,
    code: DiscoveryErrorCode,
    message_ar: str,
    detail: str | None = None,
) -> ProviderDiscoveryResult:
    return ProviderDiscoveryResult(
        provider_slug=provider_slug,
        provider_name=provider_name,
        provider_account_key=account_key,
        account_display_name=account_display,
        discovered_at=_utcnow_iso(),
        ok=False,
        item_count=0,
        items=[],
        error=ProviderDiscoveryError(
            code=code,
            message_ar=message_ar,
            detail=_safe_error_text(detail) if detail else None,
        ),
    )


def _is_empty_catalog_payload(raw: Any, services: list[dict[str, Any]]) -> bool:
    if services:
        return False
    if isinstance(raw, list):
        return True
    if isinstance(raw, dict):
        for key in ("services", "data", "result", "items", "list"):
            nested = raw.get(key)
            if isinstance(nested, list):
                return True
    return False


async def discover_provider_catalog(
    *,
    provider_slug: str,
    account_key: str,
) -> ProviderDiscoveryResult:
    """Discover provider services for one existing provider account (read-only)."""
    slug = str(provider_slug or "").strip().lower()
    account = str(account_key or "").strip().lower() or "default"
    discovered_at = _utcnow_iso()

    provider = get_provider_record(slug) if slug else None
    provider_name = provider.name if provider else slug
    account_rec = get_provider_account_record(slug, account) if slug else None
    account_display = account_rec.display_name if account_rec else account

    if not slug:
        return _failure(
            provider_slug="",
            provider_name="",
            account_key=account,
            account_display=account_display,
            code="not_found",
            message_ar="معرّف المزوّد مطلوب.",
        )

    if provider is None:
        return _failure(
            provider_slug=slug,
            provider_name=slug,
            account_key=account,
            account_display=account_display,
            code="not_found",
            message_ar="المزوّد غير موجود أو غير مهيأ.",
        )

    if not provider.is_active:
        return _failure(
            provider_slug=slug,
            provider_name=provider_name,
            account_key=account,
            account_display=account_display,
            code="auth_config",
            message_ar="المزوّد غير نشط.",
        )

    adapter = str(provider.adapter_type or GOZIBRA_ADAPTER).strip().lower()
    if adapter and adapter not in _SUPPORTED_ADAPTERS:
        return _failure(
            provider_slug=slug,
            provider_name=provider_name,
            account_key=account,
            account_display=account_display,
            code="unsupported",
            message_ar="نوع محوّل المزوّد لا يدعم اكتشاف الكتالوج حالياً.",
            detail=adapter,
        )

    if account_rec is None or not account_rec.is_active:
        return _failure(
            provider_slug=slug,
            provider_name=provider_name,
            account_key=account,
            account_display=account_display,
            code="auth_config",
            message_ar="حساب المزوّد غير موجود أو غير نشط.",
        )

    try:
        raw = await fetch_provider_account_services(
            provider_slug=slug,
            account_key=account,
        )
    except ProviderAuthError as exc:
        logger.warning("Provider discovery auth failed %s/%s", slug, account)
        return _failure(
            provider_slug=slug,
            provider_name=provider_name,
            account_key=account,
            account_display=account_display,
            code="auth_config",
            message_ar="فشل التوثيق لدى المزوّد. تحقق من إعدادات المفتاح.",
            detail=str(exc),
        )
    except ProviderMalformedResponseError as exc:
        logger.warning("Provider discovery malformed %s/%s", slug, account)
        return _failure(
            provider_slug=slug,
            provider_name=provider_name,
            account_key=account,
            account_display=account_display,
            code="malformed",
            message_ar="استجابة المزوّد غير صالحة أو غير مفهومة.",
            detail=str(exc),
        )
    except ProviderUnavailableError as exc:
        msg = str(exc)
        code: DiscoveryErrorCode = "api_failure"
        message_ar = "فشل طلب كتالوج المزوّد."
        if "مهلة" in msg or "timeout" in msg.lower():
            code = "timeout"
            message_ar = "انتهت مهلة الاتصال بمزوّد الخدمة."
        elif "تعذّر الاتصال" in msg or "connect" in msg.lower():
            code = "network"
            message_ar = "تعذّر الاتصال بمزوّد الخدمة."
        logger.warning("Provider discovery API failure %s/%s: %s", slug, account, code)
        return _failure(
            provider_slug=slug,
            provider_name=provider_name,
            account_key=account,
            account_display=account_display,
            code=code,
            message_ar=message_ar,
            detail=msg,
        )
    except RuntimeError as exc:
        raw_msg = str(exc)
        message_ar = "إعدادات حساب المزوّد غير مكتملة."
        if "not set" in raw_msg.lower() or "environment" in raw_msg.lower():
            message_ar = "مفتاح API غير مضبوط في إعدادات البيئة."
        logger.warning("Provider discovery config failure %s/%s", slug, account)
        return _failure(
            provider_slug=slug,
            provider_name=provider_name,
            account_key=account,
            account_display=account_display,
            code="auth_config",
            message_ar=message_ar,
            detail=raw_msg,
        )
    except Exception as exc:
        logger.exception("Provider discovery unexpected failure %s/%s", slug, account)
        return _failure(
            provider_slug=slug,
            provider_name=provider_name,
            account_key=account,
            account_display=account_display,
            code="api_failure",
            message_ar="خطأ غير متوقع أثناء اكتشاف كتالوج المزوّد.",
            detail=str(exc),
        )

    services = normalize_provider_services_list(raw)
    if not services and not _is_empty_catalog_payload(raw, services):
        return _failure(
            provider_slug=slug,
            provider_name=provider_name,
            account_key=account,
            account_display=account_display,
            code="malformed",
            message_ar="تعذّر تفسير قائمة خدمات المزوّد.",
        )

    items: list[ProviderCatalogItem] = []
    skipped = 0
    for entry in services:
        item = normalize_provider_catalog_item(
            entry,
            provider_slug=slug,
            provider_account_key=account,
        )
        if item is None:
            skipped += 1
            continue
        items.append(item)

    # Reject duplicate opaque external IDs — never last-wins silently.
    seen_ids: set[str] = set()
    for item in items:
        eid = item.external_service_id
        if eid in seen_ids:
            return _failure(
                provider_slug=slug,
                provider_name=provider_name,
                account_key=account,
                account_display=account_display,
                code="malformed",
                message_ar=(
                    "استجابة المزود تحتوي على معرّفات خدمة مكررة ولا يمكن اعتمادها."
                ),
            )
        seen_ids.add(eid)

    return ProviderDiscoveryResult(
        provider_slug=slug,
        provider_name=provider_name,
        provider_account_key=account,
        account_display_name=account_display,
        discovered_at=discovered_at,
        ok=True,
        item_count=len(items),
        items=items,
        error=None,
        skipped_without_id=skipped,
    )
