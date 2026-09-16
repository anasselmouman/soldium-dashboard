# -*- coding: utf-8 -*-
"""Catalog core models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from catalog_core.commercial import (
    DEFAULT_FULFILLMENT_MODE,
    DEFAULT_MAX_QUANTITY,
    DEFAULT_MIN_QUANTITY,
    DEFAULT_ORDERING_MODE,
    DEFAULT_SERVICE_TYPE,
    fulfillment_mode_label_ar,
    ordering_mode_label_ar,
    service_type_label_ar,
)

EntryType = Literal["node", "service"]
ServiceStatus = Literal["draft", "active", "archived"]
NodeStatus = Literal["active", "archived"]
ServiceTypeCode = Literal[
    "followers",
    "likes",
    "views",
    "comments",
    "shares",
    "saves",
    "members",
    "live_viewers",
    "other",
]
OrderingModeCode = Literal["quantity_based", "package_based"]


@dataclass
class CatalogService:
    id: str
    name_ar: str
    note_ar: str = ""
    status: ServiceStatus = "draft"
    service_type: str = DEFAULT_SERVICE_TYPE
    ordering_mode: str = DEFAULT_ORDERING_MODE
    min_quantity: int = DEFAULT_MIN_QUANTITY
    max_quantity: int = DEFAULT_MAX_QUANTITY
    fulfillment_mode: str = DEFAULT_FULFILLMENT_MODE
    target_platform_key: str | None = None
    target_section_key: str | None = None
    target_subsection_key: str | None = None
    target_link_prompt_key: str | None = None
    target_link_type: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    entry_id: str | None = None
    parent_entry_id: str | None = None
    sort_order: int | None = None
    location_path: list[str] = field(default_factory=list)
    current_source: "ExecutionSource | None" = None
    current_price: "CatalogPrice | None" = None
    readiness: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "name_ar": self.name_ar,
            "note_ar": self.note_ar,
            "status": self.status,
            "service_type": self.service_type,
            "service_type_label_ar": service_type_label_ar(self.service_type),
            "ordering_mode": self.ordering_mode,
            "ordering_mode_label_ar": ordering_mode_label_ar(self.ordering_mode),
            "min_quantity": self.min_quantity,
            "max_quantity": self.max_quantity,
            "fulfillment_mode": self.fulfillment_mode,
            "fulfillment_mode_label_ar": fulfillment_mode_label_ar(self.fulfillment_mode),
            "target_platform_key": self.target_platform_key,
            "target_section_key": self.target_section_key,
            "target_subsection_key": self.target_subsection_key,
            "target_link_prompt_key": self.target_link_prompt_key,
            "target_link_type": self.target_link_type,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "entry_id": self.entry_id,
            "parent_entry_id": self.parent_entry_id,
            "sort_order": self.sort_order,
            "location_path": list(self.location_path),
            "current_source": (
                self.current_source.to_dict() if self.current_source else None
            ),
            "has_execution_source": self.current_source is not None,
            "current_price": (
                self.current_price.to_dict() if self.current_price else None
            ),
            "has_price": self.current_price is not None,
        }
        if self.readiness is not None:
            data["readiness"] = self.readiness
        return data


@dataclass
class CatalogNode:
    id: str
    name_ar: str
    note_ar: str = ""
    status: NodeStatus = "active"
    created_at: str | None = None
    updated_at: str | None = None
    entry_id: str | None = None
    parent_entry_id: str | None = None
    sort_order: int | None = None
    location_path: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name_ar": self.name_ar,
            "note_ar": self.note_ar,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "entry_id": self.entry_id,
            "parent_entry_id": self.parent_entry_id,
            "sort_order": self.sort_order,
            "location_path": list(self.location_path),
        }


@dataclass
class CatalogEntry:
    id: str
    parent_entry_id: str | None
    entry_type: EntryType
    node_id: str | None
    service_id: str | None
    sort_order: int
    created_at: str | None = None
    updated_at: str | None = None
    # hydrated display
    name_ar: str = ""
    status: str = ""
    children: list["CatalogEntry"] = field(default_factory=list)

    def to_dict(self, *, include_children: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "parent_entry_id": self.parent_entry_id,
            "entry_type": self.entry_type,
            "node_id": self.node_id,
            "service_id": self.service_id,
            "sort_order": self.sort_order,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "name_ar": self.name_ar,
            "status": self.status,
        }
        if include_children:
            data["children"] = [
                c.to_dict(include_children=True) for c in self.children
            ]
        return data


ExecutionSourceStatus = Literal["active", "historical"]


@dataclass
class ExecutionSource:
    id: str
    service_id: str
    provider_slug: str
    provider_account_key: str
    external_service_id: str
    status: ExecutionSourceStatus = "active"
    assigned_at: str | None = None
    ended_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # display (not identity)
    provider_name: str = ""
    account_display_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "service_id": self.service_id,
            "provider_slug": self.provider_slug,
            "provider_account_key": self.provider_account_key,
            "external_service_id": self.external_service_id,
            "status": self.status,
            "assigned_at": self.assigned_at,
            "ended_at": self.ended_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "provider_name": self.provider_name,
            "account_display_name": self.account_display_name,
        }

    def summary_label(self) -> str:
        provider = self.provider_name or self.provider_slug
        account = self.account_display_name or self.provider_account_key
        return f"{provider} / {account} / {self.external_service_id}"


@dataclass
class ChangeExecutionSourceResult:
    unchanged: bool
    message: str
    current: ExecutionSource | None
    previous: ExecutionSource | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "unchanged": self.unchanged,
            "message": self.message,
            "current": self.current.to_dict() if self.current else None,
            "previous": self.previous.to_dict() if self.previous else None,
        }


PriceStatus = Literal["active", "historical"]


@dataclass
class CatalogPrice:
    id: str
    service_id: str
    amount_millimes: int
    currency: str = "MAD"
    pricing_mode: str = "per_1000"
    status: PriceStatus = "active"
    effective_from: str | None = None
    effective_to: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        from catalog_core.pricing import (
            format_dh_amount,
            format_price_display,
            pricing_mode_label_ar,
        )

        return {
            "id": self.id,
            "service_id": self.service_id,
            "amount_millimes": self.amount_millimes,
            "amount_dh": format_dh_amount(self.amount_millimes),
            "currency": self.currency,
            "pricing_mode": self.pricing_mode,
            "pricing_mode_label_ar": pricing_mode_label_ar(self.pricing_mode),
            "status": self.status,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "display_ar": format_price_display(
                self.amount_millimes, self.pricing_mode, currency=self.currency
            ),
        }


@dataclass
class ChangePriceResult:
    unchanged: bool
    message: str
    current: CatalogPrice | None
    previous: CatalogPrice | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "unchanged": self.unchanged,
            "message": self.message,
            "current": self.current.to_dict() if self.current else None,
            "previous": self.previous.to_dict() if self.previous else None,
        }
