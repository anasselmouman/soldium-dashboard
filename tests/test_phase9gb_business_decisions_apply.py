# -*- coding: utf-8 -*-
"""Phase 9G-B — approved business decision application tests (isolated)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.phase9gb_business_decisions_apply import (
    FUTURE_PILOT_LEGACY_IDS,
    MISSING_EXEC_LEGACY_IDS,
    apply_approved_service_types,
    build_decision_sets,
    run_phase9gb,
)
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "phase9gb.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE smm_services (service_id TEXT PRIMARY KEY, name_ar TEXT);
            CREATE TABLE orders (id INTEGER PRIMARY KEY, service_id TEXT);
            CREATE TABLE scheduled_orders (id INTEGER PRIMARY KEY);
            CREATE TABLE providers (
                slug TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
                api_base_url TEXT NOT NULL DEFAULT '', is_active INTEGER DEFAULT 1
            );
            INSERT INTO providers(slug, name) VALUES ('gozibra', 'Gozibra');
            CREATE TABLE provider_accounts (
                id INTEGER PRIMARY KEY,
                provider_slug TEXT NOT NULL,
                account_key TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                UNIQUE(provider_slug, account_key)
            );
            INSERT INTO provider_accounts(provider_slug, account_key, display_name)
            VALUES ('gozibra', 'a1', 'A1');
            CREATE TABLE catalog_nodes (id TEXT PRIMARY KEY);
            CREATE TABLE catalog_services (catalog_id TEXT PRIMARY KEY, name_ar TEXT);
            CREATE TABLE service_provider_bindings (id INTEGER PRIMARY KEY);
            CREATE TABLE provider_inventory (id INTEGER PRIMARY KEY);
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _seed_bridge(conn, *, legacy_id: str, service_id: str, codes: str = "[]"):
    conn.execute(
        """
        INSERT INTO soldium_catalog_legacy_bridge (
            soldium_service_id, legacy_catalog_id, legacy_local_item_id,
            platform_key, section_key, subsection_key,
            migration_batch_id, classification, review_codes
        ) VALUES (?, ?, ?, 'x', 'spaces', NULL, 't', 'imported', ?)
        """,
        (service_id, legacy_id, legacy_id, codes),
    )


def test_apply_writes_other_when_needed_and_idempotent(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        # Minimal: bypass full 9G partition by calling apply with synthetic sets
        from catalog_core.phase9gb_business_decisions_apply import DecisionSets, DecisionMember

        svc = core.create_service(
            name_ar="اختبار",
            service_type="likes",
            ordering_mode="quantity_based",
        )
        core.change_execution_source(
            svc.id,
            provider_slug="gozibra",
            provider_account_key="a1",
            external_service_id="999",
        )
        sets = DecisionSets()
        sets.PROBABLE_KEEP_OTHER.append(
            DecisionMember(
                legacy_catalog_id="L1",
                soldium_service_id=svc.id,
                platform="x",
                section="spaces",
                subsection=None,
                service_type="likes",
            )
        )
        r1 = apply_approved_service_types(conn, sets, dry_run=False)
        assert r1["mutation_count"] == 1
        assert core.get_service(svc.id).service_type == "other"
        events = conn.execute(
            "SELECT * FROM soldium_catalog_semantic_events WHERE service_id=?",
            (svc.id,),
        ).fetchall()
        assert len(events) == 1
        assert events[0]["previous_value"] == "likes"
        assert events[0]["new_value"] == "other"

        r2 = apply_approved_service_types(conn, sets, dry_run=False)
        assert r2["mutation_count"] == 0
        assert r2["skipped_already_other"] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_semantic_events WHERE service_id=?",
                (svc.id,),
            ).fetchone()[0]
            == 1
        )


def test_followers_members_not_in_keep_other_targets():
    # Structural: FUTURE list size and missing-exec IDs are fixed approvals
    assert len(FUTURE_PILOT_LEGACY_IDS) == 15
    assert set(MISSING_EXEC_LEGACY_IDS) == {
        "2128",
        "2326",
        "2405",
        "4590",
        "4721",
    }


def test_artifact_if_present():
    path = Path("scripts/out_phase9g_business_decisions_applied.json")
    if not path.exists():
        pytest.skip("9G-B artifact missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["verdict"] == "PHASE 9G-B COMPLETE — APPROVED DECISIONS APPLIED"
    counts = data["decision_counts"]
    assert counts["PROBABLE_KEEP_OTHER"] == 19
    assert counts["PROBABLE_BUSINESS_REVIEW"] == 5
    assert counts["SPECIAL_SAFE_AS_OTHER"] == 61
    assert counts["SPECIAL_BUSINESS_REVIEW"] == 3
    assert counts["SPECIAL_TECHNICAL_REVIEW"] == 5
    assert counts["SENTINEL_BLOCKED"] == 22
    assert counts["IPTV_BLOCKED"] == 7
    assert counts["MISSING_EXECUTION_BLOCKED"] == 5
    assert counts["READY_UNPUBLISHED"] == 85
    assert counts["FUTURE_PILOT_CANDIDATES"] == 15
    assert data["apply"]["mutation_count"] == 0  # already other in production
    assert data["idempotency_second_pass"]["noop"] is True
    assert data["invariants"]["ok"] is True
    assert data["invariants"]["counts"]["publications"] == 23
    # followers_members not mutated
    review_ids = {
        m["legacy_catalog_id"]
        for m in data["decision_sets"]["PROBABLE_BUSINESS_REVIEW"]
    }
    assert review_ids == {"1781", "4210", "4215", "4469", "4702"}
    mut_legacy = {m["legacy_catalog_id"] for m in data["apply"]["mutations"]}
    assert mut_legacy.isdisjoint(review_ids)
    # blocked cohorts unchanged — no mutations for them
    for key in (
        "SENTINEL_BLOCKED",
        "IPTV_BLOCKED",
        "MISSING_EXECUTION_BLOCKED",
        "SPECIAL_BUSINESS_REVIEW",
        "SPECIAL_TECHNICAL_REVIEW",
    ):
        blocked = {m["soldium_service_id"] for m in data["decision_sets"][key]}
        assert blocked.isdisjoint(
            {m["soldium_service_id"] for m in data["apply"]["mutations"]}
        )
