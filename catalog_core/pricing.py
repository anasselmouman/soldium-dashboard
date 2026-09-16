# -*- coding: utf-8 -*-
"""Catalog selling-price helpers (Phase 4B).

Money: 1 DH = 1000 millimes (exact integer minor units).
Zero MAD is rejected as an active selling price — use missing price instead.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from catalog_core.errors import CatalogValidationError

# Internal codes → Arabic labels (independent from ordering_mode).
PRICING_MODE_LABELS_AR: dict[str, str] = {
    "per_1000": "لكل 1000",
    "per_unit": "لكل وحدة",
    "fixed_package": "سعر ثابت للباقة",
}

PRICING_MODE_CODES: frozenset[str] = frozenset(PRICING_MODE_LABELS_AR)

CURRENCY_MAD = "MAD"
SUPPORTED_CURRENCIES: frozenset[str] = frozenset({CURRENCY_MAD})

MILLIMES_PER_DH = 1000
DEFAULT_PRICING_MODE = "per_1000"
DEFAULT_CURRENCY = CURRENCY_MAD


def pricing_mode_label_ar(code: str) -> str:
    return PRICING_MODE_LABELS_AR.get(code, code)


def pricing_meta() -> dict[str, Any]:
    return {
        "pricing_modes": [
            {"code": code, "label_ar": label}
            for code, label in PRICING_MODE_LABELS_AR.items()
        ],
        "currencies": [{"code": CURRENCY_MAD, "label_ar": "درهم"}],
        "money": {
            "currency": CURRENCY_MAD,
            "minor_units_per_major": MILLIMES_PER_DH,
            "minor_unit_name": "millime",
        },
    }


def normalize_pricing_mode(value: str | None) -> str:
    code = str(value or "").strip().lower()
    if not code:
        raise CatalogValidationError("طريقة التسعير مطلوبة")
    if code not in PRICING_MODE_CODES:
        raise CatalogValidationError("طريقة التسعير غير صالحة")
    return code


def normalize_currency(value: str | None) -> str:
    code = str(value or DEFAULT_CURRENCY).strip().upper()
    if code in {"DH", "MAD"}:
        code = CURRENCY_MAD
    if code not in SUPPORTED_CURRENCIES:
        raise CatalogValidationError("العملة غير مدعومة")
    return code


def dh_to_millimes(value: object) -> int:
    """Convert a DH amount string/number to integer millimes without float math.

    Accepts at most 3 decimal places (millime precision). Does not silently
    round fractional millimes — invalid precision is rejected.
    """
    raw = str(value if value is not None else "").strip()
    if not raw:
        raise CatalogValidationError("السعر مطلوب")
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise CatalogValidationError("قيمة السعر غير صالحة") from exc
    if amount.is_nan() or amount.is_infinite():
        raise CatalogValidationError("قيمة السعر غير صالحة")
    if amount < 0:
        raise CatalogValidationError("السعر لا يمكن أن يكون سالباً")

    scaled = amount * Decimal(MILLIMES_PER_DH)
    if scaled != scaled.to_integral_value():
        raise CatalogValidationError(
            "قيمة السعر غير صالحة — استخدم حتى 3 أرقام بعد الفاصلة كحد أقصى"
        )
    millimes = int(scaled)
    if millimes == 0:
        # Explicit product decision: free (0 DH) is not a valid selling price.
        # Administrators leave the service without a price instead.
        raise CatalogValidationError(
            "السعر صفر غير مسموح — اترك الخدمة بدون سعر إن لم تحدد سعراً بعد"
        )
    return millimes


def millimes_to_dh_decimal(millimes: int) -> Decimal:
    return Decimal(int(millimes)) / Decimal(MILLIMES_PER_DH)


def format_dh_amount(millimes: int) -> str:
    """Human-readable DH without unnecessary trailing zeros."""
    d = millimes_to_dh_decimal(int(millimes))
    text = format(d.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_price_display(
    amount_millimes: int,
    pricing_mode: str,
    *,
    currency: str = CURRENCY_MAD,
) -> str:
    amount = format_dh_amount(amount_millimes)
    # Prefer DH label for administrators (MAD == DH commercially).
    unit = "DH"
    if currency and currency.upper() not in {"MAD", "DH"}:
        unit = currency.upper()
    if pricing_mode == "per_1000":
        return f"{amount} {unit} / 1000"
    if pricing_mode == "per_unit":
        return f"{amount} {unit} / وحدة"
    if pricing_mode == "fixed_package":
        return f"{amount} {unit} (ثابت)"
    return f"{amount} {unit}"


def quote_total_millimes(
    amount_millimes: int,
    pricing_mode: str,
    quantity: int,
) -> int:
    """Compute a customer quote in millimes from a published unit price.

    ``per_1000``: ``amount_millimes * quantity / 1000`` (half-up to millime).
    ``per_unit``: ``amount_millimes * quantity``.
    ``fixed_package``: not quantity-quotable — raises ``CatalogValidationError``.
    """
    mode = normalize_pricing_mode(pricing_mode)
    if mode == "fixed_package":
        raise CatalogValidationError(
            "التسعير الثابت للباقة غير مدعوم للتسعير حسب الكمية"
        )
    try:
        qty = int(quantity)
    except (TypeError, ValueError) as exc:
        raise CatalogValidationError("الكمية غير صالحة") from exc
    if qty < 1:
        raise CatalogValidationError("الكمية غير صالحة")

    unit = Decimal(int(amount_millimes))
    if unit <= 0:
        raise CatalogValidationError("السعر غير صالح")

    if mode == "per_unit":
        total = unit * Decimal(qty)
    else:
        # per_1000
        total = (unit * Decimal(qty)) / Decimal(MILLIMES_PER_DH)

    millimes = int(total.to_integral_value(rounding=ROUND_HALF_UP))
    if millimes <= 0:
        raise CatalogValidationError("المبلغ المحسوب غير صالح")
    return millimes
