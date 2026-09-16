# -*- coding: utf-8 -*-
"""Phase 7 — Catalog publication tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.errors import CatalogNotFoundError, CatalogPublishError
from catalog_core.provider_mapping import ProviderMappingService
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import SOLDIUM_CATALOG_SCHEMA_VERSION, ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "catalog_publish_test.db"
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


def _ready_service(core: CatalogCoreService, name: str = "Instagram Followers"):
    svc = core.create_service(
        name_ar=name,
        note_ar="ملاحظة",
        service_type="followers",
        ordering_mode="quantity_based",
        min_quantity=10,
        max_quantity=10000,
        status="active",
        fulfillment_mode="auto",
        target_platform_key="instagram",
        target_section_key="followers",
    )
    core.change_execution_source(
        svc.id,
        provider_slug="gozibra",
        provider_account_key="default",
        external_service_id="12345",
    )
    core.change_price(svc.id, amount_dh="2", pricing_mode="per_1000", currency="MAD")
    return core.get_service(svc.id)


def test_schema_version_and_publications_table(catalog_db: Path):
    assert SOLDIUM_CATALOG_SCHEMA_VERSION == "10"
    with catalog_transaction(catalog_db) as conn:
        ver = conn.execute(
            "SELECT value FROM soldium_catalog_schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert ver == "10"
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='soldium_catalog_publications'"
        ).fetchone()


def test_publish_valid_service(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_service(core)
        pub = CatalogPublicationService(conn)
        result = pub.publish(svc.id, published_by="admin")
        assert result.outcome == "published"
        assert result.unchanged is False
        assert result.publication["name_ar"] == "Instagram Followers"
        assert result.publication["amount_millimes"] == 2000
        assert result.publication["external_service_id"] == "12345"
        assert isinstance(result.publication["external_service_id"], str)
        assert result.publication["published_by"] == "admin"
        status = pub.get_publication_status(svc.id)
        assert status["publication_status"] == "published"
        assert status["has_unpublished_changes"] is False


def test_publish_missing_service(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        with pytest.raises(CatalogNotFoundError):
            CatalogPublicationService(conn).publish("svc_missing")


def test_publish_archived_fails(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_service(core)
        core.archive_service(svc.id)
        with pytest.raises(CatalogPublishError) as ei:
            CatalogPublicationService(conn).publish(svc.id)
        assert ei.value.code == "service_archived"


def test_publish_without_price_fails(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = core.create_service(
            name_ar="بلا سعر",
            service_type="followers",
            ordering_mode="quantity_based",
            min_quantity=1,
            max_quantity=100,
            status="active",
        )
        core.change_execution_source(
            svc.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="1",
        )
        with pytest.raises(CatalogPublishError) as ei:
            CatalogPublicationService(conn).publish(svc.id)
        assert ei.value.code in ("invalid_price", "readiness_failed", "validation_failed")


def test_publish_without_source_fails(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = core.create_service(
            name_ar="بلا مصدر",
            service_type="followers",
            ordering_mode="quantity_based",
            min_quantity=1,
            max_quantity=100,
            status="active",
        )
        core.change_price(svc.id, amount_dh="2", pricing_mode="per_1000")
        with pytest.raises(CatalogPublishError) as ei:
            CatalogPublicationService(conn).publish(svc.id)
        assert "مصدر التنفيذ" in ei.value.message


def test_publish_mapping_absent_ok(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = _ready_service(CatalogCoreService(conn))
        result = CatalogPublicationService(conn).publish(svc.id)
        assert result.outcome == "published"


def test_publish_mapping_matches_ok(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_service(core)
        ProviderMappingService(conn).create_or_change_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="12345",
            soldium_service_id=svc.id,
        )
        result = CatalogPublicationService(conn).publish(svc.id)
        assert result.outcome == "published"


def test_publish_mapping_conflict_fails(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_service(core)
        ProviderMappingService(conn).create_or_change_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="99999",
            soldium_service_id=svc.id,
        )
        with pytest.raises(CatalogPublishError) as ei:
            CatalogPublicationService(conn).publish(svc.id)
        assert ei.value.code == "mapping_source_conflict"
        assert "تعارض" in ei.value.message


def test_snapshot_exact_and_immutable(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_service(core)
        pub = CatalogPublicationService(conn)
        result = pub.publish(svc.id)
        pub_id = result.publication["id"]
        core.change_price(svc.id, amount_dh="5", pricing_mode="per_1000")
        row = conn.execute(
            "SELECT amount_millimes, name_ar FROM soldium_catalog_publications WHERE id=?",
            (pub_id,),
        ).fetchone()
        assert int(row["amount_millimes"]) == 2000
        assert row["name_ar"] == "Instagram Followers"
        # no in-place update API — count stays
        again = pub.publish(svc.id)
        assert again.outcome == "published"
        assert again.publication["amount_millimes"] == 5000
        hist = pub.list_publications(svc.id)
        publish_events = [h for h in hist if h.event_type == "publish"]
        assert len(publish_events) == 2
        assert publish_events[1].amount_millimes == 2000


def test_unpublished_changes_detection(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_service(core)
        pub = CatalogPublicationService(conn)
        pub.publish(svc.id)
        assert pub.get_publication_status(svc.id)["has_unpublished_changes"] is False
        core.change_price(svc.id, amount_dh="3", pricing_mode="per_1000")
        assert pub.get_publication_status(svc.id)["has_unpublished_changes"] is True


def test_publish_idempotent_no_change(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = _ready_service(CatalogCoreService(conn))
        pub = CatalogPublicationService(conn)
        first = pub.publish(svc.id)
        second = pub.publish(svc.id)
        assert second.outcome == "no_change"
        assert second.unchanged is True
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_publications "
                "WHERE service_id=? AND event_type='publish'",
                (svc.id,),
            ).fetchone()[0]
            == 1
        )
        assert second.publication["id"] == first.publication["id"]


def test_unpublish_and_history_preserved(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = _ready_service(CatalogCoreService(conn))
        pub = CatalogPublicationService(conn)
        pub.publish(svc.id)
        result = pub.unpublish(svc.id, published_by="admin")
        assert result.outcome == "unpublished"
        status = pub.get_publication_status(svc.id)
        assert status["publication_status"] == "unpublished"
        hist = pub.list_publications(svc.id)
        assert any(h.event_type == "publish" for h in hist)
        assert hist[0].event_type == "unpublish"
        again = pub.unpublish(svc.id)
        assert again.outcome == "no_change"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_publications "
                "WHERE service_id=? AND event_type='unpublish'",
                (svc.id,),
            ).fetchone()[0]
            == 1
        )


def test_preview_readonly(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = _ready_service(CatalogCoreService(conn))
        pub = CatalogPublicationService(conn)
        preview = pub.preview(svc.id)
        assert preview.can_publish is True
        assert preview.snapshot["name_ar"] == "Instagram Followers"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_publications WHERE service_id=?",
                (svc.id,),
            ).fetchone()[0]
            == 0
        )


def test_failed_publish_no_mutation(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = core.create_service(name_ar="ناقصة", status="active")
        before_smm = conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0]
        before_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        with pytest.raises(CatalogPublishError):
            CatalogPublicationService(conn).publish(svc.id)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_publications WHERE service_id=?",
                (svc.id,),
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == before_smm
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == before_orders
        assert conn.execute("SELECT COUNT(*) FROM catalog_services").fetchone()[0] == 1


def test_concurrent_fingerprint_mismatch(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = _ready_service(CatalogCoreService(conn))
        with pytest.raises(CatalogPublishError) as ei:
            CatalogPublicationService(conn).publish(
                svc.id, expected_content_fingerprint="stale-fp"
            )
        assert ei.value.code == "concurrent_change"


def test_api_publication_routes_registered():
    from main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/soldium-catalog/services/{service_id}/publication-preview" in paths
    assert "/api/soldium-catalog/services/{service_id}/publish" in paths
    assert "/api/soldium-catalog/services/{service_id}/unpublish" in paths
    assert "/api/soldium-catalog/services/{service_id}/publications" in paths
    assert "/api/soldium-catalog/services/{service_id}/publication" in paths


def test_ready_draft_cannot_publish(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = core.create_service(
            name_ar="مسودة جاهزة",
            service_type="followers",
            ordering_mode="quantity_based",
            min_quantity=10,
            max_quantity=1000,
            status="draft",
            fulfillment_mode="auto",
            target_platform_key="instagram",
            target_section_key="followers",
        )
        core.change_execution_source(
            svc.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="55",
        )
        core.change_price(svc.id, amount_dh="2", pricing_mode="per_1000")
        ready = core.get_service_readiness(svc.id)
        assert ready.ready is True
        with pytest.raises(CatalogPublishError) as ei:
            CatalogPublicationService(conn).publish(svc.id)
        assert ei.value.code == "service_not_active"
        preview = CatalogPublicationService(conn).preview(svc.id)
        assert preview.can_publish is False
        assert preview.blocking_code == "service_not_active"


def test_customer_catalog_eligibility_matrix(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = _ready_service(core)
        pub.publish(svc.id)

        st = pub.get_publication_status(svc.id)
        assert st["publication_status"] == "published"
        assert st["customer_catalog_eligible"] is True

        # published + active + needs_review
        price = core.get_price(svc.id)
        assert price is not None
        conn.execute(
            """
            UPDATE soldium_catalog_prices
            SET status='historical', effective_to=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (price.id,),
        )
        st2 = pub.get_publication_status(svc.id)
        assert st2["publication_status"] == "published"
        assert st2["customer_catalog_eligible"] is False

        # restore price, archive
        core.change_price(svc.id, amount_dh="2", pricing_mode="per_1000")
        # still published; archive
        core.archive_service(svc.id)
        st3 = pub.get_publication_status(svc.id)
        assert st3["publication_status"] == "published"
        assert core.get_service(svc.id).status == "archived"
        assert st3["customer_catalog_eligible"] is False

        # unpublished + active + ready
        core.restore_service(svc.id, status="active")
        pub.unpublish(svc.id)
        st4 = pub.get_publication_status(svc.id)
        assert st4["publication_status"] == "unpublished"
        assert core.get_service_readiness(svc.id).ready is True
        assert st4["customer_catalog_eligible"] is False


def test_same_timestamp_event_ordering_uses_rowid(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_service(core)
        pub = CatalogPublicationService(conn)
        pub.publish(svc.id)
        pub.unpublish(svc.id)
        # Force identical timestamps; later rowid must win.
        conn.execute(
            """
            UPDATE soldium_catalog_publications
            SET published_at = '2026-01-01T00:00:00Z'
            WHERE service_id = ?
            """,
            (svc.id,),
        )
        latest = pub.get_latest_event(svc.id)
        assert latest is not None
        assert latest.event_type == "unpublish"
        assert pub.get_publication_status(svc.id)["publication_status"] == "unpublished"

        # Insert another publish with same timestamp after unpublish row
        result = pub.publish(svc.id)
        assert result.outcome == "published"
        conn.execute(
            """
            UPDATE soldium_catalog_publications
            SET published_at = '2026-01-01T00:00:00Z'
            WHERE service_id = ?
            """,
            (svc.id,),
        )
        latest2 = pub.get_latest_event(svc.id)
        assert latest2 is not None
        assert latest2.event_type == "publish"
        assert pub.get_publication_status(svc.id)["publication_status"] == "published"
