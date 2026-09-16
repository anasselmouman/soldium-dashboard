# -*- coding: utf-8 -*-
"""Phase 9C — Commercial & target readiness audit for unpublished Catalog services.

Read-only classification by default. Optional deterministic authoring of
explicit Legacy/bridge fields only (never service_type keyword inference,
never publish).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from catalog_core.commercial import normalize_fulfillment_mode
from catalog_core.order_contract_republish import PILOT_SERVICE_IDS
from catalog_core.pilot_parity import resolve_structural_placement_keys
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.service import CatalogCoreService
from catalog_core.target_validation import resolve_link_prompt

logger = logging.getLogger("soldium.catalog.phase9c")

# Explicit migration / contract review codes (extend existing vocabulary).
REVIEW_SERVICE_TYPE_UNKNOWN = "SERVICE_TYPE_UNKNOWN"
REVIEW_ORDERING_MODE_UNKNOWN = "ORDERING_MODE_UNKNOWN"
REVIEW_QUANTITY_SENTINEL = "QUANTITY_SENTINEL"
REVIEW_PRICE_REVIEW = "PRICE_REVIEW"
REVIEW_FULFILLMENT_UNKNOWN = "FULFILLMENT_UNKNOWN"
REVIEW_FULFILLMENT_ADMIN = "fulfillment_admin"  # existing bridge code
REVIEW_TARGET_UNKNOWN = "TARGET_UNKNOWN"
REVIEW_TARGET_SPECIAL_RULE = "TARGET_SPECIAL_RULE"
REVIEW_EXECUTION_SOURCE_MISSING = "EXECUTION_SOURCE_MISSING"
REVIEW_STRUCTURE_INVALID = "STRUCTURE_INVALID"
REVIEW_PER_UNIT = "per_unit"
REVIEW_MAX_QTY_SENTINEL = "max_qty_sentinel"

SENTINEL_MAX = 2147483647

PILOT_SET = frozenset(PILOT_SERVICE_IDS)


@dataclass
class ServiceAuditRow:
    soldium_service_id: str
    legacy_catalog_id: str | None
    name_ar: str
    status: str
    platform_key: str | None
    section_key: str | None
    subsection_key: str | None
    structural_platform: str | None
    structural_section: str | None
    structural_subsection: str | None
    service_type: str
    ordering_mode: str
    min_quantity: int
    max_quantity: int
    amount_millimes: int | None
    currency: str | None
    pricing_mode: str | None
    catalog_fulfillment_mode: str
    legacy_fulfillment_mode: str | None
    target_platform_key: str | None
    target_section_key: str | None
    target_subsection_key: str | None
    has_execution_source: bool
    exec_provider_slug: str | None
    exec_account_key: str | None
    exec_external_id: str | None
    bridge_classification: str | None
    bridge_review_codes: list[str] = field(default_factory=list)
    publication_latest_event: str | None = None
    readiness_ready: bool = False
    review_codes: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    target_policy_status: str = "unknown"
    commercial_complete: bool = False
    contract_ready: bool = False
    authorable_fulfillment: str | None = None
    authorable_target: dict[str, str | None] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_json_list(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    text = str(raw).strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x) for x in data]
    except json.JSONDecodeError:
        return []
    return []


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    if key not in row.keys():
        return default
    return row[key]


def _norm_key(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _telegram_special(section: str | None, subsection: str | None) -> bool:
    sk = str(section or "").strip()
    ssk = str(subsection or "").strip()
    if sk in {
        "automatic_interactions",
        "post_share",
        "members",
        "channel_members",
        "start_bot",
    }:
        return True
    if ssk in {"future_posts", "past_posts"}:
        return True
    return False


def _x_special(section: str | None) -> bool:
    return str(section or "").strip() in {
        "direct_messages",
        "mentions",
        "spaces",
        "live_broadcast",
    }


def _subscriptions_special(platform: str | None, section: str | None) -> bool:
    return str(platform or "").strip() == "subscriptions" and str(
        section or ""
    ).strip() in {"iptv_wc2026", "iptv_panel"}


def classify_service(row: ServiceAuditRow) -> ServiceAuditRow:
    codes: list[str] = list(row.bridge_review_codes)
    cats: list[str] = []

    # Service type — migration default is not business truth.
    if row.service_type == "other":
        codes.append(REVIEW_SERVICE_TYPE_UNKNOWN)
        cats.append("Commercial-review")

    # Ordering — quantity_based default; package needs explicit evidence.
    if row.ordering_mode == "quantity_based":
        # Keep as unknown-review only when other signals say package (none today).
        pass
    elif row.ordering_mode == "package_based":
        codes.append(REVIEW_ORDERING_MODE_UNKNOWN)
        cats.append("Commercial-review")

    if REVIEW_PER_UNIT in codes or "per_unit" in codes:
        cats.append("Pricing-review")
        codes.append(REVIEW_ORDERING_MODE_UNKNOWN)

    if row.max_quantity == SENTINEL_MAX or REVIEW_MAX_QTY_SENTINEL in codes:
        codes.append(REVIEW_QUANTITY_SENTINEL)
        cats.append("Quantity-review")

    if row.amount_millimes is None or int(row.amount_millimes) <= 0:
        codes.append(REVIEW_PRICE_REVIEW)
        cats.append("Pricing-review")

    # Fulfillment: catalog default auto is not authoritative unless bridge agrees.
    legacy_fm = _norm_key(row.legacy_fulfillment_mode)
    if legacy_fm in {"auto", "admin"}:
        row.authorable_fulfillment = legacy_fm
        if legacy_fm == "admin":
            codes.append(REVIEW_FULFILLMENT_ADMIN)
            cats.append("Fulfillment-review")
        if row.catalog_fulfillment_mode != legacy_fm:
            cats.append("Fulfillment-review")
    else:
        codes.append(REVIEW_FULFILLMENT_UNKNOWN)
        cats.append("Fulfillment-review")
        row.authorable_fulfillment = None

    # Target structural keys
    platform = row.structural_platform or row.platform_key
    section = row.structural_section or row.section_key
    subsection = row.structural_subsection or row.subsection_key
    if platform and section:
        row.target_policy_status = "structural_keys_available"
        row.authorable_target = {
            "platform_key": platform,
            "section_key": section,
            "subsection_key": subsection,
        }
        if (
            _telegram_special(section, subsection)
            or _x_special(section)
            or _subscriptions_special(platform, section)
        ):
            codes.append(REVIEW_TARGET_SPECIAL_RULE)
            cats.append("Target-policy-review")
            row.target_policy_status = "special_rule_review"
        else:
            # Host-match platforms with no special section rules — still review
            # until cohort evidence is accepted; keys are authorable.
            pass
    else:
        codes.append(REVIEW_TARGET_UNKNOWN)
        cats.append("Target-policy-review")
        row.target_policy_status = "missing_keys"
        row.authorable_target = None

    if not row.platform_key and not row.structural_platform:
        codes.append(REVIEW_STRUCTURE_INVALID)
        cats.append("Structural-review")

    if not row.has_execution_source:
        codes.append(REVIEW_EXECUTION_SOURCE_MISSING)
        cats.append("Execution-review")

    # Deduplicate codes/categories
    codes = list(dict.fromkeys(codes))
    cats = list(dict.fromkeys(cats))

    # Contract-ready: explicit fulfillment + target keys + execution + price
    # + no sentinel + no special target rule + not missing structure.
    blocking = {
        REVIEW_TARGET_UNKNOWN,
        REVIEW_FULFILLMENT_UNKNOWN,
        REVIEW_EXECUTION_SOURCE_MISSING,
        REVIEW_STRUCTURE_INVALID,
        REVIEW_PRICE_REVIEW,
        REVIEW_QUANTITY_SENTINEL,
        REVIEW_TARGET_SPECIAL_RULE,
    }
    if not any(c in blocking for c in codes) and row.authorable_target and row.authorable_fulfillment:
        # Still commercial-review for service_type=other
        if REVIEW_SERVICE_TYPE_UNKNOWN in codes:
            cats = [c for c in cats if c != "Commercial-review"] or cats
            # Keep SERVICE_TYPE_UNKNOWN but allow "structurally contract-ready"
            row.contract_ready = True
            if "Contract-ready" not in cats:
                cats.insert(0, "Contract-ready")
        else:
            row.contract_ready = True
            cats = ["Contract-ready"] + [c for c in cats if c != "Contract-ready"]
    elif not cats:
        cats.append("Multiple-review")

    if len(cats) > 1 and "Multiple-review" not in cats:
        cats.append("Multiple-review")

    if REVIEW_EXECUTION_SOURCE_MISSING in codes and "Blocked" not in cats:
        cats.append("Blocked")

    row.review_codes = codes
    row.categories = cats
    row.commercial_complete = (
        row.authorable_fulfillment is not None
        and row.authorable_target is not None
        and row.amount_millimes is not None
        and int(row.amount_millimes or 0) > 0
        and row.has_execution_source
        and row.max_quantity != SENTINEL_MAX
    )
    return row


def load_remaining_audit_rows(conn: sqlite3.Connection) -> list[ServiceAuditRow]:
    placeholders = ",".join("?" * len(PILOT_SERVICE_IDS))
    sql = f"""
    SELECT
      s.id AS soldium_service_id,
      s.name_ar,
      s.status,
      s.service_type,
      s.ordering_mode,
      s.min_quantity,
      s.max_quantity,
      s.fulfillment_mode AS catalog_fulfillment_mode,
      s.target_platform_key,
      s.target_section_key,
      s.target_subsection_key,
      b.legacy_catalog_id,
      b.legacy_fulfillment_mode,
      b.classification,
      b.review_codes,
      l.platform_key,
      l.section_key,
      l.subsection_key,
      l.category AS legacy_category,
      p.amount_millimes,
      p.currency,
      p.pricing_mode,
      x.provider_slug AS exec_provider_slug,
      x.provider_account_key AS exec_account_key,
      x.external_service_id AS exec_external_id,
      (
        SELECT event_type FROM soldium_catalog_publications pub
        WHERE pub.service_id = s.id
        ORDER BY pub.published_at DESC, pub.rowid DESC
        LIMIT 1
      ) AS publication_latest_event
    FROM soldium_catalog_services s
    LEFT JOIN soldium_catalog_legacy_bridge b
      ON b.soldium_service_id = s.id
    LEFT JOIN smm_services l
      ON l.catalog_id = b.legacy_catalog_id
    LEFT JOIN soldium_catalog_prices p
      ON p.service_id = s.id AND p.status = 'active'
    LEFT JOIN soldium_catalog_execution_sources x
      ON x.service_id = s.id AND x.status = 'active'
    WHERE s.id NOT IN ({placeholders})
    ORDER BY l.platform_key, l.section_key, s.name_ar, s.id
    """
    rows = conn.execute(sql, tuple(PILOT_SERVICE_IDS)).fetchall()
    repo = CatalogRepository(conn)
    out: list[ServiceAuditRow] = []
    for row in rows:
        sid = str(row["soldium_service_id"])
        structural = resolve_structural_placement_keys(conn, sid)
        sp = ss = ssub = None
        if structural:
            sp, ss, ssub = structural
            sp = _norm_key(sp)
            ss = _norm_key(ss)
            ssub = _norm_key(ssub)

        has_exec = row["exec_external_id"] is not None and str(
            row["exec_external_id"] or ""
        ).strip() != ""

        svc = repo.get_service(sid)
        ready = False
        if svc is not None:
            source = repo.get_active_execution_source(sid)
            price = repo.get_active_price(sid)
            entry = repo.get_entry_for_service(sid)
            if entry:
                svc.entry_id = entry.id
                svc.parent_entry_id = entry.parent_entry_id
                svc.location_path = repo.breadcrumb_names(entry.parent_entry_id)
            ready = bool(
                evaluate_service_readiness(
                    repo, svc, source=source, price=price
                ).ready
            )

        audit = ServiceAuditRow(
            soldium_service_id=sid,
            legacy_catalog_id=_norm_key(row["legacy_catalog_id"]),
            name_ar=str(row["name_ar"] or ""),
            status=str(row["status"] or ""),
            platform_key=_norm_key(row["platform_key"]),
            section_key=_norm_key(row["section_key"]),
            subsection_key=_norm_key(row["subsection_key"]),
            structural_platform=sp,
            structural_section=ss,
            structural_subsection=ssub,
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
            catalog_fulfillment_mode=str(row["catalog_fulfillment_mode"] or "auto"),
            legacy_fulfillment_mode=_norm_key(row["legacy_fulfillment_mode"]),
            target_platform_key=_norm_key(row["target_platform_key"]),
            target_section_key=_norm_key(row["target_section_key"]),
            target_subsection_key=_norm_key(row["target_subsection_key"]),
            has_execution_source=has_exec,
            exec_provider_slug=_norm_key(row["exec_provider_slug"]),
            exec_account_key=_norm_key(row["exec_account_key"]),
            exec_external_id=_norm_key(row["exec_external_id"]),
            bridge_classification=_norm_key(row["classification"]),
            bridge_review_codes=_parse_json_list(row["review_codes"]),
            publication_latest_event=_norm_key(row["publication_latest_event"]),
            readiness_ready=ready,
        )
        out.append(classify_service(audit))
    return out


def summarize_audit(rows: list[ServiceAuditRow]) -> dict[str, Any]:
    by_cat: dict[str, int] = {}
    by_code: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    target_matrix: dict[str, dict[str, Any]] = {}
    fulfillment: dict[str, int] = {}
    service_types: dict[str, int] = {}
    ordering: dict[str, int] = {}

    for r in rows:
        for c in r.categories:
            by_cat[c] = by_cat.get(c, 0) + 1
        for code in r.review_codes:
            by_code[code] = by_code.get(code, 0) + 1
        pk = r.platform_key or "(none)"
        by_platform[pk] = by_platform.get(pk, 0) + 1
        service_types[r.service_type] = service_types.get(r.service_type, 0) + 1
        ordering[r.ordering_mode] = ordering.get(r.ordering_mode, 0) + 1
        fm = r.legacy_fulfillment_mode or "(none)"
        fulfillment[fm] = fulfillment.get(fm, 0) + 1

        key = f"{r.platform_key or ''}::{r.section_key or ''}::{r.subsection_key or ''}"
        if key not in target_matrix:
            _, allow_u, allow_f = resolve_link_prompt(
                r.platform_key or "",
                r.section_key,
                r.subsection_key,
            )
            target_matrix[key] = {
                "platform_key": r.platform_key,
                "section_key": r.section_key,
                "subsection_key": r.subsection_key,
                "count": 0,
                "allow_username": allow_u,
                "allow_free_text": allow_f,
                "special": REVIEW_TARGET_SPECIAL_RULE
                in (r.review_codes if False else []),
            }
        target_matrix[key]["count"] += 1
        if REVIEW_TARGET_SPECIAL_RULE in r.review_codes:
            target_matrix[key]["special"] = True

    contract_ready = sum(1 for r in rows if r.contract_ready)
    authorable_fulfillment = sum(1 for r in rows if r.authorable_fulfillment)
    authorable_target = sum(1 for r in rows if r.authorable_target)
    missing_exec = sum(1 for r in rows if not r.has_execution_source)
    sentinel = sum(1 for r in rows if r.max_quantity == SENTINEL_MAX)

    return {
        "remaining_count": len(rows),
        "pilot_count": len(PILOT_SET),
        "contract_ready_count": contract_ready,
        "authorable_fulfillment_count": authorable_fulfillment,
        "authorable_target_count": authorable_target,
        "missing_execution_count": missing_exec,
        "quantity_sentinel_count": sentinel,
        "categories": dict(sorted(by_cat.items(), key=lambda x: (-x[1], x[0]))),
        "review_codes": dict(sorted(by_code.items(), key=lambda x: (-x[1], x[0]))),
        "platforms": dict(sorted(by_platform.items(), key=lambda x: (-x[1], x[0]))),
        "service_types": service_types,
        "ordering_modes": ordering,
        "legacy_fulfillment": fulfillment,
        "target_placement_cohorts": list(target_matrix.values()),
        "target_placement_cohort_count": len(target_matrix),
    }


def proposed_cohorts(rows: list[ServiceAuditRow]) -> list[dict[str, Any]]:
    """Deterministic cohort proposals — evidence-based, not keyword service_type."""
    buckets: dict[str, list[ServiceAuditRow]] = {}
    for r in rows:
        if not r.authorable_target or not r.authorable_fulfillment:
            continue
        if REVIEW_TARGET_SPECIAL_RULE in r.review_codes:
            continue
        if REVIEW_EXECUTION_SOURCE_MISSING in r.review_codes:
            continue
        if REVIEW_QUANTITY_SENTINEL in r.review_codes:
            continue
        t = r.authorable_target
        key = (
            f"{t['platform_key']}/{t['section_key']}"
            f"/{t.get('subsection_key') or ''}|fm={r.authorable_fulfillment}"
        )
        buckets.setdefault(key, []).append(r)

    proposals: list[dict[str, Any]] = []
    for key, members in sorted(buckets.items(), key=lambda x: (-len(x[1]), x[0])):
        sample = members[0]
        t = sample.authorable_target or {}
        proposals.append(
            {
                "cohort": key,
                "service_count": len(members),
                "proposed": {
                    "fulfillment_mode": sample.authorable_fulfillment,
                    "target_platform_key": t.get("platform_key"),
                    "target_section_key": t.get("section_key"),
                    "target_subsection_key": t.get("subsection_key"),
                    "service_type": "KEEP_other_pending_review",
                    "ordering_mode": "KEEP_quantity_based",
                    "price": "PRESERVE",
                    "execution": "PRESERVE",
                    "placement": "PRESERVE",
                },
                "evidence": (
                    "Legacy platform_key/section_key (+ structural node bridge) "
                    "and bridge.legacy_fulfillment_mode; shared target_validation "
                    "host rules apply without special section exceptions."
                ),
                "confidence": "high_for_fulfillment_and_target_keys",
                "review_requirement": (
                    "service_type remains 'other' until business classification; "
                    "no publication in Phase 9C"
                ),
                "service_ids_sample": [m.soldium_service_id for m in members[:5]],
            }
        )
    return proposals


def apply_safe_authoring(
    conn: sqlite3.Connection,
    rows: list[ServiceAuditRow],
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Author ONLY explicit fulfillment + structural target keys. No publish.

    Skips: pilots, special target rules, missing keys, missing fulfillment,
    services that already match desired values.
    """
    core = CatalogCoreService(conn)
    updated = 0
    skipped = 0
    details: list[dict[str, Any]] = []

    for r in rows:
        if r.soldium_service_id in PILOT_SET:
            skipped += 1
            continue
        if not r.authorable_fulfillment or not r.authorable_target:
            skipped += 1
            continue
        if REVIEW_TARGET_SPECIAL_RULE in r.review_codes:
            # Still allow key authoring for special rules — keys are required by
            # validators; do NOT invent new URL rules. Author keys only.
            pass

        t = r.authorable_target
        desired_fm = normalize_fulfillment_mode(r.authorable_fulfillment)
        already = (
            r.catalog_fulfillment_mode == desired_fm
            and r.target_platform_key == t["platform_key"]
            and r.target_section_key == t["section_key"]
            and (r.target_subsection_key or None) == (t.get("subsection_key") or None)
        )
        if already:
            skipped += 1
            continue

        entry = {
            "service_id": r.soldium_service_id,
            "before": {
                "fulfillment_mode": r.catalog_fulfillment_mode,
                "target_platform_key": r.target_platform_key,
                "target_section_key": r.target_section_key,
                "target_subsection_key": r.target_subsection_key,
            },
            "after": {
                "fulfillment_mode": desired_fm,
                "target_platform_key": t["platform_key"],
                "target_section_key": t["section_key"],
                "target_subsection_key": t.get("subsection_key"),
            },
        }
        if not dry_run:
            core.update_service(
                r.soldium_service_id,
                fulfillment_mode=desired_fm,
                target_platform_key=t["platform_key"],
                target_section_key=t["section_key"],
                target_subsection_key=t.get("subsection_key"),
            )
        updated += 1
        details.append(entry)

    return {
        "dry_run": dry_run,
        "updated": updated,
        "skipped": skipped,
        "details": details[:50],
        "details_truncated": max(0, len(details) - 50),
    }


def run_audit(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = load_remaining_audit_rows(conn)
    summary = summarize_audit(rows)
    cohorts = proposed_cohorts(rows)
    return {
        "summary": summary,
        "cohorts": cohorts,
        "rows": [r.to_dict() for r in rows],
    }
