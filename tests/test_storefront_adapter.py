# -*- coding: utf-8 -*-
"""Phase 9B.2 — StorefrontAdapter tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.pricing import quote_total_millimes
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_adapter import StorefrontAdapter, StorefrontAdapterError


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "storefront_adapter.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE smm_services (
                service_id TEXT PRIMARY KEY,
                name_ar TEXT NOT NULL DEFAULT 'legacy',
                local_price_dh REAL NOT NULL DEFAULT 99
            );
            INSERT INTO smm_services(service_id, name_ar, local_price_dh)
            VALUES ('legacy-1', 'قديم', 99);
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
            INSERT INTO catalog_services VALUES ('old', 'قديم');
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


def _ready_service(
    core: CatalogCoreService,
    *,
    name: str = "Svc",
    path: list[str] | None = None,
    external_id: str = "123",
    amount_dh: str = "2",
    pricing_mode: str = "per_1000",
    provider_slug: str = "gozibra",
    provider_account_key: str = "default",
    min_quantity: int = 10,
    max_quantity: int = 5000,
):
    svc = core.create_service(
        name_ar=name,
        note_ar="ملاحظة",
        service_type="followers",
        ordering_mode="quantity_based",
        min_quantity=min_quantity,
        max_quantity=max_quantity,
        status="active",
        fulfillment_mode="auto",
        target_platform_key="instagram",
        target_section_key="followers",
    )
    core.change_execution_source(
        svc.id,
        provider_slug=provider_slug,
        provider_account_key=provider_account_key,
        external_service_id=external_id,
    )
    core.change_price(
        svc.id, amount_dh=amount_dh, pricing_mode=pricing_mode, currency="MAD"
    )
    _place(core, svc.id, path or ["منصة", "قسم"])
    return core.get_service(svc.id)


def test_empty_publications_no_legacy_fallback(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        _ready_service(core, name="Ready but unpublished")
        adapter = StorefrontAdapter(conn)
        assert adapter.list_platforms() == []
        assert adapter.list_services() == []
        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == 1
        with pytest.raises(StorefrontAdapterError) as ei:
            adapter.resolve_order_intent("legacy-1", 100)
        assert ei.value.code == "service_unavailable"


def test_published_service_returned(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core, name="منشورة", external_id="123")
        pub.publish(svc.id, published_by="admin")
        adapter = StorefrontAdapter(conn)
        got = adapter.get_service(svc.id)
        assert got.service_id == svc.id
        assert got.service_id.startswith("svc_")
        assert got.name_ar == "منشورة"
        assert got.execution.external_service_id == "123"
        assert got.orderable is True


def test_unpublished_archived_readiness_excluded(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        a = _ready_service(core, name="A", external_id="1")
        b = _ready_service(core, name="B", external_id="2")
        c = _ready_service(core, name="C", external_id="3")
        pub.publish(a.id, published_by="admin")
        pub.publish(b.id, published_by="admin")
        pub.publish(c.id, published_by="admin")
        pub.unpublish(a.id, published_by="admin")
        core.archive_service(b.id)
        src = core.get_execution_source(c.id)
        assert src is not None
        conn.execute(
            """
            UPDATE soldium_catalog_execution_sources
            SET status='historical', ended_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (src.id,),
        )
        ids = {s.service_id for s in StorefrontAdapter(conn).list_services()}
        assert ids == set()


def test_execution_identity_from_live_catalog(catalog_db: Path):
    """After publish, live execution-source changes apply without republish."""
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core, external_id="123")
        pub.publish(svc.id, published_by="admin")
        core.change_execution_source(
            svc.id,
            provider_slug="other",
            provider_account_key="main",
            external_service_id="456",
        )
        adapter = StorefrontAdapter(conn)
        got = adapter.get_service(svc.id)
        assert got.execution.provider_slug == "other"
        assert got.execution.provider_account_key == "main"
        assert got.execution.external_service_id == "456"
        intent = adapter.resolve_order_intent(
            svc.id, 100, target="https://instagram.com/x"
        )
        assert intent.provider_slug == "other"
        assert intent.provider_account_key == "main"
        assert intent.external_service_id == "456"
        assert intent.target == "https://instagram.com/x"
        assert intent.fulfillment_mode == "auto"


def test_price_and_placement_from_live_catalog(catalog_db: Path):
    """Live name/price/placement edits are customer-visible without republish."""
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(
            core, name="الاسم", path=["تيك", "لايك"], amount_dh="3"
        )
        pub.publish(svc.id, published_by="admin")
        core.update_service(svc.id, name_ar="مسودة")
        core.change_price(svc.id, amount_dh="9", pricing_mode="per_1000", currency="MAD")
        other = core.create_node(name_ar="منصة أخرى", parent_entry_id=None)
        core.move_service(svc.id, new_parent_entry_id=other.entry_id)
        got = StorefrontAdapter(conn).get_service(svc.id)
        assert got.name_ar == "مسودة"
        assert got.price.amount_millimes == 9000
        assert got.platform_label == "منصة أخرى"
        assert got.section_label in (None, "")
        assert got.subsection_label in (None, "")


def test_quantity_validation(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core, min_quantity=10, max_quantity=100)
        pub.publish(svc.id, published_by="admin")
        adapter = StorefrontAdapter(conn)
        assert adapter.validate_quantity(svc.id, 50).ok is True
        low = adapter.validate_quantity(svc.id, 5)
        assert low.ok is False
        assert low.code == "quantity_below_minimum"
        high = adapter.validate_quantity(svc.id, 101)
        assert high.ok is False
        assert high.code == "quantity_above_maximum"


def test_pricing_modes_and_fixed_package(catalog_db: Path):
    assert quote_total_millimes(2000, "per_1000", 1000) == 2000
    assert quote_total_millimes(2000, "per_1000", 500) == 1000
    assert quote_total_millimes(1500, "per_unit", 3) == 4500

    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        per = _ready_service(
            core, name="Per", external_id="10", pricing_mode="per_1000", amount_dh="2"
        )
        fixed = _ready_service(
            core,
            name="Fixed",
            external_id="11",
            pricing_mode="fixed_package",
            amount_dh="5",
            min_quantity=1,
            max_quantity=1,
        )
        pub.publish(per.id, published_by="admin")
        pub.publish(fixed.id, published_by="admin")
        adapter = StorefrontAdapter(conn)
        quote = adapter.quote_price(per.id, 1000)
        assert quote.quoted_amount_millimes == 2000
        fsvc = adapter.get_service(fixed.id)
        assert fsvc.orderable is False
        assert fsvc.price.pricing_mode == "fixed_package"
        check = adapter.validate_quantity(fixed.id, 1)
        assert check.ok is False
        assert check.code == "unsupported_pricing_mode"
        with pytest.raises(StorefrontAdapterError) as ei:
            adapter.resolve_order_intent(fixed.id, 1)
        assert ei.value.code == "unsupported_pricing_mode"


def test_order_intent_no_mutation(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core, external_id="777")
        pub.publish(svc.id, published_by="admin")
        before_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        before_pubs = conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_publications"
        ).fetchone()[0]
        before_smm = conn.execute(
            "SELECT local_price_dh FROM smm_services WHERE service_id='legacy-1'"
        ).fetchone()[0]
        intent = StorefrontAdapter(conn).resolve_order_intent(
            svc.id, 1000, target="https://instagram.com/x"
        )
        assert intent.service_id == svc.id
        assert intent.external_service_id == "777"
        assert intent.quoted_amount_millimes == 2000
        assert intent.fulfillment_mode == "auto"
        assert intent.target == "https://instagram.com/x"
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == before_orders
        assert (
            conn.execute("SELECT COUNT(*) FROM soldium_catalog_publications").fetchone()[
                0
            ]
            == before_pubs
        )
        assert (
            conn.execute(
                "SELECT local_price_dh FROM smm_services WHERE service_id='legacy-1'"
            ).fetchone()[0]
            == before_smm
        )


def test_legacy_isolation_poison_row(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core, name="Catalog", external_id="555", amount_dh="2")
        pub.publish(svc.id, published_by="admin")
        # Mutate legacy catalog aggressively — adapter must ignore it
        conn.execute(
            "UPDATE smm_services SET name_ar=?, local_price_dh=? WHERE service_id=?",
            ("POISON", 12345.0, "legacy-1"),
        )
        adapter = StorefrontAdapter(conn)
        got = adapter.get_service(svc.id)
        assert got.name_ar == "Catalog"
        assert got.price.amount_millimes == 2000
        assert got.execution.external_service_id == "555"
        assert "POISON" not in got.name_ar


def test_fail_closed_unavailable_intent(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core)
        pub.publish(svc.id, published_by="admin")
        pub.unpublish(svc.id, published_by="admin")
        with pytest.raises(StorefrontAdapterError) as ei:
            StorefrontAdapter(conn).resolve_order_intent(svc.id, 100)
        assert ei.value.code == "service_unavailable"
