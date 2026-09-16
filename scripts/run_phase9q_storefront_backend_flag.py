# -*- coding: utf-8 -*-
"""Phase 9Q runner — storefront backend flag report."""
from __future__ import annotations

import json

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9q_storefront_backend_flag import run_phase9q


def _md(report: dict) -> str:
    sections = [
        ("1. Verdict", f"`{report.get('verdict')}`\n\nNext: `{report.get('next_step')}`"),
        ("2. Architecture implemented", f"```json\n{json.dumps(report.get('architecture_implemented'), ensure_ascii=False, indent=2)}\n```"),
        ("3. Storefront contract", "\n".join(f"- `{x}`" for x in (report.get("storefront_contract") or []))),
        ("4. Legacy backend", f"```json\n{json.dumps(report.get('legacy_backend'), ensure_ascii=False, indent=2)}\n```"),
        ("5. Catalog backend", f"```json\n{json.dumps(report.get('catalog_backend'), ensure_ascii=False, indent=2)}\n```"),
        ("6. Backend selection mechanism", f"```json\n{json.dumps(report.get('backend_selection'), ensure_ascii=False, indent=2)}\n```"),
        ("7. Default behavior", str(report.get("default_behavior"))),
        ("8. Telegram handler integration", f"```json\n{json.dumps(report.get('telegram_handler_integration'), ensure_ascii=False, indent=2)}\n```"),
        ("9. Order contract boundary", f"```json\n{json.dumps(report.get('order_contract_boundary'), ensure_ascii=False, indent=2)}\n```"),
        ("10. Pricing boundary", f"```json\n{json.dumps(report.get('pricing_boundary'), ensure_ascii=False, indent=2)}\n```"),
        ("11. Execution identity boundary", f"```json\n{json.dumps(report.get('execution_identity_boundary'), ensure_ascii=False, indent=2)}\n```"),
        ("12. Target/link boundary", f"```json\n{json.dumps(report.get('target_link_boundary'), ensure_ascii=False, indent=2)}\n```"),
        ("13. Static coupling audit", f"```json\n{json.dumps(report.get('static_coupling_audit'), ensure_ascii=False, indent=2)[:6000]}\n```"),
        ("14. Test results", f"```json\n{json.dumps(report.get('tests'), ensure_ascii=False, indent=2)}\n```"),
        (
            "15. Production invariants",
            f"Baseline: `{json.dumps(report.get('production_baseline'), ensure_ascii=False)}`\n\n"
            f"After: `{json.dumps(report.get('production_after'), ensure_ascii=False)}`\n\n"
            f"Unchanged: **{report.get('production_unchanged')}**",
        ),
        ("16. Rollback mechanism", f"```json\n{json.dumps(report.get('rollback_mechanism'), ensure_ascii=False, indent=2)}\n```"),
        (
            "17. Remaining blockers",
            "\n".join(f"- {b}" for b in (report.get("remaining_blockers") or [])),
        ),
        (
            "18. Explicit statement",
            f"**Catalog was NOT enabled for production customers.** "
            f"`catalog_enabled_for_production_customers={report.get('catalog_enabled_for_production_customers')}`\n\n"
            f"```json\n{json.dumps(report.get('explicit_non_goals'), ensure_ascii=False, indent=2)}\n```",
        ),
    ]
    lines = [
        "# Phase 9Q — Telegram Storefront Backend Flag",
        "",
        f"Generated: `{report.get('timestamp')}`",
        "",
        f"**Verdict:** `{report.get('verdict')}`",
        "",
    ]
    for title, body in sections:
        lines.extend([f"## {title}", "", body, ""])
    lines.extend(
        [
            "## HARD STOP",
            "",
            "Do not enable Catalog in production. Do not start customer cutover.",
            "Recommend only: `START_PHASE_9R_TELEGRAM_CATALOG_E2E_SHADOW`",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    with catalog_transaction(resolve_db_path()) as conn:
        report = run_phase9q(conn)
    report.setdefault("tests", {})
    report["tests"]["suite_previous"] = 431
    with open(
        "scripts/out_phase9q_storefront_backend_flag.json", "w", encoding="utf-8"
    ) as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open(
        "scripts/out_phase9q_storefront_backend_flag.md", "w", encoding="utf-8"
    ) as fh:
        fh.write(_md(report))
    print(
        json.dumps(
            {
                "verdict": report.get("verdict"),
                "next_step": report.get("next_step"),
                "production_unchanged": report.get("production_unchanged"),
                "catalog_enabled_for_production_customers": report.get(
                    "catalog_enabled_for_production_customers"
                ),
                "baseline": report.get("production_baseline"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
