# -*- coding: utf-8 -*-
"""Phase 9B.4 — Controlled pilot publication.

Deterministic selection and publication of a small eligible pilot set.
Uses CatalogPublicationService only. Never mass-publishes, never mutates
legacy ``smm_services``, prices, execution sources, structure, or mappings.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from catalog_core.errors import CatalogPublishError
from catalog_core.legacy_migration import LEGACY_SERVICE_BRIDGE_TABLE
from catalog_core.publication import CatalogPublicationService
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.storefront_adapter import StorefrontAdapter, StorefrontAdapterError
from catalog_core.pilot_parity import _sample_target_url

DEFAULT_PILOT_LIMIT = 5

# Ordinary finite commercial max — exclude sentinel INT32 max and above ceiling.
_SENTINEL_MAX_QUANTITY = 2_147_483_647
_MAX_ORDINARY_QUANTITY = 1_000_000

PilotRunStatus = Literal["BLOCKED", "DRY_RUN", "COMPLETE", "FAILED"]
PilotPublishOutcome = Literal[
    "published",
    "no_change",
    "blocked",
    "failed",
    "dry_run",
    "skipped",
]


@dataclass(frozen=True)
class PilotCandidate:
    soldium_service_id: str
    legacy_catalog_id: str
    platform_key: str
    legacy_name_ar: str
    catalog_name_ar: str
    selection_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PilotPreviewRow:
    soldium_service_id: str
    legacy_catalog_id: str
    legacy_name: str
    catalog_name: str
    location_path: list[str]
    service_type: str
    ordering_mode: str
    min_quantity: int
    max_quantity: int
    amount_millimes: int | None
    pricing_mode: str | None
    currency: str | None
    provider_slug: str | None
    provider_account_key: str | None
    external_service_id: str | None
    readiness_ready: bool
    readiness_reasons: list[str]
    publication_status: str
    legacy_bridge_status: str
    can_publish: bool
    blocking_code: str | None = None
    blocking_reasons: list[str] = field(default_factory=list)
    content_fingerprint: str | None = None
    message_ar: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "soldium_service_id": self.soldium_service_id,
            "legacy_catalog_id": self.legacy_catalog_id,
            "legacy_name": self.legacy_name,
            "catalog_name": self.catalog_name,
            "location_path": list(self.location_path),
            "location_path_label_ar": (
                " › ".join(self.location_path) if self.location_path else "الجذر"
            ),
            "service_type": self.service_type,
            "ordering_mode": self.ordering_mode,
            "min_quantity": self.min_quantity,
            "max_quantity": self.max_quantity,
            "amount_millimes": self.amount_millimes,
            "pricing_mode": self.pricing_mode,
            "currency": self.currency,
            "provider_slug": self.provider_slug,
            "provider_account_key": self.provider_account_key,
            "external_service_id": self.external_service_id,
            "readiness_ready": self.readiness_ready,
            "readiness_reasons": list(self.readiness_reasons),
            "publication_status": self.publication_status,
            "legacy_bridge_status": self.legacy_bridge_status,
            "can_publish": self.can_publish,
            "blocking_code": self.blocking_code,
            "blocking_reasons": list(self.blocking_reasons),
            "content_fingerprint": self.content_fingerprint,
            "message_ar": self.message_ar,
        }


@dataclass(frozen=True)
class PilotPublishResult:
    soldium_service_id: str
    legacy_catalog_id: str
    outcome: PilotPublishOutcome
    success: bool
    message: str
    publication_id: str | None = None
    content_fingerprint: str | None = None
    published_at: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PilotRunReport:
    status: PilotRunStatus
    candidates: list[PilotCandidate]
    previews: list[PilotPreviewRow]
    results: list[PilotPublishResult]
    published_ids: list[str] = field(default_factory=list)
    rejected_ids: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    dry_run: bool = False
    published_by: str = "phase9b4_pilot"
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "dry_run": self.dry_run,
            "published_by": self.published_by,
            "candidates": [c.to_dict() for c in self.candidates],
            "previews": [p.to_dict() for p in self.previews],
            "results": [r.to_dict() for r in self.results],
            "published_ids": list(self.published_ids),
            "rejected_ids": list(self.rejected_ids),
            "blocked_reasons": list(self.blocked_reasons),
        }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    keys = set(row.keys())
    if key not in keys:
        return default
    return row[key]


def _is_published(pub: CatalogPublicationService, service_id: str) -> bool:
    latest = pub.get_latest_event(service_id)
    return bool(latest and latest.event_type == "publish")


def _legacy_active_row(
    conn: sqlite3.Connection, legacy_catalog_id: str
) -> sqlite3.Row | None:
    """Active Legacy storefront row for a bridge catalog_id."""
    if not _table_exists(conn, "smm_services"):
        return None
    return conn.execute(
        """
        SELECT *
        FROM smm_services
        WHERE catalog_id = ?
          AND is_active = 1
          AND platform_key != ''
        LIMIT 1
        """,
        (legacy_catalog_id,),
    ).fetchone()


def _passes_legacy_hard_filters(legacy_row: sqlite3.Row) -> bool:
    platform_key = str(_row_get(legacy_row, "platform_key") or "").strip()
    if not platform_key:
        return False
    fulfillment = str(_row_get(legacy_row, "fulfillment_mode") or "").strip().lower()
    if fulfillment == "admin":
        return False
    category = str(_row_get(legacy_row, "category") or "").strip()
    if category == "per_unit":
        return False
    return True


def _passes_catalog_hard_filters(
    repo: CatalogRepository,
    pub: CatalogPublicationService,
    soldium_service_id: str,
) -> tuple[bool, str]:
    """Return (ok, reason_if_rejected)."""
    service = repo.get_service(soldium_service_id)
    if service is None:
        return False, "catalog_service_missing"
    if service.status != "active":
        return False, f"catalog_status_{service.status}"

    if _is_published(pub, soldium_service_id):
        return False, "already_published"

    entry = repo.get_entry_for_service(soldium_service_id)
    if entry is None:
        return False, "missing_placement_entry"
    service.entry_id = entry.id
    service.parent_entry_id = entry.parent_entry_id
    service.location_path = repo.breadcrumb_names(entry.parent_entry_id)
    if not service.location_path:
        return False, "empty_location_path"

    source = repo.get_active_execution_source(soldium_service_id)
    if source is None:
        return False, "missing_execution_source"
    if not str(source.provider_slug or "").strip():
        return False, "missing_provider_slug"
    if not str(source.provider_account_key or "").strip():
        return False, "missing_provider_account"
    if not str(source.external_service_id or "").strip():
        return False, "missing_external_service_id"

    price = repo.get_active_price(soldium_service_id)
    if price is None:
        return False, "missing_active_price"
    if int(price.amount_millimes) <= 0:
        return False, "non_positive_price"
    if str(price.pricing_mode or "").strip() != "per_1000":
        return False, f"unsupported_pricing_mode:{price.pricing_mode}"

    if str(service.ordering_mode or "").strip() != "quantity_based":
        return False, f"unsupported_ordering_mode:{service.ordering_mode}"

    min_q = int(service.min_quantity)
    max_q = int(service.max_quantity)
    if min_q < 1:
        return False, "min_quantity_below_1"
    if max_q < min_q:
        return False, "max_below_min"
    if max_q >= _SENTINEL_MAX_QUANTITY:
        return False, "sentinel_max_quantity"
    if max_q > _MAX_ORDINARY_QUANTITY:
        return False, "max_quantity_above_ordinary_ceiling"

    readiness = evaluate_service_readiness(
        repo, service, source=source, price=price
    )
    if not readiness.ready:
        return False, "readiness_not_ready"

    return True, "eligible"


def list_eligible_pilot_pool(conn: sqlite3.Connection) -> list[PilotCandidate]:
    """All services passing Phase 9B.4 hard eligibility filters (sorted)."""
    if not _table_exists(conn, LEGACY_SERVICE_BRIDGE_TABLE):
        return []
    if not _table_exists(conn, "smm_services"):
        return []

    repo = CatalogRepository(conn)
    pub = CatalogPublicationService(conn)
    bridges = conn.execute(
        f"""
        SELECT legacy_catalog_id, soldium_service_id
        FROM {LEGACY_SERVICE_BRIDGE_TABLE}
        ORDER BY legacy_catalog_id ASC, soldium_service_id ASC
        """
    ).fetchall()

    pool: list[PilotCandidate] = []
    for br in bridges:
        legacy_id = str(br["legacy_catalog_id"] or "").strip()
        soldium_id = str(br["soldium_service_id"] or "").strip()
        if not legacy_id or not soldium_id:
            continue

        legacy_row = _legacy_active_row(conn, legacy_id)
        if legacy_row is None:
            continue
        if not _passes_legacy_hard_filters(legacy_row):
            continue

        ok, reason = _passes_catalog_hard_filters(repo, pub, soldium_id)
        if not ok:
            continue

        platform_key = str(_row_get(legacy_row, "platform_key") or "").strip()
        legacy_name = str(_row_get(legacy_row, "name_ar") or "").strip()
        service = repo.get_service(soldium_id)
        catalog_name = (service.name_ar if service else "") or ""

        pool.append(
            PilotCandidate(
                soldium_service_id=soldium_id,
                legacy_catalog_id=legacy_id,
                platform_key=platform_key,
                legacy_name_ar=legacy_name,
                catalog_name_ar=catalog_name,
                selection_reason=(
                    "hard_filters_pass: bridge+active_legacy+ready+"
                    "per_1000+quantity_based+ordinary_limits+placement+"
                    "execution+unpublished"
                ),
            )
        )

    pool.sort(
        key=lambda c: (
            c.platform_key,
            c.legacy_catalog_id,
            c.soldium_service_id,
        )
    )
    return pool


def select_pilot_candidates(
    conn: sqlite3.Connection,
    limit: int = DEFAULT_PILOT_LIMIT,
) -> list[PilotCandidate]:
    """Deterministic pilot selection: platform diversity first, then fill by sort.

    Sort key for the eligible pool: ``(platform_key, legacy_catalog_id,
    soldium_service_id)``. First pass takes the earliest service per distinct
    platform; second pass fills remaining slots from the leftover pool in the
    same sort order. Never random.
    """
    if limit <= 0:
        return []

    pool = list_eligible_pilot_pool(conn)
    if not pool:
        return []

    selected: list[PilotCandidate] = []
    remaining: list[PilotCandidate] = []
    seen_platforms: set[str] = set()

    for candidate in pool:
        if len(selected) >= limit:
            remaining.append(candidate)
            continue
        if candidate.platform_key not in seen_platforms:
            selected.append(
                PilotCandidate(
                    soldium_service_id=candidate.soldium_service_id,
                    legacy_catalog_id=candidate.legacy_catalog_id,
                    platform_key=candidate.platform_key,
                    legacy_name_ar=candidate.legacy_name_ar,
                    catalog_name_ar=candidate.catalog_name_ar,
                    selection_reason=(
                        f"platform_diversity:{candidate.platform_key}; "
                        f"{candidate.selection_reason}"
                    ),
                )
            )
            seen_platforms.add(candidate.platform_key)
        else:
            remaining.append(candidate)

    for candidate in remaining:
        if len(selected) >= limit:
            break
        selected.append(
            PilotCandidate(
                soldium_service_id=candidate.soldium_service_id,
                legacy_catalog_id=candidate.legacy_catalog_id,
                platform_key=candidate.platform_key,
                legacy_name_ar=candidate.legacy_name_ar,
                catalog_name_ar=candidate.catalog_name_ar,
                selection_reason=(
                    f"fill_by_sort:{candidate.platform_key}/"
                    f"{candidate.legacy_catalog_id}; "
                    f"{candidate.selection_reason}"
                ),
            )
        )

    return selected


def _preview_one(
    conn: sqlite3.Connection,
    candidate: PilotCandidate,
) -> PilotPreviewRow:
    repo = CatalogRepository(conn)
    pub = CatalogPublicationService(conn)
    service = repo.get_service(candidate.soldium_service_id)

    entry = (
        repo.get_entry_for_service(candidate.soldium_service_id) if service else None
    )
    location_path: list[str] = []
    if service and entry:
        service.entry_id = entry.id
        service.parent_entry_id = entry.parent_entry_id
        location_path = list(repo.breadcrumb_names(entry.parent_entry_id))
        service.location_path = location_path

    source = (
        repo.get_active_execution_source(candidate.soldium_service_id)
        if service
        else None
    )
    price = (
        repo.get_active_price(candidate.soldium_service_id) if service else None
    )
    readiness = evaluate_service_readiness(
        repo, service, source=source, price=price
    )
    readiness_reasons = [i.title for i in readiness.issues] or (
        [] if readiness.ready else [readiness.state_label_ar]
    )

    pub_state = pub.get_publication_status(candidate.soldium_service_id)
    preview = pub.preview(candidate.soldium_service_id)

    snap = preview.snapshot or {}
    return PilotPreviewRow(
        soldium_service_id=candidate.soldium_service_id,
        legacy_catalog_id=candidate.legacy_catalog_id,
        legacy_name=candidate.legacy_name_ar,
        catalog_name=(service.name_ar if service else candidate.catalog_name_ar),
        location_path=list(snap.get("location_path") or location_path),
        service_type=str(
            snap.get("service_type")
            or (service.service_type if service else "")
            or ""
        ),
        ordering_mode=str(
            snap.get("ordering_mode")
            or (service.ordering_mode if service else "")
            or ""
        ),
        min_quantity=int(
            snap.get("min_quantity")
            if snap.get("min_quantity") is not None
            else (service.min_quantity if service else 0)
        ),
        max_quantity=int(
            snap.get("max_quantity")
            if snap.get("max_quantity") is not None
            else (service.max_quantity if service else 0)
        ),
        amount_millimes=(
            int(snap["amount_millimes"])
            if snap.get("amount_millimes") is not None
            else (int(price.amount_millimes) if price else None)
        ),
        pricing_mode=(
            str(snap.get("pricing_mode"))
            if snap.get("pricing_mode") is not None
            else (price.pricing_mode if price else None)
        ),
        currency=(
            str(snap.get("currency"))
            if snap.get("currency") is not None
            else (price.currency if price else None)
        ),
        provider_slug=(
            str(snap.get("provider_slug"))
            if snap.get("provider_slug") is not None
            else (source.provider_slug if source else None)
        ),
        provider_account_key=(
            str(snap.get("provider_account_key"))
            if snap.get("provider_account_key") is not None
            else (source.provider_account_key if source else None)
        ),
        external_service_id=(
            str(snap.get("external_service_id"))
            if snap.get("external_service_id") is not None
            else (str(source.external_service_id) if source else None)
        ),
        readiness_ready=bool(readiness.ready),
        readiness_reasons=list(readiness_reasons),
        publication_status=str(pub_state.get("publication_status") or "unpublished"),
        legacy_bridge_status="present",
        can_publish=bool(preview.can_publish),
        blocking_code=preview.blocking_code,
        blocking_reasons=list(preview.blocking_reasons or []),
        content_fingerprint=preview.content_fingerprint,
        message_ar=preview.message_ar or "",
    )


def preview_pilot(
    conn: sqlite3.Connection,
    candidates: list[PilotCandidate],
) -> list[PilotPreviewRow]:
    """Publication preview for each pilot candidate (no writes)."""
    return [_preview_one(conn, c) for c in candidates]


def publish_pilot(
    conn: sqlite3.Connection,
    candidates: list[PilotCandidate],
    published_by: str = "phase9b4_pilot",
    dry_run: bool = False,
) -> PilotRunReport:
    """Publish the given pilot set via CatalogPublicationService only.

    Preflight: if any candidate has ``can_publish`` false → BLOCKED and publish
    nothing. On a mid-run publish failure, stop without auto-picking replacements.
    """
    by = (published_by or "").strip() or "phase9b4_pilot"
    candidates = list(candidates or [])
    previews = preview_pilot(conn, candidates)

    blocked_reasons: list[str] = []
    for row in previews:
        if not row.can_publish:
            detail = row.blocking_code or "cannot_publish"
            reasons = "; ".join(row.blocking_reasons) if row.blocking_reasons else detail
            blocked_reasons.append(
                f"{row.soldium_service_id}: {reasons}"
            )

    if not candidates:
        return PilotRunReport(
            status="BLOCKED",
            candidates=[],
            previews=[],
            results=[],
            blocked_reasons=["empty_candidate_set"],
            dry_run=dry_run,
            published_by=by,
            message="No pilot candidates provided.",
        )

    if blocked_reasons:
        results = [
            PilotPublishResult(
                soldium_service_id=row.soldium_service_id,
                legacy_catalog_id=row.legacy_catalog_id,
                outcome="blocked",
                success=False,
                message=row.message_ar or "Publication preflight blocked.",
                reason=row.blocking_code
                or ("; ".join(row.blocking_reasons) if row.blocking_reasons else "blocked"),
                content_fingerprint=row.content_fingerprint,
            )
            for row in previews
        ]
        return PilotRunReport(
            status="BLOCKED",
            candidates=candidates,
            previews=previews,
            results=results,
            published_ids=[],
            rejected_ids=[c.soldium_service_id for c in candidates],
            blocked_reasons=blocked_reasons,
            dry_run=dry_run,
            published_by=by,
            message="Pilot publication blocked — preflight can_publish=false.",
        )

    if dry_run:
        results = [
            PilotPublishResult(
                soldium_service_id=row.soldium_service_id,
                legacy_catalog_id=row.legacy_catalog_id,
                outcome="dry_run",
                success=True,
                message="Dry run — would publish (preflight passed).",
                content_fingerprint=row.content_fingerprint,
            )
            for row in previews
        ]
        return PilotRunReport(
            status="DRY_RUN",
            candidates=candidates,
            previews=previews,
            results=results,
            published_ids=[],
            rejected_ids=[],
            blocked_reasons=[],
            dry_run=True,
            published_by=by,
            message="Dry run complete — no publications written.",
        )

    pub = CatalogPublicationService(conn)
    results: list[PilotPublishResult] = []
    published_ids: list[str] = []
    rejected_ids: list[str] = []
    legacy_by_svc = {c.soldium_service_id: c.legacy_catalog_id for c in candidates}

    for idx, candidate in enumerate(candidates):
        preview_row = previews[idx]
        try:
            outcome = pub.publish(
                candidate.soldium_service_id,
                published_by=by,
                expected_content_fingerprint=preview_row.content_fingerprint,
            )
        except CatalogPublishError as exc:
            rejected_ids.append(candidate.soldium_service_id)
            results.append(
                PilotPublishResult(
                    soldium_service_id=candidate.soldium_service_id,
                    legacy_catalog_id=candidate.legacy_catalog_id,
                    outcome="failed",
                    success=False,
                    message=exc.message,
                    reason=exc.code,
                )
            )
            # Skip remaining without auto-picking replacements.
            for skipped in candidates[idx + 1 :]:
                rejected_ids.append(skipped.soldium_service_id)
                results.append(
                    PilotPublishResult(
                        soldium_service_id=skipped.soldium_service_id,
                        legacy_catalog_id=skipped.legacy_catalog_id,
                        outcome="skipped",
                        success=False,
                        message="Skipped after prior publish failure.",
                        reason="stopped_after_failure",
                    )
                )
            return PilotRunReport(
                status="FAILED",
                candidates=candidates,
                previews=previews,
                results=results,
                published_ids=published_ids,
                rejected_ids=rejected_ids,
                blocked_reasons=[
                    f"{candidate.soldium_service_id}: {exc.code or exc.message}"
                ],
                dry_run=False,
                published_by=by,
                message=(
                    "Pilot publication stopped after failure — "
                    "no automatic replacement selected."
                ),
            )

        publication = outcome.publication or {}
        success = outcome.outcome in ("published", "no_change")
        if success:
            published_ids.append(candidate.soldium_service_id)
        else:
            rejected_ids.append(candidate.soldium_service_id)

        results.append(
            PilotPublishResult(
                soldium_service_id=candidate.soldium_service_id,
                legacy_catalog_id=legacy_by_svc[candidate.soldium_service_id],
                outcome=outcome.outcome,  # type: ignore[arg-type]
                success=success,
                message=outcome.message_ar,
                publication_id=publication.get("id"),
                content_fingerprint=publication.get("content_fingerprint"),
                published_at=publication.get("published_at"),
                reason=None if success else outcome.outcome,
            )
        )

    return PilotRunReport(
        status="COMPLETE",
        candidates=candidates,
        previews=previews,
        results=results,
        published_ids=published_ids,
        rejected_ids=rejected_ids,
        blocked_reasons=[],
        dry_run=False,
        published_by=by,
        message=f"Pilot publication complete — {len(published_ids)} service(s).",
    )


def validate_pilot_intents(
    conn: sqlite3.Connection,
    service_ids: list[str],
) -> list[dict[str, Any]]:
    """Post-publish StorefrontAdapter order-intent checks (no DB writes).

    For each service: get_service → validate_quantity → quote_price →
    resolve_order_intent. Verifies published identity, price, limits, execution,
    and fingerprint on the intent.
    """
    adapter = StorefrontAdapter(conn)
    out: list[dict[str, Any]] = []

    for raw_id in service_ids:
        service_id = str(raw_id or "").strip()
        row: dict[str, Any] = {
            "service_id": service_id,
            "ok": False,
            "checks": {},
            "intent": None,
            "error_code": None,
            "error_message": None,
        }
        if not service_id:
            row["error_code"] = "empty_service_id"
            row["error_message"] = "Empty service id"
            out.append(row)
            continue

        try:
            svc = adapter.get_service(service_id)
            quantity = int(svc.min_quantity)
            qty_check = adapter.validate_quantity(service_id, quantity)
            quote = adapter.quote_price(service_id, quantity)
            intent = adapter.resolve_order_intent(
                service_id,
                quantity,
                target=_sample_target_url(svc.target_policy.platform_key),
            )

            checks = {
                "svc_identity": bool(
                    intent.service_id
                    and str(intent.service_id).startswith("svc_")
                    and intent.service_id == service_id
                ),
                "published_name": bool(
                    intent.service_name_ar
                    and intent.service_name_ar == svc.name_ar
                ),
                "published_price": (
                    intent.quoted_amount_millimes == quote.quoted_amount_millimes
                    and intent.currency == svc.price.currency
                    and intent.pricing_mode == svc.price.pricing_mode
                ),
                "published_limits": (
                    qty_check.ok
                    and qty_check.min_quantity == svc.min_quantity
                    and qty_check.max_quantity == svc.max_quantity
                ),
                "published_execution": (
                    intent.provider_slug == svc.execution.provider_slug
                    and intent.provider_account_key
                    == svc.execution.provider_account_key
                    and intent.external_service_id
                    == svc.execution.external_service_id
                ),
                "publication_fingerprint": bool(
                    intent.content_fingerprint
                    and intent.content_fingerprint == svc.content_fingerprint
                ),
            }
            row["checks"] = checks
            row["intent"] = intent.to_dict()
            row["ok"] = all(checks.values())
            if not row["ok"]:
                failed = [k for k, v in checks.items() if not v]
                row["error_code"] = "intent_check_failed"
                row["error_message"] = "Failed checks: " + ", ".join(failed)
        except StorefrontAdapterError as exc:
            row["error_code"] = exc.code
            row["error_message"] = str(exc.message)
        except Exception as exc:  # noqa: BLE001 — surface unexpected adapter failures
            row["error_code"] = "unexpected_error"
            row["error_message"] = str(exc)

        out.append(row)

    return out
