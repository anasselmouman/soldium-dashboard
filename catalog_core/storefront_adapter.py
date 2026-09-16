# -*- coding: utf-8 -*-
"""Phase 9B.2 — StorefrontAdapter (Catalog application layer).

Stable Telegram-friendly contract over PublishedStorefrontProjection.

Never reads ``smm_services``. Never creates orders, debits balance, calls
providers, or mutates Catalog/publication state. Execution identity always
comes from the publication snapshot exposed by the projection.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from catalog_core.commercial import (
    fulfillment_mode_label_ar,
    ordering_mode_label_ar,
    service_type_label_ar,
)
from catalog_core.errors import CatalogError, CatalogNotFoundError, CatalogValidationError
from catalog_core.pricing import (
    format_dh_amount,
    pricing_mode_label_ar,
    quote_total_millimes,
)
from catalog_core.storefront_projection import (
    PublishedStorefrontNode,
    PublishedStorefrontProjection,
    PublishedStorefrontService,
    PublishedTargetPolicy,
)
from catalog_core.target_validation import validate_order_target

# Browse/order modes the Adapter can prepare for Phase 8G quantity orders.
_ORDERABLE_PRICING_MODES = frozenset({"per_1000", "per_unit"})
_ORDERABLE_ORDERING_MODES = frozenset({"quantity_based"})


class StorefrontAdapterError(CatalogError):
    """Structured storefront rejection (fail closed)."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class StorefrontExecutionIdentity:
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
class StorefrontTargetPolicy:
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
class StorefrontPrice:
    amount_millimes: int
    currency: str
    pricing_mode: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "amount_millimes": self.amount_millimes,
            "amount_dh": format_dh_amount(self.amount_millimes),
            "currency": self.currency,
            "pricing_mode": self.pricing_mode,
            "pricing_mode_label_ar": pricing_mode_label_ar(self.pricing_mode),
        }


@dataclass(frozen=True)
class StorefrontPlatform:
    label: str
    path: tuple[str, ...]
    service_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "path": list(self.path),
            "service_count": self.service_count,
        }


@dataclass(frozen=True)
class StorefrontSection:
    label: str
    platform_label: str
    path: tuple[str, ...]
    service_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "platform_label": self.platform_label,
            "path": list(self.path),
            "service_count": self.service_count,
        }


@dataclass(frozen=True)
class StorefrontSubsection:
    label: str
    platform_label: str
    section_label: str
    path: tuple[str, ...]
    service_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "platform_label": self.platform_label,
            "section_label": self.section_label,
            "path": list(self.path),
            "service_count": self.service_count,
        }


@dataclass(frozen=True)
class StorefrontService:
    service_id: str
    name_ar: str
    note_ar: str
    service_type: str
    ordering_mode: str
    min_quantity: int
    max_quantity: int
    price: StorefrontPrice
    location_path: tuple[str, ...]
    platform_label: str | None
    section_label: str | None
    subsection_label: str | None
    content_fingerprint: str | None
    published_at: str | None
    execution: StorefrontExecutionIdentity
    fulfillment_mode: str
    target_policy: StorefrontTargetPolicy
    orderable: bool

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
            "price": self.price.to_dict(),
            "location_path": list(self.location_path),
            "location_path_label_ar": (
                " › ".join(self.location_path) if self.location_path else "الجذر"
            ),
            "platform_label": self.platform_label,
            "section_label": self.section_label,
            "subsection_label": self.subsection_label,
            "content_fingerprint": self.content_fingerprint,
            "published_at": self.published_at,
            "execution": self.execution.to_dict(),
            "fulfillment_mode": self.fulfillment_mode,
            "fulfillment_mode_label_ar": fulfillment_mode_label_ar(self.fulfillment_mode),
            "target_policy": self.target_policy.to_dict(),
            "orderable": self.orderable,
        }


@dataclass(frozen=True)
class StorefrontQuantityResult:
    ok: bool
    service_id: str
    quantity: int | None
    min_quantity: int
    max_quantity: int
    code: str | None = None
    message_ar: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "service_id": self.service_id,
            "quantity": self.quantity,
            "min_quantity": self.min_quantity,
            "max_quantity": self.max_quantity,
            "code": self.code,
            "message_ar": self.message_ar,
        }


@dataclass(frozen=True)
class StorefrontPriceQuote:
    service_id: str
    quantity: int
    unit_amount_millimes: int
    quoted_amount_millimes: int
    currency: str
    pricing_mode: str
    content_fingerprint: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "quantity": self.quantity,
            "unit_amount_millimes": self.unit_amount_millimes,
            "unit_amount_dh": format_dh_amount(self.unit_amount_millimes),
            "quoted_amount_millimes": self.quoted_amount_millimes,
            "quoted_amount_dh": format_dh_amount(self.quoted_amount_millimes),
            "currency": self.currency,
            "pricing_mode": self.pricing_mode,
            "pricing_mode_label_ar": pricing_mode_label_ar(self.pricing_mode),
            "content_fingerprint": self.content_fingerprint,
        }


@dataclass(frozen=True)
class StorefrontTargetResult:
    ok: bool
    service_id: str
    target: str | None
    code: str | None = None
    message_ar: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "service_id": self.service_id,
            "target": self.target,
            "code": self.code,
            "message_ar": self.message_ar,
        }


@dataclass(frozen=True)
class StorefrontOrderIntent:
    """Validated order preparation for future Telegram → Phase 8G handoff.

    Does not create an order. Does not debit. Does not call Provider.
    """

    service_id: str
    service_name_ar: str
    quantity: int
    quoted_amount_millimes: int
    currency: str
    pricing_mode: str
    provider_slug: str
    provider_account_key: str
    external_service_id: str
    content_fingerprint: str | None
    published_at: str | None
    fulfillment_mode: str
    target: str
    target_validation_ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "service_name_ar": self.service_name_ar,
            "quantity": self.quantity,
            "quoted_amount_millimes": self.quoted_amount_millimes,
            "quoted_amount_dh": format_dh_amount(self.quoted_amount_millimes),
            "currency": self.currency,
            "pricing_mode": self.pricing_mode,
            "provider_slug": self.provider_slug,
            "provider_account_key": self.provider_account_key,
            "external_service_id": self.external_service_id,
            "content_fingerprint": self.content_fingerprint,
            "published_at": self.published_at,
            "fulfillment_mode": self.fulfillment_mode,
            "target": self.target,
            "target_validation_result": {
                "ok": self.target_validation_ok,
            },
            "execution_identity": {
                "provider_slug": self.provider_slug,
                "provider_account_key": self.provider_account_key,
                "external_service_id": self.external_service_id,
            },
        }


def _is_orderable(svc: PublishedStorefrontService) -> bool:
    return (
        svc.pricing_mode in _ORDERABLE_PRICING_MODES
        and svc.ordering_mode in _ORDERABLE_ORDERING_MODES
        and svc.fulfillment_mode in {"auto", "admin"}
        and bool(svc.target_policy.platform_key)
        and bool(svc.target_policy.section_key)
    )


def _map_target_policy(policy: PublishedTargetPolicy) -> StorefrontTargetPolicy:
    return StorefrontTargetPolicy(
        required=policy.required,
        platform_key=policy.platform_key,
        section_key=policy.section_key,
        subsection_key=policy.subsection_key,
        link_prompt_key=policy.link_prompt_key,
        link_type=policy.link_type,
        allow_username=policy.allow_username,
        allow_free_text=policy.allow_free_text,
    )


def _map_service(svc: PublishedStorefrontService) -> StorefrontService:
    return StorefrontService(
        service_id=svc.service_id,
        name_ar=svc.name_ar,
        note_ar=svc.note_ar,
        service_type=svc.service_type,
        ordering_mode=svc.ordering_mode,
        min_quantity=svc.min_quantity,
        max_quantity=svc.max_quantity,
        price=StorefrontPrice(
            amount_millimes=svc.amount_millimes,
            currency=svc.currency,
            pricing_mode=svc.pricing_mode,
        ),
        location_path=svc.location_path,
        platform_label=svc.platform_label,
        section_label=svc.section_label,
        subsection_label=svc.subsection_label,
        content_fingerprint=svc.content_fingerprint,
        published_at=svc.published_at,
        execution=StorefrontExecutionIdentity(
            provider_slug=svc.execution.provider_slug,
            provider_account_key=svc.execution.provider_account_key,
            external_service_id=svc.execution.external_service_id,
        ),
        fulfillment_mode=svc.fulfillment_mode,
        target_policy=_map_target_policy(svc.target_policy),
        orderable=_is_orderable(svc),
    )


def _map_platform(node: PublishedStorefrontNode) -> StorefrontPlatform:
    return StorefrontPlatform(
        label=node.label,
        path=node.path,
        service_count=node.service_count,
    )


class StorefrontAdapter:
    """Application boundary for the future Telegram storefront."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        projection: PublishedStorefrontProjection | None = None,
    ) -> None:
        self.connection = connection
        self.projection = projection or PublishedStorefrontProjection(connection)

    # --- Browse -----------------------------------------------------------------

    def list_platforms(self) -> list[StorefrontPlatform]:
        return [_map_platform(n) for n in self.projection.list_platforms()]

    def list_sections(self, platform_label: str) -> list[StorefrontSection]:
        plat = str(platform_label or "").strip()
        return [
            StorefrontSection(
                label=n.label,
                platform_label=plat,
                path=n.path,
                service_count=n.service_count,
            )
            for n in self.projection.list_sections(plat)
        ]

    def list_subsections(
        self, platform_label: str, section_label: str
    ) -> list[StorefrontSubsection]:
        plat = str(platform_label or "").strip()
        sect = str(section_label or "").strip()
        return [
            StorefrontSubsection(
                label=n.label,
                platform_label=plat,
                section_label=sect,
                path=n.path,
                service_count=n.service_count,
            )
            for n in self.projection.list_subsections(plat, sect)
        ]

    def list_services(
        self,
        *,
        platform_label: str | None = None,
        section_label: str | None = None,
        subsection_label: str | None = None,
    ) -> list[StorefrontService]:
        return [
            _map_service(s)
            for s in self.projection.list_services(
                platform_label=platform_label,
                section_label=section_label,
                subsection_label=subsection_label,
            )
        ]

    def get_service(self, service_id: str) -> StorefrontService:
        try:
            return _map_service(self.projection.get_service(service_id))
        except CatalogNotFoundError as exc:
            raise StorefrontAdapterError(
                str(exc.message),
                code="service_not_found",
                details={"service_id": str(service_id or "").strip()},
            ) from exc

    # --- Ordering preparation ---------------------------------------------------

    def validate_quantity(
        self, service_id: str, quantity: object
    ) -> StorefrontQuantityResult:
        svc = self._require_published_service(service_id)
        return self._validate_quantity_for(svc, quantity)

    def validate_target(
        self, service_id: str, target: object
    ) -> StorefrontTargetResult:
        svc = self._require_published_service(service_id)
        return self._validate_target_for(svc, target)

    def quote_price(self, service_id: str, quantity: object) -> StorefrontPriceQuote:
        svc = self._require_published_service(service_id)
        check = self._validate_quantity_for(svc, quantity)
        if not check.ok:
            raise StorefrontAdapterError(
                check.message_ar or "الكمية غير صالحة",
                code=check.code or "invalid_quantity",
                details={
                    "service_id": svc.service_id,
                    "quantity": check.quantity,
                    "min_quantity": svc.min_quantity,
                    "max_quantity": svc.max_quantity,
                },
            )
        assert check.quantity is not None
        try:
            total = quote_total_millimes(
                svc.amount_millimes, svc.pricing_mode, check.quantity
            )
        except CatalogValidationError as exc:
            raise StorefrontAdapterError(
                str(exc.message),
                code="unsupported_pricing_mode"
                if svc.pricing_mode == "fixed_package"
                else "invalid_quote",
                details={
                    "service_id": svc.service_id,
                    "pricing_mode": svc.pricing_mode,
                    "quantity": check.quantity,
                },
            ) from exc
        return StorefrontPriceQuote(
            service_id=svc.service_id,
            quantity=check.quantity,
            unit_amount_millimes=svc.amount_millimes,
            quoted_amount_millimes=total,
            currency=svc.currency,
            pricing_mode=svc.pricing_mode,
            content_fingerprint=svc.content_fingerprint,
        )

    def resolve_order_intent(
        self,
        service_id: str,
        quantity: object,
        *,
        target: str | None = None,
    ) -> StorefrontOrderIntent:
        """Prepare a validated order intent. No DB writes. No Provider calls."""
        svc = self._require_published_service(service_id)
        if not _is_orderable(svc):
            code = (
                "unsupported_pricing_mode"
                if svc.pricing_mode not in _ORDERABLE_PRICING_MODES
                else "unsupported_ordering_mode"
                if svc.ordering_mode not in _ORDERABLE_ORDERING_MODES
                else "order_contract_incomplete"
            )
            raise StorefrontAdapterError(
                "هذه الخدمة غير قابلة للطلب عبر واجهة المتجر الحالية",
                code=code,
                details={
                    "service_id": svc.service_id,
                    "pricing_mode": svc.pricing_mode,
                    "ordering_mode": svc.ordering_mode,
                    "fulfillment_mode": svc.fulfillment_mode,
                },
            )
        if svc.fulfillment_mode not in {"auto", "admin"}:
            raise StorefrontAdapterError(
                "وضع التنفيذ المنشور غير متوفر",
                code="fulfillment_mode_unavailable",
                details={"service_id": svc.service_id},
            )
        if not (
            svc.execution.provider_slug
            and svc.execution.provider_account_key
            and svc.execution.external_service_id
        ):
            raise StorefrontAdapterError(
                "هوية التنفيذ المنشورة غير متوفرة",
                code="execution_identity_unavailable",
                details={"service_id": svc.service_id},
            )

        target_check = self._validate_target_for(svc, target)
        if not target_check.ok:
            raise StorefrontAdapterError(
                target_check.message_ar or "الهدف غير صالح",
                code=target_check.code or "invalid_target",
                details={
                    "service_id": svc.service_id,
                    "target": target_check.target,
                },
            )
        assert target_check.target is not None

        quote = self.quote_price(svc.service_id, quantity)
        return StorefrontOrderIntent(
            service_id=svc.service_id,
            service_name_ar=svc.name_ar,
            quantity=quote.quantity,
            quoted_amount_millimes=quote.quoted_amount_millimes,
            currency=quote.currency,
            pricing_mode=quote.pricing_mode,
            provider_slug=svc.execution.provider_slug,
            provider_account_key=svc.execution.provider_account_key,
            external_service_id=svc.execution.external_service_id,
            content_fingerprint=svc.content_fingerprint,
            published_at=svc.published_at,
            fulfillment_mode=svc.fulfillment_mode,
            target=target_check.target,
            target_validation_ok=True,
        )

    # --- Internals --------------------------------------------------------------

    def _require_published_service(
        self, service_id: str
    ) -> PublishedStorefrontService:
        try:
            return self.projection.get_service(service_id)
        except CatalogNotFoundError as exc:
            raise StorefrontAdapterError(
                "الخدمة غير متاحة في المتجر المنشور",
                code="service_unavailable",
                details={"service_id": str(service_id or "").strip()},
            ) from exc

    def _validate_target_for(
        self, svc: PublishedStorefrontService, target: object
    ) -> StorefrontTargetResult:
        policy = svc.target_policy
        if not policy.platform_key or not policy.section_key:
            return StorefrontTargetResult(
                ok=False,
                service_id=svc.service_id,
                target=None,
                code="target_policy_unavailable",
                message_ar="سياسة الهدف المنشورة غير متوفرة",
            )
        raw = None if target is None else str(target)
        ok, message = validate_order_target(
            raw,
            platform_key=policy.platform_key,
            section_key=policy.section_key,
            subsection_key=policy.subsection_key,
            link_prompt_key=policy.link_prompt_key,
            link_type=policy.link_type,
            required=policy.required,
        )
        cleaned = None if raw is None else str(raw).strip() or None
        if not ok:
            return StorefrontTargetResult(
                ok=False,
                service_id=svc.service_id,
                target=cleaned,
                code="invalid_target",
                message_ar=message or "الهدف غير صالح",
            )
        return StorefrontTargetResult(
            ok=True,
            service_id=svc.service_id,
            target=cleaned,
        )

    def _validate_quantity_for(
        self, svc: PublishedStorefrontService, quantity: object
    ) -> StorefrontQuantityResult:
        if svc.pricing_mode == "fixed_package":
            return StorefrontQuantityResult(
                ok=False,
                service_id=svc.service_id,
                quantity=None,
                min_quantity=svc.min_quantity,
                max_quantity=svc.max_quantity,
                code="unsupported_pricing_mode",
                message_ar="التسعير الثابت للباقة غير مدعوم للطلب حسب الكمية",
            )
        if svc.ordering_mode not in _ORDERABLE_ORDERING_MODES:
            return StorefrontQuantityResult(
                ok=False,
                service_id=svc.service_id,
                quantity=None,
                min_quantity=svc.min_quantity,
                max_quantity=svc.max_quantity,
                code="unsupported_ordering_mode",
                message_ar="طريقة الطلب غير مدعومة عبر واجهة المتجر الحالية",
            )
        try:
            qty = int(quantity)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return StorefrontQuantityResult(
                ok=False,
                service_id=svc.service_id,
                quantity=None,
                min_quantity=svc.min_quantity,
                max_quantity=svc.max_quantity,
                code="invalid_quantity",
                message_ar="الكمية غير صالحة",
            )
        if qty < int(svc.min_quantity):
            return StorefrontQuantityResult(
                ok=False,
                service_id=svc.service_id,
                quantity=qty,
                min_quantity=svc.min_quantity,
                max_quantity=svc.max_quantity,
                code="quantity_below_minimum",
                message_ar=f"الحد الأدنى للكمية هو {svc.min_quantity}",
            )
        if qty > int(svc.max_quantity):
            return StorefrontQuantityResult(
                ok=False,
                service_id=svc.service_id,
                quantity=qty,
                min_quantity=svc.min_quantity,
                max_quantity=svc.max_quantity,
                code="quantity_above_maximum",
                message_ar=f"الحد الأقصى للكمية هو {svc.max_quantity}",
            )
        return StorefrontQuantityResult(
            ok=True,
            service_id=svc.service_id,
            quantity=qty,
            min_quantity=svc.min_quantity,
            max_quantity=svc.max_quantity,
        )
