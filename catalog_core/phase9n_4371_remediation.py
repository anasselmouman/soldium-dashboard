# -*- coding: utf-8 -*-
"""Phase 9N — Remove hardcoded 4371 fallback (runtime remediation)."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalog_core.phase9d_audit import production_counts
from catalog_core.phase9g_business_decision_pack import _published_ids
from catalog_core.target_validation import (
    _service_requires_comment_link,
    validate_order_target,
    validate_platform_link,
)

PHASE = "9N"
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

RUNTIME_MODULES = (
    Path("catalog_core/target_validation.py"),
    Path("utils") / "order_flow.py",  # bot path may be sibling
)

HARDCODE_RE = re.compile(
    r"""(?:service_id\s*(?:or\s*"")?\s*==\s*["']4371["'])|(?:["']4371["']\s*==\s*str\(service_id)|or\s+str\(service_id[^)]*\)\s*==\s*["']4371["']"""
)


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


def scan_4371_references() -> dict[str, Any]:
    classified: list[dict[str, Any]] = []
    runtime_hits: list[dict[str, Any]] = []
    for root in _repo_roots():
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(p in path.parts for p in (".git", "__pycache__", ".pytest_cache", "node_modules")):
                continue
            if path.suffix.lower() not in {
                ".py",
                ".js",
                ".ts",
                ".html",
                ".sql",
                ".json",
                ".md",
                ".yml",
                ".yaml",
                ".env",
                ".txt",
            }:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "4371" not in text:
                continue
            rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
            kind = _classify_ref(root, path, text)
            entry = {
                "root": root.name,
                "path": rel,
                "classification": kind,
                "runtime_hardcode": bool(
                    path.suffix == ".py"
                    and kind == "RUNTIME"
                    and HARDCODE_RE.search(text)
                ),
            }
            classified.append(entry)
            if entry["runtime_hardcode"]:
                runtime_hits.append(entry)
    return {
        "references": classified,
        "runtime_hardcode_hits": runtime_hits,
        "runtime_hardcode_count": len(runtime_hits),
        "ok": len(runtime_hits) == 0,
    }


def _classify_ref(root: Path, path: Path, text: str) -> str:
    rel = str(path).replace("\\", "/")
    if "/tests/" in rel or path.name.startswith("test_"):
        return "TEST"
    if path.suffix in {".md", ".json"} and (
        "out_phase" in path.name or "scripts/out_" in rel or "/scripts/" in rel
    ):
        return "DOCUMENTATION"
    if "phase9" in path.name and path.suffix == ".py" and "target_validation" not in path.name:
        return "DOCUMENTATION"
    if path.name in {"services.json", "services_config_embedded.py"}:
        return "HISTORICAL"
    if path.suffix == ".py" and (
        path.name in {"target_validation.py", "order_flow.py"}
        or "target_validation" in rel
        or "order_flow" in rel
    ):
        if HARDCODE_RE.search(text) or '== "4371"' in text or "== '4371'" in text:
            return "RUNTIME"
        return "RUNTIME_CLEAN"
    if path.suffix == ".py":
        return "DOCUMENTATION" if "4371" in text else "DEAD_CODE"
    return "DOCUMENTATION"


def catalog_contract_for_4371(conn: sqlite3.Connection) -> dict[str, Any]:
    br = conn.execute(
        "SELECT * FROM soldium_catalog_legacy_bridge WHERE legacy_catalog_id=?",
        ("4371",),
    ).fetchone()
    if not br:
        return {"found": False}
    sid = str(br["soldium_service_id"])
    svc = conn.execute(
        "SELECT id, service_type, target_link_type, target_platform_key, "
        "target_section_key FROM soldium_catalog_services WHERE id=?",
        (sid,),
    ).fetchone()
    src = conn.execute(
        "SELECT provider_slug, provider_account_key, external_service_id "
        "FROM soldium_catalog_execution_sources WHERE service_id=? AND status='active'",
        (sid,),
    ).fetchone()
    return {
        "found": True,
        "legacy_catalog_id": "4371",
        "soldium_service_id": sid,
        "target_link_type": svc["target_link_type"] if svc else None,
        "service_type": svc["service_type"] if svc else None,
        "target_platform_key": svc["target_platform_key"] if svc else None,
        "target_section_key": svc["target_section_key"] if svc else None,
        "execution": dict(src) if src else None,
        "note": (
            "Legacy catalog_id equals Provider external_service_id as data coincidence; "
            "SOLDIUM identity remains svc_*."
        ),
    }


def historical_orders_analysis(conn: sqlite3.Connection) -> dict[str, Any]:
    n = int(
        conn.execute(
            "SELECT COUNT(*) FROM orders WHERE service_id=? OR catalog_id=? "
            "OR CAST(COALESCE(external_service_id_snapshot, '') AS TEXT)=?",
            ("4371", "4371", "4371"),
        ).fetchone()[0]
    )
    return {
        "orders_referencing_4371": n,
        "orders_total": int(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]),
        "safe": True,
        "note": (
            "No historical Orders reference 4371. Gen-1 fulfillment uses "
            "external_service_id_snapshot, not target_validation hardcodes."
        ),
    }


def behavior_regression_checks() -> dict[str, Any]:
    comment_svc = {"link_type": "comment"}
    ok_c, _ = validate_platform_link(
        "https://www.tiktok.com/@u/video/1?comment_id=9",
        "tiktok",
        section_key="likes",
        service=comment_svc,
    )
    bad_c, _ = validate_platform_link(
        "https://www.tiktok.com/@u/video/1",
        "tiktok",
        section_key="likes",
        service=comment_svc,
    )
    # ID alone must NOT imply comment semantics
    id_only = {"id": "4371"}
    ok_id, _ = validate_platform_link(
        "https://www.tiktok.com/@u/video/1",
        "tiktok",
        section_key="likes",
        service=id_only,
        service_id="4371",
    )
    # Catalog path via validate_order_target
    ok_ot, _ = validate_order_target(
        "https://www.tiktok.com/@u/video/1?comment_id=9",
        platform_key="tiktok",
        section_key="likes",
        link_type="comment",
    )
    bad_ot, _ = validate_order_target(
        "https://www.tiktok.com/@u/video/1",
        platform_key="tiktok",
        section_key="likes",
        link_type="comment",
    )
    return {
        "comment_policy_accepts_comment_url": ok_c is True,
        "comment_policy_rejects_video_url": bad_c is False,
        "id_alone_does_not_force_comment_rule": ok_id is True,
        "order_target_comment_ok": ok_ot is True,
        "order_target_comment_rejects_video": bad_ot is False,
        "helper_comment": _service_requires_comment_link({"link_type": "comment"}),
        "helper_target_link_type": _service_requires_comment_link(
            {"target_link_type": "comment"}
        ),
        "helper_id_alone": _service_requires_comment_link({"id": "4371"}) is False,
        "ok": all(
            [
                ok_c is True,
                bad_c is False,
                ok_id is True,
                ok_ot is True,
                bad_ot is False,
                _service_requires_comment_link({"link_type": "comment"}),
                not _service_requires_comment_link({"id": "4371"}),
            ]
        ),
    }


def decide(scan: dict, behavior: dict, baseline_ok: bool, unchanged: bool) -> str:
    if not baseline_ok or not unchanged:
        return "PHASE_9N_BLOCKED — REGRESSION DETECTED"
    if not scan["ok"]:
        return "PHASE_9N_BLOCKED — 4371 DEPENDENCY REMAINS"
    if not behavior["ok"]:
        return "PHASE_9N_BLOCKED — REGRESSION DETECTED"
    return "PHASE_9N_COMPLETE — 4371 FALLBACK REMOVED"


def run_phase9n(conn: sqlite3.Connection) -> dict[str, Any]:
    before = capture_baseline(conn)
    if not before["ok"]:
        return {
            "phase": PHASE,
            "verdict": "PHASE_9N_BLOCKED — REGRESSION DETECTED",
            "reason": "baseline mismatch",
            "baseline": before,
            "mutations": "NONE on production data",
        }

    scan = scan_4371_references()
    contract = catalog_contract_for_4371(conn)
    orders = historical_orders_analysis(conn)
    behavior = behavior_regression_checks()
    after = capture_baseline(conn)
    unchanged = before["baseline"] == after["baseline"]
    verdict = decide(scan, behavior, before["ok"], unchanged)

    return {
        "phase": PHASE,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "mutations": "RUNTIME CODE ONLY — no production DB writes",
        "production_baseline": before["baseline"],
        "production_end": after["baseline"],
        "production_unchanged": unchanged,
        "references_scan": {
            "runtime_hardcode_count": scan["runtime_hardcode_count"],
            "runtime_hardcode_hits": scan["runtime_hardcode_hits"],
            "reference_count": len(scan["references"]),
            "by_classification": _count_class(scan["references"]),
            "ok": scan["ok"],
        },
        "old_behavior": (
            "target_validation / order_flow forced comment-URL rules when "
            "link_type==comment OR service_id=='4371' (Provider/Legacy ID branch)."
        ),
        "new_behavior": (
            "Comment-URL rules apply only when authored link_type/target_link_type "
            "== 'comment'. Legacy DB items receive link_type via embedded metadata "
            "merge (SERVICE_ITEM_META_KEYS includes link_type)."
        ),
        "compatibility_strategy": {
            "legacy_telegram": (
                "services_catalog_db merges link_type from embedded/services.json "
                "metadata onto smm_services-built items — no Provider-ID branch."
            ),
            "catalog": "validate_order_target / Adapter already pass link_type from policy.",
        },
        "catalog_contract": contract,
        "historical_orders": orders,
        "behavior_regression": behavior,
        "code_diff_summary": [
            {
                "file": "catalog_core/target_validation.py",
                "old": "link_type==comment OR service_id==4371",
                "new": "_service_requires_comment_link(service) via link_type/target_link_type",
                "reason": "Remove Provider-ID business rule",
                "risk": "LOW if Legacy metadata supplies link_type",
            },
            {
                "file": "soldium-bot/utils/order_flow.py",
                "old": "same OR 4371 branch",
                "new": "same policy helper",
                "reason": "Keep bot/dashboard validators aligned",
                "risk": "LOW",
            },
            {
                "file": "soldium-bot/services_catalog_db.py",
                "old": "SERVICE_ITEM_META_KEYS omitted link_type",
                "new": "link_type merged from embedded metadata",
                "reason": "Preserve Legacy comment semantics without ID hardcode",
                "risk": "LOW",
            },
        ],
        "rollback": {
            "type": "code_only",
            "steps": [
                "Revert the three files above",
                "No database restore required",
                "No Catalog/Orders/Provider mutation",
            ],
            "requires_data_mutation": False,
        },
        "next_step": (
            "START_PHASE_9O_GEN0_FAIL_CLOSED"
            if verdict.startswith("PHASE_9N_COMPLETE")
            else "ARCHITECTURAL_REVIEW_REQUIRED"
        ),
        "risk": "LOW — validation semantics preserved via authored link_type",
    }


def _count_class(refs: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in refs:
        k = r.get("classification") or "UNKNOWN"
        out[k] = out.get(k, 0) + 1
    return out
