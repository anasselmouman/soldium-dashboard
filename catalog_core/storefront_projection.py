# -*- coding: utf-8 -*-
"""Phase 9B.1 — Published Storefront Projection (Catalog application layer).

Read-only customer-facing view of the Catalog derived from publication history
snapshots. Never reads ``smm_services``. Never uses the live admin tree for
customer placement or display fields.

Eligibility (existing architecture):

    latest event is publish
    AND live service is not archived
    AND derived readiness.ready

Display / commercial / price / execution fields for eligible services come from
the latest **publish** snapshot, not live draft rows.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from catalog_core.commercial import (
    fulfillment_mode_label_ar,
    ordering_mode_label_ar,
    service_type_label_ar,
)
from catalog_core.errors import CatalogNotFoundError
from catalog_core.pricing import format_dh_amount, pricing_mode_label_ar
from catalog_core.publication import CatalogPublicationService, PublicationRecord
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.target_validation import resolve_link_prompt

logger = logging.getLogger("soldium.catalog.storefront_projection")

PlacementDepth = Literal["platform", "section", "subsection", "root"]


@dataclass(frozen=True)
class PublishedExecutionIdentity:
    """Frozen provider routing from the publication snapshot (not live source)."""

    provider_slug: str
    provider_account_key: str
    external_service_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_slug": self.provider_slug,
            "provider_account_key": self.provider_account_key,
            "external_service_id": self.external_service_id,
        }


@dataclass(frozen=True)
class PublishedTargetPolicy:
    """Structural target/link policy frozen at publication (not Arabic labels)."""

    required: bool
    platform_key: str
    section_key: str
    subsection_key: str | None = None
    link_prompt_key: str | None = None
    link_type: str | None = None
    allow_username: bool = False
    allow_free_text: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "platform_key": self.platform_key,
            "section_key": self.section_key,
            "subsection_key": self.subsection_key,
            "link_prompt_key": self.link_prompt_key,
            "link_type": self.link_type,
            "allow_username": self.allow_username,
            "allow_free_text": self.allow_free_text,
        }


@dataclass(frozen=True)
class PublishedStorefrontService:
    """Customer-facing published service (SOLDIUM identity)."""

    service_id: str
    name_ar: str
    note_ar: str
    service_type: str
    ordering_mode: str
    min_quantity: int
    max_quantity: int
    amount_millimes: int
    currency: str
    pricing_mode: str
    location_path: tuple[str, ...]
    platform_label: str | None
    section_label: str | None
    subsection_label: str | None
    content_fingerprint: str | None
    published_at: str | None
    execution: PublishedExecutionIdentity
    fulfillment_mode: str
    target_policy: PublishedTargetPolicy

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "name_ar": self.name_ar,
            "note_ar": self.note_ar,
            "service_type": self.service_type,
            "service_type_label_ar": service_type_label_ar(self.service_type),
            "ordering_mode": self.ordering_mode,
            "ordering_mode_label_ar": ordering_mode_label_ar(self.ordering_mode),
            "min_quantity": self.min_quantity,
            "max_quantity": self.max_quantity,
            "amount_millimes": self.amount_millimes,
            "amount_dh": format_dh_amount(self.amount_millimes),
            "currency": self.currency,
            "pricing_mode": self.pricing_mode,
            "pricing_mode_label_ar": pricing_mode_label_ar(self.pricing_mode),
            "location_path": list(self.location_path),
            "location_path_label_ar": (
                " › ".join(self.location_path) if self.location_path else "الجذر"
            ),
            "platform_label": self.platform_label,
            "section_label": self.section_label,
            "subsection_label": self.subsection_label,
            "content_fingerprint": self.content_fingerprint,
            "published_at": self.published_at,
            "fulfillment_mode": self.fulfillment_mode,
            "fulfillment_mode_label_ar": fulfillment_mode_label_ar(self.fulfillment_mode),
            "target_policy": self.target_policy.to_dict(),
            "execution": self.execution.to_dict(),
        }


@dataclass(frozen=True)
class PublishedStorefrontNode:
    label: str
    path: tuple[str, ...]
    depth: PlacementDepth
    service_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "path": list(self.path),
            "depth": self.depth,
            "service_count": self.service_count,
        }


@dataclass
class PublishedStorefrontCatalog:
    """Consistent in-memory published catalog for one projection build."""

    services: list[PublishedStorefrontService] = field(default_factory=list)
    excluded_count: int = 0
    malformed_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_count": len(self.services),
            "excluded_count": self.excluded_count,
            "malformed_count": self.malformed_count,
            "services": [s.to_dict() for s in self.services],
            "platforms": [p.to_dict() for p in _platforms_from_services(self.services)],
        }


def _placement_from_path(
    path: list[str],
) -> tuple[str | None, str | None, str | None]:
    """Map publication breadcrumb names → platform / section / subsection labels.

    Publication ``location_path`` is Arabic (or other) node **names**, not legacy
    ``platform_key`` / ``section_key``. Depth > 3 collapses into subsection label.
    """
    cleaned = [str(x).strip() for x in path if str(x).strip()]
    if not cleaned:
        return None, None, None
    platform = cleaned[0]
    section = cleaned[1] if len(cleaned) > 1 else None
    if len(cleaned) <= 2:
        subsection = None
    elif len(cleaned) == 3:
        subsection = cleaned[2]
    else:
        subsection = " › ".join(cleaned[2:])
    return platform, section, subsection


def _validate_publish_snapshot(rec: PublicationRecord) -> str | None:
    """Return a short reason if the publish row cannot be projected; else None."""
    if rec.event_type != "publish":
        return "not_publish_event"
    if not str(rec.service_id or "").startswith("svc_"):
        return "invalid_service_id"
    if not (rec.name_ar or "").strip():
        return "missing_name"
    if rec.min_quantity is None or rec.max_quantity is None:
        return "missing_quantities"
    if int(rec.min_quantity) < 1 or int(rec.max_quantity) < int(rec.min_quantity):
        return "invalid_quantities"
    if rec.amount_millimes is None or int(rec.amount_millimes) <= 0:
        return "invalid_price"
    if not (rec.currency or "").strip():
        return "missing_currency"
    if not (rec.pricing_mode or "").strip():
        return "missing_pricing_mode"
    if not (rec.service_type or "").strip() or not (rec.ordering_mode or "").strip():
        return "missing_commercial"
    if not (rec.provider_slug or "").strip():
        return "missing_provider_slug"
    if not (rec.provider_account_key or "").strip():
        return "missing_provider_account"
    if not str(rec.external_service_id or "").strip():
        return "missing_external_service_id"
    if rec.location_path is None:
        return "missing_location_path"
    mode = str(rec.fulfillment_mode or "").strip().lower()
    if mode not in {"auto", "admin"}:
        return "missing_fulfillment_mode"
    if not (rec.target_platform_key or "").strip():
        return "missing_target_platform_key"
    if not (rec.target_section_key or "").strip():
        return "missing_target_section_key"
    return None


def _target_policy_from_publish(rec: PublicationRecord) -> PublishedTargetPolicy:
    platform = str(rec.target_platform_key or "").strip()
    section = str(rec.target_section_key or "").strip()
    subsection = (
        str(rec.target_subsection_key).strip() if rec.target_subsection_key else None
    ) or None
    link_prompt_key = (
        str(rec.target_link_prompt_key).strip() if rec.target_link_prompt_key else None
    ) or None
    link_type = (
        str(rec.target_link_type).strip() if rec.target_link_type else None
    ) or None
    service: dict[str, Any] = {}
    if link_prompt_key:
        service["link_prompt_key"] = link_prompt_key
    if link_type:
        service["link_type"] = link_type
    _, allow_username, allow_free_text = resolve_link_prompt(
        platform, section, subsection, service=service
    )
    return PublishedTargetPolicy(
        required=True,
        platform_key=platform,
        section_key=section,
        subsection_key=subsection,
        link_prompt_key=link_prompt_key,
        link_type=link_type,
        allow_username=allow_username,
        allow_free_text=allow_free_text,
    )


def _service_from_publish(rec: PublicationRecord) -> PublishedStorefrontService:
    path = tuple(str(x) for x in (rec.location_path or []))
    platform, section, subsection = _placement_from_path(list(path))
    return PublishedStorefrontService(
        service_id=str(rec.service_id),
        name_ar=str(rec.name_ar or "").strip(),
        note_ar=str(rec.note_ar or "").strip(),
        service_type=str(rec.service_type),
        ordering_mode=str(rec.ordering_mode),
        min_quantity=int(rec.min_quantity or 0),
        max_quantity=int(rec.max_quantity or 0),
        amount_millimes=int(rec.amount_millimes or 0),
        currency=str(rec.currency or "MAD"),
        pricing_mode=str(rec.pricing_mode),
        location_path=path,
        platform_label=platform,
        section_label=section,
        subsection_label=subsection,
        content_fingerprint=rec.content_fingerprint,
        published_at=rec.published_at,
        execution=PublishedExecutionIdentity(
            provider_slug=str(rec.provider_slug).strip().lower(),
            provider_account_key=str(rec.provider_account_key).strip().lower(),
            external_service_id=str(rec.external_service_id).strip(),
        ),
        fulfillment_mode=str(rec.fulfillment_mode).strip().lower(),
        target_policy=_target_policy_from_publish(rec),
    )


def _platforms_from_services(
    services: list[PublishedStorefrontService],
) -> list[PublishedStorefrontNode]:
    counts: dict[str, int] = {}
    for svc in services:
        label = svc.platform_label or ""
        counts[label] = counts.get(label, 0) + 1
    nodes: list[PublishedStorefrontNode] = []
    for label in sorted(counts.keys(), key=lambda x: (x == "", x)):
        nodes.append(
            PublishedStorefrontNode(
                label=label,
                path=(label,) if label else tuple(),
                depth="platform" if label else "root",
                service_count=counts[label],
            )
        )
    return nodes


class PublishedStorefrontProjection:
    """Build a read-only published Catalog projection for future storefront adapters."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.repo = CatalogRepository(connection)
        self.publications = CatalogPublicationService(connection)

    def build(self) -> PublishedStorefrontCatalog:
        """Materialize the full eligible published catalog (consistent for this call)."""
        services: list[PublishedStorefrontService] = []
        excluded = 0
        malformed = 0

        for latest in self.publications.list_latest_events():
            if latest.event_type != "publish":
                excluded += 1
                continue

            live = self.repo.get_service(latest.service_id)
            if live is None:
                excluded += 1
                continue
            if live.status == "archived":
                excluded += 1
                continue

            source = self.repo.get_active_execution_source(latest.service_id)
            price = self.repo.get_active_price(latest.service_id)
            # Placement for readiness uses live tree (existing readiness contract).
            entry = self.repo.get_entry_for_service(latest.service_id)
            if entry:
                live.entry_id = entry.id
                live.parent_entry_id = entry.parent_entry_id
                live.location_path = self.repo.breadcrumb_names(entry.parent_entry_id)

            readiness = evaluate_service_readiness(
                self.repo, live, source=source, price=price
            )
            if not readiness.ready:
                excluded += 1
                continue

            # Customer fields: latest publish snapshot (may differ from live draft).
            # When latest event is publish, that row is the authoritative snapshot.
            publish_snap = latest
            reason = _validate_publish_snapshot(publish_snap)
            if reason is not None:
                malformed += 1
                logger.warning(
                    "storefront_projection skip malformed publish service_id=%s reason=%s",
                    publish_snap.service_id,
                    reason,
                )
                continue

            services.append(_service_from_publish(publish_snap))

        services.sort(key=lambda s: (s.name_ar.casefold(), s.service_id))
        return PublishedStorefrontCatalog(
            services=services,
            excluded_count=excluded,
            malformed_count=malformed,
        )

    def list_platforms(self) -> list[PublishedStorefrontNode]:
        return _platforms_from_services(self.build().services)

    def list_sections(self, platform_label: str) -> list[PublishedStorefrontNode]:
        plat = str(platform_label or "").strip()
        counts: dict[str, int] = {}
        for svc in self.build().services:
            if (svc.platform_label or "") != plat:
                continue
            if not svc.section_label:
                continue
            counts[svc.section_label] = counts.get(svc.section_label, 0) + 1
        return [
            PublishedStorefrontNode(
                label=label,
                path=(plat, label),
                depth="section",
                service_count=counts[label],
            )
            for label in sorted(counts.keys())
        ]

    def list_subsections(
        self, platform_label: str, section_label: str
    ) -> list[PublishedStorefrontNode]:
        plat = str(platform_label or "").strip()
        sect = str(section_label or "").strip()
        counts: dict[str, int] = {}
        for svc in self.build().services:
            if (svc.platform_label or "") != plat:
                continue
            if (svc.section_label or "") != sect:
                continue
            if not svc.subsection_label:
                continue
            counts[svc.subsection_label] = counts.get(svc.subsection_label, 0) + 1
        return [
            PublishedStorefrontNode(
                label=label,
                path=(plat, sect, label),
                depth="subsection",
                service_count=counts[label],
            )
            for label in sorted(counts.keys())
        ]

    def list_services(
        self,
        *,
        platform_label: str | None = None,
        section_label: str | None = None,
        subsection_label: str | None = None,
    ) -> list[PublishedStorefrontService]:
        plat = None if platform_label is None else str(platform_label).strip()
        sect = None if section_label is None else str(section_label).strip()
        sub = None if subsection_label is None else str(subsection_label).strip()

        out: list[PublishedStorefrontService] = []
        for svc in self.build().services:
            if plat is not None and (svc.platform_label or "") != plat:
                continue
            if sect is not None and (svc.section_label or "") != sect:
                continue
            if sub is not None:
                if (svc.subsection_label or "") != sub:
                    continue
            elif sect is not None:
                # Section-level listing: only services without a subsection.
                if svc.subsection_label:
                    continue
            out.append(svc)
        return out

    def get_service(self, service_id: str) -> PublishedStorefrontService:
        sid = str(service_id or "").strip()
        if not sid:
            raise CatalogNotFoundError("الخدمة غير موجودة")
        for svc in self.build().services:
            if svc.service_id == sid:
                return svc
        raise CatalogNotFoundError("الخدمة غير موجودة في الكتالوج المنشور")
