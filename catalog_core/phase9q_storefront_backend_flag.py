# -*- coding: utf-8 -*-
"""Phase 9Q — Storefront backend flag report (architecture verification)."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalog_core.phase9d_audit import production_counts
from catalog_core.phase9g_business_decision_pack import _published_ids
from catalog_core.storefront_gateway import (
    DEFAULT_BACKEND,
    ENV_STOREFRONT_BACKEND,
    resolve_storefront_backend_name,
)

PHASE = "9Q"
EXPECTED = {
    "services": 253,
    "nodes": 59,
    "entries": 312,
    "prices": 253,
    "execution_sources": 248,
    "mappings": 0,
    "publications": 53,
    "published_services": 43,
    "orders": 44,
    "smm_services": 2069,
    "scheduled_orders": 0,
}


def _scheduled_count(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("SELECT COUNT(*) FROM scheduled_orders").fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def capture_baseline(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = production_counts(conn)
    published = len(_published_ids(conn))
    baseline = {
        **{
            k: counts[k]
            for k in (
                "services",
                "nodes",
                "entries",
                "prices",
                "execution_sources",
                "mappings",
                "publications",
                "orders",
                "smm_services",
            )
        },
        "published_services": published,
        "scheduled_orders": _scheduled_count(conn),
    }
    mismatches = {
        k: {"expected": EXPECTED[k], "actual": baseline[k]}
        for k in EXPECTED
        if baseline.get(k) != EXPECTED[k]
    }
    return {"baseline": baseline, "mismatches": mismatches, "ok": not mismatches}


def static_coupling_audit() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    bot = root.parent / "soldium-bot"
    findings: list[dict[str, Any]] = []

    def classify(path: Path, line: str) -> str:
        rel = str(path).replace("\\", "/")
        if "storefront_gateway" in rel or "storefront/" in rel:
            return "storefront_abstraction"
        if "services_catalog_db" in rel or "services_config" in rel:
            return "legitimate_legacy_backend_impl"
        if "/handlers/" in rel:
            if "STOREFRONT_BACKEND" in line and "==" in line:
                return "forbidden_handler_selection"
            if "smm_services" in line:
                return "forbidden_storefront_coupling"
            if "navigation_tree" in line or "get_storefront" in line:
                return "handler_via_abstraction"
            return "handler_other"
        if "keyboards/" in rel:
            return "keyboard_via_abstraction" if "navigation_tree" in line or "_services" in line else "keyboard_other"
        if "test" in rel or "phase9" in rel:
            return "tests_audit"
        return "other"

    patterns = ("smm_services", "SERVICES", "services_catalog_db", "STOREFRONT_BACKEND", "get_storefront")
    scan_roots = [root / "catalog_core", bot / "handlers", bot / "keyboards", bot / "storefront", bot / "utils"]
    for base in scan_roots:
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if any(p in line for p in patterns):
                    findings.append(
                        {
                            "file": str(path),
                            "line": i,
                            "text": line.strip()[:160],
                            "class": classify(path, line),
                        }
                    )

    forbidden = [f for f in findings if f["class"] in {"forbidden_handler_selection", "forbidden_storefront_coupling"}]
    handler_text = ""
    hp = bot / "handlers" / "orders.py"
    if hp.is_file():
        handler_text = hp.read_text(encoding="utf-8")

    return {
        "findings_sample": findings[:60],
        "finding_count": len(findings),
        "forbidden": forbidden,
        "handlers_use_get_storefront": "get_storefront" in handler_text,
        "handlers_scattered_backend_if": bool(
            re.search(r"STOREFRONT_BACKEND\s*==", handler_text)
        ),
        "default_backend": DEFAULT_BACKEND,
        "env_var": ENV_STOREFRONT_BACKEND,
        "missing_env_resolves_to": resolve_storefront_backend_name(None, environ={}),
        "invalid_env_resolves_to": resolve_storefront_backend_name("nope"),
    }


def run_phase9q(conn: sqlite3.Connection) -> dict[str, Any]:
    before = capture_baseline(conn)
    audit = static_coupling_audit()
    after = capture_baseline(conn)
    production_ok = before["ok"] and after["ok"] and before["baseline"] == after["baseline"]
    architecture_ok = (
        audit["handlers_use_get_storefront"]
        and not audit["handlers_scattered_backend_if"]
        and not audit["forbidden"]
        and audit["missing_env_resolves_to"] == "legacy"
        and audit["invalid_env_resolves_to"] == "legacy"
        and production_ok
    )

    if not production_ok:
        verdict = "PHASE_9Q_BLOCKED — REGRESSION_DETECTED"
        next_step = "ARCHITECTURAL_REVIEW_REQUIRED"
    elif audit["forbidden"] or audit["handlers_scattered_backend_if"]:
        verdict = "PHASE_9Q_BLOCKED — LEGACY_COUPLING_REMAINS"
        next_step = "ARCHITECTURAL_REVIEW_REQUIRED"
    elif not architecture_ok:
        verdict = "PHASE_9Q_BLOCKED — CATALOG_CONTRACT_GAP"
        next_step = "ARCHITECTURAL_REVIEW_REQUIRED"
    else:
        verdict = "PHASE_9Q_COMPLETE — STOREFRONT_FLAG_READY"
        next_step = "START_PHASE_9R_TELEGRAM_CATALOG_E2E_SHADOW"

    return {
        "phase": PHASE,
        "phase_name": "Phase 9Q — Telegram Storefront Backend Flag",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "next_step": next_step,
        "architecture_implemented": {
            "pattern": "Telegram handlers → get_storefront() → LegacyStorefront | CatalogStorefront",
            "catalog_reuses": "StorefrontAdapter + PublishedStorefrontProjection",
            "legacy_wraps": "existing SERVICES tree via LegacyStorefrontBackend",
            "central_selection": "resolve_storefront_backend_name / build_storefront / bot storefront.get_storefront",
        },
        "storefront_contract": [
            "list_platforms",
            "list_sections",
            "list_subsections",
            "list_services",
            "get_service",
            "quote_price",
            "validate_quantity",
            "validate_target",
            "resolve_order_intent",
            "navigation_tree (Legacy-shaped keyboard compat)",
            "refresh",
        ],
        "legacy_backend": {
            "module": "catalog_core.storefront_gateway.LegacyStorefrontBackend + bot storefront facade",
            "data_source": "existing SERVICES / smm_services via services_catalog_db (unchanged)",
            "customer_ux": "preserved (same menus, callbacks, Arabic copy)",
        },
        "catalog_backend": {
            "module": "catalog_core.storefront_gateway.CatalogStorefrontBackend",
            "data_source": "publication snapshots only",
            "reads_smm_services": False,
            "excludes_unpublished": True,
        },
        "backend_selection": {
            "env": ENV_STOREFRONT_BACKEND,
            "default": DEFAULT_BACKEND,
            "missing_invalid": "legacy",
            "production_must_remain": "legacy",
        },
        "default_behavior": "Legacy — Catalog not customer-facing",
        "telegram_handler_integration": {
            "handlers_orders": "get_storefront().refresh(); navigation via _services()/navigation_tree()",
            "keyboards_orders": "navigation_tree() instead of direct SERVICES import",
            "utils_services": "find_* via navigation_tree()",
            "catalog_confirm_bridge": "when backend_name==catalog → resolve_order_intent → order_intent_to_create_bridge → create_order_with_balance_hold",
        },
        "order_contract_boundary": {
            "status": "ready_for_controlled_use",
            "helper": "order_intent_to_create_bridge",
            "note": "Maps quoted_amount_millimes → amount DH; freezes external_service_id_snapshot TEXT; does not create parallel Orders",
            "production_path": "Legacy FSM confirm unchanged when STOREFRONT_BACKEND=legacy",
        },
        "pricing_boundary": {
            "phase_9p": "LIVE_PRICE_RECOMMENDED for schedules unchanged",
            "immediate_legacy": "freeze at create + balance hold",
            "immediate_catalog": "quote published millimes → freeze at create + hold",
            "scheduled": "not modified in 9Q",
        },
        "execution_identity_boundary": {
            "catalog": "publication snapshot → Order Intent → external_service_id_snapshot",
            "no_legacy_sku_rescue": True,
            "opaque_text": True,
        },
        "target_link_boundary": {
            "catalog": "StorefrontAdapter.validate_target / target_policy.link_type",
            "legacy": "existing order_flow / Phase 9N link_type",
            "no_4371": True,
        },
        "static_coupling_audit": audit,
        "production_baseline": before["baseline"],
        "production_after": after["baseline"],
        "production_unchanged": production_ok,
        "production_mismatches": before["mismatches"] or after["mismatches"],
        "rollback_mechanism": {
            "action": "STOREFRONT_BACKEND=legacy (or unset)",
            "effect": "Telegram uses LegacyStorefront; no DB rewrite",
            "mutates_orders_publications_services": False,
        },
        "remaining_blockers": [
            "Catalog not enabled for production customers (intentional)",
            "Full Telegram Catalog E2E/shadow (Phase 9R)",
            "Parity Legacy vs Catalog (Phase 9S)",
            "Hardcoded platform menu keys vs Catalog label paths may need UX polish in 9R",
            "Provider cost snapshot on Catalog confirm still uses Legacy helper when available — review in E2E",
        ],
        "catalog_enabled_for_production_customers": False,
        "explicit_non_goals": {
            "production_env_changed": False,
            "catalog_enabled_in_prod": False,
            "customer_cutover": False,
            "publications_modified": False,
            "smm_services_modified": False,
            "orders_modified": False,
            "pricing_redesign": False,
            "scheduled_pricing_changed": False,
            "gen0_migrated": False,
        },
        "hard_stop": True,
        "files_changed": [
            "catalog_core/storefront_gateway.py",
            "catalog_core/phase9q_storefront_backend_flag.py",
            "scripts/run_phase9q_storefront_backend_flag.py",
            "tests/test_phase9q_storefront_backend_flag.py",
            "soldium-bot/storefront/__init__.py",
            "soldium-bot/config.py",
            "soldium-bot/.env.example",
            "soldium-bot/keyboards/orders.py",
            "soldium-bot/utils/services.py",
            "soldium-bot/handlers/orders.py",
        ],
    }
