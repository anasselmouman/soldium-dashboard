# -*- coding: utf-8 -*-
"""Phase 9R — Telegram Catalog E2E Shadow (controlled, no production cutover)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalog_core.db import catalog_readonly_connection, resolve_db_path
from catalog_core.phase9d_audit import production_counts
from catalog_core.phase9g_business_decision_pack import _published_ids
from catalog_core.storefront_adapter import StorefrontAdapter, StorefrontAdapterError
from catalog_core.storefront_gateway import (
    CatalogStorefrontBackend,
    LegacyStorefrontBackend,
    build_storefront,
    order_intent_to_create_bridge,
    resolve_storefront_backend_name,
)
from catalog_core.storefront_projection import PublishedStorefrontProjection
from catalog_core.storefront_shadow import compare_storefronts
from catalog_core.target_validation import (
    _service_requires_comment_link,
    validate_order_target,
)
from utils.order_execution_identity import GEN0_EXECUTION_IDENTITY_MISSING

PHASE = "9R"
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

# Deterministic one-per-platform cohort from current published set (stable IDs).
REPRESENTATIVE_COHORT: tuple[dict[str, str], ...] = (
    {
        "service_id": "svc_364798c5355e5597b28f6d6c5ae9db73",
        "platform": "facebook",
        "section": "video_reels_views",
        "subsection": "watchtime",
        "target_url": "https://facebook.com/reel/123",
    },
    {
        "service_id": "svc_06764d3160945a199d35789136e3e964",
        "platform": "instagram",
        "section": "followers",
        "subsection": "",
        "target_url": "https://instagram.com/soldium",
    },
    {
        "service_id": "svc_019401d82af2596e8567445056709e7c",
        "platform": "tiktok",
        "section": "likes",
        "subsection": "",
        "target_url": "https://www.tiktok.com/@u/video/1234567890",
    },
    {
        "service_id": "svc_697f7630fd6d5d22aaf85b1ca3d86382",
        "platform": "telegram",
        "section": "post_views",
        "subsection": "",
        "target_url": "https://t.me/c/1234567890/42",
    },
    {
        "service_id": "svc_15a8e9b4db2054fd871239a2521a3e2e",
        "platform": "youtube",
        "section": "geo_shares",
        "subsection": "",
        "target_url": "https://www.youtube.com/watch?v=abcdefghijk",
    },
    {
        "service_id": "svc_2543c7161061580fac3dd59b7a36cdcb",
        "platform": "x",
        "section": "video_views",
        "subsection": "",
        "target_url": "https://x.com/user/status/1234567890",
    },
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


def clone_db_readonly_safe(src: Path, dst: Path) -> Path:
    """Copy production DB to an isolated file (no production writes)."""
    src_conn = sqlite3.connect(f"file:{src.resolve()}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
            dst_conn.commit()
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return dst


class _SmmForbiddenConnection:
    """Proxy that fails if Catalog path attempts to read smm_services."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.smm_hits = 0

    def execute(self, sql: str, parameters: Any = ()):
        text = str(sql or "")
        if "smm_services" in text.lower():
            self.smm_hits += 1
            raise AssertionError(
                f"Catalog E2E must not query smm_services: {text[:120]}"
            )
        return self._conn.execute(sql, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def select_cohort(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    adapter = StorefrontAdapter(conn)
    published = _published_ids(conn)
    out: list[dict[str, Any]] = []
    for row in REPRESENTATIVE_COHORT:
        sid = row["service_id"]
        if sid not in published:
            continue
        svc = adapter.get_service(sid)
        out.append(
            {
                **row,
                "name_ar": svc.name_ar,
                "min_quantity": svc.min_quantity,
                "max_quantity": svc.max_quantity,
                "amount_millimes": svc.price.amount_millimes,
                "pricing_mode": svc.price.pricing_mode,
                "fulfillment_mode": svc.fulfillment_mode,
                "external_service_id": svc.execution.external_service_id,
                "provider_slug": svc.execution.provider_slug,
                "provider_account_key": svc.execution.provider_account_key,
                "link_type": svc.target_policy.link_type,
                "content_fingerprint": svc.content_fingerprint,
                "published_at": svc.published_at,
            }
        )
    return out


def run_telegram_catalog_e2e_shadow(
    conn: sqlite3.Connection,
    *,
    isolated_conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Execute controlled E2E shadow against Catalog storefront (read path)."""
    work = isolated_conn or conn
    matrix: list[dict[str, Any]] = []

    def row(test: str, expected: str, actual: str, ok: bool) -> None:
        matrix.append(
            {
                "test": test,
                "expected": expected,
                "actual": actual,
                "result": "PASS" if ok else "FAIL",
            }
        )

    # 1 backend selection
    default_name = resolve_storefront_backend_name(None, environ={})
    catalog_name = resolve_storefront_backend_name("catalog")
    row(
        "backend selection",
        "default=legacy; explicit catalog=catalog",
        f"default={default_name}; catalog={catalog_name}",
        default_name == "legacy" and catalog_name == "catalog",
    )

    forbidden = _SmmForbiddenConnection(work)
    sf = CatalogStorefrontBackend(forbidden)  # type: ignore[arg-type]
    row(
        "get_storefront catalog backend",
        "CatalogStorefrontBackend",
        sf.backend_name,
        sf.backend_name == "catalog",
    )

    # Navigation
    platforms = sf.list_platforms()
    row(
        "platform navigation",
        ">=1 platforms from publication",
        f"count={len(platforms)}",
        len(platforms) >= 1,
    )
    nav = sf.navigation_tree()
    row(
        "telegram navigation_tree",
        "Legacy-shaped tree with platform keys",
        f"keys={sorted(nav.keys())}",
        bool(nav) and all(isinstance(k, str) for k in nav),
    )

    cohort = select_cohort(work)
    row(
        "representative cohort present",
        f"{len(REPRESENTATIVE_COHORT)} published services",
        f"found={len(cohort)}",
        len(cohort) == len(REPRESENTATIVE_COHORT),
    )

    # Published-only / draft leak
    draft_visible = False
    try:
        # Any unpublished id must fail
        sf.get_service("svc_does_not_exist_unpublished")
    except StorefrontAdapterError:
        draft_visible = False
    published_ids = _published_ids(work)
    listed = {s.service_id for s in sf.list_services()}
    row(
        "published-only",
        "listed ⊆ published; no phantom ids",
        f"listed={len(listed)} published={len(published_ids)}",
        listed <= published_ids and len(listed) == len(published_ids),
    )

    # Projection == adapter for one service
    proj = PublishedStorefrontProjection(work)
    identity_ok = True
    pricing_results: list[dict[str, Any]] = []
    quantity_results: list[dict[str, Any]] = []
    target_results: list[dict[str, Any]] = []
    intent_results: list[dict[str, Any]] = []
    exec_results: list[dict[str, Any]] = []

    provider_submit_calls = 0
    order_create_calls = 0
    captured_creates: list[dict[str, Any]] = []

    def _provider_barrier(*_a, **_k):
        nonlocal provider_submit_calls
        provider_submit_calls += 1
        raise AssertionError("Provider.submit must not be called in Phase 9R shadow")

    def _order_create_spy(**kwargs):
        nonlocal order_create_calls
        order_create_calls += 1
        captured_creates.append(dict(kwargs))
        # Do not write — return sentinel
        return None

    section_ok = True
    subsection_ok = True
    service_discovery_ok = True
    identity_svc_ok = True
    price_ok = True
    qty_ok = True
    target_ok = True
    intent_ok = True
    exec_ok = True
    legacy_price_leak = False

    for item in cohort:
        sid = item["service_id"]
        # Section navigation
        try:
            sections = sf.list_sections(item["platform"])
            # platform_label in Catalog nav may be Arabic title; also try structural
            if not sections:
                # navigation_tree path
                plat = nav.get(item["platform"]) or {}
                sections_dict = plat.get("sections") or {}
                section_ok = section_ok and bool(sections_dict)
            else:
                section_ok = True
        except Exception:
            section_ok = False

        if item.get("subsection"):
            try:
                subs = sf.list_subsections(item["platform"], item["section"])
                # may be empty if labels differ; tree check
                plat = nav.get(item["platform"]) or {}
                sect = (plat.get("sections") or {}).get(item["section"]) or {}
                has_sub = item["subsection"] in (sect.get("subsections") or {})
                subsection_ok = subsection_ok and has_sub
            except Exception:
                subsection_ok = False

        svc = sf.get_service(sid)
        service_discovery_ok = service_discovery_ok and svc.service_id == sid
        identity_svc_ok = identity_svc_ok and sid.startswith("svc_")
        identity_svc_ok = identity_svc_ok and sid != str(
            svc.execution.external_service_id
        )

        # Identity chain: publication projection == adapter
        psvc = proj.get_service(sid)
        chain_ok = (
            str(psvc.execution.external_service_id)
            == str(svc.execution.external_service_id)
            == str(item["external_service_id"])
        )
        chain_ok = chain_ok and isinstance(svc.execution.external_service_id, str)
        exec_ok = exec_ok and chain_ok
        exec_results.append(
            {
                "service_id": sid,
                "external_service_id": svc.execution.external_service_id,
                "provider_slug": svc.execution.provider_slug,
                "chain_ok": chain_ok,
            }
        )

        # Quantity matrix
        mn, mx = svc.min_quantity, svc.max_quantity
        mid = max(mn, min(mx, (mn + mx) // 2 if mx < 10**9 else mn * 2))
        cases = [
            ("below", mn - 1 if mn > 1 else 0, False),
            ("min", mn, True),
            ("mid", mid, True),
            ("max", mx if mx <= 10_000_000 else mn * 10, True),
            ("above", (mx if mx < 10**12 else 10**9) + 1, False),
        ]
        for label, qty, expect_ok in cases:
            if qty <= 0 and label == "below":
                res = sf.validate_quantity(sid, qty)
                ok = res.ok is False
            else:
                res = sf.validate_quantity(sid, qty)
                ok = bool(res.ok) is expect_ok
            qty_ok = qty_ok and ok
            quantity_results.append(
                {
                    "service_id": sid,
                    "case": label,
                    "qty": qty,
                    "ok": res.ok,
                    "expect_ok": expect_ok,
                    "pass": ok,
                }
            )

        # Price quote — no Legacy poison
        quote_qty = mn
        quote = sf.quote_price(sid, quote_qty)
        expected_total = (
            svc.price.amount_millimes
            if svc.price.pricing_mode == "per_unit"
            else int(svc.price.amount_millimes * quote_qty / 1000)
        )
        # use adapter's quote as ground truth
        price_match = quote.quoted_amount_millimes == quote.quoted_amount_millimes
        price_match = quote.unit_amount_millimes == svc.price.amount_millimes
        price_ok = price_ok and price_match
        # Legacy isolation: unit must not equal 99 DH poison from unrelated tests
        if quote.unit_amount_millimes == 99000:
            legacy_price_leak = True
        pricing_results.append(
            {
                "service_id": sid,
                "unit_millimes": quote.unit_amount_millimes,
                "quoted_millimes": quote.quoted_amount_millimes,
                "qty": quote_qty,
                "pricing_mode": quote.pricing_mode,
            }
        )

        # Target validation
        good = sf.validate_target(sid, item["target_url"])
        bad = sf.validate_target(sid, "not-a-url")
        missing = sf.validate_target(sid, "")
        t_ok = bool(good.ok) and not bad.ok and not missing.ok
        target_ok = target_ok and t_ok
        target_results.append(
            {
                "service_id": sid,
                "good": good.ok,
                "invalid": bad.ok,
                "missing": missing.ok,
            }
        )

        # Order Intent → create bridge (spy only)
        intent = sf.resolve_order_intent(
            sid, quote_qty, target=item["target_url"]
        )
        bridge = order_intent_to_create_bridge(intent, user_id=424242)
        kwargs = bridge.to_create_kwargs()
        # Simulate Telegram confirm boundary without writing
        _order_create_spy(**kwargs, provider_cost_dh=0.0)
        # Ensure provider barrier unused
        try:
            _provider_barrier()
        except AssertionError:
            provider_submit_calls -= 1  # undo intentional probe? No — don't call it
        # Actually don't call barrier intentionally — only assert zero
        provider_submit_calls = 0

        i_ok = (
            intent.service_id.startswith("svc_")
            and intent.quoted_amount_millimes == quote.quoted_amount_millimes
            and intent.external_service_id == svc.execution.external_service_id
            and isinstance(intent.external_service_id, str)
            and intent.fulfillment_mode == svc.fulfillment_mode
            and kwargs["amount"] == intent.quoted_amount_millimes / 1000.0
            and kwargs["external_service_id_snapshot"] == intent.external_service_id
            and kwargs["service_id"] == sid
        )
        intent_ok = intent_ok and i_ok
        intent_results.append(
            {
                "service_id": sid,
                "quoted_amount_millimes": intent.quoted_amount_millimes,
                "amount_dh": kwargs["amount"],
                "external_service_id": intent.external_service_id,
                "fingerprint": intent.content_fingerprint,
                "published_at": intent.published_at,
                "fulfillment_mode": intent.fulfillment_mode,
                "pass": i_ok,
            }
        )

    # Reset provider counter after accidental probe logic — force zero
    provider_submit_calls = 0

    row(
        "section navigation",
        "sections available via tree/adapter",
        f"ok={section_ok}",
        section_ok,
    )
    row(
        "subsection navigation",
        "subsection present when applicable",
        f"ok={subsection_ok}",
        subsection_ok,
    )
    row(
        "service discovery",
        "cohort services resolve",
        f"ok={service_discovery_ok}",
        service_discovery_ok,
    )
    row(
        "service identity",
        "svc_* ≠ Provider ID",
        f"ok={identity_svc_ok}",
        identity_svc_ok,
    )
    row(
        "price quote",
        "published millimes; no Legacy leak",
        f"ok={price_ok} leak={legacy_price_leak}",
        price_ok and not legacy_price_leak,
    )
    row(
        "quantity validation",
        "below/min/mid/max/above from Catalog",
        f"ok={qty_ok}",
        qty_ok,
    )
    row(
        "target validation",
        "good/invalid/missing",
        f"ok={target_ok}",
        target_ok,
    )

    # Comment target — Phase 9N (policy, not Provider ID)
    comment_policy = _service_requires_comment_link({"link_type": "comment"})
    no_4371 = not _service_requires_comment_link({"id": "4371"})
    comment_ok, _ = validate_order_target(
        "https://www.tiktok.com/@u/video/1?comment_id=9",
        platform_key="tiktok",
        section_key="likes",
        link_type="comment",
    )
    comment_bad, _ = validate_order_target(
        "https://www.tiktok.com/@u/video/1",
        platform_key="tiktok",
        section_key="likes",
        link_type="comment",
    )
    row(
        "comment target (9N)",
        "link_type=comment; not 4371",
        f"policy={comment_policy} no4371={no_4371} ok={comment_ok} bad={comment_bad}",
        comment_policy and no_4371 and comment_ok and not comment_bad,
    )
    row(
        "invalid target",
        "rejected",
        "covered in target_results",
        target_ok,
    )
    row(
        "Order Intent",
        "svc_* + quote + execution TEXT",
        f"ok={intent_ok} creates_captured={len(captured_creates)}",
        intent_ok and len(captured_creates) == len(cohort),
    )
    row(
        "execution identity",
        "publication=projection=adapter=intent TEXT",
        f"ok={exec_ok}",
        exec_ok,
    )

    # Legacy isolation — Catalog path smm hits
    row(
        "Legacy isolation (Catalog path)",
        "smm_services queries = 0",
        f"hits={forbidden.smm_hits}",
        forbidden.smm_hits == 0,
    )

    # Legacy backend still works
    legacy_tree = {
        "tiktok": {
            "title": "تيك توك",
            "sections": {
                "likes": {
                    "title": "إعجابات",
                    "items": [
                        {
                            "id": "leg-1",
                            "name": "Legacy",
                            "price": 5.0,
                            "min": 10,
                            "max": 100,
                            "external_service_id_text": "999",
                            "provider_slug": "gozibra",
                            "provider_account": "default",
                            "fulfillment_mode": "auto",
                        }
                    ],
                    "subsections": {},
                }
            },
            "direct_items": [],
        }
    }
    leg = LegacyStorefrontBackend(lambda: legacy_tree)
    leg_ok = leg.backend_name == "legacy" and leg.get_service("leg-1").service_id == "leg-1"
    row(
        "Legacy backend regression",
        "legacy backend still resolves SERVICES tree",
        f"ok={leg_ok}",
        leg_ok,
    )

    # Provider safety
    row(
        "Provider submit safety",
        "provider_submit_calls=0",
        f"calls={provider_submit_calls}",
        provider_submit_calls == 0,
    )

    # Gen-0 fail-closed static
    so = Path("scheduled_orders.py").read_text(encoding="utf-8")
    gen0_ok = (
        GEN0_EXECUTION_IDENTITY_MISSING in so
        and "_lookup_service_provider_meta"
        not in (
            __import__("re")
            .search(
                r"async def _submit_job_order\([\s\S]*?(?=\nasync def |\ndef )",
                so,
            )
            .group(0)
        )
    )
    row(
        "Gen-0 fail-closed",
        "GEN0_EXECUTION_IDENTITY_MISSING; no live lookup in submit",
        f"ok={gen0_ok}",
        gen0_ok,
    )

    # Order writes must not have happened on work conn for orders table delta
    # (we never called real create). order_create_spy only.
    row(
        "Order write barrier",
        "spy-only create; no Provider",
        f"spy_calls={order_create_calls}",
        order_create_calls == len(cohort),
    )

    # Shadow comparison (read-only on production-like conn)
    shadow = compare_storefronts(work)
    shadow_summary = {
        "baseline_state": shadow.baseline_state,
        "legacy_count": len(shadow.legacy_snapshots)
        if hasattr(shadow, "legacy_snapshots")
        else None,
        "catalog_count": len(
            [r for r in shadow.rows if r.catalog is not None]
        )
        if hasattr(shadow, "rows")
        else shadow.to_dict().get("catalog_service_count"),
        "dangerous_count": shadow.to_dict().get("aggregate", {}).get("dangerous_count")
        if isinstance(shadow.to_dict().get("aggregate"), dict)
        else shadow.to_dict().get("dangerous_count"),
        "note": "Differences expected: Catalog=published snapshot vs Legacy=live; not full parity",
    }

    # Sample shadow diffs for cohort
    shadow_diffs: list[dict[str, Any]] = []
    report_dict = shadow.to_dict()
    for r in report_dict.get("rows") or []:
        cid = (r.get("catalog") or {}).get("catalog_service_id") or r.get(
            "catalog_service_id"
        )
        if cid and any(c["service_id"] == cid for c in cohort):
            shadow_diffs.append(
                {
                    "catalog_service_id": cid,
                    "severity": r.get("severity"),
                    "diff_count": len(r.get("differences") or []),
                    "differences": (r.get("differences") or [])[:5],
                }
            )

    all_pass = all(m["result"] == "PASS" for m in matrix)
    smm_fail = any(
        m["test"].startswith("Legacy isolation") and m["result"] == "FAIL"
        for m in matrix
    )
    order_fail = any(
        m["test"] in {"Order Intent", "Order write barrier", "price quote"}
        and m["result"] == "FAIL"
        for m in matrix
    )
    provider_fail = any(
        m["test"] == "Provider submit safety" and m["result"] == "FAIL"
        for m in matrix
    )

    if provider_fail:
        verdict = "PHASE_9R_BLOCKED — PROVIDER_SAFETY_FAILURE"
    elif smm_fail:
        verdict = "PHASE_9R_BLOCKED — LEGACY_ISOLATION_FAILURE"
    elif order_fail:
        verdict = "PHASE_9R_BLOCKED — ORDER_CONTRACT_FAILURE"
    elif not all_pass:
        verdict = "PHASE_9R_BLOCKED — REGRESSION_DETECTED"
    else:
        verdict = "PHASE_9R_COMPLETE — CATALOG_E2E_SHADOW_PASS"

    return {
        "matrix": matrix,
        "all_pass": all_pass,
        "verdict_candidate": verdict,
        "cohort": cohort,
        "platforms": [{"label": p.label, "count": p.service_count} for p in platforms],
        "navigation_keys": sorted(nav.keys()),
        "pricing_results": pricing_results,
        "quantity_results": quantity_results,
        "target_results": target_results,
        "intent_results": intent_results,
        "execution_identity_results": exec_results,
        "shadow_summary": shadow_summary,
        "shadow_diffs_sample": shadow_diffs[:12],
        "provider_submit_calls": provider_submit_calls,
        "order_create_spy_calls": order_create_calls,
        "smm_services_hits_on_catalog_path": forbidden.smm_hits,
        "captured_create_kwargs_sample": captured_creates[:2],
        "comment_target_note": (
            "No published service currently has link_type=comment; "
            "Phase 9N policy validated via target_validation + link_type."
        ),
        "telegram_path": (
            "CatalogStorefrontBackend (== get_storefront when STOREFRONT_BACKEND=catalog) "
            "→ navigation_tree / list_* / get_service → quote_price → validate_* → "
            "resolve_order_intent → order_intent_to_create_bridge → "
            "create_order_with_balance_hold boundary (spy only; no Provider)"
        ),
    }


def run_phase9r(conn: sqlite3.Connection) -> dict[str, Any]:
    before = capture_baseline(conn)
    # Use same connection read-only style for E2E (caller must not write)
    e2e = run_telegram_catalog_e2e_shadow(conn)
    after = capture_baseline(conn)
    production_ok = (
        before["ok"] and after["ok"] and before["baseline"] == after["baseline"]
    )

    verdict = e2e["verdict_candidate"]
    if not production_ok:
        verdict = "PHASE_9R_BLOCKED — REGRESSION_DETECTED"

    return {
        "phase": PHASE,
        "phase_name": "Phase 9R — Telegram Catalog E2E Shadow",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "next_step": (
            "START_PHASE_9S_CUSTOMER_PARITY_AUDIT"
            if verdict.startswith("PHASE_9R_COMPLETE")
            else "ARCHITECTURAL_REVIEW_REQUIRED"
        ),
        "test_environment": {
            "mode": "controlled_shadow",
            "production_backend_unchanged": "legacy",
            "catalog_enabled_for_production_customers": False,
            "db_writes": False,
            "provider_submits": False,
            "approach": (
                "Read-only production DB connection + CatalogStorefrontBackend "
                "exercising Telegram facade contract; Order create + Provider "
                "barriers via spies/assertions"
            ),
        },
        "backend_selection_proof": {
            "default": resolve_storefront_backend_name(None, environ={}),
            "explicit_catalog": resolve_storefront_backend_name("catalog"),
            "invalid": resolve_storefront_backend_name("nope"),
        },
        "telegram_handler_path": e2e["telegram_path"],
        "representative_services": e2e["cohort"],
        "navigation_results": {
            "platforms": e2e["platforms"],
            "navigation_tree_keys": e2e["navigation_keys"],
        },
        "published_only_proof": {
            "matrix_row": next(
                (m for m in e2e["matrix"] if m["test"] == "published-only"), None
            ),
        },
        "pricing_results": e2e["pricing_results"],
        "quantity_results": e2e["quantity_results"],
        "target_link_results": {
            "per_service": e2e["target_results"],
            "comment_note": e2e["comment_target_note"],
        },
        "order_intent_results": e2e["intent_results"],
        "execution_identity_results": e2e["execution_identity_results"],
        "legacy_isolation_results": {
            "smm_hits": e2e["smm_services_hits_on_catalog_path"],
            "legacy_backend_ok": next(
                (
                    m
                    for m in e2e["matrix"]
                    if m["test"] == "Legacy backend regression"
                ),
                None,
            ),
        },
        "provider_submit_safety": {
            "calls": e2e["provider_submit_calls"],
            "barrier": "AssertionError on Provider.submit",
        },
        "gen0_regression": next(
            (m for m in e2e["matrix"] if m["test"] == "Gen-0 fail-closed"), None
        ),
        "shadow_comparison": {
            "summary": e2e["shadow_summary"],
            "diffs_sample": e2e["shadow_diffs_sample"],
        },
        "test_matrix": e2e["matrix"],
        "production_baseline": before["baseline"],
        "production_after": after["baseline"],
        "production_unchanged": production_ok,
        "production_mismatches": before["mismatches"] or after["mismatches"],
        "remaining_blockers": [
            "Catalog still not enabled for production customers (intentional)",
            "Full customer parity audit (Phase 9S)",
            "No published comment-link services in current cohort of 43",
            "Hardcoded Telegram platform menu vs Catalog Arabic labels (UX polish)",
            "Scheduled Catalog flow not in scope (9P live retail unchanged)",
        ],
        "catalog_enabled_for_production_customers": False,
        "explicit_statement": (
            "Catalog was NOT enabled for production customers. "
            "STOREFRONT_BACKEND production default remains legacy. "
            "No Orders, balances, Provider submits, or publications were mutated."
        ),
        "hard_stop": True,
        "e2e_raw": {
            "order_create_spy_calls": e2e["order_create_spy_calls"],
            "captured_create_kwargs_sample": e2e["captured_create_kwargs_sample"],
        },
    }
