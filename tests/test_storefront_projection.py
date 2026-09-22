# -*- coding: utf-8 -*-
"""Phase 9B.1 — Published Storefront Projection tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.errors import CatalogNotFoundError
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_projection import PublishedStorefrontProjection


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "storefront_proj.db"
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
            INSERT INTO smm_services(service_id, name_ar) VALUES ('legacy-1', 'قديم');
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
    name: str,
    path: list[str],
    amount_dh: str = "2",
    external_id: str = "1001",
    pricing_mode: str = "per_1000",
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
        target_platform_key="instagram",
        target_section_key="followers",
    )
    core.change_execution_source(
        svc.id,
        provider_slug="gozibra",
        provider_account_key="default",
        external_service_id=external_id,
    )
    core.change_price(
        svc.id, amount_dh=amount_dh, pricing_mode=pricing_mode, currency="MAD"
    )
    _place(core, svc.id, path)
    return core.get_service(svc.id)


def test_no_publications_empty_projection(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        _ready_service(core, name="A", path=["منصة", "قسم"])
        proj = PublishedStorefrontProjection(conn)
        catalog = proj.build()
        assert catalog.services == []
        assert proj.list_platforms() == []
        assert proj.list_services() == []


def test_published_appears_unpublished_excluded(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        a = _ready_service(core, name="منشورة", path=["تيك", "لايك"])
        b = _ready_service(core, name="غير منشورة", path=["تيك", "لايك"], external_id="1002")
        pub.publish(a.id, published_by="admin")
        # b never published
        proj = PublishedStorefrontProjection(conn)
        services = proj.list_services()
        ids = {s.service_id for s in services}
        assert a.id in ids
        assert b.id not in ids
        assert a.id.startswith("svc_")
        got = proj.get_service(a.id)
        assert got.name_ar == "منشورة"
        assert got.execution.external_service_id == "1001"
        assert got.execution.external_service_id != a.id


def test_unpublish_removes_from_projection(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core, name="X", path=["P", "S"])
        pub.publish(svc.id, published_by="admin")
        proj = PublishedStorefrontProjection(conn)
        assert len(proj.list_services()) == 1
        pub.unpublish(svc.id, published_by="admin")
        assert proj.list_services() == []
        with pytest.raises(CatalogNotFoundError):
            proj.get_service(svc.id)


def test_latest_event_wins_publish_after_unpublish(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core, name="Y", path=["P", "S"])
        pub.publish(svc.id, published_by="a")
        pub.unpublish(svc.id, published_by="a")
        pub.publish(svc.id, published_by="a")
        assert len(PublishedStorefrontProjection(conn).list_services()) == 1


def test_archived_excluded(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core, name="Arch", path=["P", "S"])
        pub.publish(svc.id, published_by="admin")
        core.archive_service(svc.id)
        assert PublishedStorefrontProjection(conn).list_services() == []


def test_readiness_failure_excludes(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core, name="Ready", path=["P", "S"], external_id="2002")
        pub.publish(svc.id, published_by="admin")
        # End execution source → readiness fails while still "published"
        src = core.get_execution_source(svc.id)
        assert src is not None
        conn.execute(
            """
            UPDATE soldium_catalog_execution_sources
            SET status='historical', ended_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (src.id,),
        )
        assert PublishedStorefrontProjection(conn).list_services() == []


def test_live_edit_name_and_price_visible_without_republish(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(
            core, name="الاسم المنشور", path=["منصة", "قسم"], amount_dh="3"
        )
        pub.publish(svc.id, published_by="admin")
        # Live Catalog changes after publish must appear (publication = visibility only)
        core.update_service(svc.id, name_ar="اسم مسودة جديد")
        core.change_price(svc.id, amount_dh="9", pricing_mode="per_1000", currency="MAD")
        got = PublishedStorefrontProjection(conn).get_service(svc.id)
        assert got.name_ar == "اسم مسودة جديد"
        assert got.amount_millimes == 9000


def test_placement_from_live_catalog_tree(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(
            core, name="Svc", path=["تيك توك", "مشاهدات", "عادي"]
        )
        pub.publish(svc.id, published_by="admin")
        # Move live tree — customer placement follows live Catalog entries
        other = core.create_node(name_ar="منصة أخرى", parent_entry_id=None)
        core.move_service(svc.id, new_parent_entry_id=other.entry_id)
        got = PublishedStorefrontProjection(conn).get_service(svc.id)
        assert got.platform_label == "منصة أخرى"
        assert list(got.location_path) == ["منصة أخرى"]


def test_tree_listing_and_deterministic_order(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        s1 = _ready_service(core, name="Beta", path=["Z-Plat", "Sec"], external_id="1")
        s2 = _ready_service(core, name="Alpha", path=["A-Plat", "Sec"], external_id="2")
        s3 = _ready_service(
            core, name="Gamma", path=["A-Plat", "Sec", "Sub"], external_id="3"
        )
        for s in (s1, s2, s3):
            pub.publish(s.id, published_by="admin")
        proj = PublishedStorefrontProjection(conn)
        platforms = [p.label for p in proj.list_platforms()]
        assert platforms == ["A-Plat", "Z-Plat"]
        sections = [p.label for p in proj.list_sections("A-Plat")]
        assert sections == ["Sec"]
        subs = [p.label for p in proj.list_subsections("A-Plat", "Sec")]
        assert subs == ["Sub"]
        at_section = [s.name_ar for s in proj.list_services(platform_label="A-Plat", section_label="Sec")]
        assert at_section == ["Alpha"]  # Gamma has subsection
        at_sub = [
            s.name_ar
            for s in proj.list_services(
                platform_label="A-Plat", section_label="Sec", subsection_label="Sub"
            )
        ]
        assert at_sub == ["Gamma"]
        all_names = [s.name_ar for s in proj.list_services()]
        assert all_names == ["Alpha", "Beta", "Gamma"]


def test_price_and_limits_and_execution_from_live(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(
            core,
            name="Priced",
            path=["P", "S"],
            amount_dh="1.5",
            external_id="7788",
            pricing_mode="per_unit",
        )
        pub.publish(svc.id, published_by="admin")
        # Change live execution — projection must follow live Catalog
        core.change_execution_source(
            svc.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="9999",
        )
        got = PublishedStorefrontProjection(conn).get_service(svc.id)
        assert got.amount_millimes == 1500
        assert got.currency == "MAD"
        assert got.pricing_mode == "per_unit"
        assert got.min_quantity == 10
        assert got.max_quantity == 5000
        assert got.execution.external_service_id == "9999"
        assert got.fulfillment_mode == "auto"
        assert got.target_policy.platform_key == "instagram"
        assert got.target_policy.section_key == "followers"


def test_no_smm_services_fallback(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        # Legacy row exists in fixture; projection must stay empty without publish
        assert (
            conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == 1
        )
        catalog = PublishedStorefrontProjection(conn).build()
        assert catalog.services == []


def test_draft_status_excluded_even_if_published_history(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core, name="Drafty", path=["P", "S"], external_id="555")
        pub.publish(svc.id, published_by="admin")
        core.update_service(svc.id, status="draft")
        assert PublishedStorefrontProjection(conn).list_services() == []
        assert PublishedStorefrontProjection(conn).build().to_dict()["service_count"] == 0
