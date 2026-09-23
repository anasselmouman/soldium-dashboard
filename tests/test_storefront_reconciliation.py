# -*- coding: utf-8 -*-
"""Tests for Legacy → Catalog customer storefront reconciliation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from catalog_core.db import catalog_transaction
from catalog_core.legacy_migration import (
    LEGACY_SERVICE_BRIDGE_TABLE,
    legacy_node_key_platform,
    legacy_node_key_section,
)
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_gateway import CatalogStorefrontBackend
from catalog_core import storefront_reconciliation as recon
from catalog_core.storefront_reconciliation import (
    LEGACY_CUSTOMER_PLATFORM_LABELS,
    LEGACY_PLATFORM_BUTTON_ORDER,
)


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "storefront_recon.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE smm_services (
                catalog_id TEXT PRIMARY KEY,
                service_id TEXT,
                name_ar TEXT NOT NULL DEFAULT '',
                local_price_dh REAL NOT NULL DEFAULT 1,
                min_qty INTEGER NOT NULL DEFAULT 10,
                max_qty INTEGER NOT NULL DEFAULT 5000,
                is_active INTEGER NOT NULL DEFAULT 1,
                platform_key TEXT NOT NULL DEFAULT '',
                platform_title TEXT,
                section_key TEXT,
                section_title TEXT,
                subsection_key TEXT,
                subsection_title TEXT,
                provider_slug TEXT,
                provider_api_account TEXT,
                external_service_id TEXT,
                fulfillment_mode TEXT DEFAULT 'auto'
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
                display_name TEXT
            );
            INSERT INTO provider_accounts(provider_slug, account_key, display_name)
            VALUES ('gozibra', 'default', 'افتراضي');
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _insert_legacy(
    conn: sqlite3.Connection,
    *,
    catalog_id: str,
    name: str,
    platform_key: str = "instagram",
    section_key: str = "likes",
    is_active: int = 1,
    price: float = 2.0,
) -> None:
    conn.execute(
        """
        INSERT INTO smm_services (
            catalog_id, service_id, name_ar, local_price_dh, min_qty, max_qty,
            is_active, platform_key, platform_title, section_key, section_title,
            provider_slug, provider_api_account, external_service_id
        ) VALUES (?, ?, ?, ?, 10, 5000, ?, ?, ?, ?, ?, 'gozibra', 'default', ?)
        """,
        (
            catalog_id,
            catalog_id,
            name,
            price,
            is_active,
            platform_key,
            f"عنوان {platform_key}",
            section_key,
            f"قسم {section_key}",
            catalog_id,
        ),
    )


def _bridge(conn: sqlite3.Connection, legacy_catalog_id: str, soldium_service_id: str) -> None:
    conn.execute(
        f"""
        INSERT INTO {LEGACY_SERVICE_BRIDGE_TABLE} (
            legacy_catalog_id, legacy_local_item_id, legacy_service_id,
            soldium_service_id, classification, review_codes, migration_batch_id
        ) VALUES (?, ?, ?, ?, 'SAFE', '[]', 'test_batch')
        """,
        (legacy_catalog_id, legacy_catalog_id, legacy_catalog_id, soldium_service_id),
    )


def _node_bridge(
    conn: sqlite3.Connection,
    *,
    legacy_node_key: str,
    node_id: str,
    entry_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO soldium_catalog_legacy_node_bridge (
            legacy_node_key, soldium_node_id, soldium_entry_id, migration_batch_id
        ) VALUES (?, ?, ?, 'test_batch')
        """,
        (legacy_node_key, node_id, entry_id),
    )


def _ready_unpublished(
    core: CatalogCoreService,
    *,
    name: str,
    platform_key: str = "instagram",
    section_key: str = "likes",
    external_id: str = "1001",
):
    svc = core.create_service(
        name_ar=name,
        note_ar="",
        service_type="followers",
        ordering_mode="quantity_based",
        min_quantity=10,
        max_quantity=5000,
        status="active",
        fulfillment_mode="auto",
        target_platform_key=platform_key,
        target_section_key=section_key,
    )
    core.change_execution_source(
        svc.id,
        provider_slug="gozibra",
        provider_account_key="default",
        external_service_id=external_id,
    )
    core.change_price(svc.id, amount_dh="2", pricing_mode="per_1000", currency="MAD")
    root = core.create_node(name_ar=f"خدمات {platform_key}")
    sec = core.create_node(name_ar=f"قسم {section_key}", parent_entry_id=root.entry_id)
    core.move_service(svc.id, new_parent_entry_id=sec.entry_id)
    return core.get_service(svc.id), root, sec


def test_platform_labels_written_to_catalog_nodes(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        root = core.create_node(name_ar="خدمات فيسبوك")
        _node_bridge(
            conn,
            legacy_node_key=legacy_node_key_platform("facebook"),
            node_id=root.id,
            entry_id=root.entry_id,
        )
        updates = recon.reconcile_platform_labels(conn, dry_run=False)
        changed = [u for u in updates if u.get("legacy_node_key") == "platform:facebook"]
        assert changed and changed[0]["after"] == LEGACY_CUSTOMER_PLATFORM_LABELS["facebook"]
        again = core.get_node(root.id)
        assert again.name_ar == LEGACY_CUSTOMER_PLATFORM_LABELS["facebook"]


def test_legacy_visible_ready_becomes_published(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc, root, sec = _ready_unpublished(core, name="لايك اقتصادي", external_id="9001")
        _insert_legacy(conn, catalog_id="9001", name="لايك اقتصادي")
        _bridge(conn, "9001", svc.id)
        _node_bridge(
            conn,
            legacy_node_key=legacy_node_key_platform("instagram"),
            node_id=root.id,
            entry_id=root.entry_id,
        )
        _node_bridge(
            conn,
            legacy_node_key=legacy_node_key_section("instagram", "likes"),
            node_id=sec.id,
            entry_id=sec.entry_id,
        )

        results, blocked = recon.reconcile_publications(conn, dry_run=False)
        assert not blocked
        assert any(r["outcome"] == "published" and r["legacy_catalog_id"] == "9001" for r in results)
        assert pub.get_publication_status(svc.id).get("publication_status") == "published"

        tree = CatalogStorefrontBackend(conn).navigation_tree()
        assert root.entry_id in tree
        flat = recon._flatten_service_ids(tree)
        assert svc.id in flat


def test_legacy_hidden_not_published(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc, root, sec = _ready_unpublished(core, name="مخفي", external_id="9002")
        _insert_legacy(conn, catalog_id="9002", name="مخفي", is_active=0)
        _bridge(conn, "9002", svc.id)
        results, blocked = recon.reconcile_publications(conn, dry_run=False)
        assert all(r.get("legacy_catalog_id") != "9002" for r in results)
        assert CatalogPublicationService(conn).get_publication_status(svc.id).get(
            "publication_status"
        ) != "published"


def test_catalog_only_service_still_supported_when_published(catalog_db: Path):
    """Catalog-only (no Legacy bridge) remains publishable independently."""
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc, root, _sec = _ready_unpublished(core, name="كتالوج فقط", external_id="777")
        pub.publish(svc.id, published_by="admin")
        tree = CatalogStorefrontBackend(conn).navigation_tree()
        assert root.entry_id in tree
        assert svc.id in recon._flatten_service_ids(tree)


def test_order_node_callback_byte_length():
    entry_id = "ent_" + ("a" * 32)
    cb = f"order:node:{entry_id}"
    assert len(cb.encode("utf-8")) <= 64
    assert cb.startswith("order:node:")


def test_parity_report_after_full_reconcile(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc, root, sec = _ready_unpublished(core, name="خدمة", external_id="42")
        _insert_legacy(conn, catalog_id="42", name="خدمة")
        _bridge(conn, "42", svc.id)
        _node_bridge(
            conn,
            legacy_node_key=legacy_node_key_platform("instagram"),
            node_id=root.id,
            entry_id=root.entry_id,
        )
        _node_bridge(
            conn,
            legacy_node_key=legacy_node_key_section("instagram", "likes"),
            node_id=sec.id,
            entry_id=sec.entry_id,
        )

        def legacy_tree():
            return {
                "instagram": {
                    "title": "إنستغرام",
                    "sections": {
                        "likes": {
                            "title": "لايك",
                            "items": [{"id": "42", "name": "خدمة", "price": 2.0}],
                        }
                    },
                }
            }

        report = recon.run_reconciliation(
            conn,
            dry_run=False,
            legacy_tree_loader=legacy_tree,
            create_backup=False,
        )
        assert report.parity["missing_in_catalog_count"] == 0
        assert report.parity["catalog_visible_services"] == 1
        assert report.after["projected_services"] == 1
        # Label applied on present root
        titles = report.after["catalog_root_titles"]
        assert titles[root.entry_id] == LEGACY_CUSTOMER_PLATFORM_LABELS["instagram"]


def test_platform_button_order_constants_match_legacy_menu():
    assert LEGACY_PLATFORM_BUTTON_ORDER == [
        "instagram",
        "facebook",
        "tiktok",
        "youtube",
        "telegram",
        "x",
        "subscriptions",
    ]
