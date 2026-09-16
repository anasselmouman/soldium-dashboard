# -*- coding: utf-8 -*-
"""Phase 9D — Service semantics & special target policy review.

Audit-first classification. Author ONLY CONFIRMED service_type (and optional
existing target_link_type for service 4371). Never keyword-from-name.
Never publish. Never touch pilots, prices, exec, placement, quantities.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from catalog_core.commercial import normalize_ordering_mode, normalize_service_type
from catalog_core.order_contract_republish import PILOT_SERVICE_IDS
from catalog_core.phase9c_audit import (
    REVIEW_EXECUTION_SOURCE_MISSING,
    REVIEW_FULFILLMENT_ADMIN,
    REVIEW_QUANTITY_SENTINEL,
    REVIEW_TARGET_SPECIAL_RULE,
    SENTINEL_MAX,
    _norm_key,
    _parse_json_list,
    _subscriptions_special,
    _telegram_special,
    _x_special,
)
from catalog_core.pilot_parity import resolve_structural_placement_keys
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.service import CatalogCoreService
from catalog_core.target_validation import resolve_link_prompt

logger = logging.getLogger("soldium.catalog.phase9d")

PILOT_SET = frozenset(PILOT_SERVICE_IDS)
LEGACY_COMMENT_SERVICE_ID = "4371"

CONFIDENCE_CONFIRMED = "CONFIRMED"
CONFIDENCE_PROBABLE = "PROBABLE"
CONFIDENCE_UNKNOWN = "UNKNOWN"
CONFIDENCE_SPECIAL = "SPECIAL"
CONFIDENCE_BLOCKED = "BLOCKED"

# Deterministic section_key → service_type. Evidence = Legacy structure keys
# (migration tree / storefront navigation), NOT Arabic names.
# Subsections that break the mapping are handled separately.
_SECTION_TYPE_CONFIRMED: dict[str, str] = {
    "likes": "likes",
    "views": "views",
    "video_views": "views",
    "video_reels_views": "views",
    "post_views": "views",
    "followers": "followers",
    "subscribers": "followers",  # YouTube subscribers → Catalog followers code
    "channel_members": "members",
    "members": "members",
    "post_share": "shares",
    "geo_shares": "shares",
    "live_stream_views": "live_viewers",
    "live_broadcast": "live_viewers",
    "live_stream": "live_viewers",
}

# Subsections under an otherwise CONFIRMED section that must stay UNKNOWN.
_SECTION_SUBSECTION_UNKNOWN: frozenset[tuple[str, str]] = frozenset(
    {
        # Bundles sell members + views — not a single Catalog type.
        ("channel_members", "member_bundles"),
    }
)

# Explicit PROBABLE (review only — never auto-author).
_SECTION_TYPE_PROBABLE: dict[str, str] = {
    "followers_members": "followers",  # mixed followers/members wording
    "reactions": "likes",  # Facebook reactions include likes + others
    "engagement": "likes",  # YouTube engagement mix
    "spaces": "live_viewers",  # Space listeners ≈ live audience (not proven)
}

REVIEW_SERVICE_TYPE_UNKNOWN = "SERVICE_TYPE_UNKNOWN"
REVIEW_ORDERING_MODE_UNKNOWN = "ORDERING_MODE_UNKNOWN"
REVIEW_SENTINEL_BUSINESS = "SENTINEL_REQUIRES_BUSINESS_DECISION"
REVIEW_TARGET_REQUIRED = "TARGET_REQUIRED"
REVIEW_PRICING_CONTRACT = "PRICING_CONTRACT_REVIEW"
REVIEW_LINK_TYPE_COMMENT = "LINK_TYPE_COMMENT"


@dataclass
class Phase9DRow:
    soldium_service_id: str
    legacy_catalog_id: str | None
    name_ar: str
    platform_key: str | None
    section_key: str | None
    subsection_key: str | None
    service_type: str
    ordering_mode: str
    min_quantity: int
    max_quantity: int
    amount_millimes: int | None
    currency: str | None
    pricing_mode: str | None
    fulfillment_mode: str
    legacy_fulfillment_mode: str | None
    target_platform_key: str | None
    target_section_key: str | None
    target_subsection_key: str | None
    target_link_prompt_key: str | None
    target_link_type: str | None
    has_execution_source: bool
    legacy_category: str | None
    bridge_review_codes: list[str] = field(default_factory=list)

    # Classification outputs
    candidate_service_type: str = "other"
    service_type_confidence: str = CONFIDENCE_UNKNOWN
    service_type_evidence: str = ""
    ordering_decision: str = "quantity_based"
    ordering_confidence: str = CONFIDENCE_UNKNOWN
    ordering_evidence: str = ""
    quantity_decision: str = "UNKNOWN"
    target_required: str = "TARGET_UNKNOWN"
    target_kind: str = "unknown"
    target_special: bool = False
    allow_username: bool = False
    allow_free_text: bool = False
    review_codes: list[str] = field(default_factory=list)
    readiness_ready: bool = False
    readiness_reasons: list[str] = field(default_factory=list)
    safe_author_service_type: bool = False
    safe_author_link_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_service_type(
    *,
    platform_key: str | None,
    section_key: str | None,
    subsection_key: str | None,
) -> tuple[str, str, str]:
    """Return (candidate_type, confidence, evidence). Never uses service name."""
    pk = _norm_key(platform_key)
    sk = _norm_key(section_key)
    ssk = _norm_key(subsection_key)

    if not sk:
        return (
            "other",
            CONFIDENCE_UNKNOWN,
            "missing section_key — cannot classify",
        )

    if sk in {"iptv_wc2026", "iptv_panel"} or pk == "subscriptions":
        return (
            "other",
            CONFIDENCE_SPECIAL,
            "subscriptions/IPTV product — no Catalog metric type fits",
        )

    if (sk, ssk or "") in {
        (a, b) for a, b in _SECTION_SUBSECTION_UNKNOWN
    } or (sk, ssk) in _SECTION_SUBSECTION_UNKNOWN:
        return (
            "other",
            CONFIDENCE_SPECIAL,
            f"subsection {ssk} under {sk} mixes products — keep other",
        )

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
        return (
            "other",
            CONFIDENCE_SPECIAL if sk
            in {
                "automatic_interactions",
                "post_interactions",
                "mentions",
                "direct_messages",
                "start_bot",
            }
            else CONFIDENCE_UNKNOWN,
            f"section_key={sk} has no single Catalog service_type",
        )

    if sk in _SECTION_TYPE_CONFIRMED:
        return (
            _SECTION_TYPE_CONFIRMED[sk],
            CONFIDENCE_CONFIRMED,
            f"Legacy section_key={sk} maps deterministically to Catalog type "
            f"(structural/migration key; not name inference)",
        )

    if sk in _SECTION_TYPE_PROBABLE:
        return (
            _SECTION_TYPE_PROBABLE[sk],
            CONFIDENCE_PROBABLE,
            f"section_key={sk} suggests {_SECTION_TYPE_PROBABLE[sk]} but "
            f"wording/semantics are mixed — review only",
        )

    return (
        "other",
        CONFIDENCE_UNKNOWN,
        f"no confirmed mapping for section_key={sk}",
    )


def classify_ordering(
    *,
    pricing_mode: str | None,
    min_quantity: int,
    max_quantity: int,
    platform_key: str | None,
    section_key: str | None,
    legacy_category: str | None,
) -> tuple[str, str, str]:
    """Return (ordering_mode, confidence, evidence)."""
    pk = _norm_key(platform_key)
    sk = _norm_key(section_key)
    pm = _norm_key(pricing_mode)
    cat = _norm_key(legacy_category)

    if pm == "per_unit" or cat == "per_unit" or (
        pk == "subscriptions" and sk in {"iptv_wc2026", "iptv_panel"}
    ):
        # min=max=1 alone is NOT sufficient to convert to package_based.
        return (
            "quantity_based",
            CONFIDENCE_UNKNOWN,
            "per_unit/subscription: quantity_based retained; package_based "
            "not proven — ORDERING_MODE_UNKNOWN / pricing-contract review",
        )

    if min_quantity == max_quantity == 1 and pm != "per_unit":
        return (
            "quantity_based",
            CONFIDENCE_PROBABLE,
            "min=max=1 observed but phase forbids converting on that alone",
        )

    return (
        "quantity_based",
        CONFIDENCE_CONFIRMED,
        "existing quantity-based ordering matches metric storefront behavior",
    )


def classify_quantity(max_quantity: int, bridge_codes: list[str]) -> str:
    if max_quantity == SENTINEL_MAX or "max_qty_sentinel" in bridge_codes:
        return "SENTINEL_REQUIRES_BUSINESS_DECISION"
    if max_quantity > 0:
        return "CONFIRMED_FINITE_MAX"
    return "UNKNOWN"


def classify_target_required(
    *,
    platform_key: str | None,
    section_key: str | None,
    subsection_key: str | None,
) -> tuple[str, str, bool, bool]:
    """Return (required_class, target_kind, allow_username, allow_free_text)."""
    pk = _norm_key(platform_key) or ""
    sk = _norm_key(section_key)
    ssk = _norm_key(subsection_key)
    prompt, allow_u, allow_f = resolve_link_prompt(pk, sk, ssk)

    if pk == "subscriptions" and sk in {"iptv_wc2026", "iptv_panel"}:
        return "TARGET_REQUIRED", "free_text", allow_u, allow_f

    if allow_f:
        return "TARGET_REQUIRED", "free_text", allow_u, allow_f
    if allow_u:
        return "TARGET_REQUIRED", "url_or_username", allow_u, allow_f
    # All current storefront order flows require a link/target input.
    return "TARGET_REQUIRED", "url", allow_u, allow_f


def is_special_target(
    platform_key: str | None, section_key: str | None, subsection_key: str | None
) -> bool:
    return bool(
        _telegram_special(section_key, subsection_key)
        or _x_special(section_key)
        or _subscriptions_special(platform_key, section_key)
    )


def classify_row(row: Phase9DRow) -> Phase9DRow:
    codes: list[str] = list(row.bridge_review_codes)

    cand, conf, evid = classify_service_type(
        platform_key=row.platform_key,
        section_key=row.section_key,
        subsection_key=row.subsection_key,
    )
    row.candidate_service_type = cand
    row.service_type_confidence = conf
    row.service_type_evidence = evid
    if conf != CONFIDENCE_CONFIRMED or cand == "other":
        codes.append(REVIEW_SERVICE_TYPE_UNKNOWN)
    # Authorable when CONFIRMED (idempotent skip if already matching).
    row.safe_author_service_type = (
        conf == CONFIDENCE_CONFIRMED
        and cand != "other"
        and row.soldium_service_id not in PILOT_SET
    )

    omode, oconf, oevid = classify_ordering(
        pricing_mode=row.pricing_mode,
        min_quantity=row.min_quantity,
        max_quantity=row.max_quantity,
        platform_key=row.platform_key,
        section_key=row.section_key,
        legacy_category=row.legacy_category,
    )
    row.ordering_decision = omode
    row.ordering_confidence = oconf
    row.ordering_evidence = oevid
    if oconf == CONFIDENCE_UNKNOWN:
        codes.append(REVIEW_ORDERING_MODE_UNKNOWN)
        if row.pricing_mode == "per_unit" or _norm_key(row.legacy_category) == "per_unit":
            codes.append(REVIEW_PRICING_CONTRACT)

    row.quantity_decision = classify_quantity(row.max_quantity, row.bridge_review_codes)
    if row.quantity_decision == "SENTINEL_REQUIRES_BUSINESS_DECISION":
        codes.append(REVIEW_QUANTITY_SENTINEL)
        codes.append(REVIEW_SENTINEL_BUSINESS)

    req, kind, allow_u, allow_f = classify_target_required(
        platform_key=row.platform_key,
        section_key=row.section_key,
        subsection_key=row.subsection_key,
    )
    row.target_required = req
    row.target_kind = kind
    row.allow_username = allow_u
    row.allow_free_text = allow_f
    codes.append(REVIEW_TARGET_REQUIRED)

    row.target_special = is_special_target(
        row.platform_key, row.section_key, row.subsection_key
    )
    if row.target_special:
        codes.append(REVIEW_TARGET_SPECIAL_RULE)

    if row.legacy_fulfillment_mode == "admin":
        codes.append(REVIEW_FULFILLMENT_ADMIN)

    if not row.has_execution_source:
        codes.append(REVIEW_EXECUTION_SOURCE_MISSING)

    # Service 4371: comment link_type from Legacy services.json metadata.
    if row.legacy_catalog_id == LEGACY_COMMENT_SERVICE_ID:
        codes.append(REVIEW_LINK_TYPE_COMMENT)
        if row.target_link_type != "comment" and row.soldium_service_id not in PILOT_SET:
            row.safe_author_link_type = "comment"

    row.review_codes = list(dict.fromkeys(codes))
    return row


def load_phase9d_rows(conn: sqlite3.Connection) -> list[Phase9DRow]:
    placeholders = ",".join("?" * len(PILOT_SERVICE_IDS))
    sql = f"""
    SELECT
      s.id AS soldium_service_id,
      s.name_ar,
      s.service_type,
      s.ordering_mode,
      s.min_quantity,
      s.max_quantity,
      s.fulfillment_mode,
      s.target_platform_key,
      s.target_section_key,
      s.target_subsection_key,
      s.target_link_prompt_key,
      s.target_link_type,
      b.legacy_catalog_id,
      b.legacy_fulfillment_mode,
      b.review_codes,
      l.platform_key,
      l.section_key,
      l.subsection_key,
      l.category AS legacy_category,
      p.amount_millimes,
      p.currency,
      p.pricing_mode,
      x.external_service_id AS exec_external_id
    FROM soldium_catalog_services s
    LEFT JOIN soldium_catalog_legacy_bridge b ON b.soldium_service_id = s.id
    LEFT JOIN smm_services l ON l.catalog_id = b.legacy_catalog_id
    LEFT JOIN soldium_catalog_prices p
      ON p.service_id = s.id AND p.status = 'active'
    LEFT JOIN soldium_catalog_execution_sources x
      ON x.service_id = s.id AND x.status = 'active'
    WHERE s.id NOT IN ({placeholders})
    ORDER BY l.platform_key, l.section_key, l.subsection_key, s.id
    """
    raw = conn.execute(sql, tuple(PILOT_SERVICE_IDS)).fetchall()
    repo = CatalogRepository(conn)
    out: list[Phase9DRow] = []
    for row in raw:
        sid = str(row["soldium_service_id"])
        structural = resolve_structural_placement_keys(conn, sid)
        pk = _norm_key(row["platform_key"])
        sk = _norm_key(row["section_key"])
        ssk = _norm_key(row["subsection_key"])
        if structural:
            pk = _norm_key(structural[0]) or pk
            sk = _norm_key(structural[1]) or sk
            ssk = _norm_key(structural[2]) if structural[2] else ssk

        has_exec = bool(str(row["exec_external_id"] or "").strip())
        audit = Phase9DRow(
            soldium_service_id=sid,
            legacy_catalog_id=_norm_key(row["legacy_catalog_id"]),
            name_ar=str(row["name_ar"] or ""),
            platform_key=pk,
            section_key=sk,
            subsection_key=ssk,
            service_type=str(row["service_type"] or "other"),
            ordering_mode=str(row["ordering_mode"] or "quantity_based"),
            min_quantity=int(row["min_quantity"] or 0),
            max_quantity=int(row["max_quantity"] or 0),
            amount_millimes=(
                int(row["amount_millimes"])
                if row["amount_millimes"] is not None
                else None
            ),
            currency=_norm_key(row["currency"]),
            pricing_mode=_norm_key(row["pricing_mode"]),
            fulfillment_mode=str(row["fulfillment_mode"] or "auto"),
            legacy_fulfillment_mode=_norm_key(row["legacy_fulfillment_mode"]),
            target_platform_key=_norm_key(row["target_platform_key"]),
            target_section_key=_norm_key(row["target_section_key"]),
            target_subsection_key=_norm_key(row["target_subsection_key"]),
            target_link_prompt_key=_norm_key(row["target_link_prompt_key"]),
            target_link_type=_norm_key(row["target_link_type"]),
            has_execution_source=has_exec,
            legacy_category=_norm_key(row["legacy_category"]),
            bridge_review_codes=_parse_json_list(row["review_codes"]),
        )
        classify_row(audit)

        svc = repo.get_service(sid)
        if svc is not None:
            source = repo.get_active_execution_source(sid)
            price = repo.get_active_price(sid)
            entry = repo.get_entry_for_service(sid)
            if entry:
                svc.entry_id = entry.id
                svc.parent_entry_id = entry.parent_entry_id
                svc.location_path = repo.breadcrumb_names(entry.parent_entry_id)
            result = evaluate_service_readiness(
                repo, svc, source=source, price=price
            )
            audit.readiness_ready = bool(result.ready)
            audit.readiness_reasons = [
                f"{i.code}:{i.title}" for i in (result.issues or [])
            ]
        out.append(audit)
    return out


def build_matrices(rows: list[Phase9DRow]) -> dict[str, Any]:
    type_matrix: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = f"{r.platform_key}|{r.section_key}|{r.subsection_key or ''}"
        if key not in type_matrix:
            type_matrix[key] = {
                "platform": r.platform_key,
                "section": r.section_key,
                "subsection": r.subsection_key,
                "service_count": 0,
                "candidate_service_type": r.candidate_service_type,
                "confidence": r.service_type_confidence,
                "evidence": r.service_type_evidence,
                "ordering_decision": r.ordering_decision,
                "ordering_confidence": r.ordering_confidence,
                "quantity_decision": r.quantity_decision,
                "target_required": r.target_required,
                "target_kind": r.target_kind,
                "target_special": r.target_special,
                "allow_username": r.allow_username,
                "allow_free_text": r.allow_free_text,
                "safe_to_author_service_type": r.safe_author_service_type,
                "review_codes": [],
                "service_ids": [],
            }
        m = type_matrix[key]
        m["service_count"] += 1
        m["service_ids"].append(r.soldium_service_id)
        for c in r.review_codes:
            if c not in m["review_codes"]:
                m["review_codes"].append(c)

    conf_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for r in rows:
        conf_counts[r.service_type_confidence] = (
            conf_counts.get(r.service_type_confidence, 0) + 1
        )
        type_counts[r.candidate_service_type] = (
            type_counts.get(r.candidate_service_type, 0) + 1
        )

    special = [
        m
        for m in type_matrix.values()
        if m["target_special"] or m["confidence"] in {CONFIDENCE_SPECIAL, CONFIDENCE_PROBABLE}
    ]
    confirmed = [m for m in type_matrix.values() if m["confidence"] == CONFIDENCE_CONFIRMED]
    blocked = [
        {
            "service_id": r.soldium_service_id,
            "legacy_catalog_id": r.legacy_catalog_id,
            "reason": "EXECUTION_SOURCE_MISSING",
            "platform": r.platform_key,
            "section": r.section_key,
        }
        for r in rows
        if not r.has_execution_source
    ]
    per_unit = [
        r.to_dict()
        for r in rows
        if r.pricing_mode == "per_unit"
        or _norm_key(r.legacy_category) == "per_unit"
        or (
            r.platform_key == "subscriptions"
            and r.section_key in {"iptv_wc2026", "iptv_panel"}
        )
    ]
    sentinel = [
        {
            "service_id": r.soldium_service_id,
            "legacy_catalog_id": r.legacy_catalog_id,
            "platform": r.platform_key,
            "section": r.section_key,
            "max_quantity": r.max_quantity,
            "decision": r.quantity_decision,
        }
        for r in rows
        if r.quantity_decision == "SENTINEL_REQUIRES_BUSINESS_DECISION"
    ]
    comment_4371 = [
        r.to_dict()
        for r in rows
        if r.legacy_catalog_id == LEGACY_COMMENT_SERVICE_ID
    ]

    return {
        "service_type_matrix": sorted(
            type_matrix.values(),
            key=lambda x: (-x["service_count"], x["platform"] or "", x["section"] or ""),
        ),
        "confidence_counts": conf_counts,
        "candidate_type_counts": type_counts,
        "confirmed_cohorts": confirmed,
        "special_or_probable_cohorts": special,
        "blocked_missing_execution": blocked,
        "per_unit_services": per_unit,
        "sentinel_services": sentinel,
        "service_4371": comment_4371,
        "safe_author_service_type_count": sum(
            1 for r in rows if r.safe_author_service_type
        ),
        "safe_author_link_type_count": sum(
            1 for r in rows if r.safe_author_link_type
        ),
    }


def apply_confirmed_authoring(
    conn: sqlite3.Connection,
    rows: list[Phase9DRow],
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Author CONFIRMED service_type + comment link_type for 4371 only."""
    core = CatalogCoreService(conn)
    updated_type = 0
    updated_link = 0
    skipped = 0
    details: list[dict[str, Any]] = []

    for r in rows:
        if r.soldium_service_id in PILOT_SET:
            skipped += 1
            continue

        changed = False
        before: dict[str, Any] = {
            "service_type": r.service_type,
            "ordering_mode": r.ordering_mode,
            "target_link_type": r.target_link_type,
        }
        after = dict(before)
        kwargs: dict[str, Any] = {}

        if r.safe_author_service_type:
            desired = normalize_service_type(r.candidate_service_type)
            if r.service_type != desired:
                kwargs["service_type"] = desired
                after["service_type"] = desired
                updated_type += 1
                changed = True

        # ordering_mode: only if CONFIRMED and different — currently always
        # quantity_based already, so no writes for UNKNOWN per_unit.
        if (
            r.ordering_confidence == CONFIDENCE_CONFIRMED
            and r.ordering_decision != r.ordering_mode
        ):
            desired_om = normalize_ordering_mode(r.ordering_decision)
            kwargs["ordering_mode"] = desired_om
            after["ordering_mode"] = desired_om
            changed = True

        if r.safe_author_link_type:
            kwargs["target_link_type"] = r.safe_author_link_type
            after["target_link_type"] = r.safe_author_link_type
            updated_link += 1
            changed = True

        if not changed:
            skipped += 1
            continue

        if not dry_run:
            core.update_service(r.soldium_service_id, **kwargs)
            # Keep in-memory row current for idempotent second pass.
            if "service_type" in kwargs:
                r.service_type = kwargs["service_type"]
            if "ordering_mode" in kwargs:
                r.ordering_mode = kwargs["ordering_mode"]
            if "target_link_type" in kwargs:
                r.target_link_type = kwargs["target_link_type"]
                r.safe_author_link_type = None

        details.append(
            {
                "service_id": r.soldium_service_id,
                "legacy_catalog_id": r.legacy_catalog_id,
                "before": before,
                "after": after,
                "confidence": r.service_type_confidence,
                "evidence": r.service_type_evidence,
            }
        )

    return {
        "dry_run": dry_run,
        "updated_service_type": updated_type,
        "updated_link_type": updated_link,
        "skipped": skipped,
        "details": details[:80],
        "details_truncated": max(0, len(details) - 80),
        "change_count": len(details),
    }


def production_counts(conn: sqlite3.Connection) -> dict[str, int]:
    def c(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "orders": c("SELECT COUNT(*) FROM orders"),
        "smm_services": c("SELECT COUNT(*) FROM smm_services"),
        "services": c("SELECT COUNT(*) FROM soldium_catalog_services"),
        "nodes": c("SELECT COUNT(*) FROM soldium_catalog_nodes"),
        "entries": c("SELECT COUNT(*) FROM soldium_catalog_entries"),
        "prices": c("SELECT COUNT(*) FROM soldium_catalog_prices"),
        "execution_sources": c(
            "SELECT COUNT(*) FROM soldium_catalog_execution_sources"
        ),
        "mappings": c("SELECT COUNT(*) FROM soldium_provider_service_mappings"),
        "publications": c("SELECT COUNT(*) FROM soldium_catalog_publications"),
        "service_type_other": c(
            "SELECT COUNT(*) FROM soldium_catalog_services "
            "WHERE service_type='other'"
        ),
        "with_link_type": c(
            "SELECT COUNT(*) FROM soldium_catalog_services "
            "WHERE target_link_type IS NOT NULL "
            "AND length(trim(target_link_type))>0"
        ),
    }


def run_phase9d_audit(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = load_phase9d_rows(conn)
    matrices = build_matrices(rows)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "remaining_count": len(rows),
        "pilot_count": len(PILOT_SET),
        "matrices": matrices,
        "rows": [r.to_dict() for r in rows],
    }
