# -*- coding: utf-8 -*-
"""Phase 9C audit runner (read-only unless --apply)."""
from __future__ import annotations

import json
import sys

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9c_audit import apply_safe_authoring, load_remaining_audit_rows, run_audit
from catalog_core.storefront_shadow import compare_storefronts


def _counts(conn) -> dict:
    return {
        "orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        "smm_services": conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0],
        "services": conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_services"
        ).fetchone()[0],
        "nodes": conn.execute("SELECT COUNT(*) FROM soldium_catalog_nodes").fetchone()[0],
        "entries": conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_entries"
        ).fetchone()[0],
        "prices": conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_prices"
        ).fetchone()[0],
        "execution_sources": conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_execution_sources"
        ).fetchone()[0],
        "mappings": conn.execute(
            "SELECT COUNT(*) FROM soldium_provider_service_mappings"
        ).fetchone()[0],
        "publications": conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_publications"
        ).fetchone()[0],
        "with_target_keys": conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_services "
            "WHERE target_platform_key IS NOT NULL "
            "AND length(trim(target_platform_key)) > 0"
        ).fetchone()[0],
        "fulfillment_admin": conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_services "
            "WHERE fulfillment_mode = 'admin'"
        ).fetchone()[0],
    }


def main() -> None:
    apply = "--apply" in sys.argv
    out_path = "scripts/out_phase9c_audit.json"
    path = resolve_db_path()

    with catalog_transaction(path) as conn:
        before = _counts(conn)
        report = run_audit(conn)
        rows = load_remaining_audit_rows(conn)
        auth = apply_safe_authoring(conn, rows, dry_run=not apply)
        report["authoring"] = auth
        report["production_counts_before"] = before
        report["production_counts_after"] = _counts(conn)
        shadow = compare_storefronts(conn).to_dict()
        report["shadow"] = {
            "legacy_count": shadow.get("legacy_count"),
            "catalog_count": shadow.get("catalog_count"),
            "correlated_count": shadow.get("correlated_count"),
            "legacy_only_count": shadow.get("legacy_only_count"),
            "catalog_only_count": shadow.get("catalog_only_count"),
            "dangerous_count": shadow.get("dangerous_count"),
            "review_count": shadow.get("review_count"),
            "changed_count": shadow.get("changed_count"),
            "equal_count": shadow.get("equal_count"),
            "publication_count": shadow.get("publication_count"),
        }
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        slim = {
            k: report[k]
            for k in (
                "summary",
                "cohorts",
                "authoring",
                "production_counts_before",
                "production_counts_after",
                "shadow",
            )
        }
        print(json.dumps(slim, ensure_ascii=False, indent=2))
        if not apply:
            # Ensure dry-run never commits authoring (none written).
            pass


if __name__ == "__main__":
    main()
