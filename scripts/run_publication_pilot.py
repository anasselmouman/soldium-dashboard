# -*- coding: utf-8 -*-
"""Phase 9B.4 — Controlled pilot publication runner (production)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from catalog_core.db import catalog_readonly_connection, catalog_transaction, resolve_db_path
from catalog_core.publication_pilot import (
    publish_pilot,
    select_pilot_candidates,
    validate_pilot_intents,
)
from catalog_core.storefront_adapter import StorefrontAdapter
from catalog_core.storefront_projection import PublishedStorefrontProjection
from catalog_core.storefront_shadow import compare_storefronts


def _counts(conn) -> dict:
    def c(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "orders": c("SELECT COUNT(*) FROM orders"),
        "smm_services": c("SELECT COUNT(*) FROM smm_services"),
        "catalog_services": c("SELECT COUNT(*) FROM soldium_catalog_services"),
        "nodes": c("SELECT COUNT(*) FROM soldium_catalog_nodes"),
        "entries": c("SELECT COUNT(*) FROM soldium_catalog_entries"),
        "prices": c("SELECT COUNT(*) FROM soldium_catalog_prices"),
        "execution_sources": c(
            "SELECT COUNT(*) FROM soldium_catalog_execution_sources"
        ),
        "mappings": c("SELECT COUNT(*) FROM soldium_provider_service_mappings"),
        "publications": c("SELECT COUNT(*) FROM soldium_catalog_publications"),
        "bridges": c("SELECT COUNT(*) FROM soldium_catalog_legacy_bridge"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--apply", action="store_true", help="Actually publish")
    parser.add_argument("--out", default=None, help="Write JSON report path")
    args = parser.parse_args()
    db = resolve_db_path(args.db)

    with catalog_readonly_connection(db) as conn:
        before = _counts(conn)
        selected = select_pilot_candidates(conn, limit=args.limit)
        dry = publish_pilot(conn, selected, dry_run=True)

    report: dict = {
        "before": before,
        "selected": [c.to_dict() for c in selected],
        "dry_run": dry.to_dict(),
    }

    if not args.apply:
        report["mode"] = "dry_run_only"
        text = json.dumps(report, ensure_ascii=False, indent=2)
        print(text)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        return 0 if dry.status == "DRY_RUN" else 1

    if dry.status != "DRY_RUN":
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    with catalog_transaction(db) as conn:
        run = publish_pilot(conn, selected, dry_run=False)
        report["publish"] = run.to_dict()
        if run.status != "COMPLETE":
            report["after"] = _counts(conn)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1

        proj = PublishedStorefrontProjection(conn).build()
        adapter = StorefrontAdapter(conn)
        adapter_services = adapter.list_services()
        intents = validate_pilot_intents(conn, run.published_ids)
        shadow = compare_storefronts(conn, generated_at="phase9b4")

        # execution identity: adapter == projection == publication
        exec_checks = []
        for sid in run.published_ids:
            pub_row = next(
                r for r in run.results if r.soldium_service_id == sid and r.success
            )
            psvc = next(s for s in proj.services if s.service_id == sid)
            asvc = adapter.get_service(sid)
            exec_checks.append(
                {
                    "service_id": sid,
                    "publication_external": (pub_row.publication_id and True),
                    "fingerprint": pub_row.content_fingerprint,
                    "projection_external": psvc.execution.external_service_id,
                    "adapter_external": asvc.execution.external_service_id,
                    "match": (
                        psvc.execution.external_service_id
                        == asvc.execution.external_service_id
                        and psvc.execution.provider_slug == asvc.execution.provider_slug
                        and psvc.content_fingerprint == asvc.content_fingerprint
                        and psvc.content_fingerprint == pub_row.content_fingerprint
                    ),
                }
            )

        after = _counts(conn)
        report["after"] = after
        report["projection"] = {
            "count": len(proj.services),
            "ids": [s.service_id for s in proj.services],
        }
        report["adapter"] = {
            "count": len(adapter_services),
            "ids": [s.service_id for s in adapter_services],
        }
        report["intents"] = intents
        report["execution_checks"] = exec_checks
        shadow_dict = shadow.to_dict()
        # Trim rows for CLI readability; keep correlated diffs
        correlated_diffs = []
        for row in shadow.rows:
            if row.correlation != "correlated":
                continue
            meaningful = [
                d.to_dict()
                for d in row.differences
                if d.code
                not in {
                    "correlated",
                    "both_available",
                    "name_equal",
                    "pricing_mode_equal",
                    "currency_equal",
                    "price_equal",
                    "min_equal",
                    "max_equal",
                    "ordering_mode_equal",
                    "execution_equal",
                    "both_orderable",
                    "neither_orderable",
                }
            ]
            correlated_diffs.append(
                {
                    "legacy_identity": row.legacy_identity,
                    "catalog_identity": row.catalog_identity,
                    "service_name": row.service_name,
                    "severity": row.severity,
                    "notes": row.notes,
                    "differences": meaningful,
                }
            )
        report["shadow_summary"] = {
            k: shadow_dict[k]
            for k in shadow_dict
            if k not in {"rows", "known_risks"}
        }
        report["shadow_known_risks"] = shadow_dict["known_risks"]
        report["correlated_differences"] = correlated_diffs
        report["count_deltas"] = {
            k: after[k] - before[k] for k in before
        }

    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
