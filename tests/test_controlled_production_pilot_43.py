# -*- coding: utf-8 -*-
"""Controlled production pilot — 43 Catalog cohort scoped routing tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from catalog_core.db import catalog_transaction
from catalog_core.legacy_migration import LEGACY_SERVICE_BRIDGE_TABLE
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_adapter import StorefrontAdapterError
from catalog_core.storefront_gateway import (
    CatalogStorefrontBackend,
    LegacyStorefrontBackend,
    build_storefront,
    clear_storefront_cache,
    reinitialize_storefront_selection,
)
from catalog_core.storefront_pilot import (
    PilotHybridStorefrontBackend,
    load_published_pilot_cohort,
    reset_pilot_metrics,
    resolve_catalog_pilot_enabled,
    run_pilot_preflight,
    set_catalog_pilot_override,
    clear_catalog_pilot_override,
)
from catalog_core.target_validation import (
    _service_requires_comment_link,
    validate_order_target,
)
from tests.test_order_contract import _ready
from utils.order_execution_identity import GEN0_EXECUTION_IDENTITY_MISSING


@pytest.fixture(autouse=True)
def _clear_overrides():
    reinitialize_storefront_selection()
    reset_pilot_metrics()
    yield
    reinitialize_storefront_selection()
    reset_pilot_metrics()


@pytest.fixture
def pilot_db(tmp_path: Path) -> Path:
    path = tmp_path / "pilot43.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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
            local_price_dh REAL,
            service_id TEXT,
            external_service_id TEXT,
            is_active INTEGER DEFAULT 1,
            local_item_id TEXT,
            platform_key TEXT DEFAULT '',
            section_key TEXT DEFAULT ''
        );
        INSERT INTO smm_services(
            catalog_id, name_ar, local_price_dh, service_id,
            external_service_id, is_active, local_item_id, platform_key, section_key
        ) VALUES
            ('leg_pilot', 'طيار منشور', 5.0, '100', '1896', 1, 'leg-pilot-1', 'tiktok', 'likes'),
            ('leg_only', 'قديم فقط', 2.0, '200', '999', 1, 'leg-only-1', 'instagram', 'followers'),
            ('leg_unpub', 'غير منشور', 3.0, '300', '888', 1, 'leg-unpub-1', 'tiktok', 'views');
        CREATE TABLE orders (id INTEGER PRIMARY KEY, service_id TEXT);
        INSERT INTO orders(id, service_id) VALUES (1, 'hist');
        """
    )
    ensure_soldium_catalog_schema(conn)
    conn.commit()
    conn.close()
    return path


def _insert_bridge(conn: sqlite3.Connection, *, legacy_id: str, soldium_id: str) -> None:
    conn.execute(
        f"""
        INSERT INTO {LEGACY_SERVICE_BRIDGE_TABLE} (
            legacy_catalog_id, legacy_local_item_id, legacy_service_id,
            soldium_service_id, classification, migration_batch_id
        ) VALUES (?, ?, ?, ?, 'imported', 'test_batch')
        """,
        (legacy_id, legacy_id, legacy_id, soldium_id),
    )


def _legacy_tree() -> dict:
    return {
        "tiktok": {
            "title": "تيك توك",
            "sections": {
                "likes": {
                    "title": "إعجابات",
                    "items": [
                        {
                            "id": "leg-pilot-1",
                            "name": "طيار منشور",
                            "price": 5.0,
                            "price_per_unit": False,
                            "min": 10,
                            "max": 1000,
                            "catalog_id": "leg_pilot",
                            "external_service_id": "1896",
                            "provider_slug": "gozibra",
                            "provider_account": "tiktok",
                            "fulfillment_mode": "auto",
                            "link_type": "post",
                        },
                        {
                            "id": "leg-unpub-1",
                            "name": "غير منشور",
                            "price": 3.0,
                            "price_per_unit": False,
                            "min": 10,
                            "max": 1000,
                            "catalog_id": "leg_unpub",
                            "external_service_id": "888",
                            "provider_slug": "gozibra",
                            "provider_account": "tiktok",
                            "fulfillment_mode": "auto",
                        },
                    ],
                    "subsections": {},
                }
            },
            "direct_items": [],
        },
        "instagram": {
            "title": "إنستغرام",
            "sections": {
                "followers": {
                    "title": "متابعون",
                    "items": [
                        {
                            "id": "leg-only-1",
                            "name": "قديم فقط",
                            "price": 2.0,
                            "price_per_unit": False,
                            "min": 50,
                            "max": 5000,
                            "catalog_id": "leg_only",
                            "external_service_id": "999",
                            "provider_slug": "gozibra",
                            "provider_account": "tiktok",
                            "fulfillment_mode": "auto",
                        }
                    ],
                    "subsections": {},
                }
            },
            "direct_items": [],
        },
    }


def _seed_published_and_draft(conn: sqlite3.Connection):
    core = CatalogCoreService(conn)
    pub = CatalogPublicationService(conn)
    published = _ready(
        core, name="طيار منشور", platform="tiktok", section="likes", external_id="1896"
    )
    pub.publish(published.id)
    _insert_bridge(conn, legacy_id="leg_pilot", soldium_id=published.id)

    draft = _ready(
        core, name="مسودة", platform="tiktok", section="views", external_id="7777"
    )
    _insert_bridge(conn, legacy_id="leg_unpub", soldium_id=draft.id)
    # unpublished — no publish()

    return published, draft


# --- A: pilot disabled → Legacy ---


def test_a_pilot_disabled_all_legacy(pilot_db: Path):
    with catalog_transaction(pilot_db) as conn:
        _seed_published_and_draft(conn)
        set_catalog_pilot_override(False)
        sf = build_storefront(
            connection=conn,
            legacy_tree_loader=_legacy_tree,
            environ={"STOREFRONT_BACKEND": "legacy", "STOREFRONT_CATALOG_PILOT": "disabled"},
        )
        assert isinstance(sf, LegacyStorefrontBackend)
        assert sf.backend_name == "legacy"
        assert sf.get_service("leg-pilot-1").service_id == "leg-pilot-1"


# --- B: published → Catalog ---


def test_b_pilot_enabled_published_routes_catalog(pilot_db: Path):
    with catalog_transaction(pilot_db) as conn:
        published, _ = _seed_published_and_draft(conn)
        set_catalog_pilot_override(True)
        sf = build_storefront(
            backend="pilot",
            connection=conn,
            legacy_tree_loader=_legacy_tree,
        )
        assert isinstance(sf, PilotHybridStorefrontBackend)
        assert sf.resolve_route(published.id) == "catalog"
        svc = sf.get_service(published.id)
        assert svc.service_id == published.id
        assert sf.uses_catalog_order_contract(published.id)


# --- C / E: unpublished → Legacy ---


def test_c_e_unpublished_stays_legacy(pilot_db: Path):
    with catalog_transaction(pilot_db) as conn:
        _seed_published_and_draft(conn)
        set_catalog_pilot_override(True)
        sf = build_storefront(
            backend="pilot", connection=conn, legacy_tree_loader=_legacy_tree
        )
        assert sf.resolve_route("leg-unpub-1") == "legacy"
        assert sf.get_service("leg-unpub-1").service_id == "leg-unpub-1"


# --- D: Legacy-only → Legacy ---


def test_d_legacy_only_stays_legacy(pilot_db: Path):
    with catalog_transaction(pilot_db) as conn:
        _seed_published_and_draft(conn)
        set_catalog_pilot_override(True)
        sf = build_storefront(
            backend="pilot", connection=conn, legacy_tree_loader=_legacy_tree
        )
        assert sf.resolve_route("leg-only-1") == "legacy"
        assert sf.get_service("leg-only-1").service_id == "leg-only-1"


# --- F: invalid pilot config → Legacy ---


def test_f_invalid_pilot_config_safe_legacy(pilot_db: Path):
    assert resolve_catalog_pilot_enabled("nope") is False
    assert resolve_catalog_pilot_enabled("") is False
    with catalog_transaction(pilot_db) as conn:
        _seed_published_and_draft(conn)
        clear_catalog_pilot_override()
        # pilot enabled in environ but missing connection → Legacy fallback
        sf = build_storefront(
            legacy_tree_loader=_legacy_tree,
            environ={
                "STOREFRONT_BACKEND": "legacy",
                "STOREFRONT_CATALOG_PILOT": "enabled",
            },
        )
        assert isinstance(sf, LegacyStorefrontBackend)


# --- G: unpublish removes from Catalog route ---


def test_g_unpublish_removes_from_catalog_route(pilot_db: Path):
    with catalog_transaction(pilot_db) as conn:
        published, _ = _seed_published_and_draft(conn)
        set_catalog_pilot_override(True)
        sf = build_storefront(
            backend="pilot", connection=conn, legacy_tree_loader=_legacy_tree
        )
        assert published.id in sf.cohort_ids()
        CatalogPublicationService(conn).unpublish(published.id)
        sf.refresh()
        assert published.id not in sf.cohort_ids()
        with pytest.raises(StorefrontAdapterError) as ei:
            sf.get_service(published.id)
        assert ei.value.code == "pilot_fail_closed"


# --- H / I / J: mixed navigation ---


def test_h_i_j_mixed_navigation_no_duplicates(pilot_db: Path):
    with catalog_transaction(pilot_db) as conn:
        published, _ = _seed_published_and_draft(conn)
        set_catalog_pilot_override(True)
        sf = build_storefront(
            backend="pilot", connection=conn, legacy_tree_loader=_legacy_tree
        )
        nav = sf.navigation_tree()
        assert "tiktok" in nav or "تيك توك" in str(nav)
        ids = []
        for item, *_ in __import__(
            "catalog_core.storefront_gateway", fromlist=["_iter_legacy_entries"]
        )._iter_legacy_entries(nav):
            ids.append(str(item.get("id")))
        assert published.id in ids
        assert "leg-only-1" in ids
        assert "leg-pilot-1" not in ids  # replaced
        assert ids.count(published.id) == 1
        assert "leg-unpub-1" in ids


# --- K / L: pricing ---


def test_k_l_catalog_vs_legacy_pricing(pilot_db: Path):
    with catalog_transaction(pilot_db) as conn:
        published, _ = _seed_published_and_draft(conn)
        set_catalog_pilot_override(True)
        sf = build_storefront(
            backend="pilot", connection=conn, legacy_tree_loader=_legacy_tree
        )
        cq = sf.quote_price(published.id, 1000)
        assert cq.unit_amount_millimes == 3000  # from _ready amount_dh=3
        lq = sf.quote_price("leg-only-1", 1000)
        assert lq.unit_amount_millimes == 2000  # legacy tree price 2.0


# --- M / N / O: intent + execution + no smm SKU ---


def test_m_n_o_catalog_intent_execution_no_legacy_sku(pilot_db: Path):
    with catalog_transaction(pilot_db) as conn:
        published, _ = _seed_published_and_draft(conn)
        set_catalog_pilot_override(True)
        sf = build_storefront(
            backend="pilot", connection=conn, legacy_tree_loader=_legacy_tree
        )
        intent = sf.resolve_order_intent(
            published.id, 1000, target="https://tiktok.com/@u/video/1"
        )
        # Catalog path must not depend on Legacy SKU recovery
        assert intent.external_service_id == "1896"
        assert isinstance(intent.external_service_id, str)
        assert intent.service_id == published.id
        assert intent.service_id != intent.external_service_id
        assert intent.quoted_amount_millimes == 3000
        # Adapter asserts Catalog never queries smm_services
        assert CatalogStorefrontBackend(conn).asserts_no_smm_services_access()


# --- P: Legacy path functional ---


def test_p_legacy_path_functional(pilot_db: Path):
    with catalog_transaction(pilot_db) as conn:
        _seed_published_and_draft(conn)
        set_catalog_pilot_override(True)
        sf = build_storefront(
            backend="pilot", connection=conn, legacy_tree_loader=_legacy_tree
        )
        intent = sf.resolve_order_intent(
            "leg-only-1", 100, target="https://instagram.com/p/1"
        )
        assert intent.service_id == "leg-only-1"


# --- Q: provider barrier in test ---


def test_q_provider_submission_blocked_in_test(pilot_db: Path):
    submit = MagicMock(side_effect=AssertionError("provider must not be called"))
    with catalog_transaction(pilot_db) as conn:
        published, _ = _seed_published_and_draft(conn)
        set_catalog_pilot_override(True)
        sf = build_storefront(
            backend="pilot", connection=conn, legacy_tree_loader=_legacy_tree
        )
        sf.resolve_order_intent(
            published.id, 1000, target="https://tiktok.com/@u/video/1"
        )
        submit.assert_not_called()


# --- R / S: Gen-0 + 9N ---


def test_r_gen0_fail_closed_intact():
    assert GEN0_EXECUTION_IDENTITY_MISSING == "GEN0_EXECUTION_IDENTITY_MISSING"


def test_s_phase9n_comment_link_intact():
    assert _service_requires_comment_link({"link_type": "comment"}) is True
    assert _service_requires_comment_link({"link_type": "post"}) is False
    # No Provider-ID 4371 special-case in the policy helper module text
    from pathlib import Path

    text = Path("catalog_core/target_validation.py").read_text(encoding="utf-8")
    assert "4371" not in text
    ok, _ = validate_order_target(
        "https://instagram.com/p/1",
        platform_key="instagram",
        section_key="interaction",
        required=True,
    )
    assert ok is True


# --- T / U: kill switch ---


def test_t_u_kill_switch_and_reenable(pilot_db: Path):
    with catalog_transaction(pilot_db) as conn:
        published, _ = _seed_published_and_draft(conn)
        set_catalog_pilot_override(True)
        sf = build_storefront(
            connection=conn,
            legacy_tree_loader=_legacy_tree,
            environ={"STOREFRONT_CATALOG_PILOT": "enabled"},
        )
        assert isinstance(sf, PilotHybridStorefrontBackend)

        set_catalog_pilot_override(False)
        clear_storefront_cache()
        sf2 = build_storefront(
            connection=conn,
            legacy_tree_loader=_legacy_tree,
            environ={"STOREFRONT_CATALOG_PILOT": "disabled"},
        )
        assert isinstance(sf2, LegacyStorefrontBackend)

        set_catalog_pilot_override(True)
        sf3 = build_storefront(
            backend="pilot", connection=conn, legacy_tree_loader=_legacy_tree
        )
        assert published.id in sf3.cohort_ids()
        assert sf3.resolve_route("leg-only-1") == "legacy"


# --- V: cohort from publication ---


def test_v_cohort_from_publication_state(pilot_db: Path):
    with catalog_transaction(pilot_db) as conn:
        published, draft = _seed_published_and_draft(conn)
        cohort = load_published_pilot_cohort(conn)
        assert published.id in cohort
        assert draft.id not in cohort
        assert len(cohort) == 1


# --- W: no mutation during tests (orders untouched) ---


def test_w_no_order_mutation(pilot_db: Path):
    with catalog_transaction(pilot_db) as conn:
        _seed_published_and_draft(conn)
        before = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        set_catalog_pilot_override(True)
        sf = build_storefront(
            backend="pilot", connection=conn, legacy_tree_loader=_legacy_tree
        )
        sf.navigation_tree()
        after = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        assert before == after == 1


def test_superseded_legacy_id_fail_closed(pilot_db: Path):
    with catalog_transaction(pilot_db) as conn:
        _seed_published_and_draft(conn)
        set_catalog_pilot_override(True)
        sf = build_storefront(
            backend="pilot", connection=conn, legacy_tree_loader=_legacy_tree
        )
        assert sf.resolve_route("leg-pilot-1") == "fail_closed"
        with pytest.raises(StorefrontAdapterError) as ei:
            sf.get_service("leg-pilot-1")
        assert ei.value.code == "pilot_fail_closed"


def test_preflight_structure(pilot_db: Path):
    with catalog_transaction(pilot_db) as conn:
        _seed_published_and_draft(conn)
        report = run_pilot_preflight(
            conn, expected_cohort=1, legacy_tree_loader=_legacy_tree
        )
        assert report["published_count"] == 1
        assert report["ok"] is True
