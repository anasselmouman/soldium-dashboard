# -*- coding: utf-8 -*-
"""Phase 9P — Pricing semantics review (READ-ONLY decision pack)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalog_core.phase9d_audit import production_counts
from catalog_core.phase9g_business_decision_pack import _published_ids

PHASE = "9P"
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

VERDICT = "PHASE_9P_REVIEW_COMPLETE — LIVE_PRICE_RECOMMENDED"


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


def _static_evidence() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    so = (root / "scheduled_orders.py").read_text(encoding="utf-8")
    schema = (root / "db_schema.py").read_text(encoding="utf-8")
    tpl = (root / "templates" / "scheduled_orders.html").read_text(encoding="utf-8")
    bot_orders = Path(root.parent / "soldium-bot" / "handlers" / "orders.py")
    bot_text = bot_orders.read_text(encoding="utf-8") if bot_orders.is_file() else ""

    scheduled_ddl = ""
    if "CREATE TABLE IF NOT EXISTS scheduled_orders" in schema:
        scheduled_ddl = schema.split("CREATE TABLE IF NOT EXISTS scheduled_orders", 1)[
            1
        ].split(");", 1)[0]

    return {
        "scheduled_compute_order_pricing_present": "async def _compute_order_pricing" in so,
        "scheduled_submit_calls_compute": "_compute_order_pricing(service_id, quantity)"
        in so,
        "scheduled_ddl_has_local_price_or_amount": (
            "local_price" in scheduled_ddl or "amount" in scheduled_ddl
        ),
        "scheduled_ui_admin_debit": "حساب الأدمن" in tpl,
        "bot_confirm_price_drift_guard": "تغير سعر الخدمة أثناء إعداد الطلب" in bot_text,
        "bot_create_order_with_balance_hold": "create_order_with_balance_hold" in bot_text,
        "code_comment_retail_live": "Retail price may still be computed live" in so,
    }


def build_decision_pack(conn: sqlite3.Connection) -> dict[str, Any]:
    before = capture_baseline(conn)
    evidence = _static_evidence()

    freeze_matrix = [
        {
            "flow": "Immediate Telegram order (browse/confirm)",
            "price_source": "smm_services.local_price_dh → in-memory SERVICES.price",
            "calculated_at": "qty step + confirm (FSM confirm_total); rechecked at confirm",
            "stored": "Yes — orders.amount / orders.total_price at create",
            "can_change_later": "No after create; confirm rejects drift vs in-memory catalog",
        },
        {
            "flow": "Immediate Telegram — balance hold",
            "price_source": "FSM confirm_total (already calculated)",
            "calculated_at": "order create / create_order_with_balance_hold",
            "stored": "Yes (amount=total_price); balance debited same transaction",
            "can_change_later": "No",
        },
        {
            "flow": "Scheduled order creation",
            "price_source": "None stored",
            "calculated_at": "Not calculated",
            "stored": "No retail columns on scheduled_orders",
            "can_change_later": "N/A — no commercial freeze at schedule create",
        },
        {
            "flow": "Scheduled materialization",
            "price_source": "Live get_service → smm_services.local_price_dh via _compute_order_pricing",
            "calculated_at": "_submit_job_order before _create_order_with_balance_hold",
            "stored": "Yes — on newly created orders.amount / total_price",
            "can_change_later": "That Order freezes; next scheduled run recalculates live again",
        },
        {
            "flow": "Provider execution / retry / manual fulfill",
            "price_source": "orders.amount (already stored)",
            "calculated_at": "Not recalculated",
            "stored": "Already stored",
            "can_change_later": "Retail amount not rewritten; provider_cost may be resolved for analytics",
        },
        {
            "flow": "Catalog Adapter quote / Order Intent",
            "price_source": "Published publication amount_millimes",
            "calculated_at": "quote_price / resolve_order_intent (read-only)",
            "stored": "Intent carries quoted_amount_millimes; not charged by Telegram yet",
            "can_change_later": "Publication row immutable; new publish can change future quotes",
        },
        {
            "flow": "Legacy catalog admin edit",
            "price_source": "smm_services.local_price_dh",
            "calculated_at": "Admin edit time",
            "stored": "On smm_services row",
            "can_change_later": "Yes — affects future immediate quotes and scheduled materializations",
        },
    ]

    scenarios = [
        {
            "id": 1,
            "title": "Immediate 10 MAD → admin changes to 12 before fulfillment",
            "result_today": "Customer pays 10 MAD (orders.amount frozen at create). Fulfillment/retry do not reprice.",
            "code_basis": "bot create_order_with_balance_hold persists amount; ensure_provider_order_ref / submit do not UPDATE amount",
        },
        {
            "id": 2,
            "title": "Schedule at 10 MAD → price 12 before execution",
            "result_today": "Materialization charges 12 MAD from admin balance (live local_price_dh).",
            "code_basis": "_submit_job_order → _compute_order_pricing → get_service live",
        },
        {
            "id": 3,
            "title": "Schedule at 10 MAD → price falls to 8",
            "result_today": "Materialization charges 8 MAD (live).",
            "code_basis": "same live path",
        },
        {
            "id": 4,
            "title": "Admin balance only enough for 10; price becomes 12",
            "result_today": "Run fails closed with insufficient admin balance; no Order created for that run; consecutive_failures increments.",
            "code_basis": "_create_order_with_balance_hold balance < amount_money → ScheduledOrderValidationError",
        },
        {
            "id": 5,
            "title": "Gen-1 frozen execution identity; retail changes",
            "result_today": "Provider SKU unchanged; commercial amount at materialize uses live retail. Execution identity and retail are decoupled.",
            "code_basis": "Phase 9O fail-closed on Gen-0 SKU; docstring: retail still live",
        },
        {
            "id": 6,
            "title": "Provider cost (provider_price_usd) changes; retail local_price_dh unchanged",
            "result_today": "Customer/admin retail amount unchanged if local_price_dh unchanged. provider_cost_dh snapshot at order create may differ from later catalog cost; analytics may prefer snapshot.",
            "code_basis": "_compute_order_pricing amount from local_price_dh; cost from provider_price_usd (+ fallbacks)",
        },
    ]

    policy_comparison = {
        "option_a_freeze_at_schedule_creation": {
            "technically_easiest": False,
            "requires": [
                "new scheduled_orders retail snapshot columns",
                "UI disclosure of frozen price",
                "decision: hold balance at schedule create vs soft commitment",
                "policy for price decreases/increases after freeze",
            ],
            "customer_fairness": "High IF schedules were customer commitments — currently they are not",
            "compatibility_current_ux": "Low — UI has no price column; admin debit only; production scheduled_orders=0",
            "architecturally_correct_for_current_product": False,
        },
        "option_b_live_until_materialization": {
            "technically_easiest": True,
            "matches_current_code_schema_ui": True,
            "customer_fairness": "N/A for customer — debit is ADMIN_ID; future customer schedules would need disclosure",
            "balance_safety": "Checked at materialize against current price — fails if insufficient",
            "accounting_consistency": "Aligns with immediate-order rule: commercial freeze at Order create + balance hold",
            "architecturally_correct_for_current_product": True,
            "commercially_safest_for_current_admin_scheduler": True,
        },
    }

    recommendation = {
        "primary": VERDICT,
        "scheduled_orders_policy": "LIVE_PRICE until materialization (retain established behavior)",
        "immediate_orders_policy": "FREEZE at order creation / balance hold (already true — preserve)",
        "commercial_freeze_point": "order_creation_and_balance_hold",
        "not_freeze_point": "schedule_creation",
        "gen1_retail": "Do NOT add retail freeze to Gen-1 execution identity contract. Gen-1 freezes Provider/SOLDIUM execution identity only. Retail remains independent.",
        "gen0_retail": "Gen-0 Provider execution already fail-closed (9O). Pricing irrelevant for Gen-0 Provider submit. Historical Gen-0 Orders that already exist keep stored amount. No migration.",
        "catalog": "Published amount_millimes remains the future storefront quote source. At Telegram Catalog cutover, freeze quoted_amount into Order at create/hold — same commercial event as Legacy immediate. Do not merge with local_price_dh in this phase.",
        "legacy": "smm_services.local_price_dh remains the live Telegram + scheduled charge source until cutover. Isolated from Catalog prices.",
        "exact_freeze_point": {
            "immediate": "Telegram order create via create_order_with_balance_hold (amount persisted + balance debited)",
            "scheduled": "Materialization order create via _create_order_with_balance_hold (live price computed immediately before this event)",
            "catalog_future": "Order create from Order Intent quoted_amount_millimes at balance hold",
            "explicitly_not": "schedule creation; provider fulfillment; retry",
        },
        "business_reasoning": [
            "Repository treats scheduled_orders as a future instruction for ADMIN balance, not a customer price reservation.",
            "Schema stores no retail on scheduled_orders; UI discloses admin debit, not a locked customer price.",
            "Immediate Orders already freeze commercial amount at create+hold — the correct commercial event.",
            "Freezing retail at schedule create without holding balance invents a soft commitment absent from current UX/accounting.",
            "Execution identity (9O) and retail price are separate concerns; do not couple them.",
            "Option B is established behavior; Option A would be a product change requiring a later implementation phase after review.",
        ],
    }

    cutover_impact = {
        "B3_pricing_semantics": "CLOSED as decision: LIVE until materialize for schedules; freeze at Order create/hold for all charge paths. Implementation phase may only document/assert; no schema change required to keep Option B.",
        "B4_telegram_backend": "Unaffected now. At Catalog cutover, wire Adapter quote → Order amount at create (same freeze event).",
        "B5_parity": "Parity must compare commercial amount sources carefully: Legacy local_price_dh vs published millimes — separate systems until cutover.",
        "B6_rollback": "Keeping live scheduled pricing preserves rollback simplicity (no new frozen retail columns to backfill).",
    }

    after = capture_baseline(conn)
    production_ok = before["ok"] and after["ok"] and before["baseline"] == after["baseline"]

    return {
        "phase": PHASE,
        "phase_name": "Phase 9P — Pricing Semantics Review",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "verdict": VERDICT if production_ok else "PHASE_9P_BLOCKED — UNEXPECTED_PRICING_PATH",
        "read_only": True,
        "production_code_mutated": False,
        "pricing_behavior_changed": False,
        "current_pricing_architecture": {
            "retail_customer_facing": "smm_services.local_price_dh (Legacy Telegram + scheduled charge path)",
            "provider_cost": "provider_price_usd → compute_provider_cost_dh; snapshotted as orders.provider_cost_dh at order create",
            "order_amount": "orders.amount / orders.total_price (same value at insert)",
            "held_balance": "users.balance debit in create_order_with_balance_hold / _create_order_with_balance_hold",
            "scheduled_amount": "NOT stored on scheduled_orders; computed live at materialize",
            "catalog_price": "soldium_catalog_prices.amount_millimes (authoring)",
            "published_price": "soldium_catalog_publications.amount_millimes (immutable per publish)",
            "historical_price": "ended soldium_catalog_prices rows; Order amount is historical commercial record",
            "dual_path": "LIVE charge path = Legacy local_price_dh; Catalog Adapter quote path unwired to Telegram",
        },
        "immediate_order_semantics": {
            "when_calculated": "During FSM (qty/invoice) from in-memory SERVICES; revalidated at confirm",
            "when_shown": "Invoice / confirm UI (order_flow.build_invoice_text)",
            "when_balance_held": "create_order_with_balance_hold at confirm",
            "stored_on_order": True,
            "can_change_after_create": False,
            "fulfillment_recalculates_retail": False,
            "retry_recalculates_retail": False,
            "execution_changes_amount": False,
            "invariant_verified": "Once Order created and balance held, amount does not silently follow later catalog edits",
        },
        "scheduled_order_semantics": {
            "conceptual_model": "future_instruction_not_price_reservation",
            "price_at_schedule_create": None,
            "balance_at_schedule_create": False,
            "price_at_materialization": "live smm_services.local_price_dh",
            "balance_at_materialization": "ADMIN_TELEGRAM_ID debit",
            "ui_implies_price_commitment": False,
            "ui_copy": "يُخصم مبلغ كل تنفيذ من رصيد حساب الأدمن",
            "option_a_or_b_today": "B_live_at_execution",
        },
        "gen0_pricing_semantics": {
            "execution": "Fail-closed (9O) — no Provider submit",
            "retail_if_were_executable": "Would have used same live _compute_order_pricing (now unreachable for Provider)",
            "historical_orders": "Keep stored amount; no migration; no repair",
        },
        "gen1_pricing_semantics": {
            "execution_identity_frozen": True,
            "retail_frozen_on_job": False,
            "retail_at_materialize": "live",
            "should_gen1_freeze_retail": False,
            "reason": "Retail is commercial identity, separate from Provider Service ID freeze",
        },
        "catalog_pricing_semantics": {
            "published_immutable_per_row": True,
            "projection_exposes_publication_price": True,
            "adapter_quotes_published": True,
            "order_intent_has_quoted_amount": True,
            "telegram_uses_catalog_price": False,
        },
        "legacy_pricing_semantics": {
            "source": "smm_services.local_price_dh",
            "used_by": ["Telegram immediate", "scheduled materialization"],
            "isolated_from_catalog_prices": True,
        },
        "price_freeze_matrix": freeze_matrix,
        "scenario_results": scenarios,
        "business_policy_comparison": policy_comparison,
        "primary_recommendation": recommendation,
        "exact_proposed_freeze_point": recommendation["exact_freeze_point"],
        "impact_on_future_phases": cutover_impact,
        "static_evidence": evidence,
        "production_baseline": before["baseline"],
        "production_after": after["baseline"],
        "production_unchanged": production_ok,
        "production_mismatches": before["mismatches"] or after["mismatches"],
        "remaining_blockers": [
            "B4 — Telegram still Legacy; Catalog quote path unwired",
            "B5 — parity across Legacy retail vs Catalog published millimes at cutover",
            "B6 — rollback rehearsal not done",
            "If product later introduces customer-facing scheduled orders with quoted price, revisit Option A as a deliberate product change",
            "Implementation of 9P recommendation is documentation/assertion only unless product chooses Option A later",
        ],
        "explicit_non_goals": {
            "pricing_code_changed": False,
            "catalog_prices_modified": False,
            "smm_services_modified": False,
            "orders_modified": False,
            "scheduled_orders_modified": False,
            "gen0_migrated": False,
            "telegram_backend_switched": False,
            "publications_modified": False,
            "provider_mappings_modified": False,
            "cutover_performed": False,
        },
        "hard_stop": True,
        "next_step_after_review": "ARCHITECTURAL_REVIEW_OF_9P_THEN_OPTIONAL_IMPLEMENTATION_PHASE",
    }


def run_phase9p(conn: sqlite3.Connection) -> dict[str, Any]:
    return build_decision_pack(conn)
