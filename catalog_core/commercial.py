# -*- coding: utf-8 -*-
"""Commercial profile constants and validation (Phase 4A — no pricing)."""

from __future__ import annotations

from typing import Any

from catalog_core.errors import CatalogValidationError

# Internal codes → Arabic labels (UI/API display). Independent of Catalog tree.
SERVICE_TYPE_LABELS_AR: dict[str, str] = {
    "followers": "متابعون",
    "likes": "إعجابات",
    "views": "مشاهدات",
    "comments": "تعليقات",
    "shares": "مشاركات",
    "saves": "حفظ",
    "members": "أعضاء",
    "live_viewers": "مشاهدو البث",
    "other": "أخرى",
}

SERVICE_TYPE_CODES: frozenset[str] = frozenset(SERVICE_TYPE_LABELS_AR)

# How the customer orders — NOT provider fulfillment_mode (auto/admin).
ORDERING_MODE_LABELS_AR: dict[str, str] = {
    "quantity_based": "حسب الكمية",
    "package_based": "حسب الباقة",
}

ORDERING_MODE_CODES: frozenset[str] = frozenset(ORDERING_MODE_LABELS_AR)

# Order/fulfillment routing — NOT provider execution identity.
FULFILLMENT_MODE_LABELS_AR: dict[str, str] = {
    "auto": "تلقائي",
    "admin": "يدوي / أدمن",
}
FULFILLMENT_MODE_CODES: frozenset[str] = frozenset(FULFILLMENT_MODE_LABELS_AR)

# Structural storefront platforms (shared with target_validation host rules).
TARGET_PLATFORM_LABELS_AR: dict[str, str] = {
    "instagram": "إنستغرام",
    "facebook": "فيسبوك",
    "tiktok": "تيك توك",
    "youtube": "يوتيوب",
    "telegram": "تيليجرام",
    "x": "X (تويتر)",
    "subscriptions": "اشتراكات",
}

# Optional authored link semantics (Phase 9N comment rules use link_type=comment).
TARGET_LINK_TYPE_LABELS_AR: dict[str, str] = {
    "comment": "تعليق",
    "post": "منشور",
}

TARGET_LINK_PROMPT_LABELS_AR: dict[str, str] = {
    "x_live_broadcast": "بث مباشر X",
    "x_direct_messages": "رسائل مباشرة X",
    "x_spaces": "مساحات X",
}

DEFAULT_SERVICE_TYPE = "other"
DEFAULT_ORDERING_MODE = "quantity_based"
DEFAULT_FULFILLMENT_MODE = "auto"
DEFAULT_MIN_QUANTITY = 1
DEFAULT_MAX_QUANTITY = 1_000_000


def service_type_label_ar(code: str) -> str:
    return SERVICE_TYPE_LABELS_AR.get(code, code)


def ordering_mode_label_ar(code: str) -> str:
    return ORDERING_MODE_LABELS_AR.get(code, code)


def fulfillment_mode_label_ar(code: str) -> str:
    return FULFILLMENT_MODE_LABELS_AR.get(code, code)


def target_platform_label_ar(code: str) -> str:
    return TARGET_PLATFORM_LABELS_AR.get(code, code)


def commercial_meta() -> dict[str, Any]:
    """Options for UI / API (codes + Arabic labels)."""
    return {
        "service_types": [
            {"code": code, "label_ar": label}
            for code, label in SERVICE_TYPE_LABELS_AR.items()
        ],
        "ordering_modes": [
            {"code": code, "label_ar": label}
            for code, label in ORDERING_MODE_LABELS_AR.items()
        ],
        "fulfillment_modes": [
            {"code": code, "label_ar": label}
            for code, label in FULFILLMENT_MODE_LABELS_AR.items()
        ],
        "target_platforms": [
            {"code": code, "label_ar": label}
            for code, label in TARGET_PLATFORM_LABELS_AR.items()
        ],
        "target_link_types": [
            {"code": code, "label_ar": label}
            for code, label in TARGET_LINK_TYPE_LABELS_AR.items()
        ],
        "target_link_prompt_keys": [
            {"code": code, "label_ar": label}
            for code, label in TARGET_LINK_PROMPT_LABELS_AR.items()
        ],
    }


def normalize_service_type(value: str | None) -> str:
    code = str(value or "").strip().lower()
    if not code:
        return DEFAULT_SERVICE_TYPE
    if code not in SERVICE_TYPE_CODES:
        raise CatalogValidationError("نوع الخدمة غير صالح")
    return code


def normalize_ordering_mode(value: str | None) -> str:
    code = str(value or "").strip().lower()
    if not code:
        return DEFAULT_ORDERING_MODE
    if code not in ORDERING_MODE_CODES:
        raise CatalogValidationError("طريقة الطلب غير صالحة")
    return code


def normalize_fulfillment_mode(value: str | None) -> str:
    """Require explicit auto|admin. Empty → default auto; unknown → error."""
    if value is None or str(value).strip() == "":
        return DEFAULT_FULFILLMENT_MODE
    code = str(value).strip().lower()
    if code not in FULFILLMENT_MODE_CODES:
        raise CatalogValidationError("وضع التنفيذ غير صالح")
    return code


def normalize_optional_key(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def normalize_quantity(value: Any, *, field_label: str) -> int:
    if value is None or value == "":
        raise CatalogValidationError(f"{field_label} مطلوب")
    try:
        # Accept int-like strings; reject floats / bools as quantities.
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError
            qty = int(value)
        else:
            qty = int(str(value).strip())
    except (TypeError, ValueError):
        raise CatalogValidationError(f"{field_label} يجب أن يكون عدداً صحيحاً") from None
    if qty < 0:
        raise CatalogValidationError(f"{field_label} لا يمكن أن يكون سالباً")
    return qty


def validate_commercial_profile(
    *,
    name_ar: str,
    service_type: str,
    ordering_mode: str,
    min_quantity: int,
    max_quantity: int,
    fulfillment_mode: str | None = None,
) -> None:
    if not (name_ar or "").strip():
        raise CatalogValidationError("اسم الخدمة مطلوب")
    if service_type not in SERVICE_TYPE_CODES:
        raise CatalogValidationError("نوع الخدمة غير صالح")
    if ordering_mode not in ORDERING_MODE_CODES:
        raise CatalogValidationError("طريقة الطلب غير صالحة")
    if fulfillment_mode is not None and fulfillment_mode not in FULFILLMENT_MODE_CODES:
        raise CatalogValidationError("وضع التنفيذ غير صالح")
    if min_quantity < 0:
        raise CatalogValidationError("الحد الأدنى لا يمكن أن يكون سالباً")
    if max_quantity < 0:
        raise CatalogValidationError("الحد الأقصى لا يمكن أن يكون سالباً")
    if ordering_mode == "quantity_based":
        # Required for quantity-based ordering.
        if min_quantity < 0 or max_quantity < 0:
            raise CatalogValidationError("حدود الكمية مطلوبة لطلب حسب الكمية")
    if min_quantity > max_quantity:
        raise CatalogValidationError("الحد الأدنى لا يمكن أن يتجاوز الحد الأقصى")


def validate_target_policy_for_publish(
    *,
    platform_key: str | None,
    section_key: str | None,
) -> None:
    """Publish requires structural target keys (not Arabic labels)."""
    if not (platform_key or "").strip():
        raise CatalogValidationError("مفتاح المنصة للهدف مطلوب قبل النشر")
    if not (section_key or "").strip():
        raise CatalogValidationError("مفتاح القسم للهدف مطلوب قبل النشر")
