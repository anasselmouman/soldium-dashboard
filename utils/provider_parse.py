"""Parse PerfectPanel / SMM API v2 service payloads (Gozibra, etc.)."""
from __future__ import annotations

import re
from typing import Any

_RATE_KEYS = (
    "rate",
    "price",
    "cost",
    "rate_per_1000",
    "provider_rate",
    "charge",
)
_SERVICE_ID_KEYS = ("service", "service_id", "id")
_LIST_WRAPPER_KEYS = ("services", "data", "result", "items", "list")


def _case_insensitive_get(entry: dict[str, Any], *keys: str) -> Any:
    if not isinstance(entry, dict):
        return None
    lowered = {
        str(k).lower(): v
        for k, v in entry.items()
        if isinstance(k, str)
    }
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def parse_provider_rate(entry: dict[str, Any]) -> float | None:
    """
  Extract USD (or provider currency) cost per 1000 from a service object.
  Returns None if no usable price field is present.
    """
    if not isinstance(entry, dict):
        return None

    for key in _RATE_KEYS:
        raw = _case_insensitive_get(entry, key)
        if raw is None or raw == "":
            continue
        parsed = parse_float_loose(raw)
        if parsed is not None:
            return parsed
    return None


def parse_float_loose(value: object) -> float | None:
    """Cast provider numbers: strings, commas, currency symbols."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    # European decimal comma when no dot present: "0,50" -> 0.50
    if "," in text and "." not in text:
        text = text.replace(",", ".")

    # Strip currency words/symbols; keep digits, dot, minus
    cleaned = re.sub(r"[^\d.\-]", "", text.replace(" ", ""))
    if not cleaned or cleaned in {".", "-", "-."}:
        return None

    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_provider_service_id(entry: dict[str, Any]) -> int | None:
    if not isinstance(entry, dict):
        return None
    raw = _case_insensitive_get(entry, *_SERVICE_ID_KEYS)
    if raw is None or raw == "":
        return None
    try:
        pid = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def normalize_provider_services_list(data: Any) -> list[dict[str, Any]]:
    """Accept raw API JSON (list or wrapped dict) and return service dicts."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    if isinstance(data, dict):
        if data.get("error"):
            return []
        for key in _LIST_WRAPPER_KEYS:
            nested = data.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        # Single service object
        if parse_provider_service_id(data) is not None:
            return [data]

    return []
