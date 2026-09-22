# -*- coding: utf-8 -*-
"""Phase 9I — expansion validation tests (isolated fixtures + artifact checks)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_adapter import StorefrontAdapter
from catalog_core.storefront_projection import PublishedStorefrontProjection


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "phase9i.db"
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
                price REAL,
                provider_slug TEXT,
                external_service_id TEXT
            );
            INSERT INTO smm_services(catalog_id, name_ar, platform_key, section_key, price)
            VALUES ('L1', 'قديم', 'instagram', 'followers', 9.99);
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


def _ready_publish(conn, *, name="خدمة", external="111"):
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
    pub = CatalogPublicationService(conn)
    result = pub.publish(svc.id, published_by="phase9i_test")
    assert result.outcome == "published"
    return svc.id, result.publication["content_fingerprint"], result.publication["id"]


def test_snapshot_immutability_after_live_edits(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        sid, fp, pub_id = _ready_publish(conn)
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        # Mutate live draft fields
        core.update_service(sid, name_ar="اسم جديد", service_type="likes")
        core.change_price(sid, amount_dh="9", pricing_mode="per_1000")
        core.update_service(sid, min_quantity=50, max_quantity=500)
        core.update_service(
            sid,
            fulfillment_mode="admin",
            target_section_key="likes",
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
        assert latest.amount_millimes != 9000
        assert str(latest.external_service_id) == "111"
        assert latest.fulfillment_mode == "auto"
        # Publication fingerprint stays; customer projection uses live Catalog
        proj = PublishedStorefrontProjection(conn).get_service(sid)
        adapted = StorefrontAdapter(conn).get_service(sid)
        assert proj.content_fingerprint == fp
        assert adapted.content_fingerprint == fp
        assert proj.name_ar == "اسم جديد"
        assert adapted.name_ar == "اسم جديد"
        assert proj.amount_millimes == 9000
        assert str(proj.execution.external_service_id) == "999-NEW"
        assert str(adapted.execution.external_service_id) == "999-NEW"
        st = pub.get_publication_status(sid)
        assert st["has_unpublished_changes"] is True


def test_execution_source_history_no_auto_republish(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        sid, fp, pub_id = _ready_publish(conn, external="OLD-ID")
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        result = core.change_execution_source(
            sid,
            provider_slug="gozibra",
            provider_account_key="a1",
            external_service_id="NEW-ID",
            changed_by="tester",
        )
        assert result.unchanged is False
        assert result.previous.external_service_id == "OLD-ID"
        assert result.current.external_service_id == "NEW-ID"
        hist = core.list_execution_source_history(sid)
        assert hist[0].status == "active"
        assert any(h.status == "historical" and h.external_service_id == "OLD-ID" for h in hist)
        events = core.list_execution_source_events(sid)
        assert any(e["new_external_service_id"] == "NEW-ID" for e in events)
        latest = pub.get_latest_publish(sid)
        assert latest.id == pub_id
        assert str(latest.external_service_id) == "OLD-ID"
        assert latest.content_fingerprint == fp
        # No new publication row
        n = conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_publications WHERE service_id=?",
            (sid,),
        ).fetchone()[0]
        assert int(n) == 1
        assert sid.startswith("svc_")


def test_legacy_isolation(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        sid, fp, _ = _ready_publish(conn, external="LEG-KEEP")
        conn.execute(
            "UPDATE smm_services SET name_ar=?, price=?, external_service_id=? WHERE catalog_id='L1'",
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
        assert str(proj.execution.external_service_id) == "LEG-KEEP"
        assert str(intent.external_service_id) == "LEG-KEEP"


def test_artifact_if_present():
    path = Path("scripts/out_phase9i_expansion_validation.json")
    if not path.exists():
        pytest.skip("9I artifact missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["verdict"] in {
        "EXPANSION_READY",
        "EXPANSION_READY_WITH_REMEDIATIONS",
        "EXPANSION_BLOCKED",
    }
    assert data["mutations"].startswith("NONE")
    assert data["production_unchanged"] is True
    assert data["production_baseline"]["published_services"] == 28
    assert data["production_baseline"]["publications"] == 38
    assert data["execution_identity_audit"]["mismatch_count"] == 0
    assert data["contracts"]["ok"] is True
    assert data["shadow"]["aggregate"]["dangerous_count"] == 0
    assert data["shadow"]["aggregate"]["catalog_count"] == 28
    assert data["order_intent_safety"]["unchanged"] is True
    # Provider Service ID present on inventory
    for row in data["published_inventory"]:
        assert row["provider_service_id"] is not None
        assert isinstance(row["provider_service_id"], str)
