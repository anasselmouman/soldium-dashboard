"""Tests for order alert message formatting."""
from __future__ import annotations

from utils.order_alert_format import format_order_alert_messages


class _FakeRow:
    def __init__(self, data: dict) -> None:
        self._data = data

    def __getitem__(self, key: str):
        return self._data[key]

    def keys(self):
        return self._data.keys()


def test_format_includes_copyable_refs_platform_provider() -> None:
    row = _FakeRow(
        {
            "id": 42,
            "user_id": 1,
            "provider_order_id": "PO-999",
            "service_name": "متابعين",
            "platform_title": "إنستغرام",
            "platform_key": "instagram",
            "provider_name": "Gozibra",
            "provider_slug": "gozibra",
            "api_account": "instagram",
        }
    )
    plain, html = format_order_alert_messages(row, headline="طلب عالق")
    assert "PO-999" in plain
    assert "42" in plain
    assert "إنستغرام" in plain
    assert "gozibra" in plain
    assert "<code>PO-999</code>" in html
    assert "<code>42</code>" in html
    assert "instagram" in html or "إنستغرام" in html
