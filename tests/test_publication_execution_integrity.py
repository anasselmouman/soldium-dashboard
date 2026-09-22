# -*- coding: utf-8 -*-
"""Publication / execution integrity (Catalog SoT customer projection).

Locks:

- Publication snapshots remain immutable (audit / history / fingerprint).
- Changing the live execution source does NOT rewrite the published snapshot.
- Customer-facing projection uses **live** Catalog execution/commercial data.
- Publication remains a visibility gate; live readiness gates eligibility.
- Removing/invalidating the live source drops eligibility / projection visibility.
- Projection never reads ``smm_services``.
"""

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
    path = tmp_path / "pub_exec_integrity.db"
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
    external_id: str = "123",
    provider_slug: str = "gozibra",
    provider_account_key: str = "default",
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
        provider_slug=provider_slug,
        provider_account_key=provider_account_key,
        external_service_id=external_id,
    )
    core.change_price(svc.id, amount_dh="2", pricing_mode="per_1000", currency="MAD")
    _place(core, svc.id, ["منصة", "قسم"])
    return core.get_service(svc.id)


def test_source_change_keeps_published_snapshot_live_projection(catalog_db: Path):
    """A→B: snapshot stays A; customer projection follows live B."""
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core, external_id="123")
        pub.publish(svc.id, published_by="admin")

        core.change_execution_source(
            svc.id,
            provider_slug="other",
            provider_account_key="main",
            external_service_id="999",
        )

        st = pub.get_publication_status(svc.id)
        assert st["publication_status"] == "published"
        assert st["has_unpublished_changes"] is True
        assert st["customer_catalog_eligible"] is True
        assert st["latest_publication"]["external_service_id"] == "123"
        assert st["latest_publication"]["provider_slug"] == "gozibra"

        live = core.get_execution_source(svc.id)
        assert live is not None
        assert live.external_service_id == "999"
        assert live.provider_slug == "other"

        got = PublishedStorefrontProjection(conn).get_service(svc.id)
        assert got.execution.provider_slug == "other"
        assert got.execution.provider_account_key == "main"
        assert got.execution.external_service_id == "999"
        assert set(got.execution.to_dict().keys()) == {
            "provider_slug",
            "provider_account_key",
            "external_service_id",
        }


def test_external_id_only_change_keeps_snapshot_live_projection(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core, external_id="111")
        pub.publish(svc.id, published_by="admin")
        core.change_execution_source(
            svc.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="222",
        )
        latest = pub.get_latest_publish(svc.id)
        assert str(latest.external_service_id) == "111"
        got = PublishedStorefrontProjection(conn).get_service(svc.id)
        assert got.execution.external_service_id == "222"
        assert pub.get_publication_status(svc.id)["customer_catalog_eligible"] is True


def test_source_removal_excludes_from_projection(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core, external_id="123")
        pub.publish(svc.id, published_by="admin")
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
        st = pub.get_publication_status(svc.id)
        assert st["publication_status"] == "published"
        assert st["customer_catalog_eligible"] is False
        assert PublishedStorefrontProjection(conn).list_services() == []
        with pytest.raises(CatalogNotFoundError):
            PublishedStorefrontProjection(conn).get_service(svc.id)


def test_readiness_restore_returns_live_execution(catalog_db: Path):
    """After live source restored, eligibility returns with current live identity."""
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core, external_id="123")
        pub.publish(svc.id, published_by="admin")
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
        assert pub.get_publication_status(svc.id)["customer_catalog_eligible"] is False

        core.change_execution_source(
            svc.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="999",
        )
        st = pub.get_publication_status(svc.id)
        assert st["customer_catalog_eligible"] is True
        got = PublishedStorefrontProjection(conn).get_service(svc.id)
        assert got.execution.external_service_id == "999"
        assert str(pub.get_latest_publish(svc.id).external_service_id) == "123"


def test_archive_and_unpublish_exclude(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        a = _ready_service(core, name="A", external_id="1")
        b = _ready_service(core, name="B", external_id="2")
        pub.publish(a.id, published_by="admin")
        pub.publish(b.id, published_by="admin")
        core.archive_service(a.id)
        pub.unpublish(b.id, published_by="admin")
        ids = {s.service_id for s in PublishedStorefrontProjection(conn).list_services()}
        assert ids == set()
        assert pub.get_publication_status(a.id)["customer_catalog_eligible"] is False
        assert pub.get_publication_status(b.id)["customer_catalog_eligible"] is False


def test_projection_uses_live_never_smm(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core, external_id="555")
        pub.publish(svc.id, published_by="admin")
        core.change_execution_source(
            svc.id,
            provider_slug="other",
            provider_account_key="main",
            external_service_id="live-999",
        )
        got = PublishedStorefrontProjection(conn).get_service(svc.id)
        assert got.service_id.startswith("svc_")
        assert got.execution.external_service_id == "live-999"
        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == 1
