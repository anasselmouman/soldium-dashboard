# -*- coding: utf-8 -*-
"""Phase 9E Stage B — Controlled second-pilot publication.

Publishes ONLY the eight Legacy IDs approved in Stage A.
No mass publish, no unpublish, no Telegram/Orders/Provider changes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from catalog_core.order_contract_republish import PILOT_SERVICE_IDS
from catalog_core.phase9c_audit import SENTINEL_MAX
from catalog_core.phase9d_audit import production_counts
from catalog_core.pilot_parity import _sample_target_url
from catalog_core.publication import CatalogPublicationService
from catalog_core.publication_pilot import PilotCandidate, publish_pilot, validate_pilot_intents
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.storefront_adapter import StorefrontAdapter, StorefrontAdapterError
from catalog_core.storefront_projection import PublishedStorefrontProjection
from catalog_core.storefront_shadow import compare_storefronts
from catalog_core.target_validation import validate_order_target

PUBLISHED_BY = "phase9e_second_pilot"

APPROVED_LEGACY_IDS: tuple[str, ...] = (
    "1896",
    "4459",
    "1632",
    "2223",
    "4867",
    "2488",
    "4547",
    "1407",
)

EXPECTED_SEMANTICS: dict[str, dict[str, str]] = {
    "1896": {
        "platform": "facebook",
        "section": "video_reels_views",
        "service_type": "views",
    },
    "4459": {
        "platform": "instagram",
        "section": "followers",
        "service_type": "followers",
    },
    "1632": {
        "platform": "tiktok",
        "section": "likes",
        "service_type": "likes",
    },
    "2223": {
        "platform": "youtube",
        "section": "geo_shares",
        "service_type": "shares",
    },
    "4867": {
        "platform": "telegram",
        "section": "post_views",
        "service_type": "views",
    },
    "2488": {
        "platform": "x",
        "section": "video_views",
        "service_type": "views",
    },
    "4547": {
        "platform": "facebook",
        "section": "live_stream_views",
        "service_type": "live_viewers",
    },
    "1407": {
        "platform": "tiktok",
        "section": "views",
        "service_type": "views",
    },
}

FIRST_PILOT_SET = frozenset(PILOT_SERVICE_IDS)


@dataclass
class StageBBlock(Exception):
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


def map_approved_pilots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for legacy_id in APPROVED_LEGACY_IDS:
        rows = conn.execute(
            """
            SELECT b.legacy_catalog_id, b.soldium_service_id,
                   l.platform_key, l.section_key, l.subsection_key
            FROM soldium_catalog_legacy_bridge b
            LEFT JOIN smm_services l ON l.catalog_id = b.legacy_catalog_id
            WHERE b.legacy_catalog_id = ?
            """,
            (legacy_id,),
        ).fetchall()
        if len(rows) != 1:
            raise StageBBlock(
                f"Legacy {legacy_id} mapping ambiguous or missing "
                f"(rows={len(rows)})",
                {"legacy_catalog_id": legacy_id, "row_count": len(rows)},
            )
        row = rows[0]
        mapped.append(
            {
                "legacy_catalog_id": legacy_id,
                "soldium_service_id": str(row["soldium_service_id"]),
                "platform_key": str(row["platform_key"] or "").strip() or None,
                "section_key": str(row["section_key"] or "").strip() or None,
                "subsection_key": (
                    str(row["subsection_key"]).strip()
                    if row["subsection_key"] not in (None, "")
                    else None
                ),
            }
        )
    svc_ids = [m["soldium_service_id"] for m in mapped]
    if len(set(svc_ids)) != 8:
        raise StageBBlock(
            "Duplicate soldium_service_id in approved mapping",
            {"ids": svc_ids},
        )
    overlap = set(svc_ids) & FIRST_PILOT_SET
    if overlap:
        raise StageBBlock(
            "Approved second pilot overlaps existing first pilots",
            {"overlap": sorted(overlap)},
        )
    return mapped


def _verify_one_candidate(
    conn: sqlite3.Connection, mapping: dict[str, Any]
) -> dict[str, Any]:
    legacy_id = mapping["legacy_catalog_id"]
    sid = mapping["soldium_service_id"]
    expected = EXPECTED_SEMANTICS[legacy_id]
    repo = CatalogRepository(conn)
    pub = CatalogPublicationService(conn)

    svc = repo.get_service(sid)
    if svc is None:
        raise StageBBlock(f"Service missing: {sid}")
    if str(svc.status) == "archived":
        raise StageBBlock(f"Service archived: {sid}")

    price = repo.get_active_price(sid)
    source = repo.get_active_execution_source(sid)
    entry = repo.get_entry_for_service(sid)
    if entry:
        svc.entry_id = entry.id
        svc.parent_entry_id = entry.parent_entry_id
        svc.location_path = repo.breadcrumb_names(entry.parent_entry_id)

    ready = evaluate_service_readiness(repo, svc, source=source, price=price)
    pub_status = pub.get_publication_status(sid)

    errors: list[str] = []
    if svc.service_type != expected["service_type"]:
        errors.append(
            f"service_type={svc.service_type} expected={expected['service_type']}"
        )
    if mapping["platform_key"] != expected["platform"]:
        errors.append(
            f"platform={mapping['platform_key']} expected={expected['platform']}"
        )
    if mapping["section_key"] != expected["section"]:
        errors.append(
            f"section={mapping['section_key']} expected={expected['section']}"
        )
    if svc.ordering_mode not in {"quantity_based", "package_based"}:
        errors.append(f"ordering_mode unknown: {svc.ordering_mode}")
    if int(svc.min_quantity) <= 0:
        errors.append("min_quantity invalid")
    if int(svc.max_quantity) <= 0 or int(svc.max_quantity) == SENTINEL_MAX:
        errors.append(f"max_quantity not finite commercial: {svc.max_quantity}")
    if price is None or int(price.amount_millimes) <= 0:
        errors.append("price invalid")
    if price is not None and str(price.currency).upper() != "MAD":
        errors.append(f"currency={price.currency}")
    if price is not None and price.pricing_mode not in {
        "per_1000",
        "per_unit",
        "fixed_package",
    }:
        errors.append(f"pricing_mode={price.pricing_mode}")
    if str(svc.fulfillment_mode or "") not in {"auto", "admin"}:
        errors.append(f"fulfillment_mode={svc.fulfillment_mode}")
    if not (svc.target_platform_key and svc.target_section_key):
        errors.append("target policy keys missing")
    if source is None:
        errors.append("execution source missing")
    else:
        if not str(source.provider_slug or "").strip():
            errors.append("provider_slug missing")
        if not str(source.provider_account_key or "").strip():
            errors.append("provider_account_key missing")
        if (
            source.external_service_id is None
            or str(source.external_service_id).strip() == ""
        ):
            errors.append("external_service_id missing")
    if not ready.ready:
        errors.append(
            "readiness not ready: "
            + ",".join(i.code for i in (ready.issues or []))
        )
    if pub_status.get("publication_status") == "published":
        errors.append("already published")
    if pub_status.get("customer_catalog_eligible") is True:
        errors.append("customer_catalog_eligible unexpectedly true before publish")

    if errors:
        raise StageBBlock(
            f"Candidate {legacy_id}/{sid} failed preflight",
            {"errors": errors, "mapping": mapping},
        )

    return {
        "legacy_catalog_id": legacy_id,
        "soldium_service_id": sid,
        "platform_key": mapping["platform_key"],
        "section_key": mapping["section_key"],
        "subsection_key": mapping["subsection_key"],
        "service_type": svc.service_type,
        "ordering_mode": svc.ordering_mode,
        "min_quantity": svc.min_quantity,
        "max_quantity": svc.max_quantity,
        "fulfillment_mode": svc.fulfillment_mode,
        "target_platform_key": svc.target_platform_key,
        "target_section_key": svc.target_section_key,
        "target_subsection_key": svc.target_subsection_key,
        "target_link_type": svc.target_link_type,
        "amount_millimes": int(price.amount_millimes) if price else None,
        "currency": price.currency if price else None,
        "pricing_mode": price.pricing_mode if price else None,
        "provider_slug": source.provider_slug if source else None,
        "provider_account_key": source.provider_account_key if source else None,
        "external_service_id": (
            str(source.external_service_id) if source else None
        ),
        "readiness_ready": True,
        "publication_status": pub_status.get("publication_status"),
        "customer_catalog_eligible": pub_status.get("customer_catalog_eligible"),
        "location_path": list(svc.location_path or []),
    }


def snapshot_first_pilots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    pub = CatalogPublicationService(conn)
    out: list[dict[str, Any]] = []
    for sid in PILOT_SERVICE_IDS:
        latest = pub.get_latest_publish(sid)
        status = pub.get_publication_status(sid)
        out.append(
            {
                "service_id": sid,
                "publication_status": status.get("publication_status"),
                "customer_catalog_eligible": status.get(
                    "customer_catalog_eligible"
                ),
                "content_fingerprint": (
                    latest.content_fingerprint if latest else None
                ),
                "publication_id": latest.id if latest else None,
                "fulfillment_mode": latest.fulfillment_mode if latest else None,
                "target_platform_key": (
                    latest.target_platform_key if latest else None
                ),
                "target_section_key": (
                    latest.target_section_key if latest else None
                ),
                "provider_slug": latest.provider_slug if latest else None,
                "provider_account_key": (
                    latest.provider_account_key if latest else None
                ),
                "external_service_id": (
                    str(latest.external_service_id)
                    if latest and latest.external_service_id is not None
                    else None
                ),
            }
        )
    published = sum(1 for x in out if x["publication_status"] == "published")
    if published != 5:
        raise StageBBlock(
            f"Expected 5 published first pilots, found {published}",
            {"pilots": out},
        )
    return out


def verify_first_pilots_unchanged(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> None:
    if before != after:
        raise StageBBlock(
            "Existing five pilots changed unexpectedly",
            {"before": before, "after": after},
        )


def _projection_count(conn: sqlite3.Connection) -> int:
    return len(PublishedStorefrontProjection(conn).list_services())


def run_preflight(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = production_counts(conn)
    expected_baseline = {
        "orders": 44,
        "smm_services": 2069,
        "services": 253,
        "nodes": 59,
        "entries": 312,
        "prices": 253,
        "execution_sources": 248,
        "mappings": 0,
        "publications": 15,
    }
    for key, val in expected_baseline.items():
        if int(counts.get(key, -1)) != val:
            raise StageBBlock(
                f"Baseline mismatch for {key}: got {counts.get(key)} "
                f"expected {val}",
                {"counts": counts},
            )

    first_pilots = snapshot_first_pilots(conn)
    mapped = map_approved_pilots(conn)
    candidates = [_verify_one_candidate(conn, m) for m in mapped]
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "production_counts_before": counts,
        "first_pilots_before": first_pilots,
        "mapping": mapped,
        "candidates": candidates,
        "projection_count_before": _projection_count(conn),
    }


def _pilot_sample_target(
    platform: str | None,
    section: str | None,
    subsection: str | None,
) -> str:
    pk = str(platform or "").strip()
    sk = str(section or "").strip()
    ssk = str(subsection or "").strip()
    if pk == "telegram" and sk == "post_views" and ssk != "past_posts":
        return "https://t.me/channel/123"
    if pk == "facebook" and sk == "live_stream_views":
        return "https://facebook.com/watch/live/?v=123"
    if pk == "x" and sk in {"video_views", "views"}:
        return "https://x.com/user/status/1234567890"
    if pk == "youtube":
        return "https://youtube.com/watch?v=dQw4w9WgXcQ"
    return _sample_target_url(pk)


def verify_post_publish(
    conn: sqlite3.Connection,
    candidates: list[dict[str, Any]],
    first_pilots_before: list[dict[str, Any]],
) -> dict[str, Any]:
    pub = CatalogPublicationService(conn)
    proj = PublishedStorefrontProjection(conn)
    adapter = StorefrontAdapter(conn)

    active = proj.list_services()
    active_ids = {s.service_id for s in active}
    if len(active) != 13:
        raise StageBBlock(
            f"projection_count expected 13, got {len(active)}",
            {"ids": sorted(active_ids)},
        )

    second_ids = {c["soldium_service_id"] for c in candidates}
    if not second_ids.issubset(active_ids):
        raise StageBBlock(
            "Not all second pilots in projection",
            {"missing": sorted(second_ids - active_ids)},
        )
    if not FIRST_PILOT_SET.issubset(active_ids):
        raise StageBBlock(
            "First pilots missing from projection",
            {"missing": sorted(FIRST_PILOT_SET - active_ids)},
        )

    parity_rows: list[dict[str, Any]] = []
    target_matrix: list[dict[str, Any]] = []

    for c in candidates:
        sid = c["soldium_service_id"]
        latest = pub.get_latest_publish(sid)
        status = pub.get_publication_status(sid)
        if latest is None or status.get("publication_status") != "published":
            raise StageBBlock(f"Not published after Stage B: {sid}")
        if status.get("customer_catalog_eligible") is not True:
            raise StageBBlock(f"Not eligible after publish: {sid}")

        projected = proj.get_service(sid)
        adapted = adapter.get_service(sid)

        snap = {
            "service_id": latest.service_id,
            "name_ar": latest.name_ar,
            "note_ar": latest.note_ar,
            "location_path": list(latest.location_path or []),
            "fulfillment_mode": latest.fulfillment_mode,
            "target_platform_key": latest.target_platform_key,
            "target_section_key": latest.target_section_key,
            "target_subsection_key": latest.target_subsection_key,
            "provider_slug": latest.provider_slug,
            "provider_account_key": latest.provider_account_key,
            "external_service_id": (
                str(latest.external_service_id)
                if latest.external_service_id is not None
                else None
            ),
            "content_fingerprint": latest.content_fingerprint,
            "published_at": latest.published_at,
            "min_quantity": latest.min_quantity,
            "max_quantity": latest.max_quantity,
            "amount_millimes": latest.amount_millimes,
            "currency": latest.currency,
            "pricing_mode": latest.pricing_mode,
            "service_type": latest.service_type,
            "ordering_mode": latest.ordering_mode,
        }
        for req in (
            "service_id",
            "name_ar",
            "fulfillment_mode",
            "target_platform_key",
            "target_section_key",
            "provider_slug",
            "provider_account_key",
            "external_service_id",
            "content_fingerprint",
            "published_at",
            "amount_millimes",
            "service_type",
        ):
            if snap.get(req) in (None, ""):
                raise StageBBlock(
                    f"Publication snapshot missing {req} for {sid}",
                    {"snap": snap},
                )

        pub_exec = (
            latest.provider_slug,
            latest.provider_account_key,
            str(latest.external_service_id),
        )
        proj_exec = (
            projected.execution.provider_slug,
            projected.execution.provider_account_key,
            str(projected.execution.external_service_id),
        )
        adapt_exec = (
            adapted.execution.provider_slug,
            adapted.execution.provider_account_key,
            str(adapted.execution.external_service_id),
        )
        if not (pub_exec == proj_exec == adapt_exec):
            raise StageBBlock(
                f"Execution identity mismatch for {sid}",
                {"pub": pub_exec, "proj": proj_exec, "adapter": adapt_exec},
            )

        pub_fm = latest.fulfillment_mode
        if not (pub_fm == projected.fulfillment_mode == adapted.fulfillment_mode):
            raise StageBBlock(
                f"Fulfillment mismatch for {sid}",
                {
                    "pub": pub_fm,
                    "proj": projected.fulfillment_mode,
                    "adapter": adapted.fulfillment_mode,
                },
            )

        pub_tgt = (
            latest.target_platform_key,
            latest.target_section_key,
            latest.target_subsection_key or None,
        )
        proj_tgt = (
            projected.target_policy.platform_key,
            projected.target_policy.section_key,
            projected.target_policy.subsection_key or None,
        )
        adapt_tgt = (
            adapted.target_policy.platform_key,
            adapted.target_policy.section_key,
            adapted.target_policy.subsection_key or None,
        )
        if not (pub_tgt == proj_tgt == adapt_tgt):
            raise StageBBlock(
                f"Target policy mismatch for {sid}",
                {"pub": pub_tgt, "proj": proj_tgt, "adapter": adapt_tgt},
            )

        target = _pilot_sample_target(
            c["platform_key"] or latest.target_platform_key,
            c["section_key"] or latest.target_section_key,
            c.get("subsection_key") or latest.target_subsection_key,
        )
        qty = int(latest.min_quantity or 1)
        try:
            intent = adapter.resolve_order_intent(sid, qty, target=target)
        except StorefrontAdapterError as exc:
            raise StageBBlock(
                f"Order intent failed for {sid}: {exc}",
                {
                    "target": target,
                    "qty": qty,
                    "code": getattr(exc, "code", None),
                },
            ) from exc

        intent_exec = (
            intent.provider_slug,
            intent.provider_account_key,
            str(intent.external_service_id),
        )
        if intent_exec != pub_exec:
            raise StageBBlock(
                f"Intent execution mismatch for {sid}",
                {"pub": pub_exec, "intent": intent_exec},
            )
        if intent.fulfillment_mode != pub_fm:
            raise StageBBlock(
                f"Intent fulfillment mismatch for {sid}",
                {"pub": pub_fm, "intent": intent.fulfillment_mode},
            )
        if not intent.target_validation_ok:
            raise StageBBlock(f"Intent target_validation_ok false for {sid}")

        ok_valid, msg = validate_order_target(
            target,
            platform_key=latest.target_platform_key or "",
            section_key=latest.target_section_key,
            subsection_key=latest.target_subsection_key,
            link_prompt_key=latest.target_link_prompt_key,
            link_type=latest.target_link_type,
        )
        ok_invalid, _ = validate_order_target(
            "https://example.com/not-a-platform",
            platform_key=latest.target_platform_key or "",
            section_key=latest.target_section_key,
            subsection_key=latest.target_subsection_key,
            link_prompt_key=latest.target_link_prompt_key,
            link_type=latest.target_link_type,
        )
        target_matrix.append(
            {
                "service_id": sid,
                "platform": latest.target_platform_key,
                "section": latest.target_section_key,
                "valid_target": target,
                "valid_ok": bool(ok_valid),
                "invalid_rejected": not bool(ok_invalid),
            }
        )
        if not ok_valid:
            raise StageBBlock(
                f"Valid target rejected for {sid}",
                {"target": target, "message": msg},
            )
        if ok_invalid:
            raise StageBBlock(f"Invalid target accepted for {sid}")

        parity_rows.append(
            {
                "service_id": sid,
                "legacy_catalog_id": c["legacy_catalog_id"],
                "publication_id": latest.id,
                "fingerprint": latest.content_fingerprint,
                "execution": pub_exec,
                "fulfillment_mode": pub_fm,
                "target_policy": pub_tgt,
                "intent": intent.to_dict(),
                "eligible": True,
                "snapshot": snap,
            }
        )

    intent_helper = validate_pilot_intents(
        conn, [c["soldium_service_id"] for c in candidates]
    )

    first_after = snapshot_first_pilots(conn)
    verify_first_pilots_unchanged(first_pilots_before, first_after)

    for sid in sorted(active_ids):
        latest = pub.get_latest_publish(sid)
        projected = proj.get_service(sid)
        adapted = adapter.get_service(sid)
        if latest is None:
            raise StageBBlock(f"13-set missing publication for {sid}")
        pub_e = (
            latest.provider_slug,
            latest.provider_account_key,
            str(latest.external_service_id),
        )
        if pub_e != (
            projected.execution.provider_slug,
            projected.execution.provider_account_key,
            str(projected.execution.external_service_id),
        ):
            raise StageBBlock(f"13-set exec pub≠proj for {sid}")
        if pub_e != (
            adapted.execution.provider_slug,
            adapted.execution.provider_account_key,
            str(adapted.execution.external_service_id),
        ):
            raise StageBBlock(f"13-set exec pub≠adapter for {sid}")

    shadow = compare_storefronts(conn).to_dict()
    if int(shadow.get("dangerous_count") or 0) != 0:
        raise StageBBlock(
            "Dangerous shadow differences present",
            {"shadow": shadow},
        )
    if int(shadow.get("catalog_count") or 0) != 13:
        raise StageBBlock(
            f"Shadow catalog_count expected 13, got {shadow.get('catalog_count')}",
            {"shadow": shadow},
        )

    counts_after = production_counts(conn)
    if int(counts_after["publications"]) != 23:
        raise StageBBlock(
            f"publications expected 23, got {counts_after['publications']}",
            {"counts": counts_after},
        )

    return {
        "projection_count_after": len(active),
        "parity_rows": parity_rows,
        "intent_rows": [r["intent"] for r in parity_rows],
        "intent_helper_results": intent_helper,
        "target_matrix": target_matrix,
        "first_pilots_after": first_after,
        "shadow": {
            "legacy_count": shadow.get("legacy_count"),
            "catalog_count": shadow.get("catalog_count"),
            "correlated_count": shadow.get("correlated_count"),
            "legacy_only_count": shadow.get("legacy_only_count"),
            "catalog_only_count": shadow.get("catalog_only_count"),
            "dangerous_count": shadow.get("dangerous_count"),
            "review_count": shadow.get("review_count"),
            "changed_count": shadow.get("changed_count"),
            "equal_count": shadow.get("equal_count"),
            "publication_count": shadow.get("publication_count"),
        },
        "production_counts_after": counts_after,
    }


def publish_second_pilot(
    conn: sqlite3.Connection, *, dry_run: bool = False
) -> dict[str, Any]:
    preflight = run_preflight(conn)
    candidates = [
        PilotCandidate(
            soldium_service_id=c["soldium_service_id"],
            legacy_catalog_id=c["legacy_catalog_id"],
            platform_key=c["platform_key"] or "",
            legacy_name_ar="",
            catalog_name_ar="",
            selection_reason="phase9e_stage_a_approved",
        )
        for c in preflight["candidates"]
    ]
    if len(candidates) != 8:
        raise StageBBlock(f"Expected 8 candidates, got {len(candidates)}")

    run = publish_pilot(
        conn,
        candidates,
        published_by=PUBLISHED_BY,
        dry_run=dry_run,
    )
    report: dict[str, Any] = {
        "timestamp": preflight["timestamp"],
        "published_by": PUBLISHED_BY,
        "dry_run": dry_run,
        "approved_legacy_ids": list(APPROVED_LEGACY_IDS),
        "preflight": preflight,
        "publish_status": run.status,
        "publish_message": run.message,
        "publish_results": [r.to_dict() for r in run.results],
        "published_ids": list(run.published_ids),
        "blocked_reasons": list(run.blocked_reasons),
    }
    if dry_run:
        report["post_publish"] = None
        report["verdict"] = "DRY_RUN"
        return report

    if run.status != "COMPLETE" or len(run.published_ids) != 8:
        raise StageBBlock(
            f"Publish incomplete: status={run.status} "
            f"published={len(run.published_ids)}",
            {"report": report},
        )

    if set(run.published_ids) != {
        c["soldium_service_id"] for c in preflight["candidates"]
    }:
        raise StageBBlock("Published ID set mismatch")

    post = verify_post_publish(
        conn, preflight["candidates"], preflight["first_pilots_before"]
    )
    before = preflight["production_counts_before"]
    after = post["production_counts_after"]
    for key in (
        "orders",
        "smm_services",
        "services",
        "nodes",
        "entries",
        "prices",
        "execution_sources",
        "mappings",
    ):
        if before[key] != after[key]:
            raise StageBBlock(
                f"Unexpected production change in {key}",
                {"before": before, "after": after},
            )
    if after["publications"] != before["publications"] + 8:
        raise StageBBlock(
            "Publication row delta is not +8",
            {
                "before": before["publications"],
                "after": after["publications"],
            },
        )

    report["post_publish"] = post
    report["verdict"] = "PHASE 9E STAGE B COMPLETE — SECOND PILOT VERIFIED"
    return report
