# -*- coding: utf-8 -*-
"""Phase 9B.4 — Controlled publication pilot tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.legacy_migration import LEGACY_SERVICE_BRIDGE_TABLE
from catalog_core.publication_pilot import (
    publish_pilot,
    select_pilot_candidates,
    validate_pilot_intents,
)
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_adapter import StorefrontAdapter
from catalog_core.storefront_projection import PublishedStorefrontProjection
from catalog_core.storefront_shadow import compare_storefronts


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "publication_pilot.db"
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
            INSERT INTO orders(id, service_id) VALUES (1, 'legacy-1');
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


def _insert_legacy(conn, *, catalog_id: str, name: str, platform_key: str, **kw):
    conn.execute(
        """
        INSERT INTO smm_services (
            service_id, catalog_id, local_item_id, name_ar, local_price_dh,
            min_qty, max_qty, category, platform_key, platform_title,
            section_key, section_title, subsection_key, subsection_title,
            external_service_id, provider_slug, provider_api_account,
            fulfillment_mode, is_active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sec', 'sec', '', '', ?, 'gozibra', 'default', 'auto', 1)
        """,
        (
            catalog_id,
            catalog_id,
            catalog_id,
            name,
            kw.get("price_dh", 2.0),
            kw.get("min_qty", 10),
            kw.get("max_qty", 5000),
            kw.get("category", "followers"),
            platform_key,
            platform_key,
            kw.get("external_id", "100"),
        ),
    )


def _place(core, service_id, path):
    parent = None
    for name in path:
        node = core.create_node(name_ar=name, parent_entry_id=parent)
        parent = node.entry_id
    core.move_service(service_id, new_parent_entry_id=parent)


def _make_ready(core, *, name, external_id, path):
    svc = core.create_service(
        name_ar=name,
        note_ar="",
        service_type="followers",
        ordering_mode="quantity_based",
        min_quantity=10,
        max_quantity=5000,
        status="active",
        fulfillment_mode="auto",
        target_platform_key=path[0] if path else "instagram",
        target_section_key=path[1] if len(path) > 1 else "followers",
    )
    core.change_execution_source(
        svc.id,
        provider_slug="gozibra",
        provider_account_key="default",
        external_service_id=external_id,
    )
    core.change_price(svc.id, amount_dh="2", pricing_mode="per_1000", currency="MAD")
    _place(core, svc.id, path)
    return core.get_service(svc.id)


def _bridge(conn, legacy_id, soldium_id):
    conn.execute(
        f"""
        INSERT INTO {LEGACY_SERVICE_BRIDGE_TABLE} (
            legacy_catalog_id, legacy_local_item_id, legacy_service_id,
            soldium_service_id, classification, review_codes, migration_batch_id
        ) VALUES (?, ?, ?, ?, 'SAFE', '[]', 'test')
        """,
        (legacy_id, legacy_id, legacy_id, soldium_id),
    )


def test_deterministic_selection_and_publish(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        platforms = ["tiktok", "instagram", "facebook"]
        for i, plat in enumerate(platforms):
            svc = _make_ready(
                core,
                name=f"Svc {plat}",
                external_id=str(1000 + i),
                path=[plat, "sec"],
            )
            _insert_legacy(
                conn,
                catalog_id=f"leg_{plat}",
                name=f"Svc {plat}",
                platform_key=plat,
                external_id=str(1000 + i),
            )
            _bridge(conn, f"leg_{plat}", svc.id)

        # Fourth legacy-only service keeps the fixture in pilot/partial scope.
        _insert_legacy(
            conn,
            catalog_id="leg_extra",
            name="Extra only",
            platform_key="youtube",
            external_id="9999",
        )

        a = select_pilot_candidates(conn, limit=3)
        b = select_pilot_candidates(conn, limit=3)
        assert [c.soldium_service_id for c in a] == [c.soldium_service_id for c in b]
        assert len(a) == 3
        assert {c.platform_key for c in a} == set(platforms)

        before_smm = conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0]
        before_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        before_prices = conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_prices"
        ).fetchone()[0]
        before_src = conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_execution_sources"
        ).fetchone()[0]

        dry = publish_pilot(conn, a, dry_run=True)
        assert dry.status == "DRY_RUN"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_publications"
            ).fetchone()[0]
            == 0
        )

        run = publish_pilot(conn, a, dry_run=False)
        assert run.status == "COMPLETE"
        assert len(run.published_ids) == 3
        pubs = conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_publications"
        ).fetchone()[0]
        assert pubs == 3

        proj = PublishedStorefrontProjection(conn).build()
        assert len(proj.services) == 3
        adapter_ids = {s.service_id for s in StorefrontAdapter(conn).list_services()}
        assert adapter_ids == set(run.published_ids)

        intents = validate_pilot_intents(conn, run.published_ids)
        assert len(intents) == 3
        for item in intents:
            assert item["ok"] is True
            assert item["intent"]["external_service_id"]
            assert item["intent"]["service_id"].startswith("svc_")
            assert item["intent"]["content_fingerprint"]

        shadow = compare_storefronts(conn, generated_at="t")
        assert shadow.baseline_state == "PARTIAL_PUBLICATION_PILOT"
        assert shadow.catalog_count == 3
        assert shadow.legacy_count == 4
        assert shadow.correlated_count == 3
        assert shadow.legacy_only_count == 1
        assert shadow.baseline_count == 1

        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == before_smm
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == before_orders
        assert (
            conn.execute("SELECT COUNT(*) FROM soldium_catalog_prices").fetchone()[0]
            == before_prices
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_execution_sources"
            ).fetchone()[0]
            == before_src
        )


def test_rejection_without_source(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = core.create_service(
            name_ar="NoSrc",
            service_type="followers",
            ordering_mode="quantity_based",
            min_quantity=1,
            max_quantity=10,
            status="active",
        )
        core.change_price(svc.id, amount_dh="2", pricing_mode="per_1000")
        _place(core, svc.id, ["P", "S"])
        _insert_legacy(conn, catalog_id="leg_nosrc", name="NoSrc", platform_key="p")
        _bridge(conn, "leg_nosrc", svc.id)
        selected = select_pilot_candidates(conn, limit=5)
        assert selected == []
