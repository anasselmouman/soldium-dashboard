# -*- coding: utf-8 -*-
"""Phase 9O runner — Gen-0 fail-closed report."""
from __future__ import annotations

import json

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9o_gen0_fail_closed import run_phase9o


def _md(report: dict) -> str:
    lines = [
        "# Phase 9O — Gen-0 Fail-Closed",
        "",
        f"Generated: `{report.get('timestamp')}`",
        "",
        f"**Verdict:** `{report.get('verdict')}`",
        "",
        f"Next step (recommend only): `{report.get('next_step')}`",
        "",
        f"Production unchanged: **{report.get('production_unchanged')}**",
        "",
        "## 1. Phase name",
        "",
        str(report.get("phase_name")),
        "",
        "## 2. Verdict",
        "",
        f"`{report.get('verdict')}`",
        "",
        "## 3. Files changed",
        "",
    ]
    for f in report.get("files_changed") or []:
        lines.append(f"- `{f}`")
    lines.extend(
        [
            "",
            "## 4. Exact execution paths audited",
            "",
            f"```json\n{json.dumps(report.get('execution_paths_audited'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## 5. Gen-0 definition",
            "",
            str(report.get("gen0_definition")),
            "",
            "## 6. Gen-1 definition",
            "",
            str(report.get("gen1_definition")),
            "",
            "## 7. Legacy lookup findings",
            "",
            f"```json\n{json.dumps(report.get('legacy_lookup_findings'), ensure_ascii=False, indent=2)[:8000]}\n```",
            "",
            "## 8. Fail-closed behavior",
            "",
            f"```json\n{json.dumps(report.get('fail_closed_behavior'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## 9. Test results",
            "",
            f"```json\n{json.dumps(report.get('tests'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## 10. Static search results",
            "",
            f"```json\n{json.dumps(report.get('static_search'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## 11. Production invariant verification",
            "",
            f"Baseline: `{json.dumps(report.get('production_baseline'), ensure_ascii=False)}`",
            "",
            f"After: `{json.dumps(report.get('production_after'), ensure_ascii=False)}`",
            "",
            f"Mismatches: `{json.dumps(report.get('production_mismatches'), ensure_ascii=False)}`",
            "",
            "## 12. Remaining cutover blockers",
            "",
        ]
    )
    for b in report.get("remaining_cutover_blockers") or []:
        lines.append(f"- {b}")
    lines.extend(
        [
            "",
            "## 13. Explicit non-goals",
            "",
            "No pricing / Telegram backend / E2E / parity / rollback / cutover / Gen-0 migration /",
            "smm_services / Orders / Provider mappings / publication work was performed.",
            "",
            f"```json\n{json.dumps(report.get('explicit_non_goals'), ensure_ascii=False, indent=2)}\n```",
            "",
            "## HARD STOP",
            "",
            "Do **not** start Phase 9P in this phase. Report only.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    with catalog_transaction(resolve_db_path()) as conn:
        report = run_phase9o(conn)
    report.setdefault("tests", {})
    report["tests"]["suite_previous"] = 418
    with open("scripts/out_phase9o_gen0_fail_closed.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open("scripts/out_phase9o_gen0_fail_closed.md", "w", encoding="utf-8") as fh:
        fh.write(_md(report))
    slim = {
        "verdict": report.get("verdict"),
        "next_step": report.get("next_step"),
        "production_unchanged": report.get("production_unchanged"),
        "submit_job_order_calls_lookup": (report.get("static_search") or {}).get(
            "submit_job_order_calls_lookup"
        ),
        "ensure_provider_order_ref_calls_lookup": (report.get("static_search") or {}).get(
            "ensure_provider_order_ref_calls_lookup"
        ),
        "baseline": report.get("production_baseline"),
    }
    print(json.dumps(slim, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
