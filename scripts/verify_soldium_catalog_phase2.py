# -*- coding: utf-8 -*-
"""One-shot integrity check for Phase 2 Catalog tables."""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database_connector import DB_PATH  # noqa: E402

NEW = (
    "soldium_catalog_services",
    "soldium_catalog_nodes",
    "soldium_catalog_entries",
    "soldium_catalog_execution_sources",
    "soldium_catalog_prices",
    "soldium_catalog_schema_meta",
)
V2 = (
    "catalog_nodes",
    "catalog_services",
    "service_provider_bindings",
    "provider_inventory",
    "price_rules",
    "catalog_audit_log",
    "catalog_meta",
)
LEGACY = ("smm_services", "orders", "providers", "provider_accounts")


def main() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        print("DB:", DB_PATH)
        print("NEW present:", [t for t in NEW if t in tables])
        print("NEW missing:", [t for t in NEW if t not in tables])
        print("V2 present:", [t for t in V2 if t in tables])
        print("Legacy present:", [t for t in LEGACY if t in tables])
        for t in [*NEW, *V2, *LEGACY]:
            if t in tables:
                n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                print(f"  count {t}: {n}")
        ver = conn.execute(
            "SELECT value FROM soldium_catalog_schema_meta WHERE key=?",
            ("schema_version",),
        ).fetchone()
        print("schema_version:", ver[0] if ver else None)
        conn.execute("PRAGMA foreign_keys = ON")
        print("foreign_keys:", conn.execute("PRAGMA foreign_keys").fetchone()[0])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
