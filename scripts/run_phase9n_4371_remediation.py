# -*- coding: utf-8 -*-
"""Phase 9N runner — 4371 fallback removal report."""
from __future__ import annotations

import json

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9n_4371_remediation import run_phase9n


def _md(report: dict) -> str:
    lines = [
        "# Phase 9N — Remove Hardcoded 4371 Fallback",
        "",
        f"Generated: `{report.get('timestamp')}`",
        "",
        f"**Verdict:** `{report.get('verdict')}`",
        "",
        f"Next step: `{report.get('next_step')}`",
        "",
        f"Production unchanged: **{report.get('production_unchanged')}**",
        "",
        "## Baseline",
        "",
        f"`{json.dumps(report.get('production_baseline'), ensure_ascii=False)}`",
        "",
        "## Old → New",
        "",
        f"- Old: {report.get('old_behavior')}",
        f"- New: {report.get('new_behavior')}",
        "",
        "## Compatibility",
        "",
        f"`{json.dumps(report.get('compatibility_strategy'), ensure_ascii=False)}`",
        "",
        "## Catalog contract",
        "",
        f"`{json.dumps(report.get('catalog_contract'), ensure_ascii=False)}`",
        "",
        "## Historical orders",
        "",
        f"`{json.dumps(report.get('historical_orders'), ensure_ascii=False)}`",
        "",
        "## References scan",
        "",
        f"`{json.dumps(report.get('references_scan'), ensure_ascii=False)[:2000]}`",
        "",
        "## Behavior regression",
        "",
        f"`{json.dumps(report.get('behavior_regression'), ensure_ascii=False)}`",
        "",
        "## Code diff summary",
        "",
    ]
    for row in report.get("code_diff_summary") or []:
        lines.append(
            f"- `{row.get('file')}`: {row.get('old')} → {row.get('new')} "
            f"(risk={row.get('risk')})"
        )
    lines.extend(
        [
            "",
            "## Rollback",
            "",
            f"`{json.dumps(report.get('rollback'), ensure_ascii=False)}`",
            "",
            "## HARD STOP",
            "",
            "Do not start Gen-0 / Telegram cutover / publish without review of next phase.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    with catalog_transaction(resolve_db_path()) as conn:
        report = run_phase9n(conn)
    report.setdefault("tests", {})
    report["tests"]["suite_previous"] = 413
    with open("scripts/out_phase9n_4371_remediation.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open("scripts/out_phase9n_4371_remediation.md", "w", encoding="utf-8") as fh:
        fh.write(_md(report))
    slim = {
        "verdict": report.get("verdict"),
        "next_step": report.get("next_step"),
        "production_unchanged": report.get("production_unchanged"),
        "runtime_hardcode_count": (report.get("references_scan") or {}).get(
            "runtime_hardcode_count"
        ),
        "behavior_ok": (report.get("behavior_regression") or {}).get("ok"),
        "orders_4371": (report.get("historical_orders") or {}).get(
            "orders_referencing_4371"
        ),
    }
    print(json.dumps(slim, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
