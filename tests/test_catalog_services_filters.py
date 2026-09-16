# -*- coding: utf-8 -*-
"""Services list placement filters — Platform → Section → Subsection via Catalog Structure."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from catalog_core.db import catalog_transaction
from catalog_core.errors import CatalogValidationError
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "catalog_services_filters.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE providers (slug TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '');
            INSERT INTO providers(slug, name) VALUES ('gozibra', 'Gozibra');
            CREATE TABLE provider_accounts (
                id INTEGER PRIMARY KEY,
                provider_slug TEXT NOT NULL,
                account_key TEXT NOT NULL DEFAULT 'default'
            );
            INSERT INTO provider_accounts(provider_slug, account_key) VALUES ('gozibra', 'default');
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def test_list_services_under_platform_section_subsection(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        ig = svc.create_node(name_ar="إنستغرام")
        likes = svc.create_node(name_ar="إعجابات", parent_entry_id=ig.entry_id)
        eco = svc.create_node(name_ar="اقتصادي", parent_entry_id=likes.entry_id)
        tt = svc.create_node(name_ar="تيك توك")
        tt_likes = svc.create_node(name_ar="إعجابات", parent_entry_id=tt.entry_id)

        s_ig_likes = svc.create_service(
            name_ar="إعجابات إنستغرام", parent_entry_id=likes.entry_id
        )
        s_ig_eco = svc.create_service(
            name_ar="إعجابات اقتصادية", parent_entry_id=eco.entry_id
        )
        s_tt = svc.create_service(
            name_ar="إعجابات تيك توك", parent_entry_id=tt_likes.entry_id
        )
        svc.create_service(name_ar="بدون مكان")

        under_ig, n_ig = svc.list_services(under_entry_id=ig.entry_id)
        assert n_ig == 2
        assert {s.id for s in under_ig} == {s_ig_likes.id, s_ig_eco.id}

        under_likes, n_likes = svc.list_services(under_entry_id=likes.entry_id)
        assert n_likes == 2
        assert {s.id for s in under_likes} == {s_ig_likes.id, s_ig_eco.id}

        under_eco, n_eco = svc.list_services(under_entry_id=eco.entry_id)
        assert n_eco == 1
        assert under_eco[0].id == s_ig_eco.id

        under_tt, n_tt = svc.list_services(under_entry_id=tt.entry_id)
        assert n_tt == 1
        assert under_tt[0].id == s_tt.id

        all_items, n_all = svc.list_services()
        assert n_all == 4
        assert len(all_items) == 4


def test_list_services_under_entry_combined_with_type(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        plat = svc.create_node(name_ar="فيسبوك")
        sec = svc.create_node(name_ar="مشاهدات", parent_entry_id=plat.entry_id)
        svc.create_service(
            name_ar="مشاهدات أ",
            parent_entry_id=sec.entry_id,
            service_type="views",
            status="active",
        )
        svc.create_service(
            name_ar="إعجابات أ",
            parent_entry_id=sec.entry_id,
            service_type="likes",
            status="active",
        )
        items, total = svc.list_services(
            under_entry_id=plat.entry_id, service_type="views"
        )
        assert total == 1
        assert items[0].service_type == "views"


def test_list_services_under_entry_rejects_service_entry(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        node = svc.create_node(name_ar="منصة")
        created = svc.create_service(name_ar="س", parent_entry_id=node.entry_id)
        with pytest.raises(CatalogValidationError):
            svc.list_services(under_entry_id=created.entry_id)


def test_services_template_has_placement_filters():
    root = Path(__file__).resolve().parent.parent
    html = (root / "templates" / "workspaces" / "catalog_services.html").read_text(
        encoding="utf-8"
    )
    assert 'id="svc-platform"' in html
    assert 'id="svc-section"' in html
    assert 'id="svc-subsection"' in html
    assert "المنصة" in html
    assert "القسم الفرعي" in html
    assert 'id="svc-clear-filters"' in html
    assert "مسح الفلاتر" in html

    js = (root / "static" / "js" / "catalog_core_ui.js").read_text(encoding="utf-8")
    assert "under_entry_id" in js
    assert "refreshPlacementFilters" in js
    assert "selectedUnderEntryId" in js
