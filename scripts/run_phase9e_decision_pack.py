# -*- coding: utf-8 -*-
"""Phase 9E Stage A — write Decision Pack artifacts (read-only)."""
from __future__ import annotations

import json

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9e_decision_pack import (
    build_decision_pack,
    production_counts,
    summarize_decision_pack,
)
from catalog_core.storefront_shadow import compare_storefronts


def main() -> None:
    path = resolve_db_path()
    with catalog_transaction(path) as conn:
        before = production_counts(conn)
        pack = build_decision_pack(conn)
        after = production_counts(conn)
        shadow = compare_storefronts(conn).to_dict()
        report = {
            "summary": summarize_decision_pack(pack),
            "production_before": before,
            "production_after": after,
            "production_unchanged": before == after,
            "shadow": {
                "legacy_count": shadow.get("legacy_count"),
                "catalog_count": shadow.get("catalog_count"),
                "correlated_count": shadow.get("correlated_count"),
                "legacy_only_count": shadow.get("legacy_only_count"),
                "dangerous_count": shadow.get("dangerous_count"),
                "publication_count": shadow.get("publication_count"),
            },
            "stage_b_blocked": True,
            "mutations": "NONE",
        }
        with open(
            "scripts/out_phase9e_decision_pack.json", "w", encoding="utf-8"
        ) as fh:
            json.dump(pack, fh, ensure_ascii=False, indent=2)
        with open(
            "scripts/out_phase9e_decision_report.json", "w", encoding="utf-8"
        ) as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
