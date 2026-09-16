# -*- coding: utf-8 -*-
"""Phase 9H Wave 1 — controlled expansion from approved 15-candidate pool."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
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
from catalog_core.phase9g_business_decision_pack import _published_ids
from catalog_core.pilot_parity import _sample_target_url
from catalog_core.phase9e_stage_b import _pilot_sample_target
from catalog_core.phase9gb_business_decisions_apply import FUTURE_PILOT_LEGACY_IDS
from catalog_core.publication import CatalogPublicationService
from catalog_core.publication_pilot import PilotCandidate, publish_pilot, validate_pilot_intents
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.storefront_adapter import StorefrontAdapter, StorefrontAdapterError
from catalog_core.storefront_projection import PublishedStorefrontProjection
from catalog_core.storefront_shadow import compare_storefronts
from catalog_core.target_validation import validate_order_target

PHASE = "9H"
PUBLISHED_BY = "phase9h_wave1"
WAVE1_CANDIDATE_LEGACY_IDS: tuple[str, ...] = FUTURE_PILOT_LEGACY_IDS

EXPECTED_BASELINE = {
    "orders": 44,
    "smm_services": 2069,
    "services": 253,
    "nodes": 59,
    "entries": 312,
    "prices": 253,
    "execution_sources": 248,
    "mappings": 0,
    "publications": 23,
    "published_services": 13,
}


class Wave1Block(Exception):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


def map_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for legacy_id in WAVE1_CANDIDATE_LEGACY_IDS:
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
            raise Wave1Block(
                f"Legacy {legacy_id} mapping ambiguous or missing (rows={len(rows)})",
                {"legacy_catalog_id": legacy_id, "row_count": len(rows)},
            )
        row = rows[0]
        mapped.append(
            {
                "legacy_catalog_id": legacy_id,
                "soldium_service_id": str(row["soldium_service_id"]),
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
    if len(set(svc_ids)) != len(WAVE1_CANDIDATE_LEGACY_IDS):
        raise Wave1Block("Duplicate soldium_service_id in candidate mapping", {"ids": svc_ids})
    return mapped


def _classify_row(r) -> str:
    if not r.has_execution_source:
        return "MISSING_EXECUTION"
    if r.pricing_mode == "per_unit" or (
        r.platform_key == "subscriptions"
        and r.section_key in {"iptv_panel", "iptv_wc2026"}
    ):
        return "IPTV"
    if r.max_quantity == SENTINEL_MAX or "max_qty_sentinel" in (
        r.bridge_review_codes or []
    ):
        return "SENTINEL"
    if r.service_type_confidence == CONFIDENCE_PROBABLE:
        return "PROBABLE"
    if r.service_type_confidence in {CONFIDENCE_SPECIAL, CONFIDENCE_UNKNOWN}:
        return "SPECIAL"
    if (
        r.service_type_confidence == CONFIDENCE_CONFIRMED
        and r.service_type != "other"
        and not r.target_special
        and r.ordering_confidence != CONFIDENCE_UNKNOWN
    ):
        return "STANDARD"
    return "REVIEW"


def gate_candidate(conn: sqlite3.Connection, mapping: dict[str, Any], *, phase9d_row) -> dict[str, Any]:
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
        elif not isinstance(ext, str) and not isinstance(ext, (int, float)):
            pass
        # Opaque TEXT — never require numeric
        if ext is not None and not isinstance(str(ext), str):
            errors.append("external_service_id_not_text")
    if classification != "STANDARD":
        errors.append(f"classification_not_standard:{classification}")
    if svc.ordering_mode not in {"quantity_based", "package_based"}:
        errors.append(f"ordering_mode:{svc.ordering_mode}")

    score = 0
    if classification == "STANDARD":
        score += 40
    if ready.ready:
        score += 20
    if source and price:
        score += 15
    if svc.fulfillment_mode == "auto":
        score += 10
    if price and price.pricing_mode == "per_1000":
        score += 10
    if int(svc.max_quantity) != SENTINEL_MAX and int(svc.max_quantity) > 0:
        score += 5

    passed = not errors
    return {
        "legacy_catalog_id": legacy_id,
        "soldium_service_id": sid,
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
        "target_platform_key": svc.target_platform_key,
        "target_section_key": svc.target_section_key,
        "target_subsection_key": svc.target_subsection_key,
        "target_link_type": svc.target_link_type,
        "amount_millimes": int(price.amount_millimes) if price else None,
        "currency": price.currency if price else None,
        "pricing_mode": price.pricing_mode if price else None,
        "provider_slug": source.provider_slug if source else None,
        "provider_account_key": source.provider_account_key if source else None,
        "external_service_id": (
            str(source.external_service_id) if source else None
        ),
        "provider_service_id": (
            str(source.external_service_id) if source else None
        ),
        "readiness_ready": bool(ready.ready),
        "publication_status": pub_status.get("publication_status"),
        "classification": classification,
        "score": score,
        "gate_passed": passed,
        "gate_errors": errors,
        "inclusion_reason": (
            "STANDARD + readiness + finite qty + exec + price + target"
            if passed
            else None
        ),
        "exclusion_reason": "; ".join(errors) if errors else None,
        "location_path": list(svc.location_path or []),
    }


def _fail(mapping: dict[str, Any], errors: list[str], classification: str) -> dict[str, Any]:
    return {
        **mapping,
        "gate_passed": False,
        "gate_errors": errors,
        "classification": classification,
        "exclusion_reason": "; ".join(errors),
        "inclusion_reason": None,
        "score": 0,
        "provider_service_id": None,
        "external_service_id": None,
    }


def run_candidate_gate(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = production_counts(conn)
    published = _published_ids(conn)
    if len(published) != EXPECTED_BASELINE["published_services"]:
        raise Wave1Block(
            f"published_services expected {EXPECTED_BASELINE['published_services']}, "
            f"got {len(published)}"
        )
    for key, val in EXPECTED_BASELINE.items():
        if key == "published_services":
            continue
        if int(counts.get(key, -1)) != val:
            raise Wave1Block(
                f"Baseline mismatch {key}: got {counts.get(key)} expected {val}",
                {"counts": counts},
            )

    mapped = map_candidates(conn)
    all_rows = {
        r.soldium_service_id: r
        for r in load_phase9d_rows(conn)
    }
    # Include published rows for classification lookup — candidates should be unpublished
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

    selected = [g for g in gated if g["gate_passed"]]
    excluded = [g for g in gated if not g["gate_passed"]]

    # Prefer all 15 if all pass; else subset. Hard floor: 10.
    wave = selected  # all that passed; no substitution from Ready pool

    return {
        "baseline": {**counts, "published_services": len(published)},
        "candidate_pool_size": len(WAVE1_CANDIDATE_LEGACY_IDS),
        "candidates": gated,
        "selected_wave": wave,
        "excluded": excluded,
        "wave_size": len(wave),
        "all_fifteen_passed": len(wave) == len(WAVE1_CANDIDATE_LEGACY_IDS),
        "sufficient_for_publication": len(wave) >= 10,
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


def snapshot_published_thirteen(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ids = _published_ids(conn)
    if len(ids) != 13:
        raise Wave1Block(f"Expected 13 published before Wave 1, got {len(ids)}")
    return snapshot_published_ids(conn, ids)

def verify_post_publish(
    conn: sqlite3.Connection,
    wave: list[dict[str, Any]],
    thirteen_before: list[dict[str, Any]],
    pubs_before: int,
) -> dict[str, Any]:
    n = len(wave)
    pub = CatalogPublicationService(conn)
    proj = PublishedStorefrontProjection(conn)
    adapter = StorefrontAdapter(conn)

    active = proj.list_services()
    if len(active) != 13 + n:
        raise Wave1Block(
            f"projection expected {13 + n}, got {len(active)}"
        )
    wave_ids = {c["soldium_service_id"] for c in wave}
    active_ids = {s.service_id for s in active}
    if not wave_ids.issubset(active_ids):
        raise Wave1Block(
            "Wave missing from projection",
            {"missing": sorted(wave_ids - active_ids)},
        )

    thirteen_ids = {x["service_id"] for x in thirteen_before}
    thirteen_after = snapshot_published_ids(conn, thirteen_ids)
    before_by_id = {x["service_id"]: x for x in thirteen_before}
    for row in thirteen_after:
        b = before_by_id[row["service_id"]]
        if b["content_fingerprint"] != row["content_fingerprint"]:
            raise Wave1Block(
                f"Existing published fingerprint changed: {row['service_id']}"
            )
        if b["publication_id"] != row["publication_id"]:
            raise Wave1Block(
                f"Existing publication id changed: {row['service_id']}"
            )

    counts = production_counts(conn)
    if counts["publications"] != pubs_before + n:
        # idempotent re-run may yield no_change without new rows — check carefully
        if counts["publications"] < pubs_before + n:
            # allow if some were no_change on second run only when already published
            pass
        if counts["publications"] != pubs_before + n:
            # Count newly published outcomes
            new_pubs = counts["publications"] - pubs_before
            if new_pubs != n:
                raise Wave1Block(
                    f"publications expected {pubs_before + n}, got {counts['publications']}"
                )

    parity_rows = []
    target_matrix = []
    qty_matrix = []
    provider_ids = []

    for c in wave:
        sid = c["soldium_service_id"]
        latest = pub.get_latest_publish(sid)
        status = pub.get_publication_status(sid)
        if latest is None or status.get("publication_status") != "published":
            raise Wave1Block(f"Not published: {sid}")
        if status.get("customer_catalog_eligible") is not True:
            raise Wave1Block(f"Not eligible: {sid}")

        projected = proj.get_service(sid)
        adapted = adapter.get_service(sid)

        ext = str(latest.external_service_id)
        provider_ids.append(
            {
                "legacy_catalog_id": c["legacy_catalog_id"],
                "soldium_service_id": sid,
                "provider_service_id": ext,
                "provider_slug": latest.provider_slug,
                "provider_account_key": latest.provider_account_key,
            }
        )

        pub_exec = (
            latest.provider_slug,
            latest.provider_account_key,
            ext,
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
        if not (pub_exec == proj_exec == adapt_exec):
            raise Wave1Block(
                f"Execution mismatch {sid}",
                {"pub": pub_exec, "proj": proj_exec, "adapter": adapt_exec},
            )

        if not (
            latest.fulfillment_mode
            == projected.fulfillment_mode
            == adapted.fulfillment_mode
        ):
            raise Wave1Block(f"Fulfillment mismatch {sid}")

        if not (
            latest.amount_millimes
            == projected.amount_millimes
            == adapted.price.amount_millimes
        ):
            raise Wave1Block(f"Price mismatch {sid}")

        if not (
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
        ):
            raise Wave1Block(f"Quantity/ordering mismatch {sid}")

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
            raise Wave1Block(f"Intent execution mismatch {sid}")
        if intent.content_fingerprint != latest.content_fingerprint:
            raise Wave1Block(f"Intent fingerprint mismatch {sid}")
        if intent.quoted_amount_millimes != quote.quoted_amount_millimes:
            raise Wave1Block(f"Intent quote mismatch {sid}")
        if quote.unit_amount_millimes != latest.amount_millimes:
            raise Wave1Block(f"Unit price mismatch {sid}")
        if intent.fulfillment_mode != latest.fulfillment_mode:
            raise Wave1Block(f"Intent fulfillment mismatch {sid}")

        # Quantity boundaries via adapter
        for q, expect_ok in (
            (int(latest.min_quantity) - 1, False),
            (int(latest.min_quantity), True),
            (int(latest.max_quantity), True),
            (int(latest.max_quantity) + 1, False),
        ):
            if q <= 0 and not expect_ok:
                qty_matrix.append(
                    {"service_id": sid, "qty": q, "ok": False, "skipped_non_positive": True}
                )
                continue
            check = adapter.validate_quantity(sid, q)
            if bool(check.ok) != expect_ok:
                raise Wave1Block(
                    f"Quantity gate unexpected for {sid} qty={q}",
                    {"ok": check.ok, "expected": expect_ok},
                )
            qty_matrix.append({"service_id": sid, "qty": q, "ok": bool(check.ok)})

        # Target valid/invalid
        valid_ok, valid_msg = validate_order_target(
            sample,
            platform_key=latest.target_platform_key or "",
            section_key=latest.target_section_key,
            subsection_key=latest.target_subsection_key,
        )
        invalid_ok, _invalid_msg = validate_order_target(
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
            raise Wave1Block(f"Valid target rejected for {sid}", {"msg": valid_msg})
        if invalid_ok:
            raise Wave1Block(f"Invalid target accepted for {sid}")

        parity_rows.append(
            {
                "legacy_catalog_id": c["legacy_catalog_id"],
                "soldium_service_id": sid,
                "provider_service_id": ext,
                "publication_id": latest.id,
                "content_fingerprint": latest.content_fingerprint,
                "execution_equal": True,
                "price_equal": True,
                "qty_equal": True,
                "fulfillment_equal": True,
                "intent_ok": True,
            }
        )

    intents = validate_pilot_intents(conn, [c["soldium_service_id"] for c in wave])
    if not all(i.get("ok") for i in intents):
        raise Wave1Block(
            "validate_pilot_intents failed",
            {"failed": [i for i in intents if not i.get("ok")]},
        )

    shadow = compare_storefronts(conn).to_dict()
    if shadow.get("dangerous_count", 1) != 0:
        raise Wave1Block("Shadow dangerous > 0", {"shadow": shadow})
    if shadow.get("catalog_count") != 13 + n:
        raise Wave1Block(
            f"Shadow catalog_count expected {13 + n}, got {shadow.get('catalog_count')}"
        )
    if shadow.get("catalog_only_count", 0) != 0:
        raise Wave1Block("Shadow catalog_only != 0", {"shadow": shadow})
    if shadow.get("legacy_count") != 253:
        raise Wave1Block(
            f"Shadow legacy_count expected 253, got {shadow.get('legacy_count')}"
        )

    # Full published set parity sample: every projected service has adapter
    for s in active:
        a = adapter.get_service(s.service_id)
        if str(a.execution.external_service_id) != str(
            s.execution.external_service_id
        ):
            raise Wave1Block(f"Full-set exec mismatch {s.service_id}")

    # Production invariants (non-publication)
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
            raise Wave1Block(
                f"Invariant broken {key}: {counts[key]} vs {EXPECTED_BASELINE[key]}"
            )
    scheduled = int(
        conn.execute("SELECT COUNT(*) FROM scheduled_orders").fetchone()[0]
    )
    if scheduled != 0:
        raise Wave1Block(f"scheduled_orders={scheduled}")

    return {
        "projection_count": len(active),
        "publications": counts["publications"],
        "parity_rows": parity_rows,
        "provider_service_ids": provider_ids,
        "target_matrix": target_matrix,
        "quantity_matrix_sample": qty_matrix[:20],
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
        "existing_thirteen_unchanged": True,
        "counts": counts,
    }


def run_wave1(conn: sqlite3.Connection, *, dry_run: bool = True) -> dict[str, Any]:
    gate = run_candidate_gate(conn)
    if not gate["sufficient_for_publication"]:
        return {
            "phase": PHASE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "verdict": "PHASE 9H WAVE 1 BLOCKED — CANDIDATE POOL INSUFFICIENT",
            "dry_run": dry_run,
            "gate": gate,
            "published": False,
        }

    wave = gate["selected_wave"]
    pubs_before = production_counts(conn)["publications"]
    thirteen_before = snapshot_published_thirteen(conn)

    candidates = [
        PilotCandidate(
            soldium_service_id=c["soldium_service_id"],
            legacy_catalog_id=c["legacy_catalog_id"],
            platform_key=c["platform_key"] or "",
            legacy_name_ar=c.get("name_ar") or "",
            catalog_name_ar=c.get("name_ar") or "",
            selection_reason="phase9h_wave1_gate_STANDARD",
        )
        for c in wave
    ]

    selection_report = {
        "wave_size": len(wave),
        "legacy_ids": [c["legacy_catalog_id"] for c in wave],
        "service_ids": [c["soldium_service_id"] for c in wave],
        "provider_service_ids": [c["provider_service_id"] for c in wave],
        "excluded_legacy_ids": [c["legacy_catalog_id"] for c in gate["excluded"]],
        "approval_boundary": (
            "User-approved FUTURE_PILOT_LEGACY_IDS pool; all gated STANDARD "
            "candidates published as Wave 1 under published_by=phase9h_wave1"
        ),
    }

    if dry_run:
        run = publish_pilot(
            conn, candidates, published_by=PUBLISHED_BY, dry_run=True
        )
        return {
            "phase": PHASE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "verdict": "PHASE 9H WAVE 1 DRY-RUN — GATES PASSED (not published)",
            "dry_run": True,
            "gate": {
                "candidate_pool_size": gate["candidate_pool_size"],
                "wave_size": gate["wave_size"],
                "all_fifteen_passed": gate["all_fifteen_passed"],
                "excluded": gate["excluded"],
                "selected_wave": gate["selected_wave"],
            },
            "selection_report": selection_report,
            "publish_pilot": run.to_dict() if hasattr(run, "to_dict") else str(run),
            "published": False,
        }

    run = publish_pilot(
        conn, candidates, published_by=PUBLISHED_BY, dry_run=False
    )
    run_dict = run.to_dict() if hasattr(run, "to_dict") else {}
    if getattr(run, "status", None) != "COMPLETE":
        return {
            "phase": PHASE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "verdict": "PHASE 9H WAVE 1 BLOCKED — REVIEW REQUIRED",
            "dry_run": False,
            "gate": gate,
            "selection_report": selection_report,
            "publish_pilot": run_dict,
            "published": False,
            "error": getattr(run, "message", "publish failed"),
        }

    # Idempotency: second publish must be no_change
    run2 = publish_pilot(
        conn, candidates, published_by=PUBLISHED_BY, dry_run=False
    )
    second_outcomes = [r.outcome for r in run2.results]
    if any(o == "published" for o in second_outcomes):
        raise Wave1Block(
            "Idempotency failed — second pass created new publications",
            {"outcomes": second_outcomes},
        )

    verify = verify_post_publish(conn, wave, thirteen_before, pubs_before)

    return {
        "phase": PHASE,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "verdict": "PHASE 9H WAVE 1 COMPLETE — CONTROLLED EXPANSION VERIFIED",
        "dry_run": False,
        "published_by": PUBLISHED_BY,
        "gate": {
            "candidate_pool_size": gate["candidate_pool_size"],
            "wave_size": gate["wave_size"],
            "all_fifteen_passed": gate["all_fifteen_passed"],
            "candidates": gate["candidates"],
            "excluded": gate["excluded"],
            "selected_wave": gate["selected_wave"],
        },
        "selection_report": selection_report,
        "publish_pilot": run_dict,
        "idempotency_second_pass": {
            "status": run2.status,
            "outcomes": second_outcomes,
            "noop_or_no_change": all(
                o in {"no_change", "published"} for o in second_outcomes
            )
            and not any(o == "published" for o in second_outcomes),
        },
        "verification": verify,
        "published": True,
        "execution_source_replacement_check": (
            "9G.1 workflow available; no replacements performed in Wave 1"
        ),
    }
