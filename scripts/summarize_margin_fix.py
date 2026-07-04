# -*- coding: utf-8 -*-
"""Generate summary of margin price fixes applied to users.db."""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "soldium-bot" / "users.db"
EDGE_SUMMARY = DB.parent / "margin_fix_summary_edge.json"
OUT = DB.parent / "margin_fix_final_summary.json"


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("SELECT COUNT(*) AS n FROM smm_services")
    total = int(c.fetchone()["n"])
    c.execute("SELECT COUNT(*) AS n FROM smm_services WHERE is_active = 1")
    active = int(c.fetchone()["n"])
    c.execute(
        """
        SELECT COUNT(*) AS n FROM smm_services
        WHERE provider_price_usd > 0 AND local_price_dh > 0
          AND local_price_dh < (provider_price_usd * 14)
        """
    )
    below = int(c.fetchone()["n"])

    edge_changes = []
    if EDGE_SUMMARY.is_file():
        edge_changes = json.loads(EDGE_SUMMARY.read_text(encoding="utf-8")).get("changes", [])

    # Services priced at minimum (likely touched by fix passes)
    c.execute(
        """
        SELECT catalog_id, name_ar, platform_title, provider_slug, is_active,
               provider_price_usd, local_price_dh
        FROM smm_services
        WHERE provider_price_usd > 0 AND local_price_dh > 0
          AND ABS(local_price_dh - (CEIL(provider_price_usd * 14 * 100) / 100.0)) < 0.001
        ORDER BY provider_price_usd DESC
        """
    )
    at_minimum = [dict(r) for r in c.fetchall()]

    # Estimate first-pass updates: at minimum but not in edge list
    edge_ids = {str(x["catalog_id"]) for x in edge_changes}
    first_pass_estimate = [s for s in at_minimum if str(s["catalog_id"]) not in edge_ids]

    summary = {
        "database": str(DB),
        "rule": "local_price_dh >= provider_price_usd * 14",
        "total_services": total,
        "active_services": active,
        "still_below_minimum": below,
        "first_pass_estimate_count": len(first_pass_estimate),
        "edge_case_pass_count": len(edge_changes),
        "total_adjusted_estimate": len(first_pass_estimate) + len(edge_changes),
        "at_minimum_price_count": len(at_minimum),
        "edge_pass": {
            "updated": len(edge_changes),
            "active_updated": sum(1 for x in edge_changes if x.get("is_active")),
            "inactive_updated": sum(1 for x in edge_changes if not x.get("is_active")),
            "total_increase_dh": round(sum(float(x.get("increase_dh") or 0) for x in edge_changes), 2),
            "largest_increases": sorted(edge_changes, key=lambda x: -float(x.get("increase_dh") or 0))[:15],
        },
        "sample_first_pass_services": first_pass_estimate[:15],
    }
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}")
    print(f"total_adjusted_estimate={summary['total_adjusted_estimate']}")
    print(f"still_below={below}")


if __name__ == "__main__":
    main()
