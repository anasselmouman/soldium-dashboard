# -*- coding: utf-8 -*-
"""Phase 9G-A runner — business decision pack (read-only)."""
from __future__ import annotations

import json

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9d_audit import production_counts
from catalog_core.phase9g_business_decision_pack import build_decision_pack


def _md(pack: dict) -> str:
    ex = pack["executive_summary"]
    lines = [
        "# Phase 9G-A — Business Decision Pack",
        "",
        f"Generated: `{pack['timestamp']}`",
        "",
        f"**Verdict:** `{pack['verdict']}`",
        "",
        f"**Mutations:** {pack['mutations']}",
        "",
        "## 9G-A Executive Summary",
        "",
        ex["headline"],
        "",
        f"- Published: **{ex['published']}**",
        f"- Remaining unpublished: **{ex['remaining_unpublished']}**",
        f"- PROBABLE: **{ex['probable']}**",
        f"- SPECIAL/UNKNOWN: **{ex['special_unknown']}**",
        f"- Sentinel: **{ex['sentinel']}**",
        f"- IPTV: **{ex['iptv']}**",
        f"- Missing execution: **{ex['missing_execution']}**",
        f"- Other review: **{ex['other_review']}**",
        f"- Ready (technical): **{ex['ready_total']}** (business-ready **{ex['business_ready']}**, tech-only **{ex['technically_ready_only']}**)",
        "",
        "## 9G-B Cohort Inventory",
        "",
        f"Exclusive partition (must sum to remaining unpublished): "
        f"`{json.dumps(pack.get('cohort_inventory', {}), ensure_ascii=False)}`",
        "",
        "## 9G-B Business Risk Matrix",
        "",
        "| Cohort | Count | Semantic | Technical | Customer risk | Decision |",
        "|--------|------:|----------|-----------|---------------|----------|",
    ]
    for m in pack["business_risk_matrix"]:
        lines.append(
            f"| {m['cohort']} | {m['count']} | {m['semantic_certainty']} | "
            f"{m['technical_readiness']} | {m['customer_risk']} | {m['decision']} |"
        )

    lines.extend(["", "## 9G-C PROBABLE", ""])
    lines.append(pack["probable"]["conservative_recommendation"])
    lines.append("")
    for c in pack["probable"]["cohorts"]:
        lines.append(
            f"### `{c['cohort_id']}` (n={c['count']}) → recommend **{c['recommendation']}**"
        )
        lines.append(f"- Proposed type: `{c['proposed_type']}`")
        lines.append(f"- Why probable: {c['why_probable']}")
        lines.append(f"- CX risk: {c['customer_experience']}")
        if c["examples"]:
            e = c["examples"][0]
            lines.append(
                f"- Example: Legacy `{e['legacy_catalog_id']}` / `{e['soldium_service_id']}` "
                f"— {e['name_ar'][:60]}"
            )
        lines.append("")

    lines.extend(["", "## 9G-D SPECIAL/UNKNOWN", ""])
    lines.append(pack["special_unknown"]["conservative_recommendation"])
    lines.append(f"By classification (service counts): {pack['special_unknown']['by_classification']}")
    lines.append("")
    lines.append("| Cohort | n | Classification | CX |")
    lines.append("|--------|--:|----------------|----|")
    for c in pack["special_unknown"]["cohorts"]:
        lines.append(
            f"| `{c['cohort_id']}` | {c['count']} | {c['classification']} | "
            f"{c['customer_experience']['customer_experience_risk']} |"
        )
    lines.append("")

    lines.extend(["", "## 9G-E Sentinel", ""])
    lines.append(pack["sentinel"]["conservative_recommendation"])
    lines.append(
        f"Actual INT32 sentinel: {pack['sentinel']['actual_sentinel_count']}; "
        f"bridge-only: {pack['sentinel']['bridge_only_count']}"
    )
    lines.append("")
    lines.append("| Legacy | Catalog max | Legacy max | Kind | Rec |")
    lines.append("|--------|------------:|-----------:|------|-----|")
    for s in pack["sentinel"]["services"]:
        lines.append(
            f"| {s['legacy_catalog_id']} | {s['max_quantity']} | {s['legacy_max_qty']} | "
            f"{s['sentinel_kind']} | {s['recommendation']} |"
        )
    lines.append("")

    lines.extend(["", "## 9G-F IPTV", ""])
    lines.append(pack["iptv"]["conservative_recommendation"])
    for c in pack["iptv"]["cohorts"]:
        lines.append(
            f"- `{c['cohort_id']}` n={c['count']} → **{c['recommendation']}** "
            f"(pricing={c['pricing_mode']}; fixed_package_proven={c['fixed_package_proven']})"
        )
    lines.append("")

    lines.extend(["", "## 9G-G Missing execution", ""])
    lines.append(pack["missing_execution"]["conservative_recommendation"])
    for s in pack["missing_execution"]["services"]:
        lines.append(
            f"- Legacy `{s['legacy_catalog_id']}` {s['platform']}/{s['section']}: "
            f"account=`{s['provider_account_key']}` external=`{s['external_service_id']}` "
            f"→ **{s['recommendation']}**"
        )
    lines.append("")

    lines.extend(["", "## 9G-H Other review", ""])
    lines.append(pack["other_review"]["conservative_recommendation"])
    for g in pack["other_review"]["groups"]:
        lines.append(f"- `{g['reason']}`: n={g['count']} — {g['required_action']}")
    lines.append("")

    lines.extend(["", "## 9G-I Ready expansion pool", ""])
    r = pack["ready_expansion_pool"]
    lines.append(r["note"])
    lines.append(
        f"Total Ready={r['count']}; BUSINESS_READY={r['business_ready_count']}; "
        f"TECHNICALLY_READY_ONLY={r['technically_ready_only_count']}"
    )
    lines.append("")

    lines.extend(["", "## 9G-J Cross-cohort risks", ""])
    for x in pack["cross_cohort_risks"]:
        lines.append(f"- {x}")
    lines.append("")

    lines.extend(["", "## 9G-K Recommended decision set", ""])
    lines.append("```json")
    lines.append(json.dumps(pack["recommended_decision_set"], ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")

    lines.extend(["", "## Next expansion cohort (DO NOT PUBLISH)", ""])
    np = pack["next_expansion_cohort"]
    lines.append(f"Size: {np['recommended_size']}")
    lines.append("")
    lines.append("| Legacy | svc_* | platform | section | type |")
    lines.append("|--------|-------|----------|---------|------|")
    for c in np["candidates"]:
        lines.append(
            f"| {c['legacy_catalog_id']} | `{c['soldium_service_id']}` | "
            f"{c['platform']} | {c['section']} | {c['service_type']} |"
        )
    lines.append("")

    lines.extend(["", "## Cutover blockers (not solved here)", ""])
    for b in pack["cutover_blockers"]:
        lines.append(f"- {b}")
    lines.append("")

    lines.extend(["", "## 9G-L Approval checklist", ""])
    for a in pack["approval_checklist"]:
        lines.append(
            f"- [ ] {a['question']} *(recommended: {a['default_recommendation']})*"
        )
    lines.append("")
    lines.append("---")
    lines.append("HARD STOP — no production mutations. Await human approval.")
    return "\n".join(lines)


def main() -> None:
    with catalog_transaction(resolve_db_path()) as conn:
        before = production_counts(conn)
        pack = build_decision_pack(conn)
        after = production_counts(conn)
        pack["production_counts_before"] = before
        pack["production_counts_after"] = after
        pack["production_unchanged"] = before == after
        if before != after:
            pack["verdict"] = "BUSINESS DECISION PACK BLOCKED — DATA/EVIDENCE INSUFFICIENT"

        # Shrink examples in JSON if huge — keep structure
        with open(
            "scripts/out_phase9g_business_decision_pack.json",
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(pack, fh, ensure_ascii=False, indent=2)
        with open(
            "scripts/out_phase9g_business_decision_pack.md",
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write(_md(pack))

        slim = {
            "verdict": pack["verdict"],
            "executive_summary": pack["executive_summary"],
            "production_unchanged": pack["production_unchanged"],
            "risk_matrix": pack["business_risk_matrix"],
            "next_pilot_size": pack["next_expansion_cohort"]["recommended_size"],
            "next_pilot_legacy_ids": [
                c["legacy_catalog_id"]
                for c in pack["next_expansion_cohort"]["candidates"]
            ],
            "approval_checklist_ids": [
                a["id"] for a in pack["approval_checklist"]
            ],
        }
        print(json.dumps(slim, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
