# -*- coding: utf-8 -*-
"""Phase 9S runner — customer parity & cutover readiness (read-only)."""
from __future__ import annotations

import json

from catalog_core.db import catalog_readonly_connection, resolve_db_path
from catalog_core.phase9s_customer_parity import run_phase9s


def _md(report: dict) -> str:
    def j(key: str, limit: int | None = None) -> str:
        raw = json.dumps(report.get(key), ensure_ascii=False, indent=2)
        if limit and len(raw) > limit:
            return raw[:limit] + "\n… [truncated]"
        return raw

    blockers = report.get("cutover_blocker_register") or []
    blocker_lines = [
        f"| {b.get('id')} | {b.get('name')} | {b.get('status')} | {b.get('remaining')} |"
        for b in blockers
    ]

    return "\n".join(
        [
            "# Phase 9S — Customer Parity & Cutover Readiness",
            "",
            f"Generated: `{report.get('timestamp')}`",
            "",
            f"**Verdict:** `{report.get('verdict')}`",
            "",
            f"Next (recommend only): `{report.get('next_step')}`",
            "",
            f"Read-only: **{report.get('read_only')}** · Cutover occurred: **{report.get('production_cutover_occurred')}**",
            "",
            f"Production unchanged: **{report.get('production_unchanged')}**",
            "",
            "## 1. Verdict",
            "",
            f"`{report.get('verdict')}`",
            "",
            "## 2. 43-service cohort analysis",
            "",
            f"```json\n{j('cohort_43_analysis', 12000)}\n```",
            "",
            "## 3. 253-service coverage analysis",
            "",
            f"```json\n{j('coverage_253_analysis')}\n```",
            "",
            "## 4–10. Difference classifications & breaking lists",
            "",
            f"Classifications: `{json.dumps(report.get('difference_classifications'), ensure_ascii=False)}`",
            "",
            f"Customer-breaking: `{json.dumps(report.get('customer_breaking_differences'), ensure_ascii=False)}`",
            "",
            f"Commercial-breaking: `{json.dumps(report.get('commercial_breaking_differences'), ensure_ascii=False)}`",
            "",
            f"Execution-breaking: `{json.dumps(report.get('execution_breaking_differences'), ensure_ascii=False)}`",
            "",
            f"Safety-breaking: `{json.dumps(report.get('safety_breaking_differences'), ensure_ascii=False)}`",
            "",
            f"Expected/intentional: ```json\n{j('expected_intentional_differences')}\n```",
            "",
            "## 11. Pricing parity",
            "",
            f"```json\n{j('pricing_parity')}\n```",
            "",
            "## 12. Quantity parity",
            "",
            f"```json\n{j('quantity_parity')}\n```",
            "",
            "## 13. Target/link parity",
            "",
            f"```json\n{j('target_link_parity')}\n```",
            "",
            "## 14. Fulfillment parity",
            "",
            f"```json\n{j('fulfillment_parity')}\n```",
            "",
            "## 15. Execution identity parity",
            "",
            f"```json\n{j('execution_identity_parity')}\n```",
            "",
            "## 16. Navigation/menu parity",
            "",
            f"```json\n{j('navigation_menu_parity')}\n```",
            "",
            "## 17. Order-flow parity",
            "",
            f"```json\n{j('order_flow_parity')}\n```",
            "",
            "## 18. Legacy-only 210-service accounting",
            "",
            f"```json\n{j('legacy_only_210_accounting')}\n```",
            "",
            "## 19. Recommended migration strategy",
            "",
            f"```json\n{j('recommended_migration_strategy')}\n```",
            "",
            "## 20. Cutover blocker register",
            "",
            "| ID | Name | Status | Remaining |",
            "|----|------|--------|-----------|",
            *blocker_lines,
            "",
            "## 21. Rollback readiness",
            "",
            f"```json\n{j('rollback_readiness')}\n```",
            "",
            "## 22. Tests",
            "",
            f"```json\n{j('tests')}\n```",
            "",
            "## 23. Production invariants",
            "",
            f"Baseline: `{json.dumps(report.get('production_baseline'), ensure_ascii=False)}`",
            "",
            f"After: `{json.dumps(report.get('production_after'), ensure_ascii=False)}`",
            "",
            "## 24. Remaining prerequisites",
            "",
            *[f"- {p}" for p in (report.get("remaining_prerequisites") or [])],
            "",
            "## 25. Explicit statement",
            "",
            str(report.get("explicit_statement")),
            "",
            "## HARD STOP",
            "",
            "Do not enable Catalog in production. Do not start cutover. Recommend 9T only.",
            "",
        ]
    )


def main() -> None:
    with catalog_readonly_connection(resolve_db_path()) as conn:
        report = run_phase9s(conn)
    report.setdefault("tests", {})
    report["tests"]["suite_previous"] = 452
    with open("scripts/out_phase9s_customer_parity.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open("scripts/out_phase9s_customer_parity.md", "w", encoding="utf-8") as fh:
        fh.write(_md(report))
    print(
        json.dumps(
            {
                "verdict": report.get("verdict"),
                "next_step": report.get("next_step"),
                "production_unchanged": report.get("production_unchanged"),
                "production_cutover_occurred": report.get("production_cutover_occurred"),
                "counts": (report.get("cohort_43_analysis") or {}).get("counts"),
                "coverage": report.get("coverage_253_analysis"),
                "strategy": (report.get("recommended_migration_strategy") or {}).get(
                    "recommended"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
