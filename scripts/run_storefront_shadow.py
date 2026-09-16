# -*- coding: utf-8 -*-
"""CLI — Phase 9B.3 read-only storefront shadow comparison."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as scripts/run_storefront_shadow.py from repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from catalog_core.storefront_shadow import compare_storefronts_at_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Legacy vs Published Catalog storefront shadow comparison"
    )
    parser.add_argument("--db", default=None, help="Path to users.db (optional)")
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print summary counts only (no per-row dump)",
    )
    args = parser.parse_args()
    report = compare_storefronts_at_path(args.db)
    data = report.to_dict()
    if args.summary:
        data = {k: v for k, v in data.items() if k != "rows"}
        data["row_count"] = len(report.rows)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
