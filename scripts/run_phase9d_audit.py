# -*- coding: utf-8 -*-
"""Phase 9D runner — audit + optional CONFIRMED authoring."""
from __future__ import annotations

import json
import sys

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.order_contract_republish import PILOT_SERVICE_IDS
from catalog_core.phase9d_audit import (
    apply_confirmed_authoring,
    load_phase9d_rows,
    production_counts,
    run_phase9d_audit,
)
from catalog_core.storefront_shadow import compare_storefronts


def main() -> None:
    apply = "--apply" in sys.argv
    path = resolve_db_path()
    with catalog_transaction(path) as conn:
        before = production_counts(conn)
        audit = run_phase9d_audit(conn)
        rows = load_phase9d_rows(conn)

        # Snapshot pilots before
        pilots_before = []
        for pid in PILOT_SERVICE_IDS:
            r = conn.execute(
                """
                SELECT id, service_type, ordering_mode, fulfillment_mode,
                       min_quantity, max_quantity,
                       target_platform_key, target_section_key,
                       target_subsection_key, target_link_type,
                       target_link_prompt_key
                FROM soldium_catalog_services WHERE id=?
                """,
                (pid,),
            ).fetchone()
            pilots_before.append(dict(r))

        auth1 = apply_confirmed_authoring(conn, rows, dry_run=not apply)
        # Reload for idempotency check
        rows2 = load_phase9d_rows(conn)
        auth2 = apply_confirmed_authoring(conn, rows2, dry_run=not apply)

        after = production_counts(conn)
        pilots_after = []
        for pid in PILOT_SERVICE_IDS:
            r = conn.execute(
                """
                SELECT id, service_type, ordering_mode, fulfillment_mode,
                       min_quantity, max_quantity,
                       target_platform_key, target_section_key,
                       target_subsection_key, target_link_type,
                       target_link_prompt_key
                FROM soldium_catalog_services WHERE id=?
                """,
                (pid,),
            ).fetchone()
            pilots_after.append(dict(r))

        shadow = compare_storefronts(conn).to_dict()
        report = {
            "timestamp": audit["timestamp"],
            "apply": apply,
            "summary": {
                "remaining_count": audit["remaining_count"],
                "confidence_counts": audit["matrices"]["confidence_counts"],
                "candidate_type_counts": audit["matrices"]["candidate_type_counts"],
                "safe_author_service_type_count": audit["matrices"][
                    "safe_author_service_type_count"
                ],
                "safe_author_link_type_count": audit["matrices"][
                    "safe_author_link_type_count"
                ],
                "confirmed_cohort_count": len(audit["matrices"]["confirmed_cohorts"]),
                "special_or_probable_cohort_count": len(
                    audit["matrices"]["special_or_probable_cohorts"]
                ),
                "blocked_missing_execution": len(
                    audit["matrices"]["blocked_missing_execution"]
                ),
                "sentinel_count": len(audit["matrices"]["sentinel_services"]),
                "per_unit_count": len(audit["matrices"]["per_unit_services"]),
            },
            "matrices": {
                k: audit["matrices"][k]
                for k in audit["matrices"]
                if k
                not in {
                    # full service_ids lists kept in audit file
                }
            },
            "authoring_pass1": auth1,
            "authoring_pass2": auth2,
            "production_before": before,
            "production_after": after,
            "pilots_before": pilots_before,
            "pilots_after": pilots_after,
            "pilots_unchanged": pilots_before == pilots_after,
            "shadow": {
                "legacy_count": shadow.get("legacy_count"),
                "catalog_count": shadow.get("catalog_count"),
                "correlated_count": shadow.get("correlated_count"),
                "legacy_only_count": shadow.get("legacy_only_count"),
                "dangerous_count": shadow.get("dangerous_count"),
                "review_count": shadow.get("review_count"),
                "changed_count": shadow.get("changed_count"),
                "equal_count": shadow.get("equal_count"),
                "publication_count": shadow.get("publication_count"),
            },
        }

        with open("scripts/out_phase9d_audit.json", "w", encoding="utf-8") as fh:
            json.dump(audit, fh, ensure_ascii=False, indent=2)
        with open("scripts/out_phase9d_report.json", "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        if apply or auth1["change_count"]:
            with open(
                "scripts/out_phase9d_authoring.json", "w", encoding="utf-8"
            ) as fh:
                json.dump(
                    {
                        "pass1": auth1,
                        "pass2": auth2,
                        "production_before": before,
                        "production_after": after,
                    },
                    fh,
                    ensure_ascii=False,
                    indent=2,
                )

        slim = {
            "summary": report["summary"],
            "authoring_pass1": {
                k: auth1[k]
                for k in (
                    "dry_run",
                    "updated_service_type",
                    "updated_link_type",
                    "skipped",
                    "change_count",
                )
            },
            "authoring_pass2": {
                k: auth2[k]
                for k in (
                    "dry_run",
                    "updated_service_type",
                    "updated_link_type",
                    "skipped",
                    "change_count",
                )
            },
            "production_before": before,
            "production_after": after,
            "pilots_unchanged": report["pilots_unchanged"],
            "shadow": report["shadow"],
        }
        print(json.dumps(slim, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
