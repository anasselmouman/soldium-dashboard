# -*- coding: utf-8 -*-
"""حساب تكلفة المورد بالدرهم — يطابق soldium-bot/utils/order_economics.py"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from settings import SERVICE_USD_TO_DH_MULTIPLIER

DEFAULT_MARKUP_FALLBACK = 15.0
_MONEY_STEP = Decimal("0.01")


def _to_decimal(value: object) -> Decimal:
    return Decimal(str(value))


def compute_provider_cost_dh(
    quantity: int,
    *,
    provider_price_usd: float,
    local_price_dh: float = 0.0,
    price_per_unit: bool = False,
    usd_to_dh: float | None = None,
    markup_fallback: float = DEFAULT_MARKUP_FALLBACK,
) -> float:
    mult = _to_decimal(usd_to_dh if usd_to_dh is not None else SERVICE_USD_TO_DH_MULTIPLIER)
    qty = _to_decimal(max(int(quantity), 0))
    rate = _to_decimal(provider_price_usd)
    local = _to_decimal(local_price_dh)
    markup = _to_decimal(markup_fallback)
    if markup <= 0:
        markup = _to_decimal(DEFAULT_MARKUP_FALLBACK)

    if price_per_unit:
        if rate > 0:
            cost = qty * rate * mult
        elif local > 0:
            cost = qty * local * (mult / markup)
        else:
            cost = Decimal(0)
    else:
        thousand = Decimal(1000)
        if rate > 0:
            cost = (qty / thousand) * rate * mult
        elif local > 0:
            cost = (qty / thousand) * (local / markup) * mult
        else:
            cost = Decimal(0)

    return float(cost.quantize(_MONEY_STEP, rounding=ROUND_HALF_UP))


def catalog_margin_dh(
    *,
    provider_price_usd: float,
    local_price_dh: float,
    price_per_unit: bool,
    quantity: int = 1000,
) -> float | None:
    """هامش الربح بالدرهم لكمية مرجعية (1000 أو وحدة واحدة لـ per_unit)."""
    ref_qty = 1 if price_per_unit else 1000
    cost = compute_provider_cost_dh(
        ref_qty,
        provider_price_usd=provider_price_usd,
        local_price_dh=local_price_dh,
        price_per_unit=price_per_unit,
    )
    if cost <= 0 and local_price_dh <= 0:
        return None
    if price_per_unit:
        retail = local_price_dh if local_price_dh > 0 else 0.0
    else:
        retail = local_price_dh
    if retail <= 0:
        return None
    return round(retail - cost, 2)
