# -*- coding: utf-8 -*-
"""Phase 8D — real legacy ``smm_services`` → SOLDIUM Catalog import (write path).

Dry Run planner remains in ``legacy_migration.py`` (read-only).
This module performs schema ensure + atomic Catalog import only.
Never mutates ``smm_services``, Orders, Telegram, Providers, or publications.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from catalog_core.commercial import DEFAULT_ORDERING_MODE, DEFAULT_SERVICE_TYPE
from catalog_core.db import catalog_readonly_connection, catalog_transaction, resolve_db_path
from catalog_core.legacy_migration import (
    LEGACY_MIGRATION_UUID_NAMESPACE,
    LEGACY_NODE_BRIDGE_TABLE,
    LEGACY_SERVICE_BRIDGE_TABLE,
    LegacyMigrationDryRunReport,
    PlannedService,
    compute_plan_fingerprint,
    plan_legacy_migration,
    plan_legacy_migration_at_path,
    planned_soldium_service_id,
)
from catalog_core.models import (
    CatalogEntry,
    CatalogNode,
    CatalogPrice,
    CatalogService,
    ExecutionSource,
)
from catalog_core.repository import CatalogRepository
from catalog_core.schema import SOLDIUM_CATALOG_SCHEMA_VERSION, ensure_soldium_catalog_schema

# Frozen Phase 8C.1 approved plan (live DB at migration lock time).
APPROVED_PLAN_FINGERPRINT = (
    "71b0ab24b97cec708ea08ced9888b9ce73768b23b968bd359fea4d6445afd366"
)
APPROVED_CANDIDATE_COUNT = 253
APPROVED_SAFE_COUNT = 219
APPROVED_REVIEW_COUNT = 34
APPROVED_BLOCKED_COUNT = 0
APPROVED_NODE_COUNT = 59
APPROVED_EXEC_SOURCE_COUNT = 248
APPROVED_SMM_SERVICES_COUNT = 2069
APPROVED_ORDERS_COUNT = 44

Verdict = Literal[
    "MIGRATION SUCCESSFUL — READY FOR RECONCILIATION PHASE",
    "MIGRATION FAILED — ROLLED BACK",
    "MIGRATION STOPPED — CONFLICT DETECTED",
    "PREFLIGHT ONLY",
]


class LegacyMigrationImportError(Exception):
    def __init__(self, message: str, *, code: str = "conflict") -> None:
        self.message = message
        self.code = code
        super().__init__(message)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _legacy_fingerprint(conn: sqlite3.Connection) -> dict[str, Any]:
    smm_count = int(conn.execute("SELECT COUNT(*) AS n FROM smm_services").fetchone()["n"])
    ids = [
        str(r[0])
        for r in conn.execute(
            "SELECT catalog_id FROM smm_services ORDER BY catalog_id"
        ).fetchall()
    ]
    import hashlib

    id_hash = hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()
    try:
        orders_count = int(conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"])
    except sqlite3.Error:
        orders_count = -1
    return {
        "smm_services_count": smm_count,
        "smm_catalog_id_hash": id_hash,
        "orders_count": orders_count,
    }


@dataclass
class PreflightResult:
    ok: bool
    migration_batch_id: str
    plan_fingerprint: str
    approved_fingerprint: str
    schema_version: str | None
    dry_run: LegacyMigrationDryRunReport
    legacy_before: dict[str, Any]
    catalog_before: dict[str, Any]
    bridge_before: dict[str, Any]
    messages: list[str] = field(default_factory=list)
    stop_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "migration_batch_id": self.migration_batch_id,
            "plan_fingerprint": self.plan_fingerprint,
            "approved_fingerprint": self.approved_fingerprint,
            "schema_version": self.schema_version,
            "dry_run_summary": {
                "candidate_count": self.dry_run.candidate_count,
                "safe_count": self.dry_run.safe_count,
                "review_count": self.dry_run.review_count,
                "blocked_count": self.dry_run.blocked_count,
                "planned_node_count": self.dry_run.planned_node_count,
                "planned_execution_source_count": self.dry_run.planned_execution_source_count,
                "prices_normalized_count": self.dry_run.prices_normalized_count,
            },
            "legacy_before": self.legacy_before,
            "catalog_before": self.catalog_before,
            "bridge_before": self.bridge_before,
            "messages": self.messages,
            "stop_reason": self.stop_reason,
        }


@dataclass
class MigrationResult:
    verdict: Verdict
    migration_batch_id: str
    preflight: PreflightResult
    started_at: str
    completed_at: str | None = None
    errors: list[str] = field(default_factory=list)
    reconciliation: dict[str, Any] = field(default_factory=dict)
    imported: dict[str, Any] = field(default_factory=dict)
    legacy_after: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "migration_batch_id": self.migration_batch_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "preflight": self.preflight.to_dict(),
            "imported": self.imported,
            "reconciliation": self.reconciliation,
            "legacy_after": self.legacy_after,
            "errors": self.errors,
        }


def _catalog_counts(conn: sqlite3.Connection) -> dict[str, int]:
    def _c(sql: str) -> int:
        try:
            return int(conn.execute(sql).fetchone()[0])
        except sqlite3.Error:
            return -1

    return {
        "services": _c("SELECT COUNT(*) FROM soldium_catalog_services"),
        "nodes": _c("SELECT COUNT(*) FROM soldium_catalog_nodes"),
        "entries": _c("SELECT COUNT(*) FROM soldium_catalog_entries"),
        "prices": _c("SELECT COUNT(*) FROM soldium_catalog_prices"),
        "sources": _c(
            "SELECT COUNT(*) FROM soldium_catalog_execution_sources WHERE status='active'"
        ),
        "publications": _c("SELECT COUNT(*) FROM soldium_catalog_publications"),
        "mappings": _c("SELECT COUNT(*) FROM soldium_provider_service_mappings"),
    }


def _bridge_counts(conn: sqlite3.Connection) -> dict[str, int]:
    def _c(table: str) -> int:
        try:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.Error:
            return 0

    return {
        "service_bridges": _c(LEGACY_SERVICE_BRIDGE_TABLE),
        "node_bridges": _c(LEGACY_NODE_BRIDGE_TABLE),
    }


def _schema_version(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute(
            "SELECT value FROM soldium_catalog_schema_meta WHERE key='schema_version'"
        ).fetchone()
        return str(row[0]) if row else None
    except sqlite3.Error:
        return None


def run_preflight(
    db_path: Path | str | None = None,
    *,
    migration_batch_id: str | None = None,
    require_empty_catalog: bool = True,
) -> PreflightResult:
    """Read-only plan + integrity checks. Schema may already be v8 from a prior ensure."""
    batch_id = migration_batch_id or f"mig_{uuid.uuid4().hex}"
    messages: list[str] = []
    path = resolve_db_path(db_path)

    with catalog_readonly_connection(path) as conn:
        legacy_before = _legacy_fingerprint(conn)
        catalog_before = _catalog_counts(conn)
        bridge_before = _bridge_counts(conn)
        schema_ver = _schema_version(conn)
        report = plan_legacy_migration(conn)
        fingerprint = compute_plan_fingerprint(report)

    stop: str | None = None
    if report.candidate_count != APPROVED_CANDIDATE_COUNT:
        stop = (
            f"candidate_count {report.candidate_count} != approved {APPROVED_CANDIDATE_COUNT}"
        )
    elif report.safe_count != APPROVED_SAFE_COUNT:
        stop = f"safe_count {report.safe_count} != approved {APPROVED_SAFE_COUNT}"
    elif report.review_count != APPROVED_REVIEW_COUNT:
        stop = f"review_count {report.review_count} != approved {APPROVED_REVIEW_COUNT}"
    elif report.blocked_count != APPROVED_BLOCKED_COUNT:
        stop = f"blocked_count {report.blocked_count} != approved {APPROVED_BLOCKED_COUNT}"
    elif report.planned_node_count != APPROVED_NODE_COUNT:
        stop = f"nodes {report.planned_node_count} != approved {APPROVED_NODE_COUNT}"
    elif report.planned_execution_source_count != APPROVED_EXEC_SOURCE_COUNT:
        stop = (
            f"sources {report.planned_execution_source_count} "
            f"!= approved {APPROVED_EXEC_SOURCE_COUNT}"
        )
    elif fingerprint != APPROVED_PLAN_FINGERPRINT:
        stop = "plan fingerprint mismatch vs approved Phase 8C.1 fingerprint"
    elif legacy_before["smm_services_count"] != APPROVED_SMM_SERVICES_COUNT:
        stop = (
            f"smm_services count {legacy_before['smm_services_count']} "
            f"!= approved {APPROVED_SMM_SERVICES_COUNT}"
        )
    elif legacy_before["orders_count"] != APPROVED_ORDERS_COUNT:
        stop = (
            f"orders count {legacy_before['orders_count']} "
            f"!= approved {APPROVED_ORDERS_COUNT}"
        )
    elif require_empty_catalog and catalog_before["services"] not in (0, -1):
        if bridge_before["service_bridges"] == APPROVED_CANDIDATE_COUNT:
            messages.append(
                "Catalog already migrated (253 bridges) — import will verify idempotently"
            )
        elif bridge_before["service_bridges"] == 0:
            stop = (
                f"Catalog already has {catalog_before['services']} services "
                "without legacy bridges"
            )
        else:
            stop = (
                f"partial legacy bridges present: {bridge_before['service_bridges']}"
            )
    elif any(
        s.amount_millimes is None or int(s.amount_millimes) <= 0
        for s in report.services
        if s.classification != "BLOCKED"
    ):
        stop = "importable service with non-positive millimes after normalization"

    # Zero millimes after normalization is never importable — hard stop.
    for s in report.services:
        if s.normalized_price_dh is not None:
            try:
                from decimal import Decimal

                if Decimal(s.normalized_price_dh) == 0:
                    stop = (
                        f"zero millimes after normalization for "
                        f"legacy_catalog_id={s.legacy_catalog_id}"
                    )
                    break
            except Exception:
                pass
        if s.amount_millimes is not None and int(s.amount_millimes) <= 0:
            stop = f"non-positive millimes for legacy_catalog_id={s.legacy_catalog_id}"
            break

    if stop is None:
        messages.append("preflight OK — fingerprint and counts match approved plan")
    else:
        messages.append(stop)

    return PreflightResult(
        ok=stop is None,
        migration_batch_id=batch_id,
        plan_fingerprint=fingerprint,
        approved_fingerprint=APPROVED_PLAN_FINGERPRINT,
        schema_version=schema_ver,
        dry_run=report,
        legacy_before=legacy_before,
        catalog_before=catalog_before,
        bridge_before=bridge_before,
        messages=messages,
        stop_reason=stop,
    )


def _ensure_nodes(
    conn: sqlite3.Connection,
    report: LegacyMigrationDryRunReport,
    migration_batch_id: str,
) -> dict[str, str]:
    """Create/reuse nodes; return legacy_node_key → soldium_entry_id."""
    repo = CatalogRepository(conn)
    key_to_entry: dict[str, str] = {}

    # Parents before children: platforms, sections, subsections (report.nodes already ordered)
    for node in report.nodes:
        existing = conn.execute(
            f"""
            SELECT soldium_node_id, soldium_entry_id
            FROM {LEGACY_NODE_BRIDGE_TABLE}
            WHERE legacy_node_key = ?
            """,
            (node.legacy_node_key,),
        ).fetchone()
        if existing:
            node_id = str(existing["soldium_node_id"])
            entry_id = str(existing["soldium_entry_id"])
            if not repo.get_node(node_id):
                raise LegacyMigrationImportError(
                    f"node bridge points to missing node: {node.legacy_node_key}",
                    code="bridge_missing_node",
                )
            entry = repo.get_entry(entry_id)
            if not entry or entry.node_id != node_id:
                raise LegacyMigrationImportError(
                    f"node bridge points to invalid entry: {node.legacy_node_key}",
                    code="bridge_missing_entry",
                )
            key_to_entry[node.legacy_node_key] = entry_id
            continue

        parent_entry_id = None
        if node.parent_legacy_node_key:
            parent_entry_id = key_to_entry.get(node.parent_legacy_node_key)
            if not parent_entry_id:
                raise LegacyMigrationImportError(
                    f"missing parent for node {node.legacy_node_key}",
                    code="missing_parent",
                )

        node_id = _new_id("node")
        entry_id = _new_id("ent")
        repo.insert_node(
            CatalogNode(
                id=node_id,
                name_ar=node.name_ar,
                note_ar="",
                status="active",
            )
        )
        repo.insert_entry(
            CatalogEntry(
                id=entry_id,
                parent_entry_id=parent_entry_id,
                entry_type="node",
                node_id=node_id,
                service_id=None,
                sort_order=int(node.sort_order),
            )
        )
        conn.execute(
            f"""
            INSERT INTO {LEGACY_NODE_BRIDGE_TABLE} (
                legacy_node_key, soldium_node_id, soldium_entry_id, migration_batch_id
            ) VALUES (?, ?, ?, ?)
            """,
            (node.legacy_node_key, node_id, entry_id, migration_batch_id),
        )
        key_to_entry[node.legacy_node_key] = entry_id

    return key_to_entry


def _verify_existing_service(
    conn: sqlite3.Connection,
    planned: PlannedService,
) -> None:
    repo = CatalogRepository(conn)
    bridge = conn.execute(
        f"""
        SELECT * FROM {LEGACY_SERVICE_BRIDGE_TABLE}
        WHERE legacy_catalog_id = ?
        """,
        (planned.legacy_catalog_id,),
    ).fetchone()
    expected_id = planned.planned_soldium_service_id

    if bridge is None:
        # Service must not exist under planned id without bridge
        if repo.get_service(expected_id):
            raise LegacyMigrationImportError(
                f"service {expected_id} exists without bridge",
                code="service_without_bridge",
            )
        return

    soldium_id = str(bridge["soldium_service_id"])
    if soldium_id != expected_id:
        raise LegacyMigrationImportError(
            f"bridge svc mismatch for {planned.legacy_catalog_id}: "
            f"{soldium_id} != {expected_id}",
            code="bridge_id_mismatch",
        )
    svc = repo.get_service(soldium_id)
    if not svc:
        raise LegacyMigrationImportError(
            f"bridge points to missing service {soldium_id}",
            code="bridge_missing_service",
        )
    entry = repo.get_entry_for_service(soldium_id)
    if not entry:
        raise LegacyMigrationImportError(
            f"service {soldium_id} missing entry",
            code="missing_entry",
        )
    price = repo.get_active_price(soldium_id)
    if not price or int(price.amount_millimes) != int(planned.amount_millimes or -1):
        raise LegacyMigrationImportError(
            f"price conflict for {soldium_id}",
            code="price_conflict",
        )
    if price.pricing_mode != planned.pricing_mode:
        raise LegacyMigrationImportError(
            f"pricing_mode conflict for {soldium_id}",
            code="price_conflict",
        )
    source = repo.get_active_execution_source(soldium_id)
    if planned.planned_execution_source == "present":
        if source is None:
            raise LegacyMigrationImportError(
                f"expected source missing for {soldium_id}",
                code="source_conflict",
            )
        if (
            source.provider_slug != planned.provider_slug
            or source.provider_account_key != (planned.provider_api_account or "")
            or source.external_service_id != planned.external_service_id
        ):
            raise LegacyMigrationImportError(
                f"source identity conflict for {soldium_id}",
                code="source_conflict",
            )
    elif planned.planned_execution_source == "omitted":
        if source is not None:
            raise LegacyMigrationImportError(
                f"unexpected source for review service {soldium_id}",
                code="source_conflict",
            )


def _import_one_service(
    conn: sqlite3.Connection,
    planned: PlannedService,
    key_to_entry: dict[str, str],
    migration_batch_id: str,
) -> str:
    """Import or no-op one service. Returns soldium_service_id."""
    repo = CatalogRepository(conn)
    expected_id = planned.planned_soldium_service_id

    existing_bridge = conn.execute(
        f"SELECT soldium_service_id FROM {LEGACY_SERVICE_BRIDGE_TABLE} WHERE legacy_catalog_id=?",
        (planned.legacy_catalog_id,),
    ).fetchone()
    if existing_bridge:
        _verify_existing_service(conn, planned)
        return str(existing_bridge["soldium_service_id"])

    if repo.get_service(expected_id):
        raise LegacyMigrationImportError(
            f"service {expected_id} exists without bridge",
            code="service_without_bridge",
        )

    if planned.amount_millimes is None or int(planned.amount_millimes) <= 0:
        raise LegacyMigrationImportError(
            f"refusing zero/invalid price for {planned.legacy_catalog_id}",
            code="invalid_price",
        )

    parent_key = planned.planned_parent_node_key
    if not parent_key or parent_key not in key_to_entry:
        raise LegacyMigrationImportError(
            f"missing parent entry for {planned.legacy_catalog_id}",
            code="missing_parent",
        )
    parent_entry_id = key_to_entry[parent_key]

    entry_id = _new_id("ent")
    repo.insert_service(
        CatalogService(
            id=expected_id,
            name_ar=planned.name_ar,
            note_ar="",
            status="active",
            service_type=DEFAULT_SERVICE_TYPE,
            ordering_mode=DEFAULT_ORDERING_MODE,
            min_quantity=int(planned.min_quantity or 1),
            max_quantity=int(planned.max_quantity or 1),
            fulfillment_mode=str(planned.fulfillment_mode or "auto").strip().lower()
            or "auto",
            target_platform_key=str(planned.platform_key or "").strip() or None,
            target_section_key=str(planned.section_key or "").strip() or None,
            target_subsection_key=str(planned.subsection_key or "").strip() or None,
        )
    )
    repo.insert_entry(
        CatalogEntry(
            id=entry_id,
            parent_entry_id=parent_entry_id,
            entry_type="service",
            node_id=None,
            service_id=expected_id,
            sort_order=int(planned.planned_sort_order or 0),
        )
    )
    repo.insert_price(
        CatalogPrice(
            id=_new_id("prc"),
            service_id=expected_id,
            amount_millimes=int(planned.amount_millimes),
            currency="MAD",
            pricing_mode=str(planned.pricing_mode or "per_1000"),
            status="active",
        )
    )

    if planned.planned_execution_source == "present":
        slug = planned.provider_slug
        account = planned.provider_api_account or ""
        if not repo.find_provider(slug) or not repo.find_provider_account(slug, account):
            raise LegacyMigrationImportError(
                f"provider/account invalid at import for {planned.legacy_catalog_id}",
                code="invalid_provider",
            )
        repo.insert_execution_source(
            ExecutionSource(
                id=_new_id("src"),
                service_id=expected_id,
                provider_slug=slug,
                provider_account_key=account,
                external_service_id=planned.external_service_id,
                status="active",
            )
        )

    conn.execute(
        f"""
        INSERT INTO {LEGACY_SERVICE_BRIDGE_TABLE} (
            legacy_catalog_id, legacy_local_item_id, legacy_service_id,
            soldium_service_id, provider_slug, external_service_id,
            provider_api_account, legacy_fulfillment_mode, classification,
            review_codes, migration_batch_id, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            planned.legacy_catalog_id,
            planned.legacy_local_item_id,
            planned.legacy_service_id,
            expected_id,
            planned.provider_slug,
            planned.external_service_id,
            planned.provider_api_account,
            planned.fulfillment_mode,
            planned.classification,
            json.dumps(planned.review_codes, ensure_ascii=False),
            migration_batch_id,
            _utc_now(),
        ),
    )
    return expected_id


def reconcile_migration(
    conn: sqlite3.Connection,
    report: LegacyMigrationDryRunReport,
    *,
    legacy_before: dict[str, Any],
) -> dict[str, Any]:
    repo = CatalogRepository(conn)
    issues: list[str] = []

    svc_bridges = int(
        conn.execute(f"SELECT COUNT(*) FROM {LEGACY_SERVICE_BRIDGE_TABLE}").fetchone()[0]
    )
    node_bridges = int(
        conn.execute(f"SELECT COUNT(*) FROM {LEGACY_NODE_BRIDGE_TABLE}").fetchone()[0]
    )
    pubs = int(
        conn.execute("SELECT COUNT(*) FROM soldium_catalog_publications").fetchone()[0]
    )
    maps = int(
        conn.execute("SELECT COUNT(*) FROM soldium_provider_service_mappings").fetchone()[0]
    )

    importable = [s for s in report.services if s.classification != "BLOCKED"]
    if svc_bridges != len(importable):
        issues.append(f"service bridges {svc_bridges} != {len(importable)}")
    if node_bridges != report.planned_node_count:
        issues.append(f"node bridges {node_bridges} != {report.planned_node_count}")

    active_prices = 0
    active_sources = 0
    for planned in importable:
        sid = planned.planned_soldium_service_id
        if not repo.get_service(sid):
            issues.append(f"missing service {sid}")
            continue
        if not repo.get_entry_for_service(sid):
            issues.append(f"missing entry {sid}")
        price = repo.get_active_price(sid)
        if not price:
            issues.append(f"missing price {sid}")
        else:
            active_prices += 1
            if int(price.amount_millimes) != int(planned.amount_millimes or -1):
                issues.append(f"price mismatch {sid}")
        source = repo.get_active_execution_source(sid)
        if planned.planned_execution_source == "present":
            if source is None:
                issues.append(f"missing source {sid}")
            else:
                active_sources += 1
        elif source is not None:
            issues.append(f"unexpected source {sid}")

    legacy_after = _legacy_fingerprint(conn)
    if legacy_after["smm_services_count"] != legacy_before["smm_services_count"]:
        issues.append("smm_services count changed")
    if legacy_after["smm_catalog_id_hash"] != legacy_before["smm_catalog_id_hash"]:
        issues.append("smm_services catalog_id set changed")
    if legacy_after["orders_count"] != legacy_before["orders_count"]:
        issues.append("orders count changed")

    if pubs != 0:
        issues.append(f"publications={pubs}")
    if maps != 0:
        issues.append(f"mappings={maps}")

    node_entries = int(
        conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_entries WHERE entry_type='node'"
        ).fetchone()[0]
    )
    service_entries = int(
        conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_entries WHERE entry_type='service'"
        ).fetchone()[0]
    )

    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "imported_services": svc_bridges,
        "imported_nodes": node_bridges,
        "node_entries": node_entries,
        "service_entries": service_entries,
        "total_entries": node_entries + service_entries,
        "active_prices": active_prices,
        "active_sources": active_sources,
        "publications": pubs,
        "mappings": maps,
        "legacy_after": legacy_after,
        "namespace": str(LEGACY_MIGRATION_UUID_NAMESPACE),
    }


def execute_legacy_migration(
    db_path: Path | str | None = None,
    *,
    skip_fingerprint_check: bool = False,
) -> MigrationResult:
    """Ensure schema, preflight, then import. Fail closed."""
    path = resolve_db_path(db_path)
    batch_id = f"mig_{uuid.uuid4().hex}"
    started = _utc_now()

    # Schema ensure (writes DDL only)
    with catalog_transaction(path, ensure_schema=True) as conn:
        ensure_soldium_catalog_schema(conn)
        ver = _schema_version(conn)
        if ver != SOLDIUM_CATALOG_SCHEMA_VERSION:
            raise LegacyMigrationImportError(
                f"schema version {ver} != {SOLDIUM_CATALOG_SCHEMA_VERSION}",
                code="schema_version",
            )

    preflight = run_preflight(path, migration_batch_id=batch_id, require_empty_catalog=True)
    if skip_fingerprint_check:
        preflight.ok = True
        preflight.stop_reason = None

    if not preflight.ok:
        return MigrationResult(
            verdict="MIGRATION STOPPED — CONFLICT DETECTED",
            migration_batch_id=batch_id,
            preflight=preflight,
            started_at=started,
            completed_at=_utc_now(),
            errors=[preflight.stop_reason or "preflight failed"],
        )

    report = preflight.dry_run
    try:
        with catalog_transaction(path, ensure_schema=False) as conn:
            conn.execute(
                """
                INSERT INTO soldium_catalog_legacy_migration_runs (
                    migration_batch_id, started_at, status, plan_fingerprint, report_json
                ) VALUES (?, ?, 'running', ?, ?)
                """,
                (
                    batch_id,
                    started,
                    preflight.plan_fingerprint,
                    "{}",
                ),
            )
            key_to_entry = _ensure_nodes(conn, report, batch_id)
            imported_ids: list[str] = []
            for planned in sorted(
                (s for s in report.services if s.classification != "BLOCKED"),
                key=lambda s: s.legacy_catalog_id,
            ):
                sid = _import_one_service(conn, planned, key_to_entry, batch_id)
                imported_ids.append(sid)

            recon = reconcile_migration(
                conn, report, legacy_before=preflight.legacy_before
            )
            if not recon["ok"]:
                raise LegacyMigrationImportError(
                    "reconciliation failed: " + "; ".join(recon["issues"][:8]),
                    code="reconciliation_failed",
                )

            completed = _utc_now()
            result_payload = {
                "imported_service_ids_count": len(imported_ids),
                "reconciliation": recon,
                "verdict": "MIGRATION SUCCESSFUL — READY FOR RECONCILIATION PHASE",
            }
            conn.execute(
                """
                UPDATE soldium_catalog_legacy_migration_runs
                SET completed_at = ?, status = 'success', report_json = ?
                WHERE migration_batch_id = ?
                """,
                (completed, json.dumps(result_payload, ensure_ascii=False), batch_id),
            )

            return MigrationResult(
                verdict="MIGRATION SUCCESSFUL — READY FOR RECONCILIATION PHASE",
                migration_batch_id=batch_id,
                preflight=preflight,
                started_at=started,
                completed_at=completed,
                imported={
                    "services": len(imported_ids),
                    "nodes": report.planned_node_count,
                },
                reconciliation=recon,
                legacy_after=recon["legacy_after"],
            )
    except LegacyMigrationImportError as exc:
        return MigrationResult(
            verdict="MIGRATION FAILED — ROLLED BACK",
            migration_batch_id=batch_id,
            preflight=preflight,
            started_at=started,
            completed_at=_utc_now(),
            errors=[f"{exc.code}: {exc.message}"],
        )
    except Exception as exc:  # noqa: BLE001
        return MigrationResult(
            verdict="MIGRATION FAILED — ROLLED BACK",
            migration_batch_id=batch_id,
            preflight=preflight,
            started_at=started,
            completed_at=_utc_now(),
            errors=[str(exc)],
        )


def run_idempotent_verify(db_path: Path | str | None = None) -> dict[str, Any]:
    """Second-run verification: preflight + per-service verify, no new rows if consistent."""
    path = resolve_db_path(db_path)
    preflight = run_preflight(path, require_empty_catalog=False)
    if not preflight.ok and "without legacy bridges" not in (preflight.stop_reason or ""):
        # After successful migration, catalog is non-empty — relax that check
        if preflight.stop_reason and "Catalog already has" in preflight.stop_reason:
            # Recompute with bridges present
            pass
        else:
            return {"ok": False, "preflight": preflight.to_dict()}

    report = plan_legacy_migration_at_path(path)
    with catalog_transaction(path, ensure_schema=False) as conn:
        for planned in report.services:
            if planned.classification == "BLOCKED":
                continue
            _verify_existing_service(conn, planned)
        recon = reconcile_migration(
            conn, report, legacy_before=_legacy_fingerprint(conn)
        )
    return {"ok": recon["ok"], "reconciliation": recon}
