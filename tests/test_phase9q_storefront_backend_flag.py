# -*- coding: utf-8 -*-
"""Phase 9Q — Storefront backend flag tests."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from catalog_core.db import catalog_transaction
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_adapter import StorefrontAdapterError
from catalog_core.storefront_gateway import (
    CatalogStorefrontBackend,
    LegacyStorefrontBackend,
    build_storefront,
    clear_storefront_cache,
    order_intent_to_create_bridge,
    resolve_storefront_backend_name,
    set_storefront_backend_override,
    shadow_pair,
)
from catalog_core.target_validation import (
    _service_requires_comment_link,
    validate_order_target,
)
from utils.order_execution_identity import GEN0_EXECUTION_IDENTITY_MISSING


@pytest.fixture(autouse=True)
def _clear_overrides():
    set_storefront_backend_override(None)
    clear_storefront_cache()
    yield
    set_storefront_backend_override(None)
    clear_storefront_cache()


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "sf9q.db"
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
            is_active INTEGER DEFAULT 1
        );
        INSERT INTO smm_services(catalog_id, name_ar, local_price_dh, service_id, external_service_id)
        VALUES ('leg_poison', 'LEGACY POISON', 99.0, '999', '99999');
        """
    )
    ensure_soldium_catalog_schema(conn)
    conn.commit()
    conn.close()
    return path


def _ready_and_publish(conn: sqlite3.Connection, *, external_id: str = "1154"):
    core = CatalogCoreService(conn)
    # Minimal ready service via existing test helpers pattern
    from tests.test_order_contract import _ready

    svc = _ready(core, platform="tiktok", section="followers", external_id=external_id)
    CatalogPublicationService(conn).publish(svc.id)
    return svc


# --- A B C D Q: selection ---


def test_a_q_default_and_missing_select_legacy():
    assert resolve_storefront_backend_name(None, environ={}) == "legacy"
    assert resolve_storefront_backend_name("", environ={}) == "legacy"
    assert resolve_storefront_backend_name(None, environ={"STOREFRONT_BACKEND": ""}) == "legacy"


def test_b_explicit_legacy():
    assert resolve_storefront_backend_name("legacy") == "legacy"
    assert resolve_storefront_backend_name("LEGACY") == "legacy"


def test_c_catalog_in_isolated_config(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        _ready_and_publish(conn)
        sf = build_storefront(backend="catalog", connection=conn)
        assert sf.backend_name == "catalog"
        assert isinstance(sf, CatalogStorefrontBackend)
        assert len(sf.list_services()) >= 1


def test_d_invalid_cannot_select_catalog():
    for bad in ("prod", "telegram", "catalogue", "1", "true", "legacy "):
        # trailing space stripped → legacy for "legacy "; others → legacy
        assert resolve_storefront_backend_name(bad) == "legacy"
    assert resolve_storefront_backend_name("catalog") == "catalog"


# --- E R: handler uses abstraction / no scattered selection ---


def test_e_r_handlers_use_storefront_not_backend_flag():
    bot_handlers = Path("../soldium-bot/handlers/orders.py")
    if not bot_handlers.is_file():
        pytest.skip("bot handlers not present")
    text = bot_handlers.read_text(encoding="utf-8")
    assert "get_storefront" in text
    assert "navigation_tree" in text or "_services()" in text
    # No scattered selection
    assert not re.search(r"STOREFRONT_BACKEND\s*==", text)
    assert "if backend" not in text.lower() or "backend_name" in text


def test_p_backend_switching_centralized():
    assert callable(resolve_storefront_backend_name)
    assert callable(build_storefront)
    src = Path("catalog_core/storefront_gateway.py").read_text(encoding="utf-8")
    assert "DEFAULT_BACKEND" in src
    assert 'return "legacy"' in src


# --- F O: Legacy behavior ---


def test_f_o_legacy_preserves_discovery():
    tree = {
        "tiktok": {
            "title": "تيك توك",
            "sections": {
                "followers": {
                    "title": "متابعين",
                    "items": [
                        {
                            "id": "1001",
                            "name": "متابعين",
                            "price": 10.0,
                            "min": 10,
                            "max": 1000,
                            "external_service_id_text": "555",
                            "provider_slug": "gozibra",
                            "provider_account": "default",
                            "fulfillment_mode": "auto",
                        }
                    ],
                    "subsections": {},
                }
            },
            "direct_items": [],
        }
    }
    sf = LegacyStorefrontBackend(lambda: tree)
    assert sf.backend_name == "legacy"
    plats = sf.list_platforms()
    assert any(p.label == "تيك توك" for p in plats)
    svc = sf.get_service("1001")
    assert svc.service_id == "1001"
    assert svc.execution.external_service_id == "555"
    assert isinstance(svc.execution.external_service_id, str)
    nav = sf.navigation_tree()
    assert "tiktok" in nav
    assert nav["tiktok"]["sections"]["followers"]["items"][0]["id"] == "1001"


# --- G H I J K L M N: Catalog ---


def test_g_h_i_j_k_l_m_n_catalog_backend(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = _ready_and_publish(conn, external_id="7788")
        # Draft unpublished must not appear
        core = CatalogCoreService(conn)
        draft = core.create_service(name_ar="مسودة غير منشورة")
        assert draft.status == "draft"

        sf = CatalogStorefrontBackend(conn)
        assert sf.backend_name == "catalog"
        assert sf.asserts_no_smm_services_access() is True

        # H: Catalog does not need smm_services — poison row must not affect quote
        ids = {s.service_id for s in sf.list_services()}
        assert svc.id in ids
        assert draft.id not in ids  # I unpublished excluded
        assert all(i.startswith("svc_") for i in ids)

        got = sf.get_service(svc.id)
        quote = sf.quote_price(svc.id, 1000)
        # K: published unit price (not Legacy poison 99 DH = 99000 millimes)
        assert got.price.amount_millimes == quote.unit_amount_millimes
        assert got.price.amount_millimes != 99000
        assert quote.quoted_amount_millimes == got.price.amount_millimes  # qty 1000

        intent = sf.resolve_order_intent(
            svc.id,
            max(got.min_quantity, 100),
            target="https://www.tiktok.com/@u",
        )
        assert intent.service_id.startswith("svc_")
        assert intent.external_service_id == "7788"
        assert isinstance(intent.external_service_id, str)
        assert intent.fulfillment_mode in {"auto", "admin"}
        assert intent.quoted_amount_millimes > 0
        assert intent.target_validation_ok is True

        bridge = order_intent_to_create_bridge(intent, user_id=42)
        kwargs = bridge.to_create_kwargs()
        assert kwargs["service_id"].startswith("svc_")
        assert kwargs["external_service_id_snapshot"] == "7788"
        assert isinstance(kwargs["external_service_id_snapshot"], str)
        assert kwargs["amount"] == intent.quoted_amount_millimes / 1000.0

        # N: no Legacy SKU lookup in Catalog backend source
        src = Path("catalog_core/storefront_gateway.py").read_text(encoding="utf-8")
        cat_class = src.split("class CatalogStorefrontBackend")[1].split(
            "class LegacyStorefrontBackend"
        )[0]
        assert "smm_services" not in cat_class or "never reads" in cat_class.lower()


def test_catalog_excludes_archived(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = _ready_and_publish(conn)
        CatalogPublicationService(conn).unpublish(svc.id)
        sf = CatalogStorefrontBackend(conn)
        ids = {s.service_id for s in sf.list_services()}
        assert svc.id not in ids


# --- Shadow ---


def test_shadow_pair(catalog_db: Path):
    tree = {"tiktok": {"title": "t", "sections": {}, "direct_items": []}}
    with catalog_transaction(catalog_db) as conn:
        _ready_and_publish(conn)
        leg, cat = shadow_pair(connection=conn, legacy_tree_loader=lambda: tree)
        assert leg.backend_name == "legacy"
        assert cat.backend_name == "catalog"


# --- S T: 9N / 9O intact ---


def test_s_phase9n_link_type_intact():
    assert _service_requires_comment_link({"link_type": "comment"}) is True
    assert _service_requires_comment_link({"id": "4371"}) is False
    ok, _ = validate_order_target(
        "https://www.tiktok.com/@u/video/1?comment_id=9",
        platform_key="tiktok",
        section_key="likes",
        link_type="comment",
    )
    assert ok is True


def test_t_phase9o_gen0_constant_intact():
    assert GEN0_EXECUTION_IDENTITY_MISSING == "GEN0_EXECUTION_IDENTITY_MISSING"
    so = Path("scheduled_orders.py").read_text(encoding="utf-8")
    assert GEN0_EXECUTION_IDENTITY_MISSING in so
    body = re.search(
        r"async def _submit_job_order\([\s\S]*?(?=\nasync def |\ndef )", so
    )
    assert body is not None
    assert "_lookup_service_provider_meta" not in body.group(0)


def test_override_for_isolated_tests(catalog_db: Path):
    set_storefront_backend_override("catalog")
    with catalog_transaction(catalog_db) as conn:
        _ready_and_publish(conn)
        sf = build_storefront(connection=conn, legacy_tree_loader=lambda: {})
        assert sf.backend_name == "catalog"
