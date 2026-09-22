# -*- coding: utf-8 -*-
"""Catalog customer storefront projection (live Catalog SoT).

Publication is a visibility gate only. Customer-facing commercial, execution,
target, and placement fields come from the **live** Catalog (services, prices,
execution sources, entries tree) — not from publication snapshots.

Eligibility:

    latest publication event is publish
    AND live service status == active
    AND live service not archived
    AND derived readiness.ready

Never reads ``smm_services``.
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
from catalog_core.models import CatalogPrice, CatalogService, ExecutionSource
from catalog_core.pricing import format_dh_amount, pricing_mode_label_ar
from catalog_core.publication import CatalogPublicationService
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.target_validation import resolve_link_prompt

logger = logging.getLogger("soldium.catalog.storefront_projection")

PlacementDepth = Literal["platform", "section", "subsection", "root"]


@dataclass(frozen=True)
class PublishedExecutionIdentity:
    """Live Catalog execution source (customer-facing)."""

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
    """Live Catalog target/link policy (not placement)."""

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
    """Customer-facing Catalog service (live fields + publication metadata)."""

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
    parent_entry_id: str | None = None
    sort_order: int = 0

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
            "parent_entry_id": self.parent_entry_id,
            "sort_order": self.sort_order,
        }


@dataclass(frozen=True)
class PublishedStorefrontNode:
    label: str
    path: tuple[str, ...]
    depth: PlacementDepth
    service_count: int
    entry_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "path": list(self.path),
            "depth": self.depth,
            "service_count": self.service_count,
            "entry_id": self.entry_id,
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
    """Map live breadcrumb names → platform / section / subsection labels."""
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


def _validate_live_customer_fields(
    service: CatalogService,
    source: ExecutionSource | None,
    price: CatalogPrice | None,
) -> str | None:
    """Return a short reason if live fields cannot be shown; else None."""
    if not str(service.id or "").startswith("svc_"):
        return "invalid_service_id"
    if not (service.name_ar or "").strip():
        return "missing_name"
    if int(service.min_quantity or 0) < 1 or int(service.max_quantity or 0) < int(
        service.min_quantity or 0
    ):
        return "invalid_quantities"
    if price is None or int(price.amount_millimes or 0) <= 0:
        return "invalid_price"
    if not (price.currency or "").strip():
        return "missing_currency"
    if not (price.pricing_mode or "").strip():
        return "missing_pricing_mode"
    if not (service.service_type or "").strip() or not (service.ordering_mode or "").strip():
        return "missing_commercial"
    if source is None:
        return "missing_execution_source"
    if not (source.provider_slug or "").strip():
        return "missing_provider_slug"
    if not (source.provider_account_key or "").strip():
        return "missing_provider_account"
    if not str(source.external_service_id or "").strip():
        return "missing_external_service_id"
    mode = str(service.fulfillment_mode or "").strip().lower()
    if mode not in {"auto", "admin"}:
        return "missing_fulfillment_mode"
    if not (service.target_platform_key or "").strip():
        return "missing_target_platform_key"
    if not (service.target_section_key or "").strip():
        return "missing_target_section_key"
    return None


def _target_policy_from_live(service: CatalogService) -> PublishedTargetPolicy:
    platform = str(service.target_platform_key or "").strip()
    section = str(service.target_section_key or "").strip()
    subsection = (
        str(service.target_subsection_key).strip()
        if service.target_subsection_key
        else None
    ) or None
    link_prompt_key = (
        str(service.target_link_prompt_key).strip()
        if service.target_link_prompt_key
        else None
    ) or None
    link_type = (
        str(service.target_link_type).strip() if service.target_link_type else None
    ) or None
    svc_dict: dict[str, Any] = {}
    if link_prompt_key:
        svc_dict["link_prompt_key"] = link_prompt_key
    if link_type:
        svc_dict["link_type"] = link_type
    _, allow_username, allow_free_text = resolve_link_prompt(
        platform, section, subsection, service=svc_dict
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


def _service_from_live(
    service: CatalogService,
    source: ExecutionSource,
    price: CatalogPrice,
    *,
    location_path: list[str],
    parent_entry_id: str | None,
    sort_order: int,
    published_at: str | None,
    content_fingerprint: str | None,
) -> PublishedStorefrontService:
    path = tuple(str(x) for x in location_path)
    platform, section, subsection = _placement_from_path(list(path))
    return PublishedStorefrontService(
        service_id=str(service.id),
        name_ar=str(service.name_ar or "").strip(),
        note_ar=str(service.note_ar or "").strip(),
        service_type=str(service.service_type),
        ordering_mode=str(service.ordering_mode),
        min_quantity=int(service.min_quantity or 0),
        max_quantity=int(service.max_quantity or 0),
        amount_millimes=int(price.amount_millimes),
        currency=str(price.currency or "MAD"),
        pricing_mode=str(price.pricing_mode),
        location_path=path,
        platform_label=platform,
        section_label=section,
        subsection_label=subsection,
        content_fingerprint=content_fingerprint,
        published_at=published_at,
        execution=PublishedExecutionIdentity(
            provider_slug=str(source.provider_slug).strip().lower(),
            provider_account_key=str(source.provider_account_key).strip().lower(),
            external_service_id=str(source.external_service_id).strip(),
        ),
        fulfillment_mode=str(service.fulfillment_mode).strip().lower(),
        target_policy=_target_policy_from_live(service),
        parent_entry_id=parent_entry_id,
        sort_order=int(sort_order or 0),
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
    """Build a read-only customer Catalog projection from live Catalog + publish gate."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.repo = CatalogRepository(connection)
        self.publications = CatalogPublicationService(connection)

    def build(self) -> PublishedStorefrontCatalog:
        """Materialize eligible customer services from live Catalog data."""
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
            if live.status != "active":
                excluded += 1
                continue

            source = self.repo.get_active_execution_source(latest.service_id)
            price = self.repo.get_active_price(latest.service_id)
            entry = self.repo.get_entry_for_service(latest.service_id)
            parent_entry_id = entry.parent_entry_id if entry else None
            sort_order = int(entry.sort_order or 0) if entry else 0
            if entry:
                live.entry_id = entry.id
                live.parent_entry_id = entry.parent_entry_id
                live.location_path = self.repo.breadcrumb_names(entry.parent_entry_id)
                live.sort_order = sort_order

            readiness = evaluate_service_readiness(
                self.repo, live, source=source, price=price
            )
            if not readiness.ready:
                excluded += 1
                continue

            reason = _validate_live_customer_fields(live, source, price)
            if reason is not None or source is None or price is None:
                malformed += 1
                logger.warning(
                    "storefront_projection skip malformed live service_id=%s reason=%s",
                    live.id,
                    reason or "missing_source_or_price",
                )
                continue

            path = list(live.location_path or [])
            services.append(
                _service_from_live(
                    live,
                    source,
                    price,
                    location_path=path,
                    parent_entry_id=parent_entry_id,
                    sort_order=sort_order,
                    published_at=latest.published_at,
                    content_fingerprint=latest.content_fingerprint,
                )
            )

        services.sort(
            key=lambda s: (s.sort_order, s.name_ar.casefold(), s.service_id)
        )
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
        parent_entry_id: str | None = ...,  # type: ignore[assignment]
    ) -> list[PublishedStorefrontService]:
        plat = None if platform_label is None else str(platform_label).strip()
        sect = None if section_label is None else str(section_label).strip()
        sub = None if subsection_label is None else str(subsection_label).strip()

        out: list[PublishedStorefrontService] = []
        for svc in self.build().services:
            if parent_entry_id is not ...:
                want = parent_entry_id
                if want is None:
                    if svc.parent_entry_id is not None:
                        continue
                elif svc.parent_entry_id != want:
                    continue
            if plat is not None and (svc.platform_label or "") != plat:
                continue
            if sect is not None and (svc.section_label or "") != sect:
                continue
            if sub is not None:
                if (svc.subsection_label or "") != sub:
                    continue
            elif sect is not None and parent_entry_id is ...:
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
