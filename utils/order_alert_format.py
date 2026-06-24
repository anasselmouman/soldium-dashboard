"""تنسيق رسائل تحذيرات الطلبات للأدمن (نسخ الرقم + منصة + مزوّد)."""
from __future__ import annotations

import html
from typing import Any

PLATFORM_LABELS: dict[str, str] = {
    "instagram": "إنستغرام",
    "tiktok": "تيك توك",
    "facebook": "فيسبوك",
    "youtube": "يوتيوب",
    "telegram": "تيليغرام",
    "twitter": "تويتر",
    "snapchat": "سناب شات",
    "subscriptions": "اشتراكات",
}

ORDER_ALERT_CATALOG_JOIN = """
LEFT JOIN smm_services s ON (
    s.service_id = o.service_id
    OR s.catalog_id = o.service_id
    OR s.local_item_id = o.service_id
)
LEFT JOIN providers p ON LOWER(p.slug) = LOWER(
    COALESCE(NULLIF(TRIM(o.provider_slug), ''), NULLIF(TRIM(s.provider_slug), ''), 'gozibra')
)
"""

ORDER_ALERT_SELECT_EXTRA = """
    s.platform_title,
    s.platform_key,
    p.name AS provider_name
"""


def platform_label(platform_title: str | None, platform_key: str | None) -> str:
    title = str(platform_title or "").strip()
    if title:
        return title
    key = str(platform_key or "").strip().lower()
    if not key:
        return "—"
    return PLATFORM_LABELS.get(key, key)


def provider_label(
    provider_name: str | None,
    provider_slug: str | None,
    api_account: str | None = None,
) -> str:
    name = str(provider_name or "").strip()
    slug = str(provider_slug or "").strip().lower()
    account = str(api_account or "").strip().lower()
    if name and slug:
        base = f"{name} ({slug})"
    elif name:
        base = name
    elif slug:
        base = slug
    else:
        base = "—"
    if account and account not in {"", "default"}:
        return f"{base} · حساب {account}"
    return base


def copyable_refs_plain(provider_order_id: str | None, order_id: int) -> str:
    provider_ref = str(provider_order_id or "").strip()
    parts: list[str] = []
    if provider_ref:
        parts.append(f"مرجع المزوّد: {provider_ref}")
    parts.append(f"المعرّف الداخلي: {order_id}")
    return " · ".join(parts)


def copyable_refs_html(provider_order_id: str | None, order_id: int) -> str:
    provider_ref = str(provider_order_id or "").strip()
    parts: list[str] = []
    if provider_ref:
        parts.append(f'مرجع المزوّد: <code>{html.escape(provider_ref)}</code>')
    parts.append(f'المعرّف الداخلي: <code>{order_id}</code>')
    return "\n".join(parts)


def catalog_fields_from_row(row: Any) -> tuple[str, str, str]:
    """استخراج منصة/مزوّد من صف استعلام مع JOIN."""
    keys = row.keys() if hasattr(row, "keys") else ()
    platform_title = str(row["platform_title"]) if "platform_title" in keys else None
    platform_key = str(row["platform_key"]) if "platform_key" in keys else None
    provider_name = str(row["provider_name"]) if "provider_name" in keys else None
    provider_slug = str(row["provider_slug"]) if "provider_slug" in keys else None
    api_account = str(row["api_account"]) if "api_account" in keys else None
    platform = platform_label(platform_title, platform_key)
    provider = provider_label(provider_name, provider_slug, api_account)
    return platform, provider, provider_slug or ""


def format_order_alert_messages(
    row: Any,
    *,
    headline: str,
    extra_plain: list[str] | None = None,
    extra_html: list[str] | None = None,
) -> tuple[str, str]:
    """يعيد (نص عادي، HTML) لرسالة تحذير طلب."""
    order_id = int(row["id"])
    provider_order_id = row["provider_order_id"]
    service_name = str(row["service_name"] or "—")
    platform, provider, _slug = catalog_fields_from_row(row)

    plain_lines = [
        headline,
        copyable_refs_plain(provider_order_id, order_id),
        f"المنصة: {platform}",
        f"المزوّد: {provider}",
        f"الخدمة: {service_name}",
    ]
    html_lines = [
        f"<b>{html.escape(headline)}</b>",
        copyable_refs_html(provider_order_id, order_id),
        f"المنصة: <b>{html.escape(platform)}</b>",
        f"المزوّد: <b>{html.escape(provider)}</b>",
        f"الخدمة: <b>{html.escape(service_name)}</b>",
    ]
    if extra_plain:
        plain_lines.extend(extra_plain)
    if extra_html:
        html_lines.extend(extra_html)
    return "\n".join(plain_lines), "\n".join(html_lines)
