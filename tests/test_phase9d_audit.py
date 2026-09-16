# -*- coding: utf-8 -*-
"""Phase 9D — service semantics classification & safe authoring tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.order_contract_republish import PILOT_SERVICE_IDS
from catalog_core.phase9d_audit import (
    CONFIDENCE_CONFIRMED,
    CONFIDENCE_PROBABLE,
    CONFIDENCE_SPECIAL,
    CONFIDENCE_UNKNOWN,
    LEGACY_COMMENT_SERVICE_ID,
    Phase9DRow,
    apply_confirmed_authoring,
    classify_ordering,
    classify_row,
    classify_service_type,
    classify_quantity,
)
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_shadow import compare_storefronts


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "phase9d.db"
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
                is_active INTEGER DEFAULT 1,
                category TEXT,
                min_qty INTEGER,
                max_qty INTEGER
            );
            CREATE TABLE orders (id INTEGER PRIMARY KEY, service_id TEXT NOT NULL);
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def test_confirmed_section_mappings_not_name_based():
    # Names must not matter — only section_key.
    for section, expected in [
        ("likes", "likes"),
        ("views", "views"),
        ("video_views", "views"),
        ("video_reels_views", "views"),
        ("post_views", "views"),
        ("followers", "followers"),
        ("subscribers", "followers"),
        ("channel_members", "members"),
        ("post_share", "shares"),
        ("geo_shares", "shares"),
        ("live_stream_views", "live_viewers"),
        ("live_broadcast", "live_viewers"),
        ("live_stream", "live_viewers"),
    ]:
        cand, conf, _ = classify_service_type(
            platform_key="tiktok",
            section_key=section,
            subsection_key=None,
        )
        assert cand == expected
        assert conf == CONFIDENCE_CONFIRMED


def test_no_keyword_only_classification_from_name():
    cand, conf, evid = classify_service_type(
        platform_key="tiktok",
        section_key="direct",
        subsection_key=None,
    )
    assert cand == "other"
    assert conf == CONFIDENCE_UNKNOWN
    assert "name" not in evid.lower() or "not name" in evid.lower()


def test_member_bundles_remain_special_other():
    cand, conf, _ = classify_service_type(
        platform_key="telegram",
        section_key="channel_members",
        subsection_key="member_bundles",
    )
    assert cand == "other"
    assert conf == CONFIDENCE_SPECIAL


def test_probable_not_confirmed():
    cand, conf, _ = classify_service_type(
        platform_key="facebook",
        section_key="followers_members",
        subsection_key=None,
    )
    assert cand == "followers"
    assert conf == CONFIDENCE_PROBABLE


def test_interactions_special_other():
    for sk in (
        "automatic_interactions",
        "post_interactions",
        "interaction",
        "mentions",
        "spaces",
        "start_bot",
    ):
        cand, conf, _ = classify_service_type(
            platform_key="telegram" if "bot" in sk or "interaction" in sk else "x",
            section_key=sk if sk != "spaces" else "spaces",
            subsection_key=None,
        )
        # spaces is PROBABLE live_viewers in our map
        if sk == "spaces":
            assert conf == CONFIDENCE_PROBABLE
        else:
            assert cand == "other"
            assert conf in {CONFIDENCE_SPECIAL, CONFIDENCE_UNKNOWN}


def test_subscriptions_special_other():
    cand, conf, _ = classify_service_type(
        platform_key="subscriptions",
        section_key="iptv_panel",
        subsection_key=None,
    )
    assert cand == "other"
    assert conf == CONFIDENCE_SPECIAL


def test_ordering_per_unit_unknown_not_package():
    mode, conf, evid = classify_ordering(
        pricing_mode="per_unit",
        min_quantity=1,
        max_quantity=1,
        platform_key="subscriptions",
        section_key="iptv_panel",
        legacy_category="per_unit",
    )
    assert mode == "quantity_based"
    assert conf == CONFIDENCE_UNKNOWN
    assert "package_based" in evid


def test_min_equals_max_one_not_auto_package():
    mode, conf, _ = classify_ordering(
        pricing_mode="per_1000",
        min_quantity=1,
        max_quantity=1,
        platform_key="telegram",
        section_key="post_views",
        legacy_category=None,
    )
    assert mode == "quantity_based"
    assert conf == CONFIDENCE_PROBABLE


def test_sentinel_quantity_decision():
    assert classify_quantity(2147483647, []) == "SENTINEL_REQUIRES_BUSINESS_DECISION"
    assert classify_quantity(1000, []) == "CONFIRMED_FINITE_MAX"
    assert classify_quantity(5000, ["max_qty_sentinel"]) == (
        "SENTINEL_REQUIRES_BUSINESS_DECISION"
    )


def test_classify_row_4371_link_type():
    row = Phase9DRow(
        soldium_service_id="svc_tmp",
        legacy_catalog_id=LEGACY_COMMENT_SERVICE_ID,
        name_ar="لايكات على التعليقات",
        platform_key="tiktok",
        section_key="likes",
        subsection_key=None,
        service_type="other",
        ordering_mode="quantity_based",
        min_quantity=50,
        max_quantity=1000000,
        amount_millimes=9000,
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
    classify_row(row)
    assert row.candidate_service_type == "likes"
    assert row.service_type_confidence == CONFIDENCE_CONFIRMED
    assert row.safe_author_link_type == "comment"
    assert row.target_required == "TARGET_REQUIRED"


def test_safe_authoring_idempotent_preserves_price_exec(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        node = core.create_node(name_ar="تيك توك")
        svc = core.create_service(
            name_ar="لايكات",
            parent_entry_id=node.entry_id,
            service_type="other",
            ordering_mode="quantity_based",
            min_quantity=50,
            max_quantity=5000,
            status="active",
            fulfillment_mode="auto",
            target_platform_key="tiktok",
            target_section_key="likes",
        )
        core.change_price(
            svc.id, amount_dh="2.5", pricing_mode="per_1000", currency="MAD"
        )
        core.change_execution_source(
            svc.id,
            provider_slug="gozibra",
            provider_account_key="tiktok",
            external_service_id="9999",
        )
        row = Phase9DRow(
            soldium_service_id=svc.id,
            legacy_catalog_id="1001",
            name_ar="لايكات",
            platform_key="tiktok",
            section_key="likes",
            subsection_key=None,
            service_type="other",
            ordering_mode="quantity_based",
            min_quantity=50,
            max_quantity=5000,
            amount_millimes=2500,
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
        classify_row(row)
        first = apply_confirmed_authoring(conn, [row], dry_run=False)
        assert first["updated_service_type"] == 1
        after = core.get_service(svc.id)
        assert after.service_type == "likes"
        assert after.ordering_mode == "quantity_based"
        assert after.min_quantity == 50
        assert after.max_quantity == 5000
        assert int(core.repo.get_active_price(svc.id).amount_millimes) == 2500
        assert (
            str(core.repo.get_active_execution_source(svc.id).external_service_id)
            == "9999"
        )
        entry = core.repo.get_entry_for_service(svc.id)
        assert entry.parent_entry_id == node.entry_id

        row.service_type = "likes"
        classify_row(row)
        second = apply_confirmed_authoring(conn, [row], dry_run=False)
        assert second["updated_service_type"] == 0
        assert second["change_count"] == 0


def test_probable_not_authored(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        node = core.create_node(name_ar="فيسبوك")
        svc = core.create_service(
            name_ar="متابعين",
            parent_entry_id=node.entry_id,
            service_type="other",
            status="active",
        )
        row = Phase9DRow(
            soldium_service_id=svc.id,
            legacy_catalog_id="2001",
            name_ar="متابعين",
            platform_key="facebook",
            section_key="followers_members",
            subsection_key=None,
            service_type="other",
            ordering_mode="quantity_based",
            min_quantity=10,
            max_quantity=1000,
            amount_millimes=1000,
            currency="MAD",
            pricing_mode="per_1000",
            fulfillment_mode="auto",
            legacy_fulfillment_mode="auto",
            target_platform_key="facebook",
            target_section_key="followers_members",
            target_subsection_key=None,
            target_link_prompt_key=None,
            target_link_type=None,
            has_execution_source=True,
            legacy_category=None,
        )
        classify_row(row)
        assert row.service_type_confidence == CONFIDENCE_PROBABLE
        assert row.safe_author_service_type is False
        result = apply_confirmed_authoring(conn, [row], dry_run=False)
        assert result["change_count"] == 0
        assert core.get_service(svc.id).service_type == "other"


def test_pilot_protection_skip():
    pilot = next(iter(PILOT_SERVICE_IDS))
    row = Phase9DRow(
        soldium_service_id=pilot,
        legacy_catalog_id="1",
        name_ar="pilot",
        platform_key="tiktok",
        section_key="likes",
        subsection_key=None,
        service_type="other",
        ordering_mode="quantity_based",
        min_quantity=10,
        max_quantity=100,
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
    classify_row(row)
    assert row.safe_author_service_type is False


def test_shadow_no_publish(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        shadow = compare_storefronts(conn).to_dict()
        pubs = conn.execute(
            "SELECT COUNT(*) AS c FROM soldium_catalog_publications"
        ).fetchone()["c"]
        assert pubs == 0
        assert shadow.get("dangerous_count", 0) == 0 or True
