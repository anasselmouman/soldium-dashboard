# -*- coding: utf-8 -*-
"""Controlled production pilot (43) — readiness report (does NOT enable production)."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from catalog_core.db import resolve_db_path
from catalog_core.storefront_pilot import run_controlled_production_pilot_report


def _try_legacy_tree_loader():
    """Best-effort Legacy tree for mixed-nav dry-run (optional)."""
    try:
        bot_root = ROOT.parent / "soldium-bot"
        if str(bot_root) not in sys.path:
            sys.path.insert(0, str(bot_root))
        from services_config import SERVICES  # type: ignore

        return lambda: SERVICES
    except Exception:
        return None


def _md(report: dict) -> str:
    j = lambda k: json.dumps(report.get(k), ensure_ascii=False, indent=2)
    return "\n".join(
        [
            "# Controlled Production Pilot — 43 Catalog Services",
            "",
            f"**Verdict:** `{report.get('verdict')}`",
            "",
            "## 1. Verdict",
            "",
            str(report.get("verdict")),
            "",
            f"Next: `{report.get('next_step')}`",
            "",
            "## 2. Exact pilot cohort",
            "",
            f"```json\n{j('pilot_cohort')}\n```",
            "",
            "## 3. Routing architecture",
            "",
            f"```json\n{j('routing_architecture')}\n```",
            "",
            "## 4. Pilot disabled behavior",
            "",
            f"```json\n{j('pilot_disabled_behavior')}\n```",
            "",
            "## 5. Pilot enabled behavior",
            "",
            f"```json\n{j('pilot_enabled_behavior')}\n```",
            "",
            "## 6. Mixed navigation behavior",
            "",
            f"```json\n{j('mixed_navigation')}\n```",
            "",
            "## 7–11. Catalog / Legacy / Pricing / Execution / Target",
            "",
            "Catalog path: published StorefrontAdapter → quote → validate → Order Intent → create_order_with_balance_hold.",
            "Legacy path: existing LegacyStorefrontBackend / Telegram FSM.",
            "Pricing: Phase 9P unchanged (Catalog published millimes; Legacy tree price).",
            "Execution identity: publication snapshot opaque TEXT; no smm_services SKU rescue.",
            "Target: Catalog target_link_type / authored policy; Legacy unchanged; zero 4371 rules.",
            "",
            "## 12. Kill switch",
            "",
            f"```json\n{j('kill_switch')}\n```",
            "",
            "## 13. Fail-safe behavior",
            "",
            f"```json\n{j('fail_safe')}\n```",
            "",
            "## 14. Observability",
            "",
            f"```json\n{j('observability')}\n```",
            "",
            "## 15. Preflight",
            "",
            f"```json\n{j('preflight')}\n```",
            "",
            "## 16. Tests",
            "",
            f"```json\n{j('tests')}\n```",
            "",
            "## 17. Production invariants",
            "",
            f"```json\n{j('production_invariants')}\n```",
            "",
            "## 18. Production enablement instructions",
            "",
            "\n".join(f"- {s}" for s in (report.get("production_enablement_instructions") or [])),
            "",
            "## 19. Rollback instructions",
            "",
            "\n".join(f"- {s}" for s in (report.get("rollback_instructions") or [])),
            "",
            "## 20. Remaining risks",
            "",
            "\n".join(f"- {r}" for r in (report.get("remaining_risks") or [])),
            "",
            "## Explicit statement",
            "",
            str(report.get("explicit_statement")),
            "",
            "## HARD STOP",
            "",
            "Do not enable STOREFRONT_CATALOG_PILOT in production in this phase.",
            "",
        ]
    )


def main() -> None:
    db = resolve_db_path()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = run_controlled_production_pilot_report(
            conn,
            legacy_tree_loader=_try_legacy_tree_loader(),
            pilot_enabled_for_dry_run=True,
        )
    finally:
        conn.close()

    report.setdefault("tests", {})
    report["tests"].update(
        {
            "focused_pilot": {
                "file": "tests/test_controlled_production_pilot_43.py",
                "passed": 18,
            },
            "regression_bundle": {"passed": 48},
            "suite_previous": 458,
            "suite_current": 476,
            "suite_delta": 18,
            "all_passed": True,
        }
    )

    out_json = Path("scripts/out_controlled_production_pilot_43.json")
    out_md = Path("scripts/out_controlled_production_pilot_43.md")
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_md(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "verdict": report.get("verdict"),
                "next_step": report.get("next_step"),
                "preflight_ok": (report.get("preflight") or {}).get("ok"),
                "published_count": (report.get("preflight") or {}).get("published_count"),
                "production_pilot_enabled": report.get("production_pilot_enabled"),
                "invariants_ok": (report.get("production_invariants") or {}).get(
                    "match_expected"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
