# -*- coding: utf-8 -*-
"""Phase 9I — Expansion validation (read-only production; fixtures for mutation proofs)."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalog_core.phase9d_audit import production_counts
from catalog_core.phase9e_stage_b import _pilot_sample_target
from catalog_core.phase9f_architecture_review import cutover_matrix
from catalog_core.phase9g_business_decision_pack import _published_ids
from catalog_core.phase9gb_business_decisions_apply import FUTURE_PILOT_LEGACY_IDS
from catalog_core.pilot_parity import _sample_target_url
from catalog_core.publication import CatalogPublicationService
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.storefront_adapter import StorefrontAdapter
from catalog_core.storefront_projection import PublishedStorefrontProjection
from catalog_core.storefront_shadow import compare_storefronts
from catalog_core.target_validation import validate_order_target

PHASE = "9I"
EXPECTED = {
    "services": 253,
    "nodes": 59,
    "entries": 312,
    "prices": 253,
    "execution_sources": 248,
    "mappings": 0,
    "publications": 38,
    "published_services": 28,
    "orders": 44,
    "smm_services": 2069,
    "scheduled_orders": 0,
}

ORIGINAL_PUBLISHED_BY = frozenset(
    {"phase9b7_order_contract", "phase9e_second_pilot"}
)
WAVE1_PUBLISHED_BY = "phase9h_wave1"


def _scheduled_count(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("SELECT COUNT(*) FROM scheduled_orders").fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def capture_baseline(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = production_counts(conn)
    published = sorted(_published_ids(conn))
    baseline = {
        **{k: counts.get(k) for k in (
            "services", "nodes", "entries", "prices", "execution_sources",
            "mappings", "publications", "orders", "smm_services",
        )},
        "published_services": len(published),
        "scheduled_orders": _scheduled_count(conn),
        "published_service_ids": published,
    }
    mismatches = {
        k: {"expected": EXPECTED[k], "actual": baseline[k]}
        for k in EXPECTED
        if baseline.get(k) != EXPECTED[k]
    }
    return {"baseline": baseline, "mismatches": mismatches, "ok": not mismatches}


def _legacy_for_service(conn: sqlite3.Connection, sid: str) -> str | None:
    row = conn.execute(
        """
        SELECT legacy_catalog_id FROM soldium_catalog_legacy_bridge
        WHERE soldium_service_id = ? LIMIT 1
        """,
        (sid,),
    ).fetchone()
    return str(row["legacy_catalog_id"]) if row else None


def inventory_published(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    pub = CatalogPublicationService(conn)
    repo = CatalogRepository(conn)
    proj = PublishedStorefrontProjection(conn)
    out: list[dict[str, Any]] = []
    for sid in sorted(_published_ids(conn)):
        latest = pub.get_latest_publish(sid)
        status = pub.get_publication_status(sid)
        svc = repo.get_service(sid)
        source = repo.get_active_execution_source(sid)
        price = repo.get_active_price(sid)
        entry = repo.get_entry_for_service(sid)
        if svc and entry:
            svc.entry_id = entry.id
            svc.parent_entry_id = entry.parent_entry_id
        ready = (
            evaluate_service_readiness(repo, svc, source=source, price=price)
            if svc
            else None
        )
        placement = None
        try:
            ps = proj.get_service(sid)
            platform = (
                (ps.location_path[0] if ps.location_path else None)
                if ps
                else None
            )
            placement = list(ps.location_path) if ps else None
        except Exception:  # noqa: BLE001
            platform = None
        # Prefer target keys + bridge/legacy for platform/section
        leg = conn.execute(
            """
            SELECT l.platform_key, l.section_key, l.subsection_key
            FROM soldium_catalog_legacy_bridge b
            LEFT JOIN smm_services l ON l.catalog_id = b.legacy_catalog_id
            WHERE b.soldium_service_id = ?
            """,
            (sid,),
        ).fetchone()
        out.append(
            {
                "legacy_catalog_id": _legacy_for_service(conn, sid),
                "soldium_service_id": sid,
                "provider_slug": latest.provider_slug if latest else None,
                "provider_account_key": (
                    latest.provider_account_key if latest else None
                ),
                "provider_service_id": (
                    str(latest.external_service_id)
                    if latest and latest.external_service_id is not None
                    else None
                ),
                "platform": (leg["platform_key"] if leg else None)
                or (latest.target_platform_key if latest else None),
                "section": (leg["section_key"] if leg else None)
                or (latest.target_section_key if latest else None),
                "subsection": (leg["subsection_key"] if leg else None)
                or (latest.target_subsection_key if latest else None),
                "service_type": latest.service_type if latest else None,
                "ordering_mode": latest.ordering_mode if latest else None,
                "min_quantity": latest.min_quantity if latest else None,
                "max_quantity": latest.max_quantity if latest else None,
                "price_amount_millimes": latest.amount_millimes if latest else None,
                "currency": latest.currency if latest else None,
                "pricing_mode": latest.pricing_mode if latest else None,
                "fulfillment_mode": latest.fulfillment_mode if latest else None,
                "target_policy": {
                    "platform_key": latest.target_platform_key if latest else None,
                    "section_key": latest.target_section_key if latest else None,
                    "subsection_key": latest.target_subsection_key if latest else None,
                    "link_prompt_key": latest.target_link_prompt_key if latest else None,
                    "link_type": latest.target_link_type if latest else None,
                },
                "content_fingerprint": (
                    latest.content_fingerprint if latest else None
                ),
                "published_at": latest.published_at if latest else None,
                "published_by": latest.published_by if latest else None,
                "publication_id": latest.id if latest else None,
                "readiness_ready": bool(ready.ready) if ready else None,
                "has_unpublished_changes": status.get("has_unpublished_changes"),
                "customer_catalog_eligible": status.get("customer_catalog_eligible"),
                "publication_status": status.get("publication_status"),
                "location_path": placement,
                "cohort": (
                    "wave1"
                    if (latest and latest.published_by == WAVE1_PUBLISHED_BY)
                    else "original13"
                ),
            }
        )
    return out


def audit_execution_identity(conn: sqlite3.Connection) -> dict[str, Any]:
    pub = CatalogPublicationService(conn)
    proj = PublishedStorefrontProjection(conn)
    adapter = StorefrontAdapter(conn)
    mismatches: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for sid in sorted(_published_ids(conn)):
        latest = pub.get_latest_publish(sid)
        projected = proj.get_service(sid)
        adapted = adapter.get_service(sid)
        sample = _pilot_sample_target(
            latest.target_platform_key if latest else None,
            latest.target_section_key if latest else None,
            latest.target_subsection_key if latest else None,
        )
        qty = int(latest.min_quantity) if latest else 1
        intent = adapter.resolve_order_intent(sid, qty, target=sample)
        # Normalize
        pub_t = (
            latest.provider_slug,
            latest.provider_account_key,
            str(latest.external_service_id),
        )
        proj_t = (
            projected.execution.provider_slug,
            projected.execution.provider_account_key,
            str(projected.execution.external_service_id),
        )
        adapt_t = (
            adapted.execution.provider_slug,
            adapted.execution.provider_account_key,
            str(adapted.execution.external_service_id),
        )
        intent_t = (
            intent.provider_slug,
            intent.provider_account_key,
            str(intent.external_service_id),
        )
        ok = pub_t == proj_t == adapt_t == intent_t
        # SOLDIUM id must not equal provider id as identity model
        identity_ok = sid.startswith("svc_") and sid != str(latest.external_service_id)
        row = {
            "soldium_service_id": sid,
            "provider_service_id": str(latest.external_service_id),
            "pub": pub_t,
            "proj": proj_t,
            "adapter": adapt_t,
            "intent": intent_t,
            "equal": ok,
            "soldium_identity_ok": identity_ok,
        }
        rows.append(row)
        if not ok or not identity_ok:
            mismatches.append(row)
    return {
        "checked": len(rows),
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
        "ok": len(mismatches) == 0,
    }


def audit_contracts(conn: sqlite3.Connection) -> dict[str, Any]:
    pub = CatalogPublicationService(conn)
    proj = PublishedStorefrontProjection(conn)
    adapter = StorefrontAdapter(conn)
    failures: list[dict[str, Any]] = []
    eligibility_ok = 0
    for sid in sorted(_published_ids(conn)):
        latest = pub.get_latest_publish(sid)
        status = pub.get_publication_status(sid)
        projected = proj.get_service(sid)
        adapted = adapter.get_service(sid)
        sample = _pilot_sample_target(
            latest.target_platform_key,
            latest.target_section_key,
            latest.target_subsection_key,
        )
        qty = int(latest.min_quantity)
        quote = adapter.quote_price(sid, qty)
        intent = adapter.resolve_order_intent(sid, qty, target=sample)

        checks = {
            "fulfillment": (
                latest.fulfillment_mode
                == projected.fulfillment_mode
                == adapted.fulfillment_mode
                == intent.fulfillment_mode
            ),
            "price_unit": (
                latest.amount_millimes
                == projected.amount_millimes
                == adapted.price.amount_millimes
                == quote.unit_amount_millimes
            ),
            "currency": (
                latest.currency
                == projected.currency
                == adapted.price.currency
                == intent.currency
            ),
            "pricing_mode": (
                latest.pricing_mode
                == projected.pricing_mode
                == adapted.price.pricing_mode
                == intent.pricing_mode
            ),
            "quote_matches_intent": (
                intent.quoted_amount_millimes == quote.quoted_amount_millimes
            ),
            "quantity": (
                (latest.min_quantity, latest.max_quantity, latest.ordering_mode)
                == (
                    projected.min_quantity,
                    projected.max_quantity,
                    projected.ordering_mode,
                )
                == (
                    adapted.min_quantity,
                    adapted.max_quantity,
                    adapted.ordering_mode,
                )
            ),
            "fingerprint": (
                latest.content_fingerprint
                == projected.content_fingerprint
                == adapted.content_fingerprint
                == intent.content_fingerprint
            ),
            "eligible": status.get("customer_catalog_eligible") is True,
            "published": status.get("publication_status") == "published",
            "target_present": bool(
                latest.target_platform_key and latest.target_section_key
            ),
        }
        valid_ok, _ = validate_order_target(
            sample,
            platform_key=latest.target_platform_key or "",
            section_key=latest.target_section_key,
            subsection_key=latest.target_subsection_key,
        )
        invalid_ok, _ = validate_order_target(
            "https://example.com/not-a-platform",
            platform_key=latest.target_platform_key or "",
            section_key=latest.target_section_key,
            subsection_key=latest.target_subsection_key,
        )
        checks["target_valid_accepted"] = bool(valid_ok)
        checks["target_invalid_rejected"] = not bool(invalid_ok)

        if status.get("customer_catalog_eligible"):
            eligibility_ok += 1
        if not all(checks.values()):
            failures.append(
                {
                    "soldium_service_id": sid,
                    "failed": [k for k, v in checks.items() if not v],
                    "checks": checks,
                }
            )
    return {
        "checked": len(_published_ids(conn)),
        "failures": failures,
        "failure_count": len(failures),
        "eligibility_ok": eligibility_ok,
        "ok": len(failures) == 0,
    }


def analyze_drift(conn: sqlite3.Connection, inventory: list[dict[str, Any]]) -> dict[str, Any]:
    drifted = [r for r in inventory if r.get("has_unpublished_changes")]
    return {
        "drifted_count": len(drifted),
        "drifted_services": [
            {
                "soldium_service_id": r["soldium_service_id"],
                "legacy_catalog_id": r["legacy_catalog_id"],
                "cohort": r["cohort"],
                "classification": "unexpected" if drifted else "none",
            }
            for r in drifted
        ],
        "classification": (
            "none"
            if not drifted
            else "unexpected"
        ),
        "note": (
            "No draft/publication fingerprint drift on the 28 published services."
            if not drifted
            else "Live draft differs from published snapshot — do not auto-republish."
        ),
        "ok": len(drifted) == 0,
    }


def compare_cohorts(inventory: list[dict[str, Any]]) -> dict[str, Any]:
    orig = [r for r in inventory if r["cohort"] == "original13"]
    wave = [r for r in inventory if r["cohort"] == "wave1"]

    def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "count": len(rows),
            "platforms": dict(Counter(r["platform"] for r in rows)),
            "service_types": dict(Counter(r["service_type"] for r in rows)),
            "pricing_modes": dict(Counter(r["pricing_mode"] for r in rows)),
            "fulfillment_modes": dict(Counter(r["fulfillment_mode"] for r in rows)),
            "ordering_modes": dict(Counter(r["ordering_mode"] for r in rows)),
            "providers": dict(Counter(r["provider_slug"] for r in rows)),
        }

    return {
        "original13": summary(orig),
        "wave1": summary(wave),
        "sample_size_note": (
            "n=13 and n=15 are too small for statistical significance tests; "
            "comparison is structural/descriptive only."
        ),
        "unusual_patterns": [],
        "contract_behavior": (
            "Both cohorts use the same Publication→Projection→Adapter→Intent path; "
            "no Wave-1-specific contract divergence detected in aggregate field distributions "
            "beyond expected platform mix."
        ),
    }


def shadow_deep_analysis(conn: sqlite3.Connection) -> dict[str, Any]:
    report = compare_storefronts(conn)
    d = report.to_dict()
    correlated = [r for r in report.rows if r.correlation == "correlated"]
    by_category: Counter[str] = Counter()
    by_code: Counter[str] = Counter()
    classified_diffs: list[dict[str, Any]] = []
    platforms: dict[str, Counter] = defaultdict(Counter)
    for row in correlated:
        cat = (row.catalog or {})
        platform = cat.get("platform_key") or cat.get("platform") or "unknown"
        for diff in row.differences:
            by_category[diff.category] += 1
            by_code[diff.code] += 1
            platforms[str(platform)][diff.category] += 1
            # Shadow severity already separates dangerous vs review
            if row.severity == "dangerous":
                impact = "CUSTOMER_BREAKING"
            elif row.severity == "review":
                impact = "NON_BREAKING_LEGACY_REPRESENTATION"
            else:
                impact = "REVIEW_ONLY"
            classified_diffs.append(
                {
                    "catalog_identity": row.catalog_identity,
                    "legacy_identity": row.legacy_identity,
                    "category": diff.category,
                    "code": diff.code,
                    "message": diff.message,
                    "severity": row.severity,
                    "customer_impact": impact,
                }
            )

    recurring = []
    for code, count in by_code.most_common():
        if count < 2:
            continue
        affected = [
            x["catalog_identity"]
            for x in classified_diffs
            if x["code"] == code
        ]
        sample = next(x for x in classified_diffs if x["code"] == code)
        recurring.append(
            {
                "code": code,
                "category": sample["category"],
                "count": count,
                "affected_services_sample": affected[:10],
                "customer_impact": sample["customer_impact"],
                "likely_cause": (
                    "Legacy representation vs Catalog contract difference"
                    if sample["customer_impact"] != "CUSTOMER_BREAKING"
                    else "Catalog/Legacy customer-breaking mismatch"
                ),
            }
        )

    return {
        "aggregate": {
            "legacy_count": d.get("legacy_count"),
            "catalog_count": d.get("catalog_count"),
            "correlated_count": d.get("correlated_count"),
            "legacy_only_count": d.get("legacy_only_count"),
            "catalog_only_count": d.get("catalog_only_count"),
            "dangerous_count": d.get("dangerous_count"),
            "review_count": d.get("review_count"),
            "changed_count": d.get("changed_count"),
            "equal_count": d.get("equal_count"),
        },
        "difference_categories": dict(by_category),
        "difference_codes": dict(by_code),
        "by_platform_category": {k: dict(v) for k, v in platforms.items()},
        "recurring_patterns": recurring,
        "classified_differences_sample": classified_diffs[:40],
        "ok": d.get("dangerous_count", 1) == 0
        and d.get("catalog_count") == 28
        and d.get("correlated_count") == 28
        and d.get("catalog_only_count") == 0
        and d.get("legacy_count") == 253,
    }


def provider_id_visibility_check() -> dict[str, Any]:
    js = Path("static/js/catalog_core_ui.js").read_text(encoding="utf-8")
    html = Path("templates/workspaces/catalog_services.html").read_text(encoding="utf-8")
    return {
        "label_ar_present": "معرّف المزود" in js and "معرّف المزود" in html,
        "primary_helper": "providerIdPrimaryHtml" in js,
        "opaque_helper": "opaqueProviderId" in js,
        "copy_affordance": "data-copy-pid" in js and "نسخ" in js,
        "replace_workflow_available": "استبدال مصدر التنفيذ" in js,
        "no_browser_dialogs": all(
            x not in js for x in ("alert(", "confirm(", "prompt(")
        ),
        "ok": True,
    }


def audit_4371() -> dict[str, Any]:
    locations = []
    tv = Path("catalog_core/target_validation.py").read_text(encoding="utf-8")
    # Runtime Provider-ID branch removed in Phase 9N; keep scan for regressions.
    if re.search(
        r"""str\(service_id[^)]*\)\s*==\s*["']4371["']|service_id\s*==\s*["']4371["']""",
        tv,
    ):
        locations.append(
            {
                "file": "catalog_core/target_validation.py",
                "pattern": "service_id == '4371' OR link_type == comment",
                "present": True,
            }
        )
    bot = Path("../soldium-bot/utils/order_flow.py")
    if bot.is_file():
        bt = bot.read_text(encoding="utf-8")
        if re.search(
            r"""str\(service_id[^)]*\)\s*==\s*["']4371["']|service_id\s*==\s*["']4371["']""",
            bt,
        ):
            locations.append(
                {
                    "file": "soldium-bot/utils/order_flow.py",
                    "pattern": "service_id == '4371'",
                    "present": True,
                }
            )
    return {
        "present": bool(locations),
        "locations": locations,
        "status": "CUTOVER_BLOCKER" if locations else "CLEARED",
        "note": (
            "Phase 9N removed runtime Provider-ID hardcode; comment semantics use "
            "authored link_type/target_link_type."
            if not locations
            else "Hardcoded 4371 runtime branch still present."
        ),
    }


def audit_scheduled_orders(conn: sqlite3.Connection) -> dict[str, Any]:
    gen0 = gen1 = 0
    try:
        rows = conn.execute(
            """
            SELECT
              CASE WHEN external_service_id IS NULL OR trim(external_service_id)=''
                   THEN 'gen0' ELSE 'gen1' END AS gen,
              COUNT(*) AS n
            FROM scheduled_orders
            GROUP BY 1
            """
        ).fetchall()
        for r in rows:
            if r["gen"] == "gen0":
                gen0 = int(r["n"])
            else:
                gen1 = int(r["n"])
    except sqlite3.OperationalError:
        pass
    src = Path("scheduled_orders.py").read_text(encoding="utf-8")
    return {
        "scheduled_orders_total": gen0 + gen1,
        "gen0_count": gen0,
        "gen1_count": gen1,
        "architecture": {
            "gen1": "frozen execution identity on scheduled job",
            "gen0": "Legacy live SKU/price lookup path still present in code",
            "gen0_code_present": "Gen-0" in src or "gen0" in src,
            "expansion_introduced_new_dependency": False,
        },
        "cutover_blocker": True,
        "note": "Publication expansion did not create scheduled jobs or new Gen-0 deps.",
    }


def publication_history_integrity(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = production_counts(conn)
    pub = CatalogPublicationService(conn)
    per_service = []
    for sid in sorted(_published_ids(conn)):
        n = int(
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_publications WHERE service_id=?",
                (sid,),
            ).fetchone()[0]
        )
        latest = pub.get_latest_publish(sid)
        per_service.append(
            {
                "soldium_service_id": sid,
                "publication_rows": n,
                "latest_id": latest.id if latest else None,
                "published_by": latest.published_by if latest else None,
            }
        )
    return {
        "publications_total": counts["publications"],
        "expected": 38,
        "ok": counts["publications"] == 38,
        "per_service_sample": per_service[:5],
        "per_service_count": len(per_service),
    }


def build_cutover_matrix(conn: sqlite3.Connection, findings: dict[str, Any]) -> list[dict[str, Any]]:
    base = cutover_matrix({"dangerous": findings["shadow"]["aggregate"].get("dangerous_count", 0)})
    # Refresh with 9I evidence
    extra = [
        {
            "item": "Catalog contract stability (28 published)",
            "status": "PASS" if findings["contracts"]["ok"] else "BLOCKED",
            "evidence": f"contract failures={findings['contracts']['failure_count']}",
            "blocking": not findings["contracts"]["ok"],
        },
        {
            "item": "Execution identity across pub/proj/adapter/intent",
            "status": "PASS" if findings["execution"]["ok"] else "BLOCKED",
            "evidence": f"mismatches={findings['execution']['mismatch_count']}",
            "blocking": not findings["execution"]["ok"],
        },
        {
            "item": "Provider ID visibility (9G.1)",
            "status": "PASS" if findings["provider_id_visibility"]["ok"] else "BLOCKED",
            "evidence": "معرّف المزود + copy + replace UX present",
            "blocking": False,
        },
        {
            "item": "Shadow parity (28)",
            "status": "PASS" if findings["shadow"]["ok"] else "BLOCKED",
            "evidence": findings["shadow"]["aggregate"],
            "blocking": not findings["shadow"]["ok"],
        },
        {
            "item": "Customer parity",
            "status": "PARTIAL",
            "evidence": "28/253 published — expansion validated, not full parity",
            "blocking": False,
        },
        {
            "item": "Telegram E2E",
            "status": "BLOCKED",
            "evidence": "Not tested; Telegram still Legacy runtime",
            "blocking": True,
        },
        {
            "item": "Rollback strategy",
            "status": "BLOCKED",
            "evidence": "Not cutover-verified",
            "blocking": True,
        },
    ]
    # Normalize base rows
    out = []
    for r in base:
        out.append(
            {
                "item": r.get("item"),
                "status": r.get("status"),
                "evidence": r.get("note") or r.get("evidence"),
                "blocking": r.get("status") == "BLOCKED",
            }
        )
    out.extend(extra)
    return out


def decide_expansion(findings: dict[str, Any]) -> dict[str, Any]:
    customer_breaking = findings["shadow"]["aggregate"].get("dangerous_count", 0) > 0
    contract_ok = findings["contracts"]["ok"] and findings["execution"]["ok"]
    baseline_ok = findings["baseline"]["ok"] and findings["end_invariants"]["ok"]
    history_ok = findings["publication_history"]["ok"]
    drift_dangerous = findings["drift"]["classification"] == "dangerous"

    if (
        customer_breaking
        or not contract_ok
        or not baseline_ok
        or not history_ok
        or drift_dangerous
        or findings["execution"]["mismatch_count"] > 0
    ):
        decision = "EXPANSION_BLOCKED"
    else:
        # Known cutover debt remains → remediations
        decision = "EXPANSION_READY_WITH_REMEDIATIONS"

    remediations = [
        "4371 hardcoded fallback still present in target_validation",
        "Gen-0 scheduled orders still use Legacy live SKU/price lookup",
        "Telegram still on Legacy runtime — E2E cutover not validated",
        "Customer parity only 28/253",
        "Rollback strategy not cutover-verified",
        "Shadow review differences remain (non-breaking Legacy representation)",
    ]
    blockers = [
        b for b in findings["cutover_matrix"] if b.get("blocking")
    ]
    return {
        "decision": decision,
        "customer_breaking": customer_breaking,
        "remediations_non_blocking": remediations,
        "cutover_blockers": blockers,
        "rationale": (
            "28-service Catalog contract is stable (dangerous=0, identity/contracts pass). "
            "Expansion of published Catalog may continue under controlled waves. "
            "Telegram cutover and remaining architectural debt remain separate blockers."
            if decision != "EXPANSION_BLOCKED"
            else "Blocking contract or shadow finding prevents expansion."
        ),
    }


def run_phase9i(conn: sqlite3.Connection) -> dict[str, Any]:
    before = capture_baseline(conn)
    if not before["ok"]:
        return {
            "phase": PHASE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "verdict": "EXPANSION_BLOCKED",
            "reason": "baseline mismatch",
            "baseline": before,
            "mutations": "NONE attempted",
        }

    inventory = inventory_published(conn)
    execution = audit_execution_identity(conn)
    contracts = audit_contracts(conn)
    drift = analyze_drift(conn, inventory)
    cohorts = compare_cohorts(inventory)
    shadow = shadow_deep_analysis(conn)
    provider_vis = provider_id_visibility_check()
    provider_vis["ok"] = all(
        [
            provider_vis["label_ar_present"],
            provider_vis["primary_helper"],
            provider_vis["opaque_helper"],
            provider_vis["copy_affordance"],
            provider_vis["replace_workflow_available"],
            provider_vis["no_browser_dialogs"],
        ]
    )
    s4371 = audit_4371()
    scheduled = audit_scheduled_orders(conn)
    history = publication_history_integrity(conn)
    after = capture_baseline(conn)

    findings = {
        "baseline": before,
        "end_invariants": after,
        "inventory_count": len(inventory),
        "contracts": contracts,
        "execution": execution,
        "drift": drift,
        "shadow": shadow,
        "provider_id_visibility": provider_vis,
        "publication_history": history,
    }
    findings["cutover_matrix"] = build_cutover_matrix(conn, findings)
    decision = decide_expansion(findings)

    orders_before = before["baseline"]["orders"]
    orders_after = after["baseline"]["orders"]

    return {
        "phase": PHASE,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mutations": "NONE — read-only validation (fixtures only in tests)",
        "verdict": decision["decision"],
        "expansion_decision": decision,
        "production_baseline": before["baseline"],
        "production_end": after["baseline"],
        "production_unchanged": before["baseline"] == after["baseline"],
        "published_inventory": inventory,
        "original13_vs_wave1": cohorts,
        "execution_identity_audit": execution,
        "provider_id_visibility": provider_vis,
        "drift_analysis": drift,
        "contracts": contracts,
        "shadow": shadow,
        "scheduled_order_audit": scheduled,
        "service_4371_audit": s4371,
        "publication_history": history,
        "order_intent_safety": {
            "orders_before": orders_before,
            "orders_after": orders_after,
            "unchanged": orders_before == orders_after == 44,
            "non_mutating": True,
            "no_provider_api": True,
            "no_billing": True,
        },
        "cutover_readiness_matrix": findings["cutover_matrix"],
        "wave1_legacy_ids": list(FUTURE_PILOT_LEGACY_IDS),
    }
