# -*- coding: utf-8 -*-
"""Run Legacy → Catalog customer storefront reconciliation.

Usage:
  python scripts/run_storefront_reconciliation.py --dry-run
  python scripts/run_storefront_reconciliation.py --apply

Does NOT change STOREFRONT_BACKEND. Leaves production Telegram on Legacy.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT_ROOT = ROOT.parent / "soldium-bot"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BOT_ROOT))

from catalog_core.db import catalog_connection, resolve_db_path
from catalog_core.storefront_reconciliation import run_reconciliation


def _legacy_tree_loader():
    from services_catalog_db import build_services_dict_from_db

    return build_services_dict_from_db()


def _md(report: dict) -> str:
    parity = report.get("parity") or {}
    before = report.get("before") or {}
    after = report.get("after") or {}
    lines = [
        "# Storefront Reconciliation Report",
        "",
        f"Generated: `{report.get('timestamp')}`",
        "",
        f"Dry-run: `{report.get('dry_run')}`",
        "",
        f"Backup: `{report.get('backup_path')}`",
        "",
        f"**safe_to_switch_telegram:** `{report.get('safe_to_switch_telegram')}`",
        "",
        "## Counts",
        "",
        f"| Metric | Before | After |",
        f"|---|---:|---:|",
        f"| Legacy active | {before.get('legacy_active')} | {after.get('legacy_active')} |",
        f"| Published | {before.get('published_services')} | {after.get('published_services')} |",
        f"| Projected | {before.get('projected_services')} | {after.get('projected_services')} |",
        f"| Catalog roots | {before.get('catalog_roots')} | {after.get('catalog_roots')} |",
        f"| Publication rows | {before.get('publication_rows')} | {after.get('publication_rows')} |",
        "",
        "## Parity",
        "",
        f"- Legacy visible services: `{parity.get('legacy_visible_services')}`",
        f"- Catalog visible services: `{parity.get('catalog_visible_services')}`",
        f"- Missing in Catalog: `{parity.get('missing_in_catalog_count')}`",
        f"- Roots OK: `{parity.get('roots_ok')}`",
        f"- Present labels OK: `{parity.get('present_labels_ok')}`",
        f"- Customer parity achieved: `{parity.get('customer_parity_achieved')}`",
        "",
        "## Blocked (Legacy-visible, not published)",
        "",
        f"Count: `{len(report.get('blocked') or [])}`",
        "",
    ]
    for b in (report.get("blocked") or [])[:30]:
        lines.append(
            f"- legacy `{b.get('legacy_catalog_id')}` / `{b.get('soldium_service_id')}` "
            f"— {b.get('reason')} {b.get('issues') or b.get('code') or ''}"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
        ]
    )
    for n in report.get("notes") or []:
        lines.append(f"- {n}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--apply", action="store_true", default=False)
    parser.add_argument("--db", type=str, default=None)
    args = parser.parse_args()
    if args.apply and args.dry_run:
        print("Choose either --dry-run or --apply", file=sys.stderr)
        return 2
    dry_run = not args.apply
    db_path = resolve_db_path(args.db) if args.db else Path(
        r"C:\Users\surface\Desktop\soldium proj\soldium-bot\users.db"
    )
    if args.db:
        db_path = Path(args.db)

    out_json = ROOT / "scripts" / "out_storefront_reconciliation.json"
    out_md = ROOT / "scripts" / "out_storefront_reconciliation.md"

    with catalog_connection(db_path) as conn:
        report = run_reconciliation(
            conn,
            dry_run=dry_run,
            db_path=db_path,
            legacy_tree_loader=_legacy_tree_loader,
            create_backup=not dry_run,
        )
        payload = report.to_dict()

    out_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    out_md.write_text(_md(payload), encoding="utf-8")
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(
        "safe_to_switch_telegram=",
        payload.get("safe_to_switch_telegram"),
        "projected=",
        (payload.get("after") or {}).get("projected_services"),
        "missing=",
        (payload.get("parity") or {}).get("missing_in_catalog_count"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
