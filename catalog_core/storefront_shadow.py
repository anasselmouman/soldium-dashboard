# -*- coding: utf-8 -*-
"""Phase 9B.3 — Storefront Shadow Comparison / Parity Audit.

Read-only comparison of:

    Legacy Telegram storefront (smm_services active sellable rows)
        VS
    Published Catalog storefront (StorefrontAdapter → PublishedStorefrontProjection)

Never publishes, never mutates, never calls Providers, never cutover.
Never fuzzy-matches identity. Correlation uses soldium_catalog_legacy_bridge only.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from catalog_core.db import catalog_readonly_connection, resolve_db_path
from catalog_core.legacy_migration import LEGACY_SERVICE_BRIDGE_TABLE
from catalog_core.pricing import MILLIMES_PER_DH, format_dh_amount
from catalog_core.storefront_adapter import StorefrontAdapter, StorefrontService

Severity = Literal["baseline", "dangerous", "review", "info", "equal"]
BaselineState = Literal[
    "PRE_PUBLICATION_BASELINE",
    "PARTIAL_PUBLICATION",
    "PARTIAL_PUBLICATION_PILOT",
    "FULL_COMPARISON",
]

# Same sellable filter as Telegram services_catalog_db._fetch_active_rows.
_LEGACY_ACTIVE_SQL = """
SELECT *
FROM smm_services
WHERE is_active = 1
  AND platform_key != ''
ORDER BY platform_key, section_key, subsection_key, name_ar
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _norm_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _norm_key(value: object) -> str:
    return str(value or "").strip().lower()


def _dh_to_millimes_exact(value: object) -> int | None:
    """Convert legacy local_price_dh to millimes; None if invalid/non-positive."""
    raw = str(value if value is not None else "").strip()
    if not raw:
        return None
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if amount.is_nan() or amount.is_infinite() or amount <= 0:
        return None
    scaled = amount * Decimal(MILLIMES_PER_DH)
    millimes = int(scaled.to_integral_value(rounding=ROUND_HALF_UP))
    return millimes if millimes > 0 else None


def _legacy_pricing_mode(category: object) -> str:
    """Only ``per_unit`` is a pricing mode; all other categories → per_1000."""
    if str(category or "").strip() == "per_unit":
        return "per_unit"
    return "per_1000"


@dataclass(frozen=True)
class ShadowExecutionIdentity:
    provider_slug: str | None
    provider_account_key: str | None
    external_service_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_slug": self.provider_slug,
            "provider_account_key": self.provider_account_key,
            "external_service_id": self.external_service_id,
        }

    def canonical(self) -> tuple[str, str, str]:
        return (
            _norm_key(self.provider_slug or ""),
            _norm_key(self.provider_account_key or ""),
            str(self.external_service_id or "").strip(),
        )


@dataclass(frozen=True)
class ShadowServiceSnapshot:
    source: Literal["legacy", "catalog"]
    legacy_catalog_id: str | None
    legacy_local_item_id: str | None
    catalog_service_id: str | None
    name: str
    placement_keys: tuple[str, str, str] | None
    placement_path: tuple[str, ...] | None
    service_type: str | None
    ordering_mode: str | None
    min_quantity: int | None
    max_quantity: int | None
    pricing_mode: str | None
    amount_millimes: int | None
    currency: str | None
    orderable: bool
    available: bool
    execution: ShadowExecutionIdentity | None
    legacy_category: str | None = None
    fulfillment_mode: str | None = None
    target_platform_key: str | None = None
    content_fingerprint: str | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["placement_keys"] = (
            list(self.placement_keys) if self.placement_keys is not None else None
        )
        data["placement_path"] = (
            list(self.placement_path) if self.placement_path is not None else None
        )
        data["notes"] = list(self.notes)
        data["amount_dh"] = (
            format_dh_amount(self.amount_millimes)
            if self.amount_millimes is not None
            else None
        )
        return data


@dataclass(frozen=True)
class ShadowDifference:
    category: str
    code: str
    message: str
    legacy_value: Any = None
    catalog_value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ShadowComparisonRow:
    correlation: Literal["legacy_only", "catalog_only", "correlated"]
    legacy_identity: str | None
    catalog_identity: str | None
    service_name: str
    differences: list[ShadowDifference] = field(default_factory=list)
    severity: Severity = "info"
    notes: list[str] = field(default_factory=list)
    legacy: dict[str, Any] | None = None
    catalog: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "correlation": self.correlation,
            "legacy_identity": self.legacy_identity,
            "catalog_identity": self.catalog_identity,
            "service_name": self.service_name,
            "differences": [d.to_dict() for d in self.differences],
            "severity": self.severity,
            "notes": list(self.notes),
            "legacy": self.legacy,
            "catalog": self.catalog,
        }


@dataclass
class ShadowComparisonReport:
    generated_at: str
    baseline_state: BaselineState
    publication_count: int
    legacy_count: int
    catalog_count: int
    correlated_count: int
    legacy_only_count: int
    catalog_only_count: int
    equal_count: int
    changed_count: int
    dangerous_count: int
    review_count: int
    baseline_count: int
    rows: list[ShadowComparisonRow] = field(default_factory=list)
    known_risks: list[dict[str, Any]] = field(default_factory=list)
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "baseline_state": self.baseline_state,
            "publication_count": self.publication_count,
            "legacy_count": self.legacy_count,
            "catalog_count": self.catalog_count,
            "correlated_count": self.correlated_count,
            "legacy_only_count": self.legacy_only_count,
            "catalog_only_count": self.catalog_only_count,
            "equal_count": self.equal_count,
            "changed_count": self.changed_count,
            "dangerous_count": self.dangerous_count,
            "review_count": self.review_count,
            "baseline_count": self.baseline_count,
            "fingerprint": self.fingerprint,
            "known_risks": list(self.known_risks),
            "rows": [r.to_dict() for r in self.rows],
        }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def load_bridge_correlation(conn: sqlite3.Connection) -> dict[str, str]:
    """legacy_catalog_id → soldium_service_id (explicit Phase 8 bridge only)."""
    if not _table_exists(conn, LEGACY_SERVICE_BRIDGE_TABLE):
        return {}
    rows = conn.execute(
        f"""
        SELECT legacy_catalog_id, soldium_service_id
        FROM {LEGACY_SERVICE_BRIDGE_TABLE}
        ORDER BY legacy_catalog_id ASC
        """
    ).fetchall()
    out: dict[str, str] = {}
    for row in rows:
        legacy_id = str(row["legacy_catalog_id"] or "").strip()
        soldium_id = str(row["soldium_service_id"] or "").strip()
        if legacy_id and soldium_id:
            out[legacy_id] = soldium_id
    return out


def load_legacy_snapshots(conn: sqlite3.Connection) -> list[ShadowServiceSnapshot]:
    """Active sellable Legacy storefront rows (Telegram filter)."""
    if not _table_exists(conn, "smm_services"):
        return []
    rows = conn.execute(_LEGACY_ACTIVE_SQL).fetchall()
    snapshots: list[ShadowServiceSnapshot] = []
    for row in rows:
        keys = set(row.keys())
        catalog_id = (
            str(row["catalog_id"]).strip()
            if "catalog_id" in keys and row["catalog_id"] is not None
            else str(row["service_id"]).strip()
        )
        local_id = (
            str(row["local_item_id"] or catalog_id).strip()
            if "local_item_id" in keys
            else catalog_id
        )
        category = str(row["category"] or "").strip() if "category" in keys else ""
        pricing_mode = _legacy_pricing_mode(category)
        millimes = _dh_to_millimes_exact(
            row["local_price_dh"] if "local_price_dh" in keys else None
        )
        provider_slug = None
        if "provider_slug" in keys and row["provider_slug"]:
            provider_slug = _norm_key(row["provider_slug"])
        account = None
        if "provider_api_account" in keys and row["provider_api_account"]:
            account = _norm_key(row["provider_api_account"])
        external = ""
        if "external_service_id" in keys and row["external_service_id"] is not None:
            external = str(row["external_service_id"]).strip()
        fulfillment = None
        if "fulfillment_mode" in keys and row["fulfillment_mode"] is not None:
            fulfillment = str(row["fulfillment_mode"]).strip().lower() or None

        platform = str(row["platform_key"] or "").strip()
        section = str(row["section_key"] or "").strip() if "section_key" in keys else ""
        subsection = (
            str(row["subsection_key"] or "").strip() if "subsection_key" in keys else ""
        )

        notes: list[str] = []
        if category and category != "per_unit":
            notes.append("legacy_category_not_pricing_mode")
        if millimes is None:
            notes.append("legacy_price_unconvertible")

        snapshots.append(
            ShadowServiceSnapshot(
                source="legacy",
                legacy_catalog_id=catalog_id,
                legacy_local_item_id=local_id,
                catalog_service_id=None,
                name=_norm_text(row["name_ar"] if "name_ar" in keys else ""),
                placement_keys=(platform, section, subsection),
                placement_path=None,
                service_type=None,
                ordering_mode="quantity_based",
                min_quantity=int(row["min_qty"] or 1) if "min_qty" in keys else None,
                max_quantity=int(row["max_qty"] or 0) if "max_qty" in keys else None,
                pricing_mode=pricing_mode,
                amount_millimes=millimes,
                currency="MAD",
                orderable=millimes is not None and millimes > 0,
                available=True,
                execution=ShadowExecutionIdentity(
                    provider_slug=provider_slug,
                    provider_account_key=account,
                    external_service_id=external or None,
                ),
                legacy_category=category or None,
                fulfillment_mode=fulfillment,
                notes=tuple(notes),
            )
        )
    snapshots.sort(
        key=lambda s: (
            s.legacy_catalog_id or "",
            s.legacy_local_item_id or "",
            s.name,
        )
    )
    return snapshots


def load_catalog_snapshots(conn: sqlite3.Connection) -> list[ShadowServiceSnapshot]:
    """Published Catalog storefront via StorefrontAdapter only."""
    adapter = StorefrontAdapter(conn)
    services = adapter.list_services()
    snapshots = [_catalog_service_to_snapshot(svc) for svc in services]
    snapshots.sort(key=lambda s: (s.catalog_service_id or "", s.name))
    return snapshots


def _catalog_service_to_snapshot(svc: StorefrontService) -> ShadowServiceSnapshot:
    notes: list[str] = []
    if not svc.orderable:
        notes.append("catalog_not_orderable")
    if svc.fulfillment_mode is None:
        notes.append("fulfillment_mode_absent")
    target_platform = str(svc.target_policy.platform_key or "").strip() or None
    if target_platform is None:
        notes.append("target_policy_absent")
    return ShadowServiceSnapshot(
        source="catalog",
        legacy_catalog_id=None,
        legacy_local_item_id=None,
        catalog_service_id=svc.service_id,
        name=_norm_text(svc.name_ar),
        placement_keys=None,
        placement_path=tuple(svc.location_path),
        service_type=svc.service_type,
        ordering_mode=svc.ordering_mode,
        min_quantity=int(svc.min_quantity),
        max_quantity=int(svc.max_quantity),
        pricing_mode=svc.price.pricing_mode,
        amount_millimes=int(svc.price.amount_millimes),
        currency=svc.price.currency,
        orderable=bool(svc.orderable),
        available=True,
        execution=ShadowExecutionIdentity(
            provider_slug=svc.execution.provider_slug,
            provider_account_key=svc.execution.provider_account_key,
            external_service_id=svc.execution.external_service_id,
        ),
        fulfillment_mode=svc.fulfillment_mode,
        target_platform_key=target_platform,
        content_fingerprint=svc.content_fingerprint,
        notes=tuple(notes),
    )


def _diff(
    category: str,
    code: str,
    message: str,
    *,
    legacy_value: Any = None,
    catalog_value: Any = None,
) -> ShadowDifference:
    return ShadowDifference(
        category=category,
        code=code,
        message=message,
        legacy_value=legacy_value,
        catalog_value=catalog_value,
    )


_DANGEROUS_CODES = frozenset(
    {
        "legacy_only",
        "catalog_only",
        "legacy_active_catalog_unavailable",
        "catalog_published_legacy_missing",
        "price_changed",
        "price_missing",
        "currency_changed",
        "pricing_mode_changed",
        "unsupported_pricing_mode",
        "min_changed",
        "max_changed",
        "ordering_mode_changed",
        "ordering_not_supported",
        "execution_changed",
        "execution_unavailable",
        "execution_missing",
        "legacy_orderable_catalog_not_orderable",
    }
)

_REVIEW_CODES = frozenset(
    {
        "placement_comparison_indeterminate",
        "placement_changed",
        "placement_missing",
        "service_type_legacy_absent",
        "service_type_changed",
        "fulfillment_mode_catalog_absent",
        "name_changed",
        "target_validation_requires_future_contract",
    }
)


def _severity_for_diffs(diffs: list[ShadowDifference]) -> Severity:
    codes = {d.code for d in diffs}
    meaningful = codes - {
        "correlated",
        "both_available",
        "name_equal",
        "pricing_mode_equal",
        "currency_equal",
        "price_equal",
        "min_equal",
        "max_equal",
        "ordering_mode_equal",
        "execution_equal",
        "both_orderable",
        "neither_orderable",
        "placement_equal",
    }
    if not meaningful:
        return "equal"
    if codes & _DANGEROUS_CODES:
        return "dangerous"
    if codes & _REVIEW_CODES:
        return "review"
    return "info"


def _compare_pair(
    legacy: ShadowServiceSnapshot | None,
    catalog: ShadowServiceSnapshot | None,
    *,
    outside_pilot_scope: bool,
    structural_placement_keys: tuple[str, str, str] | None = None,
) -> ShadowComparisonRow:
    if legacy is not None and catalog is None:
        if outside_pilot_scope:
            sev: Severity = "baseline"
            notes = ["PILOT_PUBLICATION_SCOPE", "intentionally_unpublished"]
            avail_code = "legacy_active_outside_pilot_scope"
            avail_msg = (
                "Legacy active; intentionally outside published pilot scope "
                "(not an actual parity failure)"
            )
        else:
            sev = "dangerous"
            notes = ["ACTUAL_PARITY_FAILURE", "legacy_sellable_missing_from_published_storefront"]
            avail_code = "legacy_active_catalog_unavailable"
            avail_msg = "Legacy active; published Catalog unavailable for this service"
        return ShadowComparisonRow(
            correlation="legacy_only",
            legacy_identity=legacy.legacy_catalog_id,
            catalog_identity=None,
            service_name=legacy.name,
            differences=[
                _diff(
                    "IDENTITY",
                    "legacy_only",
                    "Legacy sellable service has no published Catalog counterpart",
                    legacy_value=legacy.legacy_catalog_id,
                ),
                _diff("AVAILABILITY", avail_code, avail_msg),
                _diff(
                    "EXECUTION",
                    "execution_unavailable",
                    "Published execution identity unavailable (no publication)",
                    legacy_value=(
                        legacy.execution.to_dict() if legacy.execution else None
                    ),
                ),
            ],
            severity=sev,
            notes=notes,
            legacy=legacy.to_dict(),
            catalog=None,
        )

    if catalog is not None and legacy is None:
        return ShadowComparisonRow(
            correlation="catalog_only",
            legacy_identity=None,
            catalog_identity=catalog.catalog_service_id,
            service_name=catalog.name,
            differences=[
                _diff(
                    "IDENTITY",
                    "catalog_only",
                    "Published Catalog service has no correlated Legacy storefront row",
                    catalog_value=catalog.catalog_service_id,
                ),
                _diff(
                    "AVAILABILITY",
                    "catalog_published_legacy_missing",
                    "Published Catalog service missing from Legacy active storefront",
                ),
            ],
            severity="dangerous",
            notes=["published_without_legacy_correlation"],
            legacy=None,
            catalog=catalog.to_dict(),
        )

    assert legacy is not None and catalog is not None
    diffs: list[ShadowDifference] = [
        _diff(
            "IDENTITY",
            "correlated",
            "Correlated via legacy bridge",
            legacy_value=legacy.legacy_catalog_id,
            catalog_value=catalog.catalog_service_id,
        ),
        _diff("AVAILABILITY", "both_available", "Both sides available"),
    ]

    if legacy.name == catalog.name:
        diffs.append(
            _diff(
                "NAME",
                "name_equal",
                "Names equal",
                legacy_value=legacy.name,
                catalog_value=catalog.name,
            )
        )
    else:
        diffs.append(
            _diff(
                "NAME",
                "name_changed",
                "Customer-facing name differs",
                legacy_value=legacy.name,
                catalog_value=catalog.name,
            )
        )

    if legacy.pricing_mode == catalog.pricing_mode:
        diffs.append(
            _diff(
                "PRICE",
                "pricing_mode_equal",
                "Pricing modes equal",
                legacy_value=legacy.pricing_mode,
                catalog_value=catalog.pricing_mode,
            )
        )
    else:
        diffs.append(
            _diff(
                "PRICE",
                "pricing_mode_changed",
                "Pricing mode differs",
                legacy_value=legacy.pricing_mode,
                catalog_value=catalog.pricing_mode,
            )
        )
    if catalog.pricing_mode == "fixed_package":
        diffs.append(
            _diff(
                "PRICE",
                "unsupported_pricing_mode",
                "Catalog fixed_package is not orderable via current Adapter quantity flow",
                catalog_value=catalog.pricing_mode,
            )
        )
    if (legacy.currency or "MAD") == (catalog.currency or "MAD"):
        diffs.append(
            _diff(
                "PRICE",
                "currency_equal",
                "Currency equal",
                legacy_value=legacy.currency,
                catalog_value=catalog.currency,
            )
        )
    else:
        diffs.append(
            _diff(
                "PRICE",
                "currency_changed",
                "Currency differs",
                legacy_value=legacy.currency,
                catalog_value=catalog.currency,
            )
        )
    if legacy.amount_millimes is None or catalog.amount_millimes is None:
        diffs.append(
            _diff(
                "PRICE",
                "price_missing",
                "Price missing on one side",
                legacy_value=legacy.amount_millimes,
                catalog_value=catalog.amount_millimes,
            )
        )
    elif int(legacy.amount_millimes) == int(catalog.amount_millimes):
        diffs.append(
            _diff(
                "PRICE",
                "price_equal",
                "Unit price millimes equal",
                legacy_value=legacy.amount_millimes,
                catalog_value=catalog.amount_millimes,
            )
        )
    else:
        diffs.append(
            _diff(
                "PRICE",
                "price_changed",
                "Unit price differs",
                legacy_value=legacy.amount_millimes,
                catalog_value=catalog.amount_millimes,
            )
        )

    if legacy.min_quantity == catalog.min_quantity:
        diffs.append(
            _diff(
                "QUANTITY",
                "min_equal",
                "Min quantity equal",
                legacy_value=legacy.min_quantity,
                catalog_value=catalog.min_quantity,
            )
        )
    else:
        diffs.append(
            _diff(
                "QUANTITY",
                "min_changed",
                "Min quantity differs",
                legacy_value=legacy.min_quantity,
                catalog_value=catalog.min_quantity,
            )
        )
    if legacy.max_quantity == catalog.max_quantity:
        diffs.append(
            _diff(
                "QUANTITY",
                "max_equal",
                "Max quantity equal",
                legacy_value=legacy.max_quantity,
                catalog_value=catalog.max_quantity,
            )
        )
    else:
        diffs.append(
            _diff(
                "QUANTITY",
                "max_changed",
                "Max quantity differs",
                legacy_value=legacy.max_quantity,
                catalog_value=catalog.max_quantity,
            )
        )

    if legacy.ordering_mode == catalog.ordering_mode:
        diffs.append(
            _diff(
                "ORDERING",
                "ordering_mode_equal",
                "Ordering modes equal",
                legacy_value=legacy.ordering_mode,
                catalog_value=catalog.ordering_mode,
            )
        )
    else:
        diffs.append(
            _diff(
                "ORDERING",
                "ordering_mode_changed",
                "Ordering mode differs",
                legacy_value=legacy.ordering_mode,
                catalog_value=catalog.ordering_mode,
            )
        )
    if catalog.ordering_mode and catalog.ordering_mode != "quantity_based":
        diffs.append(
            _diff(
                "ORDERING",
                "ordering_not_supported",
                "Catalog ordering mode not supported by Telegram quantity flow",
                catalog_value=catalog.ordering_mode,
            )
        )

    diffs.append(
        _diff(
            "SERVICE_TYPE",
            "service_type_legacy_absent",
            "Legacy storefront item has no first-class service_type; "
            "Catalog 'other' is sufficient for Adapter browse/quote/intent",
            catalog_value=catalog.service_type,
        )
    )

    # Placement: prefer structural node-bridge keys over Arabic location_path labels.
    legacy_keys = tuple(str(x or "") for x in (legacy.placement_keys or ("", "", "")))
    if structural_placement_keys is not None and legacy.placement_keys is not None:
        struct = tuple(str(x or "") for x in structural_placement_keys)
        if legacy_keys == struct:
            diffs.append(
                _diff(
                    "PLACEMENT",
                    "placement_equal",
                    "Structural placement keys match via legacy node bridge "
                    "(not Arabic location_path labels)",
                    legacy_value=list(legacy_keys),
                    catalog_value=list(struct),
                )
            )
        else:
            diffs.append(
                _diff(
                    "PLACEMENT",
                    "placement_changed",
                    "Structural placement keys differ from Legacy keys",
                    legacy_value=list(legacy_keys),
                    catalog_value=list(struct),
                )
            )
    else:
        diffs.append(
            _diff(
                "PLACEMENT",
                "placement_comparison_indeterminate",
                "Legacy uses platform/section/subsection keys; Catalog uses published "
                "location_path names; structural node-bridge keys unavailable",
                legacy_value=list(legacy.placement_keys or ()),
                catalog_value=list(catalog.placement_path or ()),
            )
        )

    if not (catalog.target_platform_key or "").strip():
        diffs.append(
            _diff(
                "TARGET",
                "target_validation_requires_future_contract",
                "StorefrontAdapter target is passthrough only; Telegram platform "
                "link validation is not on the Catalog contract yet",
            )
        )

    leg_ex = legacy.execution.canonical() if legacy.execution else ("", "", "")
    cat_ex = catalog.execution.canonical() if catalog.execution else ("", "", "")
    if not any(cat_ex):
        diffs.append(
            _diff(
                "EXECUTION",
                "execution_unavailable",
                "Published execution identity unavailable",
                legacy_value=legacy.execution.to_dict() if legacy.execution else None,
            )
        )
    elif leg_ex == cat_ex:
        diffs.append(
            _diff(
                "EXECUTION",
                "execution_equal",
                "Execution identity equal",
                legacy_value=legacy.execution.to_dict() if legacy.execution else None,
                catalog_value=catalog.execution.to_dict() if catalog.execution else None,
            )
        )
    else:
        diffs.append(
            _diff(
                "EXECUTION",
                "execution_changed",
                "Execution identity differs (affects order routing)",
                legacy_value=legacy.execution.to_dict() if legacy.execution else None,
                catalog_value=catalog.execution.to_dict() if catalog.execution else None,
            )
        )

    if legacy.orderable and catalog.orderable:
        diffs.append(_diff("ORDERABILITY", "both_orderable", "Both orderable"))
    elif legacy.orderable and not catalog.orderable:
        diffs.append(
            _diff(
                "ORDERABILITY",
                "legacy_orderable_catalog_not_orderable",
                "Legacy orderable but Catalog Adapter marks not orderable",
            )
        )
    elif catalog.orderable and not legacy.orderable:
        diffs.append(
            _diff(
                "ORDERABILITY",
                "catalog_orderable_legacy_not_orderable",
                "Catalog orderable but Legacy not orderable",
            )
        )
    else:
        diffs.append(
            _diff("ORDERABILITY", "neither_orderable", "Neither side orderable")
        )

    if legacy.fulfillment_mode and catalog.fulfillment_mode is None:
        diffs.append(
            _diff(
                "FULFILLMENT",
                "fulfillment_mode_catalog_absent",
                "Legacy has fulfillment_mode; Catalog publication does not expose it",
                legacy_value=legacy.fulfillment_mode,
            )
        )

    severity = _severity_for_diffs(diffs)
    notes: list[str] = []
    if any(n.startswith("legacy_category") for n in legacy.notes):
        notes.append("legacy_category_preserved_not_as_pricing_mode")
    if severity == "dangerous":
        notes.append("ACTUAL_PARITY_FAILURE")
    return ShadowComparisonRow(
        correlation="correlated",
        legacy_identity=legacy.legacy_catalog_id,
        catalog_identity=catalog.catalog_service_id,
        service_name=catalog.name or legacy.name,
        differences=diffs,
        severity=severity,
        notes=notes,
        legacy=legacy.to_dict(),
        catalog=catalog.to_dict(),
    )


def _compute_fingerprint(report: ShadowComparisonReport) -> str:
    payload = report.to_dict()
    payload.pop("generated_at", None)
    payload.pop("fingerprint", None)
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _known_risks(
    *,
    publication_count: int,
    legacy_count: int,
    catalog_count: int,
    rows: list[ShadowComparisonRow],
) -> list[dict[str, Any]]:
    return [
        {
            "id": "provider_limits_intersection",
            "status": "deferred",
            "summary": "Legacy may apply provider∩catalog limits; published Catalog exposes catalog limits only.",
        },
        {
            "id": "fulfillment_mode_gap",
            "status": "observed"
            if any(
                d.code == "fulfillment_mode_catalog_absent"
                for r in rows
                for d in r.differences
            )
            else "known",
            "summary": (
                "fulfillment_mode_catalog_absent only when Catalog still omits "
                "fulfillment_mode while Legacy has one."
            ),
        },
        {
            "id": "target_link_validation_gap",
            "status": "observed"
            if any(
                d.code == "target_validation_requires_future_contract"
                for r in rows
                for d in r.differences
            )
            else "known",
            "summary": (
                "target_validation_requires_future_contract only when Catalog "
                "target_policy.platform_key is still absent."
            ),
        },
        {
            "id": "package_based_ordering",
            "status": "known",
            "summary": "package_based / fixed_package not orderable via current Adapter quantity contract.",
        },
        {
            "id": "location_path_semantics",
            "status": "observed",
            "summary": "Placement comparison is indeterminate: Legacy keys vs published name breadcrumbs.",
        },
        {
            "id": "identity_systems",
            "status": "observed",
            "summary": "Legacy local_item_id/catalog_id ≠ Catalog svc_*; bridge correlation only.",
        },
        {
            "id": "pre_publication_empty_catalog",
            "status": "observed"
            if publication_count == 0 and catalog_count == 0
            else "n/a",
            "summary": (
                f"publications={publication_count}; "
                f"legacy_active={legacy_count}; catalog_published={catalog_count}"
            ),
        },
        {
            "id": "partial_publication_pilot_scope",
            "status": "observed"
            if catalog_count > 0 and catalog_count < legacy_count
            else "n/a",
            "summary": (
                "Legacy-only rows outside the published pilot are "
                "PILOT_PUBLICATION_SCOPE, not ACTUAL_PARITY_FAILURE."
            ),
        },
    ]


def compare_storefronts(
    connection: sqlite3.Connection,
    *,
    generated_at: str | None = None,
) -> ShadowComparisonReport:
    """Build a deterministic Legacy vs Published Catalog shadow report (read-only)."""
    publication_count = 0
    if _table_exists(connection, "soldium_catalog_publications"):
        publication_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM soldium_catalog_publications"
            ).fetchone()[0]
        )

    legacy_snaps = load_legacy_snapshots(connection)
    catalog_snaps = load_catalog_snapshots(connection)
    bridge = load_bridge_correlation(connection)

    pre_publication = publication_count == 0 and len(catalog_snaps) == 0

    catalog_by_id = {
        s.catalog_service_id: s for s in catalog_snaps if s.catalog_service_id
    }

    def _legacy_fully_covered() -> bool:
        if not legacy_snaps:
            return True
        for leg in legacy_snaps:
            sid = bridge.get(leg.legacy_catalog_id or "")
            if not sid or sid not in catalog_by_id:
                return False
        return True

    fully_covered = _legacy_fully_covered()
    partial_pilot = len(catalog_snaps) > 0 and not fully_covered

    if pre_publication:
        baseline_state: BaselineState = "PRE_PUBLICATION_BASELINE"
    elif partial_pilot:
        baseline_state = "PARTIAL_PUBLICATION_PILOT"
    elif publication_count > 0 and len(catalog_snaps) < len(legacy_snaps):
        baseline_state = "PARTIAL_PUBLICATION"
    else:
        baseline_state = "FULL_COMPARISON"

    # Outside pilot/partial scope: legacy-only is expected, not a parity defect.
    outside_pilot_scope = pre_publication or partial_pilot

    soldium_to_legacy = {v: k for k, v in bridge.items()}

    matched_catalog_ids: set[str] = set()
    rows: list[ShadowComparisonRow] = []

    for legacy in legacy_snaps:
        soldium_id = bridge.get(legacy.legacy_catalog_id or "")
        catalog = catalog_by_id.get(soldium_id) if soldium_id else None
        if catalog is not None and soldium_id:
            matched_catalog_ids.add(soldium_id)
        structural_keys = None
        if catalog is not None and soldium_id:
            try:
                from catalog_core.pilot_parity import resolve_structural_placement_keys

                structural_keys = resolve_structural_placement_keys(
                    connection, soldium_id
                )
            except Exception:
                structural_keys = None
        rows.append(
            _compare_pair(
                legacy,
                catalog,
                outside_pilot_scope=outside_pilot_scope,
                structural_placement_keys=structural_keys,
            )
        )

    for cid, catalog in sorted(catalog_by_id.items(), key=lambda x: x[0]):
        if cid in matched_catalog_ids:
            continue
        legacy_id = soldium_to_legacy.get(cid)
        rows.append(
            _compare_pair(None, catalog, outside_pilot_scope=False)
        )
        if legacy_id:
            rows[-1].notes.append(
                f"bridge_legacy_catalog_id={legacy_id}_not_in_active_legacy"
            )

    rows.sort(
        key=lambda r: (
            {"legacy_only": 0, "correlated": 1, "catalog_only": 2}[r.correlation],
            r.legacy_identity or "",
            r.catalog_identity or "",
            r.service_name,
        )
    )

    legacy_only = sum(1 for r in rows if r.correlation == "legacy_only")
    catalog_only = sum(1 for r in rows if r.correlation == "catalog_only")
    correlated = sum(1 for r in rows if r.correlation == "correlated")
    equal = sum(1 for r in rows if r.severity == "equal")
    changed = sum(1 for r in rows if r.severity in {"dangerous", "review", "info"})
    dangerous = sum(1 for r in rows if r.severity == "dangerous")
    review = sum(1 for r in rows if r.severity == "review")
    baseline = sum(1 for r in rows if r.severity == "baseline")

    report = ShadowComparisonReport(
        generated_at=generated_at or _utcnow_iso(),
        baseline_state=baseline_state,
        publication_count=publication_count,
        legacy_count=len(legacy_snaps),
        catalog_count=len(catalog_snaps),
        correlated_count=correlated,
        legacy_only_count=legacy_only,
        catalog_only_count=catalog_only,
        equal_count=equal,
        changed_count=changed,
        dangerous_count=dangerous,
        review_count=review,
        baseline_count=baseline,
        rows=rows,
        known_risks=_known_risks(
            publication_count=publication_count,
            legacy_count=len(legacy_snaps),
            catalog_count=len(catalog_snaps),
            rows=rows,
        ),
    )
    report.fingerprint = _compute_fingerprint(report)
    return report


def compare_storefronts_at_path(
    db_path: Path | str | None = None,
    *,
    generated_at: str | None = None,
) -> ShadowComparisonReport:
    with catalog_readonly_connection(resolve_db_path(db_path)) as conn:
        return compare_storefronts(conn, generated_at=generated_at)
