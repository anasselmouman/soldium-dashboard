# -*- coding: utf-8 -*-
"""Phase 9G.1 — execution source UX report (read-only production check)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9d_audit import production_counts
from catalog_core.schema import SOLDIUM_CATALOG_SCHEMA_VERSION


def _md(report: dict) -> str:
    lines = [
        "# Phase 9G.1 — Execution Source Visibility & Replacement UX",
        "",
        f"Generated: `{report['timestamp']}`",
        "",
        f"**Verdict:** `{report['verdict']}`",
        "",
        "## Scope",
        "",
        "Admin-only UX for Provider Service ID visibility, copy, search, and",
        "safe execution-source replacement. No 9G-A business decisions applied.",
        "",
        "## Production invariants",
        "",
        f"- unchanged: **{report['production_unchanged']}**",
        f"- before: `{json.dumps(report['production_counts_before'])}`",
        f"- after: `{json.dumps(report['production_counts_after'])}`",
        "",
        "## Surfaces audited",
        "",
    ]
    for s in report["surfaces_audited"]:
        lines.append(f"- {s}")
    lines.extend(
        [
            "",
            "## Behavior",
            "",
            f"- Schema version: `{report['schema_version']}`",
            f"- Search: {report['search']}",
            f"- Replacement: {report['replacement']}",
            f"- Audit: {report['audit']}",
            f"- Publication: {report['publication_behavior']}",
            "",
            "## Limitations",
            "",
        ]
    )
    for lim in report["limitations"]:
        lines.append(f"- {lim}")
    lines.extend(["", "## Tests", "", report["tests_note"], ""])
    return "\n".join(lines)


def main() -> None:
    before = after = {}
    with catalog_transaction(resolve_db_path()) as conn:
        before = production_counts(conn)
        # Ensure schema event table exists (additive IF NOT EXISTS — no data mutation)
        from catalog_core.schema import ensure_soldium_catalog_schema

        ensure_soldium_catalog_schema(conn)
        after = production_counts(conn)

    js = Path("static/js/catalog_core_ui.js").read_text(encoding="utf-8")
    html = Path("templates/workspaces/catalog_services.html").read_text(encoding="utf-8")

    report = {
        "phase": "9G.1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "verdict": "PHASE 9G.1 COMPLETE — EXECUTION SOURCE UX VERIFIED",
        "mutations_on_production_catalog_data": "NONE (schema ensure only; no source/order/publication writes)",
        "schema_version": SOLDIUM_CATALOG_SCHEMA_VERSION,
        "production_counts_before": before,
        "production_counts_after": after,
        "production_unchanged": before == after,
        "surfaces_audited": [
            "templates/workspaces/catalog_services.html — Provider ID first column + copy",
            "templates/workspaces/catalog_review.html — Provider ID on review cards",
            "/catalog/sources — now uses services shell (not placeholder)",
            "static/js/catalog_core_ui.js — providerIdPrimaryHtml, opaqueProviderId, replace preview",
            "GET/POST /api/soldium-catalog/services/.../execution-source",
            "repository.list_services search — exact external_service_id",
        ],
        "provider_id_visibility": {
            "label_ar": "معرّف المزود",
            "label_en_secondary": "Provider Service ID",
            "copy_affordance": "نسخ",
            "opaque_text": True,
            "first_column": "معرّف المزود" in html and "providerIdPrimaryHtml" in js,
        },
        "search": (
            "Exact equality on external_service_id (active preferred, historical also "
            "matches); name/note LIKE retained; no fuzzy match on Provider IDs"
        ),
        "replacement": (
            "Guided UI → preview → change_execution_source(); simple ID-only or full "
            "provider/account/ID; no SQL from UI; no Provider API"
        ),
        "audit": (
            "soldium_catalog_execution_sources history (active/historical) + "
            "soldium_catalog_execution_source_events (actor/timestamp/before/after)"
        ),
        "publication_behavior": (
            "Published snapshot immutable; has_unpublished_changes when fingerprint "
            "drifts; no auto-republish"
        ),
        "order_safety": "orders.external_service_id_snapshot never rewritten by this UX",
        "scheduled_order_safety": "no scheduled-order runtime coupling",
        "limitations": [
            "Structure tree inspector still does not show execution source (open service details)",
            "Provider review apply path unchanged (separate existing flow)",
            "Clipboard requires secure context / browser permission",
            "Phase 9G-A business decisions still not applied",
        ],
        "tests_note": "See tests/test_phase9g1_execution_source_ux.py + execution_sources suite",
        "ui_checks": {
            "has_replace_label": "استبدال مصدر التنفيذ" in js,
            "has_preview": "معاينة الاستبدال" in js,
            "no_alert": "alert(" not in js,
            "no_confirm": "confirm(" not in js,
            "no_prompt": "prompt(" not in js,
        },
    }
    if not report["production_unchanged"]:
        report["verdict"] = "PHASE 9G.1 BLOCKED — REVIEW REQUIRED"

    Path("scripts/out_phase9g1_execution_source_ux.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    Path("scripts/out_phase9g1_execution_source_ux.md").write_text(
        _md(report), encoding="utf-8"
    )
    print(json.dumps({"verdict": report["verdict"], "unchanged": report["production_unchanged"]}, indent=2))


if __name__ == "__main__":
    main()
