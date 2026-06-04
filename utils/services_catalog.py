"""Shared services.json read/write and catalog flattening for the dashboard."""
from __future__ import annotations

import copy
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterator

_DIR = Path(__file__).resolve().parent.parent
_BOT_DIR = _DIR.parent / "soldium-bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

from services_catalog_loader import (  # noqa: E402
    SERVICES_JSON_PATH,
    load_services_dict,
    save_services_dict,
)

_READ_RETRIES = 5
_READ_RETRY_DELAY = 0.06


def catalog_path() -> Path:
    return SERVICES_JSON_PATH


def load_catalog() -> dict[str, Any]:
    return copy.deepcopy(load_services_dict())


def save_catalog(data: dict[str, Any]) -> None:
    save_services_dict(data)


def make_item_key(
    platform_key: str,
    section_key: str | None,
    subsection_key: str | None,
    item_id: str,
) -> str:
    return "|".join(
        [
            platform_key,
            section_key or "",
            subsection_key or "",
            str(item_id),
        ],
    )


def parse_item_key(key: str) -> tuple[str, str | None, str | None, str]:
    parts = key.split("|", 3)
    if len(parts) != 4:
        raise ValueError(f"invalid catalog key: {key}")
    platform_key, section_key, subsection_key, item_id = parts
    return (
        platform_key,
        section_key or None,
        subsection_key or None,
        item_id,
    )


def _iter_catalog_items(
    services: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    for platform_key, category in services.items():
        if not isinstance(category, dict):
            continue
        platform_title = str(category.get("title") or platform_key)

        for item in category.get("items") or []:
            if isinstance(item, dict):
                yield _row(platform_key, platform_title, None, None, None, None, item)

        for item in category.get("direct_items") or []:
            if isinstance(item, dict):
                yield _row(
                    platform_key,
                    platform_title,
                    "direct",
                    "مباشر",
                    None,
                    None,
                    item,
                )

        for section_key, section in (category.get("sections") or {}).items():
            if not isinstance(section, dict):
                continue
            section_title = str(section.get("title") or section_key)
            for item in section.get("items") or []:
                if isinstance(item, dict):
                    yield _row(
                        platform_key,
                        platform_title,
                        str(section_key),
                        section_title,
                        None,
                        None,
                        item,
                    )
            for subsection_key, subsection in (section.get("subsections") or {}).items():
                if not isinstance(subsection, dict):
                    continue
                subsection_title = str(subsection.get("title") or subsection_key)
                for item in subsection.get("items") or []:
                    if isinstance(item, dict):
                        yield _row(
                            platform_key,
                            platform_title,
                            str(section_key),
                            section_title,
                            str(subsection_key),
                            subsection_title,
                            item,
                        )


def _row(
    platform_key: str,
    platform_title: str,
    section_key: str | None,
    section_title: str | None,
    subsection_key: str | None,
    subsection_title: str | None,
    item: dict[str, Any],
) -> dict[str, Any]:
    item_id = str(item.get("id") or "")
    provider_id = int(item.get("provider_id") or item_id or 0)
    category_parts = [platform_title]
    if section_title:
        category_parts.append(section_title)
    if subsection_title:
        category_parts.append(subsection_title)
    return {
        "key": make_item_key(platform_key, section_key, subsection_key, item_id),
        "platform_key": platform_key,
        "platform_title": platform_title,
        "section_key": section_key,
        "section_title": section_title,
        "subsection_key": subsection_key,
        "subsection_title": subsection_title,
        "category_label": " › ".join(category_parts),
        "item_id": item_id,
        "provider_id": provider_id,
        "name": str(item.get("name") or ""),
        "price_dh": float(item.get("price") or 0.0),
        "min": int(item.get("min") or 1),
        "max": int(item.get("max") or 0),
        "provider_rate_usd": item.get("provider_rate_usd"),
        "provider_name": item.get("provider_name"),
        "provider_category": item.get("provider_category"),
        "highlight_new": bool(item.get("highlight_new")),
    }


def flatten_catalog(services: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = services if services is not None else load_catalog()
    return list(_iter_catalog_items(data))


def _locate_item_ref(
    services: dict[str, Any],
    platform_key: str,
    section_key: str | None,
    subsection_key: str | None,
    item_id: str,
) -> dict[str, Any] | None:
    category = services.get(platform_key)
    if not isinstance(category, dict):
        return None

    if section_key is None:
        for item in category.get("items") or []:
            if str(item.get("id")) == item_id:
                return item
        return None

    if section_key == "direct":
        for item in category.get("direct_items") or []:
            if str(item.get("id")) == item_id:
                return item
        return None

    section = (category.get("sections") or {}).get(section_key)
    if not isinstance(section, dict):
        return None

    if subsection_key is None:
        for item in section.get("items") or []:
            if str(item.get("id")) == item_id:
                return item
        return None

    subsection = (section.get("subsections") or {}).get(subsection_key)
    if not isinstance(subsection, dict):
        return None
    for item in subsection.get("items") or []:
        if str(item.get("id")) == item_id:
            return item
    return None


def apply_item_updates(
    services: dict[str, Any],
    updates: list[dict[str, Any]],
) -> int:
    changed = 0
    for upd in updates:
        key = str(upd.get("key") or "")
        if not key:
            continue
        platform_key, section_key, subsection_key, item_id = parse_item_key(key)
        item = _locate_item_ref(services, platform_key, section_key, subsection_key, item_id)
        if item is None:
            continue
        if "name" in upd and upd["name"] is not None:
            item["name"] = str(upd["name"])
        if "price" in upd and upd["price"] is not None:
            item["price"] = float(upd["price"])
        changed += 1
    return changed


def merge_provider_snapshot(
    services: dict[str, Any],
    provider_services: list[dict[str, Any]],
) -> dict[str, Any]:
    """يربط أسعار المزوّد بالخدمات المحلية ويُبرز الجديدة غير المربوطة."""
    by_provider: dict[int, dict[str, Any]] = {}
    for entry in provider_services:
        if not isinstance(entry, dict):
            continue
        try:
            pid = int(entry.get("service") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        try:
            rate = float(entry.get("rate") or 0)
        except (TypeError, ValueError):
            rate = 0.0
        by_provider[pid] = {
            "rate_usd": rate,
            "name": str(entry.get("name") or ""),
            "category": str(entry.get("category") or ""),
            "min": entry.get("min"),
            "max": entry.get("max"),
        }

    local_provider_ids: set[int] = set()
    for row in _iter_catalog_items(services):
        pid = int(row["provider_id"])
        local_provider_ids.add(pid)
        item = _locate_item_ref(
            services,
            row["platform_key"],
            row["section_key"],
            row["subsection_key"],
            row["item_id"],
        )
        if item is None:
            continue
        prov = by_provider.get(pid)
        if prov:
            item["provider_rate_usd"] = prov["rate_usd"]
            item["provider_name"] = prov["name"]
            item["provider_category"] = prov["category"]
            item["highlight_new"] = False
        else:
            item.pop("provider_rate_usd", None)

    provider_only: list[dict[str, Any]] = []
    for pid, prov in sorted(by_provider.items()):
        if pid not in local_provider_ids:
            provider_only.append(
                {
                    "provider_id": pid,
                    "provider_rate_usd": prov["rate_usd"],
                    "name": prov["name"],
                    "category": prov["category"],
                    "min": prov.get("min"),
                    "max": prov.get("max"),
                    "highlight_new": True,
                },
            )

    return {
        "matched_provider_count": len(local_provider_ids & set(by_provider.keys())),
        "provider_only": provider_only,
        "total_provider_services": len(by_provider),
    }
