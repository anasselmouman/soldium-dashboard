"""Ledger naming aligned with SOLDUIM/utils/payment_banks.py."""
from __future__ import annotations

CRYPTO_LEDGER_NAME = "Binance/Crypto"
PAYPAL_LEDGER_NAME = "PayPal"


def is_crypto_ledger(method_name: str) -> bool:
    name = method_name.strip()
    if name == CRYPTO_LEDGER_NAME:
        return True
    return name.startswith("USDT —")


def is_paypal_ledger(method_name: str) -> bool:
    return method_name.strip() == PAYPAL_LEDGER_NAME


def ledger_method_for_approved_deposit(deposit_method: str) -> str:
    """Same mapping as handlers/payment._ledger_method_for_approved_deposit."""
    if is_paypal_ledger(deposit_method):
        return PAYPAL_LEDGER_NAME
    if is_crypto_ledger(deposit_method):
        return CRYPTO_LEDGER_NAME
    return deposit_method
