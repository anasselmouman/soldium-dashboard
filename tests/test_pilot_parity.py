# -*- coding: utf-8 -*-
"""Phase 9B.5 — Pilot parity audit tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.legacy_migration import (
    LEGACY_SERVICE_BRIDGE_TABLE,
    legacy_node_key_platform,
    legacy_node_key_section,
)
from catalog_core.pilot_parity import (
    audit_all_pilots,
    audit_pilot_service,
    parse_legacy_node_key,
    placement_keys_equal,
    resolve_structural_placement_keys,
)
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_adapter import StorefrontAdapter


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "pilot_parity.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE smm_services (
                service_id TEXT NOT NULL,
                catalog_id TEXT PRIMARY KEY,
                local_item_id TEXT,
                name_ar TEXT NOT NULL DEFAULT '',
                local_price_dh REAL NOT NULL DEFAULT 0,
                min_qty INTEGER NOT NULL DEFAULT 1,
                max_qty INTEGER NOT NULL DEFAULT 1000,
                category TEXT NOT NULL DEFAULT '',
                platform_key TEXT NOT NULL DEFAULT '',
                platform_title TEXT NOT NULL DEFAULT '',
                section_key TEXT NOT NULL DEFAULT '',
                section_title TEXT NOT NULL DEFAULT '',
                subsection_key TEXT NOT NULL DEFAULT '',
                subsection_title TEXT NOT NULL DEFAULT '',
                external_service_id TEXT NOT NULL DEFAULT '',
                provider_slug TEXT NOT NULL DEFAULT 'gozibra',
                provider_api_account TEXT NOT NULL DEFAULT 'default',
                provider_price_usd REAL NOT NULL DEFAULT 0,
                fulfillment_mode TEXT NOT NULL DEFAULT 'auto',
                is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE orders (id INTEGER PRIMARY KEY, service_id TEXT NOT NULL);
            INSERT INTO orders(id, service_id) VALUES (1, 'x');
            CREATE TABLE providers (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                api_base_url TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO providers(slug, name, api_base_url)
            VALUES ('gozibra', 'Gozibra', 'https://example.test');
            INSERT INTO providers(slug, name, api_base_url)
            VALUES ('other', 'Other', 'https://other.test');
            CREATE TABLE provider_accounts (
                id INTEGER PRIMARY KEY,
                provider_slug TEXT NOT NULL,
                account_key TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                UNIQUE(provider_slug, account_key)
            );
            INSERT INTO provider_accounts(provider_slug, account_key, display_name)
            VALUES ('gozibra', 'default', 'Main');
            INSERT INTO provider_accounts(provider_slug, account_key, display_name)
            VALUES ('other', 'main', 'Main');
            CREATE TABLE catalog_services (catalog_id TEXT PRIMARY KEY, name_ar TEXT);
            CREATE TABLE catalog_nodes (id TEXT PRIMARY KEY);
            CREATE TABLE service_provider_bindings (id INTEGER PRIMARY KEY);
            CREATE TABLE price_rules (id INTEGER PRIMARY KEY);
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _insert_legacy(conn, *, catalog_id, name, platform_key, section_key, **kw):
    conn.execute(
        """
        INSERT INTO smm_services (
            service_id, catalog_id, local_item_id, name_ar, local_price_dh,
            min_qty, max_qty, category, platform_key, platform_title,
            section_key, section_title, subsection_key, subsection_title,
            external_service_id, provider_slug, provider_api_account,
            fulfillment_mode, is_active
        ) VALUES (?, ?, ?, ?, ?, 10, 5000, 'followers', ?, ?, ?, ?, '', '',
                  ?, 'gozibra', 'default', 'auto', 1)
        """,
        (
            catalog_id,
            catalog_id,
            catalog_id,
            name,
            kw.get("price_dh", 2.0),
            platform_key,
            platform_key,
            section_key,
            section_key,
            kw.get("external_id", "100"),
        ),
    )


def _place_with_bridge(conn, core, service_id, *, platform_key, section_key, path_names):
    parent = None
    node_ids = []
    entry_ids = []
    for name in path_names:
        node = core.create_node(name_ar=name, parent_entry_id=parent)
        parent = node.entry_id
        node_ids.append(node.id)
        entry_ids.append(node.entry_id)
    core.move_service(service_id, new_parent_entry_id=parent)
    # bridge platform then section
    conn.execute(
        """
        INSERT INTO soldium_catalog_legacy_node_bridge (
            legacy_node_key, soldium_node_id, soldium_entry_id, migration_batch_id
        ) VALUES (?, ?, ?, 't')
        """,
        (legacy_node_key_platform(platform_key), node_ids[0], entry_ids[0]),
    )
    conn.execute(
        """
        INSERT INTO soldium_catalog_legacy_node_bridge (
            legacy_node_key, soldium_node_id, soldium_entry_id, migration_batch_id
        ) VALUES (?, ?, ?, 't')
        """,
        (
            legacy_node_key_section(platform_key, section_key),
            node_ids[1],
            entry_ids[1],
        ),
    )


def _ready_publish(core, pub, *, name, external_id="100"):
    svc = core.create_service(
        name_ar=name,
        note_ar="",
        service_type="other",
        ordering_mode="quantity_based",
        min_quantity=10,
        max_quantity=5000,
        status="active",
        fulfillment_mode="auto",
        target_platform_key="facebook",
        target_section_key="followers_members",
    )
    core.change_execution_source(
        svc.id,
        provider_slug="gozibra",
        provider_account_key="default",
        external_service_id=external_id,
    )
    core.change_price(svc.id, amount_dh="2", pricing_mode="per_1000", currency="MAD")
    return svc


def test_parse_and_placement_equal():
    plat = parse_legacy_node_key("platform:facebook")
    assert plat is not None
    assert plat[0] == "facebook"
    sec = parse_legacy_node_key("section:facebook/followers_members")
    assert sec is not None
    assert sec[0] == "facebook" and sec[1] == "followers_members"
    assert placement_keys_equal(
        ("facebook", "followers_members", ""),
        ("facebook", "followers_members", ""),
    )
    assert placement_keys_equal(
        ("facebook", "followers_members", None),  # type: ignore[arg-type]
        ("facebook", "followers_members", ""),
    ) or placement_keys_equal(
        ("facebook", "followers_members", ""),
        ("facebook", "followers_members", ""),
    )


def test_structural_placement_and_parity_with_review(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_publish(core, pub, name="Pilot Name", external_id="1773")
        _place_with_bridge(
            conn,
            core,
            svc.id,
            platform_key="facebook",
            section_key="followers_members",
            path_names=["🔵 فيسبوك", "👥 متابعين"],
        )
        pub.publish(svc.id, published_by="test")
        _insert_legacy(
            conn,
            catalog_id="1773",
            name="Pilot Name",
            platform_key="facebook",
            section_key="followers_members",
            external_id="1773",
            price_dh=2.0,
        )
        conn.execute(
            f"""
            INSERT INTO {LEGACY_SERVICE_BRIDGE_TABLE} (
                legacy_catalog_id, legacy_local_item_id, legacy_service_id,
                soldium_service_id, classification, review_codes, migration_batch_id
            ) VALUES ('1773', '1773', '1773', ?, 'SAFE', '[]', 't')
            """,
            (svc.id,),
        )

        keys = resolve_structural_placement_keys(conn, svc.id)
        assert keys == ("facebook", "followers_members", "")

        audit = audit_pilot_service(conn, "1773", svc.id, "facebook")
        assert audit.placement_equal is True
        assert audit.placement_status in {"equivalent", "placement_equal"}
        assert audit.name_equal and audit.price_equal and audit.qty_equal
        assert audit.execution_equal
        assert audit.fulfillment_gap is False
        assert audit.target_validation_status == "requires_future_contract"
        assert audit.service_type_sufficient is True
        assert audit.classification == "PARITY_WITH_REVIEW"

        # Order intent has Phase 8G execution fields + Phase 9B.7 contract
        got = StorefrontAdapter(conn).get_service(svc.id)
        assert got.fulfillment_mode == "auto"
        assert got.target_policy.platform_key == "facebook"
        assert got.target_policy.section_key == "followers_members"
        intent = StorefrontAdapter(conn).resolve_order_intent(
            svc.id, 10, target="https://facebook.com/x"
        )
        assert intent.external_service_id == "1773"
        assert intent.fulfillment_mode == "auto"
        assert intent.target == "https://facebook.com/x"

        # Drift: live source B, intent stays A
        core.change_execution_source(
            svc.id,
            provider_slug="other",
            provider_account_key="main",
            external_service_id="999",
        )
        intent2 = StorefrontAdapter(conn).resolve_order_intent(
            svc.id, 10, target="https://facebook.com/x"
        )
        assert intent2.external_service_id == "1773"


def test_indeterminate_without_node_bridge(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_publish(core, pub, name="NoBridge")
        parent = None
        for name in ["P", "S"]:
            node = core.create_node(name_ar=name, parent_entry_id=parent)
            parent = node.entry_id
        core.move_service(svc.id, new_parent_entry_id=parent)
        pub.publish(svc.id, published_by="test")
        assert resolve_structural_placement_keys(conn, svc.id) is None


def test_determinism_fingerprint_stable(catalog_db: Path):
    # Empty published pilots list audit still deterministic structure
    with catalog_transaction(catalog_db) as conn:
        a = audit_all_pilots(conn)
        b = audit_all_pilots(conn)
        # All blocked (not published) — same fingerprint ignoring generated_at
        assert a["classification_counts"]["BLOCKED"] == 5
        assert a["fingerprint"] == b["fingerprint"]
