# -*- coding: utf-8 -*-
"""Two bridged Catalog services may share the same provider external ID."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from catalog_core.db import catalog_transaction
from catalog_core.legacy_migration import LEGACY_SERVICE_BRIDGE_TABLE
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_adapter import StorefrontAdapter
from catalog_core.storefront_gateway import order_intent_to_create_bridge


@pytest.fixture
def dual_db(tmp_path: Path) -> Path:
    path = tmp_path / "dual_bridged_shared_sku.db"
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
            CREATE INDEX IF NOT EXISTS idx_smm_services_provider_external
            ON smm_services (provider_slug, external_service_id);
            CREATE TABLE orders (id INTEGER PRIMARY KEY, service_id TEXT NOT NULL);
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
            VALUES ('gozibra', 'default', 'Main');
            INSERT INTO provider_accounts(provider_slug, account_key, display_name)
            VALUES ('gozibra', 'instagram', 'IG');
            CREATE TABLE catalog_services (catalog_id TEXT PRIMARY KEY, name_ar TEXT);
            CREATE TABLE catalog_nodes (id TEXT PRIMARY KEY);
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _place(core: CatalogCoreService, service_id: str) -> None:
    parent = None
    for name in ("منصة", "قسم"):
        node = core.create_node(name_ar=name, parent_entry_id=parent)
        parent = node.entry_id
    core.move_service(service_id, new_parent_entry_id=parent)


def _seed_bridged_pair(
    conn: sqlite3.Connection,
    *,
    legacy_id: str,
    name: str,
    external_id: str = "4210",
    local_price_dh: float = 3.0,
) -> str:
    conn.execute(
        """
        INSERT INTO smm_services (
            service_id, catalog_id, local_item_id, name_ar, local_price_dh,
            min_qty, max_qty, category, platform_key, platform_title,
            section_key, section_title, subsection_key, subsection_title,
            external_service_id, provider_slug, provider_api_account,
            provider_price_usd, fulfillment_mode, is_active
        ) VALUES (
            ?, ?, ?, ?, ?,
            10, 100000, 'default', 'instagram', 'Instagram',
            'likes', 'Likes', '', '',
            ?, 'gozibra', 'default',
            0.5, 'auto', 1
        )
        """,
        (legacy_id, legacy_id, legacy_id, name, local_price_dh, external_id),
    )
    core = CatalogCoreService(conn)
    created = core.create_service(
        name_ar=name,
        note_ar="note",
        status="active",
        min_quantity=10,
        max_quantity=100000,
        fulfillment_mode="auto",
        service_type="likes",
        ordering_mode="quantity_based",
        target_platform_key="instagram",
        target_section_key="likes",
    )
    core.change_price(created.id, amount_dh=str(local_price_dh), pricing_mode="per_1000")
    core.change_execution_source(
        created.id,
        provider_slug="gozibra",
        provider_account_key="default",
        external_service_id=external_id,
    )
    _place(core, created.id)
    conn.execute(
        f"""
        INSERT INTO {LEGACY_SERVICE_BRIDGE_TABLE} (
            legacy_catalog_id, legacy_local_item_id, legacy_service_id,
            soldium_service_id, provider_slug, external_service_id,
            provider_api_account, legacy_fulfillment_mode,
            classification, review_codes, migration_batch_id
        ) VALUES (?, ?, ?, ?, 'gozibra', ?, 'default', 'auto',
                  'SAFE', '[]', 'dual-share')
        """,
        (legacy_id, legacy_id, legacy_id, created.id, external_id),
    )
    return created.id


def test_dual_bridged_share_external_write_through_isolation(dual_db: Path) -> None:
    with catalog_transaction(dual_db) as conn:
        svc_a = _seed_bridged_pair(conn, legacy_id="A", name="Service A", local_price_dh=3.0)
        svc_b = _seed_bridged_pair(conn, legacy_id="B", name="Service B", local_price_dh=5.0)
        assert svc_a != svc_b

        twins = conn.execute(
            """
            SELECT catalog_id, external_service_id FROM smm_services
            WHERE provider_slug='gozibra' AND external_service_id='4210'
            ORDER BY catalog_id
            """
        ).fetchall()
        assert [r["catalog_id"] for r in twins] == ["A", "B"]

        # Write-through for A updates only A.
        CatalogCoreService(conn).update_service(svc_a, name_ar="A-updated")
        CatalogCoreService(conn).change_price(svc_a, amount_dh="9.5", pricing_mode="per_1000")
        a = conn.execute(
            "SELECT name_ar, local_price_dh FROM smm_services WHERE catalog_id='A'"
        ).fetchone()
        b = conn.execute(
            "SELECT name_ar, local_price_dh FROM smm_services WHERE catalog_id='B'"
        ).fetchone()
        assert a["name_ar"] == "A-updated"
        assert float(a["local_price_dh"]) == 9.5
        assert b["name_ar"] == "Service B"
        assert float(b["local_price_dh"]) == 5.0

        # Write-through for B updates only B.
        CatalogCoreService(conn).update_service(svc_b, name_ar="B-updated")
        b2 = conn.execute(
            "SELECT name_ar FROM smm_services WHERE catalog_id='B'"
        ).fetchone()
        a2 = conn.execute(
            "SELECT name_ar FROM smm_services WHERE catalog_id='A'"
        ).fetchone()
        assert b2["name_ar"] == "B-updated"
        assert a2["name_ar"] == "A-updated"


def test_remap_cases_bridged_shared_external(dual_db: Path) -> None:
    with catalog_transaction(dual_db) as conn:
        svc_a = _seed_bridged_pair(conn, legacy_id="A", name="A")
        svc_b = _seed_bridged_pair(conn, legacy_id="B", name="B")
        core = CatalogCoreService(conn)

        # CASE 2: A 4210 → 5000; B stays 4210
        r2 = core.change_execution_source(
            svc_a,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="5000",
        )
        assert r2.unchanged is False
        assert r2.legacy_write_through["applied"] is True
        assert (
            str(
                conn.execute(
                    "SELECT external_service_id FROM smm_services WHERE catalog_id='A'"
                ).fetchone()["external_service_id"]
            )
            == "5000"
        )
        assert (
            str(
                conn.execute(
                    "SELECT external_service_id FROM smm_services WHERE catalog_id='B'"
                ).fetchone()["external_service_id"]
            )
            == "4210"
        )

        # CASE 3: A 5000 → 4210 (share again with B) — must succeed
        r3 = core.change_execution_source(
            svc_a,
            provider_slug="gozibra",
            provider_account_key="instagram",
            external_service_id="4210",
        )
        assert r3.unchanged is False
        assert r3.current.external_service_id == "4210"
        assert r3.legacy_write_through["applied"] is True

        twins = conn.execute(
            """
            SELECT catalog_id, provider_api_account, external_service_id
            FROM smm_services
            WHERE provider_slug='gozibra' AND external_service_id='4210'
            ORDER BY catalog_id
            """
        ).fetchall()
        assert [r["catalog_id"] for r in twins] == ["A", "B"]
        assert twins[0]["provider_api_account"] == "instagram"
        assert twins[1]["provider_api_account"] == "default"

        # CASE 1 / 4: both remain on 4210, both active
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM smm_services WHERE is_active=1 AND external_service_id='4210'"
            ).fetchone()[0]
            == 2
        )
        # Touch B must not clobber A's account
        core.change_price(svc_b, amount_dh="7", pricing_mode="per_1000")
        a_acct = conn.execute(
            "SELECT provider_api_account, local_price_dh FROM smm_services WHERE catalog_id='A'"
        ).fetchone()
        assert a_acct["provider_api_account"] == "instagram"
        assert float(a_acct["local_price_dh"]) == 3.0


def test_two_services_shared_external_independent_order_snapshots(dual_db: Path) -> None:
    with catalog_transaction(dual_db) as conn:
        svc_a = _seed_bridged_pair(conn, legacy_id="A", name="A", local_price_dh=2.0)
        svc_b = _seed_bridged_pair(conn, legacy_id="B", name="B", local_price_dh=4.0)
        pub = CatalogPublicationService(conn)
        pub.publish(svc_a, published_by="admin")
        pub.publish(svc_b, published_by="admin")

        adapter = StorefrontAdapter(conn)
        intent_a = adapter.resolve_order_intent(
            svc_a, 100, target="https://instagram.com/a"
        )
        intent_b = adapter.resolve_order_intent(
            svc_b, 200, target="https://instagram.com/b"
        )
        assert intent_a.service_id == svc_a
        assert intent_b.service_id == svc_b
        assert intent_a.external_service_id == "4210"
        assert intent_b.external_service_id == "4210"

        bridge_a = order_intent_to_create_bridge(intent_a, user_id=1, connection=conn)
        bridge_b = order_intent_to_create_bridge(intent_b, user_id=2, connection=conn)
        kwargs_a = bridge_a.to_create_kwargs()
        kwargs_b = bridge_b.to_create_kwargs()
        assert kwargs_a["catalog_id"] == svc_a
        assert kwargs_b["catalog_id"] == svc_b
        assert kwargs_a["external_service_id_snapshot"] == "4210"
        assert kwargs_b["external_service_id_snapshot"] == "4210"
        assert kwargs_a["provider_slug"] == "gozibra"
        assert kwargs_b["provider_slug"] == "gozibra"
        # Frozen snapshots — no smm_services provider/external resolve at create.
        assert "smm_services" not in str(kwargs_a)
        assert kwargs_a["service_id"] != kwargs_b["service_id"]
