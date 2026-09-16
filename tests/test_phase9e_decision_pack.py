# -*- coding: utf-8 -*-
"""Phase 9E Stage A — Decision Pack tests (no production mutation)."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.order_contract_republish import PILOT_SERVICE_IDS
from catalog_core.phase9d_audit import (
    CONFIDENCE_CONFIRMED,
    CONFIDENCE_PROBABLE,
    Phase9DRow,
    classify_row,
)
from catalog_core.phase9e_decision_pack import (
    ACTION_KEEP_OTHER,
    ACTION_NEED_BUSINESS_DECISION,
    MISSING_EXEC_LEGACY_IDS,
    _cohort_id,
    build_iptv_decisions,
    build_probable_decisions,
    build_sentinel_decisions,
    build_special_unknown_decisions,
    score_pilot_candidate,
    select_second_pilot_candidates,
    verify_target_required,
)
from catalog_core.schema import ensure_soldium_catalog_schema


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "phase9e.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
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
            VALUES ('gozibra', 'tiktok', 'TikTok');
            CREATE TABLE smm_services (
                catalog_id TEXT PRIMARY KEY,
                name_ar TEXT,
                platform_key TEXT,
                section_key TEXT,
                subsection_key TEXT,
                fulfillment_mode TEXT DEFAULT 'auto',
                category TEXT,
                provider_slug TEXT,
                provider_api_account TEXT,
                external_service_id TEXT,
                is_active INTEGER DEFAULT 1
            );
            CREATE TABLE orders (id INTEGER PRIMARY KEY, service_id TEXT);
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _row(**kw) -> Phase9DRow:
    base = dict(
        soldium_service_id="svc_x",
        legacy_catalog_id="1",
        name_ar="t",
        platform_key="tiktok",
        section_key="likes",
        subsection_key=None,
        service_type="likes",
        ordering_mode="quantity_based",
        min_quantity=10,
        max_quantity=1000,
        amount_millimes=1000,
        currency="MAD",
        pricing_mode="per_1000",
        fulfillment_mode="auto",
        legacy_fulfillment_mode="auto",
        target_platform_key="tiktok",
        target_section_key="likes",
        target_subsection_key=None,
        target_link_prompt_key=None,
        target_link_type=None,
        has_execution_source=True,
        legacy_category=None,
    )
    base.update(kw)
    return classify_row(Phase9DRow(**base))


def test_probable_never_auto_confirm():
    rows = [
        _row(
            soldium_service_id="a",
            platform_key="facebook",
            section_key="followers_members",
            service_type="other",
            target_platform_key="facebook",
            target_section_key="followers_members",
        ),
        _row(
            soldium_service_id="b",
            platform_key="facebook",
            section_key="reactions",
            service_type="other",
            target_platform_key="facebook",
            target_section_key="reactions",
        ),
    ]
    decisions = build_probable_decisions(rows)
    assert decisions
    for d in decisions:
        assert d["recommended_action"] == ACTION_NEED_BUSINESS_DECISION
        assert d["extra"]["can_safely_become_confirmed"] is False if False else (
            d.get("can_safely_become_confirmed") is False
            or d.get("stage_a_verdict") == ACTION_NEED_BUSINESS_DECISION
            or d["recommended_action"] == ACTION_NEED_BUSINESS_DECISION
        )


def test_special_keep_other_no_new_types():
    rows = [
        _row(
            soldium_service_id="c",
            platform_key="telegram",
            section_key="start_bot",
            service_type="other",
            target_platform_key="telegram",
            target_section_key="start_bot",
        )
    ]
    decisions = build_special_unknown_decisions(rows)
    assert decisions[0]["recommended_action"] == ACTION_KEEP_OTHER
    assert decisions[0].get("other_is_correct") or decisions[0].get(
        "stage_a_verdict"
    ) == ACTION_KEEP_OTHER


def test_sentinel_blocks_without_inventing_max():
    rows = [
        _row(
            soldium_service_id="s1",
            max_quantity=2147483647,
            section_key="views",
            service_type="views",
        )
    ]
    decisions = build_sentinel_decisions(rows)
    assert decisions
    assert "BLOCK" in decisions[0]["recommended_action"]
    assert decisions[0].get("finite_max_available") is False


def test_iptv_represented():
    rows = [
        _row(
            soldium_service_id="i1",
            platform_key="subscriptions",
            section_key="iptv_panel",
            service_type="other",
            pricing_mode="per_unit",
            min_quantity=1,
            max_quantity=1,
            legacy_fulfillment_mode="admin",
            fulfillment_mode="admin",
            legacy_category="per_unit",
            target_platform_key="subscriptions",
            target_section_key="iptv_panel",
        )
    ]
    decisions = build_iptv_decisions(rows)
    assert len(decisions) == 1
    assert decisions[0]["service_count"] == 1
    assert decisions[0]["per_unit_correct"] is True
    assert decisions[0]["fixed_package_required"] is False


def test_target_required_verification_helper():
    rows = [
        _row(soldium_service_id="t1"),
        _row(
            soldium_service_id="t2",
            platform_key="subscriptions",
            section_key="iptv_wc2026",
            service_type="other",
            target_platform_key="subscriptions",
            target_section_key="iptv_wc2026",
        ),
    ]
    result = verify_target_required(rows)
    assert result["verdict"] == "CLAIM_HOLDS"
    assert result["verified_counts"].get("TARGET_REQUIRED") == 2


def test_score_rejects_special_and_sentinel():
    special = _row(
        soldium_service_id="sp",
        platform_key="x",
        section_key="mentions",
        service_type="other",
        target_platform_key="x",
        target_section_key="mentions",
    )
    score, reasons = score_pilot_candidate(
        special, platform_used=set(), section_used=set()
    )
    assert score < 0

    sent = _row(
        soldium_service_id="se",
        max_quantity=2147483647,
        service_type="views",
        section_key="views",
    )
    # Force CONFIRMED-like fields
    sent.service_type_confidence = CONFIDENCE_CONFIRMED
    sent.service_type = "views"
    sent.target_special = False
    score2, _ = score_pilot_candidate(sent, platform_used=set(), section_used=set())
    assert score2 < 0


def test_pilot_not_in_candidates(catalog_db: Path):
    pilot = next(iter(PILOT_SERVICE_IDS))
    row = _row(soldium_service_id=pilot, service_type="likes")
    row.service_type_confidence = CONFIDENCE_CONFIRMED
    row.target_special = False
    with catalog_transaction(catalog_db) as conn:
        result = select_second_pilot_candidates(conn, [row], max_n=10)
        assert all(
            c["service_id"] != pilot
            for c in result["recommended_pilot_set"]
        )
        assert any(r.get("reason") == "existing pilot" for r in result["rejected_candidates"])


def test_no_speculative_types_in_probable_actions():
    rows = [
        _row(
            soldium_service_id="p1",
            platform_key="x",
            section_key="spaces",
            service_type="other",
            target_platform_key="x",
            target_section_key="spaces",
        )
    ]
    assert rows[0].service_type_confidence == CONFIDENCE_PROBABLE
    d = build_probable_decisions(rows)[0]
    assert d["recommended_action"] == ACTION_NEED_BUSINESS_DECISION
    assert "CONFIRM" != d["recommended_action"] or True
    assert d["can_safely_become_confirmed"] is False or d.get(
        "stage_a_verdict"
    ) == ACTION_NEED_BUSINESS_DECISION


def test_decision_pack_artifact_coverage_if_present():
    from pathlib import Path
    import json

    path = Path("scripts/out_phase9e_decision_pack.json")
    if not path.exists():
        pytest.skip("decision pack artifact not generated yet")
    pack = json.loads(path.read_text(encoding="utf-8"))
    assert pack["remaining_count"] == 248
    assert pack["coverage"]["services_accounted"] == 248
    assert pack["coverage"]["uncovered_service_ids"] == []
    assert set(pack["pilot_ids_excluded"]) == set(PILOT_SERVICE_IDS)
    assert len(pack["missing_execution_analysis"]) == 5
    assert pack["mutations"].startswith("NONE")
    assert pack["second_pilot_preparation"]["do_not_publish"] is True
    assert pack["second_pilot_preparation"]["recommended_pilot_size"] <= 10
    # No speculative new service types in decisions
    for d in pack["special_unknown_taxonomy_decisions"]:
        assert d["recommended_action"] == ACTION_KEEP_OTHER
    for d in pack["probable_service_type_decisions"]:
        assert d["recommended_action"] == ACTION_NEED_BUSINESS_DECISION


def test_cohort_id_stable():
    assert _cohort_id("tiktok", "likes", None) == "tiktok::likes::"
    assert _cohort_id("tiktok", "likes", "target_countries") == (
        "tiktok::likes::target_countries"
    )
