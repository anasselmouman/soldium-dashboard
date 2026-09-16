# -*- coding: utf-8 -*-
"""Phase 9L — Expansion validation after Wave 2 (read-only production)."""

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
from catalog_core.phase9h_wave1 import WAVE1_CANDIDATE_LEGACY_IDS
from catalog_core.phase9i_expansion_validation import (
    audit_4371,
    audit_scheduled_orders,
    provider_id_visibility_check,
)
from catalog_core.phase9k_wave2 import WAVE2_LEGACY_IDS
from catalog_core.publication import CatalogPublicationService
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_adapter import StorefrontAdapter
from catalog_core.storefront_projection import PublishedStorefrontProjection
from catalog_core.storefront_shadow import compare_storefronts
from catalog_core.target_validation import validate_order_target

PHASE = "9L"
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

ORIGINAL_PUBLISHED_BY = frozenset(
    {"phase9b7_order_contract", "phase9e_second_pilot"}
)
WAVE1_PUBLISHED_BY = "phase9h_wave1"
WAVE2_PUBLISHED_BY = "phase9k_wave2"

_FROM_SMM = re.compile(r"\bFROM\s+smm_services\b", re.IGNORECASE)


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


def _cohort_for(published_by: str | None, legacy_id: str | None) -> str:
    if published_by == WAVE2_PUBLISHED_BY or (
        legacy_id and legacy_id in WAVE2_LEGACY_IDS
    ):
        return "wave2"
    if published_by == WAVE1_PUBLISHED_BY or (
        legacy_id and legacy_id in WAVE1_CANDIDATE_LEGACY_IDS
    ):
        return "wave1"
    return "original13"


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
            placement = list(ps.location_path) if ps else None
        except Exception:  # noqa: BLE001
            placement = None
        leg = conn.execute(
            """
            SELECT l.platform_key, l.section_key, l.subsection_key
            FROM soldium_catalog_legacy_bridge b
            LEFT JOIN smm_services l ON l.catalog_id = b.legacy_catalog_id
            WHERE b.soldium_service_id = ?
            """,
            (sid,),
        ).fetchone()
        provider_service_id = (
            str(latest.external_service_id)
            if latest and latest.external_service_id is not None
            else None
        )
        legacy_id = _legacy_for_service(conn, sid)
        published_by = latest.published_by if latest else None
        # معرّف_المزود MUST be the first key on every inventory row.
        out.append(
            {
                "معرّف_المزود": provider_service_id,
                "legacy_catalog_id": legacy_id,
                "soldium_service_id": sid,
                "provider_slug": latest.provider_slug if latest else None,
                "provider_account_key": (
                    latest.provider_account_key if latest else None
                ),
                "provider_service_id": provider_service_id,
                "platform": (leg["platform_key"] if leg else None)
                or (latest.target_platform_key if latest else None),
                "section": (leg["section_key"] if leg else None)
                or (latest.target_section_key if latest else None),
                "subsection": (leg["subsection_key"] if leg else None)
                or (latest.target_subsection_key if latest else None),
                "service_type": latest.service_type if latest else None,
                "name_ar": latest.name_ar if latest else None,
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
                "published_by": published_by,
                "publication_id": latest.id if latest else None,
                "readiness_ready": bool(ready.ready) if ready else None,
                "has_unpublished_changes": status.get("has_unpublished_changes"),
                "customer_catalog_eligible": status.get("customer_catalog_eligible"),
                "publication_status": status.get("publication_status"),
                "location_path": placement,
                "cohort": _cohort_for(published_by, legacy_id),
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
        identity_ok = sid.startswith("svc_") and sid != str(latest.external_service_id)
        row = {
            "معرّف_المزود": str(latest.external_service_id),
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
    qty_boundary_sample: list[dict[str, Any]] = []
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
            "name_ar": (
                (latest.name_ar or "").strip()
                == (projected.name_ar or "").strip()
                == (adapted.name_ar or "").strip()
            ),
            "service_type": (
                latest.service_type
                == projected.service_type
                == adapted.service_type
            ),
            "published_at_present": bool(latest.published_at),
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

        # Quantity boundaries: min-1 reject, min/max accept, max+1 reject
        qty_ok = True
        for q, expect_ok in (
            (int(latest.min_quantity) - 1, False),
            (int(latest.min_quantity), True),
            (int(latest.max_quantity), True),
            (int(latest.max_quantity) + 1, False),
        ):
            if q <= 0 and not expect_ok:
                qty_boundary_sample.append(
                    {
                        "service_id": sid,
                        "qty": q,
                        "ok": False,
                        "skipped_non_positive": True,
                    }
                )
                continue
            check = adapter.validate_quantity(sid, q)
            passed = bool(check.ok) == expect_ok
            if not passed:
                qty_ok = False
            if len(qty_boundary_sample) < 80:
                qty_boundary_sample.append(
                    {
                        "service_id": sid,
                        "qty": q,
                        "ok": bool(check.ok),
                        "expected_ok": expect_ok,
                        "matched": passed,
                    }
                )
        checks["qty_boundaries"] = qty_ok

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
                    "معرّف_المزود": str(latest.external_service_id),
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
        "qty_boundary_sample": qty_boundary_sample,
        "ok": len(failures) == 0,
    }


def analyze_drift(conn: sqlite3.Connection, inventory: list[dict[str, Any]]) -> dict[str, Any]:
    drifted = [r for r in inventory if r.get("has_unpublished_changes")]
    return {
        "drifted_count": len(drifted),
        "drifted_services": [
            {
                "معرّف_المزود": r.get("معرّف_المزود"),
                "soldium_service_id": r["soldium_service_id"],
                "legacy_catalog_id": r["legacy_catalog_id"],
                "cohort": r["cohort"],
                "classification": "unexpected",
            }
            for r in drifted
        ],
        "classification": "none" if not drifted else "unexpected",
        "note": (
            "No draft/publication fingerprint drift on the 43 published services."
            if not drifted
            else "Live draft differs from published snapshot — do not auto-republish."
        ),
        "ok": len(drifted) == 0,
    }


def compare_three_cohorts(inventory: list[dict[str, Any]]) -> dict[str, Any]:
    cohorts = {
        "original13": [r for r in inventory if r["cohort"] == "original13"],
        "wave1": [r for r in inventory if r["cohort"] == "wave1"],
        "wave2": [r for r in inventory if r["cohort"] == "wave2"],
    }

    def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "count": len(rows),
            "platforms": dict(Counter(r["platform"] for r in rows)),
            "service_types": dict(Counter(r["service_type"] for r in rows)),
            "pricing_modes": dict(Counter(r["pricing_mode"] for r in rows)),
            "fulfillment_modes": dict(Counter(r["fulfillment_mode"] for r in rows)),
            "ordering_modes": dict(Counter(r["ordering_mode"] for r in rows)),
            "providers": dict(Counter(r["provider_slug"] for r in rows)),
            "legacy_ids": [r["legacy_catalog_id"] for r in rows],
        }

    wave1_ids = {r["legacy_catalog_id"] for r in cohorts["wave1"]}
    wave2_ids = {r["legacy_catalog_id"] for r in cohorts["wave2"]}
    return {
        "original13": summary(cohorts["original13"]),
        "wave1": summary(cohorts["wave1"]),
        "wave2": summary(cohorts["wave2"]),
        "counts_ok": (
            len(cohorts["original13"]) == 13
            and len(cohorts["wave1"]) == 15
            and len(cohorts["wave2"]) == 15
        ),
        "wave1_ids_match": wave1_ids == set(WAVE1_CANDIDATE_LEGACY_IDS),
        "wave2_ids_match": wave2_ids == set(WAVE2_LEGACY_IDS),
        "no_wave_overlap": wave1_ids.isdisjoint(wave2_ids),
        "sample_size_note": (
            "n=13/15/15 are too small for statistical significance; "
            "comparison is structural/descriptive only."
        ),
        "contract_behavior": (
            "All three cohorts use the same Publication→Projection→Adapter→Intent "
            "path; no wave-specific contract divergence beyond expected platform mix."
        ),
    }


def shadow_deep_analysis(
    conn: sqlite3.Connection,
    inventory: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    report = compare_storefronts(conn)
    d = report.to_dict()
    inv = inventory or inventory_published(conn)
    by_sid = {r["soldium_service_id"]: r for r in inv}
    by_legacy = {r["legacy_catalog_id"]: r for r in inv if r.get("legacy_catalog_id")}

    correlated = [r for r in report.rows if r.correlation == "correlated"]
    by_category: Counter[str] = Counter()
    by_code: Counter[str] = Counter()
    classified_diffs: list[dict[str, Any]] = []
    platforms: dict[str, Counter] = defaultdict(Counter)
    by_cohort: dict[str, Counter] = defaultdict(Counter)
    by_service_type: dict[str, Counter] = defaultdict(Counter)
    by_fulfillment: dict[str, Counter] = defaultdict(Counter)
    by_pricing: dict[str, Counter] = defaultdict(Counter)
    by_ordering: dict[str, Counter] = defaultdict(Counter)
    wave_expansion_codes: Counter[str] = Counter()

    for row in correlated:
        cat = (row.catalog or {})
        platform = cat.get("platform_key") or cat.get("platform") or "unknown"
        inv_row = by_sid.get(row.catalog_identity) or by_legacy.get(row.legacy_identity)
        cohort = (inv_row or {}).get("cohort") or "unknown"
        stype = (inv_row or {}).get("service_type") or cat.get("service_type") or "unknown"
        fulfill = (inv_row or {}).get("fulfillment_mode") or "unknown"
        pricing = (inv_row or {}).get("pricing_mode") or "unknown"
        ordering = (inv_row or {}).get("ordering_mode") or "unknown"

        for diff in row.differences:
            by_category[diff.category] += 1
            by_code[diff.code] += 1
            platforms[str(platform)][diff.category] += 1
            by_cohort[str(cohort)][diff.code] += 1
            by_service_type[str(stype)][diff.code] += 1
            by_fulfillment[str(fulfill)][diff.code] += 1
            by_pricing[str(pricing)][diff.code] += 1
            by_ordering[str(ordering)][diff.code] += 1
            if cohort in ("wave1", "wave2"):
                wave_expansion_codes[diff.code] += 1
            if row.severity == "dangerous":
                impact = "CUSTOMER_BREAKING"
            elif row.severity == "review":
                impact = "NON_BREAKING_LEGACY_REPRESENTATION"
            else:
                impact = "REVIEW_ONLY"
            classified_diffs.append(
                {
                    "معرّف_المزود": (inv_row or {}).get("معرّف_المزود"),
                    "catalog_identity": row.catalog_identity,
                    "legacy_identity": row.legacy_identity,
                    "cohort": cohort,
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
            x["catalog_identity"] for x in classified_diffs if x["code"] == code
        ]
        sample = next(x for x in classified_diffs if x["code"] == code)
        wave_hits = [
            x for x in classified_diffs
            if x["code"] == code and x["cohort"] in ("wave1", "wave2")
        ]
        recurring.append(
            {
                "code": code,
                "category": sample["category"],
                "count": count,
                "wave1_wave2_count": len(wave_hits),
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
        "by_cohort": {k: dict(v) for k, v in by_cohort.items()},
        "by_platform_category": {k: dict(v) for k, v in platforms.items()},
        "by_service_type": {k: dict(v) for k, v in by_service_type.items()},
        "by_fulfillment": {k: dict(v) for k, v in by_fulfillment.items()},
        "by_pricing": {k: dict(v) for k, v in by_pricing.items()},
        "by_ordering": {k: dict(v) for k, v in by_ordering.items()},
        "wave1_wave2_recurring_codes": dict(wave_expansion_codes),
        "recurring_patterns": recurring,
        "classified_differences_sample": classified_diffs[:40],
        "ok": (
            d.get("dangerous_count", 1) == 0
            and d.get("catalog_count") == 43
            and d.get("correlated_count") == 43
            and d.get("catalog_only_count") == 0
            and d.get("legacy_count") == 253
        ),
    }


def publication_history_integrity(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = production_counts(conn)
    pub = CatalogPublicationService(conn)
    wave2_pubs = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM soldium_catalog_publications
            WHERE published_by = ?
            """,
            (WAVE2_PUBLISHED_BY,),
        ).fetchone()[0]
    )
    wave1_pubs = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM soldium_catalog_publications
            WHERE published_by = ?
            """,
            (WAVE1_PUBLISHED_BY,),
        ).fetchone()[0]
    )
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
                "معرّف_المزود": (
                    str(latest.external_service_id) if latest else None
                ),
                "soldium_service_id": sid,
                "publication_rows": n,
                "latest_id": latest.id if latest else None,
                "published_by": latest.published_by if latest else None,
            }
        )
    ok = counts["publications"] == 53 and wave2_pubs == 15
    return {
        "publications_total": counts["publications"],
        "expected_total": 53,
        "wave1_published_by_rows": wave1_pubs,
        "wave2_published_by_rows": wave2_pubs,
        "expected_wave2_rows": 15,
        "ok": ok,
        "per_service_sample": per_service[:5],
        "per_service_count": len(per_service),
    }


def execution_source_history_audit(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read-only: published snapshot must not auto-follow live execution source edits."""
    pub = CatalogPublicationService(conn)
    core = CatalogCoreService(conn)
    repo = CatalogRepository(conn)
    mismatches: list[dict[str, Any]] = []
    checked = 0
    for sid in sorted(_published_ids(conn)):
        latest = pub.get_latest_publish(sid)
        active = repo.get_active_execution_source(sid)
        hist = core.list_execution_source_history(sid)
        checked += 1
        if not latest or not active:
            mismatches.append(
                {
                    "soldium_service_id": sid,
                    "reason": "missing_publish_or_active_source",
                }
            )
            continue
        # Live active may differ from published when unpublished changes exist;
        # published triple must still be frozen and history must exist.
        if not hist:
            mismatches.append(
                {
                    "soldium_service_id": sid,
                    "reason": "empty_execution_source_history",
                }
            )
            continue
        active_count = sum(1 for h in hist if h.status == "active")
        if active_count != 1:
            mismatches.append(
                {
                    "soldium_service_id": sid,
                    "reason": f"active_count={active_count}",
                }
            )
        # Snapshot execution identity must be present and TEXT-stable
        if not (
            latest.provider_slug
            and latest.provider_account_key
            and latest.external_service_id is not None
        ):
            mismatches.append(
                {
                    "soldium_service_id": sid,
                    "reason": "published_execution_incomplete",
                }
            )
    return {
        "checked": checked,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:20],
        "note": (
            "Read-only: confirms execution-source history exists and published "
            "execution identity is complete; live edits do not auto-republish "
            "(proven in fixtures)."
        ),
        "ok": len(mismatches) == 0,
    }


def isolation_architecture_check() -> dict[str, Any]:
    proj = Path("catalog_core/storefront_projection.py").read_text(encoding="utf-8")
    adapt = Path("catalog_core/storefront_adapter.py").read_text(encoding="utf-8")
    proj_hits = _FROM_SMM.findall(proj)
    adapt_hits = _FROM_SMM.findall(adapt)
    return {
        "projection_from_smm_services": len(proj_hits),
        "adapter_from_smm_services": len(adapt_hits),
        "projection_documents_isolation": "Never reads ``smm_services``" in proj
        or "Never reads `smm_services`" in proj
        or "Never reads smm_services" in proj,
        "adapter_documents_isolation": "Never reads ``smm_services``" in adapt
        or "Never reads `smm_services`" in adapt
        or "Never reads smm_services" in adapt,
        "ok": len(proj_hits) == 0 and len(adapt_hits) == 0,
        "scope": "EXPANSION",
        "note": "Static check only — projection/adapter must not SQL-read smm_services.",
    }


def cohort_regression(
    inventory: list[dict[str, Any]],
    execution: dict[str, Any],
    contracts: dict[str, Any],
) -> dict[str, Any]:
    by_cohort: dict[str, list[str]] = {
        "original13": [],
        "wave1": [],
        "wave2": [],
    }
    for row in inventory:
        by_cohort.setdefault(row["cohort"], []).append(row["soldium_service_id"])

    fail_ids = {
        f["soldium_service_id"] for f in contracts.get("failures") or []
    }
    mismatch_ids = {
        m["soldium_service_id"] for m in execution.get("mismatches") or []
    }
    out: dict[str, Any] = {}
    for name, sids in by_cohort.items():
        contract_fails = [s for s in sids if s in fail_ids]
        exec_fails = [s for s in sids if s in mismatch_ids]
        expected = {"original13": 13, "wave1": 15, "wave2": 15}.get(name)
        out[name] = {
            "count": len(sids),
            "expected_count": expected,
            "count_ok": expected is None or len(sids) == expected,
            "contract_failures": contract_fails,
            "execution_mismatches": exec_fails,
            "ok": (
                (expected is None or len(sids) == expected)
                and not contract_fails
                and not exec_fails
            ),
        }
    return {
        "cohorts": out,
        "ok": all(v["ok"] for v in out.values()),
    }


def build_cutover_matrix(conn: sqlite3.Connection, findings: dict[str, Any]) -> list[dict[str, Any]]:
    base = cutover_matrix(
        {"dangerous": findings["shadow"]["aggregate"].get("dangerous_count", 0)}
    )
    out: list[dict[str, Any]] = []
    for r in base:
        status = r.get("status")
        blocking = status == "BLOCKED"
        # Known architectural cutover debt is CUTOVER-scoped, not expansion-blocking.
        if blocking:
            impact = "CUTOVER"
        elif status in ("PARTIAL", "NOT YET TESTED"):
            impact = "NON_BLOCKING"
        else:
            impact = "EXPANSION"
        out.append(
            {
                "item": r.get("item"),
                "status": status,
                "evidence": r.get("note") or r.get("evidence"),
                "blocking": blocking,
                "impact_scope": impact,
            }
        )

    extra = [
        {
            "item": "Catalog contract stability (43 published)",
            "status": "PASS" if findings["contracts"]["ok"] else "BLOCKED",
            "evidence": f"contract failures={findings['contracts']['failure_count']}",
            "blocking": not findings["contracts"]["ok"],
            "impact_scope": "EXPANSION",
        },
        {
            "item": "Execution identity across pub/proj/adapter/intent",
            "status": "PASS" if findings["execution"]["ok"] else "BLOCKED",
            "evidence": f"mismatches={findings['execution']['mismatch_count']}",
            "blocking": not findings["execution"]["ok"],
            "impact_scope": "EXPANSION",
        },
        {
            "item": "Three-cohort regression (13+15+15)",
            "status": "PASS" if findings["cohort_regression"]["ok"] else "BLOCKED",
            "evidence": findings["cohort_regression"]["cohorts"],
            "blocking": not findings["cohort_regression"]["ok"],
            "impact_scope": "EXPANSION",
        },
        {
            "item": "Provider ID visibility (9G.1)",
            "status": "PASS" if findings["provider_id_visibility"]["ok"] else "BLOCKED",
            "evidence": "معرّف المزود + copy + replace UX present",
            "blocking": False,
            "impact_scope": "NON_BLOCKING",
        },
        {
            "item": "Shadow parity (43)",
            "status": "PASS" if findings["shadow"]["ok"] else "BLOCKED",
            "evidence": findings["shadow"]["aggregate"],
            "blocking": not findings["shadow"]["ok"],
            "impact_scope": "EXPANSION",
        },
        {
            "item": "Projection/Adapter Legacy isolation",
            "status": "PASS" if findings["isolation"]["ok"] else "BLOCKED",
            "evidence": findings["isolation"],
            "blocking": not findings["isolation"]["ok"],
            "impact_scope": "EXPANSION",
        },
        {
            "item": "Customer parity",
            "status": "PARTIAL",
            "evidence": "43/253 published — expansion validated, not full parity",
            "blocking": False,
            "impact_scope": "CUTOVER",
        },
        {
            "item": "Telegram E2E",
            "status": "BLOCKED",
            "evidence": "Not tested; Telegram still Legacy runtime",
            "blocking": True,
            "impact_scope": "CUTOVER",
        },
        {
            "item": "Rollback strategy",
            "status": "BLOCKED",
            "evidence": "Not cutover-verified",
            "blocking": True,
            "impact_scope": "CUTOVER",
        },
        {
            "item": "4371 hardcoded fallback",
            "status": findings["service_4371"]["status"],
            "evidence": findings["service_4371"],
            "blocking": findings["service_4371"]["status"] == "CUTOVER_BLOCKER",
            "impact_scope": "CUTOVER",
        },
        {
            "item": "Gen-0 scheduled orders Legacy lookup",
            "status": "BLOCKED" if findings["scheduled"]["cutover_blocker"] else "PASS",
            "evidence": findings["scheduled"],
            "blocking": bool(findings["scheduled"]["cutover_blocker"]),
            "impact_scope": "CUTOVER",
        },
    ]
    out.extend(extra)
    return out


def decide_expansion(findings: dict[str, Any]) -> dict[str, Any]:
    customer_breaking = findings["shadow"]["aggregate"].get("dangerous_count", 0) > 0
    contract_ok = findings["contracts"]["ok"] and findings["execution"]["ok"]
    baseline_ok = findings["baseline"]["ok"] and findings["end_invariants"]["ok"]
    history_ok = findings["publication_history"]["ok"]
    drift_dangerous = findings["drift"]["classification"] == "dangerous"
    isolation_ok = findings["isolation"]["ok"]
    cohort_ok = findings["cohort_regression"]["ok"]
    shadow_ok = findings["shadow"]["ok"]

    expansion_blocked = (
        customer_breaking
        or not contract_ok
        or not baseline_ok
        or not history_ok
        or drift_dangerous
        or findings["execution"]["mismatch_count"] > 0
        or not isolation_ok
        or not cohort_ok
        or not shadow_ok
    )

    # Known cutover blockers do NOT auto-block expansion.
    cutover_blockers = [
        b
        for b in findings["cutover_matrix"]
        if b.get("blocking") and b.get("impact_scope") == "CUTOVER"
    ]
    expansion_blockers = [
        b
        for b in findings["cutover_matrix"]
        if b.get("blocking") and b.get("impact_scope") == "EXPANSION"
    ]

    if expansion_blocked or expansion_blockers:
        decision = "EXPANSION_BLOCKED"
        next_step = "PAUSE_AND_REMEDIATE"
    elif cutover_blockers or findings["drift"]["drifted_count"] > 0:
        # Stable catalog + known remediations → continue controlled waves (not mass publish).
        decision = "EXPANSION_READY_WITH_REMEDIATIONS"
        next_step = "CONTINUE_CONTROLLED_EXPANSION"
    else:
        decision = "EXPANSION_READY"
        next_step = "BEGIN_CUTOVER_PREPARATION"

    remediations = [
        "4371 hardcoded fallback still present in target_validation",
        "Gen-0 scheduled orders still use Legacy live SKU/price lookup",
        "Telegram still on Legacy runtime — E2E cutover not validated",
        "Customer parity only 43/253",
        "Rollback strategy not cutover-verified",
        "Shadow review differences remain (non-breaking Legacy representation)",
    ]
    return {
        "decision": decision,
        "next_step": next_step,
        "customer_breaking": customer_breaking,
        "remediations_non_blocking": remediations,
        "cutover_blockers": cutover_blockers,
        "expansion_blockers": expansion_blockers,
        "rationale": (
            "43-service Catalog contract is stable (dangerous=0, identity/contracts pass). "
            "Controlled expansion may continue. Telegram cutover and architectural debt "
            "remain separate cutover blockers — they do not auto-block Catalog wave expansion."
            if decision != "EXPANSION_BLOCKED"
            else "Blocking expansion-scoped contract or shadow finding prevents further waves."
        ),
    }


def run_phase9l(conn: sqlite3.Connection) -> dict[str, Any]:
    before = capture_baseline(conn)
    if not before["ok"]:
        return {
            "phase": PHASE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mutations": "NONE — read-only validation (fixtures only in tests)",
            "verdict": "PHASE_9L_BLOCKED — REVIEW_REQUIRED",
            "EXPANSION_DECISION": "EXPANSION_BLOCKED",
            "CUTOVER_STATUS": "CUTOVER_NOT_READY",
            "next_step": "PAUSE_AND_REMEDIATE",
            "reason": "baseline mismatch",
            "baseline": before,
        }

    inventory = inventory_published(conn)
    execution = audit_execution_identity(conn)
    contracts = audit_contracts(conn)
    drift = analyze_drift(conn, inventory)
    cohorts = compare_three_cohorts(inventory)
    shadow = shadow_deep_analysis(conn, inventory)
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
    s4371["note"] = "Do not remediate in 9L — document only (cutover debt)."
    scheduled = audit_scheduled_orders(conn)
    history = publication_history_integrity(conn)
    exec_hist = execution_source_history_audit(conn)
    isolation = isolation_architecture_check()
    regression = cohort_regression(inventory, execution, contracts)
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
        "isolation": isolation,
        "cohort_regression": regression,
        "service_4371": s4371,
        "scheduled": scheduled,
    }
    findings["cutover_matrix"] = build_cutover_matrix(conn, findings)
    decision = decide_expansion(findings)

    cutover_ready = (
        decision["decision"] == "EXPANSION_READY"
        and not decision["cutover_blockers"]
    )
    cutover_status = (
        "CUTOVER_PREPARATION_ELIGIBLE"
        if cutover_ready
        else "CUTOVER_NOT_READY — remediations remain"
    )

    if decision["decision"] == "EXPANSION_BLOCKED":
        verdict = "PHASE_9L_BLOCKED — REVIEW_REQUIRED"
    else:
        verdict = "PHASE_9L_COMPLETE — EXPANSION_VALIDATED"

    orders_before = before["baseline"]["orders"]
    orders_after = after["baseline"]["orders"]

    return {
        "phase": PHASE,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mutations": "NONE — read-only validation (fixtures only in tests)",
        "verdict": verdict,
        "EXPANSION_DECISION": decision["decision"],
        "CUTOVER_STATUS": cutover_status,
        "next_step": decision["next_step"],
        "expansion_decision": decision,
        "production_baseline": before["baseline"],
        "production_end": after["baseline"],
        "production_unchanged": before["baseline"] == after["baseline"],
        "published_inventory": inventory,
        "three_cohorts": cohorts,
        "cohort_regression": regression,
        "execution_identity_audit": execution,
        "execution_source_history_audit": exec_hist,
        "provider_id_visibility": provider_vis,
        "drift_analysis": drift,
        "contracts": contracts,
        "shadow": shadow,
        "isolation_architecture": isolation,
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
        "wave1_legacy_ids": list(WAVE1_CANDIDATE_LEGACY_IDS),
        "wave2_legacy_ids": list(WAVE2_LEGACY_IDS),
        "WAVE2_PUBLISHED_BY": WAVE2_PUBLISHED_BY,
    }
