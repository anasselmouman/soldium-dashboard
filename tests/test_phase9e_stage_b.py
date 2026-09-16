# -*- coding: utf-8 -*-
"""Phase 9E Stage B — second-pilot publication tests (no production mutation)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.legacy_migration import LEGACY_SERVICE_BRIDGE_TABLE
from catalog_core.order_contract_republish import PILOT_SERVICE_IDS
from catalog_core.phase9e_stage_b import (
    APPROVED_LEGACY_IDS,
    EXPECTED_SEMANTICS,
    PUBLISHED_BY,
    StageBBlock,
    map_approved_pilots,
)
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_adapter import StorefrontAdapter
from catalog_core.storefront_projection import PublishedStorefrontProjection
from catalog_core.target_validation import validate_order_target


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "phase9e_b.db"
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


def _ready_service(
    conn,
    *,
    legacy_id: str,
    platform: str,
    section: str,
    service_type: str,
    subsection: str | None = None,
    external_id: str | None = None,
):
    core = CatalogCoreService(conn)
    parent = None
    for name in (platform, section):
        node = core.create_node(name_ar=name, parent_entry_id=parent)
        parent = node.entry_id
    if subsection:
        node = core.create_node(name_ar=subsection, parent_entry_id=parent)
        parent = node.entry_id
    svc = core.create_service(
        name_ar=f"{platform}-{section}",
        parent_entry_id=parent,
        service_type=service_type,
        ordering_mode="quantity_based",
        min_quantity=10,
        max_quantity=5000,
        status="active",
        fulfillment_mode="auto",
        target_platform_key=platform,
        target_section_key=section,
        target_subsection_key=subsection,
    )
    core.change_price(
        svc.id, amount_dh="2", pricing_mode="per_1000", currency="MAD"
    )
    core.change_execution_source(
        svc.id,
        provider_slug="gozibra",
        provider_account_key="tiktok",
        external_service_id=external_id or legacy_id,
    )
    conn.execute(
        """
        INSERT INTO smm_services(
          catalog_id, name_ar, platform_key, section_key, subsection_key,
          fulfillment_mode, provider_slug, provider_api_account,
          external_service_id, is_active
        ) VALUES (?, ?, ?, ?, ?, 'auto', 'gozibra', 'tiktok', ?, 1)
        """,
        (legacy_id, svc.name_ar, platform, section, subsection, external_id or legacy_id),
    )
    conn.execute(
        f"""
        INSERT INTO {LEGACY_SERVICE_BRIDGE_TABLE}(
          legacy_catalog_id, legacy_local_item_id, legacy_service_id,
          soldium_service_id, classification, review_codes, migration_batch_id,
          legacy_fulfillment_mode, provider_slug, provider_api_account,
          external_service_id
        ) VALUES (?, ?, ?, ?, 'SAFE', '[]', 'phase9e-b-test', 'auto',
                  'gozibra', 'tiktok', ?)
        """,
        (legacy_id, legacy_id, legacy_id, svc.id, external_id or legacy_id),
    )
    return svc


def test_approved_legacy_set_exact():
    assert APPROVED_LEGACY_IDS == (
        "1896",
        "4459",
        "1632",
        "2223",
        "4867",
        "2488",
        "4547",
        "1407",
    )
    assert len(APPROVED_LEGACY_IDS) == 8
    assert PUBLISHED_BY == "phase9e_second_pilot"


def test_expected_semantics_match_stage_a():
    assert EXPECTED_SEMANTICS["1896"]["service_type"] == "views"
    assert EXPECTED_SEMANTICS["4459"]["service_type"] == "followers"
    assert EXPECTED_SEMANTICS["1632"]["service_type"] == "likes"
    assert EXPECTED_SEMANTICS["2223"]["service_type"] == "shares"
    assert EXPECTED_SEMANTICS["4867"]["service_type"] == "views"
    assert EXPECTED_SEMANTICS["2488"]["service_type"] == "views"
    assert EXPECTED_SEMANTICS["4547"]["service_type"] == "live_viewers"
    assert EXPECTED_SEMANTICS["1407"]["service_type"] == "views"


def test_map_requires_exact_one_row(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        with pytest.raises(StageBBlock):
            map_approved_pilots(conn)


def test_publish_exactly_eight_no_unpublish(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        # Seed only the eight approved legacy ids with matching semantics.
        specs = [
            ("1896", "facebook", "video_reels_views", "views", "watchtime"),
            ("4459", "instagram", "followers", "followers", None),
            ("1632", "tiktok", "likes", "likes", "target_countries"),
            ("2223", "youtube", "geo_shares", "shares", None),
            ("4867", "telegram", "post_views", "views", "premium_views"),
            ("2488", "x", "video_views", "views", None),
            ("4547", "facebook", "live_stream_views", "live_viewers", None),
            ("1407", "tiktok", "views", "views", "target_countries"),
        ]
        svcs = []
        for legacy, plat, sec, stype, sub in specs:
            svcs.append(
                _ready_service(
                    conn,
                    legacy_id=legacy,
                    platform=plat,
                    section=sec,
                    service_type=stype,
                    subsection=sub,
                )
            )
        mapped = map_approved_pilots(conn)
        assert len(mapped) == 8
        assert {m["legacy_catalog_id"] for m in mapped} == set(APPROVED_LEGACY_IDS)

        pub = CatalogPublicationService(conn)
        for m in mapped:
            result = pub.publish(m["soldium_service_id"], published_by=PUBLISHED_BY)
            assert result.outcome in {"published", "no_change"}

        pubs = conn.execute(
            "SELECT COUNT(*) AS c FROM soldium_catalog_publications "
            "WHERE event_type='publish'"
        ).fetchone()["c"]
        assert pubs == 8
        unpubs = conn.execute(
            "SELECT COUNT(*) AS c FROM soldium_catalog_publications "
            "WHERE event_type='unpublish'"
        ).fetchone()["c"]
        assert unpubs == 0

        proj = PublishedStorefrontProjection(conn)
        assert len(proj.list_services()) == 8
        adapter = StorefrontAdapter(conn)
        for m in mapped:
            svc = adapter.get_service(m["soldium_service_id"])
            assert svc.fulfillment_mode == "auto"
            assert svc.execution.external_service_id == m["legacy_catalog_id"]


def test_publication_immutability_vs_draft_change(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = _ready_service(
            conn,
            legacy_id="1896",
            platform="facebook",
            section="video_reels_views",
            service_type="views",
            subsection="watchtime",
        )
        # Need all 8 for map_approved_pilots if we used that — publish directly.
        pub = CatalogPublicationService(conn)
        first = pub.publish(svc.id, published_by=PUBLISHED_BY)
        fp = first.publication["content_fingerprint"]
        core = CatalogCoreService(conn)
        core.update_service(svc.id, name_ar="CHANGED-DRAFT-NAME")
        latest = pub.get_latest_publish(svc.id)
        assert latest.content_fingerprint == fp
        assert latest.name_ar != "CHANGED-DRAFT-NAME"
        projected = PublishedStorefrontProjection(conn).get_service(svc.id)
        assert projected.name_ar == latest.name_ar
        adapted = StorefrontAdapter(conn).get_service(svc.id)
        assert adapted.name_ar == latest.name_ar


def test_legacy_isolation_after_publish(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = _ready_service(
            conn,
            legacy_id="4459",
            platform="instagram",
            section="followers",
            service_type="followers",
        )
        pub = CatalogPublicationService(conn)
        pub.publish(svc.id, published_by=PUBLISHED_BY)
        before = PublishedStorefrontProjection(conn).get_service(svc.id)
        conn.execute(
            "UPDATE smm_services SET name_ar='POISONED', external_service_id='99999' "
            "WHERE catalog_id='4459'"
        )
        after = PublishedStorefrontProjection(conn).get_service(svc.id)
        assert after.name_ar == before.name_ar
        assert after.execution.external_service_id == before.execution.external_service_id
        intent = StorefrontAdapter(conn).resolve_order_intent(
            svc.id, 10, target="https://instagram.com/x"
        )
        assert intent.external_service_id == before.execution.external_service_id


def test_target_validation_accept_reject():
    ok, _ = validate_order_target(
        "https://tiktok.com/@x",
        platform_key="tiktok",
        section_key="likes",
    )
    bad, _ = validate_order_target(
        "https://example.com/x",
        platform_key="tiktok",
        section_key="likes",
    )
    assert ok is True
    assert bad is False


def test_first_pilots_disjoint_from_second_pilot_ids():
    # Hard-coded Stage A second-pilot svc ids (from decision pack / bridge).
    second = {
        "svc_364798c5355e5597b28f6d6c5ae9db73",
        "svc_06764d3160945a199d35789136e3e964",
        "svc_019401d82af2596e8567445056709e7c",
        "svc_15a8e9b4db2054fd871239a2521a3e2e",
        "svc_a8f0649664095732b493da6f8794f689",
        "svc_2543c7161061580fac3dd59b7a36cdcb",
        "svc_62c5e703a61a56be9f59a9c6cc45ea7b",
        "svc_1bd6b1d55a9f50e88050ffe28ec29d14",
    }
    assert set(PILOT_SERVICE_IDS).isdisjoint(second)


def test_artifact_if_present_has_exact_eight():
    path = Path("scripts/out_phase9e_stage_b_publish.json")
    if not path.exists():
        pytest.skip("stage b artifact missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("verdict") != "PHASE 9E STAGE B COMPLETE — SECOND PILOT VERIFIED":
        pytest.skip("artifact is not a successful Stage B run")
    assert len(data["published_ids"]) == 8
    assert data["preflight"]["production_counts_before"]["publications"] == 15
    assert data["post_publish"]["production_counts_after"]["publications"] == 23
    assert data["post_publish"]["projection_count_after"] == 13
    assert data["post_publish"]["shadow"]["dangerous_count"] == 0
    assert data["post_publish"]["shadow"]["catalog_count"] == 13
    assert data["approved_legacy_ids"] == list(APPROVED_LEGACY_IDS)
    assert data["preflight"]["first_pilots_before"] == data["post_publish"][
        "first_pilots_after"
    ]
    # No orders created
    assert data["post_publish"]["production_counts_after"]["orders"] == 44
