# -*- coding: utf-8 -*-
"""Catalog Single Source of Truth — customer storefront cutover tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from catalog_core.db import catalog_transaction
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_gateway import (
    CatalogStorefrontBackend,
    build_storefront,
    catalog_service_to_legacy_item,
    clear_storefront_cache,
    order_intent_to_create_bridge,
    resolve_storefront_backend_name,
    set_storefront_backend_override,
)
from catalog_core.storefront_projection import PublishedStorefrontProjection


@pytest.fixture(autouse=True)
def _clear_overrides():
    set_storefront_backend_override(None)
    clear_storefront_cache()
    yield
    set_storefront_backend_override(None)
    clear_storefront_cache()


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "catalog_sot.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE smm_services (
                catalog_id TEXT PRIMARY KEY,
                service_id TEXT NOT NULL DEFAULT '',
                name_ar TEXT NOT NULL DEFAULT 'legacy',
                local_price_dh REAL NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                platform_key TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO smm_services(catalog_id, service_id, name_ar)
            VALUES ('legacy-1', 'legacy-1', 'قديم');
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                service_id TEXT NOT NULL,
                catalog_id TEXT,
                soldium_service_id TEXT,
                amount REAL,
                provider_cost_dh REAL DEFAULT 0,
                external_service_id_snapshot TEXT
            );
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


def _ready_publish(
    core: CatalogCoreService,
    pub: CatalogPublicationService,
    *,
    name: str,
    path: list[str],
    amount_dh: str = "2",
    external_id: str = "1001",
):
    svc = core.create_service(
        name_ar=name,
        status="active",
        service_type="followers",
        ordering_mode="quantity_based",
        min_quantity=10,
        max_quantity=5000,
        fulfillment_mode="auto",
        target_platform_key="instagram",
        target_section_key="followers",
    )
    core.change_execution_source(
        svc.id,
        provider_slug="gozibra",
        provider_account_key="default",
        external_service_id=external_id,
    )
    core.change_price(svc.id, amount_dh=amount_dh, pricing_mode="per_1000", currency="MAD")
    _place(core, svc.id, path)
    pub.publish(svc.id, published_by="admin")
    return core.get_service(svc.id)


def test_default_backend_is_catalog():
    assert resolve_storefront_backend_name("") == "catalog"
    assert resolve_storefront_backend_name(None, environ={}) == "catalog"
    assert resolve_storefront_backend_name("legacy") == "legacy"
    assert resolve_storefront_backend_name("catalog") == "catalog"


def test_new_catalog_service_without_smm_row(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        before = conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0]
        svc = _ready_publish(core, pub, name="جديد", path=["منصة", "قسم"], external_id="4242")
        after = conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0]
        assert after == before  # no legacy projection created
        assert svc.id.startswith("svc_")
        proj = PublishedStorefrontProjection(conn)
        assert proj.get_service(svc.id).name_ar == "جديد"
        sf = CatalogStorefrontBackend(conn)
        assert sf.asserts_no_smm_services_access()
        tree = sf.navigation_tree()
        assert tree  # placed under Catalog root entry
        # Ensure no smm_services query path: tree ids are svc_*
        flat_ids = []
        for root in tree.values():
            for item in root.get("direct_items") or []:
                flat_ids.append(item["id"])
            for item in root.get("items") or []:
                flat_ids.append(item["id"])
            for sec in (root.get("sections") or {}).values():
                for item in sec.get("items") or []:
                    flat_ids.append(item["id"])
        assert svc.id in flat_ids


def test_live_edits_and_move_and_archive(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_publish(
            core, pub, name="أصل", path=["جذر", "فرع"], amount_dh="5", external_id="77"
        )
        core.update_service(svc.id, name_ar="محدث", min_quantity=20, max_quantity=900)
        core.change_price(svc.id, amount_dh="12", pricing_mode="per_1000", currency="MAD")
        core.change_execution_source(
            svc.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="888",
        )
        other = core.create_node(name_ar="مكان جديد", parent_entry_id=None)
        core.move_service(svc.id, new_parent_entry_id=other.entry_id)

        got = PublishedStorefrontProjection(conn).get_service(svc.id)
        assert got.name_ar == "محدث"
        assert got.amount_millimes == 12000
        assert got.min_quantity == 20
        assert got.max_quantity == 900
        assert got.execution.external_service_id == "888"
        assert got.platform_label == "مكان جديد"

        tree = CatalogStorefrontBackend(conn).navigation_tree()
        assert other.entry_id in tree

        core.archive_service(svc.id)
        assert PublishedStorefrontProjection(conn).list_services() == []

        core.restore_service(svc.id, status="active")
        assert PublishedStorefrontProjection(conn).get_service(svc.id).name_ar == "محدث"


def test_order_bridge_carries_catalog_identity(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_publish(
            core, pub, name="طلب", path=["P", "S"], amount_dh="10", external_id="5555"
        )
        sf = CatalogStorefrontBackend(conn)
        intent = sf.resolve_order_intent(svc.id, 1000, target="https://instagram.com/x")
        bridge = order_intent_to_create_bridge(intent, user_id=1, connection=conn)
        kwargs = bridge.to_create_kwargs()
        assert kwargs["service_id"] == svc.id
        assert kwargs["catalog_id"] == svc.id
        assert kwargs["soldium_service_id"] == svc.id
        assert kwargs["external_service_id_snapshot"] == "5555"
        assert kwargs["api_account"] == "default"
        assert kwargs["amount"] == 10.0  # 10 DH per 1000 * 1000 qty / 1000
        assert "provider_cost_dh" in kwargs


def test_build_storefront_default_catalog(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        sf = build_storefront(backend=None, connection=conn, environ={})
        assert isinstance(sf, CatalogStorefrontBackend)
        assert sf.backend_name == "catalog"


def test_legacy_item_mapping_has_no_smm_dependency(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_publish(core, pub, name="X", path=["A", "B"], external_id="9")
        mapped = catalog_service_to_legacy_item(
            CatalogStorefrontBackend(conn).get_service(svc.id)
        )
        assert mapped["id"] == svc.id
        assert mapped["soldium_service_id"] == svc.id
        assert mapped["catalog_id"] == svc.id
        assert mapped["provider_account"] == "default"
