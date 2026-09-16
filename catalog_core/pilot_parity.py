# -*- coding: utf-8 -*-
"""Phase 9B.5 — Pilot Parity audit (read-only).

Audits the five published pilot services against Legacy ``smm_services`` using
stable structural keys from ``soldium_catalog_legacy_node_bridge``.

Never publishes, never mutates Catalog/Legacy/Orders, never calls Providers,
never invents target validation or fulfillment semantics.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from catalog_core.db import catalog_readonly_connection, resolve_db_path
from catalog_core.publication import CatalogPublicationService
from catalog_core.repository import CatalogRepository
from catalog_core.storefront_adapter import (
    StorefrontAdapter,
    StorefrontAdapterError,
)
from catalog_core.storefront_projection import PublishedStorefrontProjection
from catalog_core.storefront_shadow import _dh_to_millimes_exact
from catalog_core.target_validation import PLATFORM_HOST_RULES


def _sample_target_url(platform_key: str) -> str:
    """Minimal valid HTTPS URL for Adapter order-intent checks in parity audit."""
    pk = str(platform_key or "").strip().lower()
    hosts = {
        "instagram": "https://instagram.com/x",
        "facebook": "https://facebook.com/x",
        "tiktok": "https://tiktok.com/@x",
        "youtube": "https://youtube.com/watch?v=dQw4w9WgXcQ",
        "telegram": "https://t.me/x",
        "x": "https://x.com/x",
    }
    if pk in hosts:
        return hosts[pk]
    if pk in PLATFORM_HOST_RULES:
        return f"https://{PLATFORM_HOST_RULES[pk][0]}/x"
    return "https://instagram.com/x"

# (legacy_catalog_id, soldium_service_id, platform_key)
PILOT_SERVICES: tuple[tuple[str, str, str], ...] = (
    ("1773", "svc_5a1e241e84d95d2f9de0ba6337b0a43d", "facebook"),
    ("1154", "svc_a2745c669e395d52b0c5cfb6848889e8", "instagram"),
    ("2252", "svc_697f7630fd6d5d22aaf85b1ca3d86382", "telegram"),
    ("1566", "svc_d0a133fc5c5a5ad2a393886ae8d48439", "tiktok"),
    ("2414", "svc_444a46c672b25e41852f403c12a9928f", "x"),
)

ParityClassification = Literal[
    "VERIFIED_PARITY",
    "PARITY_WITH_REVIEW",
    "ACTUAL_PARITY_FAILURE",
    "BLOCKED",
]

TargetValidationStatus = Literal[
    "requires_future_contract",
    "accounted",
]

_NODE_BRIDGE_TABLE = "soldium_catalog_legacy_node_bridge"
_SENTINEL_MAX_QUANTITY = 2_147_483_647


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _norm_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _norm_key(value: object) -> str:
    return str(value or "").strip().lower()


def _row_get(row: sqlite3.Row | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        if key not in row.keys():
            return default
    except Exception:
        return default
    val = row[key]
    return default if val is None else val


# ---------------------------------------------------------------------------
# Placement keys (structural — never Arabic location_path labels)
# ---------------------------------------------------------------------------


def parse_legacy_node_key(
    key: str | None,
) -> tuple[str | None, str | None, str | None] | None:
    """Parse ``platform:`` / ``section:`` / ``subsection:`` bridge keys.

    Returns ``(platform, section, subsection)`` with ``None`` for absent levels,
    or ``None`` if the key is empty/unrecognized.
    """
    raw = str(key or "").strip()
    if not raw:
        return None

    if raw.startswith("platform:"):
        platform = raw[len("platform:") :].strip()
        if not platform or "/" in platform:
            return None
        return (platform, None, None)

    if raw.startswith("section:"):
        rest = raw[len("section:") :].strip()
        parts = rest.split("/")
        if len(parts) != 2:
            return None
        platform, section = parts[0].strip(), parts[1].strip()
        if not platform or not section:
            return None
        return (platform, section, None)

    if raw.startswith("subsection:"):
        rest = raw[len("subsection:") :].strip()
        parts = rest.split("/")
        if len(parts) != 3:
            return None
        platform, section, subsection = (
            parts[0].strip(),
            parts[1].strip(),
            parts[2].strip(),
        )
        if not platform or not section or not subsection:
            return None
        return (platform, section, subsection)

    return None


def _normalize_placement_keys(
    keys: tuple[Any, ...] | list[Any] | None,
) -> tuple[str, str, str] | None:
    if keys is None:
        return None
    vals = list(keys)
    while len(vals) < 3:
        vals.append("")
    platform, section, subsection = vals[0], vals[1], vals[2]
    return (
        _norm_key(platform),
        _norm_key(section),
        _norm_key(subsection),
    )


def placement_keys_equal(
    legacy_keys: tuple[Any, ...] | list[Any] | None,
    structural_keys: tuple[Any, ...] | list[Any] | None,
) -> bool:
    """True when both sides normalize to the same (platform, section, subsection)."""
    left = _normalize_placement_keys(legacy_keys)
    right = _normalize_placement_keys(structural_keys)
    if left is None or right is None:
        return False
    return left == right


def _bridge_legacy_key_for_entry(
    conn: sqlite3.Connection, entry_id: str, node_id: str | None
) -> str | None:
    if node_id:
        row = conn.execute(
            f"""
            SELECT legacy_node_key FROM {_NODE_BRIDGE_TABLE}
            WHERE soldium_node_id = ?
            LIMIT 1
            """,
            (node_id,),
        ).fetchone()
        if row and row["legacy_node_key"]:
            return str(row["legacy_node_key"])
    row = conn.execute(
        f"""
        SELECT legacy_node_key FROM {_NODE_BRIDGE_TABLE}
        WHERE soldium_entry_id = ?
        LIMIT 1
        """,
        (entry_id,),
    ).fetchone()
    if row and row["legacy_node_key"]:
        return str(row["legacy_node_key"])
    return None


def resolve_structural_placement_keys(
    conn: sqlite3.Connection,
    soldium_service_id: str,
    *,
    prefer_publication_parent: bool = True,
) -> tuple[str, str, str] | None:
    """Resolve Legacy-equivalent placement via node bridge ancestry.

    Starts from publication ``parent_entry_id`` when available (historical
    publish context), otherwise the live Catalog entry parent. Walks ancestors
    and joins ``soldium_catalog_legacy_node_bridge``.

    Does **not** use Arabic ``location_path`` labels for equality.
    Returns ``(platform, section, subsection)`` with empty strings for missing
    levels, or ``None`` when structural keys cannot be proven.
    """
    svc_id = str(soldium_service_id or "").strip()
    if not svc_id:
        return None

    parent_entry_id: str | None = None
    if prefer_publication_parent:
        pubs = CatalogPublicationService(conn)
        latest = pubs.get_latest_publish(svc_id)
        if latest and latest.parent_entry_id:
            parent_entry_id = str(latest.parent_entry_id).strip() or None

    if not parent_entry_id:
        repo = CatalogRepository(conn)
        entry = repo.get_entry_for_service(svc_id)
        if entry and entry.parent_entry_id:
            parent_entry_id = str(entry.parent_entry_id).strip() or None

    if not parent_entry_id:
        return None

    collected: list[str] = []
    current: str | None = parent_entry_id
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        row = conn.execute(
            """
            SELECT id, parent_entry_id, node_id, entry_type
            FROM soldium_catalog_entries
            WHERE id = ?
            """,
            (current,),
        ).fetchone()
        if not row:
            break
        node_id = str(row["node_id"]) if row["node_id"] else None
        bridge_key = _bridge_legacy_key_for_entry(conn, str(row["id"]), node_id)
        if bridge_key:
            collected.append(bridge_key)
        current = (
            str(row["parent_entry_id"]).strip() if row["parent_entry_id"] else None
        )

    if not collected:
        return None

    best: tuple[int, str, str, str] | None = None
    for key in collected:
        parsed = parse_legacy_node_key(key)
        if parsed is None:
            continue
        platform, section, subsection = parsed
        if not platform:
            continue
        score = (1 if platform else 0) + (1 if section else 0) + (
            1 if subsection else 0
        )
        candidate = (
            score,
            _norm_key(platform),
            _norm_key(section),
            _norm_key(subsection),
        )
        if best is None or candidate[0] > best[0]:
            best = candidate

    if best is None or not best[1]:
        return None
    return (best[1], best[2], best[3])


# ---------------------------------------------------------------------------
# Audit model
# ---------------------------------------------------------------------------


@dataclass
class PilotParityAudit:
    """Read-only parity classification for one published pilot service."""

    legacy_catalog_id: str
    soldium_service_id: str
    platform: str
    classification: ParityClassification

    legacy_name: str | None = None
    published_name: str | None = None
    name_equal: bool = False

    legacy_placement_keys: tuple[str, str, str] | None = None
    structural_placement_keys: tuple[str, str, str] | None = None
    published_location_path: list[str] = field(default_factory=list)
    placement_equal: bool = False
    placement_status: str = "indeterminate"

    legacy_category: str | None = None
    catalog_service_type: str | None = None
    service_type_sufficient: bool = True

    legacy_ordering_mode: str = "quantity_based"
    catalog_ordering_mode: str | None = None
    ordering_equal: bool = False

    legacy_min_quantity: int | None = None
    legacy_max_quantity: int | None = None
    catalog_min_quantity: int | None = None
    catalog_max_quantity: int | None = None
    qty_equal: bool = False

    legacy_price_millimes: int | None = None
    catalog_price_millimes: int | None = None
    catalog_pricing_mode: str | None = None
    price_equal: bool = False

    legacy_execution: dict[str, Any] = field(default_factory=dict)
    publication_execution: dict[str, Any] = field(default_factory=dict)
    projection_execution: dict[str, Any] = field(default_factory=dict)
    adapter_execution: dict[str, Any] = field(default_factory=dict)
    intent_execution: dict[str, Any] = field(default_factory=dict)
    execution_equal: bool = False

    legacy_fulfillment_mode: str | None = None
    adapter_fulfillment_mode: str | None = None
    fulfillment_gap: bool = False

    orderable: bool = False
    orderable_ok: bool = False

    target_validation_status: TargetValidationStatus = "requires_future_contract"

    content_fingerprint: str | None = None
    published_at: str | None = None
    publication_status: str | None = None

    core_ok: bool = False
    notes: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    intent: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "legacy_catalog_id": self.legacy_catalog_id,
            "soldium_service_id": self.soldium_service_id,
            "platform": self.platform,
            "classification": self.classification,
            "legacy_name": self.legacy_name,
            "published_name": self.published_name,
            "name_equal": self.name_equal,
            "legacy_placement_keys": (
                list(self.legacy_placement_keys)
                if self.legacy_placement_keys is not None
                else None
            ),
            "structural_placement_keys": (
                list(self.structural_placement_keys)
                if self.structural_placement_keys is not None
                else None
            ),
            "published_location_path": list(self.published_location_path),
            "placement_equal": self.placement_equal,
            "placement_status": self.placement_status,
            "legacy_category": self.legacy_category,
            "catalog_service_type": self.catalog_service_type,
            "service_type_sufficient": self.service_type_sufficient,
            "legacy_ordering_mode": self.legacy_ordering_mode,
            "catalog_ordering_mode": self.catalog_ordering_mode,
            "ordering_equal": self.ordering_equal,
            "legacy_min_quantity": self.legacy_min_quantity,
            "legacy_max_quantity": self.legacy_max_quantity,
            "catalog_min_quantity": self.catalog_min_quantity,
            "catalog_max_quantity": self.catalog_max_quantity,
            "qty_equal": self.qty_equal,
            "legacy_price_millimes": self.legacy_price_millimes,
            "catalog_price_millimes": self.catalog_price_millimes,
            "catalog_pricing_mode": self.catalog_pricing_mode,
            "price_equal": self.price_equal,
            "legacy_execution": dict(self.legacy_execution),
            "publication_execution": dict(self.publication_execution),
            "projection_execution": dict(self.projection_execution),
            "adapter_execution": dict(self.adapter_execution),
            "intent_execution": dict(self.intent_execution),
            "execution_equal": self.execution_equal,
            "legacy_fulfillment_mode": self.legacy_fulfillment_mode,
            "adapter_fulfillment_mode": self.adapter_fulfillment_mode,
            "fulfillment_gap": self.fulfillment_gap,
            "orderable": self.orderable,
            "orderable_ok": self.orderable_ok,
            "target_validation_status": self.target_validation_status,
            "content_fingerprint": self.content_fingerprint,
            "published_at": self.published_at,
            "publication_status": self.publication_status,
            "core_ok": self.core_ok,
            "notes": list(self.notes),
            "blocked_reason": self.blocked_reason,
            "intent": self.intent,
        }


def _classify(
    *,
    blocked_reason: str | None,
    core_ok: bool,
    fulfillment_gap: bool,
    target_validation_status: TargetValidationStatus,
) -> ParityClassification:
    if blocked_reason:
        return "BLOCKED"
    if not core_ok:
        return "ACTUAL_PARITY_FAILURE"
    # VERIFIED_PARITY requires fulfillment present AND target accounted.
    # Today every pilot has fulfillment_gap and requires_future_contract.
    if (
        not fulfillment_gap
        and target_validation_status == "accounted"
    ):
        return "VERIFIED_PARITY"
    return "PARITY_WITH_REVIEW"


def _load_legacy_row(
    conn: sqlite3.Connection, legacy_catalog_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM smm_services WHERE catalog_id = ? LIMIT 1",
        (str(legacy_catalog_id),),
    ).fetchone()


def audit_pilot_service(
    conn: sqlite3.Connection,
    legacy_catalog_id: str,
    soldium_service_id: str,
    platform: str,
) -> PilotParityAudit:
    """Read-only parity audit for one published pilot.

    Uses ``StorefrontAdapter``, ``PublishedStorefrontProjection``, and
    ``CatalogPublicationService`` only — no production mutations.
    """
    legacy_id = str(legacy_catalog_id).strip()
    svc_id = str(soldium_service_id).strip()
    plat = _norm_key(platform)
    notes: list[str] = []

    # Target validation is not on the Storefront contract today.
    target_validation_status: TargetValidationStatus = "requires_future_contract"
    notes.append(
        "target_validation_status=requires_future_contract "
        "(Adapter target is passthrough only; Telegram platform rules not on contract)"
    )

    leg = _load_legacy_row(conn, legacy_id)
    if leg is None:
        return PilotParityAudit(
            legacy_catalog_id=legacy_id,
            soldium_service_id=svc_id,
            platform=plat,
            classification="BLOCKED",
            target_validation_status=target_validation_status,
            notes=notes + ["legacy_row_missing"],
            blocked_reason="legacy_row_missing",
        )

    pubs = CatalogPublicationService(conn)
    pub_status = pubs.get_publication_status(svc_id)
    latest_pub = pubs.get_latest_publish(svc_id)
    publication_status = str(pub_status.get("publication_status") or "")

    if latest_pub is None or publication_status != "published":
        return PilotParityAudit(
            legacy_catalog_id=legacy_id,
            soldium_service_id=svc_id,
            platform=plat,
            classification="BLOCKED",
            legacy_name=_norm_text(_row_get(leg, "name_ar")),
            publication_status=publication_status or None,
            target_validation_status=target_validation_status,
            notes=notes + ["not_published"],
            blocked_reason="not_published",
        )

    adapter = StorefrontAdapter(conn)
    projection = PublishedStorefrontProjection(conn)
    try:
        asvc = adapter.get_service(svc_id)
    except StorefrontAdapterError as exc:
        return PilotParityAudit(
            legacy_catalog_id=legacy_id,
            soldium_service_id=svc_id,
            platform=plat,
            classification="BLOCKED",
            legacy_name=_norm_text(_row_get(leg, "name_ar")),
            publication_status=publication_status,
            target_validation_status=target_validation_status,
            notes=notes + [f"adapter_unavailable:{exc.code}"],
            blocked_reason=f"adapter_unavailable:{exc.code}",
        )

    try:
        psvc = projection.get_service(svc_id)
    except Exception as exc:  # noqa: BLE001 — audit must not raise
        return PilotParityAudit(
            legacy_catalog_id=legacy_id,
            soldium_service_id=svc_id,
            platform=plat,
            classification="BLOCKED",
            legacy_name=_norm_text(_row_get(leg, "name_ar")),
            published_name=asvc.name_ar,
            publication_status=publication_status,
            target_validation_status=target_validation_status,
            notes=notes + [f"projection_unavailable:{type(exc).__name__}"],
            blocked_reason="projection_unavailable",
        )

    legacy_name = _norm_text(_row_get(leg, "name_ar"))
    published_name = _norm_text(asvc.name_ar)
    name_equal = legacy_name == published_name

    legacy_placement = (
        _norm_key(_row_get(leg, "platform_key")),
        _norm_key(_row_get(leg, "section_key")),
        _norm_key(_row_get(leg, "subsection_key")),
    )
    structural = resolve_structural_placement_keys(
        conn, svc_id, prefer_publication_parent=True
    )
    placement_equal = placement_keys_equal(legacy_placement, structural)
    if structural is None:
        placement_status = "indeterminate"
        notes.append("placement_structural_unresolved")
    elif placement_equal:
        placement_status = "equivalent"
    else:
        placement_status = "mismatch"

    legacy_category = str(_row_get(leg, "category") or "").strip() or None
    catalog_service_type = str(asvc.service_type or "").strip() or None
    # Adapter orderability ignores service_type; "other" is sufficient for pilots.
    service_type_sufficient = True
    if catalog_service_type == "other":
        notes.append(
            "service_type=other is sufficient for Adapter browse/quote/intent "
            "(orderability uses pricing_mode + ordering_mode only)"
        )
    else:
        notes.append(
            f"service_type={catalog_service_type!r}; Adapter still does not "
            "gate orderability on service_type"
        )

    catalog_ordering_mode = str(asvc.ordering_mode or "").strip() or None
    ordering_equal = catalog_ordering_mode == "quantity_based"

    legacy_min = int(_row_get(leg, "min_qty") or 0)
    legacy_max = int(_row_get(leg, "max_qty") or 0)
    catalog_min = int(asvc.min_quantity)
    catalog_max = int(asvc.max_quantity)
    qty_equal = legacy_min == catalog_min and legacy_max == catalog_max
    if legacy_max >= _SENTINEL_MAX_QUANTITY:
        notes.append("legacy_max_qty_sentinel")

    legacy_price_millimes = _dh_to_millimes_exact(_row_get(leg, "local_price_dh"))
    catalog_price_millimes = int(asvc.price.amount_millimes)
    catalog_pricing_mode = str(asvc.price.pricing_mode or "").strip() or None
    price_equal = (
        legacy_price_millimes is not None
        and legacy_price_millimes == catalog_price_millimes
    )

    legacy_execution = {
        "provider_slug": _norm_key(_row_get(leg, "provider_slug")) or None,
        "provider_account_key": _norm_key(_row_get(leg, "provider_api_account"))
        or None,
        "external_service_id": str(
            _row_get(leg, "external_service_id") or ""
        ).strip()
        or None,
    }
    publication_execution = {
        "provider_slug": _norm_key(latest_pub.provider_slug) or None,
        "provider_account_key": _norm_key(latest_pub.provider_account_key) or None,
        "external_service_id": str(latest_pub.external_service_id or "").strip()
        or None,
    }
    projection_execution = psvc.execution.to_dict()
    adapter_execution = asvc.execution.to_dict()

    intent_dict: dict[str, Any] | None = None
    intent_execution: dict[str, Any] = {}
    intent_error: str | None = None
    qty_for_intent = max(catalog_min, 1)
    try:
        intent = adapter.resolve_order_intent(
            svc_id,
            qty_for_intent,
            target=_sample_target_url(asvc.target_policy.platform_key or plat),
        )
        intent_execution = {
            "provider_slug": intent.provider_slug,
            "provider_account_key": intent.provider_account_key,
            "external_service_id": intent.external_service_id,
        }
        intent_dict = {
            "service_id": intent.service_id,
            "service_name_ar": intent.service_name_ar,
            "quantity": intent.quantity,
            "quoted_amount_millimes": intent.quoted_amount_millimes,
            "currency": intent.currency,
            "pricing_mode": intent.pricing_mode,
            "provider_slug": intent.provider_slug,
            "provider_account_key": intent.provider_account_key,
            "external_service_id": intent.external_service_id,
            "content_fingerprint": intent.content_fingerprint,
            "published_at": intent.published_at,
            "fulfillment_mode": intent.fulfillment_mode,
            "target": intent.target,
        }
    except StorefrontAdapterError as exc:
        intent_error = exc.code
        notes.append(f"order_intent_failed:{exc.code}")

    execution_equal = (
        intent_error is None
        and publication_execution == projection_execution
        == adapter_execution
        == intent_execution
        and publication_execution.get("provider_slug")
        and publication_execution.get("provider_account_key")
        and publication_execution.get("external_service_id")
    )

    legacy_fulfillment = (
        str(_row_get(leg, "fulfillment_mode") or "").strip().lower() or None
    )
    adapter_fulfillment = asvc.fulfillment_mode
    fulfillment_gap = adapter_fulfillment is None
    if fulfillment_gap:
        notes.append(
            "fulfillment_gap: Adapter/projection expose fulfillment_mode=None; "
            "Legacy value is not copied into the published Storefront contract"
        )

    orderable = bool(asvc.orderable)
    orderable_ok = orderable and intent_error is None

    core_ok = all(
        (
            name_equal,
            price_equal,
            qty_equal,
            ordering_equal,
            execution_equal,
            placement_equal,
            orderable_ok,
        )
    )

    classification = _classify(
        blocked_reason=None,
        core_ok=core_ok,
        fulfillment_gap=fulfillment_gap,
        target_validation_status=target_validation_status,
    )
    if not core_ok:
        failed = [
            name
            for name, ok in (
                ("name", name_equal),
                ("price", price_equal),
                ("qty", qty_equal),
                ("ordering", ordering_equal),
                ("execution", execution_equal),
                ("placement", placement_equal),
                ("orderability", orderable_ok),
            )
            if not ok
        ]
        notes.append("core_failures:" + ",".join(failed))

    return PilotParityAudit(
        legacy_catalog_id=legacy_id,
        soldium_service_id=svc_id,
        platform=plat,
        classification=classification,
        legacy_name=legacy_name,
        published_name=published_name,
        name_equal=name_equal,
        legacy_placement_keys=legacy_placement,
        structural_placement_keys=structural,
        published_location_path=list(asvc.location_path),
        placement_equal=placement_equal,
        placement_status=placement_status,
        legacy_category=legacy_category,
        catalog_service_type=catalog_service_type,
        service_type_sufficient=service_type_sufficient,
        legacy_ordering_mode="quantity_based",
        catalog_ordering_mode=catalog_ordering_mode,
        ordering_equal=ordering_equal,
        legacy_min_quantity=legacy_min,
        legacy_max_quantity=legacy_max,
        catalog_min_quantity=catalog_min,
        catalog_max_quantity=catalog_max,
        qty_equal=qty_equal,
        legacy_price_millimes=legacy_price_millimes,
        catalog_price_millimes=catalog_price_millimes,
        catalog_pricing_mode=catalog_pricing_mode,
        price_equal=price_equal,
        legacy_execution=legacy_execution,
        publication_execution=publication_execution,
        projection_execution=projection_execution,
        adapter_execution=adapter_execution,
        intent_execution=intent_execution,
        execution_equal=execution_equal,
        legacy_fulfillment_mode=legacy_fulfillment,
        adapter_fulfillment_mode=adapter_fulfillment,
        fulfillment_gap=fulfillment_gap,
        orderable=orderable,
        orderable_ok=orderable_ok,
        target_validation_status=target_validation_status,
        content_fingerprint=asvc.content_fingerprint,
        published_at=asvc.published_at,
        publication_status=publication_status,
        core_ok=core_ok,
        notes=notes,
        intent=intent_dict,
    )


def _compute_fingerprint(payload: dict[str, Any]) -> str:
    data = dict(payload)
    data.pop("generated_at", None)
    data.pop("fingerprint", None)
    canonical = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def audit_all_pilots(conn: sqlite3.Connection) -> dict[str, Any]:
    """Audit all ``PILOT_SERVICES``; return a deterministic report dict."""
    audits = [
        audit_pilot_service(conn, legacy_id, svc_id, platform)
        for legacy_id, svc_id, platform in PILOT_SERVICES
    ]
    counts: dict[str, int] = {
        "VERIFIED_PARITY": 0,
        "PARITY_WITH_REVIEW": 0,
        "ACTUAL_PARITY_FAILURE": 0,
        "BLOCKED": 0,
    }
    for audit in audits:
        counts[audit.classification] = counts.get(audit.classification, 0) + 1

    report: dict[str, Any] = {
        "phase": "9B.5",
        "generated_at": _utcnow_iso(),
        "pilot_count": len(audits),
        "classification_counts": counts,
        "audits": [a.to_dict() for a in audits],
        "fingerprint": "",
    }
    report["fingerprint"] = _compute_fingerprint(report)
    return report


def audit_all_pilots_at_path(
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Open the DB read-only and run ``audit_all_pilots``."""
    with catalog_readonly_connection(resolve_db_path(db_path)) as conn:
        return audit_all_pilots(conn)
