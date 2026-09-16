# -*- coding: utf-8 -*-
"""Phase 9F read-only architecture review runner."""
from __future__ import annotations

import json
import traceback

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9d_audit import production_counts
from catalog_core.phase9f_architecture_review import run_architecture_review


def _md(report: dict) -> str:
    inv = report["inventory"]
    lines = [
        "# Phase 9F — Architectural Review & Expansion Gate",
        "",
        f"Generated: `{report['timestamp']}`",
        "",
        f"**Recommendation:** `{report['recommendation']}`",
        "",
        report["recommendation_rationale"],
        "",
        "## Inventory",
        "",
        f"- legacy smm_services: **{inv['legacy_smm_services_total']}**",
        f"- legacy active storefront estimate: **{inv['legacy_active_storefront_estimate']}**",
        f"- catalog services/nodes/entries/prices: **{inv['catalog_services']} / {inv['catalog_nodes']} / {inv['catalog_entries']} / {inv['catalog_prices']}**",
        f"- execution sources: **{inv['execution_sources']}**",
        f"- publications rows: **{inv['publications_rows']}**",
        f"- published services: **{inv['published_services']}**",
        f"- mappings: **{inv['mappings']}**",
        f"- orders: **{inv['orders']}**",
        f"- scheduled_orders: **{inv['scheduled_orders']}**",
        "",
        "## Immutability",
        "",
        f"- published audited: {report['immutability_summary']['published_count']}",
        f"- immutability OK: {report['immutability_summary']['immutability_ok']}",
        f"- parity VERIFIED: {report['immutability_summary']['parity_verified']}",
        "",
        "## Shadow",
        "",
        "```",
        json.dumps(report["shadow"], indent=2),
        "```",
        "",
        "## Remaining unpublished cohorts",
        "",
        f"Total remaining: **{report['remaining_unpublished']['remaining_count']}**",
        "",
    ]
    for c in report["remaining_unpublished"]["categories"]:
        lines.append(
            f"- `{c['category']}`: **{c['count']}** — {c['blocking_reason']} "
            f"({c['decision_type']})"
        )
    lines.extend(
        [
            "",
            "## Business decision matrix",
            "",
            "| Cohort | Count | Current | Decision | Safe action |",
            "|--------|------:|---------|----------|-------------|",
        ]
    )
    for m in report["business_decision_matrix"]:
        lines.append(
            f"| {m['cohort']} | {m['count']} | {m['current_state']} | "
            f"{m['required_decision']} | {m['safe_action']} |"
        )
    lines.extend(["", "## Cutover readiness", ""])
    for c in report["cutover_readiness_matrix"]:
        lines.append(f"- [{c['status']}] {c['item']} — {c['note']}")
    lines.extend(
        [
            "",
            "## Expansion options",
            "",
        ]
    )
    for o in report["expansion_options"]:
        lines.append(f"### Option {o['option']}: {o['title']}")
        lines.append(f"- Advantages: {'; '.join(o['advantages'])}")
        lines.append(f"- Risks: {'; '.join(o['risks'])}")
        lines.append("")
    lines.append("---")
    lines.append("HARD STOP — no production mutations in Phase 9F.")
    return "\n".join(lines)


def main() -> int:
    before = None
    after = None
    try:
        with catalog_transaction(resolve_db_path()) as conn:
            before = production_counts(conn)
            report = run_architecture_review(conn)
            after = production_counts(conn)
            report["production_counts_before"] = before
            report["production_counts_after"] = after
            report["production_unchanged"] = before == after
            if before != after:
                report["recommendation"] = "ARCHITECTURAL BLOCKER FOUND"
                report["recommendation_rationale"] = (
                    "Production counts changed during read-only review — unexpected."
                )

            # Slim buckets for JSON size
            rem = report["remaining_unpublished"]
            rem["buckets_counts_only"] = {
                k: len(v) for k, v in rem.get("buckets", {}).items()
            }
            rem.pop("buckets", None)

            with open(
                "scripts/out_phase9f_architecture_review.json",
                "w",
                encoding="utf-8",
            ) as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
            with open(
                "scripts/out_phase9f_architecture_review.md",
                "w",
                encoding="utf-8",
            ) as fh:
                fh.write(_md(report))

            slim = {
                "recommendation": report["recommendation"],
                "inventory": report["inventory"],
                "immutability_summary": report["immutability_summary"],
                "shadow": report["shadow"],
                "remaining": report["remaining_unpublished"]["categories"],
                "remaining_count": report["remaining_unpublished"]["remaining_count"],
                "business_matrix": report["business_decision_matrix"],
                "pilot_comparison": report["pilot_comparison"],
                "cutover": report["cutover_readiness_matrix"],
                "production_unchanged": report["production_unchanged"],
            }
            print(json.dumps(slim, ensure_ascii=False, indent=2))
            return 0
    except Exception as exc:
        payload = {
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "production_before": before,
            "production_after": after,
        }
        with open(
            "scripts/out_phase9f_architecture_review.json",
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
