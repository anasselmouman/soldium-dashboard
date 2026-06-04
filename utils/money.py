"""Money helpers aligned with soldium-bot/utils/money.py."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

MONEY_STEP = Decimal("0.000001")


def to_decimal(value: object) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_STEP, rounding=ROUND_HALF_UP)


def to_float(value: object) -> float:
    return float(to_decimal(value))
