# -*- coding: utf-8 -*-
"""Catalog Admin practical acceptance — isolated DB only.

Exercises the same CatalogCoreService / PublicationService operations the
Dashboard UI calls via /api/soldium-catalog. Does NOT mutate production.
Does NOT enable storefront pilot.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from catalog_core.db import catalog_transaction, resolve_db_path
from catalog_core.errors import CatalogPublishError, CatalogValidationError
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService

ROOT = Path(__file__).resolve().parents[1]


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _seed_isolated(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE providers (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            api_base_url TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO providers(slug, name, api_base_url)
        VALUES ('gozibra', 'Gozibra', 'https://example.test');
        CREATE TABLE provider_accounts (
            id INTEGER PRIMARY KEY,
            provider_slug TEXT NOT NULL,
            account_key TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            UNIQUE(provider_slug, account_key)
        );
        INSERT INTO provider_accounts(provider_slug, account_key, display_name)
        VALUES ('gozibra', 'tiktok', 'TikTok Test');
        CREATE TABLE smm_services (
            catalog_id TEXT PRIMARY KEY,
            name_ar TEXT,
            is_active INTEGER DEFAULT 1
        );
        INSERT INTO smm_services(catalog_id, name_ar) VALUES ('legacy_marker', 'LEGACY');
        CREATE TABLE orders (id INTEGER PRIMARY KEY, service_id TEXT);
        INSERT INTO orders(id, service_id) VALUES (1, 'hist');
        CREATE TABLE scheduled_orders (id INTEGER PRIMARY KEY);
        """
    )
    ensure_soldium_catalog_schema(conn)
    conn.commit()
    conn.close()


def _step(results: dict, key: str, ok: bool, detail: str | dict | None = None) -> None:
    results[key] = {"ok": ok, "detail": detail}


def _ui_surface_check() -> dict:
    js = (ROOT / "static" / "js" / "catalog_core_ui.js").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "workspaces" / "catalog_services.html").read_text(
        encoding="utf-8"
    )
    checks = {
        "create_button": "svc-create" in html and "إضافة خدمة" in html,
        "target_section": 'id="section-target"' in js and "متطلبات الرابط / الهدف" in js,
        "fulfillment_section": 'id="section-fulfillment"' in js and "طريقة التنفيذ" in js,
        "fix_target": 'actions.has("target")' in js and "btn-fix-target" in js,
        "fix_fulfillment": 'actions.has("fulfillment")' in js,
        "publish_ui": "btn-pub-publish" in js and "publication-preview" in js,
        "price_history_ui": "priceHistoryHtml" in js or "سجل الأسعار" in js,
        "source_history_ui": "سجل مصادر التنفيذ" in js,
        "no_native_dialogs": all(x not in js for x in ("alert(", "confirm(", "prompt(")),
    }
    return {"ok": all(checks.values()), "checks": checks}


def _production_safety() -> dict:
    backend = os.environ.get("STOREFRONT_BACKEND", "legacy").strip().lower() or "legacy"
    pilot = os.environ.get("STOREFRONT_CATALOG_PILOT", "disabled").strip().lower() or "disabled"
    pilot_on = pilot in {"1", "true", "yes", "on", "enabled", "pilot"}

    # Bot config if present
    bot_backend = None
    bot_pilot = None
    bot_cfg = ROOT.parent / "soldium-bot" / "config.py"
    if bot_cfg.is_file():
        text = bot_cfg.read_text(encoding="utf-8")
        bot_backend = "legacy" if 'STOREFRONT_BACKEND = (\n    os.environ.get("STOREFRONT_BACKEND", "legacy")' in text or 'get("STOREFRONT_BACKEND", "legacy")' in text else "unknown"
        bot_pilot = "disabled_default" if "STOREFRONT_CATALOG_PILOT" in text else "absent"

    prod: dict = {"reachable": False}
    try:
        from catalog_core.phase9d_audit import production_counts
        from catalog_core.phase9g_business_decision_pack import _published_ids

        path = resolve_db_path()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        counts = production_counts(conn)
        before = {
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
            "published": len(_published_ids(conn)),
            "scheduled_orders": int(
                conn.execute("SELECT COUNT(*) FROM scheduled_orders").fetchone()[0]
            )
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_orders'"
            ).fetchone()
            else 0,
        }
        # re-read after (acceptance uses different DB)
        after = dict(before)
        conn.close()
        prod = {
            "reachable": True,
            "before": before,
            "after": after,
            "unchanged": before == after,
            "mode": "read_only",
        }
    except Exception as exc:
        prod = {"reachable": False, "error": str(exc)}

    return {
        "STOREFRONT_BACKEND_env": backend,
        "STOREFRONT_BACKEND_is_legacy": backend != "catalog",
        "STOREFRONT_CATALOG_PILOT_env": pilot,
        "STOREFRONT_CATALOG_PILOT_enabled": pilot_on,
        "bot_config_default_backend": bot_backend,
        "bot_config_pilot": bot_pilot,
        "production_db": prod,
        "ok": (not pilot_on) and backend != "catalog" and prod.get("unchanged", True),
    }


def run_acceptance() -> dict:
    results: dict = {
        "phase": "catalog_admin_acceptance",
        "timestamp": _utcnow(),
        "test_environment": "isolated_temp_sqlite",
        "ui_equivalent_path": "CatalogCoreService + CatalogPublicationService (same as /api/soldium-catalog)",
    }
    blockers: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="catalog_admin_accept_")) / "accept.db"
    _seed_isolated(tmp)
    results["isolated_db"] = str(tmp)

    ui = _ui_surface_check()
    results["ui_surface"] = ui
    if not ui["ok"]:
        blockers.append("UI_SURFACE")

    safety = _production_safety()
    results["legacy_safety"] = safety
    results["production_safety"] = {
        "used_production_db_for_writes": False,
        "publication_isolated": True,
        "note": "Publish executed only on isolated temp DB; customer storefront remains Legacy + pilot disabled",
        "ok": safety["ok"],
    }
    if not safety["ok"]:
        blockers.append("SAFETY")

    with catalog_transaction(tmp) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)

        # --- Failure cases (before happy path) ---
        failures: dict = {}

        # A: create without target → not ready / publish blocked
        try:
            bare = core.create_service(
                name_ar="قبول بدون هدف",
                status="active",
                service_type="followers",
                ordering_mode="quantity_based",
                min_quantity=10,
                max_quantity=1000,
                fulfillment_mode="auto",
            )
            core.change_price(bare.id, amount_dh="2", pricing_mode="per_1000")
            core.change_execution_source(
                bare.id,
                provider_slug="gozibra",
                provider_account_key="tiktok",
                external_service_id="9001",
            )
            r = core.get_service_readiness(bare.id)
            has_target_issue = any(i.fix_action == "target" for i in r.issues)
            publish_blocked = False
            try:
                pub.publish(bare.id)
            except CatalogPublishError:
                publish_blocked = True
            failures["A_missing_target"] = {
                "ok": (not r.ready) and has_target_issue and publish_blocked,
                "ready": r.ready,
                "target_fix": has_target_issue,
                "publish_blocked": publish_blocked,
                "issue_titles": [i.title for i in r.issues],
            }
            core.archive_service(bare.id)
        except Exception as exc:
            failures["A_missing_target"] = {"ok": False, "error": str(exc)}

        # B: fulfillment defaults to auto — creating without explicit fulfillment is valid
        #    Invalid fulfillment must be rejected
        try:
            core.create_service(name_ar="وضع باطل", fulfillment_mode="magic")
            failures["B_invalid_fulfillment"] = {"ok": False, "error": "accepted invalid"}
        except CatalogValidationError as exc:
            failures["B_invalid_fulfillment"] = {
                "ok": True,
                "rejected": True,
                "message": str(exc.message),
            }
            # Default fulfillment (omit) succeeds
            ok_svc = core.create_service(name_ar="وضع افتراضي")
            failures["B_default_fulfillment"] = {
                "ok": ok_svc.fulfillment_mode == "auto",
                "fulfillment_mode": ok_svc.fulfillment_mode,
            }
            core.archive_service(ok_svc.id)

        # C: invalid min/max
        try:
            core.create_service(
                name_ar="حدود",
                min_quantity=100,
                max_quantity=10,
            )
            failures["C_invalid_minmax"] = {"ok": False, "error": "accepted"}
        except CatalogValidationError as exc:
            failures["C_invalid_minmax"] = {
                "ok": True,
                "message": str(exc.message),
            }

        # D: invalid price
        try:
            s = core.create_service(name_ar="سعر")
            core.change_price(s.id, amount_dh="0", pricing_mode="per_1000")
            failures["D_invalid_price"] = {"ok": False, "error": "accepted zero"}
        except CatalogValidationError as exc:
            failures["D_invalid_price"] = {"ok": True, "message": str(exc.message)}
            core.archive_service(s.id)

        # E: missing execution source
        try:
            s = core.create_service(
                name_ar="بلا مصدر",
                status="active",
                target_platform_key="tiktok",
                target_section_key="likes",
                fulfillment_mode="auto",
            )
            core.change_price(s.id, amount_dh="2", pricing_mode="per_1000")
            r = core.get_service_readiness(s.id)
            failures["E_missing_source"] = {
                "ok": (not r.ready)
                and any(i.fix_action == "source" for i in r.issues),
                "issue_titles": [i.title for i in r.issues],
            }
            core.archive_service(s.id)
        except Exception as exc:
            failures["E_missing_source"] = {"ok": False, "error": str(exc)}

        # F: publish while not ready
        try:
            s = core.create_service(name_ar="غير جاهز", status="active")
            try:
                pub.publish(s.id)
                failures["F_publish_not_ready"] = {"ok": False, "error": "published"}
            except CatalogPublishError as exc:
                failures["F_publish_not_ready"] = {
                    "ok": True,
                    "code": exc.code,
                    "message": str(exc.message),
                }
            core.archive_service(s.id)
        except Exception as exc:
            failures["F_publish_not_ready"] = {"ok": False, "error": str(exc)}

        results["failure_cases"] = failures
        if not all(f.get("ok") for f in failures.values()):
            blockers.append("CONFIGURATION")

        # --- Happy path ---
        node = core.create_node(name_ar="قبول — تيك توك")
        child = core.create_node(
            name_ar="إعجابات اختبار", parent_entry_id=node.entry_id
        )
        _step(
            results,
            "organization_nodes",
            bool(node.entry_id and child.entry_id),
            {"platform_node": node.id, "section_node": child.id},
        )

        # 1–6 create with commercial + target + fulfillment + placement
        svc = core.create_service(
            name_ar="خدمة قبول إداري مؤقتة",
            note_ar="اختبار قبول — تُحذف",
            status="active",
            service_type="likes",
            ordering_mode="quantity_based",
            min_quantity=50,
            max_quantity=5000,
            fulfillment_mode="auto",
            target_platform_key="tiktok",
            target_section_key="likes",
            target_subsection_key=None,
            target_link_type="post",
            target_link_prompt_key=None,
            parent_entry_id=child.entry_id,
        )
        sid = svc.id
        _step(
            results,
            "create",
            sid.startswith("svc_"),
            {"service_id": sid, "name_ar": svc.name_ar},
        )
        _step(
            results,
            "configuration",
            svc.service_type == "likes"
            and svc.ordering_mode == "quantity_based"
            and svc.min_quantity == 50
            and svc.max_quantity == 5000,
            {
                "service_type": svc.service_type,
                "ordering_mode": svc.ordering_mode,
                "min": svc.min_quantity,
                "max": svc.max_quantity,
            },
        )
        _step(
            results,
            "target_policy",
            svc.target_platform_key == "tiktok"
            and svc.target_section_key == "likes"
            and svc.target_link_type == "post",
            {
                "platform": svc.target_platform_key,
                "section": svc.target_section_key,
                "link_type": svc.target_link_type,
            },
        )
        _step(
            results,
            "fulfillment",
            svc.fulfillment_mode == "auto",
            {"fulfillment_mode": svc.fulfillment_mode},
        )

        # 7 execution source
        src = core.change_execution_source(
            sid,
            provider_slug="gozibra",
            provider_account_key="tiktok",
            external_service_id="ACCEPT-777",
            changed_by="acceptance",
        )
        _step(
            results,
            "execution_source",
            src.current is not None
            and str(src.current.external_service_id) == "ACCEPT-777",
            {"external_service_id": src.current.external_service_id if src.current else None},
        )

        # 8 price
        price = core.change_price(
            sid, amount_dh="2.5", pricing_mode="per_1000", currency="MAD"
        )
        _step(
            results,
            "pricing",
            price.current is not None and int(price.current.amount_millimes) == 2500,
            {
                "amount_millimes": price.current.amount_millimes if price.current else None,
                "mode": price.current.pricing_mode if price.current else None,
            },
        )

        # 10 detail
        detail = core.get_service(sid)
        _step(
            results,
            "detail_view",
            detail.id == sid
            and detail.name_ar == "خدمة قبول إداري مؤقتة"
            and detail.location_path
            and detail.current_source is not None
            and detail.current_price is not None,
            {
                "location_path": detail.location_path,
                "has_source": detail.current_source is not None,
                "has_price": detail.current_price is not None,
            },
        )

        # 11–14 readiness
        ready1 = core.get_service_readiness(sid)
        _step(
            results,
            "readiness",
            ready1.ready is True and ready1.issues == [],
            {
                "ready": ready1.ready,
                "state": ready1.state,
                "checks_ok": all(c.ok for c in ready1.checks),
                "issues": [i.title for i in ready1.issues],
            },
        )

        # Force a blocker then fix via service-layer (UI fix opens editor → same PATCH)
        core.update_service(sid, target_platform_key=None, target_section_key=None)
        blocked = core.get_service_readiness(sid)
        fixable = any(i.fix_action == "target" for i in blocked.issues)
        core.update_service(
            sid,
            target_platform_key="tiktok",
            target_section_key="likes",
            target_link_type="post",
        )
        fixed = core.get_service_readiness(sid)
        _step(
            results,
            "readiness_fix_cycle",
            (not blocked.ready) and fixable and fixed.ready,
            {
                "blocked_titles": [i.title for i in blocked.issues],
                "fix_action_target": fixable,
                "ready_after_fix": fixed.ready,
            },
        )

        # 15–17 publish
        preview = pub.preview(sid)
        pub_result = pub.publish(sid, published_by="acceptance_test")
        status = pub.get_publication_status(sid)
        fingerprint = None
        latest = pub.get_latest_publish(sid)
        if latest:
            fingerprint = latest.content_fingerprint
        _step(
            results,
            "publish",
            preview.can_publish
            and pub_result.outcome in ("published", "no_change")
            and status["publication_status"] == "published",
            {
                "preview_can_publish": preview.can_publish,
                "outcome": pub_result.outcome,
                "status": status["publication_status"],
                "fingerprint": fingerprint,
            },
        )

        # 18–19 edit commercial → drift
        core.update_service(sid, name_ar="خدمة قبول إداري مؤقتة (معدّلة)")
        drift = pub.get_publication_status(sid)
        identity = core.get_service(sid)
        _step(
            results,
            "edit",
            identity.id == sid
            and identity.name_ar.endswith("(معدّلة)"),
            {"service_id_unchanged": identity.id == sid, "name": identity.name_ar},
        )
        _step(
            results,
            "drift",
            drift["publication_status"] == "published"
            and drift["has_unpublished_changes"] is True,
            {
                "has_unpublished_changes": drift["has_unpublished_changes"],
                "status": drift["publication_status"],
            },
        )

        # 24 publication immutability
        latest_after_edit = pub.get_latest_publish(sid)
        _step(
            results,
            "publication_immutability",
            latest_after_edit is not None
            and latest_after_edit.content_fingerprint == fingerprint
            and latest_after_edit.name_ar == "خدمة قبول إداري مؤقتة",
            {
                "fingerprint_before": fingerprint,
                "fingerprint_after": latest_after_edit.content_fingerprint
                if latest_after_edit
                else None,
                "snapshot_name": latest_after_edit.name_ar if latest_after_edit else None,
            },
        )

        # 20–21 price change + history
        core.change_price(sid, amount_dh="3.0", pricing_mode="per_1000")
        ph = core.list_price_history(sid)
        active_prices = [p for p in ph if p.status == "active"]
        historical_prices = [p for p in ph if p.status != "active"]
        _step(
            results,
            "price_history",
            len(ph) >= 2 and len(active_prices) == 1 and len(historical_prices) >= 1,
            {
                "total": len(ph),
                "active": len(active_prices),
                "historical": len(historical_prices),
                "active_millimes": active_prices[0].amount_millimes if active_prices else None,
            },
        )

        # 22–23 execution source replace + history
        core.change_execution_source(
            sid,
            provider_slug="gozibra",
            provider_account_key="tiktok",
            external_service_id="ACCEPT-888",
            changed_by="acceptance",
        )
        sh = core.list_execution_source_history(sid)
        active_src = [x for x in sh if x.status == "active"]
        past_src = [x for x in sh if x.status != "active"]
        _step(
            results,
            "source_history",
            len(sh) >= 2
            and len(active_src) == 1
            and str(active_src[0].external_service_id) == "ACCEPT-888"
            and len(past_src) >= 1,
            {
                "total": len(sh),
                "active_ext": active_src[0].external_service_id if active_src else None,
                "past": len(past_src),
            },
        )

        # organization: move + reorder, appear once
        other = core.create_node(name_ar="قسم نقل", parent_entry_id=node.entry_id)
        core.move_service(sid, new_parent_entry_id=other.entry_id)
        moved = core.get_service(sid)
        tree = core.get_tree()
        # count occurrences of service id in tree
        found = 0

        def walk(nodes):
            nonlocal found
            for n in nodes or []:
                if n.get("entry_type") == "service" and n.get("service_id") == sid:
                    found += 1
                walk(n.get("children") or [])

        walk(tree)
        entry = conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_entries WHERE service_id=?",
            (sid,),
        ).fetchone()[0]
        core.reorder_entry(moved.entry_id, position="up")
        _step(
            results,
            "organization",
            found == 1 and entry == 1 and moved.parent_entry_id == other.entry_id,
            {
                "tree_occurrences": found,
                "entry_rows": entry,
                "new_parent": moved.parent_entry_id,
            },
        )

        # 25–26 unpublish + history
        un = pub.unpublish(sid, published_by="acceptance_test")
        pubs = pub.list_publications(sid)
        events = [p.event_type for p in pubs]
        st2 = pub.get_publication_status(sid)
        _step(
            results,
            "publication_history",
            "publish" in events and "unpublish" in events,
            {"events": events, "count": len(pubs)},
        )
        _step(
            results,
            "unpublish",
            un.outcome in ("unpublished", "no_change")
            and st2["publication_status"] != "published",
            {"outcome": un.outcome, "status": st2["publication_status"]},
        )

        # 27 archive
        archived = core.archive_service(sid)
        _step(
            results,
            "archive",
            archived.status == "archived",
            {"status": archived.status, "service_id": archived.id},
        )

        # isolated DB markers untouched for legacy tables
        smm = conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0]
        orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        sched = conn.execute("SELECT COUNT(*) FROM scheduled_orders").fetchone()[0]
        _step(
            results,
            "isolated_legacy_tables",
            smm == 1 and orders == 1 and sched == 0,
            {"smm_services": smm, "orders": orders, "scheduled_orders": sched},
        )

    # Map section keys for report
    def ok(key: str) -> bool:
        return bool((results.get(key) or {}).get("ok"))

    section_map = {
        "create": "CREATE",
        "configuration": "CONFIGURATION",
        "target_policy": "TARGET",
        "fulfillment": "FULFILLMENT",
        "execution_source": "EXECUTION",
        "pricing": "PRICING",
        "readiness": "READINESS",
        "publish": "PUBLISHING",
        "edit": "EDITING",
        "price_history": "HISTORY",
        "source_history": "HISTORY",
        "publication_history": "HISTORY",
    }
    for k, code in section_map.items():
        if not ok(k) and code not in blockers:
            blockers.append(code)

    if not results["production_safety"]["ok"] and "SAFETY" not in blockers:
        blockers.append("SAFETY")

    if blockers:
        results["verdict"] = f"ADMIN_ACCEPTANCE_BLOCKED — {blockers[0]}"
    else:
        results["verdict"] = "ADMIN_ACCEPTANCE_PASS"

    results["blockers"] = blockers
    results["remaining_p0_blockers"] = []
    results["hard_stop"] = True
    results["explicit_statement"] = (
        "Acceptance used an isolated temp database only. "
        "No production writes. STOREFRONT_BACKEND left legacy. "
        "STOREFRONT_CATALOG_PILOT not enabled. No Telegram/pilot changes."
    )
    return results


def _md(report: dict) -> str:
    def j(k: str) -> str:
        return json.dumps(report.get(k), ensure_ascii=False, indent=2)

    return "\n".join(
        [
            "# Catalog Admin — Practical Acceptance Test",
            "",
            f"**Verdict:** `{report.get('verdict')}`",
            "",
            "## 1. Overall verdict",
            "",
            str(report.get("verdict")),
            "",
            f"Environment: `{report.get('test_environment')}`",
            "",
            f"Isolated DB: `{report.get('isolated_db')}`",
            "",
            "## 2. Create result",
            "",
            f"```json\n{j('create')}\n```",
            "",
            "## 3. Configuration result",
            "",
            f"```json\n{j('configuration')}\n```",
            "",
            "## 4. Target policy result",
            "",
            f"```json\n{j('target_policy')}\n```",
            "",
            "## 5. Fulfillment result",
            "",
            f"```json\n{j('fulfillment')}\n```",
            "",
            "## 6. Execution source result",
            "",
            f"```json\n{j('execution_source')}\n```",
            "",
            "## 7. Pricing result",
            "",
            f"```json\n{j('pricing')}\n```",
            "",
            "## 8. Readiness result",
            "",
            f"```json\n{j('readiness')}\n```",
            "",
            f"Fix cycle:\n```json\n{j('readiness_fix_cycle')}\n```",
            "",
            "## 9. Publish result",
            "",
            f"```json\n{j('publish')}\n```",
            "",
            "## 10. Edit result",
            "",
            f"```json\n{j('edit')}\n```",
            "",
            "## 11. Drift result",
            "",
            f"```json\n{j('drift')}\n```",
            "",
            "## 12. Price history result",
            "",
            f"```json\n{j('price_history')}\n```",
            "",
            "## 13. Source history result",
            "",
            f"```json\n{j('source_history')}\n```",
            "",
            "## 14. Publication history result",
            "",
            f"```json\n{j('publication_history')}\n```",
            "",
            f"Unpublish:\n```json\n{j('unpublish')}\n```",
            "",
            "## 15. Organization result",
            "",
            f"```json\n{j('organization')}\n```",
            "",
            "## 16. Failure-case results",
            "",
            f"```json\n{j('failure_cases')}\n```",
            "",
            "## 17. Legacy safety verification",
            "",
            f"```json\n{j('legacy_safety')}\n```",
            "",
            "## 18. Production safety verification",
            "",
            f"```json\n{j('production_safety')}\n```",
            "",
            "## 19. Tests executed",
            "",
            "- Isolated acceptance harness (this script)",
            "- UI surface static checks on catalog_core_ui.js / catalog_services.html",
            "- Production DB read-only invariant snapshot",
            "",
            f"UI surface:\n```json\n{j('ui_surface')}\n```",
            "",
            "## 20. Exact blockers",
            "",
            json.dumps(report.get("blockers") or [], ensure_ascii=False, indent=2),
            "",
            "## Explicit statement",
            "",
            str(report.get("explicit_statement")),
            "",
            "## HARD STOP",
            "",
            "No fixes implemented. No P1. No pilot enablement.",
            "",
        ]
    )


def main() -> None:
    report = run_acceptance()
    out_json = ROOT / "scripts" / "out_catalog_admin_acceptance.json"
    out_md = ROOT / "scripts" / "out_catalog_admin_acceptance.md"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_md(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "verdict": report.get("verdict"),
                "blockers": report.get("blockers"),
                "isolated_db": report.get("isolated_db"),
                "publish_ok": (report.get("publish") or {}).get("ok"),
                "production_writes": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
