# -*- coding: utf-8 -*-
"""Phase 9B.7 — controlled republish of five pilots with order contract fields.

Sets Catalog commercial fulfillment_mode + structural target keys from Legacy
bridge / smm_services (migration source only), then unpublish → republish via
CatalogPublicationService. Does not mutate historical publication rows.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from catalog_core.commercial import normalize_fulfillment_mode
from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.pilot_parity import resolve_structural_placement_keys
from catalog_core.publication import CatalogPublicationService
from catalog_core.service import CatalogCoreService

logger = logging.getLogger("soldium.catalog.order_contract_republish")

PILOT_SERVICE_IDS: tuple[str, ...] = (
    "svc_5a1e241e84d95d2f9de0ba6337b0a43d",
    "svc_a2745c669e395d52b0c5cfb6848889e8",
    "svc_697f7630fd6d5d22aaf85b1ca3d86382",
    "svc_d0a133fc5c5a5ad2a393886ae8d48439",
    "svc_444a46c672b25e41852f403c12a9928f",
)

PUBLISHED_BY = "phase9b7_order_contract"


@dataclass
class PilotRepublishResult:
    service_id: str
    legacy_catalog_id: str | None
    fulfillment_mode: str
    platform_key: str
    section_key: str
    subsection_key: str | None
    unpublish_outcome: str
    publish_outcome: str
    old_fingerprint: str | None
    new_fingerprint: str | None
    ok: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "legacy_catalog_id": self.legacy_catalog_id,
            "fulfillment_mode": self.fulfillment_mode,
            "platform_key": self.platform_key,
            "section_key": self.section_key,
            "subsection_key": self.subsection_key,
            "unpublish_outcome": self.unpublish_outcome,
            "publish_outcome": self.publish_outcome,
            "old_fingerprint": self.old_fingerprint,
            "new_fingerprint": self.new_fingerprint,
            "ok": self.ok,
            "error": self.error,
        }


@dataclass
class OrderContractRepublishReport:
    results: list[PilotRepublishResult] = field(default_factory=list)
    blocked: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "message": self.message,
            "pilot_count": len(self.results),
            "ok_count": sum(1 for r in self.results if r.ok),
            "results": [r.to_dict() for r in self.results],
        }


def _bridge_row(conn: sqlite3.Connection, service_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT legacy_catalog_id, legacy_fulfillment_mode, soldium_service_id
        FROM soldium_catalog_legacy_bridge
        WHERE soldium_service_id = ?
        """,
        (service_id,),
    ).fetchone()


def _legacy_keys(
    conn: sqlite3.Connection, legacy_catalog_id: str
) -> tuple[str, str, str | None, str | None, str | None]:
    row = conn.execute(
        """
        SELECT *
        FROM smm_services
        WHERE catalog_id = ?
        """,
        (legacy_catalog_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"legacy catalog_id missing: {legacy_catalog_id}")
    keys = set(row.keys())
    platform = str(row["platform_key"] or "").strip()
    section = str(row["section_key"] or "").strip()
    subsection = None
    if "subsection_key" in keys and row["subsection_key"] not in (None, ""):
        subsection = str(row["subsection_key"]).strip() or None
    link_prompt = None
    if "link_prompt_key" in keys and row["link_prompt_key"] not in (None, ""):
        link_prompt = str(row["link_prompt_key"]).strip() or None
    link_type = None
    if "link_type" in keys and row["link_type"] not in (None, ""):
        link_type = str(row["link_type"]).strip() or None
    if not platform or not section:
        raise RuntimeError(
            f"legacy keys incomplete for {legacy_catalog_id}: {platform!r}/{section!r}"
        )
    return platform, section, subsection, link_prompt, link_type


def apply_order_contract_fields(
    conn: sqlite3.Connection, service_id: str
) -> dict[str, Any]:
    """Migration-time only: copy fulfillment + structural keys onto Catalog service."""
    if service_id not in PILOT_SERVICE_IDS:
        raise RuntimeError(f"refusing non-pilot service: {service_id}")

    bridge = _bridge_row(conn, service_id)
    if bridge is None:
        raise RuntimeError(f"no legacy bridge for {service_id}")
    legacy_id = str(bridge["legacy_catalog_id"])
    fulfillment = normalize_fulfillment_mode(bridge["legacy_fulfillment_mode"])

    platform, section, subsection, link_prompt, link_type = _legacy_keys(conn, legacy_id)

    # Prefer structural node-bridge keys when available (must match Legacy).
    structural = resolve_structural_placement_keys(conn, service_id)
    if structural:
        sp, ss, ssub = structural
        sp = str(sp or "").strip()
        ss = str(ss or "").strip()
        ssub_n = str(ssub or "").strip() or None
        if sp and ss:
            if (sp, ss, ssub_n or "") != (platform, section, subsection or ""):
                logger.warning(
                    "structural keys differ from legacy row service=%s structural=%s legacy=%s",
                    service_id,
                    (sp, ss, ssub_n),
                    (platform, section, subsection),
                )
            platform, section, subsection = sp, ss, ssub_n

    core = CatalogCoreService(conn)
    core.update_service(
        service_id,
        fulfillment_mode=fulfillment,
        target_platform_key=platform,
        target_section_key=section,
        target_subsection_key=subsection,
        target_link_prompt_key=link_prompt,
        target_link_type=link_type,
    )
    return {
        "legacy_catalog_id": legacy_id,
        "fulfillment_mode": fulfillment,
        "platform_key": platform,
        "section_key": section,
        "subsection_key": subsection,
        "link_prompt_key": link_prompt,
        "link_type": link_type,
    }


def republish_pilots(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = True,
    service_ids: tuple[str, ...] = PILOT_SERVICE_IDS,
) -> OrderContractRepublishReport:
    report = OrderContractRepublishReport()
    pub = CatalogPublicationService(conn)

    for sid in service_ids:
        if sid not in PILOT_SERVICE_IDS:
            report.blocked = True
            report.message = f"refusing non-pilot service {sid}"
            return report

    # Preview all first — fail closed.
    prepared: list[tuple[str, dict[str, Any], str | None]] = []
    for sid in service_ids:
        try:
            fields = apply_order_contract_fields(conn, sid)
            latest = pub.get_latest_publish(sid)
            old_fp = latest.content_fingerprint if latest else None
            preview = pub.preview(sid)
            if not preview.can_publish:
                report.blocked = True
                report.message = (
                    f"preview blocked for {sid}: {preview.blocking_code} "
                    f"{preview.blocking_reasons}"
                )
                return report
            prepared.append((sid, fields, old_fp))
        except Exception as exc:  # noqa: BLE001 — collect blocker
            report.blocked = True
            report.message = f"{sid}: {exc}"
            return report

    if dry_run:
        for sid, fields, old_fp in prepared:
            report.results.append(
                PilotRepublishResult(
                    service_id=sid,
                    legacy_catalog_id=fields["legacy_catalog_id"],
                    fulfillment_mode=fields["fulfillment_mode"],
                    platform_key=fields["platform_key"],
                    section_key=fields["section_key"],
                    subsection_key=fields["subsection_key"],
                    unpublish_outcome="dry_run",
                    publish_outcome="dry_run",
                    old_fingerprint=old_fp,
                    new_fingerprint=None,
                    ok=True,
                )
            )
        report.message = "dry_run_ok"
        return report

    for sid, fields, old_fp in prepared:
        try:
            un = pub.unpublish(sid, published_by=PUBLISHED_BY)
            pr = pub.publish(sid, published_by=PUBLISHED_BY)
            new_fp = (
                pr.publication.get("content_fingerprint")
                if pr.publication
                else None
            )
            snap = pr.publication or {}
            if snap.get("fulfillment_mode") != fields["fulfillment_mode"]:
                raise RuntimeError("published fulfillment_mode mismatch")
            tp = snap.get("target_policy") or {}
            if tp.get("platform_key") != fields["platform_key"]:
                raise RuntimeError("published platform_key mismatch")
            if tp.get("section_key") != fields["section_key"]:
                raise RuntimeError("published section_key mismatch")
            report.results.append(
                PilotRepublishResult(
                    service_id=sid,
                    legacy_catalog_id=fields["legacy_catalog_id"],
                    fulfillment_mode=fields["fulfillment_mode"],
                    platform_key=fields["platform_key"],
                    section_key=fields["section_key"],
                    subsection_key=fields["subsection_key"],
                    unpublish_outcome=un.outcome,
                    publish_outcome=pr.outcome,
                    old_fingerprint=old_fp,
                    new_fingerprint=new_fp,
                    ok=pr.outcome == "published",
                )
            )
        except Exception as exc:  # noqa: BLE001
            report.blocked = True
            report.message = f"republish failed for {sid}: {exc}"
            report.results.append(
                PilotRepublishResult(
                    service_id=sid,
                    legacy_catalog_id=fields.get("legacy_catalog_id"),
                    fulfillment_mode=fields.get("fulfillment_mode", ""),
                    platform_key=fields.get("platform_key", ""),
                    section_key=fields.get("section_key", ""),
                    subsection_key=fields.get("subsection_key"),
                    unpublish_outcome="error",
                    publish_outcome="error",
                    old_fingerprint=old_fp,
                    new_fingerprint=None,
                    ok=False,
                    error=str(exc),
                )
            )
            return report

    report.message = "republish_ok"
    return report


def run_cli(*, apply: bool = False, db_path: str | None = None) -> dict[str, Any]:
    path = resolve_db_path(db_path)
    with catalog_transaction(path) as conn:
        from catalog_core.schema import ensure_soldium_catalog_schema

        ensure_soldium_catalog_schema(conn)
        if not apply:
            conn.execute("SAVEPOINT phase9b7_dry")
            try:
                report = republish_pilots(conn, dry_run=True)
            finally:
                conn.execute("ROLLBACK TO SAVEPOINT phase9b7_dry")
                conn.execute("RELEASE SAVEPOINT phase9b7_dry")
            out = report.to_dict()
            out["db_path"] = str(path)
            out["applied"] = False
            return out

        report = republish_pilots(conn, dry_run=False)
        out = report.to_dict()
        out["db_path"] = str(path)
        out["applied"] = not report.blocked
        if report.blocked:
            raise RuntimeError(report.message)
        return out


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Phase 9B.7 pilot order-contract republish")
    parser.add_argument("--apply", action="store_true", help="Write unpublish/republish")
    parser.add_argument("--db", default=None)
    parser.add_argument(
        "--out",
        default="scripts/out_phase9b7_republish.json",
        help="Write JSON report path",
    )
    args = parser.parse_args()

    try:
        payload = run_cli(apply=args.apply, db_path=args.db)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload.get("blocked"):
        sys.exit(2)
