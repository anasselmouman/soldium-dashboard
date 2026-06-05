"""Customer-facing order reference (distributor order id only)."""
from __future__ import annotations

import html


def display_order_ref(provider_order_id: str | None) -> str:
    ref = str(provider_order_id or "").strip()
    return ref or "—"


def display_order_ref_html(provider_order_id: str | None) -> str:
    return html.escape(display_order_ref(provider_order_id))
