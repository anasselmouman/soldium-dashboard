# -*- coding: utf-8 -*-
"""Raise local_price_dh to provider_price_usd * multiplier where below minimum."""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path

MULTIPLIER = 14.0
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "soldium-bot" / "users.db"


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    if not db_path.is_file():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT catalog_id, service_id, name_ar, platform_title, provider_slug,
                   category, provider_price_usd, local_price_dh, is_active
            FROM smm_services
            WHERE provider_price_usd > 0
              AND local_price_dh > 0
              AND local_price_dh < (provider_price_usd * ?)
            ORDER BY (provider_price_usd * ?) - local_price_dh DESC
            """,
            (MULTIPLIER, MULTIPLIER),
        )
        rows = cur.fetchall()

        changes: list[dict] = []
        for row in rows:
            provider_usd = float(row["provider_price_usd"])
            old_dh = float(row["local_price_dh"])
            new_dh = math.ceil(provider_usd * MULTIPLIER * 100) / 100.0
            if new_dh <= old_dh:
                new_dh = round(old_dh + 0.01, 2)
            cur.execute(
                """
                UPDATE smm_services
                SET local_price_dh = ?
                WHERE catalog_id = ?
                """,
                (new_dh, row["catalog_id"]),
            )
            changes.append(
                {
                    "catalog_id": row["catalog_id"],
                    "service_id": row["service_id"],
                    "name_ar": row["name_ar"],
                    "platform": row["platform_title"],
                    "provider": row["provider_slug"],
                    "category": row["category"],
                    "is_active": int(row["is_active"] or 0),
                    "provider_price_usd": provider_usd,
                    "old_local_price_dh": old_dh,
                    "new_local_price_dh": new_dh,
                    "increase_dh": round(new_dh - old_dh, 2),
                }
            )

        conn.commit()

        summary = {
            "database": str(db_path),
            "multiplier": MULTIPLIER,
            "updated_count": len(changes),
            "total_increase_dh": round(sum(c["increase_dh"] for c in changes), 2),
            "active_updated": sum(1 for c in changes if c["is_active"]),
            "inactive_updated": sum(1 for c in changes if not c["is_active"]),
            "changes": changes,
        }
        out = json.dumps(summary, ensure_ascii=False, indent=2)
        out_path = db_path.parent / "margin_fix_summary.json"
        out_path.write_text(out, encoding="utf-8")
        print(f"Updated {len(changes)} services. Summary: {out_path}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
