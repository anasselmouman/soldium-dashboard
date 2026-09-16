# -*- coding: utf-8 -*-
"""Phase 9B.7 — Storefront Order Contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.commercial import normalize_fulfillment_mode
from catalog_core.db import catalog_transaction
from catalog_core.errors import CatalogValidationError
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import SOLDIUM_CATALOG_SCHEMA_VERSION, ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_adapter import StorefrontAdapter, StorefrontAdapterError
from catalog_core.storefront_projection import PublishedStorefrontProjection
from catalog_core.target_validation import validate_order_target


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "order_contract.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
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
            VALUES ('gozibra', 'tiktok', 'TikTok');
            CREATE TABLE smm_services (
                catalog_id TEXT PRIMARY KEY,
                name_ar TEXT,
                platform_key TEXT,
                section_key TEXT,
                subsection_key TEXT,
                fulfillment_mode TEXT DEFAULT 'auto',
                is_active INTEGER DEFAULT 1,
                local_price_dh REAL,
                min_qty INTEGER,
                max_qty INTEGER,
                provider_slug TEXT,
                provider_api_account TEXT,
                external_service_id TEXT
            );
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _ready(
    core: CatalogCoreService,
    *,
    name: str = "Pilot",
    platform: str = "instagram",
    section: str = "interaction",
    fulfillment: str = "auto",
    external_id: str = "1154",
):
    svc = core.create_service(
        name_ar=name,
        note_ar="",
        service_type="other",
        ordering_mode="quantity_based",
        min_quantity=10,
        max_quantity=1000,
        status="active",
        fulfillment_mode=fulfillment,
        target_platform_key=platform,
        target_section_key=section,
    )
    core.change_execution_source(
        svc.id,
        provider_slug="gozibra",
        provider_account_key="tiktok",
        external_service_id=external_id,
    )
    core.change_price(svc.id, amount_dh="3", pricing_mode="per_1000", currency="MAD")
    return core.get_service(svc.id)


def test_schema_version_9_and_order_contract_columns(catalog_db: Path):
    assert SOLDIUM_CATALOG_SCHEMA_VERSION == "10"
    with catalog_transaction(catalog_db) as conn:
        ver = conn.execute(
            "SELECT value FROM soldium_catalog_schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert ver == "10"
        svc_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(soldium_catalog_services)")
        }
        pub_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(soldium_catalog_publications)")
        }
        for col in (
            "fulfillment_mode",
            "target_platform_key",
            "target_section_key",
            "target_subsection_key",
        ):
            assert col in svc_cols
            assert col in pub_cols


def test_fulfillment_mode_validation():
    assert normalize_fulfillment_mode("auto") == "auto"
    assert normalize_fulfillment_mode("admin") == "admin"
    assert normalize_fulfillment_mode(None) == "auto"
    with pytest.raises(CatalogValidationError):
        normalize_fulfillment_mode("manual")
    with pytest.raises(CatalogValidationError):
        normalize_fulfillment_mode("AUTO_X")


@pytest.mark.parametrize(
    "platform,section,valid,invalid,extra_valid",
    [
        ("facebook", "followers_members", "https://facebook.com/page", "https://x.com/a", None),
        ("instagram", "interaction", "https://instagram.com/p/1", "https://facebook.com/x", None),
        ("telegram", "post_views", "https://t.me/channel", "https://instagram.com/x", None),
        ("tiktok", "direct", "https://tiktok.com/@u/video/1", "https://x.com/a", None),
        ("x", "followers", "https://x.com/user", "https://facebook.com/a", "@elonmusk"),
    ],
)
def test_pilot_target_validation_matrix(platform, section, valid, invalid, extra_valid):
    ok, _ = validate_order_target(
        valid, platform_key=platform, section_key=section, required=True
    )
    assert ok
    bad, _ = validate_order_target(
        invalid, platform_key=platform, section_key=section, required=True
    )
    assert not bad
    if extra_valid:
        ok2, _ = validate_order_target(
            extra_valid, platform_key=platform, section_key=section, required=True
        )
        assert ok2


def test_publish_requires_target_policy(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = core.create_service(
            name_ar="No target",
            status="active",
            service_type="other",
            ordering_mode="quantity_based",
            min_quantity=1,
            max_quantity=10,
        )
        core.change_execution_source(
            svc.id,
            provider_slug="gozibra",
            provider_account_key="tiktok",
            external_service_id="1",
        )
        core.change_price(svc.id, amount_dh="1", pricing_mode="per_1000")
        from catalog_core.errors import CatalogPublishError

        with pytest.raises(CatalogPublishError) as ei:
            CatalogPublicationService(conn).publish(svc.id)
        assert ei.value.code == "missing_target_policy"


def test_publication_freezes_fulfillment_and_target(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready(core, platform="x", section="followers", fulfillment="auto")
        pub = CatalogPublicationService(conn)
        result = pub.publish(svc.id, published_by="test")
        assert result.outcome == "published"
        snap = result.publication
        assert snap["fulfillment_mode"] == "auto"
        assert snap["target_policy"]["platform_key"] == "x"
        assert snap["target_policy"]["section_key"] == "followers"
        old_fp = snap["content_fingerprint"]

        # Live draft change must not alter projection/adapter until republish.
        core.update_service(svc.id, fulfillment_mode="admin")
        core.update_service(
            svc.id, target_platform_key="tiktok", target_section_key="direct"
        )

        projected = PublishedStorefrontProjection(conn).get_service(svc.id)
        assert projected.fulfillment_mode == "auto"
        assert projected.target_policy.platform_key == "x"
        assert projected.target_policy.section_key == "followers"

        adapter = StorefrontAdapter(conn)
        detail = adapter.get_service(svc.id)
        assert detail.fulfillment_mode == "auto"
        assert detail.target_policy.platform_key == "x"

        intent = adapter.resolve_order_intent(
            svc.id, 10, target="https://x.com/user"
        )
        assert intent.fulfillment_mode == "auto"
        assert intent.target == "https://x.com/user"

        # Fingerprint drift detected
        status = pub.get_publication_status(svc.id)
        assert status["has_unpublished_changes"] is True

        # Historical publish row unchanged
        row = conn.execute(
            """
            SELECT fulfillment_mode, target_platform_key, content_fingerprint
            FROM soldium_catalog_publications
            WHERE id = ?
            """,
            (snap["id"],),
        ).fetchone()
        assert row["fulfillment_mode"] == "auto"
        assert row["target_platform_key"] == "x"
        assert row["content_fingerprint"] == old_fp


def test_order_intent_complete_and_fail_closed(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready(core, platform="facebook", section="followers_members")
        CatalogPublicationService(conn).publish(svc.id)

        adapter = StorefrontAdapter(conn)
        intent = adapter.resolve_order_intent(
            svc.id, 100, target="https://facebook.com/page"
        )
        assert intent.service_id.startswith("svc_")
        assert intent.quantity == 100
        assert intent.fulfillment_mode == "auto"
        assert intent.provider_slug == "gozibra"
        assert intent.external_service_id == "1154"
        assert intent.content_fingerprint
        assert intent.published_at
        assert intent.target_validation_ok is True

        with pytest.raises(StorefrontAdapterError) as ei:
            adapter.resolve_order_intent(svc.id, 100, target="https://x.com/nope")
        assert ei.value.code == "invalid_target"

        with pytest.raises(StorefrontAdapterError) as ei2:
            adapter.resolve_order_intent(
                svc.id, 1, target="https://facebook.com/page"
            )
        assert ei2.value.code == "quantity_below_minimum"

        with pytest.raises(StorefrontAdapterError) as ei3:
            adapter.resolve_order_intent(
                "svc_missing", 10, target="https://facebook.com/page"
            )
        assert ei3.value.code == "service_unavailable"


def test_legacy_isolation_after_publish(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready(core, platform="tiktok", section="direct", external_id="1566")
        CatalogPublicationService(conn).publish(svc.id)

        conn.execute(
            """
            INSERT INTO smm_services(
                catalog_id, name_ar, platform_key, section_key,
                fulfillment_mode, is_active, local_price_dh, min_qty, max_qty,
                provider_slug, provider_api_account, external_service_id
            ) VALUES ('1566', 'LEGACY CHANGED', 'facebook', 'followers_members',
                      'admin', 1, 99, 1, 2, 'other', 'other', '999')
            """
        )

        projected = PublishedStorefrontProjection(conn).get_service(svc.id)
        assert projected.fulfillment_mode == "auto"
        assert projected.target_policy.platform_key == "tiktok"
        assert projected.execution.external_service_id == "1566"
        assert projected.name_ar == "Pilot"

        intent = StorefrontAdapter(conn).resolve_order_intent(
            svc.id, 10, target="https://tiktok.com/@u/video/1"
        )
        assert intent.fulfillment_mode == "auto"
        assert intent.external_service_id == "1566"


def test_x_followers_allows_username(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready(core, platform="x", section="followers")
        CatalogPublicationService(conn).publish(svc.id)
        adapter = StorefrontAdapter(conn)
        assert adapter.validate_target(svc.id, "@elonmusk").ok
        assert adapter.validate_target(svc.id, "https://x.com/elonmusk").ok
        assert not adapter.validate_target(svc.id, "not-a-url").ok


def test_unpublish_republish_preserves_history(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready(core)
        pub = CatalogPublicationService(conn)
        first = pub.publish(svc.id, published_by="a")
        first_id = first.publication["id"]
        first_fp = first.publication["content_fingerprint"]

        pub.unpublish(svc.id, published_by="b")
        second = pub.publish(svc.id, published_by="c")
        assert second.outcome == "published"
        assert second.publication["id"] != first_id

        hist = conn.execute(
            """
            SELECT id, event_type, fulfillment_mode, content_fingerprint
            FROM soldium_catalog_publications
            WHERE service_id = ?
            ORDER BY rowid
            """,
            (svc.id,),
        ).fetchall()
        assert len(hist) == 3
        assert hist[0]["id"] == first_id
        assert hist[0]["event_type"] == "publish"
        assert hist[0]["fulfillment_mode"] == "auto"
        assert hist[0]["content_fingerprint"] == first_fp
        assert hist[1]["event_type"] == "unpublish"
        assert hist[2]["event_type"] == "publish"
        assert hist[2]["fulfillment_mode"] == "auto"
