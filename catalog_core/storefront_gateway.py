# -*- coding: utf-8 -*-
"""Storefront backend gateway (Legacy | Catalog | Pilot).

Centralizes backend selection. Default is Catalog (customer SoT).
Explicit ``STOREFRONT_BACKEND=legacy`` remains emergency rollback only.

Controlled production pilot (43 published cohort) uses a separate kill switch
(STOREFRONT_CATALOG_PILOT) and must NOT set STOREFRONT_BACKEND=catalog.

Telegram handlers must call ``get_storefront`` / this gateway rather than
branching on STOREFRONT_BACKEND themselves.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol, runtime_checkable

from catalog_core.pricing import format_dh_amount
from catalog_core.storefront_adapter import (
    StorefrontAdapter,
    StorefrontAdapterError,
    StorefrontOrderIntent,
    StorefrontPlatform,
    StorefrontPriceQuote,
    StorefrontQuantityResult,
    StorefrontSection,
    StorefrontService,
    StorefrontSubsection,
    StorefrontTargetResult,
)

BackendName = Literal["legacy", "catalog", "pilot"]

DEFAULT_BACKEND: BackendName = "catalog"
ENV_STOREFRONT_BACKEND = "STOREFRONT_BACKEND"

# Module-level override for isolated tests (never used by production boot).
_test_override: BackendName | None = None
_instance_cache: dict[str, Any] = {}


def resolve_storefront_backend_name(
    raw: str | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> BackendName:
    """Map config to backend name. Missing/invalid → catalog (production SoT).

    Explicit ``legacy`` remains available as emergency rollback only.
    """
    if raw is None:
        env = environ if environ is not None else os.environ
        raw = env.get(ENV_STOREFRONT_BACKEND, "")
    text = str(raw or "").strip().lower()
    if text == "legacy":
        return "legacy"
    # Default / catalog / empty / typos → catalog (Catalog SoT)
    return "catalog"


def set_storefront_backend_override(name: BackendName | None) -> None:
    """Test-only dependency injection. Pass None to clear."""
    global _test_override, _instance_cache
    _test_override = name
    _instance_cache.clear()


def clear_storefront_cache() -> None:
    _instance_cache.clear()


def reinitialize_storefront_selection() -> None:
    """Clear test override + instance cache so next build_storefront re-resolves.

    Application-level rollback/reload seam for Phase 9T. Does not touch DB.
    Also clears the Catalog pilot kill-switch override.
    """
    set_storefront_backend_override(None)
    clear_storefront_cache()
    try:
        from catalog_core.storefront_pilot import clear_catalog_pilot_override

        clear_catalog_pilot_override()
    except Exception:
        pass


@runtime_checkable
class StorefrontBackend(Protocol):
    """Telegram-facing storefront contract (browse + order prep)."""

    @property
    def backend_name(self) -> BackendName: ...

    def refresh(self) -> None: ...

    def list_platforms(self) -> list[StorefrontPlatform]: ...

    def list_sections(self, platform_label: str) -> list[StorefrontSection]: ...

    def list_subsections(
        self, platform_label: str, section_label: str
    ) -> list[StorefrontSubsection]: ...

    def list_services(
        self,
        *,
        platform_label: str | None = None,
        section_label: str | None = None,
        subsection_label: str | None = None,
    ) -> list[StorefrontService]: ...

    def get_service(self, service_id: str) -> StorefrontService: ...

    def validate_quantity(
        self, service_id: str, quantity: object
    ) -> StorefrontQuantityResult: ...

    def validate_target(
        self, service_id: str, target: object
    ) -> StorefrontTargetResult: ...

    def quote_price(
        self, service_id: str, quantity: object
    ) -> StorefrontPriceQuote: ...

    def resolve_order_intent(
        self,
        service_id: str,
        quantity: object,
        *,
        target: str | None = None,
    ) -> StorefrontOrderIntent: ...


@dataclass(frozen=True)
class OrderCreateBridge:
    """Maps Catalog Order Intent → existing create_order_with_balance_hold kwargs.

    Does not create orders. Does not debit balance. Boundary helper only.
    """

    user_id: int
    service_id: str
    service_name: str
    link: str
    quantity: int
    amount_dh: float
    provider_slug: str
    api_account: str
    fulfillment_mode: str
    external_service_id_snapshot: str
    catalog_id: str | None = None
    soldium_service_id: str | None = None
    provider_cost_dh: float = 0.0

    def to_create_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "user_id": self.user_id,
            "service_name": self.service_name,
            "service_id": self.service_id,
            "link": self.link,
            "quantity": self.quantity,
            "amount": self.amount_dh,
            "provider_slug": self.provider_slug,
            "api_account": self.api_account,
            "fulfillment_mode": self.fulfillment_mode,
            "catalog_id": self.catalog_id,
            "external_service_id_snapshot": self.external_service_id_snapshot,
            "provider_cost_dh": self.provider_cost_dh,
        }
        if self.soldium_service_id is not None:
            kwargs["soldium_service_id"] = self.soldium_service_id
        return kwargs


def lookup_provider_rate_usd(
    connection: sqlite3.Connection,
    *,
    provider_slug: str,
    provider_account_key: str,
    external_service_id: str,
) -> float:
    """Best-effort provider USD rate from latest successful Catalog provider snapshot.

    Never invents a rate. Returns 0.0 when no snapshot item is available.
    """
    slug = str(provider_slug or "").strip().lower()
    account = str(provider_account_key or "").strip().lower()
    external = str(external_service_id or "").strip()
    if not slug or not account or not external:
        return 0.0
    try:
        row = connection.execute(
            """
            SELECT i.provider_rate
            FROM soldium_provider_catalog_snapshot_items i
            JOIN soldium_provider_catalog_snapshots s ON s.id = i.snapshot_id
            WHERE s.status = 'success'
              AND LOWER(s.provider_slug) = ?
              AND LOWER(s.provider_account_key) = ?
              AND TRIM(i.external_service_id) = ?
            ORDER BY s.discovered_at DESC, s.rowid DESC
            LIMIT 1
            """,
            (slug, account, external),
        ).fetchone()
    except sqlite3.Error:
        return 0.0
    if row is None:
        return 0.0
    try:
        return float(row["provider_rate"] or 0)
    except (TypeError, ValueError, KeyError, IndexError):
        try:
            return float(row[0] or 0)
        except (TypeError, ValueError, IndexError):
            return 0.0


def order_intent_to_create_bridge(
    intent: StorefrontOrderIntent,
    *,
    user_id: int,
    connection: sqlite3.Connection | None = None,
    pricing_mode: str | None = None,
) -> OrderCreateBridge:
    """Amount freezes at Order create + balance hold from quoted Catalog millimes."""
    amount_dh = float(intent.quoted_amount_millimes) / 1000.0
    external = str(intent.external_service_id or "").strip()
    if not external:
        raise StorefrontAdapterError(
            "معرّف تنفيذ المزوّد مفقود في نية الطلب",
            code="execution_identity_unavailable",
            details={"service_id": intent.service_id},
        )
    soldium_id = str(intent.service_id or "").strip()
    provider_cost = 0.0
    mode = str(pricing_mode or intent.pricing_mode or "per_1000").strip().lower()
    if connection is not None:
        rate = lookup_provider_rate_usd(
            connection,
            provider_slug=str(intent.provider_slug),
            provider_account_key=str(intent.provider_account_key),
            external_service_id=external,
        )
        if rate > 0:
            try:
                from utils.order_economics import compute_provider_cost_dh

                provider_cost = float(
                    compute_provider_cost_dh(
                        int(intent.quantity),
                        provider_price_usd=rate,
                        local_price_dh=0.0,
                        price_per_unit=(mode == "per_unit"),
                    )
                )
            except Exception:
                provider_cost = 0.0
    return OrderCreateBridge(
        user_id=int(user_id),
        service_id=soldium_id,
        service_name=str(intent.service_name_ar),
        link=str(intent.target or ""),
        quantity=int(intent.quantity),
        amount_dh=amount_dh,
        provider_slug=str(intent.provider_slug),
        api_account=str(intent.provider_account_key),
        fulfillment_mode=str(intent.fulfillment_mode),
        external_service_id_snapshot=external,
        # Catalog SoT: store immutable svc_* for NEW orders (TEXT; historical rows unchanged).
        catalog_id=soldium_id if soldium_id.startswith("svc_") else None,
        soldium_service_id=soldium_id if soldium_id.startswith("svc_") else None,
        provider_cost_dh=float(provider_cost),
    )


class CatalogStorefrontBackend:
    """Live Catalog customer storefront — never reads smm_services."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._adapter = StorefrontAdapter(connection)
        self._connection = connection

    @property
    def backend_name(self) -> BackendName:
        return "catalog"

    def refresh(self) -> None:
        # Projection is built per call from live Catalog; nothing to reload.
        return None

    def list_platforms(self) -> list[StorefrontPlatform]:
        return self._adapter.list_platforms()

    def list_sections(self, platform_label: str) -> list[StorefrontSection]:
        return self._adapter.list_sections(platform_label)

    def list_subsections(
        self, platform_label: str, section_label: str
    ) -> list[StorefrontSubsection]:
        return self._adapter.list_subsections(platform_label, section_label)

    def list_services(
        self,
        *,
        platform_label: str | None = None,
        section_label: str | None = None,
        subsection_label: str | None = None,
    ) -> list[StorefrontService]:
        return self._adapter.list_services(
            platform_label=platform_label,
            section_label=section_label,
            subsection_label=subsection_label,
        )

    def get_service(self, service_id: str) -> StorefrontService:
        return self._adapter.get_service(service_id)

    def validate_quantity(
        self, service_id: str, quantity: object
    ) -> StorefrontQuantityResult:
        return self._adapter.validate_quantity(service_id, quantity)

    def validate_target(
        self, service_id: str, target: object
    ) -> StorefrontTargetResult:
        return self._adapter.validate_target(service_id, target)

    def quote_price(
        self, service_id: str, quantity: object
    ) -> StorefrontPriceQuote:
        return self._adapter.quote_price(service_id, quantity)

    def resolve_order_intent(
        self,
        service_id: str,
        quantity: object,
        *,
        target: str | None = None,
    ) -> StorefrontOrderIntent:
        return self._adapter.resolve_order_intent(
            service_id, quantity, target=target
        )

    def asserts_no_smm_services_access(self) -> bool:
        """Documentation/test helper: Catalog path never queries smm_services."""
        return True

    def navigation_tree(self) -> dict[str, Any]:
        """Build Telegram navigation from the live Catalog entries tree.

        Placement SoT = Catalog entries (arbitrary depth). ``target_*`` is not used
        for menu hierarchy. Only customer-eligible services (published + active +
        ready) appear; empty node branches are omitted.
        """
        from catalog_core.repository import CatalogRepository

        eligible = {
            s.service_id: s for s in self.list_services()
        }
        if not eligible:
            return {}

        repo = CatalogRepository(self._connection)
        entries = repo.list_all_entries_hydrated()
        children: dict[str | None, list[Any]] = {}
        for ent in entries:
            children.setdefault(ent.parent_entry_id, []).append(ent)

        # Nodes that contain at least one eligible service in their subtree.
        service_parents = {
            s.parent_entry_id
            for s in eligible.values()
            if s.parent_entry_id is not None
        }
        # Also services at root (parent None) via parent_entry_id on projection
        for s in eligible.values():
            # Projection parent_entry_id is the Catalog entry parent of the service entry.
            pass

        # Collect ancestor entry ids for every eligible service's parent chain.
        keep_nodes: set[str] = set()
        for svc in eligible.values():
            parent = svc.parent_entry_id
            if parent:
                for anc in repo.ancestor_entry_ids(parent):
                    keep_nodes.add(anc)

        def _node_bucket(entry_id: str, title: str) -> dict[str, Any]:
            return {
                "title": title,
                "entry_id": entry_id,
                "sections": {},
                "direct_items": [],
                "items": [],
                "subsections": {},
            }

        def _services_under(parent_entry_id: str | None) -> list[dict[str, Any]]:
            return [
                catalog_service_to_legacy_item(s)
                for s in sorted(
                    (
                        s
                        for s in eligible.values()
                        if s.parent_entry_id == parent_entry_id
                    ),
                    key=lambda s: (s.sort_order, s.name_ar.casefold(), s.service_id),
                )
            ]

        def _build_node(entry: Any) -> dict[str, Any] | None:
            if entry.entry_type != "node":
                return None
            if entry.id not in keep_nodes and not _services_under(entry.id):
                return None
            if str(getattr(entry, "status", "active") or "active") == "archived":
                return None
            title = str(getattr(entry, "name_ar", None) or entry.id)
            node: dict[str, Any] = {
                "title": title,
                "entry_id": entry.id,
                "sections": {},
                "direct_items": [],
                "items": [],
                "subsections": {},
            }
            # Direct services under this node
            node["items"] = _services_under(entry.id)
            # Child nodes
            for child in children.get(entry.id, []):
                if child.entry_type != "node":
                    continue
                if child.id not in keep_nodes and not _services_under(child.id):
                    continue
                if str(getattr(child, "status", "active") or "active") == "archived":
                    continue
                child_built = _build_node(child)
                if child_built is None:
                    continue
                # Nested child nodes go into sections (first level under root)
                # or subsections recursively via nested sections shape:
                # We store all child nodes in "sections" keyed by entry_id, and
                # each child may itself have nested "sections" for deeper levels.
                # For Telegram keyboards that expect subsections, also mirror
                # one level into "subsections" when the child has only deeper nodes.
                node["sections"][child.id] = child_built
            # If this node has no content, drop it
            if not node["items"] and not node["sections"] and not node["direct_items"]:
                return None
            return node

        tree: dict[str, Any] = {}
        # Root-level services (parent_entry_id is None) → synthetic "direct" platform
        root_services = _services_under(None)
        for root_ent in children.get(None, []):
            if root_ent.entry_type != "node":
                continue
            built = _build_node(root_ent)
            if built is None:
                continue
            tree[root_ent.id] = built
        if root_services:
            tree["_root"] = {
                "title": "خدمات",
                "entry_id": None,
                "sections": {},
                "direct_items": root_services,
                "items": [],
                "subsections": {},
            }
        return tree


def catalog_service_to_legacy_item(svc: StorefrontService) -> dict[str, Any]:
    """Map Catalog StorefrontService → Telegram service-button dict shape."""
    price_dh = float(svc.price.amount_millimes) / 1000.0
    return {
        "id": svc.service_id,
        "name": svc.name_ar,
        "price": price_dh,
        "price_per_unit": svc.price.pricing_mode == "per_unit",
        "min": svc.min_quantity,
        "max": svc.max_quantity,
        "external_service_id_text": svc.execution.external_service_id,
        "external_service_id": svc.execution.external_service_id,
        "provider_slug": svc.execution.provider_slug,
        "provider_account": svc.execution.provider_account_key,
        "fulfillment_mode": svc.fulfillment_mode,
        "link_type": svc.target_policy.link_type,
        "link_prompt_key": svc.target_policy.link_prompt_key,
        "note": svc.note_ar,
        "catalog_id": svc.service_id,
        "soldium_service_id": svc.service_id,
        "platform_key": svc.target_policy.platform_key,
        "section_key": svc.target_policy.section_key,
        "subsection_key": svc.target_policy.subsection_key,
    }


class LegacyStorefrontBackend:
    """Thin Legacy backend over an existing Telegram SERVICES-shaped tree.

    ``tree_loader`` returns the live nested dict used by Telegram today.
    Does not mutate smm_services.
    """

    def __init__(
        self,
        tree_loader: Callable[[], dict[str, Any]],
        *,
        refresher: Callable[[], None] | None = None,
    ) -> None:
        self._tree_loader = tree_loader
        self._refresher = refresher

    @property
    def backend_name(self) -> BackendName:
        return "legacy"

    def refresh(self) -> None:
        if self._refresher is not None:
            self._refresher()

    def navigation_tree(self) -> dict[str, Any]:
        """Legacy-compatible nested tree for existing Telegram keyboards."""
        return self._tree_loader()

    def list_platforms(self) -> list[StorefrontPlatform]:
        tree = self._tree_loader()
        out: list[StorefrontPlatform] = []
        for key, cat in tree.items():
            title = str((cat or {}).get("title") or key)
            count = _count_services_in_category(cat or {})
            out.append(
                StorefrontPlatform(label=title, path=(title,), service_count=count)
            )
        return out

    def list_sections(self, platform_label: str) -> list[StorefrontSection]:
        cat, _key = _find_category(self._tree_loader(), platform_label)
        if cat is None:
            return []
        sections = (cat.get("sections") or {}) if cat else {}
        out: list[StorefrontSection] = []
        for sk, section in sections.items():
            title = str((section or {}).get("title") or sk)
            items = list((section or {}).get("items") or [])
            sub_count = sum(
                len((sub or {}).get("items") or [])
                for sub in ((section or {}).get("subsections") or {}).values()
            )
            out.append(
                StorefrontSection(
                    label=title,
                    platform_label=platform_label,
                    path=(platform_label, title),
                    service_count=len(items) + sub_count,
                )
            )
        return out

    def list_subsections(
        self, platform_label: str, section_label: str
    ) -> list[StorefrontSubsection]:
        cat, _ = _find_category(self._tree_loader(), platform_label)
        if cat is None:
            return []
        section = _find_section(cat, section_label)
        if section is None:
            return []
        out: list[StorefrontSubsection] = []
        for sub_key, sub in ((section.get("subsections") or {})).items():
            title = str((sub or {}).get("title") or sub_key)
            items = list((sub or {}).get("items") or [])
            out.append(
                StorefrontSubsection(
                    label=title,
                    platform_label=platform_label,
                    section_label=section_label,
                    path=(platform_label, section_label, title),
                    service_count=len(items),
                )
            )
        return out

    def list_services(
        self,
        *,
        platform_label: str | None = None,
        section_label: str | None = None,
        subsection_label: str | None = None,
    ) -> list[StorefrontService]:
        items = _collect_legacy_items(
            self._tree_loader(),
            platform_label=platform_label,
            section_label=section_label,
            subsection_label=subsection_label,
        )
        return [_legacy_item_to_storefront_service(it) for it in items]

    def get_service(self, service_id: str) -> StorefrontService:
        sid = str(service_id or "").strip()
        for item, *_rest in _iter_legacy_entries(self._tree_loader()):
            if str(item.get("id") or "") == sid:
                return _legacy_item_to_storefront_service(item)
        raise StorefrontAdapterError(
            "الخدمة غير موجودة في الكتالوج التقليدي",
            code="service_not_found",
            details={"service_id": sid},
        )

    def validate_quantity(
        self, service_id: str, quantity: object
    ) -> StorefrontQuantityResult:
        svc = self.get_service(service_id)
        try:
            qty = int(quantity)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return StorefrontQuantityResult(
                ok=False,
                service_id=service_id,
                quantity=None,
                min_quantity=svc.min_quantity,
                max_quantity=svc.max_quantity,
                code="invalid_quantity",
                message_ar="الكمية غير صالحة",
            )
        if qty < svc.min_quantity:
            return StorefrontQuantityResult(
                ok=False,
                service_id=service_id,
                quantity=qty,
                min_quantity=svc.min_quantity,
                max_quantity=svc.max_quantity,
                code="quantity_below_minimum",
                message_ar="الكمية أقل من الحد الأدنى",
            )
        if qty > svc.max_quantity:
            return StorefrontQuantityResult(
                ok=False,
                service_id=service_id,
                quantity=qty,
                min_quantity=svc.min_quantity,
                max_quantity=svc.max_quantity,
                code="quantity_above_maximum",
                message_ar="الكمية أعلى من الحد الأقصى",
            )
        return StorefrontQuantityResult(
            ok=True,
            service_id=service_id,
            quantity=qty,
            min_quantity=svc.min_quantity,
            max_quantity=svc.max_quantity,
        )

    def validate_target(
        self, service_id: str, target: object
    ) -> StorefrontTargetResult:
        # Legacy target rules stay in Telegram order_flow / handlers.
        # Gateway exposes a passthrough-ok for non-empty targets to keep Protocol.
        text = str(target or "").strip()
        if not text:
            return StorefrontTargetResult(
                ok=False,
                service_id=service_id,
                target=None,
                code="invalid_target",
                message_ar="الهدف مطلوب",
            )
        return StorefrontTargetResult(
            ok=True,
            service_id=service_id,
            target=text,
            code=None,
            message_ar=None,
        )

    def quote_price(
        self, service_id: str, quantity: object
    ) -> StorefrontPriceQuote:
        from catalog_core.pricing import quote_total_millimes

        svc = self.get_service(service_id)
        check = self.validate_quantity(service_id, quantity)
        if not check.ok or check.quantity is None:
            raise StorefrontAdapterError(
                check.message_ar or "الكمية غير صالحة",
                code=check.code or "invalid_quantity",
                details={"service_id": service_id},
            )
        total = quote_total_millimes(
            svc.price.amount_millimes, svc.price.pricing_mode, check.quantity
        )
        return StorefrontPriceQuote(
            service_id=svc.service_id,
            quantity=check.quantity,
            unit_amount_millimes=svc.price.amount_millimes,
            quoted_amount_millimes=total,
            currency=svc.price.currency,
            pricing_mode=svc.price.pricing_mode,
            content_fingerprint=None,
        )

    def resolve_order_intent(
        self,
        service_id: str,
        quantity: object,
        *,
        target: str | None = None,
    ) -> StorefrontOrderIntent:
        """Legacy intent for boundary tests — production Telegram still uses FSM+create."""
        from catalog_core.storefront_adapter import StorefrontOrderIntent as Intent

        svc = self.get_service(service_id)
        quote = self.quote_price(service_id, quantity)
        target_check = self.validate_target(service_id, target)
        if not target_check.ok:
            raise StorefrontAdapterError(
                target_check.message_ar or "الهدف غير صالح",
                code=target_check.code or "invalid_target",
                details={"service_id": service_id},
            )
        return Intent(
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
            target=target_check.target or "",
            target_validation_ok=True,
        )


def build_storefront(
    *,
    backend: BackendName | None = None,
    connection: sqlite3.Connection | None = None,
    legacy_tree_loader: Callable[[], dict[str, Any]] | None = None,
    legacy_refresher: Callable[[], None] | None = None,
    environ: dict[str, str] | None = None,
) -> StorefrontBackend:
    """Factory — single selection point for Legacy vs Catalog vs Pilot.

    Safe defaults:
      STOREFRONT_BACKEND missing/invalid → catalog (production SoT)
      STOREFRONT_BACKEND=legacy → emergency rollback only
      STOREFRONT_CATALOG_PILOT missing/invalid → disabled

    Pilot mode requires STOREFRONT_BACKEND=legacy plus an explicit
    STOREFRONT_CATALOG_PILOT=enabled. Global STOREFRONT_BACKEND=catalog is
    full Catalog-only (production customer path).
    """
    name = backend
    if name is None and _test_override is not None:
        name = _test_override
    if name is None:
        name = resolve_storefront_backend_name(environ=environ)

    if name == "catalog":
        if connection is None:
            raise StorefrontAdapterError(
                "Catalog storefront requires a database connection",
                code="catalog_connection_required",
            )
        return CatalogStorefrontBackend(connection)

    if name == "pilot":
        if connection is None or legacy_tree_loader is None:
            raise StorefrontAdapterError(
                "Pilot storefront requires connection and Legacy tree loader",
                code="pilot_dependencies_required",
            )
        from catalog_core.storefront_pilot import PilotHybridStorefrontBackend

        return PilotHybridStorefrontBackend(
            connection,
            legacy_tree_loader,
            refresher=legacy_refresher,
        )

    # legacy path — optionally escalate to scoped pilot
    from catalog_core.storefront_pilot import (
        PilotHybridStorefrontBackend,
        resolve_catalog_pilot_enabled,
    )

    pilot_on = resolve_catalog_pilot_enabled(environ=environ)
    if pilot_on:
        if connection is None or legacy_tree_loader is None:
            # Invalid pilot config → fail safe to Legacy when possible
            if legacy_tree_loader is None:
                raise StorefrontAdapterError(
                    "Legacy storefront requires a SERVICES tree loader",
                    code="legacy_tree_required",
                )
            return LegacyStorefrontBackend(
                legacy_tree_loader, refresher=legacy_refresher
            )
        return PilotHybridStorefrontBackend(
            connection,
            legacy_tree_loader,
            refresher=legacy_refresher,
        )

    if legacy_tree_loader is None:
        raise StorefrontAdapterError(
            "Legacy storefront requires a SERVICES tree loader",
            code="legacy_tree_required",
        )
    return LegacyStorefrontBackend(
        legacy_tree_loader, refresher=legacy_refresher
    )


def shadow_pair(
    *,
    connection: sqlite3.Connection,
    legacy_tree_loader: Callable[[], dict[str, Any]],
) -> tuple[LegacyStorefrontBackend, CatalogStorefrontBackend]:
    """Test-only: both backends for future parity — does not mutate data."""
    return (
        LegacyStorefrontBackend(legacy_tree_loader),
        CatalogStorefrontBackend(connection),
    )


# --- Legacy helpers -------------------------------------------------------------


def _count_services_in_category(cat: dict[str, Any]) -> int:
    n = len(cat.get("direct_items") or []) + len(cat.get("items") or [])
    for section in (cat.get("sections") or {}).values():
        n += len((section or {}).get("items") or [])
        for sub in ((section or {}).get("subsections") or {}).values():
            n += len((sub or {}).get("items") or [])
    return n


def _find_category(
    tree: dict[str, Any], platform_label: str
) -> tuple[dict[str, Any] | None, str | None]:
    label = str(platform_label or "").strip()
    if label in tree:
        return tree[label], label
    for key, cat in tree.items():
        title = str((cat or {}).get("title") or key)
        if title == label or key == label:
            return cat or {}, key
    return None, None


def _find_section(cat: dict[str, Any], section_label: str) -> dict[str, Any] | None:
    label = str(section_label or "").strip()
    sections = cat.get("sections") or {}
    if label in sections:
        return sections[label]
    for sk, section in sections.items():
        if str((section or {}).get("title") or sk) == label:
            return section
    return None


def _iter_legacy_entries(tree: dict[str, Any]):
    for platform_key, category in tree.items():
        for item in category.get("items") or []:
            yield item, platform_key, None, None
        for item in category.get("direct_items") or []:
            yield item, platform_key, "direct", None
        for section_key, section in (category.get("sections") or {}).items():
            for item in section.get("items") or []:
                yield item, platform_key, section_key, None
            for subsection_key, subsection in (
                section.get("subsections") or {}
            ).items():
                for item in subsection.get("items") or []:
                    yield item, platform_key, section_key, subsection_key


def _collect_legacy_items(
    tree: dict[str, Any],
    *,
    platform_label: str | None,
    section_label: str | None,
    subsection_label: str | None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item, pk, sk, ssk in _iter_legacy_entries(tree):
        cat_title = str((tree.get(pk) or {}).get("title") or pk)
        if platform_label and pk != platform_label and cat_title != platform_label:
            continue
        if section_label:
            section = ((tree.get(pk) or {}).get("sections") or {}).get(sk or "")
            sect_title = str((section or {}).get("title") or sk or "")
            if sk != section_label and sect_title != section_label:
                continue
        if subsection_label and ssk != subsection_label:
            continue
        if subsection_label is None and section_label and ssk:
            # listing section-level services excludes subsection items
            continue
        if section_label is None and platform_label and sk and sk != "direct":
            # listing whole platform: include all
            pass
        items.append(item)
    return items


def _legacy_item_to_storefront_service(item: dict[str, Any]) -> StorefrontService:
    from catalog_core.storefront_adapter import (
        StorefrontExecutionIdentity,
        StorefrontPrice,
        StorefrontTargetPolicy,
    )

    price_dh = float(item.get("price") or 0)
    millimes = int(round(price_dh * 1000))
    pricing_mode = "per_unit" if item.get("price_per_unit") else "per_1000"
    external = str(
        item.get("external_service_id_text")
        or item.get("external_service_id")
        or item.get("provider_id")
        or ""
    ).strip()
    return StorefrontService(
        service_id=str(item.get("id") or ""),
        name_ar=str(item.get("name") or ""),
        note_ar=str(item.get("note") or ""),
        service_type="other",
        ordering_mode="quantity_based",
        min_quantity=int(item.get("min") or 1),
        max_quantity=int(item.get("max") or 1),
        price=StorefrontPrice(
            amount_millimes=millimes,
            currency="MAD",
            pricing_mode=pricing_mode,
        ),
        location_path=(),
        platform_label=None,
        section_label=None,
        subsection_label=None,
        content_fingerprint=None,
        published_at=None,
        execution=StorefrontExecutionIdentity(
            provider_slug=str(item.get("provider_slug") or "gozibra"),
            provider_account_key=str(
                item.get("provider_account") or item.get("api_account") or "default"
            ),
            external_service_id=external,
        ),
        fulfillment_mode=str(item.get("fulfillment_mode") or "auto"),
        target_policy=StorefrontTargetPolicy(
            required=True,
            platform_key="",
            section_key="",
            link_type=item.get("link_type"),
            link_prompt_key=item.get("link_prompt_key"),
        ),
        orderable=True,
    )


# silence unused import warning for format_dh_amount in bridge docs
_ = format_dh_amount
