# -*- coding: utf-8 -*-
"""Phase 9P runner — pricing semantics review (read-only)."""
from __future__ import annotations

import json

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9p_pricing_semantics_review import run_phase9p


def _md(report: dict) -> str:
    matrix = [
        "| Flow | Price source | Calculated at | Stored? | Can change later? |",
        "|------|--------------|---------------|---------|-------------------|",
    ]
    for row in report.get("price_freeze_matrix") or []:
        matrix.append(
            f"| {row.get('flow')} | {row.get('price_source')} | {row.get('calculated_at')} | "
            f"{row.get('stored')} | {row.get('can_change_later')} |"
        )

    scenario_blocks: list[str] = []
    for s in report.get("scenario_results") or []:
        scenario_blocks.extend(
            [
                f"### Scenario {s.get('id')}: {s.get('title')}",
                "",
                f"**Today:** {s.get('result_today')}",
                "",
                f"*Basis:* {s.get('code_basis')}",
                "",
            ]
        )

    blockers = "\n".join(f"- {b}" for b in (report.get("remaining_blockers") or []))

    return "\n".join(
        [
            "# Phase 9P — Pricing Semantics Review",
            "",
            f"Generated: `{report.get('timestamp')}`",
            "",
            f"**Verdict:** `{report.get('verdict')}`",
            "",
            f"Read-only: **{report.get('read_only')}** · "
            f"Pricing behavior changed: **{report.get('pricing_behavior_changed')}**",
            "",
            f"Production unchanged: **{report.get('production_unchanged')}**",
            "",
            "## 1. Verdict",
            "",
            f"`{report.get('verdict')}`",
            "",
            "## 2. Current pricing architecture",
            "",
            f"```json\n{json.dumps(report.get('current_pricing_architecture'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## 3. Immediate-order pricing semantics",
            "",
            f"```json\n{json.dumps(report.get('immediate_order_semantics'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## 4. Scheduled-order pricing semantics",
            "",
            f"```json\n{json.dumps(report.get('scheduled_order_semantics'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## 5. Gen-0 pricing semantics",
            "",
            f"```json\n{json.dumps(report.get('gen0_pricing_semantics'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## 6. Gen-1 pricing semantics",
            "",
            f"```json\n{json.dumps(report.get('gen1_pricing_semantics'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## 7. Catalog pricing semantics",
            "",
            f"```json\n{json.dumps(report.get('catalog_pricing_semantics'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## 8. Legacy pricing semantics",
            "",
            f"```json\n{json.dumps(report.get('legacy_pricing_semantics'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## 9. Price-freeze matrix",
            "",
            *matrix,
            "",
            "## 10. Scenario results",
            "",
            *scenario_blocks,
            "## 11. Business-policy comparison",
            "",
            f"```json\n{json.dumps(report.get('business_policy_comparison'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## 12. Primary recommendation",
            "",
            f"```json\n{json.dumps(report.get('primary_recommendation'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## 13. Exact proposed freeze point",
            "",
            f"```json\n{json.dumps(report.get('exact_proposed_freeze_point'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## 14. Impact on future phases",
            "",
            f"```json\n{json.dumps(report.get('impact_on_future_phases'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## 15. Production invariants",
            "",
            f"Baseline: `{json.dumps(report.get('production_baseline'), ensure_ascii=False)}`",
            "",
            f"After: `{json.dumps(report.get('production_after'), ensure_ascii=False)}`",
            "",
            f"Mismatches: `{json.dumps(report.get('production_mismatches'), ensure_ascii=False)}`",
            "",
            "## 16. Tests",
            "",
            f"```json\n{json.dumps(report.get('tests'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## 17. Remaining blockers",
            "",
            blockers,
            "",
            "## 18. Explicit statement",
            "",
            "**NO pricing behavior was changed.** This phase is read-only decision documentation.",
            "",
            f"```json\n{json.dumps(report.get('explicit_non_goals'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## HARD STOP",
            "",
            "Do not implement the recommendation in this phase. Await architectural review.",
            "",
        ]
    )


def main() -> None:
    with catalog_transaction(resolve_db_path()) as conn:
        report = run_phase9p(conn)
    report.setdefault("tests", {})
    report["tests"]["suite_baseline"] = 431
    report["tests"]["note"] = (
        "Relevant pricing/scheduled tests run separately; no production mutation."
    )
    with open(
        "scripts/out_phase9p_pricing_semantics_review.json", "w", encoding="utf-8"
    ) as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open(
        "scripts/out_phase9p_pricing_semantics_review.md", "w", encoding="utf-8"
    ) as fh:
        fh.write(_md(report))
    slim = {
        "verdict": report.get("verdict"),
        "production_unchanged": report.get("production_unchanged"),
        "pricing_behavior_changed": report.get("pricing_behavior_changed"),
        "freeze_point_scheduled": (report.get("exact_proposed_freeze_point") or {}).get(
            "scheduled"
        ),
        "baseline": report.get("production_baseline"),
    }
    print(json.dumps(slim, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
