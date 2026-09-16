# -*- coding: utf-8 -*-
"""Phase 9G-B — apply approved 9G-A business decisions (service_type only).

No publication, prices, quantities, targets, fulfillment, or execution changes.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from catalog_core.commercial import normalize_service_type
from catalog_core.phase9c_audit import SENTINEL_MAX
from catalog_core.phase9d_audit import (
    CONFIDENCE_PROBABLE,
    CONFIDENCE_SPECIAL,
    CONFIDENCE_UNKNOWN,
    load_phase9d_rows,
    production_counts,
)
from catalog_core.phase9g_business_decision_pack import (
    _cohort_key,
    _published_ids,
    analyze_special_unknown,
    partition_unpublished,
)
from catalog_core.publication import CatalogPublicationService
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.service import CatalogCoreService, _new_id

PHASE = "9G-B"
ACTOR = "phase9g_business_decisions_apply"

FUTURE_PILOT_LEGACY_IDS: tuple[str, ...] = (
    "4551",
    "4823",
    "4866",
    "4309",
    "2226",
    "2491",
    "1922",
    "1404",
    "3045",
    "3965",
    "4549",
    "3956",
    "4496",
    "4864",
    "4868",
)

MISSING_EXEC_LEGACY_IDS: tuple[str, ...] = (
    "2128",
    "2326",
    "2405",
    "4590",
    "4721",
)

EXPECTED_BASELINE = {
    "services": 253,
    "publications": 23,
    "published_services": 13,
    "orders": 44,
    "smm_services": 2069,
    "execution_sources": 248,
    "mappings": 0,
    "nodes": 59,
    "entries": 312,
    "prices": 253,
}


@dataclass
class DecisionMember:
    legacy_catalog_id: str | None
    soldium_service_id: str
    platform: str | None
    section: str | None
    subsection: str | None
    service_type: str
    cohort_id: str = ""


@dataclass
class DecisionSets:
    PROBABLE_KEEP_OTHER: list[DecisionMember] = field(default_factory=list)
    PROBABLE_BUSINESS_REVIEW: list[DecisionMember] = field(default_factory=list)
    SPECIAL_SAFE_AS_OTHER: list[DecisionMember] = field(default_factory=list)
    SPECIAL_BUSINESS_REVIEW: list[DecisionMember] = field(default_factory=list)
    SPECIAL_TECHNICAL_REVIEW: list[DecisionMember] = field(default_factory=list)
    SENTINEL_BLOCKED: list[DecisionMember] = field(default_factory=list)
    IPTV_BLOCKED: list[DecisionMember] = field(default_factory=list)
    MISSING_EXECUTION_BLOCKED: list[DecisionMember] = field(default_factory=list)
    READY_UNPUBLISHED: list[DecisionMember] = field(default_factory=list)
    FUTURE_PILOT_CANDIDATES: list[DecisionMember] = field(default_factory=list)
    OTHER_REVIEW: list[DecisionMember] = field(default_factory=list)

    def all_members(self) -> list[tuple[str, DecisionMember]]:
        out: list[tuple[str, DecisionMember]] = []
        for name in (
            "PROBABLE_KEEP_OTHER",
            "PROBABLE_BUSINESS_REVIEW",
            "SPECIAL_SAFE_AS_OTHER",
            "SPECIAL_BUSINESS_REVIEW",
            "SPECIAL_TECHNICAL_REVIEW",
            "SENTINEL_BLOCKED",
            "IPTV_BLOCKED",
            "MISSING_EXECUTION_BLOCKED",
            "READY_UNPUBLISHED",
            "OTHER_REVIEW",
        ):
            for m in getattr(self, name):
                out.append((name, m))
        return out

    def counts(self) -> dict[str, int]:
        return {
            "PROBABLE_KEEP_OTHER": len(self.PROBABLE_KEEP_OTHER),
            "PROBABLE_BUSINESS_REVIEW": len(self.PROBABLE_BUSINESS_REVIEW),
            "SPECIAL_SAFE_AS_OTHER": len(self.SPECIAL_SAFE_AS_OTHER),
            "SPECIAL_BUSINESS_REVIEW": len(self.SPECIAL_BUSINESS_REVIEW),
            "SPECIAL_TECHNICAL_REVIEW": len(self.SPECIAL_TECHNICAL_REVIEW),
            "SENTINEL_BLOCKED": len(self.SENTINEL_BLOCKED),
            "IPTV_BLOCKED": len(self.IPTV_BLOCKED),
            "MISSING_EXECUTION_BLOCKED": len(self.MISSING_EXECUTION_BLOCKED),
            "READY_UNPUBLISHED": len(self.READY_UNPUBLISHED),
            "FUTURE_PILOT_CANDIDATES": len(self.FUTURE_PILOT_CANDIDATES),
            "OTHER_REVIEW": len(self.OTHER_REVIEW),
        }


def _member(r, cohort_id: str = "") -> DecisionMember:
    return DecisionMember(
        legacy_catalog_id=r.legacy_catalog_id,
        soldium_service_id=r.soldium_service_id,
        platform=r.platform_key,
        section=r.section_key,
        subsection=r.subsection_key,
        service_type=r.service_type,
        cohort_id=cohort_id,
    )


def _member_dict(m: DecisionMember) -> dict[str, Any]:
    return {
        "legacy_catalog_id": m.legacy_catalog_id,
        "soldium_service_id": m.soldium_service_id,
        "platform": m.platform,
        "section": m.section,
        "subsection": m.subsection,
        "service_type": m.service_type,
        "cohort_id": m.cohort_id,
    }


def build_decision_sets(conn: sqlite3.Connection) -> DecisionSets:
    """Deterministic sets from live bridge + 9G-A classification rules (no fuzzy)."""
    published = _published_ids(conn)
    rows = [r for r in load_phase9d_rows(conn) if r.soldium_service_id not in published]
    parts = partition_unpublished(rows)
    if sum(len(v) for v in parts.values()) != len(rows):
        raise RuntimeError("partition sum mismatch")

    sets = DecisionSets()
    by_svc: dict[str, str] = {}

    def claim(bucket: str, members: list, cohort_id: str = "") -> None:
        target = getattr(sets, bucket)
        for r in members:
            if r.soldium_service_id in by_svc:
                raise RuntimeError(
                    f"overlap: {r.soldium_service_id} in {by_svc[r.soldium_service_id]} "
                    f"and {bucket}"
                )
            by_svc[r.soldium_service_id] = bucket
            target.append(_member(r, cohort_id=cohort_id))

    # PROBABLE split
    keep = []
    review = []
    for r in parts["probable"]:
        if r.platform_key == "facebook" and r.section_key == "followers_members":
            review.append(r)
        else:
            keep.append(r)
    claim("PROBABLE_KEEP_OTHER", keep, "probable_keep_other")
    claim("PROBABLE_BUSINESS_REVIEW", review, "facebook::followers_members")

    # SPECIAL via 9G-A classifier
    special_analysis = analyze_special_unknown(parts["special_unknown"], conn)
    by_legacy = {r.legacy_catalog_id: r for r in parts["special_unknown"]}
    for c in special_analysis["cohorts"]:
        members = [by_legacy[lid] for lid in c["all_legacy_ids"] if lid in by_legacy]
        cls = c["classification"]
        if cls == "SAFE_AS_OTHER":
            claim("SPECIAL_SAFE_AS_OTHER", members, c["cohort_id"])
        elif cls == "BUSINESS_DECISION_REQUIRED":
            claim("SPECIAL_BUSINESS_REVIEW", members, c["cohort_id"])
        elif cls == "TECHNICAL_REVIEW_REQUIRED":
            claim("SPECIAL_TECHNICAL_REVIEW", members, c["cohort_id"])
        else:
            raise RuntimeError(f"unexpected special classification {cls}")

    claim("SENTINEL_BLOCKED", parts["sentinel"], "sentinel")
    claim("IPTV_BLOCKED", parts["iptv"], "iptv")
    claim("MISSING_EXECUTION_BLOCKED", parts["missing_execution"], "missing_exec")
    claim("READY_UNPUBLISHED", parts["ready"], "ready")
    claim("OTHER_REVIEW", parts["other_review"], "other_review")

    if len(by_svc) != len(rows):
        raise RuntimeError(
            f"exclusive coverage incomplete: {len(by_svc)} vs {len(rows)}"
        )

    # Future pilots — subset of READY by exact Legacy ID (not a second exclusive bucket)
    ready_by_legacy = {
        m.legacy_catalog_id: m for m in sets.READY_UNPUBLISHED if m.legacy_catalog_id
    }
    for lid in FUTURE_PILOT_LEGACY_IDS:
        if lid not in ready_by_legacy:
            raise RuntimeError(f"future pilot Legacy {lid} not in READY_UNPUBLISHED")
        sets.FUTURE_PILOT_CANDIDATES.append(ready_by_legacy[lid])

    # Missing-exec Legacy IDs must match approved list
    miss_ids = {m.legacy_catalog_id for m in sets.MISSING_EXECUTION_BLOCKED}
    if miss_ids != set(MISSING_EXEC_LEGACY_IDS):
        raise RuntimeError(
            f"missing-exec set mismatch: {sorted(miss_ids)} vs {list(MISSING_EXEC_LEGACY_IDS)}"
        )

    return sets


def _commercial_fingerprint(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Snapshot fields that must remain immutable except approved service_type."""
    rows = conn.execute(
        """
        SELECT id, service_type, ordering_mode, min_quantity, max_quantity,
               fulfillment_mode,
               target_platform_key, target_section_key, target_subsection_key,
               target_link_prompt_key, target_link_type
        FROM soldium_catalog_services
        """
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        sid = str(r["id"])
        src = conn.execute(
            """
            SELECT provider_slug, provider_account_key, external_service_id
            FROM soldium_catalog_execution_sources
            WHERE service_id = ? AND status = 'active' LIMIT 1
            """,
            (sid,),
        ).fetchone()
        price = conn.execute(
            """
            SELECT amount_millimes, currency, pricing_mode
            FROM soldium_catalog_prices
            WHERE service_id = ? AND status = 'active' LIMIT 1
            """,
            (sid,),
        ).fetchone()
        out[sid] = {
            "service_type": r["service_type"],
            "ordering_mode": r["ordering_mode"],
            "min_quantity": r["min_quantity"],
            "max_quantity": r["max_quantity"],
            "fulfillment_mode": r["fulfillment_mode"],
            "target_platform_key": r["target_platform_key"],
            "target_section_key": r["target_section_key"],
            "target_subsection_key": r["target_subsection_key"],
            "target_link_prompt_key": r["target_link_prompt_key"],
            "target_link_type": r["target_link_type"],
            "exec": dict(src) if src else None,
            "price": dict(price) if price else None,
        }
    return out


def _ensure_semantic_events_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS soldium_catalog_semantic_events (
            id TEXT PRIMARY KEY,
            service_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            previous_value TEXT,
            new_value TEXT,
            reason TEXT,
            phase TEXT,
            actor TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (service_id) REFERENCES soldium_catalog_services(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_soldium_catalog_semantic_events_svc
        ON soldium_catalog_semantic_events (service_id, created_at DESC)
        """
    )


def _record_semantic_event(
    conn: sqlite3.Connection,
    *,
    service_id: str,
    field_name: str,
    previous_value: str | None,
    new_value: str | None,
    reason: str,
    actor: str = ACTOR,
) -> None:
    conn.execute(
        """
        INSERT INTO soldium_catalog_semantic_events (
            id, service_id, field_name, previous_value, new_value,
            reason, phase, actor
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _new_id("sem"),
            service_id,
            field_name,
            previous_value,
            new_value,
            reason,
            PHASE,
            actor,
        ),
    )


def apply_approved_service_types(
    conn: sqlite3.Connection,
    sets: DecisionSets,
    *,
    dry_run: bool = False,
    actor: str = ACTOR,
) -> dict[str, Any]:
    """Set service_type=other only for approved cohorts; skip already-correct."""
    _ensure_semantic_events_table(conn)
    core = CatalogCoreService(conn)
    desired = normalize_service_type("other")
    mutations: list[dict[str, Any]] = []
    skipped_already: list[dict[str, Any]] = []

    targets: list[tuple[str, DecisionMember]] = [
        ("PROBABLE_KEEP_OTHER", m) for m in sets.PROBABLE_KEEP_OTHER
    ] + [("SPECIAL_SAFE_AS_OTHER", m) for m in sets.SPECIAL_SAFE_AS_OTHER]

    for decision, m in targets:
        svc = core.get_service(m.soldium_service_id)
        before = svc.service_type
        if before == desired:
            skipped_already.append(
                {
                    "decision": decision,
                    "legacy_catalog_id": m.legacy_catalog_id,
                    "soldium_service_id": m.soldium_service_id,
                    "service_type": before,
                }
            )
            continue
        if not dry_run:
            core.update_service(m.soldium_service_id, service_type=desired)
            _record_semantic_event(
                conn,
                service_id=m.soldium_service_id,
                field_name="service_type",
                previous_value=before,
                new_value=desired,
                reason=f"{decision}: approved KEEP_OTHER / SAFE_AS_OTHER",
                actor=actor,
            )
        mutations.append(
            {
                "decision": decision,
                "legacy_catalog_id": m.legacy_catalog_id,
                "soldium_service_id": m.soldium_service_id,
                "previous_service_type": before,
                "new_service_type": desired,
                "applied": not dry_run,
            }
        )

    return {
        "mutations": mutations,
        "mutation_count": len(mutations),
        "skipped_already_other": len(skipped_already),
        "skipped_details_sample": skipped_already[:10],
        "dry_run": dry_run,
    }


def _published_fingerprints(conn: sqlite3.Connection) -> dict[str, Any]:
    published = _published_ids(conn)
    out: dict[str, Any] = {}
    for sid in sorted(published):
        row = conn.execute(
            """
            SELECT id, event_type, content_fingerprint, external_service_id,
                   service_type, amount_millimes, min_quantity, max_quantity,
                   fulfillment_mode, provider_slug, provider_account_key,
                   ordering_mode, pricing_mode
            FROM soldium_catalog_publications
            WHERE service_id = ?
            ORDER BY published_at DESC, id DESC
            LIMIT 1
            """,
            (sid,),
        ).fetchone()
        st = CatalogPublicationService(conn).get_publication_status(sid)
        out[sid] = {
            "publication_status": st.get("publication_status"),
            "has_unpublished_changes": st.get("has_unpublished_changes"),
            "latest_row": dict(row) if row else None,
            "publications_count_for_service": int(
                conn.execute(
                    "SELECT COUNT(*) FROM soldium_catalog_publications WHERE service_id=?",
                    (sid,),
                ).fetchone()[0]
            ),
        }
    return out


def verify_invariants(
    conn: sqlite3.Connection,
    *,
    before_fp: dict[str, dict[str, Any]],
    after_fp: dict[str, dict[str, Any]],
    before_pub: dict[str, Any],
    after_pub: dict[str, Any],
    allowed_service_type_ids: set[str],
) -> dict[str, Any]:
    errors: list[str] = []
    counts = production_counts(conn)
    scheduled = int(conn.execute("SELECT COUNT(*) FROM scheduled_orders").fetchone()[0])
    published_n = len(_published_ids(conn))

    checks = {
        "services": counts["services"] == EXPECTED_BASELINE["services"],
        "publications": counts["publications"] == EXPECTED_BASELINE["publications"],
        "published_services": published_n == EXPECTED_BASELINE["published_services"],
        "orders": counts["orders"] == EXPECTED_BASELINE["orders"],
        "smm_services": counts["smm_services"] == EXPECTED_BASELINE["smm_services"],
        "execution_sources": counts["execution_sources"]
        == EXPECTED_BASELINE["execution_sources"],
        "mappings": counts["mappings"] == EXPECTED_BASELINE["mappings"],
        "nodes": counts["nodes"] == EXPECTED_BASELINE["nodes"],
        "entries": counts["entries"] == EXPECTED_BASELINE["entries"],
        "prices": counts["prices"] == EXPECTED_BASELINE["prices"],
        "scheduled_orders": scheduled == 0,
    }
    for k, ok in checks.items():
        if not ok:
            errors.append(f"count mismatch {k}: {counts.get(k, scheduled)}")

    # Field-level: only service_type may change, and only for allowed ids
    for sid, before in before_fp.items():
        after = after_fp.get(sid)
        if after is None:
            errors.append(f"missing service after: {sid}")
            continue
        for field in (
            "ordering_mode",
            "min_quantity",
            "max_quantity",
            "fulfillment_mode",
            "target_platform_key",
            "target_section_key",
            "target_subsection_key",
            "target_link_prompt_key",
            "target_link_type",
            "exec",
            "price",
        ):
            if before.get(field) != after.get(field):
                errors.append(f"{sid} changed {field}")
        if before["service_type"] != after["service_type"]:
            if sid not in allowed_service_type_ids:
                errors.append(f"{sid} unauthorized service_type change")
            elif after["service_type"] != "other":
                errors.append(f"{sid} service_type changed to non-other")

    # Publication fingerprints for published 13
    for sid, b in before_pub.items():
        a = after_pub.get(sid, {})
        if b.get("latest_row") != a.get("latest_row"):
            errors.append(f"publication row changed for {sid}")
        if b.get("publication_status") != a.get("publication_status"):
            errors.append(f"publication status changed for {sid}")

    return {
        "ok": not errors,
        "errors": errors,
        "checks": checks,
        "counts": counts,
        "published_services": published_n,
        "scheduled_orders": scheduled,
    }


def readiness_snapshot(
    conn: sqlite3.Connection, service_ids: list[str]
) -> dict[str, Any]:
    repo = CatalogRepository(conn)
    ready = 0
    needs = 0
    details = []
    for sid in service_ids:
        svc = repo.get_service(sid)
        if not svc:
            continue
        entry = repo.get_entry_for_service(sid)
        if entry:
            svc.entry_id = entry.id
            svc.parent_entry_id = entry.parent_entry_id
        src = repo.get_active_execution_source(sid)
        price = repo.get_active_price(sid)
        result = evaluate_service_readiness(repo, svc, source=src, price=price)
        if result.ready:
            ready += 1
        else:
            needs += 1
        details.append(
            {
                "soldium_service_id": sid,
                "ready": bool(result.ready),
                "reasons": [i.title for i in (result.issues or [])][:5],
            }
        )
    return {"ready": ready, "needs_review": needs, "details": details}


def run_phase9gb(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    actor: str = ACTOR,
) -> dict[str, Any]:
    before_counts = production_counts(conn)
    published_n = len(_published_ids(conn))
    if (
        before_counts["services"] != EXPECTED_BASELINE["services"]
        or before_counts["publications"] != EXPECTED_BASELINE["publications"]
        or published_n != EXPECTED_BASELINE["published_services"]
        or before_counts["orders"] != EXPECTED_BASELINE["orders"]
        or before_counts["smm_services"] != EXPECTED_BASELINE["smm_services"]
        or before_counts["execution_sources"] != EXPECTED_BASELINE["execution_sources"]
        or before_counts["mappings"] != EXPECTED_BASELINE["mappings"]
    ):
        return {
            "verdict": "PHASE 9G-B BLOCKED — REVIEW REQUIRED",
            "reason": "preflight baseline mismatch",
            "before_counts": before_counts,
            "published_services": published_n,
            "expected": EXPECTED_BASELINE,
        }

    sets = build_decision_sets(conn)
    before_fp = _commercial_fingerprint(conn)
    before_pub = _published_fingerprints(conn)

    apply_targets = [
        m.soldium_service_id
        for m in sets.PROBABLE_KEEP_OTHER + sets.SPECIAL_SAFE_AS_OTHER
    ]
    readiness_before = readiness_snapshot(conn, apply_targets)

    apply_result = apply_approved_service_types(
        conn, sets, dry_run=dry_run, actor=actor
    )
    allowed_ids = {m["soldium_service_id"] for m in apply_result["mutations"]}

    after_fp = _commercial_fingerprint(conn)
    after_pub = _published_fingerprints(conn)
    after_counts = production_counts(conn)
    inv = verify_invariants(
        conn,
        before_fp=before_fp,
        after_fp=after_fp,
        before_pub=before_pub,
        after_pub=after_pub,
        allowed_service_type_ids=allowed_ids,
    )

    readiness_after = readiness_snapshot(conn, apply_targets)
    unexpected_not_ready = []
    before_map = {d["soldium_service_id"]: d for d in readiness_before["details"]}
    for d in readiness_after["details"]:
        b = before_map.get(d["soldium_service_id"])
        if b and b["ready"] and not d["ready"]:
            unexpected_not_ready.append(d)

    # Idempotent second pass
    sets2 = build_decision_sets(conn)
    second = apply_approved_service_types(
        conn, sets2, dry_run=dry_run, actor=actor
    )

    verdict = "PHASE 9G-B COMPLETE — APPROVED DECISIONS APPLIED"
    if not inv["ok"] or unexpected_not_ready:
        verdict = "PHASE 9G-B BLOCKED — REVIEW REQUIRED"

    return {
        "phase": PHASE,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "dry_run": dry_run,
        "actor": actor,
        "preflight": {
            "before_counts": before_counts,
            "published_services": published_n,
            "baseline_ok": True,
        },
        "decision_sets": {
            name: [_member_dict(m) for m in getattr(sets, name)]
            for name in sets.counts()
        },
        "decision_counts": sets.counts(),
        "apply": apply_result,
        "idempotency_second_pass": {
            "mutation_count": second["mutation_count"],
            "skipped_already_other": second["skipped_already_other"],
            "noop": second["mutation_count"] == 0,
        },
        "readiness_before": readiness_before,
        "readiness_after": readiness_after,
        "unexpected_not_ready": unexpected_not_ready,
        "invariants": inv,
        "after_counts": after_counts,
        "published_fingerprints_unchanged": before_pub == after_pub
        or all(
            before_pub[s].get("latest_row") == after_pub.get(s, {}).get("latest_row")
            for s in before_pub
        ),
        "forbidden_actions": {
            "publication": False,
            "unpublication": False,
            "execution_source_change": False,
            "price_change": False,
            "quantity_change": False,
            "target_change": False,
            "fulfillment_change": False,
            "provider_mapping": False,
            "pilot_publish": False,
            "ready_publish": False,
        },
    }
