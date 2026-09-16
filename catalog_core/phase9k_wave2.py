# -*- coding: utf-8 -*-
"""Phase 9K — Controlled Expansion Wave 2 publication (exact 15 from Phase 9J)."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from catalog_core.phase9c_audit import SENTINEL_MAX
from catalog_core.phase9d_audit import load_phase9d_rows, production_counts
from catalog_core.phase9e_stage_b import _pilot_sample_target
from catalog_core.phase9g_business_decision_pack import _published_ids
from catalog_core.phase9h_wave1 import WAVE1_CANDIDATE_LEGACY_IDS, _classify_row
from catalog_core.phase9i_expansion_validation import (
    audit_4371,
    audit_scheduled_orders,
)
from catalog_core.publication import CatalogPublicationService
from catalog_core.publication_pilot import PilotCandidate, publish_pilot, validate_pilot_intents
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.storefront_adapter import StorefrontAdapter
from catalog_core.storefront_projection import PublishedStorefrontProjection
from catalog_core.storefront_shadow import compare_storefronts
from catalog_core.target_validation import validate_order_target

PHASE = "9K"
PUBLISHED_BY = "phase9k_wave2"

# Exact Phase 9J RECOMMENDED_WAVE_2 Legacy IDs (also Provider Service IDs in this set).
WAVE2_LEGACY_IDS: tuple[str, ...] = (
    "2003",
    "2484",
    "2038",
    "4554",
    "4865",
    "3044",
    "4806",
    "2035",
    "4553",
    "3043",
    "4691",
    "3604",
    "4552",
    "3042",
    "3550",
)

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

EXPECTED_AFTER = {
    "publications": 53,
    "published_services": 43,
}


class Wave2Block(Exception):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


def _scheduled_count(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("SELECT COUNT(*) FROM scheduled_orders").fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def capture_baseline(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = production_counts(conn)
    published = sorted(_published_ids(conn))
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
        "published_services": len(published),
        "scheduled_orders": _scheduled_count(conn),
    }
    mismatches = {
        k: {"expected": EXPECTED_BASELINE[k], "actual": baseline[k]}
        for k in EXPECTED_BASELINE
        if baseline.get(k) != EXPECTED_BASELINE[k]
    }
    return {"baseline": baseline, "mismatches": mismatches, "ok": not mismatches}


def map_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for legacy_id in WAVE2_LEGACY_IDS:
        rows = conn.execute(
            """
            SELECT b.legacy_catalog_id, b.soldium_service_id,
                   l.platform_key, l.section_key, l.subsection_key
            FROM soldium_catalog_legacy_bridge b
            LEFT JOIN smm_services l ON l.catalog_id = b.legacy_catalog_id
            WHERE b.legacy_catalog_id = ?
            """,
            (legacy_id,),
        ).fetchall()
        if len(rows) != 1:
            raise Wave2Block(
                f"Legacy {legacy_id} mapping ambiguous or missing (rows={len(rows)})",
                {"legacy_catalog_id": legacy_id, "row_count": len(rows)},
            )
        row = rows[0]
        sid = str(row["soldium_service_id"])
        if not sid.startswith("svc_"):
            raise Wave2Block(
                f"soldium_service_id must be svc_* for {legacy_id}",
                {"soldium_service_id": sid},
            )
        mapped.append(
            {
                "legacy_catalog_id": legacy_id,
                "soldium_service_id": sid,
                "platform_key": str(row["platform_key"] or "").strip() or None,
                "section_key": str(row["section_key"] or "").strip() or None,
                "subsection_key": (
                    str(row["subsection_key"]).strip()
                    if row["subsection_key"] not in (None, "")
                    else None
                ),
            }
        )
    svc_ids = [m["soldium_service_id"] for m in mapped]
    if len(set(svc_ids)) != len(WAVE2_LEGACY_IDS):
        raise Wave2Block(
            "Duplicate soldium_service_id in Wave 2 mapping", {"ids": svc_ids}
        )
    overlap = set(WAVE2_LEGACY_IDS) & set(WAVE1_CANDIDATE_LEGACY_IDS)
    if overlap:
        raise Wave2Block(
            "Wave 2 overlaps Wave 1 Legacy IDs", {"overlap": sorted(overlap)}
        )
    return mapped


def _fail(mapping: dict[str, Any], errors: list[str], classification: str) -> dict[str, Any]:
    return {
        **mapping,
        "gate_passed": False,
        "gate_errors": errors,
        "classification": classification,
        "exclusion_reason": "; ".join(errors),
        "معرّف_المزود": None,
        "provider_service_id": None,
        "external_service_id": None,
    }


def gate_candidate(
    conn: sqlite3.Connection, mapping: dict[str, Any], *, phase9d_row
) -> dict[str, Any]:
    sid = mapping["soldium_service_id"]
    legacy_id = mapping["legacy_catalog_id"]
    repo = CatalogRepository(conn)
    pub = CatalogPublicationService(conn)
    svc = repo.get_service(sid)
    if svc is None:
        return _fail(mapping, ["service_missing"], "REVIEW")
    if str(svc.status) == "archived":
        return _fail(mapping, ["archived"], "REVIEW")

    price = repo.get_active_price(sid)
    source = repo.get_active_execution_source(sid)
    entry = repo.get_entry_for_service(sid)
    if entry:
        svc.entry_id = entry.id
        svc.parent_entry_id = entry.parent_entry_id
        svc.location_path = repo.breadcrumb_names(entry.parent_entry_id)

    ready = evaluate_service_readiness(repo, svc, source=source, price=price)
    pub_status = pub.get_publication_status(sid)
    classification = _classify_row(phase9d_row) if phase9d_row else "REVIEW"

    errors: list[str] = []
    if pub_status.get("publication_status") == "published":
        errors.append("already_published")
    if not ready.ready:
        errors.append(
            "not_ready:" + ",".join(i.code for i in (ready.issues or []))
        )
    if int(svc.min_quantity) <= 0:
        errors.append("min_quantity_invalid")
    if int(svc.max_quantity) <= 0 or int(svc.max_quantity) == SENTINEL_MAX:
        errors.append(f"max_not_finite:{svc.max_quantity}")
    if int(svc.min_quantity) > int(svc.max_quantity):
        errors.append("min_gt_max")
    if price is None or int(price.amount_millimes) <= 0:
        errors.append("price_invalid")
    if price is not None and str(price.currency).upper() != "MAD":
        errors.append(f"currency:{price.currency}")
    if price is not None and price.pricing_mode not in {
        "per_1000",
        "per_unit",
        "fixed_package",
    }:
        errors.append(f"pricing_mode:{price.pricing_mode}")
    if str(svc.fulfillment_mode or "") not in {"auto", "admin"}:
        errors.append(f"fulfillment:{svc.fulfillment_mode}")
    if not (svc.target_platform_key and svc.target_section_key):
        errors.append("target_policy_missing")
    if phase9d_row and phase9d_row.target_special:
        errors.append("unresolved_special_target")
    if source is None:
        errors.append("execution_missing")
    else:
        if not str(source.provider_slug or "").strip():
            errors.append("provider_slug_missing")
        if not str(source.provider_account_key or "").strip():
            errors.append("provider_account_key_missing")
        ext = source.external_service_id
        if ext is None or str(ext).strip() == "":
            errors.append("external_service_id_missing")
    if classification != "STANDARD":
        errors.append(f"classification_not_standard:{classification}")
    if svc.ordering_mode not in {"quantity_based", "package_based"}:
        errors.append(f"ordering_mode:{svc.ordering_mode}")
    if phase9d_row and phase9d_row.bridge_review_codes:
        blocking = [
            c
            for c in phase9d_row.bridge_review_codes
            if c
            in {
                "missing_provider_account",
                "max_qty_sentinel",
                "needs_manual_review",
            }
        ]
        if blocking:
            errors.append("review_codes:" + ",".join(blocking))

    ext_str = str(source.external_service_id) if source else None
    passed = not errors
    readiness_components = {}
    for c in ready.checks or []:
        readiness_components[c.check] = {"ok": c.ok}

    return {
        "معرّف_المزود": ext_str,
        "legacy_catalog_id": legacy_id,
        "soldium_service_id": sid,
        "provider_slug": source.provider_slug if source else None,
        "provider_account_key": source.provider_account_key if source else None,
        "platform_key": mapping["platform_key"],
        "section_key": mapping["section_key"],
        "subsection_key": mapping["subsection_key"],
        "name_ar": svc.name_ar,
        "status": svc.status,
        "service_type": svc.service_type,
        "ordering_mode": svc.ordering_mode,
        "min_quantity": svc.min_quantity,
        "max_quantity": svc.max_quantity,
        "fulfillment_mode": svc.fulfillment_mode,
        "target_policy": {
            "platform_key": svc.target_platform_key,
            "section_key": svc.target_section_key,
            "subsection_key": svc.target_subsection_key,
            "link_prompt_key": svc.target_link_prompt_key,
            "link_type": svc.target_link_type,
        },
        "amount_millimes": int(price.amount_millimes) if price else None,
        "currency": price.currency if price else None,
        "pricing_mode": price.pricing_mode if price else None,
        "provider_service_id": ext_str,
        "external_service_id": ext_str,
        "readiness_ready": bool(ready.ready),
        "readiness_components": readiness_components,
        "publication_status": pub_status.get("publication_status"),
        "classification": classification,
        "gate_passed": passed,
        "gate_errors": errors,
        "exclusion_reason": "; ".join(errors) if errors else None,
        "location_path": list(svc.location_path or []),
    }


def run_preflight(conn: sqlite3.Connection) -> dict[str, Any]:
    before = capture_baseline(conn)
    if not before["ok"]:
        raise Wave2Block("Baseline mismatch — STOP BEFORE PUBLICATION", before)

    mapped = map_candidates(conn)
    published = _published_ids(conn)
    all_rows = {r.soldium_service_id: r for r in load_phase9d_rows(conn)}
    unpublished_rows = [
        r for r in load_phase9d_rows(conn) if r.soldium_service_id not in published
    ]
    by_legacy = {r.legacy_catalog_id: r for r in unpublished_rows}

    gated = []
    for m in mapped:
        row = by_legacy.get(m["legacy_catalog_id"]) or all_rows.get(
            m["soldium_service_id"]
        )
        gated.append(gate_candidate(conn, m, phase9d_row=row))

    failed = [g for g in gated if not g["gate_passed"]]
    if failed:
        raise Wave2Block(
            "Wave 2 preflight failed — exact 15 required",
            {
                "failed": [
                    {
                        "legacy_catalog_id": f["legacy_catalog_id"],
                        "errors": f["gate_errors"],
                    }
                    for f in failed
                ]
            },
        )
    if len(gated) != 15:
        raise Wave2Block(f"Expected exactly 15 gated rows, got {len(gated)}")

    identity_table = []
    for g in gated:
        identity_table.append(
            {
                "معرّف_المزود": g["معرّف_المزود"],
                "legacy_catalog_id": g["legacy_catalog_id"],
                "soldium_service_id": g["soldium_service_id"],
                "provider_slug": g["provider_slug"],
                "provider_account_key": g["provider_account_key"],
                "platform": g["platform_key"],
                "section": g["section_key"],
                "subsection": g["subsection_key"],
                "service_type": g["service_type"],
                "ordering_mode": g["ordering_mode"],
                "min_quantity": g["min_quantity"],
                "max_quantity": g["max_quantity"],
                "price_amount_millimes": g["amount_millimes"],
                "currency": g["currency"],
                "pricing_mode": g["pricing_mode"],
                "fulfillment_mode": g["fulfillment_mode"],
                "target_policy": g["target_policy"],
                "readiness_ready": g["readiness_ready"],
            }
        )

    return {
        "baseline": before["baseline"],
        "wave": gated,
        "identity_table": identity_table,
        "wave_size": 15,
        "all_fifteen_passed": True,
    }


def snapshot_published_ids(
    conn: sqlite3.Connection, service_ids: set[str] | list[str]
) -> list[dict[str, Any]]:
    pub = CatalogPublicationService(conn)
    out = []
    for sid in sorted(service_ids):
        latest = pub.get_latest_publish(sid)
        st = pub.get_publication_status(sid)
        out.append(
            {
                "service_id": sid,
                "publication_status": st.get("publication_status"),
                "content_fingerprint": (
                    latest.content_fingerprint if latest else None
                ),
                "publication_id": latest.id if latest else None,
                "external_service_id": (
                    str(latest.external_service_id)
                    if latest and latest.external_service_id is not None
                    else None
                ),
                "provider_slug": latest.provider_slug if latest else None,
                "provider_account_key": (
                    latest.provider_account_key if latest else None
                ),
            }
        )
    return out


def shadow_difference_breakdown(shadow: dict[str, Any]) -> dict[str, Any]:
    by_category: Counter[str] = Counter()
    by_impact: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    impact_map = {
        "dangerous": "CUSTOMER_BREAKING",
        "review": "REVIEW_ONLY",
        "info": "NON_BREAKING_LEGACY_REPRESENTATION",
        "baseline": "NON_BREAKING_LEGACY_REPRESENTATION",
        "equal": "NONE",
    }
    for row in shadow.get("rows") or []:
        sev = row.get("severity") or "info"
        impact = impact_map.get(str(sev), str(sev))
        by_impact[impact] += 1
        for d in row.get("differences") or []:
            cat = d.get("category") or "unknown"
            by_category[cat] += 1
            if len(samples) < 40:
                samples.append(
                    {
                        "service_id": row.get("catalog_identity")
                        or row.get("legacy_identity"),
                        "category": cat,
                        "code": d.get("code"),
                        "row_severity": sev,
                        "impact": impact,
                    }
                )
    return {
        "by_category": dict(by_category),
        "by_impact": dict(by_impact),
        "sample": samples,
        "categories_expected": [
            "identity",
            "availability",
            "name",
            "price",
            "quantity",
            "ordering",
            "service_type",
            "placement",
            "execution",
            "orderability",
            "fulfillment",
        ],
    }


def _wave_distribution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "platforms": dict(
            Counter(r.get("platform_key") or r.get("platform") for r in rows)
        ),
        "service_types": dict(Counter(r.get("service_type") for r in rows)),
        "ordering_modes": dict(Counter(r.get("ordering_mode") for r in rows)),
        "pricing_modes": dict(Counter(r.get("pricing_mode") for r in rows)),
        "fulfillment_modes": dict(
            Counter(r.get("fulfillment_mode") for r in rows)
        ),
        "providers": dict(Counter(r.get("provider_slug") for r in rows)),
    }


def wave1_vs_wave2(conn: sqlite3.Connection, wave2: list[dict[str, Any]]) -> dict[str, Any]:
    pub = CatalogPublicationService(conn)
    w1 = []
    for lid in WAVE1_CANDIDATE_LEGACY_IDS:
        br = conn.execute(
            "SELECT soldium_service_id FROM soldium_catalog_legacy_bridge "
            "WHERE legacy_catalog_id=?",
            (lid,),
        ).fetchone()
        if not br:
            continue
        latest = pub.get_latest_publish(str(br["soldium_service_id"]))
        if not latest:
            continue
        w1.append(
            {
                "legacy_catalog_id": lid,
                "platform_key": latest.target_platform_key,
                "service_type": latest.service_type,
                "ordering_mode": latest.ordering_mode,
                "pricing_mode": latest.pricing_mode,
                "fulfillment_mode": latest.fulfillment_mode,
                "provider_slug": latest.provider_slug,
                "min_quantity": latest.min_quantity,
                "max_quantity": latest.max_quantity,
            }
        )
    return {
        "wave1_count": len(w1),
        "wave2_count": len(wave2),
        "wave1_distributions": _wave_distribution(w1),
        "wave2_distributions": _wave_distribution(wave2),
        "note": "Descriptive only — no statistical significance claimed.",
    }


def verify_post_publish(
    conn: sqlite3.Connection,
    wave: list[dict[str, Any]],
    existing_before: list[dict[str, Any]],
    pubs_before: int,
) -> dict[str, Any]:
    n = len(wave)
    if n != 15:
        raise Wave2Block(f"Wave size must be 15, got {n}")

    pub = CatalogPublicationService(conn)
    proj = PublishedStorefrontProjection(conn)
    adapter = StorefrontAdapter(conn)

    active = proj.list_services()
    if len(active) != EXPECTED_AFTER["published_services"]:
        raise Wave2Block(
            f"projection expected {EXPECTED_AFTER['published_services']}, "
            f"got {len(active)}"
        )
    wave_ids = {c["soldium_service_id"] for c in wave}
    active_ids = {s.service_id for s in active}
    if not wave_ids.issubset(active_ids):
        raise Wave2Block(
            "Wave 2 missing from projection",
            {"missing": sorted(wave_ids - active_ids)},
        )

    existing_ids = {x["service_id"] for x in existing_before}
    existing_after = snapshot_published_ids(conn, existing_ids)
    before_by_id = {x["service_id"]: x for x in existing_before}
    for row in existing_after:
        b = before_by_id[row["service_id"]]
        if b["content_fingerprint"] != row["content_fingerprint"]:
            raise Wave2Block(
                f"Existing published fingerprint changed: {row['service_id']}"
            )
        if b["publication_id"] != row["publication_id"]:
            raise Wave2Block(
                f"Existing publication id changed: {row['service_id']}"
            )

    counts = production_counts(conn)
    if counts["publications"] != EXPECTED_AFTER["publications"]:
        raise Wave2Block(
            f"publications expected {EXPECTED_AFTER['publications']}, "
            f"got {counts['publications']}"
        )
    if counts["publications"] != pubs_before + n:
        raise Wave2Block(
            f"publications delta expected +{n}, "
            f"got +{counts['publications'] - pubs_before}"
        )

    published_now = _published_ids(conn)
    if len(published_now) != EXPECTED_AFTER["published_services"]:
        raise Wave2Block(
            f"published_services expected {EXPECTED_AFTER['published_services']}, "
            f"got {len(published_now)}"
        )

    new_ids = published_now - existing_ids
    if new_ids != wave_ids:
        raise Wave2Block(
            "Unexpected newly published set",
            {
                "expected": sorted(wave_ids),
                "actual": sorted(new_ids),
                "extra": sorted(new_ids - wave_ids),
                "missing": sorted(wave_ids - new_ids),
            },
        )

    parity_rows = []
    snapshots = []
    target_matrix = []
    qty_matrix = []
    provider_ids = []
    eligibility = []

    for c in wave:
        sid = c["soldium_service_id"]
        latest = pub.get_latest_publish(sid)
        status = pub.get_publication_status(sid)
        if latest is None or status.get("publication_status") != "published":
            raise Wave2Block(f"Not published: {sid}")
        if latest.published_by != PUBLISHED_BY:
            raise Wave2Block(
                f"published_by mismatch for {sid}: {latest.published_by}"
            )
        if status.get("customer_catalog_eligible") is not True:
            raise Wave2Block(f"Not eligible: {sid}")

        projected = proj.get_service(sid)
        adapted = adapter.get_service(sid)
        ext = str(latest.external_service_id)

        snapshots.append(
            {
                "service_id": sid,
                "name_ar": latest.name_ar,
                "note_ar": getattr(latest, "note_ar", None),
                "location_path": list(latest.location_path or []),
                "service_type": latest.service_type,
                "ordering_mode": latest.ordering_mode,
                "min_quantity": latest.min_quantity,
                "max_quantity": latest.max_quantity,
                "amount_millimes": latest.amount_millimes,
                "currency": latest.currency,
                "pricing_mode": latest.pricing_mode,
                "fulfillment_mode": latest.fulfillment_mode,
                "target_platform_key": latest.target_platform_key,
                "target_section_key": latest.target_section_key,
                "target_subsection_key": latest.target_subsection_key,
                "provider_slug": latest.provider_slug,
                "provider_account_key": latest.provider_account_key,
                "external_service_id": ext,
                "content_fingerprint": latest.content_fingerprint,
                "published_at": latest.published_at,
                "published_by": latest.published_by,
            }
        )

        provider_ids.append(
            {
                "معرّف_المزود": ext,
                "legacy_catalog_id": c["legacy_catalog_id"],
                "soldium_service_id": sid,
                "provider_service_id": ext,
                "provider_slug": latest.provider_slug,
                "provider_account_key": latest.provider_account_key,
            }
        )

        pub_exec = (latest.provider_slug, latest.provider_account_key, ext)
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
        if not (pub_exec == proj_exec == adapt_exec):
            raise Wave2Block(
                f"Execution mismatch {sid}",
                {"pub": pub_exec, "proj": proj_exec, "adapter": adapt_exec},
            )

        if not (
            latest.fulfillment_mode
            == projected.fulfillment_mode
            == adapted.fulfillment_mode
        ):
            raise Wave2Block(f"Fulfillment mismatch {sid}")

        if not (
            latest.amount_millimes
            == projected.amount_millimes
            == adapted.price.amount_millimes
        ):
            raise Wave2Block(f"Price mismatch {sid}")

        if not (latest.currency == projected.currency == adapted.price.currency):
            raise Wave2Block(f"Currency mismatch {sid}")

        if not (
            latest.pricing_mode
            == projected.pricing_mode
            == adapted.price.pricing_mode
        ):
            raise Wave2Block(f"Pricing mode mismatch {sid}")

        if not (
            (latest.service_type, latest.ordering_mode)
            == (projected.service_type, projected.ordering_mode)
            == (adapted.service_type, adapted.ordering_mode)
        ):
            raise Wave2Block(f"Ordering/type mismatch {sid}")

        if not (
            (latest.min_quantity, latest.max_quantity)
            == (projected.min_quantity, projected.max_quantity)
            == (adapted.min_quantity, adapted.max_quantity)
        ):
            raise Wave2Block(f"Quantity mismatch {sid}")

        sample = _pilot_sample_target(
            latest.target_platform_key,
            latest.target_section_key,
            latest.target_subsection_key,
        )
        qty = int(latest.min_quantity)
        quote = adapter.quote_price(sid, qty)
        intent = adapter.resolve_order_intent(sid, qty, target=sample)
        intent_exec = (
            intent.provider_slug,
            intent.provider_account_key,
            str(intent.external_service_id),
        )
        if intent_exec != pub_exec:
            raise Wave2Block(f"Intent execution mismatch {sid}")
        if intent.content_fingerprint != latest.content_fingerprint:
            raise Wave2Block(f"Intent fingerprint mismatch {sid}")
        if intent.quoted_amount_millimes != quote.quoted_amount_millimes:
            raise Wave2Block(f"Intent quote mismatch {sid}")
        if quote.unit_amount_millimes != latest.amount_millimes:
            raise Wave2Block(f"Unit price mismatch {sid}")
        if intent.fulfillment_mode != latest.fulfillment_mode:
            raise Wave2Block(f"Intent fulfillment mismatch {sid}")
        if adapted.service_type != latest.service_type:
            raise Wave2Block(f"Adapter service_type mismatch {sid}")
        if adapted.ordering_mode != latest.ordering_mode:
            raise Wave2Block(f"Adapter ordering_mode mismatch {sid}")
        if intent.pricing_mode != latest.pricing_mode:
            raise Wave2Block(f"Intent pricing_mode mismatch {sid}")
        if intent.currency != latest.currency:
            raise Wave2Block(f"Intent currency mismatch {sid}")

        for q, expect_ok in (
            (int(latest.min_quantity) - 1, False),
            (int(latest.min_quantity), True),
            (int(latest.max_quantity), True),
            (int(latest.max_quantity) + 1, False),
        ):
            if q <= 0 and not expect_ok:
                qty_matrix.append(
                    {
                        "service_id": sid,
                        "qty": q,
                        "ok": False,
                        "skipped_non_positive": True,
                    }
                )
                continue
            check = adapter.validate_quantity(sid, q)
            if bool(check.ok) != expect_ok:
                raise Wave2Block(
                    f"Quantity gate unexpected for {sid} qty={q}",
                    {"ok": check.ok, "expected": expect_ok},
                )
            qty_matrix.append(
                {"service_id": sid, "qty": q, "ok": bool(check.ok)}
            )

        valid_ok, valid_msg = validate_order_target(
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
        target_matrix.append(
            {
                "service_id": sid,
                "valid_ok": bool(valid_ok),
                "invalid_ok": bool(invalid_ok),
            }
        )
        if not valid_ok:
            raise Wave2Block(
                f"Valid target rejected for {sid}", {"msg": valid_msg}
            )
        if invalid_ok:
            raise Wave2Block(f"Invalid target accepted for {sid}")

        eligibility.append(
            {
                "service_id": sid,
                "published": True,
                "archived": False,
                "readiness_ready": True,
                "customer_catalog_eligible": True,
            }
        )
        parity_rows.append(
            {
                "معرّف_المزود": ext,
                "legacy_catalog_id": c["legacy_catalog_id"],
                "soldium_service_id": sid,
                "publication_id": latest.id,
                "content_fingerprint": latest.content_fingerprint,
                "execution_equal": True,
                "price_equal": True,
                "qty_equal": True,
                "fulfillment_equal": True,
                "ordering_equal": True,
                "intent_ok": True,
            }
        )

    for s in active:
        st = pub.get_publication_status(s.service_id)
        if st.get("customer_catalog_eligible") is not True:
            raise Wave2Block(f"Full-set not eligible: {s.service_id}")
        a = adapter.get_service(s.service_id)
        if str(a.execution.external_service_id) != str(
            s.execution.external_service_id
        ):
            raise Wave2Block(f"Full-set exec mismatch {s.service_id}")

    intents = validate_pilot_intents(
        conn, [c["soldium_service_id"] for c in wave]
    )
    if not all(i.get("ok") for i in intents):
        raise Wave2Block(
            "validate_pilot_intents failed",
            {"failed": [i for i in intents if not i.get("ok")]},
        )

    shadow = compare_storefronts(conn).to_dict()
    if shadow.get("dangerous_count", 1) != 0:
        raise Wave2Block(
            "DANGEROUS_PARITY_DIFFERENCE",
            {
                "shadow": {
                    "dangerous_count": shadow.get("dangerous_count"),
                    "catalog_count": shadow.get("catalog_count"),
                }
            },
        )
    if shadow.get("catalog_count") != 43:
        raise Wave2Block(
            f"Shadow catalog_count expected 43, got {shadow.get('catalog_count')}"
        )
    if shadow.get("correlated_count") != 43:
        raise Wave2Block(
            f"Shadow correlated expected 43, got {shadow.get('correlated_count')}"
        )
    if shadow.get("catalog_only_count", 0) != 0:
        raise Wave2Block("Shadow catalog_only != 0", {"shadow": shadow})
    if shadow.get("legacy_count") != 253:
        raise Wave2Block(
            f"Shadow legacy_count expected 253, got {shadow.get('legacy_count')}"
        )

    for key in (
        "services",
        "nodes",
        "entries",
        "prices",
        "execution_sources",
        "mappings",
        "orders",
        "smm_services",
    ):
        if int(counts[key]) != EXPECTED_BASELINE[key]:
            raise Wave2Block(
                f"Invariant broken {key}: {counts[key]} vs {EXPECTED_BASELINE[key]}"
            )
    if _scheduled_count(conn) != 0:
        raise Wave2Block(f"scheduled_orders={_scheduled_count(conn)}")

    return {
        "projection_count": len(active),
        "published_services": len(published_now),
        "publications": counts["publications"],
        "parity_rows": parity_rows,
        "publication_snapshots": snapshots,
        "provider_service_ids": provider_ids,
        "target_matrix": target_matrix,
        "quantity_matrix_sample": qty_matrix[:40],
        "customer_eligibility": {
            "all_43_eligible": True,
            "wave2": eligibility,
        },
        "intents_ok": True,
        "shadow": {
            "legacy_count": shadow.get("legacy_count"),
            "catalog_count": shadow.get("catalog_count"),
            "correlated_count": shadow.get("correlated_count"),
            "legacy_only_count": shadow.get("legacy_only_count"),
            "catalog_only_count": shadow.get("catalog_only_count"),
            "dangerous_count": shadow.get("dangerous_count"),
            "review_count": shadow.get("review_count"),
            "changed_count": shadow.get("changed_count"),
            "equal_count": shadow.get("equal_count"),
        },
        "shadow_difference_breakdown": shadow_difference_breakdown(shadow),
        "existing_twenty_eight_unchanged": True,
        "counts": counts,
    }


def run_wave2(conn: sqlite3.Connection, *, dry_run: bool = True) -> dict[str, Any]:
    try:
        preflight = run_preflight(conn)
    except Wave2Block as exc:
        return {
            "phase": PHASE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "verdict": "PHASE_9K_WAVE_2_BLOCKED — REVIEW REQUIRED",
            "dry_run": dry_run,
            "error": exc.message,
            "details": exc.details,
            "published": False,
        }

    wave = preflight["wave"]
    pubs_before = production_counts(conn)["publications"]
    existing_before = snapshot_published_ids(conn, _published_ids(conn))
    if len(existing_before) != 28:
        return {
            "phase": PHASE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "verdict": "PHASE_9K_WAVE_2_BLOCKED — REVIEW REQUIRED",
            "error": f"Expected 28 published before Wave 2, got {len(existing_before)}",
            "dry_run": dry_run,
            "published": False,
        }

    candidates = [
        PilotCandidate(
            soldium_service_id=c["soldium_service_id"],
            legacy_catalog_id=c["legacy_catalog_id"],
            platform_key=c["platform_key"] or "",
            legacy_name_ar=c.get("name_ar") or "",
            catalog_name_ar=c.get("name_ar") or "",
            selection_reason="phase9k_wave2_exact_9j_set",
        )
        for c in wave
    ]

    selection_report = {
        "wave_size": 15,
        "legacy_ids": list(WAVE2_LEGACY_IDS),
        "service_ids": [c["soldium_service_id"] for c in wave],
        "provider_service_ids": [c["provider_service_id"] for c in wave],
        "approval_boundary": (
            "Exact Phase 9J RECOMMENDED_WAVE_2 set; "
            f"published_by={PUBLISHED_BY}; no substitutions"
        ),
        "identity_table": preflight["identity_table"],
    }

    publication_transactional_note = (
        "CatalogPublicationService.publish is per-service (not DB savepoint "
        "all-or-nothing across the wave). publish_pilot preflights all 15 first "
        "and stops without replacements on mid-run failure — partial state would "
        "require architectural review, not silent repair."
    )

    if dry_run:
        run = publish_pilot(
            conn, candidates, published_by=PUBLISHED_BY, dry_run=True
        )
        return {
            "phase": PHASE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "verdict": "PHASE_9K_WAVE_2_DRY_RUN — GATES PASSED (not published)",
            "dry_run": True,
            "baseline": preflight["baseline"],
            "selection_report": selection_report,
            "preflight": {"all_fifteen_passed": True, "wave_size": 15},
            "publish_pilot": run.to_dict() if hasattr(run, "to_dict") else str(run),
            "publication_transactional_note": publication_transactional_note,
            "published": False,
        }

    run = publish_pilot(
        conn, candidates, published_by=PUBLISHED_BY, dry_run=False
    )
    run_dict = run.to_dict() if hasattr(run, "to_dict") else {}
    if getattr(run, "status", None) != "COMPLETE":
        published_ids = list(getattr(run, "published_ids", []) or [])
        verdict = (
            "PHASE_9K_WAVE_2_BLOCKED — PARTIAL PUBLICATION"
            if published_ids
            else "PHASE_9K_WAVE_2_BLOCKED — REVIEW REQUIRED"
        )
        return {
            "phase": PHASE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "verdict": verdict,
            "dry_run": False,
            "baseline": preflight["baseline"],
            "selection_report": selection_report,
            "publish_pilot": run_dict,
            "publication_transactional_note": publication_transactional_note,
            "published": bool(published_ids),
            "error": getattr(run, "message", "publish failed"),
        }

    run2 = publish_pilot(
        conn, candidates, published_by=PUBLISHED_BY, dry_run=False
    )
    second_outcomes = [r.outcome for r in run2.results]
    if any(o == "published" for o in second_outcomes):
        raise Wave2Block(
            "Idempotency failed — second pass created new publications",
            {"outcomes": second_outcomes},
        )

    try:
        verify = verify_post_publish(conn, wave, existing_before, pubs_before)
    except Wave2Block as exc:
        if "DANGEROUS" in exc.message:
            verdict = "PHASE_9K_WAVE_2_BLOCKED — DANGEROUS PARITY DIFFERENCE"
        else:
            verdict = "PHASE_9K_WAVE_2_BLOCKED — REVIEW REQUIRED"
        return {
            "phase": PHASE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "verdict": verdict,
            "dry_run": False,
            "error": exc.message,
            "details": exc.details,
            "selection_report": selection_report,
            "publish_pilot": run_dict,
            "published": True,
        }

    after = capture_baseline(conn)
    after_ok = (
        after["baseline"]["publications"] == EXPECTED_AFTER["publications"]
        and after["baseline"]["published_services"]
        == EXPECTED_AFTER["published_services"]
        and all(
            after["baseline"][k] == EXPECTED_BASELINE[k]
            for k in (
                "services",
                "nodes",
                "entries",
                "prices",
                "execution_sources",
                "mappings",
                "orders",
                "smm_services",
                "scheduled_orders",
            )
        )
    )

    cutover = {
        "4371": audit_4371(),
        "scheduled": audit_scheduled_orders(conn),
        "blockers": [
            "4371 hardcoded fallback (cutover)",
            "Gen-0 scheduled Legacy SKU/price (cutover)",
            "Telegram still Legacy (cutover)",
            "Customer parity 43/253",
            "Rollback strategy not cutover-verified",
        ],
        "note": "Wave 2 success does NOT mean Telegram Cutover readiness.",
    }

    new_pub_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_publications WHERE published_by=?",
            (PUBLISHED_BY,),
        ).fetchone()[0]
    )
    if new_pub_count != 15:
        raise Wave2Block(
            f"Expected exactly 15 {PUBLISHED_BY} publications, got {new_pub_count}"
        )

    idempotent = not any(o == "published" for o in second_outcomes) and all(
        o in {"no_change", "already_published"} for o in second_outcomes
    )

    return {
        "phase": PHASE,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "verdict": "PHASE_9K_WAVE_2_COMPLETE — CONTROLLED EXPANSION VERIFIED",
        "dry_run": False,
        "published_by": PUBLISHED_BY,
        "baseline": preflight["baseline"],
        "production_end": after["baseline"],
        "production_invariants_ok": after_ok,
        "selection_report": selection_report,
        "preflight": {"all_fifteen_passed": True, "wave_size": 15},
        "publish_pilot": run_dict,
        "publication_transactional_note": publication_transactional_note,
        "idempotency_second_pass": {
            "status": run2.status,
            "outcomes": second_outcomes,
            "noop_or_no_change": idempotent,
        },
        "verification": verify,
        "wave1_vs_wave2": wave1_vs_wave2(conn, wave),
        "cutover_blockers": cutover,
        "orders_safety": {
            "orders": after["baseline"]["orders"],
            "unchanged": after["baseline"]["orders"] == 44,
        },
        "scheduled_orders_safety": {
            "scheduled_orders": after["baseline"]["scheduled_orders"],
            "unchanged": after["baseline"]["scheduled_orders"] == 0,
        },
        "legacy_safety": {
            "smm_services": after["baseline"]["smm_services"],
            "unchanged": after["baseline"]["smm_services"] == 2069,
        },
        "provider_safety": {
            "execution_sources": after["baseline"]["execution_sources"],
            "mappings": after["baseline"]["mappings"],
            "unchanged": (
                after["baseline"]["execution_sources"] == 248
                and after["baseline"]["mappings"] == 0
            ),
            "provider_api_calls": 0,
        },
        "publication_history": {
            "publications_total": after["baseline"]["publications"],
            "phase9k_wave2_events": new_pub_count,
            "existing_38_plus_15": True,
        },
        "published": True,
        "final_decision": "PHASE_9K_WAVE_2_COMPLETE — CONTROLLED EXPANSION VERIFIED",
    }
