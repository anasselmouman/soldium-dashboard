# -*- coding: utf-8 -*-
"""Controlled production pilot — 43 published Catalog services + Legacy fallback.

Kill switch: STOREFRONT_CATALOG_PILOT (default disabled).
Does NOT flip STOREFRONT_BACKEND=catalog globally.

Routing:
    published + customer-eligible Catalog cohort → Catalog
    everything else → Legacy

Mixed navigation: Legacy tree base; bridged cohort items replaced in-place
with Catalog svc_* items (no duplicate menu entries).
"""

from __future__ import annotations

import copy
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from catalog_core.legacy_migration import LEGACY_SERVICE_BRIDGE_TABLE
from catalog_core.storefront_adapter import StorefrontAdapterError, StorefrontService
from catalog_core.storefront_gateway import (
    CatalogStorefrontBackend,
    LegacyStorefrontBackend,
    catalog_service_to_legacy_item,
    _iter_legacy_entries,
)
from catalog_core.storefront_shadow import load_bridge_correlation

logger = logging.getLogger("soldium.catalog.storefront_pilot")

ENV_STOREFRONT_CATALOG_PILOT = "STOREFRONT_CATALOG_PILOT"
PilotMode = Literal["disabled", "enabled"]
EXPECTED_PUBLISHED_COHORT = 43

_pilot_override: bool | None = None


def resolve_catalog_pilot_enabled(
    raw: str | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> bool:
    """Pilot kill switch. Missing/invalid → disabled (safe default)."""
    if _pilot_override is not None:
        return bool(_pilot_override)
    if raw is None:
        env = environ if environ is not None else os.environ
        raw = env.get(ENV_STOREFRONT_CATALOG_PILOT, "")
    text = str(raw or "").strip().lower()
    return text in {"1", "true", "yes", "on", "enabled", "pilot"}


def set_catalog_pilot_override(enabled: bool | None) -> None:
    """Test-only. Pass None to clear."""
    global _pilot_override
    _pilot_override = enabled


def clear_catalog_pilot_override() -> None:
    set_catalog_pilot_override(None)


@dataclass
class PilotMetrics:
    """Minimal structured counters (process-local)."""

    catalog_route: int = 0
    legacy_route: int = 0
    catalog_errors: int = 0
    order_intent_catalog: int = 0
    navigation_builds: int = 0
    fail_closed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "catalog_route": self.catalog_route,
            "legacy_route": self.legacy_route,
            "catalog_errors": self.catalog_errors,
            "order_intent_catalog": self.order_intent_catalog,
            "navigation_builds": self.navigation_builds,
            "fail_closed": self.fail_closed,
        }


_metrics = PilotMetrics()


def get_pilot_metrics() -> PilotMetrics:
    return _metrics


def reset_pilot_metrics() -> None:
    global _metrics
    _metrics = PilotMetrics()


def load_published_pilot_cohort(connection: sqlite3.Connection) -> frozenset[str]:
    """Authoritative cohort: currently customer-eligible published Catalog services."""
    catalog = CatalogStorefrontBackend(connection)
    return frozenset(s.service_id for s in catalog.list_services())


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def merge_pilot_navigation_tree(
    legacy_tree: dict[str, Any],
    *,
    catalog: CatalogStorefrontBackend,
    bridge_legacy_to_soldium: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge Catalog pilot cohort into Legacy tree without duplicates.

    - Base: deep copy of Legacy navigation (preserves Arabic UX placement).
    - Replace Legacy items whose catalog_id bridges to a published Catalog service.
    - Append any published Catalog services that lack Legacy placement by
      Catalog structural keys (should be rare for the 43 cohort).
    """
    tree = copy.deepcopy(legacy_tree or {})
    published = {s.service_id: s for s in catalog.list_services()}
    replaced = 0
    appended = 0
    replaced_svc: set[str] = set()

    for item, _pk, _sk, _ssk in _iter_legacy_entries(tree):
        leg_cid = str(item.get("catalog_id") or "").strip()
        if not leg_cid:
            continue
        svc_id = bridge_legacy_to_soldium.get(leg_cid)
        if not svc_id or svc_id not in published:
            continue
        if svc_id in replaced_svc:
            # Duplicate Legacy rows for same bridge — drop extras
            item.clear()
            item["_pilot_suppressed"] = True
            continue
        new_item = catalog_service_to_legacy_item(published[svc_id])
        item.clear()
        item.update(new_item)
        replaced_svc.add(svc_id)
        replaced += 1

    # Strip suppressed placeholders from lists
    _purge_suppressed(tree)

    placed = {
        str(it.get("id") or "")
        for it, *_ in _iter_legacy_entries(tree)
        if str(it.get("id") or "").startswith("svc_")
    }
    for svc_id, svc in published.items():
        if svc_id in placed:
            continue
        _append_catalog_item(tree, catalog_service_to_legacy_item(svc), svc)
        appended += 1
        placed.add(svc_id)

    # Dedup by service id (keep first)
    seen: set[str] = set()
    dupes = 0
    for item, *_ in list(_iter_legacy_entries(tree)):
        sid = str(item.get("id") or "")
        if not sid:
            continue
        if sid in seen:
            item.clear()
            item["_pilot_suppressed"] = True
            dupes += 1
        else:
            seen.add(sid)
    if dupes:
        _purge_suppressed(tree)

    stats = {
        "published_cohort": len(published),
        "replaced_in_legacy_placement": replaced,
        "appended_catalog_only": appended,
        "duplicates_removed": dupes,
        "final_svc_ids": sorted(s for s in seen if s.startswith("svc_")),
        "final_service_count": len(seen),
    }
    return tree, stats


def _purge_suppressed(tree: dict[str, Any]) -> None:
    for _pk, category in list(tree.items()):
        if not isinstance(category, dict):
            continue
        for list_key in ("items", "direct_items"):
            if list_key in category:
                category[list_key] = [
                    it
                    for it in (category.get(list_key) or [])
                    if not (isinstance(it, dict) and it.get("_pilot_suppressed"))
                ]
        for _sk, section in list((category.get("sections") or {}).items()):
            if not isinstance(section, dict):
                continue
            section["items"] = [
                it
                for it in (section.get("items") or [])
                if not (isinstance(it, dict) and it.get("_pilot_suppressed"))
            ]
            for _ssk, sub in list((section.get("subsections") or {}).items()):
                if not isinstance(sub, dict):
                    continue
                sub["items"] = [
                    it
                    for it in (sub.get("items") or [])
                    if not (isinstance(it, dict) and it.get("_pilot_suppressed"))
                ]


def _append_catalog_item(
    tree: dict[str, Any], item: dict[str, Any], svc: StorefrontService
) -> None:
    pk = str(svc.target_policy.platform_key or "").strip() or "other"
    sk = str(svc.target_policy.section_key or "").strip() or "direct"
    ssk = str(svc.target_policy.subsection_key or "").strip() or None
    plat = tree.setdefault(pk, {"title": pk, "sections": {}, "direct_items": []})
    if sk in {"", "direct", "none"} and not ssk:
        plat.setdefault("direct_items", []).append(item)
        return
    section = plat.setdefault("sections", {}).setdefault(
        sk, {"title": sk, "items": [], "subsections": {}}
    )
    if ssk:
        sub = section.setdefault("subsections", {}).setdefault(
            ssk, {"title": ssk, "items": []}
        )
        sub["items"].append(item)
    else:
        section.setdefault("items", []).append(item)


class PilotHybridStorefrontBackend:
    """Scoped Catalog cohort + Legacy fallback. backend_name == 'pilot'."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        legacy_tree_loader: Callable[[], dict[str, Any]],
        *,
        refresher: Callable[[], None] | None = None,
    ) -> None:
        self._connection = connection
        self._catalog = CatalogStorefrontBackend(connection)
        self._legacy = LegacyStorefrontBackend(
            legacy_tree_loader, refresher=refresher
        )
        self._tree_loader = legacy_tree_loader
        self._refresher = refresher
        self._bridge = load_bridge_correlation(connection)
        self._soldium_to_legacy = {v: k for k, v in self._bridge.items()}
        self._merged_cache: dict[str, Any] | None = None
        self._merge_stats: dict[str, Any] = {}

    @property
    def backend_name(self) -> Literal["pilot"]:
        return "pilot"

    def refresh(self) -> None:
        if self._refresher is not None:
            self._refresher()
        self._bridge = load_bridge_correlation(self._connection)
        self._soldium_to_legacy = {v: k for k, v in self._bridge.items()}
        self._merged_cache = None

    def cohort_ids(self) -> frozenset[str]:
        return load_published_pilot_cohort(self._connection)

    def uses_catalog_order_contract(self, service_id: str) -> bool:
        sid = str(service_id or "").strip()
        return sid.startswith("svc_") and sid in self.cohort_ids()

    def resolve_route(self, service_id: str) -> Literal["catalog", "legacy", "fail_closed"]:
        sid = str(service_id or "").strip()
        if sid.startswith("svc_"):
            if sid in self.cohort_ids():
                return "catalog"
            return "fail_closed"
        # Legacy id that was superseded by pilot Catalog → fail closed
        if self._legacy_id_superseded_by_pilot(sid):
            return "fail_closed"
        return "legacy"

    def _legacy_id_superseded_by_pilot(self, service_id: str) -> bool:
        sid = str(service_id or "").strip()
        cohort = self.cohort_ids()
        for item, *_ in _iter_legacy_entries(self._tree_loader() or {}):
            if str(item.get("id") or "") != sid:
                continue
            leg_cid = str(item.get("catalog_id") or "").strip()
            mapped = self._bridge.get(leg_cid) if leg_cid else None
            if mapped and mapped in cohort:
                return True
        return False

    def navigation_tree(self) -> dict[str, Any]:
        if self._merged_cache is not None:
            return self._merged_cache
        tree, stats = merge_pilot_navigation_tree(
            self._tree_loader() or {},
            catalog=self._catalog,
            bridge_legacy_to_soldium=self._bridge,
        )
        self._merged_cache = tree
        self._merge_stats = stats
        _metrics.navigation_builds += 1
        logger.info(
            "pilot_navigation_built cohort=%s replaced=%s appended=%s",
            stats.get("published_cohort"),
            stats.get("replaced_in_legacy_placement"),
            stats.get("appended_catalog_only"),
        )
        return tree

    def merge_stats(self) -> dict[str, Any]:
        self.navigation_tree()
        return dict(self._merge_stats)

    def list_platforms(self):
        # Platforms from merged tree via Legacy helper semantics
        return LegacyStorefrontBackend(lambda: self.navigation_tree()).list_platforms()

    def list_sections(self, platform_label: str):
        return LegacyStorefrontBackend(lambda: self.navigation_tree()).list_sections(
            platform_label
        )

    def list_subsections(self, platform_label: str, section_label: str):
        return LegacyStorefrontBackend(lambda: self.navigation_tree()).list_subsections(
            platform_label, section_label
        )

    def list_services(
        self,
        *,
        platform_label: str | None = None,
        section_label: str | None = None,
        subsection_label: str | None = None,
    ):
        return LegacyStorefrontBackend(lambda: self.navigation_tree()).list_services(
            platform_label=platform_label,
            section_label=section_label,
            subsection_label=subsection_label,
        )

    def get_service(self, service_id: str) -> StorefrontService:
        route = self.resolve_route(service_id)
        if route == "catalog":
            _metrics.catalog_route += 1
            logger.info("pilot_route backend=catalog service_id=%s", service_id)
            return self._catalog.get_service(service_id)
        if route == "fail_closed":
            _metrics.fail_closed += 1
            _metrics.catalog_errors += 1
            logger.warning(
                "pilot_fail_closed service_id=%s reason=identity_or_eligibility",
                service_id,
            )
            raise StorefrontAdapterError(
                "الخدمة غير متاحة عبر مسار الطيار الحالي",
                code="pilot_fail_closed",
                details={"service_id": str(service_id)},
            )
        _metrics.legacy_route += 1
        logger.info("pilot_route backend=legacy service_id=%s", service_id)
        return self._legacy.get_service(service_id)

    def validate_quantity(self, service_id: str, quantity: object):
        route = self.resolve_route(service_id)
        if route == "catalog":
            return self._catalog.validate_quantity(service_id, quantity)
        if route == "fail_closed":
            self.get_service(service_id)  # raises
        return self._legacy.validate_quantity(service_id, quantity)

    def validate_target(self, service_id: str, target: object):
        route = self.resolve_route(service_id)
        if route == "catalog":
            return self._catalog.validate_target(service_id, target)
        if route == "fail_closed":
            self.get_service(service_id)
        return self._legacy.validate_target(service_id, target)

    def quote_price(self, service_id: str, quantity: object):
        route = self.resolve_route(service_id)
        if route == "catalog":
            return self._catalog.quote_price(service_id, quantity)
        if route == "fail_closed":
            self.get_service(service_id)
        return self._legacy.quote_price(service_id, quantity)

    def resolve_order_intent(
        self,
        service_id: str,
        quantity: object,
        *,
        target: str | None = None,
    ):
        route = self.resolve_route(service_id)
        if route == "catalog":
            _metrics.order_intent_catalog += 1
            try:
                return self._catalog.resolve_order_intent(
                    service_id, quantity, target=target
                )
            except StorefrontAdapterError:
                _metrics.catalog_errors += 1
                raise
        if route == "fail_closed":
            self.get_service(service_id)
        return self._legacy.resolve_order_intent(
            service_id, quantity, target=target
        )


def run_pilot_preflight(
    connection: sqlite3.Connection,
    *,
    expected_cohort: int = EXPECTED_PUBLISHED_COHORT,
    legacy_tree_loader: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Read-only readiness check. Never enables pilot. Never mutates."""
    cohort = sorted(load_published_pilot_cohort(connection))
    bridge = load_bridge_correlation(connection)
    reverse = {v: k for k, v in bridge.items()}
    catalog = CatalogStorefrontBackend(connection)
    issues: list[dict[str, Any]] = []
    eligible_details: list[dict[str, Any]] = []

    for sid in cohort:
        try:
            svc = catalog.get_service(sid)
        except StorefrontAdapterError as exc:
            issues.append({"service_id": sid, "code": "not_loadable", "detail": str(exc)})
            continue
        ext = str(svc.execution.external_service_id or "").strip()
        ok = True
        row: dict[str, Any] = {
            "service_id": sid,
            "name_ar": svc.name_ar,
            "price_millimes": svc.price.amount_millimes,
            "external_service_id": ext,
            "external_is_str": isinstance(svc.execution.external_service_id, str),
            "svc_ne_ext": sid != ext,
            "has_bridge": sid in reverse,
            "legacy_catalog_id": reverse.get(sid),
            "platform_key": svc.target_policy.platform_key,
            "link_type": svc.target_policy.link_type,
        }
        if not ext:
            issues.append({"service_id": sid, "code": "missing_execution_identity"})
            ok = False
        if not isinstance(svc.execution.external_service_id, str):
            issues.append({"service_id": sid, "code": "execution_not_text"})
            ok = False
        if svc.price.amount_millimes is None or int(svc.price.amount_millimes) < 0:
            issues.append({"service_id": sid, "code": "invalid_price"})
            ok = False
        if not svc.target_policy.platform_key:
            issues.append({"service_id": sid, "code": "missing_target_platform"})
            ok = False
        if sid not in reverse:
            issues.append({"service_id": sid, "code": "missing_legacy_bridge"})
            # not necessarily hard-fail for append path, but flag
        row["ok"] = ok
        eligible_details.append(row)

    merge_stats: dict[str, Any] = {}
    if legacy_tree_loader is not None:
        _tree, merge_stats = merge_pilot_navigation_tree(
            legacy_tree_loader() or {},
            catalog=catalog,
            bridge_legacy_to_soldium=bridge,
        )
        # Unexpected: services in tree as svc_* beyond cohort
        unexpected = [
            s for s in (merge_stats.get("final_svc_ids") or []) if s not in set(cohort)
        ]
        if unexpected:
            issues.append({"code": "unexpected_pilot_services", "ids": unexpected})

    count_ok = len(cohort) == expected_cohort
    if not count_ok:
        issues.append(
            {
                "code": "cohort_count_mismatch",
                "expected": expected_cohort,
                "actual": len(cohort),
            }
        )

    hard_codes = {
        "not_loadable",
        "missing_execution_identity",
        "execution_not_text",
        "invalid_price",
        "missing_target_platform",
        "cohort_count_mismatch",
        "unexpected_pilot_services",
    }
    hard_fail = any(i.get("code") in hard_codes for i in issues)
    return {
        "ok": not hard_fail and count_ok,
        "published_count": len(cohort),
        "expected_count": expected_cohort,
        "cohort_ids": cohort,
        "bridge_coverage": sum(1 for s in cohort if s in reverse),
        "issues": issues,
        "eligible_details": eligible_details,
        "merge_stats": merge_stats,
        "bridge_table_present": _table_exists(connection, LEGACY_SERVICE_BRIDGE_TABLE),
    }


def capture_production_invariants(connection: sqlite3.Connection) -> dict[str, Any]:
    from catalog_core.phase9d_audit import production_counts
    from catalog_core.phase9g_business_decision_pack import _published_ids

    counts = production_counts(connection)
    try:
        scheduled = int(
            connection.execute("SELECT COUNT(*) FROM scheduled_orders").fetchone()[0]
        )
    except sqlite3.OperationalError:
        scheduled = 0
    return {
        "services": counts["services"],
        "nodes": counts["nodes"],
        "entries": counts["entries"],
        "prices": counts["prices"],
        "execution_sources": counts["execution_sources"],
        "mappings": counts["mappings"],
        "publications": counts["publications"],
        "published_services": len(_published_ids(connection)),
        "orders": counts["orders"],
        "scheduled_orders": scheduled,
        "smm_services": counts["smm_services"],
    }


EXPECTED_INVARIANTS = {
    "services": 253,
    "nodes": 59,
    "entries": 312,
    "prices": 253,
    "execution_sources": 248,
    "mappings": 0,
    "publications": 53,
    "published_services": 43,
    "orders": 44,
    "scheduled_orders": 0,
    "smm_services": 2069,
}


def run_controlled_production_pilot_report(
    connection: sqlite3.Connection,
    *,
    legacy_tree_loader: Callable[[], dict[str, Any]] | None = None,
    pilot_enabled_for_dry_run: bool = True,
) -> dict[str, Any]:
    """Build readiness report. Never mutates production. Never enables prod pilot."""
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    before = capture_production_invariants(connection)
    preflight = run_pilot_preflight(
        connection,
        legacy_tree_loader=legacy_tree_loader,
    )

    # Dry-run hybrid in isolation (does not touch env)
    dry: dict[str, Any] = {"ran": False}
    if pilot_enabled_for_dry_run and legacy_tree_loader is not None and preflight["ok"]:
        reset_pilot_metrics()
        set_catalog_pilot_override(True)
        try:
            hybrid = PilotHybridStorefrontBackend(
                connection, legacy_tree_loader
            )
            nav = hybrid.navigation_tree()
            cohort = hybrid.cohort_ids()
            sample_svc = next(iter(sorted(cohort)), None)
            catalog_ok = False
            legacy_ok = False
            if sample_svc:
                s = hybrid.get_service(sample_svc)
                catalog_ok = s.service_id == sample_svc
            # pick a legacy-only id from raw tree
            for item, *_ in _iter_legacy_entries(legacy_tree_loader() or {}):
                lid = str(item.get("id") or "")
                if lid and not lid.startswith("svc_") and not hybrid._legacy_id_superseded_by_pilot(lid):
                    hybrid.get_service(lid)
                    legacy_ok = True
                    break
            dry = {
                "ran": True,
                "backend_name": hybrid.backend_name,
                "nav_platforms": sorted(nav.keys()),
                "merge_stats": hybrid.merge_stats(),
                "sample_catalog_ok": catalog_ok,
                "sample_legacy_ok": legacy_ok,
                "metrics": get_pilot_metrics().as_dict(),
            }
        finally:
            clear_catalog_pilot_override()
            reset_pilot_metrics()

    after = capture_production_invariants(connection)
    inv_ok = before == after and all(
        before.get(k) == EXPECTED_INVARIANTS[k] for k in EXPECTED_INVARIANTS
    )

    if not preflight["ok"]:
        verdict = "PILOT_BLOCKED — REGRESSION_DETECTED"
        if any(i.get("code") == "cohort_count_mismatch" for i in preflight["issues"]):
            verdict = "PILOT_BLOCKED — ROUTING_AMBIGUOUS"
        next_step = "DO_NOT_ENABLE — fix preflight issues"
    elif not inv_ok:
        verdict = "PILOT_BLOCKED — REGRESSION_DETECTED"
        next_step = "DO_NOT_ENABLE — invariant mismatch"
    elif dry.get("ran") and not (
        dry.get("sample_catalog_ok") and dry.get("sample_legacy_ok")
    ):
        verdict = "PILOT_BLOCKED — MIXED_NAVIGATION_UNSAFE"
        next_step = "DO_NOT_ENABLE — mixed navigation dry-run failed"
    else:
        verdict = "PILOT_READY — 43_COHORT_SCOPED"
        next_step = "ENABLE_VIA_EXPLICIT_OPS_STEPS_ONLY"

    return {
        "phase": "controlled_production_pilot_43",
        "timestamp": ts,
        "verdict": verdict,
        "next_step": next_step,
        "pilot_cohort": {
            "source": "PublishedStorefrontProjection / CatalogStorefrontBackend.list_services()",
            "count": preflight["published_count"],
            "ids": preflight["cohort_ids"],
            "bridge_coverage": preflight["bridge_coverage"],
        },
        "routing_architecture": {
            "default_backend": "legacy",
            "global_catalog_switch": "NOT used for this pilot",
            "kill_switch_env": ENV_STOREFRONT_CATALOG_PILOT,
            "kill_switch_default": "disabled",
            "cohort_source": "live published customer-eligible Catalog publications",
            "service_route": "svc_* in published cohort → Catalog; else Legacy; superseded Legacy id → fail_closed",
            "factory": "build_storefront → PilotHybridStorefrontBackend when pilot enabled and STOREFRONT_BACKEND=legacy",
        },
        "pilot_disabled_behavior": {
            "STOREFRONT_CATALOG_PILOT": "unset/disabled",
            "result": "all traffic → LegacyStorefrontBackend",
        },
        "pilot_enabled_behavior": {
            "STOREFRONT_CATALOG_PILOT": "enabled",
            "STOREFRONT_BACKEND": "legacy (unchanged)",
            "result": "43 published cohort → Catalog; all other → Legacy",
        },
        "mixed_navigation": dry.get("merge_stats") or preflight.get("merge_stats") or {},
        "dry_run": dry,
        "preflight": preflight,
        "production_invariants": {
            "expected": EXPECTED_INVARIANTS,
            "before": before,
            "after": after,
            "unchanged": before == after,
            "match_expected": inv_ok,
        },
        "kill_switch": {
            "disable": f"unset {ENV_STOREFRONT_CATALOG_PILOT} or set to disabled",
            "effect": "immediate Legacy-only after storefront cache clear / process reload",
            "no_db_restore_required": True,
        },
        "fail_safe": {
            "invalid_pilot_config": "treated as disabled → Legacy",
            "svc_not_in_cohort": "fail_closed (no Legacy identity remap)",
            "legacy_id_superseded": "fail_closed",
            "missing_catalog": "StorefrontAdapterError",
        },
        "observability": {
            "logger": "soldium.catalog.storefront_pilot",
            "metrics": [
                "catalog_route",
                "legacy_route",
                "catalog_errors",
                "order_intent_catalog",
                "navigation_builds",
                "fail_closed",
            ],
        },
        "production_enablement_instructions": [
            "1. Confirm artifacts verdict is PILOT_READY — 43_COHORT_SCOPED",
            "2. Confirm preflight.ok is true and published_count == 43",
            "3. Deploy dashboard+bot code that includes PilotHybridStorefrontBackend",
            "4. Keep STOREFRONT_BACKEND=legacy (do NOT set catalog)",
            "5. Set STOREFRONT_CATALOG_PILOT=enabled on soldium-bot only",
            "6. Restart bot OR call get_storefront(force_reload=True) / clear caches",
            "7. Smoke-test one Catalog svc_* and one Legacy-only service",
            "8. Monitor soldium.catalog.storefront_pilot logs/metrics",
        ],
        "rollback_instructions": [
            "1. Set STOREFRONT_CATALOG_PILOT=disabled (or unset)",
            "2. Keep STOREFRONT_BACKEND=legacy",
            "3. Restart bot or force_reload storefront cache",
            "4. Verify all navigation is Legacy",
            "5. No database restore, no publication changes, no Order rewrites",
        ],
        "remaining_risks": [
            "Legacy callback payloads with superseded local ids fail closed until user re-browses",
            "Catalog-only append path uses structural keys if bridge placement missing",
            "210 Legacy-only services remain Legacy (intentional)",
            "Gen-0 historical orders still fail closed on missing frozen Provider ID",
            "Production enablement is a separate explicit ops action",
        ],
        "explicit_statement": (
            "NO production pilot enablement occurred in this phase. "
            "STOREFRONT_CATALOG_PILOT was not set in production. "
            "STOREFRONT_BACKEND remains legacy."
        ),
        "hard_stop": True,
        "production_pilot_enabled": False,
    }
