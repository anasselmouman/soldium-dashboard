"""Amount validation aligned with SOLDUIM/handlers/payment.py."""
from __future__ import annotations

import settings
from utils.deposit_ledger import is_crypto_ledger, is_paypal_ledger
from utils.messages_ar import amount_below_min, amount_exceeds_max
from utils.money import to_float


def validate_admin_deposit_amount(deposit_method: str, amount: float) -> str | None:
    """Return an error message, or None if the amount is valid."""
    if amount > to_float(settings.MAX_SINGLE_DEPOSIT_DH):
        return amount_exceeds_max(to_float(settings.MAX_SINGLE_DEPOSIT_DH))
    min_dh = _min_deposit_dh_for_method(deposit_method)
    if amount < min_dh:
        return amount_below_min(min_dh)
    return None


def _min_deposit_dh_for_method(deposit_method: str) -> float:
    if is_paypal_ledger(deposit_method):
        return to_float(settings.MIN_PAYPAL_DEPOSIT_USD) * to_float(settings.USDT_TO_DH_RATE)
    if is_crypto_ledger(deposit_method):
        return to_float(settings.MIN_CRYPTO_DEPOSIT_USDT) * to_float(settings.USDT_TO_DH_RATE)
    return to_float(settings.MIN_DEPOSIT_DH)
