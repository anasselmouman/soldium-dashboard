# -*- coding: utf-8 -*-
"""Phase 9L runner — expansion validation after Wave 2 (read-only)."""
from __future__ import annotations

import json

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9l_expansion_validation_2 import run_phase9l


def _md(report: dict) -> str:
    lines = [
        "# Phase 9L — Expansion Validation (post Wave 2)",
        "",
        f"Generated: `{report.get('timestamp')}`",
        "",
        f"**Verdict:** `{report.get('verdict')}`",
        "",
        f"**EXPANSION_DECISION:** `{report.get('EXPANSION_DECISION')}`",
        "",
        f"**CUTOVER_STATUS:** `{report.get('CUTOVER_STATUS')}`",
        "",
        f"**next_step:** `{report.get('next_step')}`",
        "",
        f"Production unchanged: **{report.get('production_unchanged')}**",
        "",
        "## Baseline",
        "",
        f"`{json.dumps(report.get('production_baseline'), ensure_ascii=False)}`",
        "",
        "## Three cohorts",
        "",
    ]
    cohorts = report.get("three_cohorts") or {}
    for name in ("original13", "wave1", "wave2"):
        c = cohorts.get(name) or {}
        lines.append(f"- **{name}**: count={c.get('count')} platforms={c.get('platforms')}")
    lines.extend(
        [
            "",
            "## Contracts",
            "",
            f"- Execution identity ok: {(report.get('execution_identity_audit') or {}).get('ok')}",
            f"- Commercial contracts ok: {(report.get('contracts') or {}).get('ok')}",
            f"- Cohort regression ok: {(report.get('cohort_regression') or {}).get('ok')}",
            f"- Isolation ok: {(report.get('isolation_architecture') or {}).get('ok')}",
            f"- Drift: {(report.get('drift_analysis') or {}).get('note')}",
            "",
            "## Shadow",
            "",
            f"`{json.dumps((report.get('shadow') or {}).get('aggregate'), ensure_ascii=False)}`",
            "",
            "## Recurring shadow patterns",
            "",
        ]
    )
    for p in ((report.get("shadow") or {}).get("recurring_patterns") or [])[:15]:
        lines.append(
            f"- `{p.get('code')}` n={p.get('count')} wave1+2={p.get('wave1_wave2_count')} "
            f"impact={p.get('customer_impact')} — {p.get('likely_cause')}"
        )
    lines.extend(
        [
            "",
            "## Cutover blockers (not solved in 9L; do not auto-block expansion)",
            "",
        ]
    )
    for b in (report.get("expansion_decision") or {}).get("cutover_blockers") or []:
        lines.append(f"- [{b.get('status')}] {b.get('item')}: {b.get('evidence')}")
    lines.extend(
        [
            "",
            "## Remediations (non-blocking for Catalog expansion)",
            "",
        ]
    )
    for r in (report.get("expansion_decision") or {}).get("remediations_non_blocking") or []:
        lines.append(f"- {r}")
    lines.extend(
        [
            "",
            "## HARD STOP",
            "",
            "No Wave 3 / Ready mass-publish / cutover / Catalog mutations. "
            "Continue only controlled expansion waves after review.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    with catalog_transaction(resolve_db_path()) as conn:
        report = run_phase9l(conn)

    report.setdefault("tests", {})
    report["tests"]["suite_previous"] = 404

    with open(
        "scripts/out_phase9l_expansion_validation_2.json", "w", encoding="utf-8"
    ) as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open(
        "scripts/out_phase9l_expansion_validation_2.md", "w", encoding="utf-8"
    ) as fh:
        fh.write(_md(report))

    slim = {
        "verdict": report.get("verdict"),
        "EXPANSION_DECISION": report.get("EXPANSION_DECISION"),
        "CUTOVER_STATUS": report.get("CUTOVER_STATUS"),
        "next_step": report.get("next_step"),
        "production_unchanged": report.get("production_unchanged"),
        "published": report.get("production_baseline", {}).get("published_services"),
        "publications": report.get("production_baseline", {}).get("publications"),
        "execution_ok": (report.get("execution_identity_audit") or {}).get("ok"),
        "contracts_ok": (report.get("contracts") or {}).get("ok"),
        "shadow": (report.get("shadow") or {}).get("aggregate"),
        "drift_count": (report.get("drift_analysis") or {}).get("drifted_count"),
        "cohorts": {
            k: (report.get("three_cohorts") or {}).get(k, {}).get("count")
            for k in ("original13", "wave1", "wave2")
        },
        "mutations": report.get("mutations"),
        "rationale": (report.get("expansion_decision") or {}).get("rationale"),
    }
    print(json.dumps(slim, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
