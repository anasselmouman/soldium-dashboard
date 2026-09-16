# -*- coding: utf-8 -*-
"""Phase 9I runner — expansion validation report (read-only)."""
from __future__ import annotations

import json

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9i_expansion_validation import run_phase9i


def _md(report: dict) -> str:
    lines = [
        "# Phase 9I — Expansion Validation",
        "",
        f"Generated: `{report.get('timestamp')}`",
        "",
        f"**Expansion decision:** `{report.get('verdict')}`",
        "",
        f"Production unchanged: **{report.get('production_unchanged')}**",
        "",
        "## Baseline",
        "",
        f"`{json.dumps(report.get('production_baseline'), ensure_ascii=False)}`",
        "",
        "## Contracts",
        "",
        f"- Execution identity ok: {(report.get('execution_identity_audit') or {}).get('ok')}",
        f"- Commercial contracts ok: {(report.get('contracts') or {}).get('ok')}",
        f"- Drift: {(report.get('drift_analysis') or {}).get('note')}",
        "",
        "## Shadow",
        "",
        f"`{json.dumps((report.get('shadow') or {}).get('aggregate'), ensure_ascii=False)}`",
        "",
        "## Recurring shadow patterns",
        "",
    ]
    for p in ((report.get("shadow") or {}).get("recurring_patterns") or [])[:15]:
        lines.append(
            f"- `{p.get('code')}` n={p.get('count')} impact={p.get('customer_impact')} — {p.get('likely_cause')}"
        )
    lines.extend(
        [
            "",
            "## Cutover blockers (not solved in 9I)",
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
            "No Wave 2 / Ready mass-publish / cutover / Catalog mutations.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    with catalog_transaction(resolve_db_path()) as conn:
        report = run_phase9i(conn)
    with open(
        "scripts/out_phase9i_expansion_validation.json", "w", encoding="utf-8"
    ) as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open(
        "scripts/out_phase9i_expansion_validation.md", "w", encoding="utf-8"
    ) as fh:
        fh.write(_md(report))
    slim = {
        "verdict": report.get("verdict"),
        "production_unchanged": report.get("production_unchanged"),
        "published": report.get("production_baseline", {}).get("published_services"),
        "publications": report.get("production_baseline", {}).get("publications"),
        "execution_ok": (report.get("execution_identity_audit") or {}).get("ok"),
        "contracts_ok": (report.get("contracts") or {}).get("ok"),
        "shadow": (report.get("shadow") or {}).get("aggregate"),
        "drift_count": (report.get("drift_analysis") or {}).get("drifted_count"),
        "rationale": (report.get("expansion_decision") or {}).get("rationale"),
    }
    print(json.dumps(slim, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
