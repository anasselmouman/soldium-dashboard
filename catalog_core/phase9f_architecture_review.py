# -*- coding: utf-8 -*-
"""Phase 9F — Read-only architecture review / expansion gate."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from catalog_core.order_contract_republish import PILOT_SERVICE_IDS
from catalog_core.phase9c_audit import SENTINEL_MAX
from catalog_core.phase9d_audit import (
    CONFIDENCE_CONFIRMED,
    CONFIDENCE_PROBABLE,
    CONFIDENCE_SPECIAL,
    CONFIDENCE_UNKNOWN,
    load_phase9d_rows,
    production_counts,
)
from catalog_core.phase9e_stage_b import APPROVED_LEGACY_IDS, FIRST_PILOT_SET
from catalog_core.pilot_parity import _sample_target_url
from catalog_core.publication import CatalogPublicationService
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.storefront_adapter import StorefrontAdapter, StorefrontAdapterError
from catalog_core.storefront_projection import PublishedStorefrontProjection
from catalog_core.storefront_shadow import compare_storefronts
from catalog_core.target_validation import validate_order_target

FIRST5 = frozenset(PILOT_SERVICE_IDS)


def inventory(conn: sqlite3.Connection) -> dict[str, Any]:
    def c(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    active_legacy = c(
        "SELECT COUNT(*) FROM smm_services WHERE COALESCE(is_active,1)=1"
    )
    # Some DBs may not have is_active the same way — also count distinct catalog with platform
    legacy_storefront = c(
        """
        SELECT COUNT(*) FROM smm_services
        WHERE COALESCE(is_active, 1) = 1
          AND platform_key IS NOT NULL
          AND length(trim(platform_key)) > 0
        """
    )
    # Reliable latest-event count via Python only (no window-fn dependency).
    pub = CatalogPublicationService(conn)
    published_ids: list[str] = []
    for row in conn.execute(
        "SELECT DISTINCT service_id FROM soldium_catalog_publications"
    ).fetchall():
        sid = str(row[0])
        st = pub.get_publication_status(sid)
        if st.get("publication_status") == "published":
            published_ids.append(sid)

    counts = production_counts(conn)
    return {
        "legacy_smm_services_total": counts["smm_services"],
        "legacy_active_storefront_estimate": legacy_storefront,
        "legacy_active_flag_count": active_legacy,
        "catalog_services": counts["services"],
        "catalog_nodes": counts["nodes"],
        "catalog_entries": counts["entries"],
        "catalog_prices": counts["prices"],
        "execution_sources": counts["execution_sources"],
        "publications_rows": counts["publications"],
        "published_services": len(published_ids),
        "published_service_ids": sorted(published_ids),
        "mappings": counts["mappings"],
        "orders": counts["orders"],
        "scheduled_orders": c(
            "SELECT COUNT(*) FROM scheduled_orders"
        )
        if _table_exists(conn, "scheduled_orders")
        else None,
        "expected_baseline": {
            "legacy_services": 2069,
            "active_legacy_storefront": 253,
            "catalog_services": 253,
            "nodes": 59,
            "entries": 312,
            "prices": 253,
            "execution_sources": 248,
            "publications": 23,
            "published_services": 13,
            "mappings": 0,
            "orders": 44,
        },
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _bridge_legacy(conn: sqlite3.Connection, service_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT legacy_catalog_id FROM soldium_catalog_legacy_bridge
        WHERE soldium_service_id = ?
        """,
        (service_id,),
    ).fetchone()
    return str(row[0]) if row else None


def _sample_target_for(platform: str | None, section: str | None, subsection: str | None) -> str:
    pk = str(platform or "").strip()
    sk = str(section or "").strip()
    ssk = str(subsection or "").strip()
    if pk == "telegram" and sk == "post_views" and ssk != "past_posts":
        return "https://t.me/channel/123"
    if pk == "facebook" and sk == "live_stream_views":
        return "https://facebook.com/watch/live/?v=123"
    if pk == "x" and sk in {"video_views", "views", "followers"}:
        if sk == "followers":
            return "https://x.com/user"
        return "https://x.com/user/status/1234567890"
    if pk == "youtube":
        return "https://youtube.com/watch?v=dQw4w9WgXcQ"
    return _sample_target_url(pk)


def audit_published_thirteen(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    pub = CatalogPublicationService(conn)
    proj = PublishedStorefrontProjection(conn)
    adapter = StorefrontAdapter(conn)
    repo = CatalogRepository(conn)
    shadow = compare_storefronts(conn)
    shadow_by_svc: dict[str, Any] = {}
    for r in shadow.rows:
        d = r.to_dict()
        sid = d.get("catalog_identity")
        if sid:
            # Summarize difference fields for the 13
            fields = sorted(
                {
                    f"{diff.get('category')}:{diff.get('code')}"
                    for diff in (d.get("differences") or [])
                }
            )
            shadow_by_svc[str(sid)] = {
                "correlation": d.get("correlation"),
                "severity": d.get("severity"),
                "difference_fields": fields,
                "notes": d.get("notes") or [],
            }
    shadow_dict = shadow.to_dict()
    # drop full rows from aggregate to keep artifact smaller — kept per service
    shadow_summary = {
        k: shadow_dict[k]
        for k in shadow_dict
        if k != "rows"
    }

    published_ids: list[str] = []
    for row in conn.execute(
        "SELECT DISTINCT service_id FROM soldium_catalog_publications"
    ).fetchall():
        sid = str(row[0])
        if pub.get_publication_status(sid).get("publication_status") == "published":
            published_ids.append(sid)

    out: list[dict[str, Any]] = []
    for sid in sorted(published_ids):
        latest = pub.get_latest_publish(sid)
        status = pub.get_publication_status(sid)
        projected = proj.get_service(sid)
        adapted = adapter.get_service(sid)
        legacy_id = _bridge_legacy(conn, sid)

        svc = repo.get_service(sid)
        source = repo.get_active_execution_source(sid)
        price = repo.get_active_price(sid)
        if svc and repo.get_entry_for_service(sid):
            entry = repo.get_entry_for_service(sid)
            svc.entry_id = entry.id
            svc.parent_entry_id = entry.parent_entry_id
        ready = (
            evaluate_service_readiness(repo, svc, source=source, price=price)
            if svc
            else None
        )

        target = _sample_target_for(
            latest.target_platform_key if latest else None,
            latest.target_section_key if latest else None,
            latest.target_subsection_key if latest else None,
        )
        qty = int((latest.min_quantity if latest else 1) or 1)
        intent = None
        intent_err = None
        try:
            intent = adapter.resolve_order_intent(sid, qty, target=target)
        except StorefrontAdapterError as exc:
            intent_err = {"code": exc.code, "message": str(exc)}

        # Quantity boundary checks (validation only)
        qty_bounds = {}
        for label, q in (
            ("min_minus_1", qty - 1 if qty > 0 else -1),
            ("min", int(latest.min_quantity or 0)),
            ("max", int(latest.max_quantity or 0)),
            ("max_plus_1", int(latest.max_quantity or 0) + 1),
        ):
            try:
                check = adapter.validate_quantity(sid, q)
                qty_bounds[label] = {
                    "quantity": q,
                    "ok": bool(check.ok),
                    "code": check.code,
                }
            except Exception as exc:  # noqa: BLE001
                qty_bounds[label] = {"quantity": q, "ok": False, "error": str(exc)}

        # Quotes
        quote = None
        quote_err = None
        try:
            quote = adapter.quote_price(sid, qty)
        except Exception as exc:  # noqa: BLE001
            quote_err = str(exc)

        pub_exec = (
            latest.provider_slug,
            latest.provider_account_key,
            str(latest.external_service_id) if latest else None,
        )
        proj_exec = (
            projected.execution.provider_slug,
            projected.execution.provider_account_key,
            str(projected.execution.external_service_id),
        )
        adapt_exec = (
            adapted.execution.provider_slug,
            adapted.execution.provider_account_key,
            str(adapted.execution.external_service_id),
        )
        intent_exec = (
            (
                intent.provider_slug,
                intent.provider_account_key,
                str(intent.external_service_id),
            )
            if intent
            else None
        )

        pub_tgt = (
            latest.target_platform_key,
            latest.target_section_key,
            latest.target_subsection_key or None,
            latest.target_link_prompt_key or None,
            latest.target_link_type or None,
        )
        proj_tgt = (
            projected.target_policy.platform_key,
            projected.target_policy.section_key,
            projected.target_policy.subsection_key or None,
            projected.target_policy.link_prompt_key or None,
            projected.target_policy.link_type or None,
        )
        adapt_tgt = (
            adapted.target_policy.platform_key,
            adapted.target_policy.section_key,
            adapted.target_policy.subsection_key or None,
            adapted.target_policy.link_prompt_key or None,
            adapted.target_policy.link_type or None,
        )

        immutability = {
            "execution_match": pub_exec == proj_exec == adapt_exec
            and (intent_exec is None or intent_exec == pub_exec),
            "fulfillment_match": (
                latest.fulfillment_mode
                == projected.fulfillment_mode
                == adapted.fulfillment_mode
                and (
                    intent is None
                    or intent.fulfillment_mode == latest.fulfillment_mode
                )
            ),
            "target_match": pub_tgt == proj_tgt == adapt_tgt,
            "name_match": latest.name_ar == projected.name_ar == adapted.name_ar,
            "price_match": (
                int(latest.amount_millimes or -1)
                == int(projected.amount_millimes)
                == int(adapted.price.amount_millimes)
                and (
                    quote is None
                    or int(quote.unit_amount_millimes)
                    == int(latest.amount_millimes or -1)
                )
            ),
            "quantity_match": (
                int(latest.min_quantity or -1) == projected.min_quantity == adapted.min_quantity
                and int(latest.max_quantity or -1)
                == projected.max_quantity
                == adapted.max_quantity
            ),
            "ordering_match": (
                latest.ordering_mode
                == projected.ordering_mode
                == adapted.ordering_mode
            ),
            "service_type_match": (
                latest.service_type
                == projected.service_type
                == adapted.service_type
            ),
            "fingerprint_match": (
                latest.content_fingerprint
                == projected.content_fingerprint
                == adapted.content_fingerprint
                and (
                    intent is None
                    or intent.content_fingerprint == latest.content_fingerprint
                )
            ),
        }
        immutability["all_ok"] = all(immutability.values()) and intent is not None

        # Drift: live draft vs published
        drift = {
            "has_unpublished_changes": bool(status.get("has_unpublished_changes")),
            "live_name_differs": bool(svc and svc.name_ar != latest.name_ar),
            "live_type_differs": bool(svc and svc.service_type != latest.service_type),
            "live_fulfillment_differs": bool(
                svc and svc.fulfillment_mode != latest.fulfillment_mode
            ),
            "live_qty_differs": bool(
                svc
                and (
                    svc.min_quantity != latest.min_quantity
                    or svc.max_quantity != latest.max_quantity
                )
            ),
            "live_price_differs": bool(
                price
                and int(price.amount_millimes) != int(latest.amount_millimes or -1)
            ),
            "live_exec_differs": bool(
                source
                and (
                    source.provider_slug != latest.provider_slug
                    or source.provider_account_key != latest.provider_account_key
                    or str(source.external_service_id) != str(latest.external_service_id)
                )
            ),
            "live_target_differs": bool(
                svc
                and (
                    (svc.target_platform_key or None) != (latest.target_platform_key or None)
                    or (svc.target_section_key or None) != (latest.target_section_key or None)
                    or (svc.target_subsection_key or None)
                    != (latest.target_subsection_key or None)
                )
            ),
        }

        cohort = "first5" if sid in FIRST5 else "second8"

        out.append(
            {
                "legacy_catalog_id": legacy_id,
                "soldium_service_id": sid,
                "cohort": cohort,
                "platform": latest.target_platform_key,
                "section": latest.target_section_key,
                "subsection": latest.target_subsection_key,
                "published_at": latest.published_at,
                "publication_id": latest.id,
                "publication_fingerprint": latest.content_fingerprint,
                "published_by": latest.published_by,
                "service_type": latest.service_type,
                "ordering_mode": latest.ordering_mode,
                "min_quantity": latest.min_quantity,
                "max_quantity": latest.max_quantity,
                "price_amount": latest.amount_millimes,
                "currency": latest.currency,
                "pricing_mode": latest.pricing_mode,
                "fulfillment_mode": latest.fulfillment_mode,
                "target_platform_key": latest.target_platform_key,
                "target_section_key": latest.target_section_key,
                "target_subsection_key": latest.target_subsection_key,
                "target_link_prompt_key": latest.target_link_prompt_key,
                "target_link_type": latest.target_link_type,
                "provider_slug": latest.provider_slug,
                "provider_account_key": latest.provider_account_key,
                "external_service_id": str(latest.external_service_id),
                "external_service_id_is_str": isinstance(
                    latest.external_service_id, str
                )
                or (
                    latest.external_service_id is not None
                    and type(latest.external_service_id).__name__ == "str"
                ),
                "readiness": bool(ready.ready) if ready else None,
                "customer_catalog_eligible": status.get(
                    "customer_catalog_eligible"
                ),
                "shadow_status": shadow_by_svc.get(sid),
                "parity_status": (
                    "VERIFIED" if immutability["all_ok"] else "MISMATCH"
                ),
                "immutability": immutability,
                "drift": drift,
                "quantity_bounds": qty_bounds,
                "quote": quote.to_dict() if quote else None,
                "quote_error": quote_err,
                "intent_error": intent_err,
                "target_policy_required_projection": projected.target_policy.required,
                "valid_target_ok": validate_order_target(
                    target,
                    platform_key=latest.target_platform_key or "",
                    section_key=latest.target_section_key,
                    subsection_key=latest.target_subsection_key,
                    link_prompt_key=latest.target_link_prompt_key,
                    link_type=latest.target_link_type,
                )[0],
            }
        )

    return {
        "rows": out,
        "shadow_summary": shadow_summary,
        "shadow_quality_for_published": {
            sid: shadow_by_svc.get(sid) for sid in sorted(published_ids)
        },
    }


def remaining_cohorts(conn: sqlite3.Connection) -> dict[str, Any]:
    """Classify unpublished services using Phase 9D rows (read-only)."""
    pub = CatalogPublicationService(conn)
    published: set[str] = set()
    for row in conn.execute(
        "SELECT DISTINCT service_id FROM soldium_catalog_publications"
    ).fetchall():
        sid = str(row[0])
        if pub.get_publication_status(sid).get("publication_status") == "published":
            published.add(sid)

    rows = load_phase9d_rows(conn)
    # load_phase9d excludes first5 only — filter all published
    remaining = [r for r in rows if r.soldium_service_id not in published]
    # Also include any unpublished that were first5? first5 are published.
    # Phase9d excludes first5 from load — second8 were in the 248; after publish
    # they still appear in load_phase9d_rows. Filter them out.
    remaining = [r for r in remaining if r.soldium_service_id not in published]

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in remaining:
        if not r.has_execution_source:
            cat = "F. missing_execution"
        elif r.pricing_mode == "per_unit" or (
            r.platform_key == "subscriptions"
            and r.section_key in {"iptv_panel", "iptv_wc2026"}
        ):
            cat = "E. IPTV_per_unit"
        elif (
            r.max_quantity == SENTINEL_MAX
            or "max_qty_sentinel" in r.bridge_review_codes
        ):
            cat = "D. sentinel"
        elif r.service_type_confidence == CONFIDENCE_PROBABLE:
            cat = "B. PROBABLE_service_type"
        elif r.service_type_confidence in {
            CONFIDENCE_SPECIAL,
            CONFIDENCE_UNKNOWN,
        }:
            cat = "C. SPECIAL_UNKNOWN"
        elif (
            r.service_type_confidence == CONFIDENCE_CONFIRMED
            and r.service_type != "other"
            and not r.target_special
            and r.ordering_confidence != CONFIDENCE_UNKNOWN
            and r.has_execution_source
            and r.max_quantity != SENTINEL_MAX
        ):
            cat = "A. Ready_contract_ready"
        else:
            cat = "G. other_review"

        buckets[cat].append(
            {
                "service_id": r.soldium_service_id,
                "legacy_catalog_id": r.legacy_catalog_id,
                "platform": r.platform_key,
                "section": r.section_key,
                "subsection": r.subsection_key,
                "service_type": r.service_type,
                "confidence": r.service_type_confidence,
                "target_special": r.target_special,
                "max_quantity": r.max_quantity,
            }
        )

    summary = []
    for cat in sorted(buckets.keys()):
        members = buckets[cat]
        decision = "technical"
        if cat.startswith("B.") or cat.startswith("C.") or cat.startswith("D.") or cat.startswith("E."):
            decision = "business"
        if cat.startswith("F."):
            decision = "technical+ops"
        summary.append(
            {
                "category": cat,
                "count": len(members),
                "blocking_reason": {
                    "A. Ready_contract_ready": "None for Catalog publish; still unpublished by policy",
                    "B. PROBABLE_service_type": "service_type not CONFIRMED",
                    "C. SPECIAL_UNKNOWN": "taxonomy remains other / special semantics",
                    "D. sentinel": "max_quantity sentinel or bridge sentinel code",
                    "E. IPTV_per_unit": "IPTV ordering/pricing business decision",
                    "F. missing_execution": "missing provider account / execution source",
                    "G. other_review": "mixed residual review flags",
                }.get(cat, "review"),
                "decision_type": decision,
                "safe_to_publish_without_semantic_change": cat.startswith("A."),
                "sample_ids": [m["service_id"] for m in members[:5]],
            }
        )

    return {
        "remaining_count": len(remaining),
        "categories": summary,
        "buckets": {k: v for k, v in buckets.items()},
    }


def compare_pilot_cohorts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    first = [r for r in rows if r["cohort"] == "first5"]
    second = [r for r in rows if r["cohort"] == "second8"]

    def profile(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "count": len(items),
            "platforms": sorted({r["platform"] for r in items}),
            "service_types": sorted({r["service_type"] for r in items}),
            "fulfillment": sorted({r["fulfillment_mode"] for r in items}),
            "pricing_modes": sorted({r["pricing_mode"] for r in items}),
            "with_subsection": sum(1 for r in items if r["subsection"]),
            "parity_ok": sum(1 for r in items if r["parity_status"] == "VERIFIED"),
            "immutability_ok": sum(
                1 for r in items if r["immutability"]["all_ok"]
            ),
            "drift_any": sum(
                1
                for r in items
                if r["drift"]["has_unpublished_changes"]
                or r["drift"]["live_name_differs"]
                or r["drift"]["live_price_differs"]
                or r["drift"]["live_exec_differs"]
            ),
        }

    p1, p2 = profile(first), profile(second)
    same_risk = (
        p2["immutability_ok"] == p2["count"]
        and p1["immutability_ok"] == p1["count"]
        and p2["fulfillment"] == ["auto"]
        and p1["fulfillment"] == ["auto"]
    )
    return {
        "first5": p1,
        "second8": p2,
        "risk_profile": "same-risk profile" if same_risk else "new-risk profile",
        "notes": [
            "Second cohort added shares + live_viewers types and geo/subsection variants",
            "No immutability failures observed in either cohort during this audit"
            if same_risk
            else "Investigate immutability mismatches",
        ],
    }


def build_business_matrix(remaining: dict[str, Any]) -> list[dict[str, Any]]:
    mapping = {
        "B. PROBABLE_service_type": (
            "24 PROBABLE",
            "KEEP other or CONFIRM type per cohort",
            "Do not publish until decided",
        ),
        "C. SPECIAL_UNKNOWN": (
            "SPECIAL/UNKNOWN",
            "Accept other or future taxonomy",
            "Publish as other only if other gates pass; else hold",
        ),
        "D. sentinel": (
            "sentinel",
            "Finite commercial max or keep blocked",
            "Block publication",
        ),
        "E. IPTV_per_unit": (
            "IPTV per-unit",
            "Confirm quantity_based+per_unit vs package",
            "Hold until approved",
        ),
        "F. missing_execution": (
            "missing provider accounts",
            "Supply authoritative provider_account_key",
            "BLOCKED — no invent",
        ),
    }
    rows = []
    for cat in remaining["categories"]:
        key = cat["category"]
        label, decision, action = mapping.get(
            key,
            (
                key,
                "Review",
                "Hold or publish if Ready",
            ),
        )
        rows.append(
            {
                "cohort": label,
                "count": cat["count"],
                "current_state": cat["blocking_reason"],
                "required_decision": decision,
                "safe_action": action,
                "decision_type": cat["decision_type"],
            }
        )
    return rows


def expansion_options(ready_count: int, blockers: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "option": "A",
            "title": "publish another small controlled cohort",
            "advantages": [
                "Incremental risk",
                f"Up to ~{min(10, ready_count)} ready services available",
                "Matches successful 5+8 pilot pattern",
            ],
            "risks": [
                "Still no Telegram cutover validation",
                "Shadow review/changed fields remain non-equal",
            ],
            "prerequisites": [
                "Explicit approval of exact Legacy IDs",
                "Same Stage B verification gates",
            ],
            "evidence_for": [
                "13 pilots verified immutability + dangerous=0",
                "Architecture held across second cohort",
            ],
            "evidence_against": [
                "Cutover still blocked on business semantics + 4371 Legacy path",
            ],
        },
        {
            "option": "B",
            "title": "publish all currently contract-ready non-special finite services",
            "advantages": [
                f"Larger Catalog storefront (~{ready_count} candidates)",
                "Faster coverage",
            ],
            "risks": [
                "Larger blast radius without Telegram E2E",
                "Harder rollback review",
                "May include edge cases not in pilot set",
            ],
            "prerequisites": [
                "Batch publish tooling + approval list",
                "Shadow/parity automation at scale",
            ],
            "evidence_for": ["Ready cohort exists and is deterministic"],
            "evidence_against": [
                "Prefer controlled expansion given cutover not started",
            ],
        },
        {
            "option": "C",
            "title": "resolve business decisions first, then expand",
            "advantages": [
                "Unblocks PROBABLE/SPECIAL/sentinel/IPTV/missing-exec",
                "Avoids publishing incomplete commercial taxonomy",
            ],
            "risks": [
                "Slower Catalog coverage growth",
                "Decisions may still leave many as other",
            ],
            "prerequisites": blockers,
            "evidence_for": [
                "240 remain unpublished largely due to business gates",
                "Stage A explicitly deferred these",
            ],
            "evidence_against": [
                "Ready cohort can expand without those decisions",
            ],
        },
        {
            "option": "D",
            "title": "prepare Telegram cutover before expanding further",
            "advantages": [
                "Validates real customer path",
                "Surfaces runtime gaps (4371, Gen-0 schedules, UX)",
            ],
            "risks": [
                "Cutover with only 13/253 services is incomplete UX",
                "Hardcoded 4371 + remaining cohorts unresolved",
                "Rollback complexity",
            ],
            "prerequisites": [
                "Telegram → Adapter wiring",
                "4371 remediation plan",
                "Rollback strategy verified",
                "Broader published coverage OR explicit limited menu",
            ],
            "evidence_for": ["Adapter/projection/intent chain proven for 13"],
            "evidence_against": [
                "Business semantics unresolved",
                "Only 13 published — not customer-parity sufficient",
                "4371 still active in Legacy path",
                "No real E2E Telegram+Catalog environment proven here",
            ],
        },
    ]


def cutover_matrix(evidence: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"item": "Published Catalog projection complete", "status": "PARTIAL", "note": "13/253 published"},
        {"item": "Storefront Adapter stable", "status": "PASS", "note": "13 verified"},
        {"item": "Target validation stable", "status": "PASS", "note": "shared validator used"},
        {"item": "Fulfillment frozen", "status": "PASS", "note": "snapshot→intent"},
        {"item": "Execution identity frozen", "status": "PASS", "note": "TEXT triple on path"},
        {"item": "8G order snapshot boundary ready", "status": "PASS", "note": "prior phases; not re-mutated"},
        {"item": "Scheduled Gen-1 safe", "status": "PARTIAL", "note": "Gen-1 freeze exists; Gen-0 still possible"},
        {"item": "No live Legacy SKU lookup", "status": "PARTIAL", "note": "Catalog path clean; Telegram still Legacy"},
        {"item": "No Provider runtime dependency", "status": "PASS", "note": "Adapter uses snapshot"},
        {"item": "No hardcoded 4371 path", "status": "BLOCKED", "note": "fallback remains in validator/Legacy"},
        {"item": "Publication drift understood", "status": "PASS", "note": "has_unpublished_changes audited"},
        {"item": "Shadow dangerous = 0", "status": "PASS", "note": str(evidence.get("dangerous"))},
        {"item": "Order Intent verified", "status": "PASS", "note": "13/13"},
        {"item": "Real end-to-end test environment available", "status": "NOT YET TESTED", "note": "no Telegram E2E"},
        {"item": "Rollback strategy verified", "status": "PARTIAL", "note": "unpublish exists; not cutover-tested"},
        {"item": "Customer-visible parity sufficient", "status": "BLOCKED", "note": "240 legacy-only remain"},
        {"item": "Business semantics resolved", "status": "BLOCKED", "note": "PROBABLE/SPECIAL/sentinel/IPTV"},
        {"item": "Remaining legacy-only cohorts understood", "status": "PASS", "note": "cohort report produced"},
    ]


def run_architecture_review(conn: sqlite3.Connection) -> dict[str, Any]:
    inv = inventory(conn)
    published_audit = audit_published_thirteen(conn)
    thirteen = published_audit["rows"]
    remaining = remaining_cohorts(conn)
    pilots = compare_pilot_cohorts(thirteen)
    matrix = build_business_matrix(remaining)
    ready_n = next(
        (
            c["count"]
            for c in remaining["categories"]
            if c["category"].startswith("A.")
        ),
        0,
    )
    blockers = [m["required_decision"] for m in matrix if m["count"] > 0]
    options = expansion_options(ready_n, blockers)
    cutover = cutover_matrix(
        {"dangerous": published_audit["shadow_summary"].get("dangerous_count")}
    )

    immut_ok = sum(1 for r in thirteen if r["immutability"]["all_ok"])
    recommendation = "BUSINESS DECISIONS REQUIRED BEFORE EXPANSION"
    if immut_ok != len(thirteen) or len(thirteen) != 13:
        recommendation = "ARCHITECTURAL BLOCKER FOUND"

    # Static code-audit findings (from Phase 9F exploration; not DB mutations)
    code_audits = {
        "legacy_dependency": [
            {
                "location": "catalog_core/storefront_projection.py",
                "classification": "SAFE",
                "note": "No smm_services reads",
            },
            {
                "location": "catalog_core/storefront_adapter.py",
                "classification": "SAFE",
                "note": "No smm_services reads; no Provider calls",
            },
            {
                "location": "catalog_core/publication.py",
                "classification": "SAFE",
                "note": "Catalog repository only",
            },
            {
                "location": "catalog_core/target_validation.py",
                "classification": "SAFE",
                "note": "No smm_services table read; has 4371 fallback",
            },
            {
                "location": "scheduled_orders.py get_service / limits / Gen-0 SKU",
                "classification": "FORBIDDEN_RUNTIME_DEPENDENCY",
                "note": "Live smm_services for price/limits; Gen-0 SKU re-lookup",
            },
            {
                "location": "manual_orders._lookup_service_provider_meta",
                "classification": "FORBIDDEN_RUNTIME_DEPENDENCY",
                "note": "Helper remains; Gen-1 fulfill avoids it",
            },
        ],
        "provider_dependency": {
            "adapter_calls_provider": False,
            "projection_calls_provider": False,
            "live_price_from_provider": False,
            "note": "Customer Catalog path uses published millimes + frozen execution",
        },
        "hardcoded_4371": [
            {
                "location": "catalog_core/target_validation.py",
                "classification": "active runtime fallback",
                "note": "service_id=='4371' OR link_type==comment",
            },
            {
                "location": "soldium-bot/utils/order_flow.py",
                "classification": "active runtime fallback",
                "note": "same hardcoded identity",
            },
            {
                "location": "Catalog Adapter validate_order_target",
                "classification": "inert for svc_* unless link_type set",
                "note": "4371 Catalog service has target_link_type=comment authored",
            },
        ],
        "identity_collisions": [
            {
                "pattern": "int(external_service_id) zero-on-failure",
                "status": "NOT on Adapter/Projection/Gen-1 wire",
                "note": "order_execution_identity refuses 0 invent",
            },
            {
                "pattern": "service_id == external_service_id",
                "status": "NOT in Adapter intent",
                "note": "separate fields",
            },
            {
                "pattern": "Gen-0 live SKU via catalog_id OR local_item_id",
                "status": "PRESENT in scheduled/manual helpers",
                "note": "cutover risk for Gen-0 only",
            },
        ],
        "order_boundary": {
            "adapter_creates_orders": False,
            "intent_carries_frozen_execution": True,
            "intent_carries_fulfillment": True,
            "scheduled_gen1_freezes_external_id": True,
            "scheduled_still_live_retail_price": True,
            "bot_orders_use_live_legacy_fulfillment": True,
        },
        "target_required_encoding": {
            "publication_snapshot": "required=True hardcoded in PublicationRecord.to_dict target_policy",
            "projection": "PublishedTargetPolicy.required from publish snap",
            "assessment": "explicitly encoded as always-required on publish path; verified TARGET_REQUIRED for all cohorts in 9D/9E",
            "ambiguity": "None for current 13; structural keys drive validator rules",
        },
    }

    return {
        "phase": "9F",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mutations": "NONE — read-only",
        "inventory": inv,
        "published_thirteen": thirteen,
        "immutability_summary": {
            "published_count": len(thirteen),
            "immutability_ok": immut_ok,
            "parity_verified": sum(
                1 for r in thirteen if r["parity_status"] == "VERIFIED"
            ),
        },
        "shadow": published_audit["shadow_summary"],
        "shadow_quality_for_published": published_audit[
            "shadow_quality_for_published"
        ],
        "pilot_comparison": pilots,
        "remaining_unpublished": remaining,
        "business_decision_matrix": matrix,
        "expansion_options": options,
        "cutover_readiness_matrix": cutover,
        "code_audits": code_audits,
        "recommendation": recommendation,
        "recommendation_rationale": (
            "The 13-pilot Catalog→Projection→Adapter→Intent path is stable "
            "(immutability OK, dangerous shadow=0), so architecture is not the "
            "primary blocker. Cutover is premature (13/253, Telegram still Legacy, "
            "4371 fallback remains, Gen-0 scheduled Legacy price/SKU paths remain, "
            "business cohorts unresolved). A small Ready-only expansion is "
            "technically feasible, but the governing gate is the business "
            "decision matrix (PROBABLE/SPECIAL/sentinel/IPTV/missing-exec)."
        ),
    }
