# -*- coding: utf-8 -*-
"""Phase 9E Stage B runner — controlled second-pilot publication."""
from __future__ import annotations

import json
import sys
import traceback

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.phase9e_stage_b import StageBBlock, publish_second_pilot


def main() -> int:
    dry_run = "--apply" not in sys.argv
    # Default is dry-run; require --apply for real publish.
    if "--dry-run" in sys.argv:
        dry_run = True
    if "--apply" in sys.argv:
        dry_run = False

    out_path = "scripts/out_phase9e_stage_b_publish.json"
    try:
        with catalog_transaction(resolve_db_path()) as conn:
            report = publish_second_pilot(conn, dry_run=dry_run)
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
            slim = {
                "verdict": report.get("verdict"),
                "dry_run": report.get("dry_run"),
                "publish_status": report.get("publish_status"),
                "published_ids": report.get("published_ids"),
                "blocked_reasons": report.get("blocked_reasons"),
                "mapping": report["preflight"]["mapping"],
                "projection_before": report["preflight"]["projection_count_before"],
                "projection_after": (
                    None
                    if not report.get("post_publish")
                    else report["post_publish"]["projection_count_after"]
                ),
                "counts_before": report["preflight"]["production_counts_before"],
                "counts_after": (
                    None
                    if not report.get("post_publish")
                    else report["post_publish"]["production_counts_after"]
                ),
                "shadow": (
                    None
                    if not report.get("post_publish")
                    else report["post_publish"]["shadow"]
                ),
            }
            print(json.dumps(slim, ensure_ascii=False, indent=2))
            return 0
    except StageBBlock as exc:
        payload = {
            "verdict": "PHASE 9E STAGE B BLOCKED — REVIEW REQUIRED",
            "error": str(exc),
            "details": exc.details,
        }
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    except Exception as exc:
        payload = {
            "verdict": "PHASE 9E STAGE B BLOCKED — REVIEW REQUIRED",
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
