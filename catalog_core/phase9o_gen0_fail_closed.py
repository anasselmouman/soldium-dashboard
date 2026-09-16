# -*- coding: utf-8 -*-
"""Phase 9O — Gen-0 fail-closed remediation report."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalog_core.phase9d_audit import production_counts
from catalog_core.phase9g_business_decision_pack import _published_ids
from utils.order_execution_identity import GEN0_EXECUTION_IDENTITY_MISSING

PHASE = "9O"
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


def _repo_roots() -> list[Path]:
    here = Path(__file__).resolve().parents[1]
    roots = [here]
    sibling = here.parent / "soldium-bot"
    if sibling.is_dir():
        roots.append(sibling)
    return roots


def _classify_lookup_hit(path: Path, line: str) -> str:
    rel = str(path).replace("\\", "/")
    lower = line.lower()
    if "/tests/" in rel or rel.endswith("_test.py") or "test_" in path.name:
        return "E.tests"
    if any(
        x in rel
        for x in (
            "phase9",
            "scripts/out_",
            "architecture_review",
            ".md",
            "MULTI_PROVIDER",
        )
    ):
        return "E.docs_audit"
    if "ensure_provider_order_ref" in lower or "_submit_job_order" in lower:
        return "B.exec_path_check"
    if "_resolve_external_service_id_at_create" in lower or "at_create" in lower:
        return "C.create_time_freeze"
    if "manual_orders.py" in rel and "def _lookup_service_provider_meta" in line:
        return "C.shared_helper_definition"
    if "from manual_orders import _lookup_service_provider_meta" in line:
        return "C.import_for_create_freeze"
    return "D.other"


def scan_lookup_and_external() -> dict[str, Any]:
    lookup_hits: list[dict[str, Any]] = []
    external_hits: list[dict[str, Any]] = []
    submit_body_has_lookup = False

    so_path = Path(__file__).resolve().parents[1] / "scheduled_orders.py"
    so_text = so_path.read_text(encoding="utf-8")
    m = re.search(
        r"async def _submit_job_order\([\s\S]*?(?=\nasync def |\ndef )",
        so_text,
    )
    if m:
        submit_body_has_lookup = "_lookup_service_provider_meta" in m.group(0)

    mo_text = (Path(__file__).resolve().parents[1] / "manual_orders.py").read_text(
        encoding="utf-8"
    )
    ensure_has_lookup = False
    em = re.search(
        r"async def ensure_provider_order_ref\([\s\S]*?(?=\nasync def |\ndef )",
        mo_text,
    )
    if em:
        ensure_has_lookup = "_lookup_service_provider_meta" in em.group(0)

    for root in _repo_roots():
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(
                p in path.parts
                for p in (".git", "__pycache__", ".pytest_cache", "node_modules")
            ):
                continue
            if path.suffix.lower() not in {".py", ".md", ".json", ".js", ".html"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if "_lookup_service_provider_meta" in line:
                    lookup_hits.append(
                        {
                            "file": str(path),
                            "line": i,
                            "text": line.strip()[:200],
                            "class": _classify_lookup_hit(path, line),
                        }
                    )
                if "smm_services.external_service_id" in line or (
                    "FROM smm_services" in line and "external_service_id" in text
                ):
                    if "external_service_id" in line or "FROM smm_services" in line:
                        external_hits.append(
                            {
                                "file": str(path),
                                "line": i,
                                "text": line.strip()[:200],
                            }
                        )

    exec_rescue_remaining = [
        h
        for h in lookup_hits
        if h["class"] in {"B.exec_path_check"}
        and "must not" not in h["text"].lower()
        and "side_effect" not in h["text"].lower()
        and "AssertionError" not in h["text"]
    ]

    return {
        "lookup_hits": lookup_hits,
        "lookup_hit_count": len(lookup_hits),
        "submit_job_order_calls_lookup": submit_body_has_lookup,
        "ensure_provider_order_ref_calls_lookup": ensure_has_lookup,
        "exec_rescue_remaining": exec_rescue_remaining,
        "external_service_id_sample": external_hits[:40],
        "external_hit_sample_count": len(external_hits),
        "gen0_constant": GEN0_EXECUTION_IDENTITY_MISSING,
        "gen0_in_submit": GEN0_EXECUTION_IDENTITY_MISSING in so_text,
        "gen0_in_ensure": GEN0_EXECUTION_IDENTITY_MISSING in mo_text,
    }


def execution_path_audit() -> dict[str, Any]:
    return {
        "A_gen1": {
            "paths": [
                "manual_orders.ensure_provider_order_ref → frozen external_service_id_snapshot → submit_provider_order",
                "scheduled_orders._submit_job_order (is_gen1_scheduled_job) → frozen job.external_service_id → submit_provider_order",
                "soldium-bot handlers/orders create freezes snapshot at order create (storefront still Legacy catalog)",
            ],
            "behavior": "Uses frozen opaque TEXT Provider Service ID; no live smm_services SKU resolve at execution.",
        },
        "B_gen0": {
            "paths": [
                "manual_orders.ensure_provider_order_ref when snapshot NULL/empty/whitespace",
                "scheduled_orders._submit_job_order when job.external_service_id NULL/empty/whitespace",
            ],
            "behavior": f"Fail closed with {GEN0_EXECUTION_IDENTITY_MISSING}; no Provider API submit; no order rewrite.",
        },
        "C_shared": {
            "paths": [
                "manual_orders._lookup_service_provider_meta (definition)",
                "scheduled_orders._resolve_external_service_id_at_create (create-time Gen-1 freeze only)",
                "utils.order_execution_identity encode/is_gen1 helpers",
                "services.smm_provider.submit_provider_order (wire TEXT validation)",
            ],
            "behavior": "Lookup retained only for schedule *create* freeze when template lacks snapshot — isolated from execution.",
        },
        "D_dead": {
            "paths": [],
            "note": "Previous Gen-0 live rescue branch in _submit_job_order removed in Phase 9O.",
        },
        "E_tests_docs": {
            "paths": [
                "tests/test_phase9o_gen0_fail_closed.py",
                "tests/test_scheduled_gen1.py",
                "tests/test_order_execution_snapshot.py",
                "scripts/out_phase9m_cutover_blocker_audit.* (historical)",
            ],
        },
        "definitions": {
            "gen1": "Order/job has non-empty frozen Provider Service ID (orders.external_service_id_snapshot or scheduled_orders.external_service_id).",
            "gen0": "Frozen Provider Service ID missing/empty/whitespace — must not execute via Legacy SKU lookup.",
        },
    }


def run_phase9o(conn: sqlite3.Connection) -> dict[str, Any]:
    before = capture_baseline(conn)
    scan = scan_lookup_and_external()
    paths = execution_path_audit()

    fail_closed_ok = (
        not scan["submit_job_order_calls_lookup"]
        and not scan["ensure_provider_order_ref_calls_lookup"]
        and scan["gen0_in_submit"]
        and scan["gen0_in_ensure"]
        and before["ok"]
    )

    if not before["ok"]:
        verdict = "PHASE_9O_BLOCKED — REGRESSION DETECTED"
        next_step = "ARCHITECTURAL_REVIEW_REQUIRED"
    elif scan["submit_job_order_calls_lookup"] or scan["ensure_provider_order_ref_calls_lookup"]:
        verdict = "PHASE_9O_BLOCKED — GEN0 LEGACY LOOKUP REMAINS"
        next_step = "ARCHITECTURAL_REVIEW_REQUIRED"
    elif fail_closed_ok:
        verdict = "PHASE_9O_COMPLETE — GEN0 FAIL-CLOSED"
        next_step = "START_PHASE_9P_PRICING_SEMANTICS_REVIEW"
    else:
        verdict = "PHASE_9O_BLOCKED — REGRESSION DETECTED"
        next_step = "ARCHITECTURAL_REVIEW_REQUIRED"

    after = capture_baseline(conn)
    return {
        "phase": PHASE,
        "phase_name": "Phase 9O — Gen-0 Fail-Closed",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "next_step": next_step,
        "files_changed": [
            "utils/order_execution_identity.py",
            "scheduled_orders.py",
            "manual_orders.py",
            "tests/test_scheduled_gen1.py",
            "tests/test_scheduled_orders.py",
            "tests/test_phase9o_gen0_fail_closed.py",
            "catalog_core/phase9o_gen0_fail_closed.py",
            "scripts/run_phase9o_gen0_fail_closed.py",
            "scripts/out_phase9o_gen0_fail_closed.json",
            "scripts/out_phase9o_gen0_fail_closed.md",
        ],
        "execution_paths_audited": paths,
        "gen0_definition": paths["definitions"]["gen0"],
        "gen1_definition": paths["definitions"]["gen1"],
        "legacy_lookup_findings": scan,
        "fail_closed_behavior": {
            "error_code": GEN0_EXECUTION_IDENTITY_MISSING,
            "scheduled": "ScheduledOrderValidationError with GEN0_EXECUTION_IDENTITY_MISSING; no order created; no Provider submit",
            "manual": "ensure_provider_order_ref returns None; logs GEN0_EXECUTION_IDENTITY_MISSING; no Provider submit; no order mutation",
            "retry": "Gen-1 retries frozen ID; Gen-0 retries fail closed again",
        },
        "production_baseline": before["baseline"],
        "production_after": after["baseline"],
        "production_unchanged": before["ok"] and after["ok"] and before["baseline"] == after["baseline"],
        "production_mismatches": before["mismatches"] or after["mismatches"],
        "static_search": {
            "submit_job_order_calls_lookup": scan["submit_job_order_calls_lookup"],
            "ensure_provider_order_ref_calls_lookup": scan[
                "ensure_provider_order_ref_calls_lookup"
            ],
            "lookup_hit_count": scan["lookup_hit_count"],
        },
        "remaining_cutover_blockers": [
            "Telegram storefront still 100% Legacy (no Catalog cutover)",
            "Pricing semantics review (Phase 9P) not started",
            "Provider mappings = 0; Catalog publish coverage 43/253",
            "Create-time schedule freeze may still one-time lookup smm_services (not execution)",
            "Gen-0 historical orders remain non-executable by design (no auto-migration)",
        ],
        "explicit_non_goals": {
            "pricing_changed": False,
            "telegram_backend_switched": False,
            "e2e_performed": False,
            "parity_completion": False,
            "rollback_rehearsal": False,
            "cutover_performed": False,
            "gen0_data_migrated": False,
            "smm_services_mutated": False,
            "orders_mutated": False,
            "provider_mappings_modified": False,
            "publications_modified": False,
        },
        "hard_stop": True,
    }
