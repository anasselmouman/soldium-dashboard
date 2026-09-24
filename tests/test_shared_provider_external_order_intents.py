# -*- coding: utf-8 -*-
"""Two Catalog services may share one provider external ID for order intents.

Catalog storefront order path is snapshot-based (svc_* → execution source →
external_service_id_snapshot) and does not re-resolve smm_services by
(provider_slug, external_service_id). This test proves independent intents
without requiring duplicate bridged smm_services rows (UNIQUE remains).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from catalog_core.db import catalog_transaction
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_adapter import StorefrontAdapter
from catalog_core.storefront_gateway import order_intent_to_create_bridge


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "shared_external_intents.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE smm_services (
                service_id TEXT PRIMARY KEY,
                name_ar TEXT NOT NULL DEFAULT 'legacy'
            );
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


def _place(core: CatalogCoreService, service_id: str, path_names: list[str]) -> None:
    parent = None
    for name in path_names:
        node = core.create_node(name_ar=name, parent_entry_id=parent)
        parent = node.entry_id
    core.move_service(service_id, new_parent_entry_id=parent)


def _ready_shared_external(
    core: CatalogCoreService,
    *,
    name: str,
    external_id: str = "4210",
) -> str:
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
    core.change_price(created.id, amount_dh="2", pricing_mode="per_1000", currency="MAD")
    core.change_execution_source(
        created.id,
        provider_slug="gozibra",
        provider_account_key="default",
        external_service_id=external_id,
    )
    _place(core, created.id, ["منصة", "قسم"])
    return created.id


def test_two_catalog_services_same_provider_external_independent_intents(
    catalog_db: Path,
) -> None:
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc_a = _ready_shared_external(core, name="Service A")
        svc_b = _ready_shared_external(core, name="Service B")
        assert svc_a != svc_b
        assert svc_a.startswith("svc_")
        assert svc_b.startswith("svc_")

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
        assert intent_a.provider_slug == "gozibra"
        assert intent_b.provider_slug == "gozibra"
        assert intent_a.quoted_amount_millimes != intent_b.quoted_amount_millimes

        bridge_a = order_intent_to_create_bridge(intent_a, user_id=1, connection=conn)
        bridge_b = order_intent_to_create_bridge(intent_b, user_id=2, connection=conn)

        assert bridge_a.soldium_service_id == svc_a
        assert bridge_b.soldium_service_id == svc_b
        assert bridge_a.catalog_id == svc_a
        assert bridge_b.catalog_id == svc_b
        assert bridge_a.external_service_id_snapshot == "4210"
        assert bridge_b.external_service_id_snapshot == "4210"
        # Snapshots are independent create payloads — no smm_services pair lookup.
        kwargs_a = bridge_a.to_create_kwargs()
        kwargs_b = bridge_b.to_create_kwargs()
        assert kwargs_a["catalog_id"] == svc_a
        assert kwargs_b["catalog_id"] == svc_b
        assert kwargs_a["external_service_id_snapshot"] == "4210"
        assert kwargs_b["external_service_id_snapshot"] == "4210"
        assert kwargs_a["service_id"] != kwargs_b["service_id"]
