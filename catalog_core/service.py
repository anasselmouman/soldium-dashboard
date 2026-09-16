# -*- coding: utf-8 -*-
"""Catalog core domain operations."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Literal

from catalog_core.commercial import (
    DEFAULT_MAX_QUANTITY,
    DEFAULT_MIN_QUANTITY,
    commercial_meta,
    normalize_fulfillment_mode,
    normalize_optional_key,
    normalize_ordering_mode,
    normalize_quantity,
    normalize_service_type,
    validate_commercial_profile,
)
from catalog_core.errors import (
    CatalogConflictError,
    CatalogNotFoundError,
    CatalogValidationError,
)
from catalog_core.models import (
    CatalogEntry,
    CatalogNode,
    CatalogPrice,
    CatalogService,
    ChangeExecutionSourceResult,
    ChangePriceResult,
    ExecutionSource,
)
from catalog_core.pricing import (
    DEFAULT_CURRENCY,
    dh_to_millimes,
    normalize_currency,
    normalize_pricing_mode,
    pricing_meta,
)
from catalog_core.readiness import ReadinessResult, evaluate_service_readiness
from catalog_core.repository import CatalogRepository

ReorderPosition = Literal["up", "down", "before", "after"]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class CatalogCoreService:
    """Transactional Catalog operations against a live sqlite3 connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys = ON")
        self.repo = CatalogRepository(connection)
        self.connection = connection

    # ── services ──

    def create_service(
        self,
        *,
        name_ar: str,
        note_ar: str = "",
        status: str = "draft",
        service_type: str | None = None,
        ordering_mode: str | None = None,
        min_quantity: int | None = None,
        max_quantity: int | None = None,
        fulfillment_mode: str | None = None,
        target_platform_key: str | None = None,
        target_section_key: str | None = None,
        target_subsection_key: str | None = None,
        target_link_prompt_key: str | None = None,
        target_link_type: str | None = None,
        parent_entry_id: str | None = None,
    ) -> CatalogService:
        name = (name_ar or "").strip()
        if status not in {"draft", "active", "archived"}:
            raise CatalogValidationError("حالة الخدمة غير صالحة")
        stype = normalize_service_type(service_type)
        omode = normalize_ordering_mode(ordering_mode)
        fmode = normalize_fulfillment_mode(fulfillment_mode)
        min_q = (
            DEFAULT_MIN_QUANTITY
            if min_quantity is None
            else normalize_quantity(min_quantity, field_label="الحد الأدنى")
        )
        max_q = (
            DEFAULT_MAX_QUANTITY
            if max_quantity is None
            else normalize_quantity(max_quantity, field_label="الحد الأقصى")
        )
        validate_commercial_profile(
            name_ar=name,
            service_type=stype,
            ordering_mode=omode,
            min_quantity=min_q,
            max_quantity=max_q,
            fulfillment_mode=fmode,
        )
        self._assert_parent_can_host_children(parent_entry_id)

        service_id = _new_id("svc")
        entry_id = _new_id("ent")
        service = CatalogService(
            id=service_id,
            name_ar=name,
            note_ar=(note_ar or "").strip(),
            status=status,  # type: ignore[arg-type]
            service_type=stype,
            ordering_mode=omode,
            min_quantity=min_q,
            max_quantity=max_q,
            fulfillment_mode=fmode,
            target_platform_key=normalize_optional_key(target_platform_key),
            target_section_key=normalize_optional_key(target_section_key),
            target_subsection_key=normalize_optional_key(target_subsection_key),
            target_link_prompt_key=normalize_optional_key(target_link_prompt_key),
            target_link_type=normalize_optional_key(target_link_type),
        )
        self.repo.insert_service(service)
        entry = CatalogEntry(
            id=entry_id,
            parent_entry_id=parent_entry_id,
            entry_type="service",
            node_id=None,
            service_id=service_id,
            sort_order=self.repo.next_sort_order(parent_entry_id),
        )
        self.repo.insert_entry(entry)
        return self.get_service(service_id)

    def get_service(self, service_id: str) -> CatalogService:
        svc = self.repo.get_service(service_id)
        if not svc:
            raise CatalogNotFoundError("الخدمة غير موجودة")
        entry = self.repo.get_entry_for_service(service_id)
        if entry:
            svc.entry_id = entry.id
            svc.parent_entry_id = entry.parent_entry_id
            svc.sort_order = entry.sort_order
            svc.location_path = self.repo.breadcrumb_names(entry.parent_entry_id)
        svc.current_source = self.repo.get_active_execution_source(service_id)
        svc.current_price = self.repo.get_active_price(service_id)
        readiness = evaluate_service_readiness(
            self.repo,
            svc,
            source=svc.current_source,
            price=svc.current_price,
        )
        svc.readiness = readiness.summary_dict()
        return svc

    def update_service(
        self,
        service_id: str,
        *,
        name_ar: str | None = None,
        note_ar: str | None = None,
        status: str | None = None,
        service_type: str | None = None,
        ordering_mode: str | None = None,
        min_quantity: int | None = None,
        max_quantity: int | None = None,
        fulfillment_mode: str | None = None,
        target_platform_key: str | None = ...,  # type: ignore[assignment]
        target_section_key: str | None = ...,  # type: ignore[assignment]
        target_subsection_key: str | None = ...,  # type: ignore[assignment]
        target_link_prompt_key: str | None = ...,  # type: ignore[assignment]
        target_link_type: str | None = ...,  # type: ignore[assignment]
    ) -> CatalogService:
        existing = self.repo.get_service(service_id)
        if not existing:
            raise CatalogNotFoundError("الخدمة غير موجودة")
        if status is not None and status not in {"draft", "active", "archived"}:
            raise CatalogValidationError("حالة الخدمة غير صالحة")

        new_name = existing.name_ar if name_ar is None else name_ar.strip()
        new_type = (
            existing.service_type
            if service_type is None
            else normalize_service_type(service_type)
        )
        new_mode = (
            existing.ordering_mode
            if ordering_mode is None
            else normalize_ordering_mode(ordering_mode)
        )
        new_fmode = (
            existing.fulfillment_mode
            if fulfillment_mode is None
            else normalize_fulfillment_mode(fulfillment_mode)
        )
        new_min = (
            existing.min_quantity
            if min_quantity is None
            else normalize_quantity(min_quantity, field_label="الحد الأدنى")
        )
        new_max = (
            existing.max_quantity
            if max_quantity is None
            else normalize_quantity(max_quantity, field_label="الحد الأقصى")
        )
        validate_commercial_profile(
            name_ar=new_name,
            service_type=new_type,
            ordering_mode=new_mode,
            min_quantity=new_min,
            max_quantity=new_max,
            fulfillment_mode=new_fmode,
        )

        update_kwargs: dict[str, Any] = {
            "name_ar": new_name if name_ar is not None else None,
            "note_ar": note_ar.strip() if note_ar is not None else None,
            "status": status,
            "service_type": new_type if service_type is not None else None,
            "ordering_mode": new_mode if ordering_mode is not None else None,
            "min_quantity": new_min if (min_quantity is not None or max_quantity is not None) else None,
            "max_quantity": new_max if (min_quantity is not None or max_quantity is not None) else None,
            "fulfillment_mode": new_fmode if fulfillment_mode is not None else None,
        }
        if target_platform_key is not ...:
            update_kwargs["target_platform_key"] = normalize_optional_key(
                target_platform_key  # type: ignore[arg-type]
            )
        if target_section_key is not ...:
            update_kwargs["target_section_key"] = normalize_optional_key(
                target_section_key  # type: ignore[arg-type]
            )
        if target_subsection_key is not ...:
            update_kwargs["target_subsection_key"] = normalize_optional_key(
                target_subsection_key  # type: ignore[arg-type]
            )
        if target_link_prompt_key is not ...:
            update_kwargs["target_link_prompt_key"] = normalize_optional_key(
                target_link_prompt_key  # type: ignore[arg-type]
            )
        if target_link_type is not ...:
            update_kwargs["target_link_type"] = normalize_optional_key(
                target_link_type  # type: ignore[arg-type]
            )
        self.repo.update_service_fields(service_id, **update_kwargs)
        return self.get_service(service_id)

    def archive_service(self, service_id: str) -> CatalogService:
        return self.update_service(service_id, status="archived")

    def restore_service(self, service_id: str, *, status: str = "draft") -> CatalogService:
        if status not in {"draft", "active"}:
            raise CatalogValidationError("يمكن الاستعادة كمسودة أو نشطة فقط")
        return self.update_service(service_id, status=status)

    def move_service(
        self,
        service_id: str,
        *,
        new_parent_entry_id: str | None,
        before_entry_id: str | None = None,
        after_entry_id: str | None = None,
    ) -> CatalogService:
        entry = self.repo.get_entry_for_service(service_id)
        if not entry:
            raise CatalogNotFoundError("الخدمة غير موجودة في الهيكل")
        self._move_entry(
            entry.id,
            new_parent_entry_id=new_parent_entry_id,
            before_entry_id=before_entry_id,
            after_entry_id=after_entry_id,
        )
        return self.get_service(service_id)

    def list_services(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        source: str | None = None,
        service_type: str | None = None,
        ordering_mode: str | None = None,
        price: str | None = None,
        pricing_mode: str | None = None,
        readiness: str | None = None,
        under_entry_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        include_readiness: bool = True,
    ) -> tuple[list[CatalogService], int]:
        if source is not None and source not in {"none", "assigned", "active"}:
            raise CatalogValidationError("فلتر مصدر التنفيذ غير صالح")
        if price is not None and price not in {"none", "assigned", "active"}:
            raise CatalogValidationError("فلتر التسعير غير صالح")
        if readiness is not None and readiness not in {"ready", "needs_review"}:
            raise CatalogValidationError("فلتر الجاهزية غير صالح")
        if service_type is not None:
            service_type = normalize_service_type(service_type)
        if ordering_mode is not None:
            ordering_mode = normalize_ordering_mode(ordering_mode)
        if pricing_mode is not None:
            pricing_mode = normalize_pricing_mode(pricing_mode)

        under = (under_entry_id or "").strip() or None
        if under is not None:
            entry = self.repo.get_entry(under)
            if not entry or entry.entry_type != "node":
                raise CatalogValidationError("مكان التصفية غير صالح — اختر قسماً من الهيكل")

        # When filtering by readiness, evaluate over the filtered commercial/source set
        # then paginate in memory (derived readiness — no stored flag).
        if readiness is not None:
            items, _ = self.repo.list_services(
                status=status,
                search=search,
                source=source,
                service_type=service_type,
                ordering_mode=ordering_mode,
                price=price,
                pricing_mode=pricing_mode,
                under_entry_id=under,
                limit=10_000,
                offset=0,
            )
            ids = [s.id for s in items]
            sources = self.repo.get_active_sources_for_services(ids)
            prices = self.repo.get_active_prices_for_services(ids)
            matched: list[CatalogService] = []
            for svc in items:
                if svc.parent_entry_id is not None or svc.entry_id:
                    svc.location_path = self.repo.breadcrumb_names(svc.parent_entry_id)
                svc.current_source = sources.get(svc.id)
                svc.current_price = prices.get(svc.id)
                result = evaluate_service_readiness(
                    self.repo,
                    svc,
                    source=svc.current_source,
                    price=svc.current_price,
                )
                if include_readiness:
                    svc.readiness = result.summary_dict()
                if result.state == readiness:
                    matched.append(svc)
            total = len(matched)
            page_items = matched[offset : offset + limit]
            return page_items, total

        items, total = self.repo.list_services(
            status=status,
            search=search,
            source=source,
            service_type=service_type,
            ordering_mode=ordering_mode,
            price=price,
            pricing_mode=pricing_mode,
            under_entry_id=under,
            limit=limit,
            offset=offset,
        )
        ids = [s.id for s in items]
        sources = self.repo.get_active_sources_for_services(ids)
        prices = self.repo.get_active_prices_for_services(ids)
        for svc in items:
            if svc.parent_entry_id is not None or svc.entry_id:
                parent = svc.parent_entry_id
                svc.location_path = self.repo.breadcrumb_names(parent)
            svc.current_source = sources.get(svc.id)
            svc.current_price = prices.get(svc.id)
            if include_readiness:
                result = evaluate_service_readiness(
                    self.repo,
                    svc,
                    source=svc.current_source,
                    price=svc.current_price,
                )
                svc.readiness = result.summary_dict()
        return items, total

    def get_service_readiness(self, service_id: str) -> ReadinessResult:
        svc = self.repo.get_service(service_id)
        if not svc:
            raise CatalogNotFoundError("الخدمة غير موجودة")
        entry = self.repo.get_entry_for_service(service_id)
        if entry:
            svc.entry_id = entry.id
            svc.parent_entry_id = entry.parent_entry_id
        source = self.repo.get_active_execution_source(service_id)
        price = self.repo.get_active_price(service_id)
        return evaluate_service_readiness(
            self.repo, svc, source=source, price=price
        )

    def review_summary(self) -> dict[str, Any]:
        """Operational counts — read-only derived readiness."""
        items, total = self.repo.list_services(limit=10_000, offset=0)
        ids = [s.id for s in items]
        sources = self.repo.get_active_sources_for_services(ids)
        prices = self.repo.get_active_prices_for_services(ids)
        ready_n = 0
        needs_n = 0
        no_source = 0
        no_price = 0
        for svc in items:
            src = sources.get(svc.id)
            prc = prices.get(svc.id)
            if src is None:
                no_source += 1
            if prc is None:
                no_price += 1
            result = evaluate_service_readiness(
                self.repo, svc, source=src, price=prc
            )
            if result.ready:
                ready_n += 1
            else:
                needs_n += 1
        return {
            "total_services": total,
            "ready": ready_n,
            "needs_review": needs_n,
            "without_source": no_source,
            "without_price": no_price,
        }

    @staticmethod
    def commercial_options() -> dict[str, Any]:
        data = commercial_meta()
        data.update(pricing_meta())
        return data

    # ── pricing ──

    def get_price(self, service_id: str) -> CatalogPrice | None:
        if not self.repo.get_service(service_id):
            raise CatalogNotFoundError("الخدمة غير موجودة")
        return self.repo.get_active_price(service_id)

    def list_price_history(self, service_id: str) -> list[CatalogPrice]:
        if not self.repo.get_service(service_id):
            raise CatalogNotFoundError("الخدمة غير موجودة")
        return self.repo.list_prices(service_id)

    def change_price(
        self,
        service_id: str,
        *,
        amount_dh: object,
        pricing_mode: str,
        currency: str | None = None,
    ) -> ChangePriceResult:
        if not self.repo.get_service(service_id):
            raise CatalogNotFoundError("الخدمة غير موجودة")

        mode = normalize_pricing_mode(pricing_mode)
        cur = normalize_currency(currency or DEFAULT_CURRENCY)
        millimes = dh_to_millimes(amount_dh)

        current = self.repo.get_active_price(service_id)
        if (
            current
            and current.amount_millimes == millimes
            and current.pricing_mode == mode
            and current.currency == cur
        ):
            return ChangePriceResult(
                unchanged=True,
                message="السعر الحالي مطابق بالفعل",
                current=current,
                previous=None,
            )

        previous = current
        if current:
            self.repo.end_price(current.id)

        new_price = CatalogPrice(
            id=_new_id("prc"),
            service_id=service_id,
            amount_millimes=millimes,
            currency=cur,
            pricing_mode=mode,
            status="active",
        )
        try:
            self.repo.insert_price(new_price)
        except sqlite3.IntegrityError as exc:
            raise CatalogConflictError(
                "تعذر حفظ السعر بسبب تعارض في البيانات"
            ) from exc

        active = self.repo.get_active_price(service_id)
        return ChangePriceResult(
            unchanged=False,
            message="تم حفظ السعر",
            current=active,
            previous=previous,
        )

    # ── execution sources ──

    def get_execution_source(self, service_id: str) -> ExecutionSource | None:
        if not self.repo.get_service(service_id):
            raise CatalogNotFoundError("الخدمة غير موجودة")
        return self.repo.get_active_execution_source(service_id)

    def list_execution_source_history(self, service_id: str) -> list[ExecutionSource]:
        if not self.repo.get_service(service_id):
            raise CatalogNotFoundError("الخدمة غير موجودة")
        return self.repo.list_execution_sources(service_id)

    def change_execution_source(
        self,
        service_id: str,
        *,
        provider_slug: str,
        provider_account_key: str,
        external_service_id: str,
        changed_by: str | None = None,
    ) -> ChangeExecutionSourceResult:
        if not self.repo.get_service(service_id):
            raise CatalogNotFoundError("الخدمة غير موجودة")

        slug = (provider_slug or "").strip()
        account_key = (provider_account_key or "").strip()
        # Opaque external id — never coerce to int; only strip whitespace.
        external_id = str(external_service_id if external_service_id is not None else "").strip()

        if not slug:
            raise CatalogValidationError("المورد مطلوب")
        if not account_key:
            raise CatalogValidationError("الحساب مطلوب")
        if not external_id:
            raise CatalogValidationError("معرّف المزود مطلوب")

        provider = self.repo.find_provider(slug)
        if not provider:
            raise CatalogValidationError("المورد غير موجود")

        account = self.repo.find_provider_account(slug, account_key)
        if not account:
            # Explicit mismatch / missing account (account must belong to provider)
            other = self.connection.execute(
                "SELECT provider_slug FROM provider_accounts WHERE account_key = ? LIMIT 1",
                (account_key,),
            ).fetchone()
            if other and str(other["provider_slug"]) != slug:
                raise CatalogValidationError(
                    "الحساب المحدد لا ينتمي إلى المورد المختار"
                )
            raise CatalogValidationError("حساب المورد غير موجود")

        resolved_slug, provider_name = provider
        resolved_account_key = account[1]
        account_display = account[2] or resolved_account_key

        current = self.repo.get_active_execution_source(service_id)
        if (
            current
            and current.provider_slug == resolved_slug
            and current.provider_account_key == resolved_account_key
            and current.external_service_id == external_id
        ):
            return ChangeExecutionSourceResult(
                unchanged=True,
                message="المصدر المحدد مستخدم بالفعل",
                current=current,
                previous=None,
            )

        previous = current
        if current:
            self.repo.end_execution_source(current.id)

        new_source = ExecutionSource(
            id=_new_id("src"),
            service_id=service_id,
            provider_slug=resolved_slug,
            provider_account_key=resolved_account_key,
            external_service_id=external_id,
            status="active",
            provider_name=provider_name,
            account_display_name=account_display,
        )
        try:
            self.repo.insert_execution_source(new_source)
        except sqlite3.IntegrityError as exc:
            # Unique active source or CHECK failure — surface as conflict
            raise CatalogConflictError(
                "تعذر تعيين مصدر التنفيذ بسبب تعارض في البيانات"
            ) from exc

        self.repo.insert_execution_source_event(
            event_id=_new_id("ese"),
            service_id=service_id,
            previous=previous,
            new_provider_slug=resolved_slug,
            new_provider_account_key=resolved_account_key,
            new_external_service_id=external_id,
            actor=(str(changed_by).strip() if changed_by else None) or None,
        )

        active = self.repo.get_active_execution_source(service_id)
        return ChangeExecutionSourceResult(
            unchanged=False,
            message="تم تغيير مصدر التنفيذ",
            current=active,
            previous=previous,
        )

    def list_execution_source_events(
        self, service_id: str, *, limit: int = 50
    ) -> list[dict]:
        if not self.repo.get_service(service_id):
            raise CatalogNotFoundError("الخدمة غير موجودة")
        return self.repo.list_execution_source_events(service_id, limit=limit)
    # ── nodes ──

    def create_node(
        self,
        *,
        name_ar: str,
        note_ar: str = "",
        parent_entry_id: str | None = None,
    ) -> CatalogNode:
        name = (name_ar or "").strip()
        if not name:
            raise CatalogValidationError("اسم القسم مطلوب")
        self._assert_parent_can_host_children(parent_entry_id)

        node_id = _new_id("node")
        entry_id = _new_id("ent")
        node = CatalogNode(
            id=node_id,
            name_ar=name,
            note_ar=(note_ar or "").strip(),
            status="active",
        )
        self.repo.insert_node(node)
        entry = CatalogEntry(
            id=entry_id,
            parent_entry_id=parent_entry_id,
            entry_type="node",
            node_id=node_id,
            service_id=None,
            sort_order=self.repo.next_sort_order(parent_entry_id),
        )
        self.repo.insert_entry(entry)
        return self.get_node(node_id)

    def get_node(self, node_id: str) -> CatalogNode:
        node = self.repo.get_node(node_id)
        if not node:
            raise CatalogNotFoundError("القسم غير موجود")
        entry = self.repo.get_entry_for_node(node_id)
        if entry:
            node.entry_id = entry.id
            node.parent_entry_id = entry.parent_entry_id
            node.sort_order = entry.sort_order
            node.location_path = self.repo.breadcrumb_names(entry.parent_entry_id)
        return node

    def rename_node(
        self,
        node_id: str,
        *,
        name_ar: str | None = None,
        note_ar: str | None = None,
    ) -> CatalogNode:
        if not self.repo.get_node(node_id):
            raise CatalogNotFoundError("القسم غير موجود")
        if name_ar is not None and not name_ar.strip():
            raise CatalogValidationError("اسم القسم مطلوب")
        self.repo.update_node_fields(
            node_id,
            name_ar=name_ar.strip() if name_ar is not None else None,
            note_ar=note_ar.strip() if note_ar is not None else None,
        )
        return self.get_node(node_id)

    def archive_node(self, node_id: str) -> CatalogNode:
        entry = self.repo.get_entry_for_node(node_id)
        if not entry:
            raise CatalogNotFoundError("القسم غير موجود في الهيكل")
        if self.repo.count_children(entry.id) > 0:
            raise CatalogConflictError(
                "لا يمكن أرشفة قسم يحتوي على عناصر. انقل العناصر أولاً."
            )
        self.repo.update_node_fields(node_id, status="archived")
        return self.get_node(node_id)

    def move_node(
        self,
        node_id: str,
        *,
        new_parent_entry_id: str | None,
        before_entry_id: str | None = None,
        after_entry_id: str | None = None,
    ) -> CatalogNode:
        entry = self.repo.get_entry_for_node(node_id)
        if not entry:
            raise CatalogNotFoundError("القسم غير موجود في الهيكل")
        self._move_entry(
            entry.id,
            new_parent_entry_id=new_parent_entry_id,
            before_entry_id=before_entry_id,
            after_entry_id=after_entry_id,
        )
        return self.get_node(node_id)

    # ── tree / reorder ──

    def get_tree(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        entries = self.repo.list_all_entries_hydrated()
        by_parent: dict[str | None, list[CatalogEntry]] = {}
        for entry in entries:
            if not include_archived and entry.status == "archived":
                continue
            by_parent.setdefault(entry.parent_entry_id, []).append(entry)

        def attach(parent_id: str | None) -> list[dict[str, Any]]:
            children = by_parent.get(parent_id, [])
            out: list[dict[str, Any]] = []
            for child in children:
                item = child.to_dict(include_children=False)
                if child.entry_type == "node":
                    item["children"] = attach(child.id)
                else:
                    item["children"] = []
                out.append(item)
            return out

        return attach(None)

    def list_children(
        self,
        parent_entry_id: str | None = None,
        *,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        if parent_entry_id is not None:
            parent = self.repo.get_entry(parent_entry_id)
            if not parent:
                raise CatalogNotFoundError("المكان الأب غير موجود")
            if parent.entry_type != "node":
                raise CatalogValidationError("لا يمكن عرض عناصر تحت خدمة")
        children = self.repo.list_children(parent_entry_id)
        out: list[dict[str, Any]] = []
        for child in children:
            if not include_archived and child.status == "archived":
                continue
            out.append(child.to_dict(include_children=False))
        return out

    def reorder_entry(
        self,
        entry_id: str,
        *,
        position: ReorderPosition,
        relative_entry_id: str | None = None,
    ) -> CatalogEntry:
        entry = self.repo.get_entry(entry_id)
        if not entry:
            raise CatalogNotFoundError("العنصر غير موجود في الهيكل")
        siblings = [
            e
            for e in self.repo.list_children(entry.parent_entry_id)
            if e.status != "archived"
        ]
        ids = [e.id for e in siblings]
        if entry_id not in ids:
            raise CatalogConflictError("العنصر غير موجود ضمن إخوته")
        idx = ids.index(entry_id)

        if position == "up":
            if idx == 0:
                return entry
            ids[idx], ids[idx - 1] = ids[idx - 1], ids[idx]
        elif position == "down":
            if idx >= len(ids) - 1:
                return entry
            ids[idx], ids[idx + 1] = ids[idx + 1], ids[idx]
        elif position in {"before", "after"}:
            if not relative_entry_id:
                raise CatalogValidationError("يجب تحديد العنصر المرجعي")
            if relative_entry_id not in ids:
                raise CatalogValidationError("العنصر المرجعي غير موجود في نفس المستوى")
            ids.remove(entry_id)
            rel_idx = ids.index(relative_entry_id)
            insert_at = rel_idx if position == "before" else rel_idx + 1
            ids.insert(insert_at, entry_id)
        else:
            raise CatalogValidationError("عملية الترتيب غير معروفة")

        for order, eid in enumerate(ids):
            self.repo.update_sort_order(eid, order)
        updated = self.repo.get_entry(entry_id)
        assert updated is not None
        return updated

    # ── internals ──

    def _assert_parent_can_host_children(self, parent_entry_id: str | None) -> None:
        if parent_entry_id is None:
            return
        parent = self.repo.get_entry(parent_entry_id)
        if not parent:
            raise CatalogNotFoundError("المكان الأب غير موجود")
        if parent.entry_type != "node":
            raise CatalogValidationError("لا يمكن وضع عناصر تحت خدمة")
        if parent.node_id:
            node = self.repo.get_node(parent.node_id)
            if node and node.status == "archived":
                raise CatalogConflictError("لا يمكن الإضافة تحت قسم مؤرشف")

    def _move_entry(
        self,
        entry_id: str,
        *,
        new_parent_entry_id: str | None,
        before_entry_id: str | None = None,
        after_entry_id: str | None = None,
    ) -> None:
        entry = self.repo.get_entry(entry_id)
        if not entry:
            raise CatalogNotFoundError("العنصر غير موجود في الهيكل")

        if new_parent_entry_id == entry_id:
            raise CatalogConflictError("لا يمكن نقل عنصر إلى نفسه")

        self._assert_parent_can_host_children(new_parent_entry_id)

        if entry.entry_type == "node":
            descendants = self.repo.descendant_entry_ids(entry_id)
            if new_parent_entry_id and (
                new_parent_entry_id == entry_id or new_parent_entry_id in descendants
            ):
                raise CatalogConflictError(
                    "لا يمكن نقل قسم إلى داخل أحد أقسامه الفرعية"
                )

        # Determine sort order among new siblings
        siblings = [
            e
            for e in self.repo.list_children(new_parent_entry_id)
            if e.id != entry_id
        ]
        sibling_ids = [e.id for e in siblings]

        if before_entry_id:
            if before_entry_id not in sibling_ids:
                raise CatalogValidationError("موضع الإدراج غير صالح")
            insert_at = sibling_ids.index(before_entry_id)
        elif after_entry_id:
            if after_entry_id not in sibling_ids:
                raise CatalogValidationError("موضع الإدراج غير صالح")
            insert_at = sibling_ids.index(after_entry_id) + 1
        else:
            insert_at = len(sibling_ids)

        sibling_ids.insert(insert_at, entry_id)
        self.repo.set_entry_parent_and_order(
            entry_id, new_parent_entry_id, insert_at
        )
        for order, eid in enumerate(sibling_ids):
            self.repo.update_sort_order(eid, order)
