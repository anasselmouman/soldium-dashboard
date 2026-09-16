# -*- coding: utf-8 -*-
"""Phase 9K — Wave 2 controlled publication tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.phase9h_wave1 import WAVE1_CANDIDATE_LEGACY_IDS
from catalog_core.phase9k_wave2 import (
    PUBLISHED_BY,
    WAVE2_LEGACY_IDS,
    map_candidates,
)
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_adapter import StorefrontAdapter
from catalog_core.storefront_projection import PublishedStorefrontProjection


def test_wave2_exact_membership_no_overlap_wave1():
    assert len(WAVE2_LEGACY_IDS) == 15
    assert len(set(WAVE2_LEGACY_IDS)) == 15
    assert set(WAVE2_LEGACY_IDS).isdisjoint(set(WAVE1_CANDIDATE_LEGACY_IDS))
    assert PUBLISHED_BY == "phase9k_wave2"
    expected = {
        "2003",
        "2484",
        "2038",
        "4554",
        "4865",
        "3044",
        "4806",
        "2035",
        "4553",
        "3043",
        "4691",
        "3604",
        "4552",
        "3042",
        "3550",
    }
    assert set(WAVE2_LEGACY_IDS) == expected


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "phase9k.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE providers (
                slug TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
                api_base_url TEXT NOT NULL DEFAULT '', is_active INTEGER DEFAULT 1
            );
            INSERT INTO providers(slug, name) VALUES ('gozibra', 'Gozibra');
            CREATE TABLE provider_accounts (
                id INTEGER PRIMARY KEY,
                provider_slug TEXT NOT NULL,
                account_key TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                UNIQUE(provider_slug, account_key)
            );
            INSERT INTO provider_accounts(provider_slug, account_key)
            VALUES ('gozibra', 'a1'), ('gozibra', 'a2');
            CREATE TABLE smm_services (
                catalog_id TEXT PRIMARY KEY,
                name_ar TEXT,
                platform_key TEXT,
                section_key TEXT,
                subsection_key TEXT,
                is_active INTEGER DEFAULT 1,
                local_price_dh REAL,
                min_qty INTEGER,
                max_qty INTEGER,
                provider_slug TEXT,
                provider_api_account TEXT,
                external_service_id TEXT,
                fulfillment_mode TEXT
            );
            CREATE TABLE orders (id INTEGER PRIMARY KEY, service_id TEXT);
            CREATE TABLE scheduled_orders (
                id INTEGER PRIMARY KEY,
                external_service_id TEXT
            );
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _ready_publish(conn, *, legacy_id: str, external: str, name="خدمة"):
    core = CatalogCoreService(conn)
    svc = core.create_service(
        name_ar=name,
        status="active",
        service_type="followers",
        ordering_mode="quantity_based",
        min_quantity=10,
        max_quantity=1000,
        fulfillment_mode="auto",
        target_platform_key="instagram",
        target_section_key="followers",
    )
    core.change_execution_source(
        svc.id,
        provider_slug="gozibra",
        provider_account_key="a1",
        external_service_id=external,
    )
    core.change_price(svc.id, amount_dh="2", pricing_mode="per_1000")
    conn.execute(
        """
        INSERT INTO smm_services(
            catalog_id, name_ar, platform_key, section_key, local_price_dh,
            min_qty, max_qty, provider_slug, provider_api_account,
            external_service_id, fulfillment_mode
        ) VALUES (?, ?, 'instagram', 'followers', 2.0, 10, 1000, 'gozibra', 'a1', ?, 'auto')
        """,
        (legacy_id, name, external),
    )
    conn.execute(
        """
        INSERT INTO soldium_catalog_legacy_bridge(
            legacy_catalog_id, legacy_local_item_id, legacy_service_id,
            soldium_service_id, provider_slug, external_service_id,
            provider_api_account, legacy_fulfillment_mode, classification,
            review_codes, migration_batch_id
        ) VALUES (?, ?, ?, ?, 'gozibra', ?, 'a1', 'auto', 'STANDARD', '[]', 'phase9k_test')
        """,
        (legacy_id, legacy_id, legacy_id, svc.id, external),
    )
    pub = CatalogPublicationService(conn)
    result = pub.publish(svc.id, published_by="phase9k_fixture")
    assert result.outcome == "published"
    return svc.id, result.publication["content_fingerprint"], result.publication["id"]


def test_immutability_after_live_edits(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        sid, fp, pub_id = _ready_publish(conn, legacy_id="W2L1", external="9001")
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        core.update_service(sid, name_ar="اسم جديد", service_type="likes")
        core.change_price(sid, amount_dh="9", pricing_mode="per_1000")
        core.update_service(sid, min_quantity=50, max_quantity=500)
        core.update_service(
            sid, fulfillment_mode="admin", target_section_key="likes"
        )
        core.change_execution_source(
            sid,
            provider_slug="gozibra",
            provider_account_key="a2",
            external_service_id="999-NEW",
        )
        latest = pub.get_latest_publish(sid)
        assert latest.id == pub_id
        assert latest.content_fingerprint == fp
        assert latest.name_ar != "اسم جديد"
        assert str(latest.external_service_id) == "9001"
        assert latest.fulfillment_mode == "auto"
        proj = PublishedStorefrontProjection(conn).get_service(sid)
        adapted = StorefrontAdapter(conn).get_service(sid)
        assert proj.content_fingerprint == fp
        assert adapted.content_fingerprint == fp
        assert str(proj.execution.external_service_id) == "9001"


def test_legacy_isolation(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        sid, fp, _ = _ready_publish(conn, legacy_id="W2L2", external="9002")
        conn.execute(
            "UPDATE smm_services SET name_ar=?, local_price_dh=?, external_service_id=? "
            "WHERE catalog_id='W2L2'",
            ("تم التخريب", 1.0, "HACKED"),
        )
        proj = PublishedStorefrontProjection(conn).get_service(sid)
        adapted = StorefrontAdapter(conn).get_service(sid)
        intent = StorefrontAdapter(conn).resolve_order_intent(
            sid, 10, target="https://instagram.com/u"
        )
        assert proj.content_fingerprint == fp
        assert adapted.content_fingerprint == fp
        assert intent.content_fingerprint == fp
        assert str(proj.execution.external_service_id) == "9002"
        assert str(intent.external_service_id) == "9002"


def test_idempotent_republish_no_new_event(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        sid, fp, pub_id = _ready_publish(conn, legacy_id="W2L3", external="9003")
        pub = CatalogPublicationService(conn)
        again = pub.publish(sid, published_by=PUBLISHED_BY)
        assert again.outcome == "no_change"
        n = conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_publications WHERE service_id=?",
            (sid,),
        ).fetchone()[0]
        assert int(n) == 1
        latest = pub.get_latest_publish(sid)
        assert latest.id == pub_id
        assert latest.content_fingerprint == fp


def test_artifact_wave2_complete():
    path = Path("scripts/out_phase9k_wave2_publication.json")
    if not path.exists():
        pytest.skip("9K artifact missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["verdict"] == (
        "PHASE_9K_WAVE_2_COMPLETE — CONTROLLED EXPANSION VERIFIED"
    )
    assert data["published"] is True
    assert data["published_by"] == "phase9k_wave2"
    sel = data["selection_report"]
    assert sel["wave_size"] == 15
    assert set(sel["legacy_ids"]) == set(WAVE2_LEGACY_IDS)
    assert data["preflight"]["all_fifteen_passed"] is True
    ver = data["verification"]
    assert ver["projection_count"] == 43
    assert ver["publications"] == 53
    assert ver["published_services"] == 43
    assert ver["existing_twenty_eight_unchanged"] is True
    assert ver["shadow"]["dangerous_count"] == 0
    assert ver["shadow"]["catalog_count"] == 43
    assert ver["shadow"]["correlated_count"] == 43
    assert ver["shadow"]["catalog_only_count"] == 0
    assert ver["shadow"]["legacy_count"] == 253
    assert data["idempotency_second_pass"]["noop_or_no_change"] is True
    assert data["orders_safety"]["unchanged"] is True
    assert data["scheduled_orders_safety"]["unchanged"] is True
    assert data["legacy_safety"]["unchanged"] is True
    assert data["provider_safety"]["unchanged"] is True
    assert data["production_invariants_ok"] is True
    # Provider ID first
    for row in ver["provider_service_ids"]:
        assert list(row.keys())[0] == "معرّف_المزود"
        assert isinstance(row["معرّف_المزود"], str)
        assert row["معرّف_المزود"]
    for row in sel["identity_table"]:
        assert list(row.keys())[0] == "معرّف_المزود"
