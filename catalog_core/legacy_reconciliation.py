# -*- coding: utf-8 -*-
"""Phase 8E — read-only post-migration reconciliation.

Compares legacy ``smm_services`` + Dry Run expectations against migrated Catalog.
NEVER writes. NEVER repairs. Failures are reported only.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from catalog_core.db import catalog_readonly_connection, resolve_db_path
from catalog_core.legacy_migration import (
    LEGACY_NODE_BRIDGE_TABLE,
    LEGACY_SERVICE_BRIDGE_TABLE,
    is_max_qty_sentinel,
    legacy_node_key_platform,
    legacy_node_key_section,
    legacy_node_key_subsection,
    normalize_legacy_price_dh,
    plan_legacy_migration,
    planned_soldium_service_id,
)
from catalog_core.legacy_migration_import import (
    APPROVED_CANDIDATE_COUNT,
    APPROVED_EXEC_SOURCE_COUNT,
    APPROVED_NODE_COUNT,
    APPROVED_ORDERS_COUNT,
    APPROVED_PLAN_FINGERPRINT,
    APPROVED_REVIEW_COUNT,
    APPROVED_SMM_SERVICES_COUNT,
)
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository

EXPECTED_MIGRATION_BATCH_ID = "mig_511b6ecfdf0d496892bd0da42a2d127a"
EXPECTED_PLATFORM_NODES = 7
EXPECTED_SECTION_NODES = 35
EXPECTED_SUBSECTION_NODES = 17
EXPECTED_SECTION_SERVICES = 137
EXPECTED_SUBSECTION_SERVICES = 116
EXPECTED_PLATFORM_SERVICES = 0
EXPECTED_NORMALIZED_PRICES = 61
EXPECTED_MISSING_SOURCES = 5

Verdict = Literal[
    "RECONCILIATION PASSED",
    "RECONCILIATION PASSED WITH WARNINGS",
    "RECONCILIATION FAILED — HARD STOP",
]


@dataclass
class Mismatch:
    entity: str
    legacy_value: Any
    migrated_value: Any
    classification: str
    why_it_matters: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReconciliationReport:
    generated_at: str
    verdict: Verdict
    sections: dict[str, Any]
    mismatches: list[Mismatch] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hard_stop_reasons: list[str] = field(default_factory=list)
    content_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "verdict": self.verdict,
            "content_fingerprint": self.content_fingerprint,
            "sections": self.sections,
            "mismatches": [m.to_dict() for m in self.mismatches],
            "warnings": self.warnings,
            "hard_stop_reasons": self.hard_stop_reasons,
        }


def _mismatch(
    mismatches: list[Mismatch],
    *,
    entity: str,
    legacy_value: Any,
    migrated_value: Any,
    classification: str,
    why: str,
) -> None:
    mismatches.append(
        Mismatch(
            entity=entity,
            legacy_value=legacy_value,
            migrated_value=migrated_value,
            classification=classification,
            why_it_matters=why,
        )
    )


def _content_fingerprint(report: ReconciliationReport) -> str:
    payload = {
        "verdict": report.verdict,
        "sections": report.sections,
        "mismatches": [m.to_dict() for m in report.mismatches],
        "warnings": report.warnings,
        "hard_stop_reasons": report.hard_stop_reasons,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def reconcile_legacy_migration(
    connection: sqlite3.Connection,
    *,
    expect_approved_baseline: bool = True,
) -> ReconciliationReport:
    """Full Phase 8E reconciliation against an open (preferably read-only) connection.

    When ``expect_approved_baseline`` is True (production Phase 8E), also enforce
    the frozen Phase 8D approved counts/batch. Tests may pass False.
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    mismatches: list[Mismatch] = []
    warnings: list[str] = []
    hard: list[str] = []
    sections: dict[str, Any] = {}

    repo = CatalogRepository(connection)
    plan = plan_legacy_migration(connection)
    importable = [s for s in plan.services if s.classification != "BLOCKED"]

    # ── A Service identity ──
    bridges = list(
        connection.execute(f"SELECT * FROM {LEGACY_SERVICE_BRIDGE_TABLE}").fetchall()
    )
    bridge_by_legacy = {str(r["legacy_catalog_id"]): r for r in bridges}
    bridge_svc_ids = [str(r["soldium_service_id"]) for r in bridges]
    catalog_svc_count = int(
        connection.execute("SELECT COUNT(*) AS n FROM soldium_catalog_services").fetchone()[
            "n"
        ]
    )
    dup_bridge_legacy = [
        k for k, v in Counter(str(r["legacy_catalog_id"]) for r in bridges).items() if v > 1
    ]
    dup_bridge_svc = [k for k, v in Counter(bridge_svc_ids).items() if v > 1]

    missing_bridges: list[str] = []
    orphan_bridges: list[str] = []
    wrong_svc_id: list[str] = []

    for planned in importable:
        expected_svc = planned.planned_soldium_service_id
        br = bridge_by_legacy.get(planned.legacy_catalog_id)
        if br is None:
            missing_bridges.append(planned.legacy_catalog_id)
            _mismatch(
                mismatches,
                entity=f"service:{planned.legacy_catalog_id}",
                legacy_value=planned.legacy_catalog_id,
                migrated_value=None,
                classification="HARD_STOP",
                why="migrated candidate missing service bridge",
            )
            continue
        actual_svc = str(br["soldium_service_id"])
        if actual_svc != expected_svc:
            wrong_svc_id.append(planned.legacy_catalog_id)
            _mismatch(
                mismatches,
                entity=f"service:{planned.legacy_catalog_id}",
                legacy_value=expected_svc,
                migrated_value=actual_svc,
                classification="HARD_STOP",
                why="bridge soldium_service_id != deterministic uuid5 identity",
            )
        if repo.get_service(actual_svc) is None:
            orphan_bridges.append(planned.legacy_catalog_id)
            _mismatch(
                mismatches,
                entity=f"bridge:{planned.legacy_catalog_id}",
                legacy_value=actual_svc,
                migrated_value=None,
                classification="HARD_STOP",
                why="orphan bridge — Catalog service missing",
            )

    # Bridges not in candidate set
    planned_legacy_ids = {s.legacy_catalog_id for s in importable}
    extra_bridges = [lid for lid in bridge_by_legacy if lid not in planned_legacy_ids]
    for lid in extra_bridges:
        _mismatch(
            mismatches,
            entity=f"bridge:{lid}",
            legacy_value=lid,
            migrated_value=str(bridge_by_legacy[lid]["soldium_service_id"]),
            classification="HARD_STOP",
            why="bridge for non-candidate / unexpected legacy id",
        )

    # Catalog services without bridge (among all catalog services)
    unbridged_services: list[str] = []
    for row in connection.execute("SELECT id FROM soldium_catalog_services"):
        sid = str(row["id"])
        if sid not in bridge_svc_ids:
            unbridged_services.append(sid)
            _mismatch(
                mismatches,
                entity=f"catalog_service:{sid}",
                legacy_value=None,
                migrated_value=sid,
                classification="HARD_STOP",
                why="Catalog service has no legacy bridge",
            )

    identity_ok = (
        len(bridges) == len(importable)
        and catalog_svc_count == len(importable)
        and not missing_bridges
        and not orphan_bridges
        and not dup_bridge_legacy
        and not dup_bridge_svc
        and not wrong_svc_id
        and not extra_bridges
        and not unbridged_services
    )
    if expect_approved_baseline and plan.candidate_count != APPROVED_CANDIDATE_COUNT:
        identity_ok = False
        hard.append(
            f"candidate_count {plan.candidate_count} != approved {APPROVED_CANDIDATE_COUNT}"
        )
    if not identity_ok:
        hard.append("service identity reconciliation failed")

    sections["service_identity"] = {
        "legacy_candidates": plan.candidate_count,
        "bridge_count": len(bridges),
        "catalog_service_count": catalog_svc_count,
        "missing_bridges": missing_bridges,
        "orphan_bridges": orphan_bridges,
        "duplicate_legacy_catalog_ids": dup_bridge_legacy,
        "duplicate_soldium_service_ids": dup_bridge_svc,
        "wrong_service_ids": wrong_svc_id,
        "extra_bridges": extra_bridges,
        "unbridged_catalog_services": unbridged_services,
        "ok": identity_ok,
    }

    # ── B Node / placement ──
    node_bridges = list(
        connection.execute(f"SELECT * FROM {LEGACY_NODE_BRIDGE_TABLE}").fetchall()
    )
    node_bridge_by_key = {str(r["legacy_node_key"]): r for r in node_bridges}

    platform_nodes = sum(1 for n in plan.nodes if n.level == "platform")
    section_nodes = sum(1 for n in plan.nodes if n.level == "section")
    subsection_nodes = sum(1 for n in plan.nodes if n.level == "subsection")

    # Reconstruct expected keys from candidates and compare to node bridges
    expected_keys: set[str] = set()
    for s in importable:
        pk = s.platform_key
        expected_keys.add(legacy_node_key_platform(pk))
        if s.section_key:
            expected_keys.add(legacy_node_key_section(pk, s.section_key))
            if s.subsection_key:
                expected_keys.add(
                    legacy_node_key_subsection(pk, s.section_key, s.subsection_key)
                )

    missing_node_bridges = sorted(expected_keys - set(node_bridge_by_key))
    extra_node_bridges = sorted(set(node_bridge_by_key) - expected_keys)
    for k in missing_node_bridges:
        _mismatch(
            mismatches,
            entity=f"node_bridge:{k}",
            legacy_value=k,
            migrated_value=None,
            classification="HARD_STOP",
            why="expected node bridge missing",
        )
    for k in extra_node_bridges:
        _mismatch(
            mismatches,
            entity=f"node_bridge:{k}",
            legacy_value=None,
            migrated_value=k,
            classification="HARD_STOP",
            why="unexpected node bridge",
        )

    orphan_node_bridges: list[str] = []
    for key, br in node_bridge_by_key.items():
        node_id = str(br["soldium_node_id"])
        entry_id = str(br["soldium_entry_id"])
        if repo.get_node(node_id) is None:
            orphan_node_bridges.append(key)
            _mismatch(
                mismatches,
                entity=f"node_bridge:{key}",
                legacy_value=node_id,
                migrated_value=None,
                classification="HARD_STOP",
                why="orphan node bridge — node missing",
            )
        entry = repo.get_entry(entry_id)
        if entry is None or entry.entry_type != "node" or entry.node_id != node_id:
            orphan_node_bridges.append(key)
            _mismatch(
                mismatches,
                entity=f"node_bridge:{key}",
                legacy_value=entry_id,
                migrated_value=str(entry.entry_type) if entry else None,
                classification="HARD_STOP",
                why="orphan/invalid node entry on bridge",
            )

    placement_counts = Counter()
    placement_issues = 0
    for planned in importable:
        br = bridge_by_legacy.get(planned.legacy_catalog_id)
        if not br:
            continue
        sid = str(br["soldium_service_id"])
        entry = repo.get_entry_for_service(sid)
        if entry is None:
            placement_issues += 1
            _mismatch(
                mismatches,
                entity=f"placement:{sid}",
                legacy_value=planned.planned_parent_node_key,
                migrated_value=None,
                classification="HARD_STOP",
                why="service missing Catalog entry",
            )
            continue
        if entry.entry_type != "service" or entry.service_id != sid:
            placement_issues += 1
            _mismatch(
                mismatches,
                entity=f"placement:{sid}",
                legacy_value="service",
                migrated_value=entry.entry_type,
                classification="HARD_STOP",
                why="invalid service entry type",
            )
            continue
        parent = entry.parent_entry_id
        if parent is None:
            placement_issues += 1
            _mismatch(
                mismatches,
                entity=f"placement:{sid}",
                legacy_value=planned.deepest_parent_type,
                migrated_value="root",
                classification="HARD_STOP",
                why="service attached at catalog root unexpectedly",
            )
            continue
        parent_entry = repo.get_entry(parent)
        if parent_entry is None or parent_entry.entry_type != "node":
            placement_issues += 1
            _mismatch(
                mismatches,
                entity=f"placement:{sid}",
                legacy_value=planned.planned_parent_node_key,
                migrated_value=parent,
                classification="HARD_STOP",
                why="service parent is not a node entry",
            )
            continue
        # Resolve parent legacy key via node bridge reverse
        parent_node_id = parent_entry.node_id
        parent_key = None
        for k, nbr in node_bridge_by_key.items():
            if str(nbr["soldium_node_id"]) == parent_node_id:
                parent_key = k
                break
        if parent_key != planned.planned_parent_node_key:
            placement_issues += 1
            _mismatch(
                mismatches,
                entity=f"placement:{sid}",
                legacy_value=planned.planned_parent_node_key,
                migrated_value=parent_key,
                classification="HARD_STOP",
                why="service attached to wrong node vs approved plan",
            )
        placement_counts[planned.deepest_parent_type or "unknown"] += 1

    # Duplicate service entries
    dup_service_entries = connection.execute(
        """
        SELECT service_id, COUNT(*) AS n
        FROM soldium_catalog_entries
        WHERE entry_type='service' AND service_id IS NOT NULL
        GROUP BY service_id HAVING n > 1
        """
    ).fetchall()
    for row in dup_service_entries:
        placement_issues += 1
        _mismatch(
            mismatches,
            entity=f"placement:{row['service_id']}",
            legacy_value=1,
            migrated_value=int(row["n"]),
            classification="HARD_STOP",
            why="duplicate service placement entries",
        )

    catalog_node_count = int(
        connection.execute("SELECT COUNT(*) AS n FROM soldium_catalog_nodes").fetchone()["n"]
    )
    node_entry_count = int(
        connection.execute(
            "SELECT COUNT(*) AS n FROM soldium_catalog_entries WHERE entry_type='node'"
        ).fetchone()["n"]
    )
    service_entry_count = int(
        connection.execute(
            "SELECT COUNT(*) AS n FROM soldium_catalog_entries WHERE entry_type='service'"
        ).fetchone()["n"]
    )

    placement_ok = (
        catalog_node_count == len(plan.nodes)
        and len(node_bridges) == len(plan.nodes)
        and not missing_node_bridges
        and not extra_node_bridges
        and not orphan_node_bridges
        and placement_issues == 0
        and not dup_service_entries
        and node_entry_count == len(plan.nodes)
        and service_entry_count == len(importable)
    )
    if expect_approved_baseline:
        if not (
            platform_nodes == EXPECTED_PLATFORM_NODES
            and section_nodes == EXPECTED_SECTION_NODES
            and subsection_nodes == EXPECTED_SUBSECTION_NODES
            and placement_counts.get("section", 0) == EXPECTED_SECTION_SERVICES
            and placement_counts.get("subsection", 0) == EXPECTED_SUBSECTION_SERVICES
            and placement_counts.get("platform", 0) == EXPECTED_PLATFORM_SERVICES
            and catalog_node_count == APPROVED_NODE_COUNT
        ):
            placement_ok = False
    if not placement_ok:
        hard.append("node/placement reconciliation failed")

    sections["node_placement"] = {
        "platform_nodes": platform_nodes,
        "section_nodes": section_nodes,
        "subsection_nodes": subsection_nodes,
        "catalog_node_count": catalog_node_count,
        "node_bridge_count": len(node_bridges),
        "node_entries": node_entry_count,
        "service_entries": service_entry_count,
        "section_level_services": placement_counts.get("section", 0),
        "subsection_level_services": placement_counts.get("subsection", 0),
        "platform_level_services": placement_counts.get("platform", 0),
        "missing_node_bridges": missing_node_bridges,
        "extra_node_bridges": extra_node_bridges,
        "orphan_node_bridges": orphan_node_bridges,
        "ok": placement_ok,
    }

    # ── C Commercial ──
    commercial_mismatches = 0
    per_unit_count = 0
    sentinel_count = 0
    admin_count = 0
    for planned in importable:
        br = bridge_by_legacy.get(planned.legacy_catalog_id)
        if not br:
            continue
        sid = str(br["soldium_service_id"])
        svc = repo.get_service(sid)
        if not svc:
            continue
        if svc.service_type != "other":
            commercial_mismatches += 1
            _mismatch(
                mismatches,
                entity=f"commercial:{sid}",
                legacy_value="other",
                migrated_value=svc.service_type,
                classification="HARD_STOP",
                why="service_type must remain other",
            )
        if svc.ordering_mode != "quantity_based":
            commercial_mismatches += 1
            _mismatch(
                mismatches,
                entity=f"commercial:{sid}",
                legacy_value="quantity_based",
                migrated_value=svc.ordering_mode,
                classification="HARD_STOP",
                why="ordering_mode must remain quantity_based (no fixed_package/package_based)",
            )
        if int(svc.min_quantity) != int(planned.min_quantity or -1):
            commercial_mismatches += 1
            _mismatch(
                mismatches,
                entity=f"commercial:{sid}",
                legacy_value=planned.min_quantity,
                migrated_value=svc.min_quantity,
                classification="HARD_STOP",
                why="min_quantity mismatch",
            )
        if int(svc.max_quantity) != int(planned.max_quantity or -1):
            commercial_mismatches += 1
            _mismatch(
                mismatches,
                entity=f"commercial:{sid}",
                legacy_value=planned.max_quantity,
                migrated_value=svc.max_quantity,
                classification="HARD_STOP",
                why="max_quantity mismatch / sentinel altered",
            )
        if planned.category.strip() == "per_unit":
            per_unit_count += 1
            if planned.pricing_mode != "per_unit":
                commercial_mismatches += 1
                _mismatch(
                    mismatches,
                    entity=f"commercial:{sid}",
                    legacy_value="per_unit",
                    migrated_value=planned.pricing_mode,
                    classification="HARD_STOP",
                    why="per_unit category did not map to per_unit pricing mode",
                )
        if planned.max_quantity is not None and is_max_qty_sentinel(int(planned.max_quantity)):
            sentinel_count += 1
        if planned.fulfillment_mode == "admin":
            admin_count += 1
        # Bridge review codes
        try:
            bridge_codes = json.loads(str(br["review_codes"] or "[]"))
        except json.JSONDecodeError:
            bridge_codes = []
            commercial_mismatches += 1
            _mismatch(
                mismatches,
                entity=f"bridge_review:{sid}",
                legacy_value=planned.review_codes,
                migrated_value=br["review_codes"],
                classification="HARD_STOP",
                why="review_codes JSON unreadable",
            )
        if sorted(bridge_codes) != sorted(planned.review_codes):
            commercial_mismatches += 1
            _mismatch(
                mismatches,
                entity=f"bridge_review:{sid}",
                legacy_value=planned.review_codes,
                migrated_value=bridge_codes,
                classification="HARD_STOP",
                why="bridge review_codes != approved plan review codes",
            )
        if str(br["legacy_fulfillment_mode"] or "") != planned.fulfillment_mode:
            commercial_mismatches += 1
            _mismatch(
                mismatches,
                entity=f"bridge_fulfillment:{sid}",
                legacy_value=planned.fulfillment_mode,
                migrated_value=br["legacy_fulfillment_mode"],
                classification="HARD_STOP",
                why="legacy_fulfillment_mode not preserved on bridge",
            )

    plan_per_unit = sum(1 for s in importable if s.category.strip() == "per_unit")
    plan_sentinel = sum(
        1
        for s in importable
        if s.max_quantity is not None and is_max_qty_sentinel(int(s.max_quantity))
    )
    plan_admin = sum(1 for s in importable if s.fulfillment_mode == "admin")

    commercial_ok = (
        commercial_mismatches == 0
        and per_unit_count == plan_per_unit
        and sentinel_count == plan_sentinel
        and admin_count == plan_admin
    )
    if expect_approved_baseline and (
        per_unit_count != 7 or sentinel_count != 26 or admin_count != 7
    ):
        commercial_ok = False
        hard.append("approved commercial aggregate mismatch")
    if not commercial_ok:
        hard.append("commercial data reconciliation failed")

    sections["commercial"] = {
        "min_max_mismatches": commercial_mismatches,
        "per_unit_count": per_unit_count,
        "sentinel_count": sentinel_count,
        "admin_count": admin_count,
        "plan_per_unit": plan_per_unit,
        "plan_sentinel": plan_sentinel,
        "plan_admin": plan_admin,
        "expected_review_bridges": sum(
            1 for r in bridges if str(r["classification"]) == "REQUIRES_REVIEW"
        ),
        "ok": commercial_ok,
    }

    # ── D Prices ──
    active_prices = list(
        connection.execute(
            "SELECT * FROM soldium_catalog_prices WHERE status='active'"
        ).fetchall()
    )
    price_by_svc = {str(r["service_id"]): r for r in active_prices}
    dup_active = connection.execute(
        """
        SELECT service_id, COUNT(*) AS n FROM soldium_catalog_prices
        WHERE status='active' GROUP BY service_id HAVING n > 1
        """
    ).fetchall()
    price_issues = 0
    normalized_verified = 0
    normalized_failed = 0
    for planned in importable:
        br = bridge_by_legacy.get(planned.legacy_catalog_id)
        if not br:
            continue
        sid = str(br["soldium_service_id"])
        prow = price_by_svc.get(sid)
        if prow is None:
            price_issues += 1
            _mismatch(
                mismatches,
                entity=f"price:{sid}",
                legacy_value=planned.normalized_price_dh,
                migrated_value=None,
                classification="HARD_STOP",
                why="missing active price",
            )
            continue
        millimes = int(prow["amount_millimes"])
        currency = str(prow["currency"] or "")
        mode = str(prow["pricing_mode"] or "")
        if currency != "MAD":
            price_issues += 1
            _mismatch(
                mismatches,
                entity=f"price:{sid}",
                legacy_value="MAD",
                migrated_value=currency,
                classification="HARD_STOP",
                why="currency mismatch",
            )
        if mode != planned.pricing_mode:
            price_issues += 1
            _mismatch(
                mismatches,
                entity=f"price:{sid}",
                legacy_value=planned.pricing_mode,
                migrated_value=mode,
                classification="HARD_STOP",
                why="pricing_mode mismatch",
            )
        if millimes != int(planned.amount_millimes or -1):
            price_issues += 1
            _mismatch(
                mismatches,
                entity=f"price:{sid}",
                legacy_value=planned.amount_millimes,
                migrated_value=millimes,
                classification="HARD_STOP",
                why="amount_millimes mismatch vs approved normalized plan",
            )
        if millimes <= 0:
            price_issues += 1
            _mismatch(
                mismatches,
                entity=f"price:{sid}",
                legacy_value=planned.legacy_price_dh,
                migrated_value=millimes,
                classification="HARD_STOP",
                why="non-positive price",
            )
        if planned.price_normalized:
            # Recompute ROUND_HALF_UP independently
            _n, text, applied = normalize_legacy_price_dh(planned.legacy_price_dh)
            expected_m = int((_n * Decimal(1000)).to_integral_value())
            if applied and millimes == expected_m and text == planned.normalized_price_dh:
                normalized_verified += 1
            else:
                normalized_failed += 1
                price_issues += 1
                _mismatch(
                    mismatches,
                    entity=f"price_normalized:{sid}",
                    legacy_value={
                        "legacy": planned.legacy_price_dh,
                        "expected_norm": text,
                        "expected_m": expected_m,
                    },
                    migrated_value={
                        "stored_norm": planned.normalized_price_dh,
                        "millimes": millimes,
                    },
                    classification="HARD_STOP",
                    why="normalized price failed ROUND_HALF_UP verification",
                )

    for row in dup_active:
        price_issues += 1
        _mismatch(
            mismatches,
            entity=f"price:{row['service_id']}",
            legacy_value=1,
            migrated_value=int(row["n"]),
            classification="HARD_STOP",
            why="duplicate active prices",
        )

    plan_normalized = sum(1 for s in importable if s.price_normalized)
    prices_ok = (
        len(active_prices) == len(importable)
        and price_issues == 0
        and normalized_verified == plan_normalized
        and normalized_failed == 0
        and not dup_active
    )
    if expect_approved_baseline and (
        len(active_prices) != APPROVED_CANDIDATE_COUNT
        or normalized_verified != EXPECTED_NORMALIZED_PRICES
    ):
        prices_ok = False
    if not prices_ok:
        hard.append("price reconciliation failed")

    sections["prices"] = {
        "total_active_prices": len(active_prices),
        "normalized_prices": normalized_verified,
        "plan_normalized": plan_normalized,
        "normalized_failed": normalized_failed,
        "invalid_or_mismatched": price_issues,
        "duplicate_active": len(dup_active),
        "ok": prices_ok,
    }

    # ── E Execution sources ──
    active_sources = list(
        connection.execute(
            "SELECT * FROM soldium_catalog_execution_sources WHERE status='active'"
        ).fetchall()
    )
    source_by_svc = {str(r["service_id"]): r for r in active_sources}
    dup_sources = connection.execute(
        """
        SELECT service_id, COUNT(*) AS n FROM soldium_catalog_execution_sources
        WHERE status='active' GROUP BY service_id HAVING n > 1
        """
    ).fetchall()
    source_issues = 0
    missing_expected = 0
    unexpected_present = 0
    for planned in importable:
        br = bridge_by_legacy.get(planned.legacy_catalog_id)
        if not br:
            continue
        sid = str(br["soldium_service_id"])
        src = source_by_svc.get(sid)
        if planned.planned_execution_source == "present":
            if src is None:
                missing_expected += 1
                source_issues += 1
                _mismatch(
                    mismatches,
                    entity=f"source:{sid}",
                    legacy_value={
                        "slug": planned.provider_slug,
                        "account": planned.provider_api_account,
                        "external": planned.external_service_id,
                    },
                    migrated_value=None,
                    classification="HARD_STOP",
                    why="expected execution source missing",
                )
            else:
                if str(src["provider_slug"]) != planned.provider_slug:
                    source_issues += 1
                    _mismatch(
                        mismatches,
                        entity=f"source:{sid}",
                        legacy_value=planned.provider_slug,
                        migrated_value=src["provider_slug"],
                        classification="HARD_STOP",
                        why="provider_slug mismatch",
                    )
                if str(src["provider_account_key"]) != (planned.provider_api_account or ""):
                    source_issues += 1
                    _mismatch(
                        mismatches,
                        entity=f"source:{sid}",
                        legacy_value=planned.provider_api_account,
                        migrated_value=src["provider_account_key"],
                        classification="HARD_STOP",
                        why="provider_account_key mismatch",
                    )
                # opaque TEXT — compare as string, no int cast
                if str(src["external_service_id"]) != planned.external_service_id:
                    source_issues += 1
                    _mismatch(
                        mismatches,
                        entity=f"source:{sid}",
                        legacy_value=planned.external_service_id,
                        migrated_value=src["external_service_id"],
                        classification="HARD_STOP",
                        why="external_service_id mismatch",
                    )
        elif planned.planned_execution_source == "omitted":
            if src is not None:
                unexpected_present += 1
                source_issues += 1
                _mismatch(
                    mismatches,
                    entity=f"source:{sid}",
                    legacy_value="omitted (missing account)",
                    migrated_value=dict(src),
                    classification="HARD_STOP",
                    why="fake/unexpected source for missing-account service",
                )

    for row in dup_sources:
        source_issues += 1
        _mismatch(
            mismatches,
            entity=f"source:{row['service_id']}",
            legacy_value=1,
            migrated_value=int(row["n"]),
            classification="HARD_STOP",
            why="duplicate active execution source",
        )

    plan_sources = sum(1 for s in importable if s.planned_execution_source == "present")
    plan_omitted = sum(1 for s in importable if s.planned_execution_source == "omitted")
    sources_ok = (
        len(active_sources) == plan_sources
        and missing_expected == 0
        and unexpected_present == 0
        and source_issues == 0
        and not dup_sources
        and (len(importable) - len(active_sources)) == plan_omitted
    )
    if expect_approved_baseline and (
        len(active_sources) != APPROVED_EXEC_SOURCE_COUNT
        or plan_omitted != EXPECTED_MISSING_SOURCES
    ):
        sources_ok = False
    if not sources_ok:
        hard.append("execution source reconciliation failed")

    sections["execution_sources"] = {
        "active_source_count": len(active_sources),
        "missing_source_count": len(importable) - len(active_sources),
        "plan_present": plan_sources,
        "plan_omitted": plan_omitted,
        "missing_where_expected_present": missing_expected,
        "unexpected_present_for_omitted": unexpected_present,
        "mismatches": source_issues,
        "ok": sources_ok,
    }

    # ── F Readiness ──
    ready_count = 0
    needs_review_count = 0
    blocking_codes: Counter[str] = Counter()
    unexpected_readiness = 0
    for planned in importable:
        br = bridge_by_legacy.get(planned.legacy_catalog_id)
        if not br:
            continue
        sid = str(br["soldium_service_id"])
        svc = repo.get_service(sid)
        if not svc:
            continue
        entry = repo.get_entry_for_service(sid)
        if entry:
            svc.entry_id = entry.id
            svc.parent_entry_id = entry.parent_entry_id
        source = repo.get_active_execution_source(sid)
        price = repo.get_active_price(sid)
        result = evaluate_service_readiness(repo, svc, source=source, price=price)
        if result.ready:
            ready_count += 1
            if planned.planned_execution_source == "omitted":
                unexpected_readiness += 1
                _mismatch(
                    mismatches,
                    entity=f"readiness:{sid}",
                    legacy_value="needs_review (no source)",
                    migrated_value="ready",
                    classification="HARD_STOP",
                    why="service without source marked ready",
                )
        else:
            needs_review_count += 1
            for issue in result.issues:
                blocking_codes[issue.code] += 1
            if planned.planned_execution_source == "present":
                # Should generally be ready; any needs_review is unexpected for fully migrated SAFE/review-with-source
                unexpected_readiness += 1
                _mismatch(
                    mismatches,
                    entity=f"readiness:{sid}",
                    legacy_value="ready expected (has source+price+placement)",
                    migrated_value=[i.code for i in result.issues],
                    classification="HARD_STOP",
                    why="unexpected needs_review despite complete migrated data",
                )
            elif "missing_execution_source" not in {i.code for i in result.issues}:
                unexpected_readiness += 1
                _mismatch(
                    mismatches,
                    entity=f"readiness:{sid}",
                    legacy_value="missing_execution_source",
                    migrated_value=[i.code for i in result.issues],
                    classification="HARD_STOP",
                    why="omitted-source service lacks expected missing_execution_source issue",
                )

    readiness_ok = (
        ready_count == plan_sources
        and needs_review_count == plan_omitted
        and unexpected_readiness == 0
    )
    if expect_approved_baseline and (
        ready_count != APPROVED_EXEC_SOURCE_COUNT
        or needs_review_count != EXPECTED_MISSING_SOURCES
    ):
        readiness_ok = False
    if not readiness_ok:
        hard.append("readiness reconciliation failed")

    sections["readiness"] = {
        "ready": ready_count,
        "needs_review": needs_review_count,
        "blocking_codes": dict(blocking_codes),
        "unexpected_states": unexpected_readiness,
        "ok": readiness_ok,
    }

    # ── G Bridge integrity + batch ──
    batch_svc = [
        str(r["migration_batch_id"])
        for r in bridges
        if str(r["migration_batch_id"]) != EXPECTED_MIGRATION_BATCH_ID
    ]
    batch_node = [
        str(r["migration_batch_id"])
        for r in node_bridges
        if str(r["migration_batch_id"]) != EXPECTED_MIGRATION_BATCH_ID
    ]
    if expect_approved_baseline:
        for lid in batch_svc[:20]:
            _mismatch(
                mismatches,
                entity="service_bridge_batch",
                legacy_value=EXPECTED_MIGRATION_BATCH_ID,
                migrated_value=lid,
                classification="HARD_STOP",
                why="service bridge not from Phase 8D batch",
            )
        for lid in batch_node[:20]:
            _mismatch(
                mismatches,
                entity="node_bridge_batch",
                legacy_value=EXPECTED_MIGRATION_BATCH_ID,
                migrated_value=lid,
                classification="HARD_STOP",
                why="node bridge not from Phase 8D batch",
            )

    bridge_ok = (
        len(bridges) == len(importable)
        and len(node_bridges) == len(plan.nodes)
        and not dup_bridge_legacy
        and not dup_bridge_svc
    )
    if expect_approved_baseline:
        if batch_svc or batch_node:
            bridge_ok = False
        if len(bridges) != APPROVED_CANDIDATE_COUNT or len(node_bridges) != APPROVED_NODE_COUNT:
            bridge_ok = False
    if not bridge_ok:
        hard.append("bridge integrity failed")

    sections["bridge_integrity"] = {
        "service_bridges": len(bridges),
        "node_bridges": len(node_bridges),
        "wrong_batch_service_bridges": len(batch_svc) if expect_approved_baseline else 0,
        "wrong_batch_node_bridges": len(batch_node) if expect_approved_baseline else 0,
        "expected_batch_id": EXPECTED_MIGRATION_BATCH_ID,
        "ok": bridge_ok,
    }

    # ── H Migration batch ──
    run_row = connection.execute(
        """
        SELECT * FROM soldium_catalog_legacy_migration_runs
        WHERE migration_batch_id = ?
        """,
        (EXPECTED_MIGRATION_BATCH_ID,),
    ).fetchone()
    review_bridges = sum(
        1 for r in bridges if str(r["classification"]) == "REQUIRES_REVIEW"
    )
    pubs = int(
        connection.execute("SELECT COUNT(*) AS n FROM soldium_catalog_publications").fetchone()[
            "n"
        ]
    )
    maps = int(
        connection.execute(
            "SELECT COUNT(*) AS n FROM soldium_provider_service_mappings"
        ).fetchone()["n"]
    )

    batch_ok = True
    if expect_approved_baseline:
        if run_row is None:
            batch_ok = False
            hard.append("migration batch record missing")
            _mismatch(
                mismatches,
                entity="migration_batch",
                legacy_value=EXPECTED_MIGRATION_BATCH_ID,
                migrated_value=None,
                classification="HARD_STOP",
                why="Phase 8D migration run row missing",
            )
        else:
            status = str(run_row["status"] or "")
            fp = str(run_row["plan_fingerprint"] or "")
            if status != "success":
                batch_ok = False
                hard.append(f"migration batch status={status}")
            if fp != APPROVED_PLAN_FINGERPRINT:
                batch_ok = False
                hard.append("migration batch fingerprint mismatch")
                _mismatch(
                    mismatches,
                    entity="migration_batch_fingerprint",
                    legacy_value=APPROVED_PLAN_FINGERPRINT,
                    migrated_value=fp,
                    classification="HARD_STOP",
                    why="stored plan fingerprint != approved",
                )

        counts_match = (
            catalog_node_count == 59
            and node_entry_count == 59
            and catalog_svc_count == 253
            and service_entry_count == 253
            and len(active_prices) == 253
            and len(active_sources) == 248
            and len(bridges) == 253
            and len(node_bridges) == 59
            and review_bridges == 34
            and pubs == 0
            and maps == 0
        )
        if not counts_match:
            batch_ok = False
            hard.append("approved Phase 8D count mismatch")
    else:
        counts_match = pubs == 0 and maps == 0
        if not counts_match:
            batch_ok = False

    sections["migration_batch"] = {
        "batch_id": EXPECTED_MIGRATION_BATCH_ID if expect_approved_baseline else None,
        "status": str(run_row["status"]) if run_row else None,
        "plan_fingerprint": str(run_row["plan_fingerprint"]) if run_row else None,
        "approved_fingerprint": APPROVED_PLAN_FINGERPRINT,
        "expect_approved_baseline": expect_approved_baseline,
        "counts": {
            "nodes": catalog_node_count,
            "node_entries": node_entry_count,
            "services": catalog_svc_count,
            "service_entries": service_entry_count,
            "prices": len(active_prices),
            "execution_sources": len(active_sources),
            "service_bridges": len(bridges),
            "node_bridges": len(node_bridges),
            "review_bridges": review_bridges,
            "publications": pubs,
            "provider_mappings": maps,
        },
        "ok": batch_ok,
    }
    if not batch_ok:
        hard.append("migration batch integrity failed")

    # ── I Legacy isolation ──
    smm_count = int(connection.execute("SELECT COUNT(*) AS n FROM smm_services").fetchone()["n"])
    try:
        orders_count = int(connection.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"])
    except sqlite3.Error:
        orders_count = -1
    legacy_ids = [
        str(r[0])
        for r in connection.execute(
            "SELECT catalog_id FROM smm_services ORDER BY catalog_id"
        ).fetchall()
    ]
    legacy_hash = hashlib.sha256("|".join(legacy_ids).encode("utf-8")).hexdigest()
    # Compare to fingerprint captured at 8D: 5232a044...
    EXPECTED_LEGACY_HASH = (
        "5232a044c2deb76c758df1852691dbe01087595f58905bd684663a9a6944730e"
    )
    legacy_ok = True
    if expect_approved_baseline:
        legacy_ok = (
            smm_count == APPROVED_SMM_SERVICES_COUNT
            and orders_count == APPROVED_ORDERS_COUNT
            and legacy_hash == EXPECTED_LEGACY_HASH
        )
        if not legacy_ok:
            hard.append("legacy isolation failed")
            if smm_count != APPROVED_SMM_SERVICES_COUNT:
                _mismatch(
                    mismatches,
                    entity="smm_services_count",
                    legacy_value=APPROVED_SMM_SERVICES_COUNT,
                    migrated_value=smm_count,
                    classification="HARD_STOP",
                    why="smm_services row count changed",
                )
            if orders_count != APPROVED_ORDERS_COUNT:
                _mismatch(
                    mismatches,
                    entity="orders_count",
                    legacy_value=APPROVED_ORDERS_COUNT,
                    migrated_value=orders_count,
                    classification="HARD_STOP",
                    why="orders row count changed",
                )
            if legacy_hash != EXPECTED_LEGACY_HASH:
                _mismatch(
                    mismatches,
                    entity="smm_catalog_id_hash",
                    legacy_value=EXPECTED_LEGACY_HASH,
                    migrated_value=legacy_hash,
                    classification="HARD_STOP",
                    why="smm_services catalog_id set changed",
                )

    sections["legacy_isolation"] = {
        "smm_services_count": smm_count,
        "orders_count": orders_count,
        "smm_catalog_id_hash": legacy_hash,
        "ok": legacy_ok,
    }

    # ── J Publication / mapping ──
    published_events = pubs
    active_mappings = maps
    pub_ok = published_events == 0 and active_mappings == 0
    if not pub_ok:
        hard.append("publication/mapping isolation failed")
        if published_events != 0:
            _mismatch(
                mismatches,
                entity="publications",
                legacy_value=0,
                migrated_value=published_events,
                classification="HARD_STOP",
                why="unexpected publication rows",
            )
        if active_mappings != 0:
            _mismatch(
                mismatches,
                entity="provider_mappings",
                legacy_value=0,
                migrated_value=active_mappings,
                classification="HARD_STOP",
                why="unexpected provider mapping rows",
            )

    sections["publication_mapping_isolation"] = {
        "publications": published_events,
        "provider_mappings": active_mappings,
        "ok": pub_ok,
    }

    # Verdict
    if hard or any(m.classification == "HARD_STOP" for m in mismatches):
        verdict: Verdict = "RECONCILIATION FAILED — HARD STOP"
    elif warnings:
        verdict = "RECONCILIATION PASSED WITH WARNINGS"
    else:
        verdict = "RECONCILIATION PASSED"

    report = ReconciliationReport(
        generated_at=generated_at,
        verdict=verdict,
        sections=sections,
        mismatches=mismatches,
        warnings=warnings,
        hard_stop_reasons=list(dict.fromkeys(hard)),
    )
    report.content_fingerprint = _content_fingerprint(report)
    sections["determinism"] = {
        "content_fingerprint": report.content_fingerprint,
        "note": "compare two runs excluding generated_at",
    }
    return report


def reconcile_legacy_migration_at_path(
    db_path: Path | str | None = None,
    *,
    expect_approved_baseline: bool = True,
) -> ReconciliationReport:
    with catalog_readonly_connection(resolve_db_path(db_path)) as conn:
        return reconcile_legacy_migration(
            conn, expect_approved_baseline=expect_approved_baseline
        )


def reconcile_twice(
    db_path: Path | str | None = None,
    *,
    expect_approved_baseline: bool = True,
) -> tuple[ReconciliationReport, ReconciliationReport, bool]:
    """Run reconciliation twice; return (r1, r2, fingerprints_equal)."""
    r1 = reconcile_legacy_migration_at_path(
        db_path, expect_approved_baseline=expect_approved_baseline
    )
    r2 = reconcile_legacy_migration_at_path(
        db_path, expect_approved_baseline=expect_approved_baseline
    )
    return r1, r2, r1.content_fingerprint == r2.content_fingerprint
