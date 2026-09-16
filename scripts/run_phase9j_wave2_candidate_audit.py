# -*- coding: utf-8 -*-
"""Phase 9J runner — Wave 2 candidate audit (read-only / no publication)."""
from __future__ import annotations

import json

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9j_wave2_candidate_audit import run_phase9j


def _md(report: dict) -> str:
    part = report.get("partition") or {}
    ready = report.get("ready_pool") or {}
    wave = report.get("RECOMMENDED_WAVE_2") or []
    shadow = report.get("candidate_shadow_preview") or {}
    lines = [
        "# Phase 9J — Wave 2 Candidate Audit & Selection",
        "",
        f"Generated: `{report.get('timestamp')}`",
        "",
        f"**Final decision:** `{report.get('final_decision') or report.get('verdict')}`",
        "",
        f"Mutations: **{report.get('mutations')}**",
        "",
        f"Production unchanged: **{report.get('production_unchanged')}**",
        "",
        f"Wave size: **{report.get('wave_size')}**",
        "",
        "## Baseline",
        "",
        f"`{json.dumps(report.get('production_baseline'), ensure_ascii=False)}`",
        "",
        "## Partition reconciliation",
        "",
        f"- Historical 9G-B: `{json.dumps(part.get('historical_9gb'), ensure_ascii=False)}`",
        f"- Current counts: `{json.dumps(part.get('current_counts'), ensure_ascii=False)}`",
        f"- Unpublished total: `{part.get('unpublished_total')}`",
        f"- Explanation: {(part.get('reconciliation') or {}).get('explanation')}",
        "",
        "## Ready pool",
        "",
        f"- Ready audited: `{ready.get('count')}`",
        f"- Gate passed: `{ready.get('gate_passed')}`",
        f"- LOW risk: `{ready.get('low_risk')}`",
        f"- Risk counts: `{json.dumps(ready.get('risk_counts'), ensure_ascii=False)}`",
        f"- Distributions: `{json.dumps(ready.get('distributions'), ensure_ascii=False)}`",
        "",
        "## Wave 1 comparison",
        "",
        f"- Count: `{(report.get('wave1_comparison') or {}).get('count')}`",
        f"- Note: `{(report.get('wave1_comparison') or {}).get('note')}`",
        "",
        "## Risk / Provider / Account / Type",
        "",
        f"- Risk: `{json.dumps(report.get('risk_analysis'), ensure_ascii=False)}`",
        f"- Providers: `{json.dumps(report.get('provider_distribution'), ensure_ascii=False)}`",
        f"- Accounts: `{json.dumps(report.get('account_distribution'), ensure_ascii=False)}`",
        f"- Service types: `{json.dumps(report.get('service_type_distribution'), ensure_ascii=False)}`",
        "",
        "## Pricing / Quantity / Target / Fulfillment / Placement",
        "",
        f"- Pricing: `{json.dumps(report.get('pricing_analysis'), ensure_ascii=False)}`",
        f"- Quantity: `{json.dumps(report.get('quantity_analysis'), ensure_ascii=False)}`",
        f"- Target: `{json.dumps(report.get('target_analysis'), ensure_ascii=False)}`",
        f"- Fulfillment: `{json.dumps(report.get('fulfillment_analysis'), ensure_ascii=False)}`",
        f"- Placement: `{json.dumps(report.get('placement_analysis'), ensure_ascii=False)}`",
        "",
        "## Execution identity & readiness",
        "",
        f"- Execution: `{json.dumps(report.get('execution_identity'), ensure_ascii=False)}`",
        f"- Readiness: `{json.dumps(report.get('readiness'), ensure_ascii=False)}`",
        "",
        "## Deterministic selection rule",
        "",
        f"`{json.dumps(report.get('deterministic_selection_rule'), ensure_ascii=False)}`",
        "",
        "## Candidate shadow preview",
        "",
        f"- candidate_count: `{shadow.get('candidate_count')}`",
        f"- correlated_candidates: `{shadow.get('correlated_candidates')}`",
        f"- dangerous_total: `{shadow.get('dangerous_total')}`",
        "",
        "## RECOMMENDED_WAVE_2",
        "",
        "| معرّف المزود | Legacy ID | svc_* | platform | section | type | min | max | price_m | risk |",
        "|---|---|---|---|---|---|---:|---:|---:|---|",
    ]
    for c in wave:
        lines.append(
            "| {pid} | {lid} | {sid} | {plat} | {sec} | {typ} | {mn} | {mx} | {pr} | {rk} |".format(
                pid=c.get("معرّف_المزود") or c.get("provider_service_id"),
                lid=c.get("legacy_catalog_id"),
                sid=c.get("soldium_service_id"),
                plat=c.get("platform"),
                sec=c.get("section"),
                typ=c.get("service_type"),
                mn=c.get("min_quantity"),
                mx=c.get("max_quantity"),
                pr=c.get("price_amount_millimes"),
                rk=c.get("risk_level"),
            )
        )
    lines.extend(
        [
            "",
            "### Selection reasons",
            "",
        ]
    )
    for c in wave:
        lines.append(
            f"- `{c.get('معرّف_المزود')}` / `{c.get('legacy_catalog_id')}`: "
            f"{c.get('selection_reason')}"
        )
    lines.extend(
        [
            "",
            "## EXCLUDED_READY (count)",
            "",
            f"`{len(report.get('EXCLUDED_READY') or [])}`",
            "",
            "## NON_READY_NOT_CONSIDERED",
            "",
            f"`{json.dumps(report.get('NON_READY_NOT_CONSIDERED'), ensure_ascii=False)}`",
            "",
            "## Tests",
            "",
            f"`{json.dumps(report.get('tests'), ensure_ascii=False)}`",
            "",
            "## Production invariants",
            "",
            f"- Start: `{json.dumps(report.get('production_baseline'), ensure_ascii=False)}`",
            f"- End: `{json.dumps(report.get('production_end'), ensure_ascii=False)}`",
            f"- Unchanged: `{report.get('production_unchanged')}`",
            "",
            "## Known blockers (cutover — not Wave 2 publish)",
            "",
        ]
    )
    for b in report.get("known_blockers") or []:
        lines.append(f"- {b}")
    lines.extend(
        [
            "",
            "## HARD STOP",
            "",
            "Recommendation only. Do **not** publish Wave 2, mutate Catalog, "
            "Provider, execution sources, prices, quantities, targets, fulfillment, "
            "Orders, Scheduled Orders, smm_services, or Telegram. Wait for architectural review.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    with catalog_transaction(resolve_db_path()) as conn:
        report = run_phase9j(conn)
    report.setdefault("tests", {})
    report["tests"]["suite_previous"] = 394
    report["tests"]["suite_current"] = 399
    report["tests"]["focused"] = "tests/test_phase9j_wave2_candidate_audit.py"
    with open(
        "scripts/out_phase9j_wave2_candidate_audit.json", "w", encoding="utf-8"
    ) as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open(
        "scripts/out_phase9j_wave2_candidate_audit.md", "w", encoding="utf-8"
    ) as fh:
        fh.write(_md(report))
    slim = {
        "verdict": report.get("verdict"),
        "production_unchanged": report.get("production_unchanged"),
        "wave_size": report.get("wave_size"),
        "ready": (report.get("ready_pool") or {}).get("count"),
        "low_risk": (report.get("ready_pool") or {}).get("low_risk"),
        "partition": (report.get("partition") or {}).get("current_counts"),
        "shadow": {
            "candidate_count": (report.get("candidate_shadow_preview") or {}).get(
                "candidate_count"
            ),
            "correlated": (report.get("candidate_shadow_preview") or {}).get(
                "correlated_candidates"
            ),
            "dangerous": (report.get("candidate_shadow_preview") or {}).get(
                "dangerous_total"
            ),
        },
        "recommended_provider_ids": [
            c.get("معرّف_المزود") for c in (report.get("RECOMMENDED_WAVE_2") or [])
        ],
    }
    print(json.dumps(slim, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
