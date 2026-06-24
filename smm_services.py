"""SMM service catalog — SQLite smm_services table (shared with the bot)."""
from __future__ import annotations

import logging
from typing import Any

from database_connector import db_transaction, get_db
from services.provider_registry import get_default_provider_slug
from settings import SERVICE_USD_TO_DH_MULTIPLIER
from utils.order_economics import catalog_margin_dh
from utils.provider_parse import parse_provider_rate, parse_provider_service_id

logger = logging.getLogger("soldium.services")

DEFAULT_MARKUP_MULTIPLIER = 15.0

# Same order as the bot platform menu (keyboards/orders.py).
PLATFORM_ORDER: tuple[str, ...] = (
    "instagram",
    "facebook",
    "tiktok",
    "youtube",
    "telegram",
    "x",
    "subscriptions",
)


def _platform_order_sql(alias: str = "platform_key") -> str:
    cases = " ".join(
        f"WHEN '{key}' THEN {index}" for index, key in enumerate(PLATFORM_ORDER, start=1)
    )
    return f"CASE {alias} {cases} ELSE 99 END"


def _location_label(row: Any) -> str:
    parts: list[str] = []
    platform_title = str(row["platform_title"] or row["platform_key"] or "").strip()
    if platform_title:
        parts.append(platform_title)
    section_title = row["section_title"]
    if section_title and str(section_title).strip():
        parts.append(str(section_title).strip())
    subsection_title = row["subsection_title"]
    if subsection_title and str(subsection_title).strip():
        parts.append(str(subsection_title).strip())
    if parts:
        return " › ".join(parts)
    category = str(row["category"] or "").strip()
    return category or "غير مُصنَّف"


def _row_keys(row: Any) -> set[str]:
    try:
        return set(row.keys())
    except Exception:
        return set()


def _row_to_dict(row: Any) -> dict[str, Any]:
    keys = _row_keys(row)
    category = str(row["category"] or "")
    provider_price_usd = float(row["provider_price_usd"] or 0)
    local_price_dh = float(row["local_price_dh"] or 0)
    price_per_unit = category == "per_unit"
    bot_display_price_dh = local_price_dh
    if price_per_unit:
        if local_price_dh > 0:
            bot_display_price_dh = local_price_dh
        elif provider_price_usd > 0:
            bot_display_price_dh = round(
                provider_price_usd * SERVICE_USD_TO_DH_MULTIPLIER,
                2,
            )
    margin_dh = catalog_margin_dh(
        provider_price_usd=provider_price_usd,
        local_price_dh=local_price_dh,
        price_per_unit=price_per_unit,
    )

    service_id = str(row["service_id"])
    catalog_id = str(row["catalog_id"]) if "catalog_id" in keys else service_id
    external_service_id = (
        str(row["external_service_id"])
        if "external_service_id" in keys and row["external_service_id"]
        else service_id
    )
    provider_slug = (
        str(row["provider_slug"]).strip().lower()
        if "provider_slug" in keys and row["provider_slug"]
        else get_default_provider_slug()
    )
    provider_api_account = (
        str(row["provider_api_account"] or "")
        if "provider_api_account" in keys
        else ""
    )

    return {
        "catalog_id": catalog_id,
        "service_id": service_id,
        "external_service_id": external_service_id,
        "provider_slug": provider_slug,
        "provider_api_account": provider_api_account,
        "category": category,
        "name_ar": str(row["name_ar"] or ""),
        "provider_price_usd": provider_price_usd,
        "local_price_dh": local_price_dh,
        "bot_display_price_dh": bot_display_price_dh,
        "margin_dh": margin_dh,
        "price_per_unit": price_per_unit,
        "min_qty": int(row["min_qty"] or 1),
        "max_qty": int(row["max_qty"] or 0),
        "is_active": bool(int(row["is_active"] or 0)),
        "platform_key": str(row["platform_key"] or ""),
        "section_key": row["section_key"],
        "subsection_key": row["subsection_key"],
        "local_item_id": str(row["local_item_id"] or ""),
        "platform_title": str(row["platform_title"] or ""),
        "section_title": row["section_title"],
        "subsection_title": row["subsection_title"],
        "location_label": _location_label(row),
    }


async def list_services(*, include_inactive: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM smm_services"
    if not include_inactive:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY platform_key, section_key, subsection_key, name_ar"
    async with get_db() as db:
        async with db.execute(sql) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


def _build_filters(
    *,
    category: str | None,
    platform: str | None,
    provider: str | None,
    search: str | None,
    bot_only: bool,
) -> tuple[str, list[Any]]:
    """Return SQL WHERE fragment (leading space + WHERE) and bound params."""
    clauses: list[str] = []
    params: list[Any] = []

    if bot_only:
        clauses.append("platform_key != ''")
        clauses.append("is_active = 1")

    if provider and provider.strip():
        clauses.append("provider_slug = ?")
        params.append(provider.strip().lower())

    if platform and platform.strip():
        if platform.strip() == "__unassigned__":
            clauses.append("(platform_key IS NULL OR platform_key = '')")
        else:
            clauses.append("platform_key = ?")
            params.append(platform.strip())

    if category and category.strip():
        clauses.append("category = ?")
        params.append(category.strip())

    if search and search.strip():
        raw = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        term = f"%{raw}%"
        clauses.append(
            "(name_ar LIKE ? ESCAPE '\\' OR service_id LIKE ? ESCAPE '\\' "
            "OR local_item_id LIKE ? ESCAPE '\\' OR catalog_id LIKE ? ESCAPE '\\' "
            "OR external_service_id LIKE ? ESCAPE '\\' OR provider_slug LIKE ? ESCAPE '\\')",
        )
        params.extend([term, term, term, term, term, term])

    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), params


async def list_categories() -> list[str]:
    async with get_db() as db:
        async with db.execute(
            """
            SELECT DISTINCT category
            FROM smm_services
            WHERE category IS NOT NULL AND TRIM(category) != ''
            ORDER BY category COLLATE NOCASE
            """,
        ) as cursor:
            rows = await cursor.fetchall()
    return [str(row[0]) for row in rows]


async def list_platforms(*, bot_only: bool = False) -> list[dict[str, Any]]:
    where_sql, params = _build_filters(
        category=None,
        platform=None,
        provider=None,
        search=None,
        bot_only=bot_only,
    )
    order_sql = _platform_order_sql("platform_key")
    async with get_db() as db:
        async with db.execute(
            f"""
            SELECT
                platform_key,
                MAX(platform_title) AS platform_title,
                COUNT(*) AS service_count
            FROM smm_services
            {where_sql}
            GROUP BY platform_key
            ORDER BY {order_sql}, platform_key COLLATE NOCASE
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {
            "platform_key": str(row["platform_key"] or ""),
            "platform_title": str(
                row["platform_title"] or row["platform_key"] or "غير مُعيَّنة في البوت",
            ),
            "service_count": int(row["service_count"] or 0),
        }
        for row in rows
    ]


async def list_services_paginated(
    *,
    category: str | None = None,
    platform: str | None = None,
    provider: str | None = None,
    search: str | None = None,
    page: int = 1,
    limit: int = 50,
    bot_only: bool = False,
) -> dict[str, Any]:
    page = max(1, int(page))
    limit = min(max(1, int(limit)), 200)
    offset = (page - 1) * limit

    where_sql, params = _build_filters(
        category=category,
        platform=platform,
        provider=provider,
        search=search,
        bot_only=bot_only,
    )
    order_sql = _platform_order_sql("platform_key")

    async with get_db() as db:
        count_sql = f"""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active_count,
                COUNT(DISTINCT platform_key) AS platform_count
            FROM smm_services
            {where_sql}
        """
        async with db.execute(count_sql, params) as cursor:
            count_row = await cursor.fetchone()

        total_items = int(count_row[0]) if count_row else 0
        active_count = int(count_row[1]) if count_row else 0
        platform_count = int(count_row[2]) if count_row else 0
        pending_count = total_items - active_count

        total_pages = max(1, (total_items + limit - 1) // limit) if total_items else 1
        if page > total_pages:
            page = total_pages
            offset = (page - 1) * limit

        select_sql = f"""
            SELECT *
            FROM smm_services
            {where_sql}
            ORDER BY
                {order_sql},
                section_key COLLATE NOCASE,
                subsection_key COLLATE NOCASE,
                is_active DESC,
                name_ar COLLATE NOCASE
            LIMIT ? OFFSET ?
        """
        async with db.execute(select_sql, [*params, limit, offset]) as cursor:
            rows = await cursor.fetchall()

    return {
        "services": [_row_to_dict(r) for r in rows],
        "total_pages": total_pages,
        "current_page": page,
        "total_items": total_items,
        "active_count": active_count,
        "pending_count": pending_count,
        "platform_count": platform_count,
        "limit": limit,
        "bot_only": bot_only,
    }


async def _fetch_service_row(db, identifier: str) -> Any | None:
    ident = str(identifier).strip()
    if not ident:
        return None
    queries = (
        "SELECT * FROM smm_services WHERE catalog_id = ?",
        "SELECT * FROM smm_services WHERE service_id = ?",
        "SELECT * FROM smm_services WHERE local_item_id = ?",
    )
    for sql in queries:
        async with db.execute(sql, (ident,)) as cursor:
            row = await cursor.fetchone()
        if row is not None:
            return row
    return None


async def get_service(service_id: str) -> dict[str, Any] | None:
    async with get_db() as db:
        row = await _fetch_service_row(db, service_id)
    return _row_to_dict(row) if row else None


async def update_service(
    service_id: str,
    *,
    name_ar: str | None = None,
    local_price_dh: float | None = None,
    is_active: bool | None = None,
) -> bool:
    fields: list[str] = []
    params: list[Any] = []
    if name_ar is not None:
        fields.append("name_ar = ?")
        params.append(str(name_ar)[:500])
    if local_price_dh is not None:
        fields.append("local_price_dh = ?")
        params.append(float(local_price_dh))
    if is_active is not None:
        fields.append("is_active = ?")
        params.append(1 if is_active else 0)
    if not fields:
        return False

    async with db_transaction() as db:
        row = await _fetch_service_row(db, service_id)
        if row is None:
            return False
        keys = _row_keys(row)
        catalog_id = str(row["catalog_id"]) if "catalog_id" in keys else str(row["service_id"])
        sql = f"UPDATE smm_services SET {', '.join(fields)} WHERE catalog_id = ?"
        cursor = await db.execute(sql, [*params, catalog_id])
        return int(cursor.rowcount or 0) > 0


def _default_local_price(rate_usd: float) -> float:
    return round(max(0.0, rate_usd) * DEFAULT_MARKUP_MULTIPLIER, 2)


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _limits_from_entry(entry: dict[str, Any]) -> tuple[int, int]:
    min_raw = _case_insensitive_get(entry, "min", "min_qty", "minimum")
    max_raw = _case_insensitive_get(entry, "max", "max_qty", "maximum")
    min_qty = max(1, _safe_int(min_raw, default=1))
    max_qty = _safe_int(max_raw, default=1_000_000)
    if max_qty < min_qty:
        max_qty = min_qty
    return min_qty, max_qty


def _case_insensitive_get(entry: dict[str, Any], *keys: str) -> Any:
    lowered = {str(k).lower(): v for k, v in entry.items() if isinstance(k, str)}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


async def sync_from_provider(provider_services: list[dict[str, Any]]) -> dict[str, int]:
    """Merge provider API list into smm_services (multi-provider aware)."""
    updated = 0
    inserted = 0
    rates_applied = 0
    skipped_no_id = 0

    async with db_transaction() as db:
        for entry in provider_services:
            if not isinstance(entry, dict):
                continue

            pid = parse_provider_service_id(entry)
            if pid is None:
                skipped_no_id += 1
                continue

            external_id = str(pid)
            provider_slug = str(
                entry.get("provider_slug") or get_default_provider_slug(),
            ).strip().lower()
            api_account = str(entry.get("api_account") or "default").strip().lower() or "default"
            rate = parse_provider_rate(entry)
            min_qty, max_qty = _limits_from_entry(entry)

            if rate is not None and rate > 0:
                rates_applied += 1

            cursor = await db.execute(
                """
                SELECT catalog_id FROM smm_services
                WHERE provider_slug = ? AND external_service_id = ?
                """,
                (provider_slug, external_id),
            )
            exists = await cursor.fetchone()

            if exists:
                catalog_id = str(exists["catalog_id"])
                if rate is not None:
                    await db.execute(
                        """
                        UPDATE smm_services
                        SET provider_price_usd = ?,
                            min_qty = ?,
                            max_qty = ?,
                            provider_api_account = ?,
                            provider_price_updated_at = datetime('now')
                        WHERE catalog_id = ?
                        """,
                        (float(rate), min_qty, max_qty, api_account, catalog_id),
                    )
                else:
                    await db.execute(
                        """
                        UPDATE smm_services
                        SET min_qty = ?, max_qty = ?, provider_api_account = ?
                        WHERE catalog_id = ?
                        """,
                        (min_qty, max_qty, api_account, catalog_id),
                    )
                    logger.warning(
                        "Provider service %s/%s missing rate: keys=%s",
                        provider_slug,
                        external_id,
                        list(entry.keys())[:12],
                    )
                updated += 1
            else:
                name = str(
                    _case_insensitive_get(entry, "name", "title")
                    or f"خدمة #{external_id}",
                )[:500]
                category = str(
                    _case_insensitive_get(entry, "category", "type") or "غير مصنّف",
                )[:500]
                usd_rate = float(rate) if rate is not None else 0.0
                catalog_id = f"{provider_slug}-{external_id}"
                await db.execute(
                    """
                    INSERT INTO smm_services (
                        catalog_id, external_service_id, service_id, provider_slug,
                        provider_api_account, category, name_ar, provider_price_usd,
                        local_price_dh, min_qty, max_qty, is_active,
                        platform_key, local_item_id, platform_title
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', ?, '')
                    """,
                    (
                        catalog_id,
                        external_id,
                        external_id,
                        provider_slug,
                        api_account,
                        category,
                        name,
                        usd_rate,
                        _default_local_price(usd_rate),
                        min_qty,
                        max_qty,
                        catalog_id,
                    ),
                )
                inserted += 1

        await db.commit()

    logger.info(
        "Provider sync: updated=%s inserted=%s rates_applied=%s skipped_no_id=%s",
        updated,
        inserted,
        rates_applied,
        skipped_no_id,
    )
    return {
        "updated": updated,
        "inserted": inserted,
        "rates_applied": rates_applied,
        "skipped_no_id": skipped_no_id,
        "total_provider": len(provider_services),
    }


async def sync_catalog_with_providers(
    *,
    provider_slug: str | None = None,
) -> dict[str, Any]:
    from services.provider_sync_bridge import refresh_catalog_prices
    from services.smm_provider import fetch_provider_services

    payload = await fetch_provider_services(provider_slug=provider_slug)
    if not payload.get("ok"):
        return {
            "ok": False,
            "error": payload.get("error") or "فشل جلب خدمات المزوّد.",
        }
    merge_stats = await sync_from_provider(payload.get("services") or [])
    price_stats = await refresh_catalog_prices(
        provider_slug=provider_slug,
        active_only=False,
    )
    return {
        "ok": True,
        **merge_stats,
        **price_stats,
    }


async def count_services() -> int:
    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM smm_services") as cursor:
            row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def count_services_missing_provider_rate() -> tuple[int, int]:
    """Return (rows_with_zero_rate, total_rows)."""
    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM smm_services") as cursor:
            total_row = await cursor.fetchone()
        async with db.execute(
            """
            SELECT COUNT(*) FROM smm_services
            WHERE COALESCE(provider_price_usd, 0) <= 0
            """
        ) as cursor:
            zero_row = await cursor.fetchone()
    total = int(total_row[0]) if total_row else 0
    zero = int(zero_row[0]) if zero_row else 0
    return zero, total
