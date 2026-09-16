# -*- coding: utf-8 -*-
"""Phase 9G-A — Business Decision Pack (read-only).

No production mutations. Recommendations only; human approval required.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from catalog_core.phase9c_audit import SENTINEL_MAX
from catalog_core.phase9d_audit import (
    CONFIDENCE_CONFIRMED,
    CONFIDENCE_PROBABLE,
    CONFIDENCE_SPECIAL,
    CONFIDENCE_UNKNOWN,
    load_phase9d_rows,
    production_counts,
)
from catalog_core.phase9f_architecture_review import inventory
from catalog_core.publication import CatalogPublicationService
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.target_validation import resolve_link_prompt


def _published_ids(conn: sqlite3.Connection) -> set[str]:
    pub = CatalogPublicationService(conn)
    out: set[str] = set()
    for row in conn.execute(
        "SELECT DISTINCT service_id FROM soldium_catalog_publications"
    ).fetchall():
        sid = str(row[0])
        if pub.get_publication_status(sid).get("publication_status") == "published":
            out.add(sid)
    return out


def _example_dict(r, conn: sqlite3.Connection) -> dict[str, Any]:
    repo = CatalogRepository(conn)
    svc = repo.get_service(r.soldium_service_id)
    source = repo.get_active_execution_source(r.soldium_service_id)
    price = repo.get_active_price(r.soldium_service_id)
    ready = False
    if svc is not None:
        entry = repo.get_entry_for_service(r.soldium_service_id)
        if entry:
            svc.entry_id = entry.id
            svc.parent_entry_id = entry.parent_entry_id
        ready = bool(
            evaluate_service_readiness(
                repo, svc, source=source, price=price
            ).ready
        )
    prompt, allow_u, allow_f = resolve_link_prompt(
        r.platform_key or "", r.section_key, r.subsection_key
    )
    return {
        "legacy_catalog_id": r.legacy_catalog_id,
        "soldium_service_id": r.soldium_service_id,
        "platform": r.platform_key,
        "section": r.section_key,
        "subsection": r.subsection_key,
        "name_ar": r.name_ar,
        "legacy_category": r.legacy_category,
        "service_type": r.service_type,
        "ordering_mode": r.ordering_mode,
        "min_quantity": r.min_quantity,
        "max_quantity": r.max_quantity,
        "price_amount_millimes": r.amount_millimes,
        "currency": r.currency,
        "pricing_mode": r.pricing_mode,
        "fulfillment_mode": r.fulfillment_mode,
        "legacy_fulfillment_mode": r.legacy_fulfillment_mode,
        "target_policy": {
            "platform_key": r.target_platform_key,
            "section_key": r.target_section_key,
            "subsection_key": r.target_subsection_key,
            "link_prompt_key": r.target_link_prompt_key,
            "link_type": r.target_link_type,
            "allow_username": allow_u,
            "allow_free_text": allow_f,
            "has_special_prompt": bool(prompt),
        },
        "execution_source_status": (
            "active" if r.has_execution_source else "missing"
        ),
        "exec": {
            "provider_slug": source.provider_slug if source else None,
            "provider_account_key": source.provider_account_key if source else None,
            "external_service_id": (
                str(source.external_service_id) if source else None
            ),
        },
        "readiness": ready,
        "review_codes": list(r.review_codes),
        "phase9d_confidence": r.service_type_confidence,
        "phase9d_candidate": r.candidate_service_type,
        "target_special": r.target_special,
    }


def _cohort_key(r) -> str:
    return f"{r.platform_key or ''}::{r.section_key or ''}::{r.subsection_key or ''}"


def _cx_risk(level: str, reason: str) -> dict[str, str]:
    return {"customer_experience_risk": level, "reason": reason}


# --- PROBABLE analysis (section-level) ---------------------------------------

PROBABLE_PLAYBOOK: dict[str, dict[str, Any]] = {
    "x::spaces::": {
        "proposed_type": "live_viewers",
        "why_probable": (
            "Section sells Space listeners/audience; closest Catalog code is "
            "live_viewers, but listeners ≠ video live viewers."
        ),
        "evidence_for": [
            "Legacy section_key=spaces",
            "Shared validator special URL rule (spaces in path)",
            "Product is live audience count",
        ],
        "evidence_against": [
            "No Catalog 'space_listeners' type",
            "Semantic mismatch with live_viewers label",
        ],
        "recommendation": "KEEP_OTHER",
        "options": {
            "A_KEEP_OTHER": {
                "benefit": "Honest taxonomy; no false analytics bucket",
                "risk": "Reporting groups Spaces under 'other'",
                "customer": "Name/placement still describe Spaces; type label secondary",
                "order": "Unchanged — target rules already special-cased",
                "analytics": "Counted as other",
            },
            "B_CLASSIFY_live_viewers": {
                "benefit": "Aligns with live-audience reporting",
                "risk": "Mislabel Space listeners as live_viewers",
                "customer": "May see type label 'مشاهدو البث' which is imperfect",
                "order": "Unchanged if target keys preserved",
                "analytics": "Inflates live_viewers metrics",
            },
        },
        "cx": _cx_risk(
            "MEDIUM",
            "Wrong type label could confuse internal reports more than buyer UX; "
            "buyer still sees Arabic name + Spaces placement.",
        ),
    },
    "facebook::reactions::": {
        "proposed_type": "likes",
        "why_probable": (
            "Facebook reactions include like/love/care/etc.; closest code is likes."
        ),
        "evidence_for": [
            "Section used for post engagement reactions",
            "Closest existing Catalog code likes",
        ],
        "evidence_against": [
            "Multi-emoji reactions ≠ likes-only",
            "No Catalog 'reactions' type (forbidden to invent in 9D/9G)",
        ],
        "recommendation": "KEEP_OTHER",
        "options": {
            "A_KEEP_OTHER": {
                "benefit": "Avoids misrepresenting reaction mix",
                "risk": "other bucket grows",
                "customer": "Name shows reaction product; type secondary",
                "order": "Unchanged",
                "analytics": "other",
            },
            "B_CLASSIFY_likes": {
                "benefit": "Simple metric grouping",
                "risk": "Overstates 'likes' when product is mixed reactions",
                "customer": "May think they buy likes-only",
                "order": "Unchanged",
                "analytics": "likes inflated",
            },
        },
        "cx": _cx_risk(
            "HIGH",
            "Customer could misunderstand mixed reactions as likes-only if type "
            "is shown as likes.",
        ),
    },
    "facebook::followers_members::": {
        "proposed_type": "followers",
        "why_probable": (
            "Section title mixes page followers and group members; first pilot "
            "also used this section with service_type=other."
        ),
        "evidence_for": [
            "Often used for page growth",
            "Candidate followers from Phase 9D",
        ],
        "evidence_against": [
            "Also hosts member-like products",
            "Catalog has both followers and members",
            "Pilot deliberately left as other",
        ],
        "recommendation": "BUSINESS_REVIEW",
        "options": {
            "A_KEEP_OTHER": {
                "benefit": "Matches first-pilot precedent",
                "risk": "Ambiguous analytics",
                "customer": "Relies on Arabic name to clarify",
                "order": "Unchanged",
                "analytics": "other",
            },
            "B_CLASSIFY_followers": {
                "benefit": "Page-follower reporting",
                "risk": "Mislabels true group-member SKUs",
                "customer": "HIGH if a members product is labeled followers",
                "order": "Unchanged",
                "analytics": "followers may be wrong",
            },
            "C_SPLIT_BY_SERVICE": {
                "benefit": "Accurate per-SKU type",
                "risk": "Requires per-service business review (not section-wide)",
                "customer": "Best accuracy",
                "order": "Unchanged",
                "analytics": "Correct if split right",
            },
        },
        "cx": _cx_risk(
            "HIGH",
            "Section mixes followers and members; wrong type is customer-facing "
            "misrepresentation if type is shown.",
        ),
    },
    "youtube::engagement::": {
        "proposed_type": "likes",
        "why_probable": "Engagement section often like-oriented but may mix actions.",
        "evidence_for": [
            "Section key engagement",
            "Closest code likes",
        ],
        "evidence_against": [
            "Engagement is not a single metric",
            "May include non-like actions",
        ],
        "recommendation": "KEEP_OTHER",
        "options": {
            "A_KEEP_OTHER": {
                "benefit": "Honest mixed bucket",
                "risk": "other grows",
                "customer": "Name clarifies product",
                "order": "Unchanged",
                "analytics": "other",
            },
            "B_CLASSIFY_likes": {
                "benefit": "Simple grouping",
                "risk": "Over-narrow",
                "customer": "MEDIUM misunderstanding risk",
                "order": "Unchanged",
                "analytics": "likes may overstate",
            },
        },
        "cx": _cx_risk(
            "MEDIUM",
            "If type shown as likes, mixed engagement SKUs may be misread.",
        ),
    },
}


def analyze_probable(rows: list, conn: sqlite3.Connection) -> dict[str, Any]:
    """Caller must pass the exclusive PROBABLE partition."""
    buckets: dict[str, list] = defaultdict(list)
    for r in rows:
        buckets[_cohort_key(r)].append(r)

    cohorts = []
    for key, members in sorted(buckets.items(), key=lambda x: (-len(x[1]), x[0])):
        play = PROBABLE_PLAYBOOK.get(key)
        if not play:
            play = {
                "proposed_type": members[0].candidate_service_type,
                "why_probable": members[0].service_type_evidence,
                "evidence_for": [members[0].service_type_evidence],
                "evidence_against": ["Not in explicit playbook — treat as BUSINESS_REVIEW"],
                "recommendation": "BUSINESS_REVIEW",
                "options": {
                    "A_KEEP_OTHER": {
                        "benefit": "Conservative",
                        "risk": "Deferred taxonomy",
                        "customer": "Relies on name",
                        "order": "Unchanged",
                        "analytics": "other",
                    },
                    "B_CLASSIFY": {
                        "benefit": f"Use candidate {members[0].candidate_service_type}",
                        "risk": "Unverified",
                        "customer": "Unknown",
                        "order": "Unchanged",
                        "analytics": "depends",
                    },
                },
                "cx": _cx_risk("HIGH", "Unlisted probable cohort — do not auto-classify"),
            }
        cohorts.append(
            {
                "cohort_id": key,
                "count": len(members),
                "platform": members[0].platform_key,
                "section": members[0].section_key,
                "subsection": members[0].subsection_key,
                "current_service_type": "other",
                "proposed_type": play["proposed_type"],
                "why_probable": play["why_probable"],
                "evidence_for": play["evidence_for"],
                "evidence_against": play["evidence_against"],
                "options": play["options"],
                "recommendation": play["recommendation"],
                "customer_experience": play["cx"],
                "semantic_certainty": "LOW",
                "technical_readiness": (
                    "HIGH"
                    if all(m.has_execution_source for m in members)
                    and all(m.max_quantity != SENTINEL_MAX for m in members)
                    else "MEDIUM"
                ),
                "examples": [_example_dict(m, conn) for m in members[:3]],
                "all_legacy_ids": [m.legacy_catalog_id for m in members],
                "all_service_ids": [m.soldium_service_id for m in members],
                "publication_safe_without_decision": False,
            }
        )
    return {
        "count": sum(c["count"] for c in cohorts),
        "cohorts": cohorts,
        "conservative_recommendation": (
            "Keep all PROBABLE as service_type=other until per-cohort business "
            "approval. Do not apply Phase 9D candidates automatically."
        ),
    }


def partition_unpublished(rows: list) -> dict[str, list]:
    """Mutually exclusive remaining cohorts (Phase 9F order). Sums to |rows|."""
    buckets: dict[str, list] = {
        "ready": [],
        "probable": [],
        "special_unknown": [],
        "sentinel": [],
        "iptv": [],
        "missing_execution": [],
        "other_review": [],
    }
    for r in rows:
        if not r.has_execution_source:
            buckets["missing_execution"].append(r)
        elif r.pricing_mode == "per_unit" or (
            r.platform_key == "subscriptions"
            and r.section_key in {"iptv_panel", "iptv_wc2026"}
        ):
            buckets["iptv"].append(r)
        elif r.max_quantity == SENTINEL_MAX or "max_qty_sentinel" in (
            r.bridge_review_codes or []
        ):
            buckets["sentinel"].append(r)
        elif r.service_type_confidence == CONFIDENCE_PROBABLE:
            buckets["probable"].append(r)
        elif r.service_type_confidence in {
            CONFIDENCE_SPECIAL,
            CONFIDENCE_UNKNOWN,
        }:
            buckets["special_unknown"].append(r)
        elif (
            r.service_type_confidence == CONFIDENCE_CONFIRMED
            and r.service_type != "other"
            and not r.target_special
            and r.ordering_confidence != CONFIDENCE_UNKNOWN
            and r.has_execution_source
            and r.max_quantity != SENTINEL_MAX
        ):
            buckets["ready"].append(r)
        else:
            buckets["other_review"].append(r)
    return buckets


def analyze_special_unknown(rows: list, conn: sqlite3.Connection) -> dict[str, Any]:
    """Caller must pass the exclusive SPECIAL/UNKNOWN partition."""
    buckets: dict[str, list] = defaultdict(list)
    for r in rows:
        buckets[_cohort_key(r)].append(r)

    def classify_cohort(sk: str | None, ssk: str | None, pk: str | None) -> tuple[str, str]:
        sk = sk or ""
        ssk = ssk or ""
        pk = pk or ""
        if pk == "subscriptions" or sk in {"iptv_panel", "iptv_wc2026"}:
            return "BUSINESS_DECISION_REQUIRED", "IPTV handled in IPTV section"
        if sk in {
            "automatic_interactions",
            "post_interactions",
            "interaction",
            "mentions",
            "direct_messages",
            "start_bot",
            "direct",
            "monetization_hours",
            "other",
        }:
            if sk in {"automatic_interactions", "post_interactions", "interaction"}:
                return (
                    "SAFE_AS_OTHER",
                    "Mixed interaction products; other is honest; target rules exist "
                    "for special Telegram/X sections where applicable.",
                )
            if sk in {"mentions", "direct_messages", "start_bot"}:
                return (
                    "SAFE_AS_OTHER",
                    "Non-metric products; other correct; special target validators exist.",
                )
            if sk == "direct":
                return (
                    "TECHNICAL_REVIEW_REQUIRED",
                    "Catch-all 'مباشر' bucket — semantics not uniform across SKUs.",
                )
            if sk == "monetization_hours":
                return (
                    "SAFE_AS_OTHER",
                    "Watch-hours product; no Catalog type; ordering still quantity-based.",
                )
            return "SAFE_AS_OTHER", "Explicit other section"
        if sk == "channel_members" and ssk == "member_bundles":
            return (
                "BUSINESS_DECISION_REQUIRED",
                "Bundles members+views — publishing as single type would misrepresent.",
            )
        if sk in {"channel_members", "members", "post_share", "post_views"}:
            # May be CONFIRMED type already authored; if still special confidence
            # for target — taxonomy may be OK as typed or other
            return (
                "SAFE_AS_OTHER",
                "Special target rules; type may already be authored CONFIRMED or other.",
            )
        return (
            "BUSINESS_DECISION_REQUIRED",
            "Unlisted special/unknown — require human read of examples",
        )

    cohorts = []
    for key, members in sorted(buckets.items(), key=lambda x: (-len(x[1]), x[0])):
        verdict, why = classify_cohort(
            members[0].section_key, members[0].subsection_key, members[0].platform_key
        )
        # If already has CONFIRMED authored type (not other), note it
        types = sorted({m.service_type for m in members})
        tech_pub_possible = all(
            m.has_execution_source
            and m.max_quantity != SENTINEL_MAX
            and m.target_platform_key
            and m.amount_millimes
            and int(m.amount_millimes) > 0
            for m in members
        )
        cx = "LOW"
        if verdict == "BUSINESS_DECISION_REQUIRED":
            cx = "HIGH"
        elif verdict == "TECHNICAL_REVIEW_REQUIRED":
            cx = "MEDIUM"
        elif members[0].target_special:
            cx = "MEDIUM"

        cohorts.append(
            {
                "cohort_id": key,
                "count": len(members),
                "platform": members[0].platform_key,
                "section": members[0].section_key,
                "subsection": members[0].subsection_key,
                "current_types": types,
                "classification": verdict,
                "why": why,
                "other_is_acceptable": verdict
                in {"SAFE_AS_OTHER", "TECHNICAL_REVIEW_REQUIRED"},
                "affects_customer_ordering": members[0].target_special,
                "target_policy_sufficient": bool(
                    members[0].target_platform_key and members[0].target_section_key
                ),
                "technical_publication_possible": tech_pub_possible,
                "customer_experience": _cx_risk(
                    cx,
                    why,
                ),
                "semantic_certainty": (
                    "MEDIUM" if verdict == "SAFE_AS_OTHER" else "LOW"
                ),
                "examples": [_example_dict(m, conn) for m in members[:2]],
                "all_legacy_ids": [m.legacy_catalog_id for m in members],
            }
        )
    by_cls: Counter[str] = Counter()
    for c in cohorts:
        by_cls[c["classification"]] += c["count"]
    return {
        "count": sum(c["count"] for c in cohorts),
        "by_classification": dict(by_cls),
        "cohorts": cohorts,
        "conservative_recommendation": (
            "Keep taxonomy as other where SAFE_AS_OTHER. Do not invent types. "
            "Hold member_bundles and catch-all direct for business/technical review. "
            "IPTV decided in IPTV section."
        ),
    }


def analyze_sentinel(rows: list, conn: sqlite3.Connection) -> dict[str, Any]:
    """Caller must pass the exclusive sentinel partition."""
    items = []
    for r in rows:
        actual = r.max_quantity == SENTINEL_MAX
        bridge = "max_qty_sentinel" in (r.bridge_review_codes or [])
        # legacy max from smm
        leg = conn.execute(
            "SELECT max_qty, min_qty FROM smm_services WHERE catalog_id=?",
            (r.legacy_catalog_id,),
        ).fetchone()
        kind = (
            "legacy_database_sentinel"
            if actual
            else "bridge_review_without_current_sentinel"
        )
        items.append(
            {
                **_example_dict(r, conn),
                "legacy_max_qty": int(leg[0]) if leg and leg[0] is not None else None,
                "legacy_min_qty": int(leg[1]) if leg and leg[1] is not None else None,
                "bridge_has_max_qty_sentinel": bridge,
                "catalog_max_is_sentinel": actual,
                "sentinel_kind": kind,
                "inference": "unknown — no authoritative finite commercial max found",
                "options": {
                    "BLOCK_UNTIL_VERIFIED": "Do not publish; keep current max",
                    "EXPLICIT_BUSINESS_MAX": "Business provides finite max with evidence",
                    "SPECIAL_ORDERING_RULE": "Only if product is truly unbounded (unproven)",
                },
                "recommendation": "BLOCK_UNTIL_VERIFIED",
                "finite_max_supported_by_evidence": False,
                "customer_experience": _cx_risk(
                    "HIGH",
                    "Publishing with INT32 sentinel max could allow absurd quantity "
                    "quotes / UX.",
                ),
                "semantic_certainty": "LOW",
                "technical_readiness": "LOW",
            }
        )
    return {
        "count": len(items),
        "actual_sentinel_count": sum(1 for i in items if i["catalog_max_is_sentinel"]),
        "bridge_only_count": sum(
            1 for i in items if not i["catalog_max_is_sentinel"]
        ),
        "services": items,
        "conservative_recommendation": (
            "BLOCK_UNTIL_VERIFIED for all. Never invent a finite max. "
            "Do not replace 2147483647 without explicit business evidence."
        ),
    }


def analyze_iptv(rows: list, conn: sqlite3.Connection) -> dict[str, Any]:
    """Caller must pass the exclusive IPTV partition."""
    cohorts: dict[str, list] = defaultdict(list)
    for r in rows:
        cohorts[_cohort_key(r)].append(r)

    out = []
    for key, members in sorted(cohorts.items()):
        sample = members[0]
        out.append(
            {
                "cohort_id": key,
                "count": len(members),
                "customer_orders": (
                    "One IPTV access/panel unit (min=max=1 historically)"
                ),
                "quantity_meaningful": False,
                "fixed_package_proven": False,
                "pricing_mode": sample.pricing_mode,
                "ordering_mode": sample.ordering_mode,
                "fulfillment": sample.fulfillment_mode,
                "target": "free_text (subscriptions validator)",
                "publication_misrepresentation_risk": (
                    "MEDIUM — if shown as quantity SMM metric product"
                ),
                "options": {
                    "KEEP_PER_UNIT": "Retain per_unit + quantity_based qty=1",
                    "CONVERT_TO_FIXED_PACKAGE": "Only if Catalog contract + business prove it",
                    "KEEP_UNPUBLISHED": "Safest until product UX approved",
                    "BUSINESS_REVIEW": "Confirm product narrative + admin fulfillment UX",
                },
                "recommendation": "KEEP_UNPUBLISHED",
                "customer_experience": _cx_risk(
                    "HIGH",
                    "IPTV is not a social metric; publishing into SMM-like storefront "
                    "without dedicated UX risks misunderstanding.",
                ),
                "semantic_certainty": "MEDIUM",
                "technical_readiness": "MEDIUM",
                "examples": [_example_dict(m, conn) for m in members],
            }
        )
    return {
        "count": sum(c["count"] for c in out),
        "cohorts": out,
        "conservative_recommendation": (
            "KEEP_UNPUBLISHED until business approves IPTV storefront UX. "
            "KEEP_PER_UNIT is technically defensible; CONVERT_TO_FIXED_PACKAGE "
            "not evidenced."
        ),
    }


def analyze_missing_execution(rows: list, conn: sqlite3.Connection) -> dict[str, Any]:
    """Caller must pass the exclusive missing-execution partition."""
    services = []
    for r in rows:
        bridge = conn.execute(
            """
            SELECT provider_slug, provider_api_account, external_service_id,
                   classification, review_codes
            FROM soldium_catalog_legacy_bridge WHERE soldium_service_id=?
            """,
            (r.soldium_service_id,),
        ).fetchone()
        smm = conn.execute(
            """
            SELECT provider_slug, provider_api_account, external_service_id
            FROM smm_services WHERE catalog_id=?
            """,
            (r.legacy_catalog_id,),
        ).fetchone()
        account = None
        if bridge:
            account = bridge["provider_api_account"]
        if smm and not account:
            account = smm["provider_api_account"]
        services.append(
            {
                **_example_dict(r, conn),
                "provider_slug": (bridge["provider_slug"] if bridge else None)
                or (smm["provider_slug"] if smm else None),
                "external_service_id": (
                    bridge["external_service_id"] if bridge else None
                )
                or (smm["external_service_id"] if smm else None),
                "provider_account_key": account,
                "why_account_missing": "missing_provider_account in bridge review_codes",
                "external_id_known": bool(
                    (bridge and bridge["external_service_id"])
                    or (smm and smm["external_service_id"])
                ),
                "mapping_exists": False,
                "account_safely_inferable": False,
                "recommendation": "OPS_ACTION_REQUIRED",
                "customer_experience": _cx_risk(
                    "HIGH",
                    "Cannot fulfill without execution identity — must stay unpublished.",
                ),
                "do_not_invent_account": True,
                "do_not_create_mapping": True,
            }
        )
    return {
        "count": len(services),
        "services": services,
        "conservative_recommendation": (
            "OPS_ACTION_REQUIRED: assign authoritative provider_account_key for "
            "Legacy 2128, 2326, 2405, 4590, 4721. Then re-evaluate readiness. "
            "Never invent accounts or auto-map."
        ),
    }


def analyze_other_review(rows: list, conn: sqlite3.Connection) -> dict[str, Any]:
    """Caller must pass the exclusive other-review partition."""
    residual = list(rows)

    by_reason: dict[str, list] = defaultdict(list)
    for r in residual:
        reasons = []
        if r.target_special:
            reasons.append("target_special_with_confirmed_or_mixed_type")
        if r.ordering_confidence == CONFIDENCE_UNKNOWN:
            reasons.append("ordering_mode_unknown")
        if "per_unit" in (r.bridge_review_codes or []) or r.pricing_mode == "per_unit":
            reasons.append("per_unit_non_iptv")
        if r.service_type == "other" and r.service_type_confidence == CONFIDENCE_CONFIRMED:
            reasons.append("confirmed_section_but_type_still_other")
        if not reasons:
            reasons.append("mixed_residual_flags")
        by_reason[",".join(reasons)].append(r)

    groups = []
    for reason, members in sorted(by_reason.items(), key=lambda x: (-len(x[1]), x[0])):
        tech = "technical" if "target_special" in reason else "business"
        if "ordering" in reason or "per_unit" in reason:
            tech = "business"
        groups.append(
            {
                "reason": reason,
                "count": len(members),
                "decision_type": tech,
                "publication_safe": False,
                "required_action": (
                    "Review examples; special-target CONFIRMED types may be "
                    "technically publishable but need UX sign-off for special rules"
                    if "target_special" in reason
                    else "Business/technical review of residual flags"
                ),
                "examples": [_example_dict(m, conn) for m in members[:3]],
                "all_legacy_ids": [m.legacy_catalog_id for m in members],
                "customer_experience": _cx_risk(
                    "MEDIUM",
                    reason,
                ),
            }
        )
    return {
        "count": len(residual),
        "groups": groups,
        "conservative_recommendation": (
            "Do not treat residual as Ready. Special-target + typed services need "
            "explicit UX approval before any expansion that includes them."
        ),
    }


def analyze_ready_pool(rows: list, conn: sqlite3.Connection) -> dict[str, Any]:
    """Caller must pass the exclusive Ready partition (Phase 9F bucket A)."""
    ready = list(rows)

    # Split technical vs business-ready (policy distinction — mandatory)
    tech_only = []
    business_ready = []
    for r in ready:
        if (
            r.fulfillment_mode == "auto"
            and r.legacy_fulfillment_mode == "auto"
            and not r.allow_free_text
            and r.pricing_mode == "per_1000"
            and r.ordering_mode == "quantity_based"
        ):
            business_ready.append(r)
        else:
            tech_only.append(r)

    def brief(members: list) -> list[dict[str, Any]]:
        return [
            {
                "legacy_catalog_id": m.legacy_catalog_id,
                "soldium_service_id": m.soldium_service_id,
                "platform": m.platform_key,
                "section": m.section_key,
                "subsection": m.subsection_key,
                "service_type": m.service_type,
                "name_ar": m.name_ar,
                "min_quantity": m.min_quantity,
                "max_quantity": m.max_quantity,
                "fulfillment_mode": m.fulfillment_mode,
            }
            for m in members
        ]

    return {
        "count": len(ready),
        "technically_ready_only_count": len(tech_only),
        "business_ready_count": len(business_ready),
        "note": (
            "TECHNICALLY_READY_ONLY means Catalog readiness gates pass. "
            "BUSINESS_READY additionally requires ordinary auto/per_1000/"
            "non-free-text target semantics suitable for metric storefront."
        ),
        "business_ready_examples": brief(business_ready[:10]),
        "technically_ready_only_examples": brief(tech_only[:5]),
        "all_business_ready_legacy_ids": [
            m.legacy_catalog_id for m in business_ready
        ],
        "all_business_ready_service_ids": [
            m.soldium_service_id for m in business_ready
        ],
    }


def propose_next_pilot(
    business_ready: list[dict[str, Any]], *, target_n: int = 15
) -> dict[str, Any]:
    """Select ~10–20 diverse BUSINESS_READY candidates (recommendation only)."""
    # business_ready examples are briefs; need full list from IDs in analyze_ready
    return {
        "target_size": target_n,
        "selection_policy": (
            "BUSINESS_READY only: CONFIRMED type, non-special target, finite qty, "
            "exec+price, auto, per_1000, no free-text; platform/type diversity"
        ),
        "candidates": business_ready[:target_n],
        "do_not_publish": True,
    }


def build_decision_pack(conn: sqlite3.Connection) -> dict[str, Any]:
    published = _published_ids(conn)
    all_rows = load_phase9d_rows(conn)
    rows = [r for r in all_rows if r.soldium_service_id not in published]
    inv = inventory(conn)
    counts_before = production_counts(conn)

    parts = partition_unpublished(rows)
    assert sum(len(v) for v in parts.values()) == len(rows)

    probable = analyze_probable(parts["probable"], conn)
    special = analyze_special_unknown(parts["special_unknown"], conn)
    sentinel = analyze_sentinel(parts["sentinel"], conn)
    iptv = analyze_iptv(parts["iptv"], conn)
    missing = analyze_missing_execution(parts["missing_execution"], conn)
    other = analyze_other_review(parts["other_review"], conn)
    ready = analyze_ready_pool(parts["ready"], conn)

    # Build next pilot from full business_ready member list
    business_ready_ids = set(ready["all_business_ready_service_ids"])
    business_ready_rows = [
        r for r in parts["ready"] if r.soldium_service_id in business_ready_ids
    ]

    # Diversity pick
    selected = []
    platforms: set[str] = set()
    types: set[str] = set()
    for r in sorted(
        business_ready_rows,
        key=lambda x: (
            x.platform_key or "",
            x.section_key or "",
            x.soldium_service_id,
        ),
    ):
        if len(selected) >= 15:
            break
        bonus = 0
        if r.platform_key not in platforms:
            bonus += 2
        if r.service_type not in types:
            bonus += 1
        # Prefer adding new platforms first: simple greedy
        selected.append(r)
        platforms.add(r.platform_key or "")
        types.add(r.service_type or "")

    # Re-pick with diversity preference
    selected = []
    platforms = set()
    types = set()
    sections = set()
    pool = list(business_ready_rows)
    while pool and len(selected) < 15:
        best = None
        best_score = -1
        for r in pool:
            score = 10
            if (r.platform_key or "") not in platforms:
                score += 30
            if (r.service_type or "") not in types:
                score += 20
            if (r.section_key or "") not in sections:
                score += 10
            same_p = sum(1 for s in selected if s.platform_key == r.platform_key)
            if same_p >= 3:
                score -= 25
            if score > best_score:
                best_score = score
                best = r
        if best is None:
            break
        selected.append(best)
        platforms.add(best.platform_key or "")
        types.add(best.service_type or "")
        sections.add(best.section_key or "")
        pool = [r for r in pool if r.soldium_service_id != best.soldium_service_id]

    next_pilot = {
        "recommended_size": len(selected),
        "do_not_publish": True,
        "candidates": [
            {
                "legacy_catalog_id": r.legacy_catalog_id,
                "soldium_service_id": r.soldium_service_id,
                "platform": r.platform_key,
                "section": r.section_key,
                "subsection": r.subsection_key,
                "service_type": r.service_type,
                "name_ar": r.name_ar,
                "min_quantity": r.min_quantity,
                "max_quantity": r.max_quantity,
            }
            for r in selected
        ],
    }

    risk_matrix = [
        {
            "cohort": "PROBABLE (24)",
            "count": probable["count"],
            "semantic_certainty": "LOW",
            "technical_readiness": "MEDIUM",
            "customer_risk": "HIGH",
            "decision": "KEEP_OTHER / BUSINESS_REVIEW",
        },
        {
            "cohort": "SPECIAL/UNKNOWN",
            "count": special["count"],
            "semantic_certainty": "MEDIUM",
            "technical_readiness": "MEDIUM",
            "customer_risk": "MEDIUM",
            "decision": "SAFE_AS_OTHER for most; hold bundles/direct",
        },
        {
            "cohort": "Sentinel",
            "count": sentinel["count"],
            "semantic_certainty": "LOW",
            "technical_readiness": "LOW",
            "customer_risk": "HIGH",
            "decision": "BLOCK_UNTIL_VERIFIED",
        },
        {
            "cohort": "IPTV",
            "count": iptv["count"],
            "semantic_certainty": "MEDIUM",
            "technical_readiness": "MEDIUM",
            "customer_risk": "HIGH",
            "decision": "KEEP_UNPUBLISHED",
        },
        {
            "cohort": "Missing execution",
            "count": missing["count"],
            "semantic_certainty": "N/A",
            "technical_readiness": "LOW",
            "customer_risk": "HIGH",
            "decision": "OPS_ACTION_REQUIRED",
        },
        {
            "cohort": "Other review",
            "count": other["count"],
            "semantic_certainty": "LOW",
            "technical_readiness": "MEDIUM",
            "customer_risk": "MEDIUM",
            "decision": "Hold pending group review",
        },
        {
            "cohort": "Ready / BUSINESS_READY",
            "count": ready["business_ready_count"],
            "semantic_certainty": "HIGH",
            "technical_readiness": "HIGH",
            "customer_risk": "LOW",
            "decision": "Eligible for future pilot approval",
        },
    ]

    recommended_set = {
        "remain_other": [
            "All PROBABLE cohorts (spaces, reactions, followers_members, engagement)",
            "SAFE_AS_OTHER special cohorts (interactions, mentions, DMs, start_bot, …)",
        ],
        "probable_accept_none_without_approval": True,
        "remain_blocked": [
            "All sentinel services",
            "All missing-execution services",
            "member_bundles",
            "IPTV until UX approval",
        ],
        "sentinel_policy": "BLOCK_UNTIL_VERIFIED — no invented max",
        "iptv_policy": "KEEP_UNPUBLISHED; KEEP_PER_UNIT if later approved",
        "provider_account_action": (
            "Ops assigns provider_account_key for Legacy 2128, 2326, 2405, 4590, 4721"
        ),
        "next_pilot_pool": next_pilot,
        "ready_expansion_policy": (
            "Only BUSINESS_READY after explicit approval; never auto-publish all 85"
        ),
    }

    cutover_blockers = [
        "4371 hardcoded fallback in shared validator / bot",
        "Gen-0 scheduled order Legacy SKU lookup",
        "Live Legacy retail price in scheduled flow",
        "Telegram E2E not tested",
        "Customer parity only 13/253",
        "Rollback strategy not cutover-verified",
    ]

    approval_checklist = [
        {
            "id": "prob_keep_other",
            "question": "Approve keeping all currently PROBABLE services as other?",
            "default_recommendation": "YES",
        },
        {
            "id": "special_safe_as_other",
            "question": "Approve SAFE_AS_OTHER handling for special/unknown (except bundles/direct/IPTV)?",
            "default_recommendation": "YES",
        },
        {
            "id": "sentinel_block",
            "question": "Approve BLOCK_UNTIL_VERIFIED for all sentinel services (no invented max)?",
            "default_recommendation": "YES",
        },
        {
            "id": "iptv_unpublished",
            "question": "Approve keeping IPTV unpublished until dedicated UX decision?",
            "default_recommendation": "YES",
        },
        {
            "id": "ops_five_accounts",
            "question": "Authorize ops to assign Provider accounts for the five missing-exec services?",
            "default_recommendation": "REQUIRED for those five only",
        },
        {
            "id": "next_pilot",
            "question": f"Approve next pilot cohort of {len(selected)} BUSINESS_READY services (list in pack)?",
            "default_recommendation": "PENDING explicit ID list approval",
        },
        {
            "id": "ready_expansion_policy",
            "question": "Approve policy: expand only from BUSINESS_READY after per-wave approval?",
            "default_recommendation": "YES",
        },
    ]

    counts_after = production_counts(conn)
    return {
        "phase": "9G-A",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mutations": "NONE — read-only business decision pack",
        "verdict": "BUSINESS DECISION PACK COMPLETE",
        "executive_summary": {
            "published": len(published),
            "remaining_unpublished": len(rows),
            "probable": probable["count"],
            "special_unknown": special["count"],
            "sentinel": sentinel["count"],
            "iptv": iptv["count"],
            "missing_execution": missing["count"],
            "other_review": other["count"],
            "ready_total": ready["count"],
            "business_ready": ready["business_ready_count"],
            "technically_ready_only": ready["technically_ready_only_count"],
            "headline": (
                "Catalog architecture for 13 published services is stable. "
                "Expansion is gated by business decisions, not by Adapter/Projection "
                "failures. Conservative path: keep PROBABLE as other, block "
                "sentinel/IPTV/missing-exec, hold special bundles, optionally "
                "approve a BUSINESS_READY pilot wave later."
            ),
        },
        "cohort_inventory": {
            "ready": len(parts["ready"]),
            "probable": len(parts["probable"]),
            "special_unknown": len(parts["special_unknown"]),
            "sentinel": len(parts["sentinel"]),
            "iptv": len(parts["iptv"]),
            "missing_execution": len(parts["missing_execution"]),
            "other_review": len(parts["other_review"]),
            "sum": sum(len(v) for v in parts.values()),
            "remaining_unpublished": len(rows),
            "partition_exclusive": True,
            "matches_phase9f": {
                "ready": 85,
                "probable": 24,
                "special_unknown": 69,
                "sentinel": 22,
                "iptv": 7,
                "missing_execution": 5,
                "other_review": 28,
            },
        },
        "inventory": inv,
        "production_counts_before": counts_before,
        "production_counts_after": counts_after,
        "production_unchanged": counts_before == counts_after,
        "probable": probable,
        "special_unknown": special,
        "sentinel": sentinel,
        "iptv": iptv,
        "missing_execution": missing,
        "other_review": other,
        "ready_expansion_pool": ready,
        "business_risk_matrix": risk_matrix,
        "cross_cohort_risks": [
            "Do not equate readiness.ready with business-safe to publish",
            "Special target rules can be technically publishable but UX-sensitive",
            "Sentinel max must never be silently normalized",
            "Provider account invent is forbidden",
            "Cutover blockers remain independent of taxonomy decisions",
        ],
        "recommended_decision_set": recommended_set,
        "next_expansion_cohort": next_pilot,
        "cutover_blockers": cutover_blockers,
        "approval_checklist": approval_checklist,
    }
