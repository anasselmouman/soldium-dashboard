# -*- coding: utf-8 -*-
"""Phase 8C — read-only legacy ``smm_services`` → Catalog migration Dry Run.

This module NEVER writes. It only SELECTs and returns a deterministic plan.
Bridge tables / real import belong to a later phase.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid5

from catalog_core.commercial import DEFAULT_ORDERING_MODE, DEFAULT_SERVICE_TYPE
from catalog_core.db import catalog_readonly_connection
from catalog_core.errors import CatalogValidationError
from catalog_core.pricing import dh_to_millimes
from catalog_core.repository import CatalogRepository

# ---------------------------------------------------------------------------
# Fixed identity namespace — NEVER change once used for real imports.
# ---------------------------------------------------------------------------
LEGACY_MIGRATION_UUID_NAMESPACE = UUID("a7c3e8f0-5b2d-4e91-9c4a-1f6d8b0e3a27")

# Phase 8C.1 — monetary normalization to Catalog millime precision (3 dp MAD).
PRICE_NORMALIZATION_QUANTUM = Decimal("0.001")
PRICE_NORMALIZATION_ROUNDING = ROUND_HALF_UP
PRICE_NORMALIZATION_ROUNDING_NAME = "ROUND_HALF_UP"

Classification = Literal["SAFE", "REQUIRES_REVIEW", "BLOCKED"]

LEGACY_SERVICE_BRIDGE_TABLE = "soldium_catalog_legacy_bridge"
LEGACY_NODE_BRIDGE_TABLE = "soldium_catalog_legacy_node_bridge"

# Quantity sentinel heuristic (literal preserved; review only).
_MAX_QTY_SENTINEL_EXACT = 2_147_483_647
_MAX_QTY_SENTINEL_MIN = 1_000_000_000

_WRITE_SQL = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|ATTACH|DETACH|"
    r"VACUUM|REINDEX|ANALYZE)\b",
    re.IGNORECASE,
)


def planned_soldium_service_id(legacy_catalog_id: str) -> str:
    """Deterministic SOLDIUM service id from legacy catalog_id (uuid5)."""
    key = f"legacy_catalog_id:{legacy_catalog_id}"
    return f"svc_{uuid5(LEGACY_MIGRATION_UUID_NAMESPACE, key).hex}"


def legacy_node_key_platform(platform_key: str) -> str:
    return f"platform:{platform_key}"


def legacy_node_key_section(platform_key: str, section_key: str) -> str:
    return f"section:{platform_key}/{section_key}"


def legacy_node_key_subsection(
    platform_key: str, section_key: str, subsection_key: str
) -> str:
    return f"subsection:{platform_key}/{section_key}/{subsection_key}"


def is_max_qty_sentinel(max_qty: int) -> bool:
    return max_qty == _MAX_QTY_SENTINEL_EXACT or max_qty >= _MAX_QTY_SENTINEL_MIN


def dh_text_for_catalog_pricing(raw: object, *, cast_text: str | None = None) -> str:
    """Safest DH string given SQLite REAL / TEXT (pre-normalization source text)."""
    if cast_text is not None:
        text = str(cast_text).strip()
        if text != "":
            return text
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return ""
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, Decimal):
        return format(raw, "f")
    if isinstance(raw, str):
        return raw.strip()
    try:
        return format(Decimal(str(raw)), "f")
    except (InvalidOperation, ValueError, TypeError):
        return str(raw).strip()


def normalize_legacy_price_dh(price_text: str) -> tuple[Decimal, str, bool]:
    """Normalize a legacy DH string to Catalog millime precision (3 decimal places).

    Uses Decimal + ROUND_HALF_UP only — never float for the rounding decision.
    Returns (normalized_decimal, normalized_text, rounding_applied).
    """
    amount = Decimal(str(price_text).strip())
    normalized = amount.quantize(
        PRICE_NORMALIZATION_QUANTUM, rounding=PRICE_NORMALIZATION_ROUNDING
    )
    rounding_applied = amount != normalized
    # Stable textual form without scientific notation.
    normalized_text = format(normalized, "f")
    return normalized, normalized_text, rounding_applied


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    keys = row.keys()
    if key not in keys:
        return default
    return row[key]


@dataclass
class PlannedNode:
    legacy_node_key: str
    level: str  # platform | section | subsection
    name_ar: str
    parent_legacy_node_key: str | None
    sort_order: int
    platform_key: str
    section_key: str | None = None
    subsection_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlannedService:
    legacy_catalog_id: str
    legacy_local_item_id: str
    legacy_service_id: str
    planned_soldium_service_id: str
    classification: Classification
    review_codes: list[str] = field(default_factory=list)
    blocking_codes: list[str] = field(default_factory=list)
    name_ar: str = ""
    platform_key: str = ""
    platform_title: str = ""
    section_key: str | None = None
    section_title: str | None = None
    subsection_key: str | None = None
    subsection_title: str | None = None
    structure_path_keys: list[str] = field(default_factory=list)
    planned_parent_node_key: str | None = None
    planned_sort_order: int | None = None
    min_quantity: int | None = None
    max_quantity: int | None = None
    category: str = ""
    legacy_price_dh: str = ""
    local_price_dh_source: str = ""
    normalized_price_dh: str | None = None
    amount_millimes: int | None = None
    pricing_mode: str | None = None
    rounding_applied: bool = False
    price_normalized: bool = False
    price_error: str | None = None
    deepest_parent_type: str | None = None
    service_type: str = DEFAULT_SERVICE_TYPE
    ordering_mode: str = DEFAULT_ORDERING_MODE
    provider_slug: str = ""
    provider_api_account: str | None = None
    external_service_id: str = ""
    fulfillment_mode: str = ""
    planned_execution_source: Literal["present", "omitted", "none"] = "none"
    planned_actions: list[str] = field(default_factory=list)
    existing_catalog: dict[str, Any] = field(default_factory=dict)
    note: str = "migration_ordering_only"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LegacyMigrationDryRunReport:
    generated_at: str
    source: dict[str, str]
    candidate_count: int
    inactive_inventory_count: int
    safe_count: int
    review_count: int
    blocked_count: int
    planned_service_count: int
    planned_node_count: int
    planned_node_entry_count: int
    planned_service_entry_count: int
    planned_total_entry_count: int
    planned_price_count: int
    planned_execution_source_count: int
    planned_bridge_count: int
    prices_normalized_count: int
    publication_count: int
    provider_mapping_count: int
    legacy_write_count: int
    order_write_count: int
    telegram_write_count: int
    bridge_state: str
    node_bridge_state: str
    identity_namespace: str
    price_normalization: dict[str, Any]
    migration_ordering_note: str
    placement: dict[str, Any]
    nodes: list[PlannedNode]
    services: list[PlannedService]
    groups: dict[str, Any]
    safety: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "source": self.source,
            "candidate_count": self.candidate_count,
            "inactive_inventory_count": self.inactive_inventory_count,
            "safe_count": self.safe_count,
            "review_count": self.review_count,
            "blocked_count": self.blocked_count,
            "planned_service_count": self.planned_service_count,
            "planned_node_count": self.planned_node_count,
            "planned_node_entry_count": self.planned_node_entry_count,
            "planned_service_entry_count": self.planned_service_entry_count,
            "planned_total_entry_count": self.planned_total_entry_count,
            "planned_price_count": self.planned_price_count,
            "planned_execution_source_count": self.planned_execution_source_count,
            "planned_bridge_count": self.planned_bridge_count,
            "prices_normalized_count": self.prices_normalized_count,
            "publication_count": self.publication_count,
            "provider_mapping_count": self.provider_mapping_count,
            "legacy_write_count": self.legacy_write_count,
            "order_write_count": self.order_write_count,
            "telegram_write_count": self.telegram_write_count,
            "bridge_state": self.bridge_state,
            "node_bridge_state": self.node_bridge_state,
            "identity_namespace": self.identity_namespace,
            "price_normalization": self.price_normalization,
            "migration_ordering_note": self.migration_ordering_note,
            "placement": self.placement,
            "nodes": [n.to_dict() for n in self.nodes],
            "services": [s.to_dict() for s in self.services],
            "groups": self.groups,
            "safety": self.safety,
        }


class _ReadOnlyCursor:
    """Cursor proxy that rejects mutating SQL (defense in depth for Dry Run)."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        if _WRITE_SQL.search(sql or ""):
            raise sqlite3.OperationalError(
                f"legacy migration dry-run forbids write SQL: {sql[:80]}"
            )
        return self._cursor.execute(sql, parameters)

    def executemany(self, sql: str, seq: Any) -> sqlite3.Cursor:
        if _WRITE_SQL.search(sql or ""):
            raise sqlite3.OperationalError(
                f"legacy migration dry-run forbids write SQL: {sql[:80]}"
            )
        return self._cursor.executemany(sql, seq)

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    def __iter__(self) -> Any:
        return iter(self._cursor)


class ReadOnlyConnection:
    """Thin wrapper: SELECT-only execute surface for Dry Run planning."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self.row_factory = connection.row_factory

    def execute(self, sql: str, parameters: Any = ()) -> _ReadOnlyCursor:
        if _WRITE_SQL.search(sql or ""):
            raise sqlite3.OperationalError(
                f"legacy migration dry-run forbids write SQL: {sql[:80]}"
            )
        return _ReadOnlyCursor(self._conn.execute(sql, parameters))

    def executescript(self, sql: str) -> None:
        raise sqlite3.OperationalError("legacy migration dry-run forbids executescript")


def _classify_and_plan_price(
    category: str, price_text: str
) -> dict[str, Any]:
    """Plan price with explicit 3-dp MAD normalization (ROUND_HALF_UP).

    >3 decimal places are normalized — not blocked.
    Zero after normalization / negative / malformed remain blocked.
    """
    mode = "per_unit" if category.strip() == "per_unit" else "per_1000"
    result: dict[str, Any] = {
        "legacy_price_dh": price_text,
        "normalized_price_dh": None,
        "amount_millimes": None,
        "pricing_mode": mode,
        "rounding_applied": False,
        "price_normalized": False,
        "price_error": None,
        "blocking_codes": [],
    }
    if price_text == "":
        result["blocking_codes"] = ["invalid_price"]
        result["price_error"] = "empty price"
        return result
    try:
        amount = Decimal(str(price_text).strip())
    except (InvalidOperation, ValueError):
        result["blocking_codes"] = ["invalid_price"]
        result["price_error"] = "unparseable price"
        return result
    if amount.is_nan() or amount.is_infinite():
        result["blocking_codes"] = ["invalid_price"]
        result["price_error"] = "non-finite price"
        return result
    if amount < 0:
        result["blocking_codes"] = ["invalid_price"]
        result["price_error"] = "negative price"
        return result

    try:
        _normalized, normalized_text, rounding_applied = normalize_legacy_price_dh(
            str(price_text).strip()
        )
    except (InvalidOperation, ValueError) as exc:
        result["blocking_codes"] = ["invalid_price"]
        result["price_error"] = str(exc)
        return result

    result["normalized_price_dh"] = normalized_text
    result["rounding_applied"] = rounding_applied
    result["price_normalized"] = rounding_applied

    try:
        millimes = dh_to_millimes(normalized_text)
    except CatalogValidationError as exc:
        msg = str(exc.message if hasattr(exc, "message") else exc)
        result["blocking_codes"] = ["invalid_price"]
        result["price_error"] = msg
        return result

    result["amount_millimes"] = millimes
    # Never infer fixed_package.
    return result


def _provider_checks(
    repo: CatalogRepository,
    provider_slug: str,
    account: str | None,
) -> tuple[list[str], list[str], bool]:
    """Return (blocking_codes, review_codes, account_valid_for_source)."""
    blocking: list[str] = []
    review: list[str] = []
    slug = (provider_slug or "").strip()
    if not slug:
        blocking.append("missing_provider_slug")
        return blocking, review, False
    if repo.find_provider(slug) is None:
        blocking.append("provider_not_found")
        return blocking, review, False

    acct = (account or "").strip()
    if not acct:
        review.append("missing_provider_account")
        return blocking, review, False
    if repo.find_provider_account(slug, acct) is None:
        review.append("invalid_provider_account")
        return blocking, review, False
    return blocking, review, True


def _structure_for_row(
    platform_key: str,
    platform_title: str,
    section_key: str | None,
    section_title: str | None,
    subsection_key: str | None,
    subsection_title: str | None,
) -> tuple[list[str], str, dict[str, PlannedNode]]:
    """Build node key path and deepest parent; return provisional nodes (sort later)."""
    nodes: dict[str, PlannedNode] = {}
    pk = platform_key
    plat_key = legacy_node_key_platform(pk)
    nodes[plat_key] = PlannedNode(
        legacy_node_key=plat_key,
        level="platform",
        name_ar=(platform_title or pk).strip() or pk,
        parent_legacy_node_key=None,
        sort_order=0,
        platform_key=pk,
    )
    path = [plat_key]
    deepest = plat_key

    sk = (section_key or "").strip()
    if sk:
        sec_key = legacy_node_key_section(pk, sk)
        nodes[sec_key] = PlannedNode(
            legacy_node_key=sec_key,
            level="section",
            name_ar=(section_title or sk).strip() or sk,
            parent_legacy_node_key=plat_key,
            sort_order=0,
            platform_key=pk,
            section_key=sk,
        )
        path.append(sec_key)
        deepest = sec_key

        ssk = (subsection_key or "").strip()
        if ssk:
            sub_key = legacy_node_key_subsection(pk, sk, ssk)
            nodes[sub_key] = PlannedNode(
                legacy_node_key=sub_key,
                level="subsection",
                name_ar=(subsection_title or ssk).strip() or ssk,
                parent_legacy_node_key=sec_key,
                sort_order=0,
                platform_key=pk,
                section_key=sk,
                subsection_key=ssk,
            )
            path.append(sub_key)
            deepest = sub_key

    return path, deepest, nodes


def _assign_node_sort_orders(nodes: dict[str, PlannedNode]) -> list[PlannedNode]:
    """Deterministic migration ordering for nodes (not business-approved)."""
    platforms = sorted(
        [n for n in nodes.values() if n.level == "platform"],
        key=lambda n: n.platform_key,
    )
    for i, n in enumerate(platforms):
        n.sort_order = i

    sections = sorted(
        [n for n in nodes.values() if n.level == "section"],
        key=lambda n: (n.platform_key, n.section_key or ""),
    )
    by_parent: dict[str | None, list[PlannedNode]] = defaultdict(list)
    for n in sections:
        by_parent[n.parent_legacy_node_key].append(n)
    for parent, kids in by_parent.items():
        for i, n in enumerate(kids):
            n.sort_order = i

    subsections = sorted(
        [n for n in nodes.values() if n.level == "subsection"],
        key=lambda n: (n.platform_key, n.section_key or "", n.subsection_key or ""),
    )
    by_parent_sub: dict[str | None, list[PlannedNode]] = defaultdict(list)
    for n in subsections:
        by_parent_sub[n.parent_legacy_node_key].append(n)
    for parent, kids in by_parent_sub.items():
        for i, n in enumerate(kids):
            n.sort_order = i

    ordered: list[PlannedNode] = []
    ordered.extend(platforms)
    ordered.extend(sections)
    ordered.extend(subsections)
    return ordered


def _existing_catalog_state(
    conn: ReadOnlyConnection,
    raw_conn: sqlite3.Connection,
    *,
    planned_id: str,
    legacy_catalog_id: str,
    bridge_exists: bool,
) -> dict[str, Any]:
    service_exists = False
    if _table_exists(raw_conn, "soldium_catalog_services"):
        row = conn.execute(
            "SELECT id FROM soldium_catalog_services WHERE id = ? LIMIT 1",
            (planned_id,),
        ).fetchone()
        service_exists = row is not None

    bridge_row = None
    if bridge_exists:
        bridge_row = conn.execute(
            f"""
            SELECT legacy_catalog_id, soldium_service_id
            FROM {LEGACY_SERVICE_BRIDGE_TABLE}
            WHERE legacy_catalog_id = ? OR soldium_service_id = ?
            LIMIT 1
            """,
            (legacy_catalog_id, planned_id),
        ).fetchone()

    bridge_for_legacy = None
    bridge_for_svc = None
    if bridge_row is not None:
        bridge_for_legacy = str(bridge_row["legacy_catalog_id"])
        bridge_for_svc = str(bridge_row["soldium_service_id"])

    if service_exists and bridge_row is not None:
        if bridge_for_svc == planned_id and bridge_for_legacy == legacy_catalog_id:
            state = "service_and_bridge_match"
        else:
            state = "service_bridge_mismatch"
    elif service_exists and not bridge_exists:
        state = "service_exists_bridge_table_absent"
    elif service_exists and bridge_row is None:
        state = "service_exists_without_bridge"
    elif bridge_row is not None and not service_exists:
        state = "bridge_without_service"
    elif not bridge_exists:
        state = "clear_bridge_not_created"
    else:
        state = "clear"

    return {
        "state": state,
        "service_exists": service_exists,
        "bridge_row_present": bridge_row is not None,
        "bridge_legacy_catalog_id": bridge_for_legacy,
        "bridge_soldium_service_id": bridge_for_svc,
    }


def plan_legacy_migration(
    connection: sqlite3.Connection,
) -> LegacyMigrationDryRunReport:
    """Build a full Dry Run plan from an open (preferably read-only) connection."""
    # Defense: wrap so accidental writes from helpers fail loudly.
    ro = ReadOnlyConnection(connection)
    raw_conn = connection

    if not _table_exists(raw_conn, "smm_services"):
        raise CatalogValidationError("جدول smm_services غير موجود")

    generated_at = datetime.now(timezone.utc).isoformat()
    bridge_exists = _table_exists(raw_conn, LEGACY_SERVICE_BRIDGE_TABLE)
    node_bridge_exists = _table_exists(raw_conn, LEGACY_NODE_BRIDGE_TABLE)
    if not bridge_exists:
        bridge_state = "not_created"
    else:
        n_bridge = int(
            ro.execute(
                f"SELECT COUNT(*) AS n FROM {LEGACY_SERVICE_BRIDGE_TABLE}"
            ).fetchone()["n"]
        )
        bridge_state = "populated" if n_bridge else "empty"
    if not node_bridge_exists:
        node_bridge_state = "not_created"
    else:
        n_node = int(
            ro.execute(
                f"SELECT COUNT(*) AS n FROM {LEGACY_NODE_BRIDGE_TABLE}"
            ).fetchone()["n"]
        )
        node_bridge_state = "populated" if n_node else "empty"

    inactive_inventory_count = int(
        ro.execute(
            """
            SELECT COUNT(*) AS n FROM smm_services
            WHERE NOT (is_active = 1 AND platform_key != '')
            """
        ).fetchone()["n"]
    )

    rows = list(
        ro.execute(
            """
            SELECT
                catalog_id,
                local_item_id,
                service_id,
                name_ar,
                platform_key,
                platform_title,
                section_key,
                section_title,
                subsection_key,
                subsection_title,
                min_qty,
                max_qty,
                category,
                local_price_dh,
                CAST(local_price_dh AS TEXT) AS local_price_dh_text,
                provider_slug,
                provider_api_account,
                external_service_id,
                fulfillment_mode
            FROM smm_services
            WHERE is_active = 1
              AND platform_key != ''
            ORDER BY platform_key, section_key, subsection_key, name_ar, catalog_id
            """
        ).fetchall()
    )

    repo = CatalogRepository(raw_conn)
    all_nodes: dict[str, PlannedNode] = {}
    planned_services: list[PlannedService] = []

    for row in rows:
        legacy_catalog_id = str(_row_get(row, "catalog_id") or "").strip()
        legacy_local = str(_row_get(row, "local_item_id") or "").strip() or legacy_catalog_id
        legacy_service_id = str(_row_get(row, "service_id") or "").strip()
        name_ar = str(_row_get(row, "name_ar") or "").strip()
        platform_key = str(_row_get(row, "platform_key") or "").strip()
        platform_title = str(_row_get(row, "platform_title") or "").strip()
        section_key_raw = _row_get(row, "section_key")
        section_key = (
            str(section_key_raw).strip() if section_key_raw not in (None, "") else None
        )
        section_title_raw = _row_get(row, "section_title")
        section_title = (
            str(section_title_raw).strip()
            if section_title_raw not in (None, "")
            else None
        )
        subsection_key_raw = _row_get(row, "subsection_key")
        subsection_key = (
            str(subsection_key_raw).strip()
            if subsection_key_raw not in (None, "")
            else None
        )
        subsection_title_raw = _row_get(row, "subsection_title")
        subsection_title = (
            str(subsection_title_raw).strip()
            if subsection_title_raw not in (None, "")
            else None
        )
        category = str(_row_get(row, "category") or "")
        provider_slug = str(_row_get(row, "provider_slug") or "").strip()
        account_raw = _row_get(row, "provider_api_account")
        provider_account = (
            str(account_raw).strip() if account_raw not in (None, "") else None
        )
        external_id = str(_row_get(row, "external_service_id") or "").strip()
        fulfillment_mode = str(_row_get(row, "fulfillment_mode") or "").strip().lower()

        min_qty_raw = _row_get(row, "min_qty")
        max_qty_raw = _row_get(row, "max_qty")
        try:
            min_qty = int(min_qty_raw)
            max_qty = int(max_qty_raw)
            qty_ok = True
        except (TypeError, ValueError):
            min_qty = None
            max_qty = None
            qty_ok = False

        price_text = dh_text_for_catalog_pricing(
            _row_get(row, "local_price_dh"),
            cast_text=str(_row_get(row, "local_price_dh_text") or ""),
        )
        price_plan = _classify_and_plan_price(category, price_text)

        blocking: list[str] = []
        review: list[str] = []
        blocking.extend(price_plan["blocking_codes"])

        if not name_ar:
            blocking.append("missing_name")
        if not qty_ok:
            blocking.append("invalid_min_quantity")
        else:
            assert min_qty is not None and max_qty is not None
            if min_qty < 1:
                blocking.append("invalid_min_quantity")
            if min_qty > max_qty:
                blocking.append("invalid_quantity_range")
            if is_max_qty_sentinel(max_qty):
                review.append("max_qty_sentinel")

        if not external_id:
            blocking.append("missing_external_service_id")

        p_block, p_review, account_ok = _provider_checks(
            repo, provider_slug, provider_account
        )
        blocking.extend(p_block)
        review.extend(p_review)

        if category.strip() == "per_unit":
            review.append("per_unit")
        if fulfillment_mode == "admin":
            review.append("fulfillment_admin")

        # Deduplicate codes preserving order
        blocking = list(dict.fromkeys(blocking))
        review = list(dict.fromkeys(review))

        if blocking:
            classification: Classification = "BLOCKED"
        elif review:
            classification = "REQUIRES_REVIEW"
        else:
            classification = "SAFE"

        planned_id = planned_soldium_service_id(legacy_catalog_id)
        path_keys, deepest, row_nodes = _structure_for_row(
            platform_key,
            platform_title,
            section_key,
            section_title,
            subsection_key,
            subsection_title,
        )
        deepest_node = row_nodes[deepest]
        deepest_parent_type = deepest_node.level

        # Only merge nodes for rows that will create Catalog objects
        if classification != "BLOCKED":
            for k, n in row_nodes.items():
                if k not in all_nodes:
                    all_nodes[k] = n

        existing = _existing_catalog_state(
            ro,
            raw_conn,
            planned_id=planned_id,
            legacy_catalog_id=legacy_catalog_id,
            bridge_exists=bridge_exists,
        )

        actions: list[str] = []
        exec_plan: Literal["present", "omitted", "none"] = "none"
        if classification == "BLOCKED":
            actions = ["no_catalog_write", "report_only"]
        else:
            actions = [
                "create_service",
                "create_service_entry",
                "create_price",
                "create_legacy_bridge",
            ]
            if account_ok and not p_block:
                actions.insert(3, "create_execution_source")
                exec_plan = "present"
            else:
                actions.append("omit_execution_source")
                exec_plan = "omitted"
            if classification == "REQUIRES_REVIEW":
                actions.append("review_required")

        planned_services.append(
            PlannedService(
                legacy_catalog_id=legacy_catalog_id,
                legacy_local_item_id=legacy_local,
                legacy_service_id=legacy_service_id,
                planned_soldium_service_id=planned_id,
                classification=classification,
                review_codes=review,
                blocking_codes=blocking,
                name_ar=name_ar,
                platform_key=platform_key,
                platform_title=platform_title,
                section_key=section_key,
                section_title=section_title,
                subsection_key=subsection_key,
                subsection_title=subsection_title,
                structure_path_keys=path_keys,
                planned_parent_node_key=deepest if classification != "BLOCKED" else None,
                planned_sort_order=None,
                min_quantity=min_qty,
                max_quantity=max_qty,
                category=category,
                legacy_price_dh=price_text,
                local_price_dh_source=price_text,
                normalized_price_dh=price_plan["normalized_price_dh"],
                amount_millimes=price_plan["amount_millimes"],
                pricing_mode=price_plan["pricing_mode"],
                rounding_applied=bool(price_plan["rounding_applied"]),
                price_normalized=bool(price_plan["price_normalized"]),
                price_error=price_plan["price_error"],
                deepest_parent_type=deepest_parent_type,
                provider_slug=provider_slug,
                provider_api_account=provider_account,
                external_service_id=external_id,
                fulfillment_mode=fulfillment_mode or "auto",
                planned_execution_source=exec_plan,
                planned_actions=actions,
                existing_catalog=existing,
            )
        )

    ordered_nodes = _assign_node_sort_orders(all_nodes)

    # Service sort_order under each parent (migration ordering only)
    by_parent: dict[str, list[PlannedService]] = defaultdict(list)
    for svc in planned_services:
        if svc.classification == "BLOCKED" or not svc.planned_parent_node_key:
            continue
        by_parent[svc.planned_parent_node_key].append(svc)
    for parent_key, kids in by_parent.items():
        kids.sort(
            key=lambda s: (
                s.name_ar,
                s.legacy_catalog_id,
            )
        )
        for i, s in enumerate(kids):
            s.planned_sort_order = i

    safe_count = sum(1 for s in planned_services if s.classification == "SAFE")
    review_count = sum(
        1 for s in planned_services if s.classification == "REQUIRES_REVIEW"
    )
    blocked_count = sum(1 for s in planned_services if s.classification == "BLOCKED")
    importable = [s for s in planned_services if s.classification != "BLOCKED"]
    planned_exec = sum(
        1 for s in importable if s.planned_execution_source == "present"
    )
    review_with_source = sum(
        1
        for s in planned_services
        if s.classification == "REQUIRES_REVIEW"
        and s.planned_execution_source == "present"
    )
    prices_normalized_count = sum(1 for s in planned_services if s.price_normalized)

    review_combos = Counter(
        "+".join(s.review_codes) if s.review_codes else "<none>"
        for s in planned_services
        if s.classification == "REQUIRES_REVIEW"
    )

    placement = {
        "platform_attached_count": sum(
            1 for s in planned_services if s.deepest_parent_type == "platform"
        ),
        "section_attached_count": sum(
            1 for s in planned_services if s.deepest_parent_type == "section"
        ),
        "subsection_attached_count": sum(
            1 for s in planned_services if s.deepest_parent_type == "subsection"
        ),
        "missing_parent_count": sum(
            1 for s in planned_services if not s.deepest_parent_type
        ),
        "note": "every candidate has deepest_parent_type; blocked rows still have structure computed",
    }

    node_levels = Counter(n.level for n in ordered_nodes)

    groups = {
        "by_platform": dict(Counter(s.platform_key for s in planned_services)),
        "by_classification": {
            "SAFE": safe_count,
            "REQUIRES_REVIEW": review_count,
            "BLOCKED": blocked_count,
        },
        "by_review_code": dict(
            Counter(code for s in planned_services for code in s.review_codes)
        ),
        "by_review_code_combination": dict(review_combos),
        "by_blocking_code": dict(
            Counter(code for s in planned_services for code in s.blocking_codes)
        ),
        "by_provider": dict(Counter(s.provider_slug for s in planned_services)),
        "by_provider_account": dict(
            Counter((s.provider_api_account or "<NULL>") for s in planned_services)
        ),
        "by_pricing_mode": dict(
            Counter((s.pricing_mode or "<none>") for s in planned_services)
        ),
        "by_fulfillment_mode": dict(
            Counter(s.fulfillment_mode for s in planned_services)
        ),
        "by_node_level": dict(node_levels),
        "execution_source_formula": {
            "safe_with_source": sum(
                1
                for s in planned_services
                if s.classification == "SAFE" and s.planned_execution_source == "present"
            ),
            "review_with_valid_account_source": review_with_source,
            "planned_execution_sources": planned_exec,
        },
    }

    return LegacyMigrationDryRunReport(
        generated_at=generated_at,
        source={"legacy_table": "smm_services"},
        candidate_count=len(planned_services),
        inactive_inventory_count=inactive_inventory_count,
        safe_count=safe_count,
        review_count=review_count,
        blocked_count=blocked_count,
        planned_service_count=len(importable),
        planned_node_count=len(ordered_nodes),
        planned_node_entry_count=len(ordered_nodes),
        planned_service_entry_count=len(importable),
        planned_total_entry_count=len(ordered_nodes) + len(importable),
        planned_price_count=len(importable),
        planned_execution_source_count=planned_exec,
        planned_bridge_count=len(importable),
        prices_normalized_count=prices_normalized_count,
        publication_count=0,
        provider_mapping_count=0,
        legacy_write_count=0,
        order_write_count=0,
        telegram_write_count=0,
        bridge_state=bridge_state,
        node_bridge_state=node_bridge_state,
        identity_namespace=str(LEGACY_MIGRATION_UUID_NAMESPACE),
        price_normalization={
            "rounding_mode": PRICE_NORMALIZATION_ROUNDING_NAME,
            "quantum_dh": str(PRICE_NORMALIZATION_QUANTUM),
            "max_decimal_places": 3,
            "catalog_compatible": "dh_to_millimes after Decimal quantize",
            "prices_normalized_count": prices_normalized_count,
        },
        migration_ordering_note=(
            "migration ordering != business-approved ordering; "
            "sort uses platform/section/subsection keys then name_ar then legacy_catalog_id"
        ),
        placement=placement,
        nodes=ordered_nodes,
        services=planned_services,
        groups=groups,
        safety={
            "read_only": True,
            "writes_forbidden": True,
            "publication_events_planned": 0,
            "provider_mappings_planned": 0,
            "provider_api_calls": 0,
            "fixed_package_inferred": 0,
            "category_used_as_structure": False,
            "price_precision_no_longer_blocking": True,
        },
    )


def plan_legacy_migration_at_path(
    db_path: Path | str | None = None,
) -> LegacyMigrationDryRunReport:
    """Open DB read-only and return Dry Run plan (no writes, no schema ensure)."""
    with catalog_readonly_connection(db_path) as conn:
        return plan_legacy_migration(conn)


def dry_run_report_json(db_path: Path | str | None = None) -> dict[str, Any]:
    """JSON-serializable Dry Run report."""
    return plan_legacy_migration_at_path(db_path).to_dict()


def compute_plan_fingerprint(report: LegacyMigrationDryRunReport) -> str:
    """Deterministic SHA-256 of the migration-critical Dry Run plan."""
    import hashlib
    import json

    parts: list[Any] = []
    for s in sorted(report.services, key=lambda x: x.legacy_catalog_id):
        parts.append(
            (
                s.legacy_catalog_id,
                s.planned_soldium_service_id,
                s.planned_parent_node_key,
                s.normalized_price_dh,
                s.amount_millimes,
                s.pricing_mode,
                s.min_quantity,
                s.max_quantity,
                s.provider_slug,
                s.provider_api_account,
                s.external_service_id,
                s.classification,
                tuple(s.review_codes),
                s.planned_execution_source,
                s.fulfillment_mode,
                s.name_ar,
            )
        )
    for n in sorted(report.nodes, key=lambda x: x.legacy_node_key):
        parts.append(
            (
                "NODE",
                n.legacy_node_key,
                n.parent_legacy_node_key,
                n.sort_order,
                n.level,
                n.name_ar,
            )
        )
    raw = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
