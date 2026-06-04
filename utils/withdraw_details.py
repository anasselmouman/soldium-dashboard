"""Parse and format withdrawal details_json for the admin dashboard."""
from __future__ import annotations

import json
from typing import Any

_DETAIL_LABELS: dict[str, str] = {
    "name": "الاسم الكامل",
    "account": "رقم الحساب / RIB",
    "phone": "رقم الهاتف",
    "destination": "وجهة الاستلام",
    "email": "إيميل PayPal",
    "details": "المعلومات",
    "crypto_network_label": "شبكة USDT",
    "network_fee_usdt": "رسوم الشبكة (USDT)",
    "payout_type": "نوع الاستلام",
    "bank_name": "اسم البنك",
    "rib": "RIB",
}

_DETAIL_ORDER = (
    "bank_name",
    "rib",
    "name",
    "account",
    "crypto_network_label",
    "network_fee_usdt",
    "payout_type",
    "destination",
    "phone",
    "email",
    "details",
)

_SKIP_KEYS = frozenset({"crypto_network_key", "reference_deposit_address", "payout_type"})


def safe_withdraw_details(raw: object) -> dict[str, str]:
    try:
        data = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def is_crypto_withdraw_details(details: dict[str, str]) -> bool:
    return bool(
        str(details.get("crypto_network_key", "") or "").strip()
        or str(details.get("crypto_network_label", "") or "").strip()
    )


def format_details_lines(details: dict[str, str]) -> list[dict[str, str]]:
    """Structured lines for API/UI: label + value."""
    if not details:
        return []

    lines: list[dict[str, str]] = []
    seen: set[str] = set()

    for key in _DETAIL_ORDER:
        value = str(details.get(key, "") or "").strip()
        if not value or key in _SKIP_KEYS:
            continue
        seen.add(key)
        lines.append({"key": key, "label": _DETAIL_LABELS.get(key, key), "value": value})

    for key, value in details.items():
        if key in seen or key in _SKIP_KEYS:
            continue
        value = str(value or "").strip()
        if not value:
            continue
        lines.append({"key": key, "label": _DETAIL_LABELS.get(key, key), "value": value})

    if is_crypto_withdraw_details(details) and not any(
        line["key"] == "destination" for line in lines
    ):
        dest = str(details.get("destination", "") or "").strip()
        if dest:
            payout = str(details.get("payout_type", "") or "").strip()
            label = "Pay ID (Binance)" if payout == "binance_pay" else "عنوان المحفظة"
            lines.append({"key": "destination", "label": label, "value": dest})

    return lines


def withdrawal_type_label(withdrawal_type: str) -> str:
    normalized = (withdrawal_type or "normal").strip().lower()
    if normalized == "referral":
        return "أرباح إحالة"
    return "رصيد عادي"
