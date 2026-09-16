# -*- coding: utf-8 -*-
"""P0 — Catalog Admin target policy + fulfillment authoring."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.errors import CatalogPublishError, CatalogValidationError
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.commercial import normalize_fulfillment_mode


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "p0_authoring.db"
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
        VALUES ('gozibra', 'default', 'Main');
        CREATE TABLE smm_services (catalog_id TEXT PRIMARY KEY, name_ar TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY);
        """
    )
    ensure_soldium_catalog_schema(conn)
    conn.commit()
    conn.close()
    return path


def _complete(core: CatalogCoreService, svc_id: str) -> None:
    core.change_execution_source(
        svc_id,
        provider_slug="gozibra",
        provider_account_key="default",
        external_service_id="1896",
    )
    core.change_price(svc_id, amount_dh="3", pricing_mode="per_1000", currency="MAD")


# A B C — create + read back


def test_a_b_c_create_with_target_and_fulfillment(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = core.create_service(
            name_ar="خدمة جديدة",
            status="active",
            fulfillment_mode="admin",
            target_platform_key="tiktok",
            target_section_key="likes",
            target_link_type="comment",
        )
        got = core.get_service(svc.id)
        assert got.fulfillment_mode == "admin"
        assert got.target_platform_key == "tiktok"
        assert got.target_section_key == "likes"
        assert got.target_link_type == "comment"


# D E — update


def test_d_e_update_target_and_fulfillment(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = core.create_service(
            name_ar="تحديث",
            fulfillment_mode="auto",
            target_platform_key="instagram",
            target_section_key="followers",
        )
        core.update_service(
            svc.id,
            fulfillment_mode="admin",
            target_platform_key="x",
            target_section_key="followers",
            target_link_type="post",
        )
        got = core.get_service(svc.id)
        assert got.fulfillment_mode == "admin"
        assert got.target_platform_key == "x"
        assert got.target_section_key == "followers"
        assert got.target_link_type == "post"


# F G — invalid


def test_f_g_invalid_rejected(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        with pytest.raises(CatalogValidationError):
            core.create_service(name_ar="x", fulfillment_mode="magic")
        with pytest.raises(CatalogValidationError):
            normalize_fulfillment_mode("nope")
        svc = core.create_service(name_ar="ok")
        with pytest.raises(CatalogValidationError):
            core.update_service(svc.id, fulfillment_mode="bogus")


# H — omit fields preserves


def test_h_omit_preserves_existing(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = core.create_service(
            name_ar="حفظ",
            fulfillment_mode="admin",
            target_platform_key="telegram",
            target_section_key="members",
            target_link_type="comment",
        )
        core.update_service(svc.id, name_ar="حفظ محدّث")
        got = core.get_service(svc.id)
        assert got.name_ar == "حفظ محدّث"
        assert got.fulfillment_mode == "admin"
        assert got.target_platform_key == "telegram"
        assert got.target_section_key == "members"
        assert got.target_link_type == "comment"


# I J — readiness


def test_i_j_readiness_target_and_fulfillment(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = core.create_service(name_ar="جاهزية", status="active")
        _complete(core, svc.id)
        r1 = core.get_service_readiness(svc.id)
        assert r1.ready is False
        assert any(i.fix_action == "target" for i in r1.issues)

        core.update_service(
            svc.id,
            target_platform_key="instagram",
            target_section_key="interaction",
            fulfillment_mode="auto",
        )
        r2 = core.get_service_readiness(svc.id)
        assert r2.ready is True
        assert not any(i.fix_action == "target" for i in r2.issues)

        # Simulate invalid fulfillment on an in-memory service object
        from catalog_core.models import CatalogService
        from catalog_core.readiness import check_fulfillment_mode

        bad = CatalogService(
            id=svc.id,
            name_ar="x",
            fulfillment_mode="zzz",
        )
        r3 = check_fulfillment_mode(bad)
        assert r3.ok is False
        assert any(i.fix_action == "fulfillment" for i in r3.issues)


# K — UI fix action wiring present


def test_k_ui_fix_actions_open_editor_sections():
    js = Path("static/js/catalog_core_ui.js").read_text(encoding="utf-8")
    assert 'fix_action == "target"' not in js  # uses Set has
    assert 'actions.has("target")' in js
    assert 'actions.has("fulfillment")' in js
    assert 'id="btn-fix-target"' in js
    assert 'id="btn-fix-fulfillment"' in js
    assert 'openIdentityEditor("section-target")' in js
    assert 'openIdentityEditor("section-fulfillment")' in js
    assert 'id="section-target"' in js
    assert 'id="section-fulfillment"' in js
    assert "متطلبات الرابط / الهدف" in js
    assert "طريقة التنفيذ" in js
    assert "prompt(" not in js
    assert "confirm(" not in js
    assert "alert(" not in js


# L M N — publish gating


def test_l_m_n_publish_after_fixing_blockers(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        pub = CatalogPublicationService(conn)
        svc = core.create_service(
            name_ar="للنشر",
            status="active",
            fulfillment_mode="auto",
        )
        _complete(core, svc.id)
        # Still missing target → readiness blocks publish path
        assert core.get_service_readiness(svc.id).ready is False
        with pytest.raises(CatalogPublishError):
            pub.publish(svc.id)

        # Fix target only — still ok
        core.update_service(
            svc.id,
            target_platform_key="tiktok",
            target_section_key="likes",
        )
        assert core.get_service_readiness(svc.id).ready is True
        result = pub.publish(svc.id, published_by="p0_test")
        assert result.outcome in ("published", "no_change")
        st = pub.get_publication_status(svc.id)
        assert st["publication_status"] == "published"


def test_api_create_update_bodies_via_router_models(catalog_db: Path):
    from routers.api_catalog_core import CreateServiceBody, UpdateServiceBody

    created = CreateServiceBody(
        name_ar="من الواجهة",
        status="active",
        fulfillment_mode="admin",
        target_platform_key="facebook",
        target_section_key="followers_members",
        target_link_type="post",
    )
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = core.create_service(**created.model_dump(exclude_unset=True))
        assert svc.fulfillment_mode == "admin"
        assert svc.target_platform_key == "facebook"

        patch = UpdateServiceBody(
            fulfillment_mode="auto", target_section_key="likes"
        )
        updated = core.update_service(
            svc.id, **patch.model_dump(exclude_unset=True)
        )
        assert updated.fulfillment_mode == "auto"
        assert updated.target_platform_key == "facebook"
        assert updated.target_section_key == "likes"


def test_commercial_options_include_authoring_meta():
    meta = CatalogCoreService.commercial_options()
    assert "fulfillment_modes" in meta
    assert "target_platforms" in meta
    assert "target_link_types" in meta
    assert any(m["code"] == "auto" for m in meta["fulfillment_modes"])
    assert any(p["code"] == "instagram" for p in meta["target_platforms"])
