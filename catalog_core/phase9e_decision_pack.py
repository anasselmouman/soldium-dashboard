# -*- coding: utf-8 -*-
"""Phase 9E Stage A — Business Decision Pack (read-only).

No authoring. No publication. No Stage B.
"""

from __future__ import annotations

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
    LEGACY_COMMENT_SERVICE_ID,
    Phase9DRow,
    classify_target_required,
    load_phase9d_rows,
    production_counts,
)
from catalog_core.publication import CatalogPublicationService
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.target_validation import resolve_link_prompt

PILOT_SET = frozenset(PILOT_SERVICE_IDS)

MISSING_EXEC_LEGACY_IDS = frozenset({"2128", "4721", "2326", "4590", "2405"})

# Explicit Stage A recommended actions (human must approve — never auto-apply).
ACTION_CONFIRM = "CONFIRM"
ACTION_KEEP_OTHER = "KEEP OTHER"
ACTION_NEED_BUSINESS_DECISION = "NEED BUSINESS DECISION"
ACTION_KEEP_SENTINEL_BLOCK = "KEEP SENTINEL / BLOCK PUBLICATION"
ACTION_FINITE_MAX_CONFIRMED = "FINITE MAX CONFIRMED"
ACTION_BLOCKED = "BLOCKED"
ACTION_REVIEW_ONLY = "REVIEW ONLY"
ACTION_SAFE_FOR_FUTURE_PUBLISH = "SAFE FOR FUTURE PUBLICATION (after Stage B approval)"
ACTION_TARGET_REVIEW = "TARGET POLICY REVIEW BEFORE PUBLICATION"


def _cohort_id(platform: str | None, section: str | None, subsection: str | None) -> str:
    return f"{platform or ''}::{section or ''}::{subsection or ''}"


def _row_current(r: Phase9DRow) -> dict[str, Any]:
    return {
        "service_type": r.service_type,
        "ordering_mode": r.ordering_mode,
        "min_quantity": r.min_quantity,
        "max_quantity": r.max_quantity,
        "pricing_mode": r.pricing_mode,
        "amount_millimes": r.amount_millimes,
        "currency": r.currency,
        "fulfillment_mode": r.fulfillment_mode,
        "legacy_fulfillment_mode": r.legacy_fulfillment_mode,
        "target_platform_key": r.target_platform_key,
        "target_section_key": r.target_section_key,
        "target_subsection_key": r.target_subsection_key,
        "target_link_type": r.target_link_type,
        "target_link_prompt_key": r.target_link_prompt_key,
        "has_execution_source": r.has_execution_source,
    }


def _decision(
    *,
    cohort_id: str,
    category: str,
    rows: list[Phase9DRow],
    proposed_decision: str,
    recommended_action: str,
    confidence: str,
    evidence_for: list[str],
    evidence_against: list[str],
    risks: list[str],
    publication_impact: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sample = rows[0]
    prompt, allow_u, allow_f = resolve_link_prompt(
        sample.platform_key or "",
        sample.section_key,
        sample.subsection_key,
    )
    req, kind, _, _ = classify_target_required(
        platform_key=sample.platform_key,
        section_key=sample.section_key,
        subsection_key=sample.subsection_key,
    )
    out: dict[str, Any] = {
        "cohort_id": cohort_id,
        "category": category,
        "platform": sample.platform_key,
        "section": sample.section_key,
        "subsection": sample.subsection_key,
        "service_count": len(rows),
        "affected_service_ids": [r.soldium_service_id for r in rows],
        "legacy_catalog_ids": [r.legacy_catalog_id for r in rows],
        "current_catalog_values": {
            "service_types": sorted({r.service_type for r in rows}),
            "ordering_modes": sorted({r.ordering_mode for r in rows}),
            "pricing_modes": sorted({r.pricing_mode or "" for r in rows}),
            "fulfillment_modes": sorted({r.fulfillment_mode for r in rows}),
            "min_quantity_range": [
                min(r.min_quantity for r in rows),
                max(r.min_quantity for r in rows),
            ],
            "max_quantity_range": [
                min(r.max_quantity for r in rows),
                max(r.max_quantity for r in rows),
            ],
        },
        "legacy_evidence": {
            "section_key": sample.section_key,
            "subsection_key": sample.subsection_key,
            "legacy_fulfillment_mode": sorted(
                {r.legacy_fulfillment_mode for r in rows}
            ),
            "legacy_category_samples": sorted(
                {r.legacy_category or "" for r in rows}
            )[:5],
            "phase9d_confidence": sample.service_type_confidence,
            "phase9d_candidate": sample.candidate_service_type,
            "phase9d_evidence": sample.service_type_evidence,
        },
        "application_behavior_evidence": evidence_for,
        "evidence_against": evidence_against,
        "target_behavior": {
            "target_required": req,
            "target_kind": kind,
            "allow_username": allow_u,
            "allow_free_text": allow_f,
            "has_special_prompt": bool(prompt),
            "prompt_preview": (prompt or "")[:160] or None,
            "target_special": sample.target_special,
            "structural_keys": {
                "platform_key": sample.target_platform_key,
                "section_key": sample.target_section_key,
                "subsection_key": sample.target_subsection_key,
                "link_type": sample.target_link_type,
                "link_prompt_key": sample.target_link_prompt_key,
            },
        },
        "fulfillment_behavior": {
            "catalog": sorted({r.fulfillment_mode for r in rows}),
            "legacy_bridge": sorted({r.legacy_fulfillment_mode for r in rows}),
        },
        "quantity_behavior": {
            "decisions": sorted({r.quantity_decision for r in rows}),
            "sentinel_count": sum(
                1 for r in rows if r.max_quantity == SENTINEL_MAX
            ),
            "bridge_sentinel_code_count": sum(
                1 for r in rows if "max_qty_sentinel" in r.bridge_review_codes
            ),
        },
        "pricing_behavior": {
            "modes": sorted({r.pricing_mode or "" for r in rows}),
            "currencies": sorted({r.currency or "" for r in rows}),
        },
        "proposed_decision": proposed_decision,
        "confidence": confidence,
        "risks": risks,
        "publication_impact": publication_impact,
        "recommended_action": recommended_action,
    }
    if extra:
        out.update(extra)
    return out


def build_probable_decisions(rows: list[Phase9DRow]) -> list[dict[str, Any]]:
    buckets: dict[str, list[Phase9DRow]] = defaultdict(list)
    for r in rows:
        if r.service_type_confidence != CONFIDENCE_PROBABLE:
            continue
        buckets[_cohort_id(r.platform_key, r.section_key, r.subsection_key)].append(r)

    decisions: list[dict[str, Any]] = []
    for cid, members in sorted(buckets.items(), key=lambda x: (-len(x[1]), x[0])):
        sk = members[0].section_key
        cand = members[0].candidate_service_type
        if sk == "followers_members":
            decisions.append(
                _decision(
                    cohort_id=cid,
                    category="PROBABLE_SERVICE_TYPE",
                    rows=members,
                    proposed_decision=(
                        f"Candidate Catalog type '{cand}' vs possible 'members'; "
                        "Facebook section mixes page followers and group members."
                    ),
                    recommended_action=ACTION_NEED_BUSINESS_DECISION,
                    confidence=CONFIDENCE_PROBABLE,
                    evidence_for=[
                        "Legacy section_key=followers_members used for page growth",
                        "Pilot facebook/followers_members already published with "
                        "service_type=other (no forced mapping)",
                        f"Phase 9D candidate={cand} as majority commercial reading",
                    ],
                    evidence_against=[
                        "Section title mixes followers AND members",
                        "Catalog has both 'followers' and 'members' codes",
                        "Wrong type would mis-label storefront commercial profile",
                    ],
                    risks=[
                        "Confirming as followers hides true group-member products",
                        "Confirming as members mislabels page-follower products",
                    ],
                    publication_impact=(
                        "Do not publish until business picks followers | members | other"
                    ),
                    extra={
                        "can_safely_become_confirmed": False,
                        "stage_a_verdict": ACTION_NEED_BUSINESS_DECISION,
                    },
                )
            )
        elif sk == "reactions":
            decisions.append(
                _decision(
                    cohort_id=cid,
                    category="PROBABLE_SERVICE_TYPE",
                    rows=members,
                    proposed_decision=(
                        "Candidate 'likes' — Facebook reactions include like/love/"
                        "care/etc., not like-only."
                    ),
                    recommended_action=ACTION_NEED_BUSINESS_DECISION,
                    confidence=CONFIDENCE_PROBABLE,
                    evidence_for=[
                        "Section sells engagement reactions on posts",
                        "Closest existing Catalog code is likes",
                    ],
                    evidence_against=[
                        "Reactions are multi-emoji, not Catalog 'likes' alone",
                        "No Catalog 'reactions' type exists (and Phase 9E forbids new types)",
                    ],
                    risks=["Misrepresenting reaction mix as likes"],
                    publication_impact="Keep other until taxonomy/business decision",
                    extra={
                        "can_safely_become_confirmed": False,
                        "stage_a_verdict": ACTION_NEED_BUSINESS_DECISION,
                        "future_taxonomy_note": (
                            "Optional future type 'reactions' — NOT created this phase"
                        ),
                    },
                )
            )
        elif sk == "engagement":
            decisions.append(
                _decision(
                    cohort_id=cid,
                    category="PROBABLE_SERVICE_TYPE",
                    rows=members,
                    proposed_decision=(
                        "Candidate 'likes' — YouTube engagement section mixes "
                        "likes and other interactions."
                    ),
                    recommended_action=ACTION_NEED_BUSINESS_DECISION,
                    confidence=CONFIDENCE_PROBABLE,
                    evidence_for=[
                        "Section key engagement used for like-oriented products",
                        "Closest existing code is likes",
                    ],
                    evidence_against=[
                        "Engagement is not a single metric type",
                        "May include non-like actions",
                    ],
                    risks=["Over-narrow typing"],
                    publication_impact="Keep other pending decision",
                    extra={
                        "can_safely_become_confirmed": False,
                        "stage_a_verdict": ACTION_NEED_BUSINESS_DECISION,
                    },
                )
            )
        elif sk == "spaces":
            decisions.append(
                _decision(
                    cohort_id=cid,
                    category="PROBABLE_SERVICE_TYPE",
                    rows=members,
                    proposed_decision=(
                        "Candidate 'live_viewers' for Space listeners — special "
                        "URL rule (spaces in path) already in shared validator."
                    ),
                    recommended_action=ACTION_NEED_BUSINESS_DECISION,
                    confidence=CONFIDENCE_PROBABLE,
                    evidence_for=[
                        "Product is audience/listeners for live Spaces",
                        "Closest Catalog code is live_viewers",
                        "Target validator already special-cases section=spaces",
                    ],
                    evidence_against=[
                        "Listeners ≠ video live viewers semantically",
                        "No Catalog 'space_listeners' type (do not invent)",
                    ],
                    risks=["Commercial label mismatch"],
                    publication_impact=(
                        "Type decision separate from target safety; target rules exist"
                    ),
                    extra={
                        "can_safely_become_confirmed": False,
                        "stage_a_verdict": ACTION_NEED_BUSINESS_DECISION,
                    },
                )
            )
        else:
            decisions.append(
                _decision(
                    cohort_id=cid,
                    category="PROBABLE_SERVICE_TYPE",
                    rows=members,
                    proposed_decision=f"Unresolved PROBABLE section={sk}",
                    recommended_action=ACTION_NEED_BUSINESS_DECISION,
                    confidence=CONFIDENCE_PROBABLE,
                    evidence_for=[members[0].service_type_evidence],
                    evidence_against=["Not listed in explicit Stage A analyses"],
                    risks=["Unknown semantics"],
                    publication_impact="Block type authoring and publication",
                    extra={
                        "can_safely_become_confirmed": False,
                        "stage_a_verdict": ACTION_NEED_BUSINESS_DECISION,
                    },
                )
            )
    return decisions


def build_special_unknown_decisions(rows: list[Phase9DRow]) -> list[dict[str, Any]]:
    buckets: dict[str, list[Phase9DRow]] = defaultdict(list)
    for r in rows:
        if r.service_type_confidence not in {CONFIDENCE_SPECIAL, CONFIDENCE_UNKNOWN}:
            continue
        # Skip pure confirmed/probable already handled
        buckets[_cohort_id(r.platform_key, r.section_key, r.subsection_key)].append(r)

    # Guidance table by section
    keep_other_sections = {
        "automatic_interactions": (
            "Customer buys scheduled/auto emoji reactions on future posts; "
            "mix of reaction kinds — no single Catalog metric type."
        ),
        "post_interactions": (
            "Customer buys post reactions (premium/normal); multi-reaction, not likes-only."
        ),
        "interaction": (
            "Instagram extra interaction bucket — mixed actions."
        ),
        "mentions": (
            "X mention service — status URL special rule; not a Catalog metric type."
        ),
        "direct_messages": (
            "X DM outreach with multi-line target format — product ≠ social metric."
        ),
        "start_bot": (
            "Telegram bot-start traffic — not followers/members/views."
        ),
        "direct": (
            "Catch-all 'مباشر' unsorted services — semantics not uniform."
        ),
        "iptv_wc2026": (
            "IPTV subscription/access product — not a social metric type."
        ),
        "iptv_panel": (
            "IPTV panel access product — not a social metric type."
        ),
        "member_bundles": (
            "Bundle of members + views — cannot map to one service_type."
        ),
        "monetization_hours": (
            "YouTube watch-hours for monetization — no Catalog type."
        ),
        "other": (
            "Explicit Legacy 'other' section — remain other."
        ),
    }

    decisions: list[dict[str, Any]] = []
    for cid, members in sorted(buckets.items(), key=lambda x: (-len(x[1]), x[0])):
        sk = members[0].section_key or ""
        ssk = members[0].subsection_key or ""
        key = ssk if sk == "channel_members" and ssk == "member_bundles" else sk
        purchase = keep_other_sections.get(
            key,
            keep_other_sections.get(sk, "Ambiguous commercial product"),
        )
        future_tax = None
        if sk in {"automatic_interactions", "post_interactions", "interaction"}:
            future_tax = "Optional future types: reactions / interactions — NOT this phase"
        elif sk in {"mentions", "direct_messages", "start_bot"}:
            future_tax = f"Optional future type for {sk} — NOT this phase"
        elif sk in {"iptv_wc2026", "iptv_panel"}:
            future_tax = "Optional future type: subscription/access — NOT this phase"
        elif sk == "monetization_hours":
            future_tax = "Optional future type: watch_hours — NOT this phase"

        decisions.append(
            _decision(
                cohort_id=cid,
                category="SPECIAL_OR_UNKNOWN_TAXONOMY",
                rows=members,
                proposed_decision=(
                    f"`other` is correct for now. Customer purchase: {purchase}"
                ),
                recommended_action=ACTION_KEEP_OTHER,
                confidence=members[0].service_type_confidence,
                evidence_for=[
                    purchase,
                    members[0].service_type_evidence,
                    "Phase 9D refused speculative type invention",
                ],
                evidence_against=[
                    "Forcing an existing metric type would be speculative",
                ],
                risks=["Publishing with wrong commercial label"],
                publication_impact=(
                    "May publish later as service_type=other IF all other "
                    "gates pass; taxonomy polish is optional"
                ),
                extra={
                    "existing_type_fits": False,
                    "other_is_correct": True,
                    "stage_a_verdict": ACTION_KEEP_OTHER,
                    "future_taxonomy_decision": future_tax,
                },
            )
        )
    return decisions


def build_sentinel_decisions(rows: list[Phase9DRow]) -> list[dict[str, Any]]:
    actual = [r for r in rows if r.max_quantity == SENTINEL_MAX]
    bridge_only = [
        r
        for r in rows
        if r.max_quantity != SENTINEL_MAX and "max_qty_sentinel" in r.bridge_review_codes
    ]

    decisions: list[dict[str, Any]] = []

    # Group actual sentinels by cohort
    buckets: dict[str, list[Phase9DRow]] = defaultdict(list)
    for r in actual:
        buckets[_cohort_id(r.platform_key, r.section_key, r.subsection_key)].append(r)
    for cid, members in sorted(buckets.items(), key=lambda x: (-len(x[1]), x[0])):
        decisions.append(
            _decision(
                cohort_id=cid,
                category="SENTINEL_QUANTITY",
                rows=members,
                proposed_decision=(
                    "max_quantity=2147483647 is Legacy sentinel — no authoritative "
                    "finite commercial max found in Catalog/Legacy metadata."
                ),
                recommended_action=ACTION_KEEP_SENTINEL_BLOCK,
                confidence=CONFIDENCE_UNKNOWN,
                evidence_for=[
                    "Exact INT32 max sentinel known from Phase 8/9C",
                    "No provider mapping to derive a commercial max",
                    "No stored finite max override on Catalog services",
                ],
                evidence_against=[
                    "Inventing a max would fabricate commercial policy",
                ],
                risks=["Publishing with unbounded max is commercially unsafe"],
                publication_impact="BLOCK PUBLICATION until finite max decided",
                extra={
                    "sentinel_kind": "actual_max_equals_2147483647",
                    "stage_a_verdict": ACTION_KEEP_SENTINEL_BLOCK,
                    "finite_max_available": False,
                },
            )
        )

    if bridge_only:
        buckets2: dict[str, list[Phase9DRow]] = defaultdict(list)
        for r in bridge_only:
            buckets2[_cohort_id(r.platform_key, r.section_key, r.subsection_key)].append(
                r
            )
        for cid, members in sorted(buckets2.items(), key=lambda x: (-len(x[1]), x[0])):
            decisions.append(
                _decision(
                    cohort_id=cid,
                    category="SENTINEL_BRIDGE_CODE",
                    rows=members,
                    proposed_decision=(
                        "Bridge carries max_qty_sentinel review code but current "
                        f"max_quantity values are finite "
                        f"({sorted({r.max_quantity for r in members})}). "
                        "Treat as business review of whether Legacy max was truncated."
                    ),
                    recommended_action=ACTION_NEED_BUSINESS_DECISION,
                    confidence=CONFIDENCE_PROBABLE,
                    evidence_for=[
                        "Migration bridge review_codes include max_qty_sentinel",
                        "Current Catalog max is not INT32 sentinel",
                    ],
                    evidence_against=[
                        "Cannot prove current finite max is the true commercial max",
                    ],
                    risks=["Silent truncation during migration"],
                    publication_impact=(
                        "Prefer review before publish; not auto-cleared"
                    ),
                    extra={
                        "sentinel_kind": "bridge_code_without_current_sentinel",
                        "stage_a_verdict": ACTION_NEED_BUSINESS_DECISION,
                        "current_max_values": sorted({r.max_quantity for r in members}),
                    },
                )
            )
    return decisions


def build_iptv_decisions(rows: list[Phase9DRow]) -> list[dict[str, Any]]:
    iptv = [
        r
        for r in rows
        if r.platform_key == "subscriptions"
        and r.section_key in {"iptv_panel", "iptv_wc2026"}
    ]
    buckets: dict[str, list[Phase9DRow]] = defaultdict(list)
    for r in iptv:
        buckets[_cohort_id(r.platform_key, r.section_key, r.subsection_key)].append(r)

    decisions: list[dict[str, Any]] = []
    for cid, members in sorted(buckets.items()):
        decisions.append(
            _decision(
                cohort_id=cid,
                category="PER_UNIT_IPTV",
                rows=members,
                proposed_decision=(
                    "Customer purchases one IPTV access/panel unit. "
                    "pricing_mode=per_unit with min=max=1 is consistent with "
                    "Legacy category=per_unit and auto_quantity=1. "
                    "quantity_based remains defensible (qty always 1). "
                    "fixed_package is NOT proven required by Catalog contract. "
                    "fulfillment=admin and free-text target are correct."
                ),
                recommended_action=ACTION_NEED_BUSINESS_DECISION,
                confidence=CONFIDENCE_SPECIAL,
                evidence_for=[
                    "Legacy category=per_unit → Catalog pricing_mode=per_unit",
                    "min_quantity=max_quantity=1 on all 7 services",
                    "Legacy fulfillment_mode=admin on bridge",
                    "Shared validator: subscriptions free-text target accepted",
                    "Catalog pricing already supports per_unit millimes math",
                ],
                evidence_against=[
                    "Package semantics could be argued, but min=max=1 alone "
                    "is forbidden as sole grounds for package_based (Phase 9D)",
                    "No business confirmation that fixed_package label is required",
                ],
                risks=[
                    "Publishing without admin fulfillment path in Telegram cutover",
                    "UX for free-text target must remain in Adapter contract",
                ],
                publication_impact=(
                    "Eligible for a dedicated IPTV pilot ONLY after explicit "
                    "approval of ordering+taxonomy; not in metric second pilot"
                ),
                extra={
                    "customer_purchase_unit": "1 IPTV access/panel unit",
                    "quantity_semantics": "always 1",
                    "pricing_semantics": "per_unit amount_millimes * quantity",
                    "per_unit_correct": True,
                    "fixed_package_required": False,
                    "quantity_based_defensible": True,
                    "stage_a_verdict": ACTION_NEED_BUSINESS_DECISION,
                },
            )
        )
    return decisions


def build_target_policy_decisions(rows: list[Phase9DRow]) -> list[dict[str, Any]]:
    special = [r for r in rows if r.target_special]
    buckets: dict[str, list[Phase9DRow]] = defaultdict(list)
    for r in special:
        buckets[_cohort_id(r.platform_key, r.section_key, r.subsection_key)].append(r)

    # Cohorts whose special rules are already fully represented in shared validator
    validator_covered = {
        "telegram::automatic_interactions",
        "telegram::post_share",
        "telegram::start_bot",
        "telegram::channel_members",
        "telegram::members",
        "telegram::post_views::future_posts",
        "telegram::post_views::past_posts",
        "x::spaces",
        "x::live_broadcast",
        "x::mentions",
        "x::direct_messages",
        "subscriptions::iptv_wc2026",
        "subscriptions::iptv_panel",
    }

    decisions: list[dict[str, Any]] = []
    for cid, members in sorted(buckets.items(), key=lambda x: (-len(x[1]), x[0])):
        sample = members[0]
        prefix = f"{sample.platform_key}::{sample.section_key}"
        covered = any(
            cid.startswith(v) or prefix == v or cid == v + "::"
            or cid.startswith(v + "::")
            for v in validator_covered
        )
        # More precise: match section-level
        covered = (
            f"{sample.platform_key}::{sample.section_key}" in {
                "telegram::automatic_interactions",
                "telegram::post_share",
                "telegram::start_bot",
                "telegram::channel_members",
                "telegram::members",
                "x::spaces",
                "x::live_broadcast",
                "x::mentions",
                "x::direct_messages",
                "subscriptions::iptv_wc2026",
                "subscriptions::iptv_panel",
            }
            or (
                sample.platform_key == "telegram"
                and sample.section_key == "post_views"
                and sample.subsection_key in {"future_posts", "past_posts"}
            )
        )
        action = (
            ACTION_SAFE_FOR_FUTURE_PUBLISH
            if covered and all(r.has_execution_source for r in members)
            and all(r.max_quantity != SENTINEL_MAX for r in members)
            and all(
                r.service_type_confidence == CONFIDENCE_CONFIRMED
                or r.service_type == "other"
                for r in members
            )
            else ACTION_TARGET_REVIEW
        )
        # Special targets can be publishable later even with type=other if gates pass;
        # Stage A marks review when type not CONFIRMED or sentinel/exec issues.
        if not covered:
            action = ACTION_TARGET_REVIEW
        elif any(r.max_quantity == SENTINEL_MAX for r in members):
            action = ACTION_KEEP_SENTINEL_BLOCK
        elif not all(r.has_execution_source for r in members):
            action = ACTION_BLOCKED
        elif sample.service_type_confidence not in {
            CONFIDENCE_CONFIRMED,
            CONFIDENCE_SPECIAL,
        } and sample.service_type_confidence == CONFIDENCE_PROBABLE:
            action = ACTION_NEED_BUSINESS_DECISION
        elif covered:
            # Validator covers target; publication still needs Stage B approval
            action = ACTION_SAFE_FOR_FUTURE_PUBLISH

        decisions.append(
            _decision(
                cohort_id=cid,
                category="SPECIAL_TARGET_POLICY",
                rows=members,
                proposed_decision=(
                    "Preserve Phase 9C structural keys. Shared target_validation "
                    "already encodes special prompts/URL rules for this cohort. "
                    "Do not invent new rules."
                ),
                recommended_action=action,
                confidence=(
                    CONFIDENCE_CONFIRMED if covered else CONFIDENCE_UNKNOWN
                ),
                evidence_for=[
                    "catalog_core/target_validation.py section/subsection branches",
                    "Phase 9C authored target_* keys from structure",
                    f"validator_covered={covered}",
                ],
                evidence_against=[]
                if covered
                else ["No explicit shared-validator branch identified"],
                risks=[
                    "Publishing without Adapter using shared validator",
                    "Telegram still on Legacy — Catalog publish does not cut over",
                ],
                publication_impact=(
                    "Target policy OK for future Catalog publish IF other gates pass; "
                    "not Stage B metric-pilot default"
                ),
                extra={
                    "validator_covered": covered,
                    "stage_a_verdict": action,
                },
            )
        )
    return decisions


def verify_target_required(rows: list[Phase9DRow]) -> dict[str, Any]:
    """Verify Phase 9D claim that all cohorts are TARGET_REQUIRED."""
    by_verdict: Counter[str] = Counter()
    discrepancies: list[dict[str, Any]] = []
    cohort_map: dict[str, dict[str, Any]] = {}
    for r in rows:
        req, kind, allow_u, allow_f = classify_target_required(
            platform_key=r.platform_key,
            section_key=r.section_key,
            subsection_key=r.subsection_key,
        )
        by_verdict[req] += 1
        cid = _cohort_id(r.platform_key, r.section_key, r.subsection_key)
        cohort_map[cid] = {
            "cohort_id": cid,
            "target_required": req,
            "target_kind": kind,
            "allow_username": allow_u,
            "allow_free_text": allow_f,
            "legacy_behavior": (
                "Order flow always collects a link/target before quantity "
                "(subscriptions: free-text placeholder; others: URL/username)."
            ),
        }
        if req != "TARGET_REQUIRED":
            discrepancies.append(cohort_map[cid])

    return {
        "claim": "Phase 9D: all cohorts TARGET_REQUIRED",
        "verified_counts": dict(by_verdict),
        "discrepancies": discrepancies,
        "cohorts": list(cohort_map.values()),
        "verdict": (
            "CLAIM_HOLDS"
            if not discrepancies and by_verdict.get("TARGET_REQUIRED", 0) == len(rows)
            else "DISCREPANCY_FOUND"
        ),
    }


def analyze_service_4371(rows: list[Phase9DRow], conn: sqlite3.Connection) -> dict[str, Any]:
    match = [r for r in rows if r.legacy_catalog_id == LEGACY_COMMENT_SERVICE_ID]
    if not match:
        # Include if somehow filtered — query directly
        return {"found": False}
    r = match[0]
    return {
        "found": True,
        "soldium_service_id": r.soldium_service_id,
        "legacy_catalog_id": LEGACY_COMMENT_SERVICE_ID,
        "current_service_type": r.service_type,
        "current_target_link_type": r.target_link_type,
        "legacy_hardcoded_behavior": (
            "Bot/shared validator treats service_id=='4371' OR link_type=='comment' "
            "as requiring /comment or comment_id in TikTok URL"
        ),
        "why_catalog_field_sufficient": (
            "Published snapshots include target_link_type; Adapter validates via "
            "service dict link_type. Explicit Catalog field removes dependency on "
            "hardcoded id for published Catalog path. Legacy fallback must remain "
            "for Telegram Legacy storefront until cutover."
        ),
        "do_not_remove_legacy_fallback": True,
        "do_not_modify_telegram": True,
        "recommended_action": "NO CHANGE — already authored in Phase 9D",
    }


def analyze_missing_execution(
    rows: list[Phase9DRow], conn: sqlite3.Connection
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for legacy_id in sorted(MISSING_EXEC_LEGACY_IDS):
        rmatch = [r for r in rows if r.legacy_catalog_id == legacy_id]
        bridge = conn.execute(
            """
            SELECT legacy_catalog_id, legacy_service_id, soldium_service_id,
                   provider_slug, provider_api_account, external_service_id,
                   classification, review_codes, legacy_fulfillment_mode
            FROM soldium_catalog_legacy_bridge
            WHERE legacy_catalog_id = ?
            """,
            (legacy_id,),
        ).fetchone()
        smm = conn.execute(
            """
            SELECT catalog_id, provider_slug, provider_api_account,
                   external_service_id, service_id, is_active
            FROM smm_services WHERE catalog_id = ?
            """,
            (legacy_id,),
        ).fetchone()
        row = rmatch[0] if rmatch else None
        has_authoritative = False
        provider = None
        account = None
        external = None
        if bridge:
            provider = bridge["provider_slug"]
            account = bridge["provider_api_account"]
            external = bridge["external_service_id"]
        if smm:
            provider = provider or smm["provider_slug"]
            account = account or smm["provider_api_account"]
            external = external or (
                str(smm["external_service_id"])
                if smm["external_service_id"] not in (None, "")
                else None
            )
        # Authoritative only if all three present and non-empty, and not requiring invent
        has_authoritative = bool(
            provider
            and str(provider).strip()
            and account
            and str(account).strip()
            and external is not None
            and str(external).strip() != ""
        )
        out.append(
            {
                "legacy_catalog_id": legacy_id,
                "soldium_service_id": row.soldium_service_id if row else None,
                "platform": row.platform_key if row else None,
                "section": row.section_key if row else None,
                "name_ar": row.name_ar if row else None,
                "bridge": dict(bridge) if bridge else None,
                "smm_services": dict(smm) if smm else None,
                "provider": provider,
                "account": account,
                "external_id": external,
                "identity_authoritative": has_authoritative,
                "has_catalog_execution_source": bool(
                    row.has_execution_source if row else False
                ),
                "recommended_action": (
                    ACTION_BLOCKED
                    if not has_authoritative
                    else "MANUAL IMPORT CANDIDATE — Stage B only after approval"
                ),
                "stage_a_verdict": ACTION_BLOCKED
                if not (row and row.has_execution_source)
                else "HAS_SOURCE",
                "do_not_create_execution_source": True,
                "do_not_auto_map": True,
            }
        )
    return out


def _is_published(conn: sqlite3.Connection, service_id: str) -> bool:
    pub = CatalogPublicationService(conn)
    latest = pub.get_latest_event(service_id)
    if latest is None:
        return False
    return str(getattr(latest, "event_type", "") or "") == "published"


def score_pilot_candidate(r: Phase9DRow, *, platform_used: set[str], section_used: set[str]) -> tuple[int, list[str]]:
    """Higher score = better second-pilot candidate. Quality > quantity."""
    reasons: list[str] = []
    score = 0

    if r.service_type_confidence != CONFIDENCE_CONFIRMED:
        return -1000, ["service_type not CONFIRMED"]
    if r.service_type == "other":
        return -1000, ["service_type still other"]
    if r.target_special:
        return -900, ["special target cohort — exclude from metric pilot"]
    if r.max_quantity == SENTINEL_MAX or "max_qty_sentinel" in r.bridge_review_codes:
        return -800, ["sentinel quantity review"]
    if not r.has_execution_source:
        return -1000, ["missing execution source"]
    if r.ordering_confidence == CONFIDENCE_UNKNOWN:
        return -700, ["ordering_mode unknown"]
    if r.pricing_mode == "per_unit":
        return -600, ["per_unit/IPTV — separate decision track"]
    if r.legacy_fulfillment_mode == "admin":
        return -500, ["admin fulfillment — not first metric pilot"]
    if r.amount_millimes is None or int(r.amount_millimes) <= 0:
        return -1000, ["invalid price"]
    if r.soldium_service_id in PILOT_SET:
        return -1000, ["already a phase-9B pilot"]

    score += 50  # base for CONFIRMED+clean
    reasons.append("CONFIRMED service_type")
    score += 20
    reasons.append("non-special target")
    score += 15
    reasons.append("finite quantity")
    score += 15
    reasons.append("execution present")

    # Prefer simple host-match targets (no username/free-text)
    if not r.allow_free_text:
        score += 10
        reasons.append("no free-text target")
    if not r.allow_username:
        score += 5
        reasons.append("URL-only target")

    # Diversity bonuses
    if r.platform_key and r.platform_key not in platform_used:
        score += 25
        reasons.append("new platform diversity")
    if r.section_key and r.section_key not in section_used:
        score += 10
        reasons.append("new section diversity")

    # Representative commercial types
    type_bonus = {
        "followers": 8,
        "likes": 8,
        "views": 8,
        "shares": 6,
        "members": 6,
        "live_viewers": 6,
        "comments": 5,
        "saves": 5,
    }
    score += type_bonus.get(r.service_type, 0)

    return score, reasons


def select_second_pilot_candidates(
    conn: sqlite3.Connection, rows: list[Phase9DRow], *, max_n: int = 10
) -> dict[str, Any]:
    repo = CatalogRepository(conn)
    pub = CatalogPublicationService(conn)

    scored: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for r in rows:
        if r.soldium_service_id in PILOT_SET:
            rejected.append(
                {
                    "service_id": r.soldium_service_id,
                    "reason": "existing pilot",
                    "score": -1000,
                }
            )
            continue

        latest = pub.get_latest_event(r.soldium_service_id)
        published = (
            latest is not None
            and str(getattr(latest, "event_type", "") or "") == "published"
        )
        if published:
            rejected.append(
                {
                    "service_id": r.soldium_service_id,
                    "reason": "already published",
                    "score": -1000,
                }
            )
            continue

        score, reasons = score_pilot_candidate(
            r, platform_used=set(), section_used=set()
        )
        if score < 0:
            rejected.append(
                {
                    "service_id": r.soldium_service_id,
                    "legacy_catalog_id": r.legacy_catalog_id,
                    "platform": r.platform_key,
                    "section": r.section_key,
                    "service_type": r.service_type,
                    "reason": "; ".join(reasons),
                    "score": score,
                }
            )
            continue

        svc = repo.get_service(r.soldium_service_id)
        ready = False
        issues: list[str] = []
        eligible = False
        if svc is not None:
            source = repo.get_active_execution_source(r.soldium_service_id)
            price = repo.get_active_price(r.soldium_service_id)
            entry = repo.get_entry_for_service(r.soldium_service_id)
            if entry:
                svc.entry_id = entry.id
                svc.parent_entry_id = entry.parent_entry_id
            result = evaluate_service_readiness(
                repo, svc, source=source, price=price
            )
            ready = bool(result.ready)
            issues = [i.code for i in (result.issues or [])]
            state = pub.get_publication_status(r.soldium_service_id)
            eligible = bool((state or {}).get("customer_catalog_eligible"))

        if not ready:
            rejected.append(
                {
                    "service_id": r.soldium_service_id,
                    "legacy_catalog_id": r.legacy_catalog_id,
                    "platform": r.platform_key,
                    "section": r.section_key,
                    "service_type": r.service_type,
                    "reason": f"readiness not ready: {issues}",
                    "score": score - 200,
                }
            )
            continue

        scored.append(
            {
                "service_id": r.soldium_service_id,
                "legacy_catalog_id": r.legacy_catalog_id,
                "name_ar": r.name_ar,
                "platform": r.platform_key,
                "section": r.section_key,
                "subsection": r.subsection_key,
                "service_type": r.service_type,
                "ordering_mode": r.ordering_mode,
                "fulfillment_mode": r.fulfillment_mode,
                "pricing_mode": r.pricing_mode,
                "min_quantity": r.min_quantity,
                "max_quantity": r.max_quantity,
                "amount_millimes": r.amount_millimes,
                "target_keys": {
                    "platform": r.target_platform_key,
                    "section": r.target_section_key,
                    "subsection": r.target_subsection_key,
                    "link_type": r.target_link_type,
                },
                "readiness_ready": ready,
                "customer_catalog_eligible": eligible,
                "latest_publication": None,
                "projection_excluded": True,
                "base_score": score,
                "score_reasons": reasons,
            }
        )

    # Greedy diversity selection
    scored.sort(
        key=lambda x: (
            -x["base_score"],
            x["platform"] or "",
            x["section"] or "",
            x["service_id"],
        )
    )
    selected: list[dict[str, Any]] = []
    platform_used: set[str] = set()
    section_used: set[str] = set()
    type_used: set[str] = set()

    # Re-score with diversity as we pick
    remaining = list(scored)
    while remaining and len(selected) < max_n:
        best = None
        best_score = -10**9
        best_reasons: list[str] = []
        for cand in remaining:
            # Rebuild row-like score adjustments
            adj = cand["base_score"]
            reasons = list(cand["score_reasons"])
            if cand["platform"] not in platform_used:
                adj += 25
                reasons = reasons + ["pick:new platform"]
            if cand["section"] not in section_used:
                adj += 10
                reasons = reasons + ["pick:new section"]
            if cand["service_type"] not in type_used:
                adj += 12
                reasons = reasons + ["pick:new service_type"]
            # Prefer not stacking same platform beyond 2
            same_plat = sum(
                1 for s in selected if s["platform"] == cand["platform"]
            )
            if same_plat >= 2:
                adj -= 20
                reasons = reasons + ["penalty:platform already represented twice"]
            if adj > best_score:
                best_score = adj
                best = cand
                best_reasons = reasons
        if best is None or best_score < 40:
            break
        entry = dict(best)
        entry["final_score"] = best_score
        entry["score_reasons"] = best_reasons
        selected.append(entry)
        platform_used.add(best["platform"] or "")
        section_used.add(best["section"] or "")
        type_used.add(best["service_type"] or "")
        remaining = [c for c in remaining if c["service_id"] != best["service_id"]]

    # Recommend size 5–10 based on quality
    if len(selected) >= 8:
        recommend_n = min(8, len(selected))
    elif len(selected) >= 5:
        recommend_n = len(selected)
    else:
        recommend_n = len(selected)

    recommended = selected[:recommend_n]
    overflow = selected[recommend_n:]
    for o in overflow:
        rejected.append(
            {
                "service_id": o["service_id"],
                "reason": "above recommended pilot size / lower diversity rank",
                "score": o.get("final_score", o["base_score"]),
            }
        )

    return {
        "eligible_pool_size": len(scored),
        "recommended_pilot_size": recommend_n,
        "recommended_pilot_set": recommended,
        "ranked_candidates_all": selected,
        "rejected_candidates": rejected[:200],
        "rejected_truncated": max(0, len(rejected) - 200),
        "selection_policy": (
            "CONFIRMED type, non-special target, finite qty, exec+price, "
            "readiness ready, not published, not IPTV/admin; diversity-aware greedy"
        ),
        "do_not_publish": True,
    }


def build_decision_pack(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = load_phase9d_rows(conn)
    assert len(rows) == 248, f"expected 248 remaining, got {len(rows)}"
    assert not any(r.soldium_service_id in PILOT_SET for r in rows)

    probable = build_probable_decisions(rows)
    special = build_special_unknown_decisions(rows)
    sentinel = build_sentinel_decisions(rows)
    iptv = build_iptv_decisions(rows)
    targets = build_target_policy_decisions(rows)
    target_req = verify_target_required(rows)
    s4371 = analyze_service_4371(rows, conn)
    missing = analyze_missing_execution(rows, conn)
    pilots = select_second_pilot_candidates(conn, rows, max_n=10)

    # Coverage: every service appears in at least one decision or is CONFIRMED-clean
    covered_ids: set[str] = set()
    for block in (probable, special, sentinel, iptv, targets):
        for d in block:
            covered_ids.update(d["affected_service_ids"])
    for m in missing:
        if m.get("soldium_service_id"):
            covered_ids.add(m["soldium_service_id"])
    confirmed_clean = [
        r.soldium_service_id
        for r in rows
        if r.service_type_confidence == CONFIDENCE_CONFIRMED
        and not r.target_special
        and r.max_quantity != SENTINEL_MAX
        and "max_qty_sentinel" not in r.bridge_review_codes
        and r.has_execution_source
        and r.ordering_confidence != CONFIDENCE_UNKNOWN
    ]
    covered_ids.update(confirmed_clean)
    uncovered = [
        r.soldium_service_id for r in rows if r.soldium_service_id not in covered_ids
    ]

    counts = production_counts(conn)
    return {
        "phase": "9E-STAGE-A",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "remaining_count": len(rows),
        "pilot_ids_excluded": list(PILOT_SERVICE_IDS),
        "production_counts": counts,
        "probable_service_type_decisions": probable,
        "special_unknown_taxonomy_decisions": special,
        "sentinel_quantity_decisions": sentinel,
        "iptv_per_unit_decisions": iptv,
        "special_target_policy_decisions": targets,
        "target_required_verification": target_req,
        "service_4371": s4371,
        "missing_execution_analysis": missing,
        "second_pilot_preparation": pilots,
        "coverage": {
            "services_accounted": len(covered_ids),
            "uncovered_service_ids": uncovered,
            "confirmed_clean_count": len(confirmed_clean),
        },
        "stage_b_scope_recommendation": {
            "apply_only_after_explicit_approval": True,
            "suggested_approvals_needed": [
                "PROBABLE type confirmations or KEEP OTHER",
                "Sentinel finite-max business values OR keep blocked",
                "IPTV ordering/taxonomy approval if including IPTV pilot",
                "Second pilot set selection from recommended list",
            ],
            "suggested_stage_b_steps": [
                "apply explicitly approved decisions",
                "verify readiness",
                "select second pilot",
                "controlled publication",
                "shadow",
                "parity",
                "hard stop",
            ],
            "do_not_execute_stage_b_now": True,
        },
        "blockers": [
            "24 PROBABLE service-type services need business decision",
            "Sentinel maxima unresolved for publication",
            "5 missing execution identities BLOCKED",
            "IPTV/per_unit needs explicit ordering approval before any IPTV publish",
            "SPECIAL taxonomy remains other (acceptable) but interactions not in metric pilot",
        ],
        "mutations": "NONE — Stage A is read-only",
    }


def summarize_decision_pack(pack: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": pack["timestamp"],
        "remaining_count": pack["remaining_count"],
        "production_counts": pack["production_counts"],
        "probable_cohorts": len(pack["probable_service_type_decisions"]),
        "probable_services": sum(
            d["service_count"] for d in pack["probable_service_type_decisions"]
        ),
        "special_unknown_cohorts": len(pack["special_unknown_taxonomy_decisions"]),
        "special_unknown_services": sum(
            d["service_count"] for d in pack["special_unknown_taxonomy_decisions"]
        ),
        "sentinel_decision_rows": len(pack["sentinel_quantity_decisions"]),
        "iptv_cohorts": len(pack["iptv_per_unit_decisions"]),
        "target_special_cohorts": len(pack["special_target_policy_decisions"]),
        "target_required_verdict": pack["target_required_verification"]["verdict"],
        "service_4371": {
            "service_type": pack["service_4371"].get("current_service_type"),
            "link_type": pack["service_4371"].get("current_target_link_type"),
        },
        "missing_execution": [
            {
                "legacy_catalog_id": m["legacy_catalog_id"],
                "verdict": m["stage_a_verdict"],
                "authoritative": m["identity_authoritative"],
            }
            for m in pack["missing_execution_analysis"]
        ],
        "second_pilot": {
            "eligible_pool_size": pack["second_pilot_preparation"]["eligible_pool_size"],
            "recommended_size": pack["second_pilot_preparation"][
                "recommended_pilot_size"
            ],
            "recommended_ids": [
                c["service_id"]
                for c in pack["second_pilot_preparation"]["recommended_pilot_set"]
            ],
            "recommended_brief": [
                {
                    "service_id": c["service_id"],
                    "legacy": c["legacy_catalog_id"],
                    "platform": c["platform"],
                    "section": c["section"],
                    "type": c["service_type"],
                    "score": c.get("final_score"),
                }
                for c in pack["second_pilot_preparation"]["recommended_pilot_set"]
            ],
        },
        "coverage": pack["coverage"],
        "blockers": pack["blockers"],
        "mutations": pack["mutations"],
    }
