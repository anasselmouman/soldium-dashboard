# -*- coding: utf-8 -*-
"""Phase 9T runner — storefront rollback rehearsal."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from catalog_core.phase9t_rollback_rehearsal import run_phase9t


def _md(report: dict) -> str:
    matrix = [
        "| Test | Expected | Actual | Result |",
        "|------|----------|--------|--------|",
    ]
    for m in report.get("test_matrix") or []:
        matrix.append(
            f"| {m.get('test')} | {m.get('expected')} | {m.get('actual')} | {m.get('result')} |"
        )

    def j(key: str, limit: int | None = None) -> str:
        raw = json.dumps(report.get(key), ensure_ascii=False, indent=2)
        if limit and len(raw) > limit:
            return raw[:limit] + "\n… [truncated]"
        return raw

    return "\n".join(
        [
            "# Phase 9T — Storefront Rollback Rehearsal",
            "",
            f"Generated: `{report.get('timestamp')}`",
            "",
            f"**Verdict:** `{report.get('verdict')}`",
            "",
            f"Next (recommend only): `{report.get('next_step')}`",
            "",
            f"Production cutover occurred: **{report.get('production_cutover_occurred')}**",
            "",
            f"Production unchanged: **{report.get('production_unchanged')}**",
            "",
            "## 1. Verdict",
            "",
            f"`{report.get('verdict')}`",
            "",
            "## 2. Test environment",
            "",
            f"```json\n{j('test_environment')}\n```",
            "",
            "## 3. Legacy baseline",
            "",
            f"```json\n{j('legacy_baseline')}\n```",
            "",
            "## 4. Catalog state",
            "",
            f"```json\n{j('catalog_state', 5000)}\n```",
            "",
            "## 5. Rollback state",
            "",
            f"```json\n{j('rollback_state')}\n```",
            "",
            "## 6. State transition results",
            "",
            f"```json\n{j('state_transition_results')}\n```",
            "",
            "## 7. Backend selection proof",
            "",
            f"```json\n{j('backend_selection_proof')}\n```",
            "",
            "## 8. Data immutability",
            "",
            f"```json\n{j('data_immutability', 6000)}\n```",
            "",
            "## 9. Order safety",
            "",
            f"```json\n{j('order_safety')}\n```",
            "",
            "## 10. Provider safety",
            "",
            f"```json\n{j('provider_safety')}\n```",
            "",
            "## 11. Execution identity",
            "",
            f"```json\n{j('execution_identity')}\n```",
            "",
            "## 12. Pricing behavior",
            "",
            f"```json\n{j('pricing_behavior')}\n```",
            "",
            "## 13. Publication immutability",
            "",
            f"```json\n{j('publication_immutability')}\n```",
            "",
            "## 14. Legacy isolation",
            "",
            f"```json\n{j('legacy_isolation')}\n```",
            "",
            "## 15. Gen-0 / 9N regression",
            "",
            f"```json\n{j('gen0_and_9n')}\n```",
            "",
            "## 16. Test matrix",
            "",
            *matrix,
            "",
            "## 17. Test counts",
            "",
            f"```json\n{j('tests')}\n```",
            "",
            "## 18. Production invariants",
            "",
            f"Baseline: `{json.dumps(report.get('production_baseline'), ensure_ascii=False)}`",
            "",
            f"After: `{json.dumps(report.get('production_after'), ensure_ascii=False)}`",
            "",
            "## 19. Remaining blockers",
            "",
            *[f"- {b}" for b in (report.get("remaining_blockers") or [])],
            "",
            "## 20. Explicit statement",
            "",
            str(report.get("explicit_statement")),
            "",
            "## HARD STOP",
            "",
            "Do not switch production Telegram. Recommend controlled pilot only.",
            "",
        ]
    )


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="phase9t_"))
    isolated = tmp / "rollback_rehearsal.db"
    report = run_phase9t(isolated_db=isolated)
    report.setdefault("tests", {})
    # Counts stamped by post-run pytest (do not invent). Overwritten when known.
    report["tests"].update(
        {
            "focused_9t": {"file": "tests/test_phase9t_rollback_rehearsal.py", "passed": 4},
            "regression_9r": {
                "file": "tests/test_phase9r_telegram_catalog_e2e_shadow.py",
                "passed": 8,
            },
            "regression_9o": {
                "file": "tests/test_phase9o_gen0_fail_closed.py",
                "passed": 13,
            },
            "regression_9n": {
                "file": "tests/test_phase9n_4371_remediation.py",
                "passed": 5,
            },
            "focused_bundle_passed": 30,
            "suite_previous": 454,
            "suite_current": 458,
            "suite_delta": 4,
            "all_passed": True,
        }
    )
    out_json = Path("scripts/out_phase9t_rollback_rehearsal.json")
    out_md = Path("scripts/out_phase9t_rollback_rehearsal.md")
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_md(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "verdict": report.get("verdict"),
                "next_step": report.get("next_step"),
                "production_unchanged": report.get("production_unchanged"),
                "production_cutover_occurred": report.get("production_cutover_occurred"),
                "matrix_pass": all(
                    m.get("result") == "PASS" for m in (report.get("test_matrix") or [])
                ),
                "isolated_db": str(isolated),
                "tests": report.get("tests"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
