# -*- coding: utf-8 -*-
"""Phase 9G-B runner — apply approved business decisions."""
from __future__ import annotations

import json
import sys

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9gb_business_decisions_apply import run_phase9gb


def _md(report: dict) -> str:
    lines = [
        "# Phase 9G-B — Approved Business Decisions Applied",
        "",
        f"Generated: `{report.get('timestamp')}`",
        "",
        f"**Verdict:** `{report.get('verdict')}`",
        "",
        f"Dry-run: `{report.get('dry_run')}`",
        "",
        "## Decision counts",
        "",
        "| Set | Count |",
        "|-----|------:|",
    ]
    for k, v in (report.get("decision_counts") or {}).items():
        lines.append(f"| {k} | {v} |")
    apply = report.get("apply") or {}
    lines.extend(
        [
            "",
            "## Apply result",
            "",
            f"- Mutations written: **{apply.get('mutation_count', 0)}**",
            f"- Already `other` (skipped): **{apply.get('skipped_already_other', 0)}**",
            f"- Idempotent second pass noop: **{(report.get('idempotency_second_pass') or {}).get('noop')}**",
            "",
            "## Invariants",
            "",
            f"- ok: **{(report.get('invariants') or {}).get('ok')}**",
            f"- published fingerprints unchanged: **{report.get('published_fingerprints_unchanged')}**",
            "",
        ]
    )
    inv = report.get("invariants") or {}
    if inv.get("errors"):
        lines.append("Errors:")
        for e in inv["errors"]:
            lines.append(f"- {e}")
    lines.extend(
        [
            "",
            "## Mutations",
            "",
        ]
    )
    muts = apply.get("mutations") or []
    if not muts:
        lines.append("_No service_type writes required — all approved targets already `other`._")
    else:
        lines.append("| Legacy | svc_* | before | after | decision |")
        lines.append("|--------|-------|--------|-------|----------|")
        for m in muts:
            lines.append(
                f"| {m['legacy_catalog_id']} | `{m['soldium_service_id']}` | "
                f"{m['previous_service_type']} | {m['new_service_type']} | {m['decision']} |"
            )
    lines.extend(
        [
            "",
            "## HARD STOP",
            "",
            "No publication, pilot publish, Ready publish, Provider/exec/price/qty/target changes.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    with catalog_transaction(resolve_db_path()) as conn:
        report = run_phase9gb(conn, dry_run=dry_run)
        with open(
            "scripts/out_phase9g_business_decisions_applied.json",
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        with open(
            "scripts/out_phase9g_business_decisions_applied.md",
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write(_md(report))
        slim = {
            "verdict": report.get("verdict"),
            "dry_run": report.get("dry_run"),
            "decision_counts": report.get("decision_counts"),
            "mutation_count": (report.get("apply") or {}).get("mutation_count"),
            "skipped_already_other": (report.get("apply") or {}).get(
                "skipped_already_other"
            ),
            "idempotent_noop": (report.get("idempotency_second_pass") or {}).get(
                "noop"
            ),
            "invariants_ok": (report.get("invariants") or {}).get("ok"),
            "errors": (report.get("invariants") or {}).get("errors"),
            "unexpected_not_ready": report.get("unexpected_not_ready"),
        }
        print(json.dumps(slim, ensure_ascii=False, indent=2))
        if report.get("verdict", "").startswith("PHASE 9G-B BLOCKED"):
            sys.exit(2)


if __name__ == "__main__":
    main()
