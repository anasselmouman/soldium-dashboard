# -*- coding: utf-8 -*-
"""Phase 9H Wave 1 runner — gate + controlled publication."""
from __future__ import annotations

import json
import sys

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9h_wave1 import Wave1Block, run_wave1


def _md(report: dict) -> str:
    lines = [
        "# Phase 9H — Controlled Expansion Wave 1",
        "",
        f"Generated: `{report.get('timestamp')}`",
        "",
        f"**Verdict:** `{report.get('verdict')}`",
        "",
        f"Dry-run: `{report.get('dry_run')}`",
        "",
    ]
    sel = report.get("selection_report") or {}
    lines.extend(
        [
            "## Selected Wave",
            "",
            f"Size: **{sel.get('wave_size')}**",
            "",
            f"Legacy IDs: `{', '.join(sel.get('legacy_ids') or [])}`",
            "",
            f"Provider Service IDs: `{', '.join(sel.get('provider_service_ids') or [])}`",
            "",
        ]
    )
    gate = report.get("gate") or {}
    if gate.get("excluded"):
        lines.append("## Exclusions")
        for e in gate["excluded"]:
            lines.append(
                f"- Legacy `{e.get('legacy_catalog_id')}`: {e.get('exclusion_reason')}"
            )
        lines.append("")
    ver = report.get("verification") or {}
    if ver:
        lines.extend(
            [
                "## Verification",
                "",
                f"- Projection count: {ver.get('projection_count')}",
                f"- Publications: {ver.get('publications')}",
                f"- Existing 13 unchanged: {ver.get('existing_thirteen_unchanged')}",
                f"- Shadow: `{json.dumps(ver.get('shadow') or {}, ensure_ascii=False)}`",
                "",
            ]
        )
    lines.extend(["## HARD STOP", "", "No further waves / cutover / Ready mass-publish.", ""])
    return "\n".join(lines)


def main() -> None:
    apply = "--apply" in sys.argv
    dry_run = not apply
    try:
        with catalog_transaction(resolve_db_path()) as conn:
            report = run_wave1(conn, dry_run=dry_run)
    except Wave1Block as exc:
        report = {
            "verdict": "PHASE 9H WAVE 1 BLOCKED — REVIEW REQUIRED",
            "error": exc.message,
            "details": exc.details,
            "dry_run": dry_run,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        with open("scripts/out_phase9h_wave1.json", "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        sys.exit(2)

    with open("scripts/out_phase9h_wave1.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open("scripts/out_phase9h_wave1.md", "w", encoding="utf-8") as fh:
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
    }
    print(json.dumps(slim, ensure_ascii=False, indent=2))
    if str(report.get("verdict", "")).startswith("PHASE 9H WAVE 1 BLOCKED"):
        sys.exit(2)


if __name__ == "__main__":
    main()
