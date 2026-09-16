# -*- coding: utf-8 -*-
"""Phase 9M — READ-ONLY cutover blocker audit (no production mutations)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalog_core.phase9d_audit import LEGACY_COMMENT_SERVICE_ID, production_counts
from catalog_core.phase9g_business_decision_pack import _published_ids
from catalog_core.phase9i_expansion_validation import audit_4371

PHASE = "9M"
REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED = {
    "services": 253,
    "published_services": 43,
    "publications": 53,
    "nodes": 59,
    "entries": 312,
    "prices": 253,
    "execution_sources": 248,
    "mappings": 0,
    "orders": 44,
    "scheduled_orders": 0,
    "smm_services": 2069,
}

PARITY_DIMENSIONS = (
    "platform",
    "section",
    "name",
    "type",
    "price",
    "qty",
    "fulfillment",
    "target",
    "execution",
    "orderability",
    "visibility",
)

REMEDIATION_PHASES = [
    {
        "phase": "9N",
        "title": "4371 remediation",
        "blocker": "B1",
        "action": (
            "Remove OR service_id==4371 only after Telegram cutover; "
            "rely on authored link_type=comment on svc_*; do not invent Provider IDs."
        ),
        "execute": False,
    },
    {
        "phase": "9O",
        "title": "Gen-0 fail-closed / removal",
        "blocker": "B2",
        "action": (
            "Keep Gen-1 create path; remove or fail-closed Gen-0 materialize branch "
            "after proofs; zero scheduled_orders simplifies."
        ),
        "execute": False,
    },
    {
        "phase": "9P",
        "title": "Gen-0/1 pricing semantics decision",
        "blocker": "B3",
        "action": (
            "Decide live-repricing vs freeze retail at schedule create; "
            "do not implement until architectural review."
        ),
        "execute": False,
    },
    {
        "phase": "9Q",
        "title": "Telegram Catalog adapter behind flag",
        "blocker": "B4",
        "action": (
            "Wire Telegram → Adapter → Published snapshot → Order Intent → "
            "create_order_with_balance_hold; env STOREFRONT_BACKEND=legacy|catalog on bot."
        ),
        "execute": False,
    },
    {
        "phase": "9R",
        "title": "Order E2E fake Provider",
        "blocker": "B4",
        "action": "Prove Order contract E2E with fake Provider using svc_* + snapshot.",
        "execute": False,
    },
    {
        "phase": "9S",
        "title": "Full parity gate",
        "blocker": "B5",
        "action": (
            "Shadow + controlled waves; acceptance dangerous=0; "
            "intentional Catalog improvements ≠ defects."
        ),
        "execute": False,
    },
    {
        "phase": "9T",
        "title": "Rollback rehearsal",
        "blocker": "B6",
        "action": (
            "Rehearse env flip to legacy; no Order rewrite; no publication mutation."
        ),
        "execute": False,
    },
    {
        "phase": "9U",
        "title": "Cutover Gate",
        "blocker": "B6",
        "action": "Owner/decision-triggered cutover after 9N–9T proofs.",
        "execute": False,
    },
]


def _scheduled_count(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("SELECT COUNT(*) FROM scheduled_orders").fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def capture_baseline(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = production_counts(conn)
    published = sorted(_published_ids(conn))
    baseline = {
        **{
            k: counts.get(k)
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
        "published_services": len(published),
        "scheduled_orders": _scheduled_count(conn),
        "published_service_ids": published,
    }
    mismatches = {
        k: {"expected": EXPECTED[k], "actual": baseline[k]}
        for k in EXPECTED
        if baseline.get(k) != EXPECTED[k]
    }
    return {"baseline": baseline, "mismatches": mismatches, "ok": not mismatches}


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _path_exists(rel: str) -> bool:
    return (REPO_ROOT / rel).exists()


def _scan_contains(rel: str, needle: str) -> bool:
    if not _path_exists(rel):
        return False
    return needle in _read(rel)


def audit_b1_4371() -> dict[str, Any]:
    """B1 — hardcoded 4371 comment-link fallback."""
    import re

    tv_rel = "catalog_core/target_validation.py"
    tv = _read(tv_rel)
    hardcode_re = re.compile(
        r"""str\(service_id[^)]*\)\s*==\s*["']4371["']|service_id\s*==\s*["']4371["']"""
    )
    hardcode_present = bool(hardcode_re.search(tv))
    # Catalog path: validate_order_target authors link_type on service dict; no service_id kw.
    vot_body = ""
    if "def validate_order_target(" in tv:
        vot_body = tv.split("def validate_order_target(", 1)[1].split(
            "\ndef ", 1
        )[0]
    validate_order_target_no_service_id = (
        "def validate_order_target(" in tv
        and "service_id=" not in vot_body
        and "service_id:" not in vot_body
    )
    catalog_path = {
        "entry": "validate_order_target",
        "uses_service_id": False,
        "uses_link_type": (
            'service["link_type"]' in vot_body
            or "service['link_type']" in vot_body
            or "link_type" in vot_body
        ),
        "note": (
            "Catalog path uses link_type=comment via validate_order_target "
            "(no service_id)."
        ),
    }
    runtime = {
        "function": "_validate_section_link_rules / _service_requires_comment_link",
        "pattern": "authored link_type/target_link_type == comment (Phase 9N)",
        "hardcode_present": hardcode_present,
        "file": tv_rel,
    }
    phase9d = {
        "constant": "LEGACY_COMMENT_SERVICE_ID",
        "value": LEGACY_COMMENT_SERVICE_ID,
        "module": "catalog_core/phase9d_audit.py",
        "note": (
            "Authoring/audit Legacy catalog id — not runtime Provider-ID validation."
        ),
    }
    bot_twin_path = REPO_ROOT.parent / "soldium-bot" / "utils" / "order_flow.py"
    if bot_twin_path.is_file():
        bot_text = bot_twin_path.read_text(encoding="utf-8")
        bot_twin = {
            "found": bool(hardcode_re.search(bot_text)),
            "path": str(bot_twin_path),
            "note": "Sibling bot order_flow runtime scan.",
        }
    else:
        bot_twin = {
            "found": False,
            "path": None,
            "note": "Sibling soldium-bot not found.",
        }

    from_phase9i = audit_4371()
    cleared = (
        not hardcode_present
        and not bot_twin.get("found")
        and from_phase9i.get("status") == "CLEARED"
    )
    return {
        "blocker_id": "B1",
        "title": "4371 hardcoded comment-link fallback",
        "severity": "HIGH" if not cleared else "CLEARED",
        "status": "CLEARED" if cleared else "OPEN",
        "evidence": {
            "target_validation_hardcode": hardcode_present,
            "runtime": runtime,
            "catalog_path": catalog_path,
            "validate_order_target_signature_omits_service_id": (
                validate_order_target_no_service_id
            ),
            "phase9d_legacy_comment_service_id": phase9d,
            "audit_4371": from_phase9i,
            "sibling_bot_twin": bot_twin,
        },
        "remediation": (
            "Phase 9N removed runtime Provider-ID branch; comment semantics use "
            "authored link_type (Legacy metadata merge + Catalog target_link_type)."
            if cleared
            else (
                "Remove OR service_id==4371; use authored link_type=comment; "
                "do not invent Provider IDs."
            )
        ),
        "remediation_status": "CLEARED" if cleared else "OPEN",
        "risk": "LOW" if cleared else "HIGH",
        "proposed_phase": "9N",
    }


def audit_b2_gen0_identity() -> dict[str, Any]:
    """B2 — Gen-0 live execution identity path still in scheduled_orders code."""
    so_rel = "scheduled_orders.py"
    so = _read(so_rel)
    ident_rel = "utils/order_execution_identity.py"
    ident = _read(ident_rel)

    create_freezes = (
        "async def create_scheduled_order" in so
        and "_resolve_external_service_id_at_create" in so
        and "frozen_external = await _resolve_external_service_id_at_create" in so
    )
    gen0_branch = (
        "Gen-0" in so
        and "_lookup_service_provider_meta" in so
        and "is_gen1_scheduled_job" in so
    )
    is_gen1_helper = "def is_gen1_scheduled_job" in ident
    return {
        "blocker_id": "B2",
        "title": "Gen-0 scheduled execution identity (live meta)",
        "severity": "HIGH",
        "status": "OPEN",
        "evidence": {
            "create_scheduled_order_always_freezes": create_freezes,
            "resolve_at_create": "_resolve_external_service_id_at_create",
            "new_jobs": "Gen-1 (frozen external_service_id)",
            "gen0_definition": "NULL/empty external_service_id",
            "gen0_materialize": (
                "_submit_job_order → live _lookup_service_provider_meta when not Gen-1"
            ),
            "is_gen1_scheduled_job": {
                "module": ident_rel,
                "present": is_gen1_helper,
                "rule": "truthy stripped external_service_id ⇒ Gen-1",
            },
            "gen0_code_path_present": gen0_branch,
            "scheduled_orders_rows": 0,
            "data_backfill_needed": False,
            "residual": "CODE path only (scheduled_orders=0)",
            "source_files": [so_rel, ident_rel, "manual_orders.py"],
        },
        "remediation": (
            "Keep Gen-1 create; remove or fail-closed Gen-0 materialize branch "
            "after proofs; zero rows simplifies."
        ),
        "remediation_status": "OPEN",
        "risk": (
            "Any future Gen-0 row (or code path exercised in tests/prod) "
            "resolves Provider SKU live from smm_services — unsafe after svc_* switch."
        ),
        "proposed_phase": "9O",
    }


def audit_b3_pricing() -> dict[str, Any]:
    """B3 — retail price live for both Gen-0 and Gen-1."""
    so = _read("scheduled_orders.py")
    live_pricing = (
        "async def _compute_order_pricing" in so
        and "local_price_dh" in so
        and "await get_service(service_id)" in so
    )
    comment_live = (
        "Retail price may still be computed live" in so
        or "independent of execution identity" in so
    )
    return {
        "blocker_id": "B3",
        "title": "Gen-0/Gen-1 retail price live-repricing",
        "severity": "MEDIUM",
        "status": "OPEN",
        "evidence": {
            "pricing_function": "_compute_order_pricing",
            "source": "get_service → local_price_dh LIVE for BOTH gens",
            "live_pricing_path_present": live_pricing,
            "docstring_notes_live_retail": comment_live,
            "execution_identity_vs_retail": (
                "Execution identity ≠ retail price; Gen-1 freezes SKU only."
            ),
            "design_options": [
                {
                    "option": "document_current",
                    "semantics": "live-repricing at materialize for Gen-0 and Gen-1",
                    "implement_now": False,
                },
                {
                    "option": "freeze_retail_at_create",
                    "semantics": "business decision; separate from execution snapshot",
                    "implement_now": False,
                },
            ],
            "compare": "Gen-1 frozen SKU vs live price (intentional decoupling today)",
        },
        "remediation": (
            "Document current live-repricing semantics; if business wants frozen "
            "retail at schedule create, decide in architectural review — do not implement in 9M."
        ),
        "remediation_status": "OPEN",
        "risk": (
            "Customer charged different amount at run vs schedule create if catalog "
            "price drifts; needs explicit product decision."
        ),
        "proposed_phase": "9P",
        "requires_architectural_review": True,
    }


def audit_b4_telegram() -> dict[str, Any]:
    """B4 — Telegram still on Legacy; Catalog adapter unwired to bot."""
    adapter = _path_exists("catalog_core/storefront_adapter.py")
    projection = _path_exists("catalog_core/storefront_projection.py")
    has_resolve = _scan_contains(
        "catalog_core/storefront_adapter.py", "def resolve_order_intent"
    )
    dash_flag = False
    for rel in (
        "main.py",
        "config.py",
        "workspaces.py",
        "catalog_core/storefront_adapter.py",
    ):
        if _path_exists(rel) and "STOREFRONT_BACKEND" in _read(rel):
            dash_flag = True
            break

    bot_root = REPO_ROOT.parent / "soldium-bot"
    bot_catalog_db = bot_root / "services_catalog_db.py"
    bot_flag = False
    bot_dep = {
        "repo": str(bot_root) if bot_root.is_dir() else None,
        "services_catalog_db_present": bot_catalog_db.is_file(),
        "STOREFRONT_BACKEND_present": False,
        "note": (
            "Bot is external; depends on smm_services via services_catalog_db pattern."
        ),
    }
    if bot_root.is_dir():
        for rel in (
            "config.py",
            "main.py",
            "services_catalog_db.py",
            "services_config.py",
            "utils/order_flow.py",
            ".env.example",
        ):
            p = bot_root / rel
            if not p.is_file():
                continue
            try:
                if "STOREFRONT_BACKEND" in p.read_text(encoding="utf-8"):
                    bot_flag = True
                    break
            except OSError:
                continue
        bot_dep["STOREFRONT_BACKEND_present"] = bot_flag

    order_safety = {
        "gen1_order_snapshot": {
            "status": "SAFE",
            "note": "Provider SKU via external_service_id_snapshot",
        },
        "gen0_scheduled": {
            "status": "UNSAFE",
            "note": "live _lookup_service_provider_meta",
        },
        "analytics_order_alert_joins": {
            "status": "UNKNOWN_UNSAFE",
            "evidence": [
                "analytics.py joins orders.service_id → smm_services.local_item_id",
                "utils/order_alert_format.py ORDER_ALERT_CATALOG_JOIN on local_item_id",
            ],
            "note": "Joins on local_item_id UNKNOWN/unsafe after svc_* switch.",
        },
    }

    return {
        "blocker_id": "B4",
        "title": "Telegram Catalog adapter unwired",
        "severity": "CRITICAL",
        "status": "OPEN",
        "evidence": {
            "dashboard": {
                "storefront_adapter": adapter,
                "storefront_projection": projection,
                "resolve_order_intent": has_resolve,
                "STOREFRONT_BACKEND_env": dash_flag,
            },
            "telegram_bot": bot_dep,
            "replacement_architecture": (
                "Telegram → Adapter → Published snapshot → Order Intent → "
                "existing create_order_with_balance_hold with svc_* + "
                "external_service_id_snapshot"
            ),
            "cutover_mechanism": (
                "new env STOREFRONT_BACKEND=legacy|catalog on bot "
                "(none exists today)"
            ),
            "order_historical_safety": order_safety,
        },
        "remediation": (
            "Implement bot flag + adapter wiring (9Q); prove Order E2E with fake "
            "Provider (9R). Do not invent flag semantics without review."
        ),
        "remediation_status": "OPEN",
        "risk": (
            "Cutover without bot path leaves customers on Legacy smm_services; "
            "analytics/alerts may break for svc_* orders."
        ),
        "proposed_phases": ["9Q", "9R"],
        "requires_architectural_review": True,
    }


def audit_b5_parity(baseline: dict[str, Any]) -> dict[str, Any]:
    published = int(baseline.get("published_services") or 0)
    services = int(baseline.get("services") or 0)
    return {
        "blocker_id": "B5",
        "title": "Catalog↔Legacy parity incomplete",
        "severity": "HIGH",
        "status": "OPEN",
        "evidence": {
            "dimensions": list(PARITY_DIMENSIONS),
            "published_services": published,
            "catalog_services": services,
            "ratio": f"{published}/{services}",
            "strategy": "shadow + controlled waves",
            "acceptance": "dangerous=0",
            "note": "Intentional Catalog improvements ≠ defects.",
        },
        "remediation": (
            "Continue controlled expansion; full parity gate (9S) before cutover."
        ),
        "remediation_status": "OPEN",
        "risk": "Partial catalog UX if Telegram flipped before parity acceptance.",
        "proposed_phase": "9S",
    }


def audit_b6_rollback() -> dict[str, Any]:
    return {
        "blocker_id": "B6",
        "title": "Rollback / cutover gate readiness",
        "severity": "HIGH",
        "status": "OPEN",
        "evidence": {
            "mechanism": "env flip to legacy (STOREFRONT_BACKEND=legacy)",
            "do_not": [
                "rewrite Orders",
                "mutate publications",
            ],
            "in_flight": "Provider submissions already sent stay as-is",
            "triggers": "owner/decision only",
            "rehearsal_required": True,
        },
        "remediation": (
            "Rollback rehearsal (9T) before Cutover Gate (9U); env flip only."
        ),
        "remediation_status": "OPEN",
        "risk": (
            "Without rehearsal, flip may strand in-flight UX or confuse ops; "
            "must not mutate historical orders/publications."
        ),
        "proposed_phases": ["9T", "9U"],
    }


def build_blocker_register(
    *,
    baseline: dict[str, Any],
    b1: dict[str, Any],
    b2: dict[str, Any],
    b3: dict[str, Any],
    b4: dict[str, Any],
    b5: dict[str, Any],
    b6: dict[str, Any],
) -> list[dict[str, Any]]:
    """Normalize B1–B6 register rows (unit-testable without production DB)."""
    _ = baseline
    rows = []
    for b in (b1, b2, b3, b4, b5, b6):
        rows.append(
            {
                "id": b["blocker_id"],
                "title": b["title"],
                "severity": b["severity"],
                "evidence": b["evidence"],
                "remediation": b["remediation"],
                "remediation_status": b.get("remediation_status", "OPEN"),
                "risk": b["risk"],
                "status": b.get("status", "OPEN"),
            }
        )
    return rows


def dependency_graph() -> dict[str, Any]:
    return {
        "edges": [
            {"from": "B1", "to": "B4", "note": "4371 remediation before Telegram cutover"},
            {
                "from": "B2",
                "to": "parallel",
                "with": "B3",
                "note": "Gen-0 identity and pricing can proceed in parallel",
            },
            {
                "from": "B3",
                "to": "parallel",
                "with": "B2",
                "note": "Pricing semantics decision parallel to Gen-0 fail-closed",
            },
            {
                "from": "B4",
                "to": "Order contract",
                "then": "E2E",
                "note": "Telegram adapter → Order contract → E2E fake Provider",
            },
            {
                "from": "B5",
                "to": "parallel",
                "with": "expansion",
                "note": "Parity work parallel with controlled expansion waves",
            },
            {
                "from": "B6",
                "to": "Cutover Gate",
                "note": "Rollback rehearsal before Cutover Gate",
            },
        ],
        "summary": "B1→B4; B2/B3 parallel; B4→Order contract→E2E; B5 parallel with expansion; B6 before Cutover Gate.",
    }


def run_phase9m(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read-only cutover blocker audit. No DB writes."""
    ts = datetime.now(timezone.utc).isoformat()
    before = capture_baseline(conn)
    if not before["ok"]:
        return {
            "phase": PHASE,
            "timestamp": ts,
            "verdict": "PHASE_9M_BLOCKED — BASELINE_MISMATCH",
            "next_step": "RESTORE_BASELINE_THEN_RERUN",
            "reason": "baseline mismatch",
            "production_baseline": before["baseline"],
            "baseline_mismatches": before["mismatches"],
            "mutations": "NONE",
            "production_unchanged": None,
            "tests": {"suite_previous": 408},
        }

    b1 = audit_b1_4371()
    b2 = audit_b2_gen0_identity()
    # Prefer live scheduled_orders count from baseline for B2 evidence.
    b2["evidence"]["scheduled_orders_rows"] = before["baseline"]["scheduled_orders"]
    b2["evidence"]["data_backfill_needed"] = (
        int(before["baseline"]["scheduled_orders"] or 0) > 0
    )
    b3 = audit_b3_pricing()
    b4 = audit_b4_telegram()
    b5 = audit_b5_parity(before["baseline"])
    b6 = audit_b6_rollback()

    register = build_blocker_register(
        baseline=before["baseline"],
        b1=b1,
        b2=b2,
        b3=b3,
        b4=b4,
        b5=b5,
        b6=b6,
    )
    blockers_by_id = {b["blocker_id"]: b for b in (b1, b2, b3, b4, b5, b6)}

    after = capture_baseline(conn)
    production_unchanged = (
        before["ok"]
        and after["ok"]
        and before["baseline"] == after["baseline"]
    )

    # Plan is coherent but pricing freeze vs live and Telegram flag design need review.
    verdict = "CUTOVER_REMEDIATION_PLAN_REQUIRES_REVIEW"
    next_step = "ARCHITECTURAL_REVIEW_REQUIRED"

    return {
        "phase": PHASE,
        "timestamp": ts,
        "verdict": verdict,
        "next_step": next_step,
        "mutations": "NONE",
        "production_unchanged": production_unchanged,
        "production_baseline": before["baseline"],
        "production_baseline_after": after["baseline"],
        "baseline_expected": dict(EXPECTED),
        "blockers": blockers_by_id,
        "blocker_register": register,
        "dependency_graph": dependency_graph(),
        "remediation_phases": REMEDIATION_PHASES,
        "parity": {
            "dimensions": list(PARITY_DIMENSIONS),
            "published": before["baseline"]["published_services"],
            "services": before["baseline"]["services"],
            "ratio": (
                f"{before['baseline']['published_services']}/"
                f"{before['baseline']['services']}"
            ),
        },
        "decision": {
            "cutover_remediation_plan": verdict,
            "all_blockers_traced": True,
            "important_open_decisions": [
                "pricing freeze vs live-repricing at schedule materialize (B3)",
                "Telegram STOREFRONT_BACKEND flag design on bot (B4)",
            ],
            "do_not": "START_REMEDIATION without architectural review",
        },
        "hard_stop": (
            "NO production mutations. NO START_REMEDIATION. "
            "Architectural review required before 9N–9U execution."
        ),
        "tests": {"suite_previous": 408},
    }
