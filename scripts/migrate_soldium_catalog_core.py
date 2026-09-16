#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apply or reverse Soldium Catalog core schema (Phase 2 + Phase 3 execution sources).

Usage:
  python scripts/migrate_soldium_catalog_core.py up
  python scripts/migrate_soldium_catalog_core.py down

Does NOT touch Catalog-v2 tables, smm_services, orders, or providers.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from catalog_core.schema import ensure_soldium_catalog_schema  # noqa: E402
from database_connector import DB_PATH  # noqa: E402


def migrate_up(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_soldium_catalog_schema(conn)
        conn.commit()
        print(f"OK: soldium_catalog_* applied on {db_path}")
    finally:
        conn.close()


def migrate_down(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # Drop in FK-safe order (Phase 3 first)
        conn.execute("DROP TABLE IF EXISTS soldium_catalog_prices")
        conn.execute("DROP TABLE IF EXISTS soldium_catalog_execution_sources")
        conn.execute("DROP TABLE IF EXISTS soldium_catalog_entries")
        conn.execute("DROP TABLE IF EXISTS soldium_catalog_services")
        conn.execute("DROP TABLE IF EXISTS soldium_catalog_nodes")
        conn.execute("DROP TABLE IF EXISTS soldium_catalog_schema_meta")
        conn.commit()
        print(f"OK: soldium_catalog_* removed from {db_path}")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Soldium Catalog core migration")
    parser.add_argument("direction", choices=("up", "down"))
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()
    path = Path(args.db)
    if args.direction == "up":
        migrate_up(path)
    else:
        migrate_down(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
