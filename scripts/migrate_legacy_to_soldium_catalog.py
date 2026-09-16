#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 8D — controlled legacy → SOLDIUM Catalog migration.

Usage:
  python scripts/migrate_legacy_to_soldium_catalog.py --preflight
  python scripts/migrate_legacy_to_soldium_catalog.py --execute

Does NOT publish, touch Telegram/Orders/Fulfillment, or modify smm_services.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from catalog_core.legacy_migration_import import (  # noqa: E402
    execute_legacy_migration,
    run_preflight,
)
from database_connector import DB_PATH  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 8D legacy Catalog migration")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--preflight", action="store_true")
    group.add_argument("--execute", action="store_true")
    parser.add_argument("--db", type=str, default=None, help="Override DB path")
    args = parser.parse_args()
    db_path = Path(args.db) if args.db else Path(DB_PATH)

    if args.preflight:
        # Ensure schema so version check is meaningful, without importing data.
        from catalog_core.schema import ensure_soldium_catalog_at_path

        ensure_soldium_catalog_at_path(db_path)
        result = run_preflight(db_path)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.ok else 2

    result = execute_legacy_migration(db_path)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if result.verdict.startswith("MIGRATION SUCCESSFUL"):
        return 0
    if "STOPPED" in result.verdict:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
