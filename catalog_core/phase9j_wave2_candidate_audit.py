# -*- coding: utf-8 -*-
"""Phase 9J — Wave 2 candidate audit & selection (read-only / no publication)."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from catalog_core.phase9c_audit import SENTINEL_MAX
from catalog_core.phase9d_audit import load_phase9d_rows, production_counts
from catalog_core.phase9g_business_decision_pack import _published_ids, partition_unpublished
from catalog_core.phase9h_wave1 import WAVE1_CANDIDATE_LEGACY_IDS, _classify_row
from catalog_core.publication import CatalogPublicationService
from catalog_core.readiness import CHECK_LABELS_AR, evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.storefront_shadow import _dh_to_millimes_exact, compare_storefronts

PHASE = "9J"
TARGET_WAVE_SIZE = 15
MIN_WAVE_SIZE = 10
MAX_WAVE_SIZE = 20
MAX_PER_PLATFORM = 3
MAX_PER_SECTION = 2

HISTORICAL_9GB_PARTITION = {
    "ready": 85,
    "probable": 24,
    "special_unknown": 69,
    "sentinel": 22,
    "iptv": 7,
    "missing_execution": 5,
    "other_review": 28,
    "total_unpublished": 240,
}

EXPECTED_BASELINE = {
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


def _scheduled_count(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("SELECT COUNT(*) FROM scheduled_orders").fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def capture_baseline(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = production_counts(conn)
    published = sorted(_published_ids(conn))
    baseline = {
        **{k: counts[k] for k in (
            "services", "nodes", "entries", "prices", "execution_sources",
            "mappings", "publications", "orders", "smm_services",
        )},
        "published_services": len(published),
        "scheduled_orders": _scheduled_count(conn),
    }
    mismatches = {
        k: {"expected": EXPECTED_BASELINE[k], "actual": baseline[k]}
        for k in EXPECTED_BASELINE
        if baseline.get(k) != EXPECTED_BASELINE[k]
    }
    return {"baseline": baseline, "mismatches": mismatches, "ok": not mismatches}


def current_partition(conn: sqlite3.Connection) -> dict[str, Any]:
    published = _published_ids(conn)
    rows = [r for r in load_phase9d_rows(conn) if r.soldium_service_id not in published]
    parts = partition_unpublished(rows)
    counts = {k: len(v) for k, v in parts.items()}
    total = sum(counts.values())
    return {
        "unpublished_total": total,
        "published_total": len(published),
        "counts": counts,
        "historical_9gb": HISTORICAL_9GB_PARTITION,
        "reconciliation": {
            "ready_delta": counts["ready"] - HISTORICAL_9GB_PARTITION["ready"],
            "unpublished_delta": total - HISTORICAL_9GB_PARTITION["total_unpublished"],
            "explanation": (
                "Phase 9H published 15 Ready services from the FUTURE_PILOT pool; "
                f"Ready 85→{counts['ready']}, unpublished 240→{total}. "
                "Other partition sizes unchanged."
            ),
            "wave1_legacy_ids_published": list(WAVE1_CANDIDATE_LEGACY_IDS),
        },
        "parts": parts,
        "rows": rows,
    }


def _bridge_placement(conn: sqlite3.Connection, legacy_id: str | None) -> dict[str, Any]:
    if not legacy_id:
        return {"platform_key": None, "section_key": None, "subsection_key": None}
    row = conn.execute(
        """
        SELECT platform_key, section_key, subsection_key, name_ar,
               provider_slug, provider_api_account, external_service_id
        FROM smm_services WHERE catalog_id = ?
        """,
        (legacy_id,),
    ).fetchone()
    if not row:
        return {"platform_key": None, "section_key": None, "subsection_key": None}
    return dict(row)


def audit_ready_service(conn: sqlite3.Connection, r) -> dict[str, Any]:
    repo = CatalogRepository(conn)
    pub = CatalogPublicationService(conn)
    sid = r.soldium_service_id
    svc = repo.get_service(sid)
    source = repo.get_active_execution_source(sid)
    price = repo.get_active_price(sid)
    entry = repo.get_entry_for_service(sid)
    if svc and entry:
        svc.entry_id = entry.id
        svc.parent_entry_id = entry.parent_entry_id
        svc.location_path = repo.breadcrumb_names(entry.parent_entry_id)
    ready = (
        evaluate_service_readiness(repo, svc, source=source, price=price)
        if svc
        else None
    )
    status = pub.get_publication_status(sid)
    place = _bridge_placement(conn, r.legacy_catalog_id)
    classification = _classify_row(r)

    gate_errors: list[str] = []
    if svc is None:
        gate_errors.append("service_missing")
    else:
        if str(svc.status) == "archived":
            gate_errors.append("archived")
        if status.get("publication_status") == "published":
            gate_errors.append("already_published")
        if not ready or not ready.ready:
            gate_errors.append(
                "not_ready:"
                + ",".join(i.code for i in ((ready.issues if ready else []) or []))
            )
        if int(svc.min_quantity) <= 0:
            gate_errors.append("min_invalid")
        if int(svc.max_quantity) <= 0 or int(svc.max_quantity) == SENTINEL_MAX:
            gate_errors.append("max_not_finite")
        if int(svc.min_quantity) > int(svc.max_quantity):
            gate_errors.append("min_gt_max")
        if price is None or int(price.amount_millimes) <= 0:
            gate_errors.append("price_invalid")
        if price and str(price.currency).upper() != "MAD":
            gate_errors.append("currency")
        if price and price.pricing_mode not in {"per_1000", "per_unit", "fixed_package"}:
            gate_errors.append("pricing_mode")
        if svc.ordering_mode not in {"quantity_based", "package_based"}:
            gate_errors.append("ordering_mode")
        if str(svc.fulfillment_mode or "") not in {"auto", "admin"}:
            gate_errors.append("fulfillment")
        if not (svc.target_platform_key and svc.target_section_key):
            gate_errors.append("target_missing")
        if r.target_special:
            gate_errors.append("unresolved_special_target")
        if source is None:
            gate_errors.append("execution_missing")
        else:
            if not str(source.provider_slug or "").strip():
                gate_errors.append("provider_missing")
            if not str(source.provider_account_key or "").strip():
                gate_errors.append("account_missing")
            if source.external_service_id is None or str(source.external_service_id).strip() == "":
                gate_errors.append("external_id_missing")
        if classification != "STANDARD":
            gate_errors.append(f"not_standard:{classification}")
        if r.bridge_review_codes:
            # allow empty; unresolved codes that block Ready should already be partitioned out
            blocking = [
                c
                for c in r.bridge_review_codes
                if c in {"missing_provider_account", "max_qty_sentinel", "needs_manual_review"}
            ]
            if blocking:
                gate_errors.append("review_codes:" + ",".join(blocking))

    # Risk profile
    risk_reasons: list[str] = []
    risk = "LOW"
    if gate_errors:
        risk = "HIGH"
        risk_reasons.extend(gate_errors)
    else:
        if svc and svc.fulfillment_mode != "auto":
            risk = "MEDIUM"
            risk_reasons.append("non_auto_fulfillment")
        if price and price.pricing_mode != "per_1000":
            risk = "MEDIUM"
            risk_reasons.append(f"pricing_mode:{price.pricing_mode}")
        if svc and svc.ordering_mode != "quantity_based":
            risk = "MEDIUM"
            risk_reasons.append(f"ordering:{svc.ordering_mode}")
        if svc and int(svc.max_quantity) > 500_000:
            risk = "MEDIUM"
            risk_reasons.append("very_high_max_quantity")
        if price and int(price.amount_millimes) < 100:
            risk_reasons.append("very_low_price_note")
            # observational only — keep LOW unless other issues
        if price and int(price.amount_millimes) > 5_000_000:
            risk = "MEDIUM"
            risk_reasons.append("very_high_price")

    readiness_components = {}
    if ready:
        for c in ready.checks or []:
            readiness_components[c.check] = {
                "ok": c.ok,
                "label_ar": CHECK_LABELS_AR.get(c.check, c.check),
            }

    return {
        "legacy_catalog_id": r.legacy_catalog_id,
        "soldium_service_id": sid,
        "platform": place.get("platform_key") or r.platform_key,
        "section": place.get("section_key") or r.section_key,
        "subsection": place.get("subsection_key") or r.subsection_key,
        "name_ar": (svc.name_ar if svc else None) or place.get("name_ar"),
        "provider_slug": source.provider_slug if source else None,
        "provider_account_key": source.provider_account_key if source else None,
        "provider_service_id": (
            str(source.external_service_id) if source else None
        ),
        "معرّف_المزود": (
            str(source.external_service_id) if source else None
        ),
        "service_type": svc.service_type if svc else r.service_type,
        "ordering_mode": svc.ordering_mode if svc else r.ordering_mode,
        "min_quantity": svc.min_quantity if svc else r.min_quantity,
        "max_quantity": svc.max_quantity if svc else r.max_quantity,
        "price_amount_millimes": (
            int(price.amount_millimes) if price else r.amount_millimes
        ),
        "currency": price.currency if price else r.currency,
        "pricing_mode": price.pricing_mode if price else r.pricing_mode,
        "fulfillment_mode": svc.fulfillment_mode if svc else r.fulfillment_mode,
        "target_policy": {
            "platform_key": svc.target_platform_key if svc else None,
            "section_key": svc.target_section_key if svc else None,
            "subsection_key": svc.target_subsection_key if svc else None,
            "link_prompt_key": svc.target_link_prompt_key if svc else None,
            "link_type": svc.target_link_type if svc else None,
        },
        "readiness_ready": bool(ready.ready) if ready else False,
        "readiness_components": readiness_components,
        "publication_status": status.get("publication_status"),
        "review_codes": list(r.bridge_review_codes or []),
        "bridge_classification": classification,
        "phase9d_confidence": r.service_type_confidence,
        "gate_passed": not gate_errors,
        "gate_errors": gate_errors,
        "risk_level": risk,
        "risk_reasons": risk_reasons,
    }


def draft_legacy_preview(conn: sqlite3.Connection, candidate: dict[str, Any]) -> dict[str, Any]:
    """Compare Catalog draft vs Legacy row for one unpublished candidate (not published shadow)."""
    lid = candidate["legacy_catalog_id"]
    sid = candidate["soldium_service_id"]
    leg = conn.execute(
        """
        SELECT name_ar, min_qty, max_qty, local_price_dh, provider_slug,
               provider_api_account, external_service_id, fulfillment_mode
        FROM smm_services WHERE catalog_id = ?
        """,
        (lid,),
    ).fetchone()
    diffs: list[dict[str, Any]] = []
    dangerous = 0
    if not leg:
        return {
            "legacy_catalog_id": lid,
            "soldium_service_id": sid,
            "correlated": False,
            "dangerous": 1,
            "difference_count": 1,
            "requires_architectural_review": True,
            "differences": [
                {
                    "category": "identity",
                    "code": "legacy_missing",
                    "impact": "CUSTOMER_BREAKING",
                }
            ],
        }

    cat_ext = candidate.get("provider_service_id")
    if not cat_ext:
        dangerous += 1
        diffs.append(
            {
                "category": "execution",
                "code": "missing_provider_service_id",
                "impact": "CUSTOMER_BREAKING",
            }
        )

    leg_ext = (
        str(leg["external_service_id"])
        if leg["external_service_id"] is not None
        else None
    )
    if leg_ext and cat_ext and str(leg_ext) != str(cat_ext):
        # Legacy external_service_id field can diverge from Catalog execution
        # identity representation without meaning a broken opaque TEXT source.
        diffs.append(
            {
                "category": "execution",
                "code": "external_id_field_differs_from_catalog_source",
                "legacy": leg_ext,
                "catalog": cat_ext,
                "impact": "NON_BREAKING_LEGACY_REPRESENTATION",
            }
        )

    leg_slug = (leg["provider_slug"] or "").strip() or None
    cat_slug = (candidate.get("provider_slug") or "").strip() or None
    if leg_slug and cat_slug and leg_slug != cat_slug:
        dangerous += 1
        diffs.append(
            {
                "category": "execution",
                "code": "provider_slug_mismatch",
                "legacy": leg_slug,
                "catalog": cat_slug,
                "impact": "CUSTOMER_BREAKING",
            }
        )

    if leg["max_qty"] is not None and int(leg["max_qty"]) == SENTINEL_MAX:
        diffs.append(
            {
                "category": "quantity",
                "code": "legacy_sentinel_but_catalog_finite",
                "impact": "NON_BREAKING_LEGACY_REPRESENTATION",
            }
        )

    leg_millimes = _dh_to_millimes_exact(leg["local_price_dh"])
    cat_millimes = candidate.get("price_amount_millimes")
    if leg_millimes is not None and cat_millimes is not None:
        if int(leg_millimes) != int(cat_millimes):
            diffs.append(
                {
                    "category": "price",
                    "code": "price_millimes_mismatch",
                    "legacy_millimes": leg_millimes,
                    "catalog_millimes": cat_millimes,
                    "impact": "REVIEW_OBSERVATION",
                }
            )

    if (
        leg["fulfillment_mode"]
        and candidate.get("fulfillment_mode")
        and str(leg["fulfillment_mode"]) != str(candidate.get("fulfillment_mode"))
    ):
        diffs.append(
            {
                "category": "fulfillment",
                "code": "fulfillment_differs",
                "legacy": leg["fulfillment_mode"],
                "catalog": candidate.get("fulfillment_mode"),
                "impact": "REVIEW_OBSERVATION",
            }
        )

    requires_review = any(
        d.get("impact") in {"REVIEW_OBSERVATION", "CUSTOMER_BREAKING"}
        for d in diffs
    )
    return {
        "legacy_catalog_id": lid,
        "soldium_service_id": sid,
        "provider_service_id": cat_ext,
        "معرّف_المزود": cat_ext,
        "correlated": True,
        "dangerous": dangerous,
        "differences": diffs,
        "difference_count": len(diffs),
        "requires_architectural_review": requires_review and dangerous == 0,
    }


def select_wave2(low_risk: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic diversity pick. Tie-break: platform, section, service_type, legacy_id."""
    pool = sorted(
        low_risk,
        key=lambda c: (
            c.get("platform") or "",
            c.get("section") or "",
            c.get("service_type") or "",
            c.get("legacy_catalog_id") or "",
            c.get("soldium_service_id") or "",
        ),
    )
    selected: list[dict[str, Any]] = []
    platforms: Counter[str] = Counter()
    sections: Counter[str] = Counter()
    types: set[str] = set()
    remaining = list(pool)

    def score(c: dict[str, Any]) -> tuple:
        p = c.get("platform") or ""
        s = c.get("section") or ""
        t = c.get("service_type") or ""
        # Higher is better; stable secondary keys for determinism
        sc = 0
        if platforms[p] == 0:
            sc += 100
        if t not in types:
            sc += 50
        if sections[s] == 0:
            sc += 25
        if platforms[p] >= MAX_PER_PLATFORM:
            sc -= 1000
        if sections[s] >= MAX_PER_SECTION:
            sc -= 500
        return (
            sc,
            # prefer lower existing density
            -platforms[p],
            -sections[s],
            p,
            s,
            t,
            c.get("legacy_catalog_id") or "",
        )

    while remaining and len(selected) < TARGET_WAVE_SIZE:
        remaining.sort(key=score, reverse=True)
        best = remaining[0]
        p = best.get("platform") or ""
        s = best.get("section") or ""
        if platforms[p] >= MAX_PER_PLATFORM or sections[s] >= MAX_PER_SECTION:
            # try next eligible under soft diversity caps
            placed = False
            for cand in remaining:
                cp = cand.get("platform") or ""
                cs = cand.get("section") or ""
                if platforms[cp] < MAX_PER_PLATFORM and sections[cs] < MAX_PER_SECTION:
                    best = cand
                    p, s = cp, cs
                    placed = True
                    break
            if not placed:
                # Soft caps exhausted — still fill to TARGET from remaining LOW
                # (safety unchanged; diversity preferred but not mandatory).
                best = remaining[0]
                p = best.get("platform") or ""
                s = best.get("section") or ""
        best = dict(best)
        best["selection_reason"] = (
            f"LOW-risk Ready; diversity platform={p} section={s} "
            f"type={best.get('service_type')}; deterministic greedy"
        )
        selected.append(best)
        platforms[p] += 1
        sections[s] += 1
        types.add(best.get("service_type") or "")
        remaining = [
            c
            for c in remaining
            if c["soldium_service_id"] != best["soldium_service_id"]
        ]

    return selected


def pool_distributions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(rows),
        "platforms": dict(Counter(r.get("platform") for r in rows)),
        "sections": dict(
            Counter(f"{r.get('platform')}/{r.get('section')}" for r in rows)
        ),
        "service_types": dict(Counter(r.get("service_type") for r in rows)),
        "pricing_modes": dict(Counter(r.get("pricing_mode") for r in rows)),
        "fulfillment_modes": dict(Counter(r.get("fulfillment_mode") for r in rows)),
        "ordering_modes": dict(Counter(r.get("ordering_mode") for r in rows)),
        "providers": dict(Counter(r.get("provider_slug") for r in rows)),
        "accounts": dict(
            Counter(
                f"{r.get('provider_slug')}/{r.get('provider_account_key')}"
                for r in rows
            )
        ),
        "price_millimes_min": min(
            (r["price_amount_millimes"] for r in rows if r.get("price_amount_millimes")),
            default=None,
        ),
        "price_millimes_max": max(
            (r["price_amount_millimes"] for r in rows if r.get("price_amount_millimes")),
            default=None,
        ),
    }


def wave1_comparison(conn: sqlite3.Connection) -> dict[str, Any]:
    pub = CatalogPublicationService(conn)
    rows = []
    for lid in WAVE1_CANDIDATE_LEGACY_IDS:
        br = conn.execute(
            "SELECT soldium_service_id FROM soldium_catalog_legacy_bridge WHERE legacy_catalog_id=?",
            (lid,),
        ).fetchone()
        if not br:
            continue
        sid = str(br["soldium_service_id"])
        latest = pub.get_latest_publish(sid)
        if not latest:
            continue
        place = _bridge_placement(conn, lid)
        rows.append(
            {
                "legacy_catalog_id": lid,
                "soldium_service_id": sid,
                "platform": place.get("platform_key"),
                "section": place.get("section_key"),
                "service_type": latest.service_type,
                "pricing_mode": latest.pricing_mode,
                "fulfillment_mode": latest.fulfillment_mode,
                "ordering_mode": latest.ordering_mode,
                "provider_slug": latest.provider_slug,
                "min_quantity": latest.min_quantity,
                "max_quantity": latest.max_quantity,
            }
        )
    return {
        "count": len(rows),
        "distributions": pool_distributions(
            [
                {
                    **r,
                    "price_amount_millimes": 0,
                    "provider_account_key": None,
                    "provider_service_id": None,
                }
                for r in rows
            ]
        ),
        "note": (
            "Wave 1 is a comparison baseline only — similarity is not approval. "
            "Prefer ordinary patterns already demonstrated (auto, per_1000, quantity_based)."
        ),
        "services": rows,
    }


def decide(wave: list[dict[str, Any]], previews: list[dict[str, Any]], baseline_ok: bool) -> str:
    if not baseline_ok:
        return "WAVE_2_CANDIDATES_BLOCKED"
    dangerous = sum(p.get("dangerous", 0) for p in previews)
    if dangerous > 0:
        return "WAVE_2_CANDIDATES_BLOCKED"
    if len(wave) < MIN_WAVE_SIZE:
        return "WAVE_2_CANDIDATES_INSUFFICIENT"
    reviewish = any(p.get("requires_architectural_review") for p in previews)
    if reviewish:
        return "WAVE_2_CANDIDATES_READY_WITH_REVIEW"
    return "WAVE_2_CANDIDATES_READY"


def run_phase9j(conn: sqlite3.Connection) -> dict[str, Any]:
    before = capture_baseline(conn)
    if not before["ok"]:
        return {
            "phase": PHASE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "verdict": "WAVE_2_CANDIDATES_BLOCKED",
            "reason": "baseline mismatch",
            "baseline": before,
            "mutations": "NONE",
        }

    part = current_partition(conn)
    ready_rows = part["parts"]["ready"]
    ready_audits = [audit_ready_service(conn, r) for r in ready_rows]
    ready_audits.sort(
        key=lambda c: (
            c.get("platform") or "",
            c.get("section") or "",
            c.get("legacy_catalog_id") or "",
        )
    )

    gated = [c for c in ready_audits if c["gate_passed"]]
    low = [c for c in gated if c["risk_level"] == "LOW"]
    excluded_ready = [
        {
            "legacy_catalog_id": c["legacy_catalog_id"],
            "soldium_service_id": c["soldium_service_id"],
            "provider_service_id": c.get("provider_service_id"),
            "risk_level": c["risk_level"],
            "exclusion_reason": (
                "; ".join(c["gate_errors"])
                if c["gate_errors"]
                else "; ".join(c["risk_reasons"]) or c["risk_level"]
            ),
        }
        for c in ready_audits
        if not c["gate_passed"] or c["risk_level"] != "LOW"
    ]

    wave = select_wave2(low)
    # Cap at MAX
    wave = wave[:MAX_WAVE_SIZE]

    previews = [draft_legacy_preview(conn, c) for c in wave]
    # Exclude any that show dangerous in preview
    safe_wave = []
    safe_previews = []
    for c, p in zip(wave, previews):
        if p.get("dangerous", 0) > 0:
            excluded_ready.append(
                {
                    "legacy_catalog_id": c["legacy_catalog_id"],
                    "soldium_service_id": c["soldium_service_id"],
                    "provider_service_id": c.get("provider_service_id"),
                    "risk_level": "HIGH",
                    "exclusion_reason": "shadow_preview_dangerous",
                }
            )
            continue
        safe_wave.append(c)
        safe_previews.append(p)

    # If exclusions from dangerous shrank wave, optionally top-up from remaining low
    selected_ids = {c["soldium_service_id"] for c in safe_wave}
    platforms_used = Counter(c.get("platform") or "" for c in safe_wave)
    sections_used = Counter(c.get("section") or "" for c in safe_wave)
    types_used = {c.get("service_type") or "" for c in safe_wave}
    if len(safe_wave) < TARGET_WAVE_SIZE:
        extra_pool = [
            c
            for c in low
            if c["soldium_service_id"] not in selected_ids
            and c["soldium_service_id"]
            not in {e["soldium_service_id"] for e in excluded_ready}
        ]
        extra_pool = sorted(
            extra_pool,
            key=lambda c: (
                c.get("platform") or "",
                c.get("section") or "",
                c.get("service_type") or "",
                c.get("legacy_catalog_id") or "",
                c.get("soldium_service_id") or "",
            ),
        )
        while extra_pool and len(safe_wave) < TARGET_WAVE_SIZE:

            def _top_score(c: dict[str, Any]) -> tuple:
                p = c.get("platform") or ""
                s = c.get("section") or ""
                t = c.get("service_type") or ""
                sc = 0
                if platforms_used[p] == 0:
                    sc += 100
                if t not in types_used:
                    sc += 50
                if sections_used[s] == 0:
                    sc += 25
                if platforms_used[p] >= MAX_PER_PLATFORM:
                    sc -= 1000
                if sections_used[s] >= MAX_PER_SECTION:
                    sc -= 500
                return (
                    sc,
                    -platforms_used[p],
                    -sections_used[s],
                    p,
                    s,
                    t,
                    c.get("legacy_catalog_id") or "",
                )

            extra_pool.sort(key=_top_score, reverse=True)
            best = None
            for cand in extra_pool:
                cp = cand.get("platform") or ""
                cs = cand.get("section") or ""
                if (
                    platforms_used[cp] < MAX_PER_PLATFORM
                    and sections_used[cs] < MAX_PER_SECTION
                ):
                    best = cand
                    break
            if best is None:
                break
            p = draft_legacy_preview(conn, best)
            if p.get("dangerous", 0) > 0:
                excluded_ready.append(
                    {
                        "legacy_catalog_id": best["legacy_catalog_id"],
                        "soldium_service_id": best["soldium_service_id"],
                        "provider_service_id": best.get("provider_service_id"),
                        "risk_level": "HIGH",
                        "exclusion_reason": "shadow_preview_dangerous",
                    }
                )
                extra_pool = [
                    c
                    for c in extra_pool
                    if c["soldium_service_id"] != best["soldium_service_id"]
                ]
                continue
            best = dict(best)
            best["selection_reason"] = (
                f"LOW-risk Ready; diversity platform={best.get('platform')} "
                f"section={best.get('section')} type={best.get('service_type')}; "
                "deterministic greedy top-up"
            )
            safe_wave.append(best)
            safe_previews.append(p)
            selected_ids.add(best["soldium_service_id"])
            platforms_used[best.get("platform") or ""] += 1
            sections_used[best.get("section") or ""] += 1
            types_used.add(best.get("service_type") or "")
            extra_pool = [
                c
                for c in extra_pool
                if c["soldium_service_id"] != best["soldium_service_id"]
            ]

    non_ready = {
        name: {
            "count": len(part["parts"][name]),
            "reason": "Outside Ready pool — not considered for Wave 2",
            "legacy_ids_sample": [
                r.legacy_catalog_id for r in part["parts"][name][:10]
            ],
        }
        for name in (
            "probable",
            "special_unknown",
            "sentinel",
            "iptv",
            "missing_execution",
            "other_review",
        )
    }

    published_shadow = compare_storefronts(conn).to_dict()
    after = capture_baseline(conn)
    verdict = decide(safe_wave, safe_previews, before["ok"] and after["ok"])

    # Recommended wave rows with Provider ID first
    recommended = []
    for c in safe_wave:
        recommended.append(
            {
                "معرّف_المزود": c.get("provider_service_id"),
                "provider_service_id": c.get("provider_service_id"),
                "legacy_catalog_id": c["legacy_catalog_id"],
                "soldium_service_id": c["soldium_service_id"],
                "provider_slug": c.get("provider_slug"),
                "provider_account_key": c.get("provider_account_key"),
                "platform": c.get("platform"),
                "section": c.get("section"),
                "subsection": c.get("subsection"),
                "service_type": c.get("service_type"),
                "ordering_mode": c.get("ordering_mode"),
                "min_quantity": c.get("min_quantity"),
                "max_quantity": c.get("max_quantity"),
                "price_amount_millimes": c.get("price_amount_millimes"),
                "currency": c.get("currency"),
                "pricing_mode": c.get("pricing_mode"),
                "fulfillment_mode": c.get("fulfillment_mode"),
                "target_policy": c.get("target_policy"),
                "readiness_ready": c.get("readiness_ready"),
                "readiness_components": c.get("readiness_components"),
                "risk_level": c.get("risk_level"),
                "selection_reason": c.get("selection_reason"),
                "name_ar": c.get("name_ar"),
            }
        )

    risk_counts = Counter(c["risk_level"] for c in ready_audits)
    fractional_prices = [
        c["legacy_catalog_id"]
        for c in ready_audits
        if c.get("price_amount_millimes") is not None
        and int(c["price_amount_millimes"]) % 1000 != 0
    ]
    qty_unusual = [
        {
            "legacy_catalog_id": c["legacy_catalog_id"],
            "min": c.get("min_quantity"),
            "max": c.get("max_quantity"),
            "reason": "max>500000",
        }
        for c in ready_audits
        if c.get("max_quantity") is not None and int(c["max_quantity"]) > 500_000
    ]
    target_classes = Counter()
    for c in ready_audits:
        tp = c.get("target_policy") or {}
        if not tp.get("platform_key"):
            target_classes["unresolved"] += 1
        elif tp.get("link_type"):
            target_classes[f"link_type:{tp.get('link_type')}"] += 1
        else:
            target_classes[
                f"platform_section:{tp.get('platform_key')}/{tp.get('section_key')}"
            ] += 1

    return {
        "phase": PHASE,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mutations": "NONE — read-only candidate audit",
        "verdict": verdict,
        "production_baseline": before["baseline"],
        "production_end": after["baseline"],
        "production_unchanged": before["baseline"] == after["baseline"],
        "partition": {
            "current_counts": part["counts"],
            "unpublished_total": part["unpublished_total"],
            "historical_9gb": part["historical_9gb"],
            "reconciliation": part["reconciliation"],
        },
        "ready_pool": {
            "count": len(ready_audits),
            "gate_passed": len(gated),
            "low_risk": len(low),
            "risk_counts": dict(risk_counts),
            "distributions": pool_distributions(ready_audits),
            "low_risk_distributions": pool_distributions(low),
            "services": ready_audits,
        },
        "wave1_comparison": wave1_comparison(conn),
        "risk_analysis": {
            "ready_risk_counts": dict(risk_counts),
            "wave_all_low": all(c.get("risk_level") == "LOW" for c in recommended),
            "note": "MEDIUM/HIGH Ready excluded from automatic Wave 2 recommendation",
        },
        "provider_distribution": {
            "ready": (pool_distributions(ready_audits).get("providers")),
            "wave": dict(Counter(c.get("provider_slug") for c in recommended)),
        },
        "account_distribution": {
            "ready": (pool_distributions(ready_audits).get("accounts")),
            "wave": dict(
                Counter(
                    f"{c.get('provider_slug')}/{c.get('provider_account_key')}"
                    for c in recommended
                )
            ),
        },
        "service_type_distribution": {
            "ready": (pool_distributions(ready_audits).get("service_types")),
            "wave": dict(Counter(c.get("service_type") for c in recommended)),
        },
        "pricing_analysis": {
            "ready_modes": (pool_distributions(ready_audits).get("pricing_modes")),
            "wave_modes": dict(Counter(c.get("pricing_mode") for c in recommended)),
            "ready_price_millimes_min": pool_distributions(ready_audits).get(
                "price_millimes_min"
            ),
            "ready_price_millimes_max": pool_distributions(ready_audits).get(
                "price_millimes_max"
            ),
            "fractional_not_divisible_by_1000_count": len(fractional_prices),
            "fractional_sample": fractional_prices[:10],
            "note": "High/low price alone is not a hard gate; corruption would be flagged elsewhere",
        },
        "quantity_analysis": {
            "unusual_high_max_count": len(qty_unusual),
            "unusual_sample": qty_unusual[:10],
            "wave_ranges": [
                {
                    "legacy_catalog_id": c["legacy_catalog_id"],
                    "min": c.get("min_quantity"),
                    "max": c.get("max_quantity"),
                }
                for c in recommended
            ],
        },
        "target_analysis": {
            "ready_classes": dict(target_classes),
            "wave_all_have_platform_section": all(
                (c.get("target_policy") or {}).get("platform_key")
                and (c.get("target_policy") or {}).get("section_key")
                for c in recommended
            ),
        },
        "fulfillment_analysis": {
            "ready": (pool_distributions(ready_audits).get("fulfillment_modes")),
            "wave": dict(Counter(c.get("fulfillment_mode") for c in recommended)),
        },
        "placement_analysis": {
            "ready_platforms": (pool_distributions(ready_audits).get("platforms")),
            "ready_sections": (pool_distributions(ready_audits).get("sections")),
            "wave_platforms": dict(Counter(c.get("platform") for c in recommended)),
            "wave_sections": dict(
                Counter(
                    f"{c.get('platform')}/{c.get('section')}" for c in recommended
                )
            ),
        },
        "execution_identity": {
            "wave_all_have_triple": all(
                c.get("provider_slug")
                and c.get("provider_account_key")
                and c.get("provider_service_id")
                for c in recommended
            ),
            "provider_id_display": "معرّف المزود is first key on each recommended row",
        },
        "readiness": {
            "wave_all_ready": all(c.get("readiness_ready") for c in recommended),
            "components_explained": [
                "basic",
                "placement",
                "commercial",
                "source",
                "price",
            ],
        },
        "published_shadow_context": {
            "legacy_count": published_shadow.get("legacy_count"),
            "catalog_count": published_shadow.get("catalog_count"),
            "dangerous_count": published_shadow.get("dangerous_count"),
            "note": "Current published set context only — candidates are unpublished.",
        },
        "candidate_shadow_preview": {
            "candidate_count": len(safe_wave),
            "correlated_candidates": sum(
                1 for p in safe_previews if p.get("correlated")
            ),
            "dangerous_total": sum(p.get("dangerous", 0) for p in safe_previews),
            "previews": safe_previews,
        },
        "deterministic_selection_rule": {
            "source": "Ready partition only",
            "hard_gates": True,
            "risk": "LOW only",
            "target_size": TARGET_WAVE_SIZE,
            "min_size": MIN_WAVE_SIZE,
            "max_per_platform": MAX_PER_PLATFORM,
            "max_per_section": MAX_PER_SECTION,
            "soft_diversity_caps": (
                "Prefer new platform/type/section first; if caps block reaching "
                "TARGET_WAVE_SIZE, continue filling from remaining LOW-risk only"
            ),
            "sort_keys": [
                "platform",
                "section",
                "service_type",
                "legacy_catalog_id",
                "soldium_service_id",
            ],
            "diversity": "greedy prefer new platform/type/section; no randomness",
        },
        "RECOMMENDED_WAVE_2": recommended,
        "EXCLUDED_READY": excluded_ready,
        "NON_READY_NOT_CONSIDERED": non_ready,
        "wave_size": len(safe_wave),
        "tests": {
            "focused": "tests/test_phase9j_wave2_candidate_audit.py",
            "suite_previous": 394,
            "suite_note": "Recorded by runner after pytest",
        },
        "known_blockers": [
            "4371 hardcoded fallback (cutover)",
            "Gen-0 scheduled Legacy SKU/price (cutover)",
            "Telegram still Legacy (cutover)",
            "Customer parity 28/253",
        ],
        "final_decision": verdict,
    }
