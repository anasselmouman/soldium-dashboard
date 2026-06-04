"""Order status normalization (aligned with soldium-bot/utils/order_status_ar.py)."""
from __future__ import annotations


def normalize_order_status_key(raw_status: object) -> str:
    status = str(raw_status or "").strip().lower().replace("_", " ")
    aliases = {
        "cancelled": "canceled",
        "ملغي": "canceled",
        "جزئي": "partial",
    }
    return aliases.get(status, status)


def status_label_ar(status_key: str) -> str:
    """Arabic label for dashboard display."""
    mapping = {
        "submitted": "قيد الانتظار",
        "pending": "قيد الانتظار",
        "in progress": "قيد التنفيذ",
        "processing": "قيد التنفيذ",
        "completed": "مكتمل",
        "canceled": "ملغي",
        "partial": "مكتمل جزئياً",
        "refunded": "مسترد",
        "failed": "فشل التنفيذ",
    }
    return mapping.get(status_key, "قيد المعالجة")
