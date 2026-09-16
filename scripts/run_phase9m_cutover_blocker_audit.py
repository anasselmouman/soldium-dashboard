# -*- coding: utf-8 -*-
"""Phase 9M runner — READ-ONLY cutover blocker audit."""
from __future__ import annotations

import json

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9m_cutover_blocker_audit import run_phase9m


def _md(report: dict) -> str:
    lines = [
        "# Phase 9M — Cutover Blocker Audit (READ-ONLY)",
        "",
        f"Generated: `{report.get('timestamp')}`",
        "",
        f"**Verdict:** `{report.get('verdict')}`",
        "",
        f"**next_step:** `{report.get('next_step')}`",
        "",
        f"Production unchanged: **{report.get('production_unchanged')}**",
        "",
        f"Mutations: `{report.get('mutations')}`",
        "",
        "## Baseline",
        "",
        f"`{json.dumps(report.get('production_baseline'), ensure_ascii=False, default=str)}`",
        "",
        "## Blocker register (B1–B6)",
        "",
    ]
    for row in report.get("blocker_register") or []:
        lines.append(
            f"- **{row.get('id')}** [{row.get('severity')}] "
            f"{row.get('title')} — remediation `{row.get('remediation_status')}`"
        )
        lines.append(f"  - Risk: {row.get('risk')}")
        lines.append(f"  - Remediation: {row.get('remediation')}")
    dep = report.get("dependency_graph") or {}
    lines.extend(
        [
            "",
            "## Dependency graph",
            "",
            f"{dep.get('summary')}",
            "",
            "## Remediation phases (propose only — NOT executed)",
            "",
        ]
    )
    for p in report.get("remediation_phases") or []:
        lines.append(
            f"- **{p.get('phase')}** ({p.get('blocker')}): {p.get('title')} — "
            f"`execute={p.get('execute')}`"
        )
    decision = report.get("decision") or {}
    lines.extend(
        [
            "",
            "## Decision",
            "",
            f"- Plan: `{decision.get('cutover_remediation_plan')}`",
            f"- All blockers traced: **{decision.get('all_blockers_traced')}**",
            f"- Do not: `{decision.get('do_not')}`",
            "",
            "## Open decisions requiring review",
            "",
        ]
    )
    for d in decision.get("important_open_decisions") or []:
        lines.append(f"- {d}")
    lines.extend(
        [
            "",
            "## HARD STOP",
            "",
            str(report.get("hard_stop") or ""),
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    with catalog_transaction(resolve_db_path()) as conn:
        report = run_phase9m(conn)

    report.setdefault("tests", {})
    report["tests"]["suite_previous"] = 408

    with open(
        "scripts/out_phase9m_cutover_blocker_audit.json", "w", encoding="utf-8"
    ) as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
    with open(
        "scripts/out_phase9m_cutover_blocker_audit.md", "w", encoding="utf-8"
    ) as fh:
        fh.write(_md(report))

    slim = {
        "verdict": report.get("verdict"),
        "next_step": report.get("next_step"),
        "production_unchanged": report.get("production_unchanged"),
        "mutations": report.get("mutations"),
        "published": (report.get("production_baseline") or {}).get("published_services"),
        "scheduled_orders": (report.get("production_baseline") or {}).get(
            "scheduled_orders"
        ),
        "blocker_ids": [r.get("id") for r in (report.get("blocker_register") or [])],
        "suite_previous": (report.get("tests") or {}).get("suite_previous"),
        "open_decisions": (report.get("decision") or {}).get(
            "important_open_decisions"
        ),
    }
    print(json.dumps(slim, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
