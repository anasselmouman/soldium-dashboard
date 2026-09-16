# -*- coding: utf-8 -*-
"""Phase 9K runner — Wave 2 controlled publication."""
from __future__ import annotations

import json
import sys

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9k_wave2 import Wave2Block, run_wave2


def _md(report: dict) -> str:
    sel = report.get("selection_report") or {}
    ver = report.get("verification") or {}
    lines = [
        "# Phase 9K — Controlled Expansion Wave 2",
        "",
        f"Generated: `{report.get('timestamp')}`",
        "",
        f"**Verdict:** `{report.get('verdict')}`",
        "",
        f"Dry-run: `{report.get('dry_run')}`",
        "",
        f"Published: `{report.get('published')}`",
        "",
        "## Baseline",
        "",
        f"`{json.dumps(report.get('baseline') or report.get('production_end'), ensure_ascii=False)}`",
        "",
        "## Exact Wave 2",
        "",
        f"Size: **{sel.get('wave_size')}**",
        "",
        f"Legacy IDs: `{', '.join(sel.get('legacy_ids') or [])}`",
        "",
        f"Provider Service IDs: `{', '.join(sel.get('provider_service_ids') or [])}`",
        "",
        "## Identity table (معرّف المزود first)",
        "",
    ]
    for row in sel.get("identity_table") or []:
        lines.append(
            f"- `{row.get('معرّف_المزود')}` | Legacy `{row.get('legacy_catalog_id')}` | "
            f"`{row.get('soldium_service_id')}` | {row.get('platform')}/{row.get('section')} | "
            f"{row.get('service_type')} | ready={row.get('readiness_ready')}"
        )
    if ver:
        lines.extend(
            [
                "",
                "## Verification",
                "",
                f"- Projection count: {ver.get('projection_count')}",
                f"- Published services: {ver.get('published_services')}",
                f"- Publications: {ver.get('publications')}",
                f"- Existing 28 unchanged: {ver.get('existing_twenty_eight_unchanged')}",
                f"- Shadow: `{json.dumps(ver.get('shadow') or {}, ensure_ascii=False)}`",
                f"- Shadow diffs: `{json.dumps(ver.get('shadow_difference_breakdown') or {}, ensure_ascii=False)[:1500]}`",
                "",
            ]
        )
    lines.extend(
        [
            "",
            "## Idempotency",
            "",
            f"`{json.dumps(report.get('idempotency_second_pass'), ensure_ascii=False)}`",
            "",
            "## Wave 1 vs Wave 2",
            "",
            f"`{json.dumps(report.get('wave1_vs_wave2'), ensure_ascii=False)[:2000]}`",
            "",
            "## Safety",
            "",
            f"- Orders: `{json.dumps(report.get('orders_safety'), ensure_ascii=False)}`",
            f"- Scheduled: `{json.dumps(report.get('scheduled_orders_safety'), ensure_ascii=False)}`",
            f"- Legacy: `{json.dumps(report.get('legacy_safety'), ensure_ascii=False)}`",
            f"- Provider: `{json.dumps(report.get('provider_safety'), ensure_ascii=False)}`",
            "",
            "## Cutover blockers (not fixed)",
            "",
        ]
    )
    for b in ((report.get("cutover_blockers") or {}).get("blockers") or []):
        lines.append(f"- {b}")
    lines.extend(
        [
            "",
            "## Production end",
            "",
            f"`{json.dumps(report.get('production_end'), ensure_ascii=False)}`",
            "",
            f"Invariants ok: `{report.get('production_invariants_ok')}`",
            "",
            "## HARD STOP",
            "",
            "No Wave 3 / Ready mass-publish / cutover / Catalog mutations beyond "
            "these exact 15 publications. Wait for architectural review.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    apply = "--apply" in sys.argv
    dry_run = not apply
    try:
        with catalog_transaction(resolve_db_path()) as conn:
            report = run_wave2(conn, dry_run=dry_run)
    except Wave2Block as exc:
        report = {
            "verdict": "PHASE_9K_WAVE_2_BLOCKED — REVIEW REQUIRED",
            "error": exc.message,
            "details": exc.details,
            "dry_run": dry_run,
            "published": False,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        with open(
            "scripts/out_phase9k_wave2_publication.json", "w", encoding="utf-8"
        ) as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        sys.exit(2)

    report.setdefault("tests", {})
    report["tests"]["suite_previous"] = 399

    with open(
        "scripts/out_phase9k_wave2_publication.json", "w", encoding="utf-8"
    ) as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open(
        "scripts/out_phase9k_wave2_publication.md", "w", encoding="utf-8"
    ) as fh:
        fh.write(_md(report))

    slim = {
        "verdict": report.get("verdict"),
        "dry_run": report.get("dry_run"),
        "wave_size": (report.get("selection_report") or {}).get("wave_size"),
        "legacy_ids": (report.get("selection_report") or {}).get("legacy_ids"),
        "provider_service_ids": (report.get("selection_report") or {}).get(
            "provider_service_ids"
        ),
        "published": report.get("published"),
        "projection": (report.get("verification") or {}).get("projection_count"),
        "publications": (report.get("verification") or {}).get("publications"),
        "shadow": (report.get("verification") or {}).get("shadow"),
        "idempotency": report.get("idempotency_second_pass"),
        "invariants_ok": report.get("production_invariants_ok"),
    }
    print(json.dumps(slim, ensure_ascii=False, indent=2))
    if str(report.get("verdict", "")).startswith("PHASE_9K_WAVE_2_BLOCKED"):
        sys.exit(2)


if __name__ == "__main__":
    main()
