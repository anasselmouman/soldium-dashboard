# -*- coding: utf-8 -*-
"""Phase 9R runner — Telegram Catalog E2E Shadow report."""
from __future__ import annotations

import json

from catalog_core.db import catalog_readonly_connection, resolve_db_path
from catalog_core.phase9r_telegram_catalog_e2e_shadow import run_phase9r


def _md(report: dict) -> str:
    matrix = [
        "| Test | Expected | Actual | Result |",
        "|------|----------|--------|--------|",
    ]
    for m in report.get("test_matrix") or []:
        matrix.append(
            f"| {m.get('test')} | {m.get('expected')} | {m.get('actual')} | {m.get('result')} |"
        )

    lines = [
        "# Phase 9R — Telegram Catalog E2E Shadow",
        "",
        f"Generated: `{report.get('timestamp')}`",
        "",
        f"**Verdict:** `{report.get('verdict')}`",
        "",
        f"Next (recommend only): `{report.get('next_step')}`",
        "",
        f"Production unchanged: **{report.get('production_unchanged')}**",
        "",
        f"Catalog enabled for production customers: **{report.get('catalog_enabled_for_production_customers')}**",
        "",
        "## 1. Verdict",
        "",
        f"`{report.get('verdict')}`",
        "",
        "## 2. Test environment",
        "",
        f"```json\n{json.dumps(report.get('test_environment'), ensure_ascii=False, indent=2)}\n```",
        "",
        "## 3. Backend selection proof",
        "",
        f"```json\n{json.dumps(report.get('backend_selection_proof'), ensure_ascii=False, indent=2)}\n```",
        "",
        "## 4. Telegram handler path",
        "",
        str(report.get("telegram_handler_path")),
        "",
        "## 5. Representative services",
        "",
        f"```json\n{json.dumps(report.get('representative_services'), ensure_ascii=False, indent=2)}\n```",
        "",
        "## 6. Navigation results",
        "",
        f"```json\n{json.dumps(report.get('navigation_results'), ensure_ascii=False, indent=2)}\n```",
        "",
        "## 7. Published-only proof",
        "",
        f"```json\n{json.dumps(report.get('published_only_proof'), ensure_ascii=False, indent=2)}\n```",
        "",
        "## 8. Pricing results",
        "",
        f"```json\n{json.dumps(report.get('pricing_results'), ensure_ascii=False, indent=2)}\n```",
        "",
        "## 9. Quantity results",
        "",
        f"```json\n{json.dumps(report.get('quantity_results'), ensure_ascii=False, indent=2)[:5000]}\n```",
        "",
        "## 10. Target/link results",
        "",
        f"```json\n{json.dumps(report.get('target_link_results'), ensure_ascii=False, indent=2)}\n```",
        "",
        "## 11. Order Intent results",
        "",
        f"```json\n{json.dumps(report.get('order_intent_results'), ensure_ascii=False, indent=2)}\n```",
        "",
        "## 12. Execution identity results",
        "",
        f"```json\n{json.dumps(report.get('execution_identity_results'), ensure_ascii=False, indent=2)}\n```",
        "",
        "## 13. Legacy isolation results",
        "",
        f"```json\n{json.dumps(report.get('legacy_isolation_results'), ensure_ascii=False, indent=2)}\n```",
        "",
        "## 14. Provider submit safety",
        "",
        f"```json\n{json.dumps(report.get('provider_submit_safety'), ensure_ascii=False, indent=2)}\n```",
        "",
        "## 15. Gen-0 regression",
        "",
        f"```json\n{json.dumps(report.get('gen0_regression'), ensure_ascii=False, indent=2)}\n```",
        "",
        "## 16. Shadow comparison",
        "",
        f"```json\n{json.dumps(report.get('shadow_comparison'), ensure_ascii=False, indent=2)[:6000]}\n```",
        "",
        "## 17. Test matrix",
        "",
        *matrix,
        "",
        "## 18. Test counts",
        "",
        f"```json\n{json.dumps(report.get('tests'), ensure_ascii=False, indent=2)}\n```",
        "",
        "## 19. Production invariants",
        "",
        f"Baseline: `{json.dumps(report.get('production_baseline'), ensure_ascii=False)}`",
        "",
        f"After: `{json.dumps(report.get('production_after'), ensure_ascii=False)}`",
        "",
        f"Unchanged: **{report.get('production_unchanged')}**",
        "",
        "## 20. Remaining blockers",
        "",
        *([f"- {b}" for b in (report.get("remaining_blockers") or [])] or ["- (none)"]),
        "",
        "## 21. Explicit statement",
        "",
        str(report.get("explicit_statement")),
        "",
        "## HARD STOP",
        "",
        "Do not enable Catalog in production. Do not start Phase 9S automatically.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    with catalog_readonly_connection(resolve_db_path()) as conn:
        report = run_phase9r(conn)
    report.setdefault("tests", {})
    report["tests"]["suite_previous"] = 444
    with open(
        "scripts/out_phase9r_telegram_catalog_e2e_shadow.json", "w", encoding="utf-8"
    ) as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open(
        "scripts/out_phase9r_telegram_catalog_e2e_shadow.md", "w", encoding="utf-8"
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
                "matrix_pass": all(
                    m.get("result") == "PASS" for m in (report.get("test_matrix") or [])
                ),
                "cohort_size": len(report.get("representative_services") or []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
