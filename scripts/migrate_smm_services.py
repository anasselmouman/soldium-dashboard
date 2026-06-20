#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-time migration: nested services.json → smm_services table.

Run from soldium-dashboard (with venv active):

    python scripts/migrate_smm_services.py

Optional: --force  re-insert/upsert all rows from services.json
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from db_schema import ensure_smm_services_table  # noqa: E402
from database_connector import db_transaction, get_db  # noqa: E402
from settings import SERVICE_USD_TO_DH_MULTIPLIER  # noqa: E402
from smm_services import count_services  # noqa: E402
from utils.services_catalog import flatten_catalog, load_catalog  # noqa: E402


def _resolve_provider_rate(row: dict) -> float:
    provider_rate = row.get("provider_rate_usd")
    if provider_rate is not None:
        try:
            rate = float(provider_rate)
            if rate > 0:
                return rate
        except (TypeError, ValueError):
            pass
    if row.get("is_per_unit"):
        price_dh = float(row.get("price_dh") or 0)
        if price_dh > 0:
            return price_dh / SERVICE_USD_TO_DH_MULTIPLIER
    return 0.0


async def migrate(*, force: bool) -> None:
    await ensure_smm_services_table()
    existing = await count_services()
    if existing > 0 and not force:
        print(
            f"smm_services already has {existing} rows. "
            "Use --force to upsert from services.json anyway.",
        )
        return

    rows = flatten_catalog(load_catalog())
    if not rows:
        print("No catalog rows found in services.json.")
        return

    upserted = 0
    zero_rate_count = 0
    async with db_transaction() as db:
        for row in rows:
            service_id = str(row["provider_id"])
            provider_rate = _resolve_provider_rate(row)
            if provider_rate <= 0:
                zero_rate_count += 1
            category = (
                "per_unit"
                if row.get("is_per_unit")
                else str(row.get("category_label") or "")
            )
            await db.execute(
                """
                INSERT INTO smm_services (
                    service_id, category, name_ar, provider_price_usd,
                    local_price_dh, min_qty, max_qty, is_active,
                    platform_key, section_key, subsection_key, local_item_id,
                    platform_title, section_title, subsection_title
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(service_id) DO UPDATE SET
                    category = excluded.category,
                    name_ar = excluded.name_ar,
                    provider_price_usd = CASE
                        WHEN excluded.provider_price_usd > 0 THEN excluded.provider_price_usd
                        ELSE smm_services.provider_price_usd
                    END,
                    local_price_dh = excluded.local_price_dh,
                    min_qty = excluded.min_qty,
                    max_qty = excluded.max_qty,
                    platform_key = excluded.platform_key,
                    section_key = excluded.section_key,
                    subsection_key = excluded.subsection_key,
                    local_item_id = excluded.local_item_id,
                    platform_title = excluded.platform_title,
                    section_title = excluded.section_title,
                    subsection_title = excluded.subsection_title
                """,
                (
                    service_id,
                    category,
                    str(row.get("name") or ""),
                    float(provider_rate),
                    float(row.get("price_dh") or 0),
                    int(row.get("min") or 1),
                    int(row.get("max") or 0),
                    str(row.get("platform_key") or ""),
                    row.get("section_key"),
                    row.get("subsection_key"),
                    str(row.get("item_id") or service_id),
                    str(row.get("platform_title") or ""),
                    row.get("section_title"),
                    row.get("subsection_title"),
                ),
            )
            upserted += 1

    print(f"Migration complete: {upserted} services upserted into smm_services.")
    if zero_rate_count:
        print(
            f"Note: {zero_rate_count} service(s) have no provider USD rate. "
            "Run the dashboard (with SMM API keys) or sync from the catalog page."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate services.json to smm_services")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Upsert even if the table already has rows",
    )
    args = parser.parse_args()
    asyncio.run(migrate(force=args.force))


if __name__ == "__main__":
    main()
