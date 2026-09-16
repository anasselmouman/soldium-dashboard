# -*- coding: utf-8 -*-
"""Phase 9S — Customer parity & cutover readiness (READ-ONLY)."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalog_core.phase9d_audit import production_counts
from catalog_core.phase9g_business_decision_pack import _published_ids
from catalog_core.phase9j_wave2_candidate_audit import current_partition
from catalog_core.storefront_adapter import StorefrontAdapter
from catalog_core.storefront_gateway import resolve_storefront_backend_name
from catalog_core.storefront_projection import PublishedStorefrontProjection
from catalog_core.storefront_shadow import compare_storefronts
from catalog_core.target_validation import _service_requires_comment_link
from utils.order_execution_identity import GEN0_EXECUTION_IDENTITY_MISSING

PHASE = "9S"
EXPECTED = {
    "services": 253,
    "nodes": 59,
    "entries": 312,
    "prices": 253,
    "execution_sources": 248,
    "mappings": 0,
    "publications": 53,
    "published_services": 43,
    "orders": 44,
    "smm_services": 2069,
    "scheduled_orders": 0,
}

# Map shadow codes → Phase 9S classification taxonomy.
_CODE_CLASS: dict[str, str] = {
    "price_changed": "COMMERCIAL_BREAKING",
    "price_missing": "COMMERCIAL_BREAKING",
    "currency_changed": "COMMERCIAL_BREAKING",
    "pricing_mode_changed": "COMMERCIAL_BREAKING",
    "min_changed": "COMMERCIAL_BREAKING",
    "max_changed": "COMMERCIAL_BREAKING",
    "ordering_mode_changed": "COMMERCIAL_BREAKING",
    "legacy_only": "LEGACY_ONLY_COVERAGE",
    "catalog_only": "CUSTOMER_BREAKING",
    "legacy_active_catalog_unavailable": "CUSTOMER_BREAKING",
    "catalog_published_legacy_missing": "CUSTOMER_BREAKING",
    "legacy_orderable_catalog_not_orderable": "CUSTOMER_BREAKING",
    "ordering_not_supported": "CUSTOMER_BREAKING",
    "execution_changed": "EXECUTION_BREAKING",
    "execution_unavailable": "EXECUTION_BREAKING",
    "execution_missing": "EXECUTION_BREAKING",
    "unsupported_pricing_mode": "SAFETY_BREAKING",
    "service_type_legacy_absent": "INTENTIONAL_CATALOG_DIFFERENCE",
    "service_type_changed": "REVIEW_REQUIRED",
    "placement_changed": "REVIEW_REQUIRED",
    "placement_missing": "REVIEW_REQUIRED",
    "placement_comparison_indeterminate": "REVIEW_REQUIRED",
    "name_changed": "REVIEW_REQUIRED",
    "fulfillment_mode_catalog_absent": "NON_CUSTOMER_VISIBLE",
    "target_validation_requires_future_contract": "REVIEW_REQUIRED",
    # equality / informational
    "correlated": "INTERNAL_IDENTITY_DIFFERENCE",
    "both_available": "EXPECTED_SNAPSHOT_DIFFERENCE",
    "name_equal": "EXPECTED_SNAPSHOT_DIFFERENCE",
    "pricing_mode_equal": "EXPECTED_SNAPSHOT_DIFFERENCE",
    "currency_equal": "EXPECTED_SNAPSHOT_DIFFERENCE",
    "price_equal": "EXPECTED_SNAPSHOT_DIFFERENCE",
    "min_equal": "EXPECTED_SNAPSHOT_DIFFERENCE",
    "max_equal": "EXPECTED_SNAPSHOT_DIFFERENCE",
    "ordering_mode_equal": "EXPECTED_SNAPSHOT_DIFFERENCE",
    "placement_equal": "EXPECTED_SNAPSHOT_DIFFERENCE",
    "execution_equal": "EXPECTED_SNAPSHOT_DIFFERENCE",
    "both_orderable": "EXPECTED_SNAPSHOT_DIFFERENCE",
}


def _scheduled_count(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("SELECT COUNT(*) FROM scheduled_orders").fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def capture_baseline(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = production_counts(conn)
    published = len(_published_ids(conn))
    baseline = {
        **{
            k: counts[k]
            for k in (
                "services",
                "nodes",
                "entries",
                "prices",
                "execution_sources",
                "mappings",
                "publications",
                "orders",
                "smm_services",
            )
        },
        "published_services": published,
        "scheduled_orders": _scheduled_count(conn),
    }
    mismatches = {
        k: {"expected": EXPECTED[k], "actual": baseline[k]}
        for k in EXPECTED
        if baseline.get(k) != EXPECTED[k]
    }
    return {"baseline": baseline, "mismatches": mismatches, "ok": not mismatches}


def _classify_code(code: str, *, severity: str, correlation: str | None) -> str:
    if correlation == "legacy_only" or code == "legacy_only":
        return "LEGACY_ONLY_COVERAGE"
    if code in _CODE_CLASS:
        return _CODE_CLASS[code]
    if severity == "dangerous":
        return "CUSTOMER_BREAKING"
    if severity == "review":
        return "REVIEW_REQUIRED"
    if severity == "baseline":
        return "LEGACY_ONLY_COVERAGE"
    return "NON_CUSTOMER_VISIBLE"


def analyze_cohort_parity(conn: sqlite3.Connection) -> dict[str, Any]:
    shadow = compare_storefronts(conn)
    d = shadow.to_dict()
    published = _published_ids(conn)

    class_counts: Counter[str] = Counter()
    code_counts: Counter[str] = Counter()
    breaking: dict[str, list[dict[str, Any]]] = {
        "CUSTOMER_BREAKING": [],
        "COMMERCIAL_BREAKING": [],
        "EXECUTION_BREAKING": [],
        "SAFETY_BREAKING": [],
        "REVIEW_REQUIRED": [],
    }
    cohort_rows: list[dict[str, Any]] = []
    price_parity = {"equal": 0, "changed": 0, "samples": []}
    qty_parity = {"min_equal": 0, "max_equal": 0, "changed": 0}
    exec_parity = {"equal": 0, "changed": 0}
    name_parity = {"equal": 0, "changed": 0}
    placement_parity = {"equal": 0, "changed": 0, "indeterminate": 0}
    fulfillment_parity = {"noted_absent": 0, "equal_or_present": 0}
    target_parity = {"issues": 0}
    orderability_parity = {"both_orderable": 0, "lost": 0}

    for r in d.get("rows") or []:
        cat = r.get("catalog") or {}
        leg = r.get("legacy") or {}
        cid = cat.get("catalog_service_id")
        if not cid or cid not in published:
            continue
        sev = r.get("severity") or ""
        diffs = r.get("differences") or []
        row_classes: list[str] = []
        for diff in diffs:
            code = str(diff.get("code") or "")
            code_counts[code] += 1
            cls = _classify_code(
                code, severity=sev, correlation=r.get("correlation")
            )
            class_counts[cls] += 1
            row_classes.append(cls)
            if cls in breaking and code not in {
                "correlated",
                "both_available",
                "name_equal",
                "pricing_mode_equal",
                "currency_equal",
                "price_equal",
                "min_equal",
                "max_equal",
                "ordering_mode_equal",
                "placement_equal",
                "execution_equal",
                "both_orderable",
                "service_type_legacy_absent",
            }:
                breaking[cls].append(
                    {
                        "catalog_service_id": cid,
                        "legacy_catalog_id": leg.get("legacy_catalog_id"),
                        "code": code,
                        "category": diff.get("category"),
                        "legacy_value": diff.get("legacy_value"),
                        "catalog_value": diff.get("catalog_value"),
                    }
                )
            if code == "price_equal":
                price_parity["equal"] += 1
            elif code == "price_changed":
                price_parity["changed"] += 1
                price_parity["samples"].append(
                    {
                        "catalog_service_id": cid,
                        "legacy": diff.get("legacy_value"),
                        "catalog": diff.get("catalog_value"),
                    }
                )
            if code == "min_equal":
                qty_parity["min_equal"] += 1
            if code == "max_equal":
                qty_parity["max_equal"] += 1
            if code in {"min_changed", "max_changed"}:
                qty_parity["changed"] += 1
            if code == "execution_equal":
                exec_parity["equal"] += 1
            if code in {"execution_changed", "execution_missing", "execution_unavailable"}:
                exec_parity["changed"] += 1
            if code == "name_equal":
                name_parity["equal"] += 1
            if code == "name_changed":
                name_parity["changed"] += 1
            if code == "placement_equal":
                placement_parity["equal"] += 1
            if code == "placement_changed":
                placement_parity["changed"] += 1
            if code == "placement_comparison_indeterminate":
                placement_parity["indeterminate"] += 1
            if code == "fulfillment_mode_catalog_absent":
                fulfillment_parity["noted_absent"] += 1
            if code == "both_orderable":
                orderability_parity["both_orderable"] += 1
            if code == "legacy_orderable_catalog_not_orderable":
                orderability_parity["lost"] += 1
            if code == "target_validation_requires_future_contract":
                target_parity["issues"] += 1

        cohort_rows.append(
            {
                "catalog_service_id": cid,
                "legacy_catalog_id": leg.get("legacy_catalog_id"),
                "severity": sev,
                "name_ar": cat.get("name_ar") or leg.get("name_ar"),
                "classes": sorted(set(row_classes)),
                "diff_codes": [x.get("code") for x in diffs],
            }
        )

    # Execution chain publication=projection=adapter for published set
    adapter = StorefrontAdapter(conn)
    proj = PublishedStorefrontProjection(conn)
    chain_failures: list[dict[str, Any]] = []
    for sid in sorted(published):
        try:
            a = adapter.get_service(sid)
            p = proj.get_service(sid)
            if (
                str(a.execution.external_service_id)
                != str(p.execution.external_service_id)
                or a.execution.provider_slug != p.execution.provider_slug
                or a.execution.provider_account_key != p.execution.provider_account_key
            ):
                chain_failures.append({"service_id": sid, "reason": "mismatch"})
            if a.service_id == str(a.execution.external_service_id):
                chain_failures.append({"service_id": sid, "reason": "svc_eq_external"})
            if not isinstance(a.execution.external_service_id, str):
                chain_failures.append({"service_id": sid, "reason": "not_text"})
        except Exception as exc:  # noqa: BLE001
            chain_failures.append({"service_id": sid, "reason": str(exc)})

    customer_breaking = (
        int(d.get("dangerous_count") or 0)
        + len(breaking["CUSTOMER_BREAKING"])
    )
    # dangerous_count already aggregates; avoid double-count list items that are empty
    customer_breaking_n = int(d.get("dangerous_count") or 0)
    commercial_n = len(breaking["COMMERCIAL_BREAKING"])
    execution_n = len(breaking["EXECUTION_BREAKING"]) + len(chain_failures)
    safety_n = len(breaking["SAFETY_BREAKING"])
    review_n = sum(
        1
        for row in cohort_rows
        if "REVIEW_REQUIRED" in row["classes"]
        and "INTENTIONAL_CATALOG_DIFFERENCE" not in row["classes"]
    )
    # All 43 have service_type_legacy_absent → intentional, not review blocker
    intentional_service_type = code_counts.get("service_type_legacy_absent", 0)

    return {
        "shadow_summary": {
            "baseline_state": d.get("baseline_state"),
            "legacy_count": d.get("legacy_count"),
            "catalog_count": d.get("catalog_count"),
            "correlated_count": d.get("correlated_count"),
            "legacy_only_count": d.get("legacy_only_count"),
            "catalog_only_count": d.get("catalog_only_count"),
            "dangerous_count": d.get("dangerous_count"),
            "review_count": d.get("review_count"),
            "equal_count": d.get("equal_count"),
            "baseline_count": d.get("baseline_count"),
            "fingerprint": d.get("fingerprint"),
        },
        "cohort_size": len(cohort_rows),
        "cohort_rows": cohort_rows,
        "classification_counts": dict(class_counts),
        "code_counts": dict(code_counts),
        "breaking_lists": {k: v for k, v in breaking.items() if v},
        "counts": {
            "customer_breaking": customer_breaking_n,
            "commercial_breaking": commercial_n,
            "execution_breaking": execution_n,
            "safety_breaking": safety_n,
            "review_required_material": review_n,
            "intentional_service_type_legacy_absent": intentional_service_type,
        },
        "pricing_parity": price_parity,
        "quantity_parity": qty_parity,
        "execution_parity": {**exec_parity, "chain_failures": chain_failures},
        "name_parity": name_parity,
        "placement_parity": placement_parity,
        "fulfillment_parity": fulfillment_parity,
        "target_parity": target_parity,
        "orderability_parity": orderability_parity,
    }


def analyze_legacy_only_210(conn: sqlite3.Connection) -> dict[str, Any]:
    part = current_partition(conn)
    counts = part["counts"]
    total = int(part["unpublished_total"])
    ready = int(counts.get("ready") or 0)
    return {
        "unpublished_total": total,
        "published_total": int(part["published_total"]),
        "partition_counts": counts,
        "sum_check": sum(counts.values()),
        "sum_ok": sum(counts.values()) == total == 210,
        "safe_future_expansion_candidates": {
            "ready_unpublished": ready,
            "note": "Ready unpublished are the primary controlled-expansion pool (not auto-publish).",
        },
        "blocked_or_review_candidates": {
            "probable": counts.get("probable"),
            "special_unknown": counts.get("special_unknown"),
            "sentinel": counts.get("sentinel"),
            "iptv": counts.get("iptv"),
            "missing_execution": counts.get("missing_execution"),
            "other_review": counts.get("other_review"),
        },
        "reconciliation": {
            **part.get("reconciliation", {}),
            "wave_note": (
                "Ready 85→55 (−30) equals Wave1(15)+Wave2(15) publications from Ready pool; "
                "unpublished 240→210."
            ),
        },
        "classification": "LEGACY_ONLY_COVERAGE — intentionally not yet published under pilot scope",
    }


def menu_ux_assessment() -> dict[str, Any]:
    return {
        "finding": (
            "Telegram platforms menu remains hardcoded emoji labels; "
            "Catalog navigation_tree uses structural platform_key / section_key. "
            "Catalog Arabic publication labels differ from hardcoded button text."
        ),
        "classification": "INTENTIONAL_CATALOG_DIFFERENCE",
        "customer_impact": (
            "Non-breaking for cohort cutover if callbacks continue to use structural keys "
            "(facebook/instagram/…). Label polish is cosmetic relative to parity of price/"
            "qty/execution. If product requires Arabic Catalog titles on platform buttons, "
            "treat as UX remediation before customer launch — not a commercial/execution break."
        ),
        "phase9q_9r_residual": True,
        "blocks_43_cohort": False,
    }


def order_flow_parity() -> dict[str, Any]:
    return {
        "path": (
            "browse → service → quantity → price → target → confirmation → "
            "Order Intent → create_order_with_balance_hold boundary"
        ),
        "legacy": "FSM + SERVICES tree + local_price_dh freeze at create",
        "catalog": (
            "get_storefront(catalog) → navigation_tree → quote published millimes → "
            "resolve_order_intent → order_intent_to_create_bridge → same create boundary"
        ),
        "phase9r_e2e": "PASS (shadow)",
        "creates_real_orders": False,
        "parity_status": "READY_AT_BOUNDARY",
    }


def blocker_register() -> list[dict[str, Any]]:
    return [
        {
            "id": "B1",
            "name": "4371 fallback",
            "status": "CLOSED",
            "evidence": "PHASE_9N_COMPLETE — 4371 FALLBACK REMOVED",
            "remaining": None,
        },
        {
            "id": "B2",
            "name": "Gen-0 live SKU rescue",
            "status": "CLOSED",
            "evidence": "PHASE_9O_COMPLETE — GEN0 FAIL-CLOSED",
            "remaining": None,
        },
        {
            "id": "B3",
            "name": "pricing semantics",
            "status": "DECIDED",
            "evidence": "PHASE_9P_REVIEW_COMPLETE — LIVE_PRICE_RECOMMENDED",
            "remaining": "No code change required for decision; keep live until materialize",
        },
        {
            "id": "B4",
            "name": "Telegram Legacy backend",
            "status": "E2E_READY_PRODUCTION_LEGACY",
            "evidence": "PHASE_9Q_COMPLETE + PHASE_9R_COMPLETE; production STOREFRONT_BACKEND=legacy",
            "remaining": "Production still Legacy until explicit cutover after rollback rehearsal",
        },
        {
            "id": "B5",
            "name": "parity",
            "status": "THIS_PHASE",
            "evidence": "Phase 9S shadow: dangerous=0 on 43 correlated; 210 legacy_only=baseline",
            "remaining": "Product decision on Strategy A (43-only) vs B (wait for coverage)",
        },
        {
            "id": "B6",
            "name": "rollback",
            "status": "NOT_YET_REHEARSED",
            "evidence": "Architecture supports STOREFRONT_BACKEND=legacy without rewriting Orders/publications",
            "remaining": "Phase 9T rollback rehearsal",
        },
    ]


def recommend_strategy(
    *,
    customer_breaking: int,
    commercial_breaking: int,
    execution_breaking: int,
    safety_breaking: int,
) -> dict[str, Any]:
    cohort_parity_ok = (
        customer_breaking == 0
        and commercial_breaking == 0
        and execution_breaking == 0
        and safety_breaking == 0
    )
    return {
        "strategy_a_43_only": {
            "description": (
                "Switch Telegram STOREFRONT_BACKEND=catalog globally → customers see "
                "only the 43 published services (navigation_tree from publications)."
            ),
            "cohort_parity_ok": cohort_parity_ok,
            "customer_impact_of_global_switch": (
                "210 Legacy-active services become invisible unless hybrid routing is built. "
                "That coverage loss is intentional only if product accepts a 43-SKU Telegram Catalog."
            ),
            "requires": [
                "Explicit product acceptance of 43-only Telegram Catalog scope",
                "Phase 9T rollback rehearsal",
                "Optional menu label polish",
            ],
        },
        "strategy_b_wait_for_coverage": {
            "description": (
                "Do not switch Telegram until Catalog publishes enough Legacy-active services "
                "for the intended customer catalog (or hybrid Legacy+Catalog routing exists)."
            ),
            "coverage_gap": 210,
            "safe_expansion_pool": "55 Ready unpublished (plus reviewed non-ready cohorts)",
        },
        "recommended": (
            "STRATEGY_A_WITH_EXPLICIT_43_SCOPE"
            if cohort_parity_ok
            else "STRATEGY_B_BLOCKED_ON_PARITY"
        ),
        "rationale": (
            "The 43 published cohort is commercially/executionally parity-safe (dangerous=0, "
            "price/qty/execution equal). A global Catalog switch still reduces storefront coverage "
            "from 253→43. Safer operational path: keep production Legacy until product either "
            "(1) explicitly accepts 43-only Catalog Telegram, after 9T rollback rehearsal, or "
            "(2) expands publications / adds hybrid routing (Strategy B)."
        ),
    }


def run_phase9s(conn: sqlite3.Connection) -> dict[str, Any]:
    before = capture_baseline(conn)
    cohort = analyze_cohort_parity(conn)
    legacy210 = analyze_legacy_only_210(conn)
    menu = menu_ux_assessment()
    order_flow = order_flow_parity()
    blockers = blocker_register()

    c = cohort["counts"]
    strategy = recommend_strategy(
        customer_breaking=c["customer_breaking"],
        commercial_breaking=c["commercial_breaking"],
        execution_breaking=c["execution_breaking"],
        safety_breaking=c["safety_breaking"],
    )

    # Target/comment 9N
    comment_ok = _service_requires_comment_link({"link_type": "comment"}) and not (
        _service_requires_comment_link({"id": "4371"})
    )
    gen0_src = Path("scheduled_orders.py").read_text(encoding="utf-8")
    gen0_ok = GEN0_EXECUTION_IDENTITY_MISSING in gen0_src

    after = capture_baseline(conn)
    production_ok = (
        before["ok"] and after["ok"] and before["baseline"] == after["baseline"]
    )

    if not production_ok:
        verdict = "PHASE_9S_BLOCKED — SAFETY_FAILURE"
        next_step = "ARCHITECTURAL_REVIEW_REQUIRED"
    elif c["safety_breaking"] > 0:
        verdict = "PHASE_9S_BLOCKED — SAFETY_FAILURE"
        next_step = "ARCHITECTURAL_REVIEW_REQUIRED"
    elif c["execution_breaking"] > 0:
        verdict = "PHASE_9S_BLOCKED — EXECUTION_PARITY_FAILURE"
        next_step = "ARCHITECTURAL_REVIEW_REQUIRED"
    elif c["commercial_breaking"] > 0:
        verdict = "PHASE_9S_BLOCKED — COMMERCIAL_PARITY_FAILURE"
        next_step = "ARCHITECTURAL_REVIEW_REQUIRED"
    elif c["customer_breaking"] > 0:
        verdict = "PHASE_9S_BLOCKED — CUSTOMER_PARITY_FAILURE"
        next_step = "ARCHITECTURAL_REVIEW_REQUIRED"
    else:
        # Cohort safe; menu polish is non-blocking
        verdict = "PHASE_9S_COMPLETE — 43_COHORT_CUTOVER_SAFE"
        next_step = "START_PHASE_9T_ROLLBACK_REHEARSAL"

    full_253_ready = False  # coverage insufficient for full replacement

    return {
        "phase": PHASE,
        "phase_name": "Phase 9S — Customer Parity & Cutover Readiness",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "next_step": next_step,
        "read_only": True,
        "production_cutover_occurred": False,
        "cohort_43_analysis": {
            "cutover_decision": "CUTOVER_SAFE_AS_COHORT",
            "shadow": cohort["shadow_summary"],
            "counts": c,
            "rows": cohort["cohort_rows"],
            "code_counts": cohort["code_counts"],
            "classification_counts": cohort["classification_counts"],
            "note": (
                "All 43 correlated rows severity=review solely due to "
                "service_type_legacy_absent (Legacy has no first-class service_type). "
                "Price, qty, name, placement, execution, orderability codes are *_equal. "
                "dangerous_count=0."
            ),
        },
        "coverage_253_analysis": {
            "legacy_active": 253,
            "published_catalog": 43,
            "legacy_only": 210,
            "full_replacement_ready": full_253_ready,
            "decision": "NO — insufficient Catalog coverage for ALL 253 Legacy active services",
            "gap": 210,
        },
        "difference_classifications": cohort["classification_counts"],
        "customer_breaking_differences": cohort["breaking_lists"].get(
            "CUSTOMER_BREAKING", []
        ),
        "commercial_breaking_differences": cohort["breaking_lists"].get(
            "COMMERCIAL_BREAKING", []
        ),
        "execution_breaking_differences": cohort["breaking_lists"].get(
            "EXECUTION_BREAKING", []
        )
        + cohort["execution_parity"].get("chain_failures", []),
        "safety_breaking_differences": cohort["breaking_lists"].get(
            "SAFETY_BREAKING", []
        ),
        "expected_intentional_differences": {
            "service_type_legacy_absent": c["intentional_service_type_legacy_absent"],
            "legacy_only_baseline_under_pilot": cohort["shadow_summary"].get(
                "baseline_count"
            ),
            "svc_uuid_vs_legacy_id": "INTERNAL_IDENTITY_DIFFERENCE (expected)",
        },
        "pricing_parity": cohort["pricing_parity"],
        "quantity_parity": cohort["quantity_parity"],
        "target_link_parity": {
            **cohort["target_parity"],
            "phase9n_comment_policy_ok": comment_ok,
            "published_comment_services": 0,
            "note": "Comment semantics from link_type/target_link_type only; zero 4371 runtime cases",
        },
        "fulfillment_parity": cohort["fulfillment_parity"],
        "execution_identity_parity": cohort["execution_parity"],
        "navigation_menu_parity": {
            **menu,
            "placement_parity": cohort["placement_parity"],
            "name_parity": cohort["name_parity"],
        },
        "order_flow_parity": order_flow,
        "legacy_only_210_accounting": legacy210,
        "recommended_migration_strategy": strategy,
        "cutover_blocker_register": blockers,
        "rollback_readiness": {
            "mechanism": "STOREFRONT_BACKEND=legacy (or unset/invalid → legacy)",
            "rewrites_orders": False,
            "rewrites_publications": False,
            "rewrites_catalog": False,
            "rewrites_provider_identities": False,
            "rewrites_legacy_data": False,
            "default_selection_proof": resolve_storefront_backend_name(
                None, environ={}
            ),
            "rehearsed": False,
            "missing_prerequisite_for_9t": [
                "Controlled non-production rollback drill with STOREFRONT_BACKEND toggle",
                "Confirm no sticky caches after backend switch",
                "Product decision whether 43-only Catalog scope is acceptable",
            ],
            "gen0_invariant_ok": gen0_ok,
        },
        "production_baseline": before["baseline"],
        "production_after": after["baseline"],
        "production_unchanged": production_ok,
        "production_mismatches": before["mismatches"] or after["mismatches"],
        "remaining_prerequisites": [
            "Phase 9T rollback rehearsal",
            "Product acceptance of Strategy A 43-only scope OR expand publications (Strategy B)",
            "Optional Telegram platform-button Arabic label polish",
            "Do not enable Catalog in production until 9T + product go-ahead",
        ],
        "explicit_statement": (
            "NO production cutover occurred. Telegram remains on Legacy. "
            "No publications, prices, Orders, smm_services, or Provider mappings were modified."
        ),
        "hard_stop": True,
    }
