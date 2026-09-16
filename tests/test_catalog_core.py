# -*- coding: utf-8 -*-
"""Catalog core domain tests — Phase 2."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.errors import CatalogConflictError, CatalogValidationError
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "catalog_core_test.db"
    # Minimal shared tables so isolation checks can assert they weren't touched.
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
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                service_id TEXT NOT NULL,
                service_name TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO orders(id, service_id, service_name) VALUES (1, 'legacy-1', 'قديم');
            CREATE TABLE providers (slug TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '');
            INSERT INTO providers(slug, name) VALUES ('gozibra', 'Gozibra');
            CREATE TABLE provider_accounts (
                id INTEGER PRIMARY KEY,
                provider_slug TEXT NOT NULL,
                account_key TEXT NOT NULL DEFAULT 'default'
            );
            INSERT INTO provider_accounts(provider_slug, account_key) VALUES ('gozibra', 'default');
            -- Old Catalog v2 tables (must remain untouched by new core)
            CREATE TABLE catalog_nodes (id TEXT PRIMARY KEY, title TEXT);
            INSERT INTO catalog_nodes(id, title) VALUES ('old', 'قديم');
            CREATE TABLE catalog_services (catalog_id TEXT PRIMARY KEY, name_ar TEXT);
            INSERT INTO catalog_services(catalog_id, name_ar) VALUES ('old-svc', 'قديم');
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def test_create_service_keeps_stable_identity(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="متابعون اقتصادي", note_ar="اختبار")
        sid = created.id
        assert sid.startswith("svc_")
        updated = svc.update_service(sid, name_ar="متابعون اقتصادي محدث")
        assert updated.id == sid
        again = svc.get_service(sid)
        assert again.id == sid
        assert again.name_ar == "متابعون اقتصادي محدث"


def test_unlimited_depth_tree(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        root = svc.create_node(name_ar="إنستغرام")
        a = svc.create_node(name_ar="متابعون", parent_entry_id=root.entry_id)
        b = svc.create_node(name_ar="اقتصادي", parent_entry_id=a.entry_id)
        c = svc.create_node(name_ar="سريع", parent_entry_id=b.entry_id)
        leaf = svc.create_service(name_ar="خدمة عميقة", parent_entry_id=c.entry_id)
        tree = svc.get_tree()
        assert tree[0]["name_ar"] == "إنستغرام"
        assert tree[0]["children"][0]["children"][0]["children"][0]["children"][0][
            "name_ar"
        ] == "خدمة عميقة"
        assert leaf.location_path == ["إنستغرام", "متابعون", "اقتصادي", "سريع"]


def test_mixed_nodes_and_services_ordering(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        root = svc.create_node(name_ar="تيك توك")
        n1 = svc.create_node(name_ar="متابعون", parent_entry_id=root.entry_id)
        s1 = svc.create_service(name_ar="خدمة أ", parent_entry_id=root.entry_id)
        n2 = svc.create_node(name_ar="إعجابات", parent_entry_id=root.entry_id)
        s2 = svc.create_service(name_ar="خدمة ب", parent_entry_id=root.entry_id)
        children = svc.list_children(root.entry_id)
        names = [c["name_ar"] for c in children]
        assert names == ["متابعون", "خدمة أ", "إعجابات", "خدمة ب"]
        # move service up
        svc.reorder_entry(s1.entry_id, position="up")
        names2 = [c["name_ar"] for c in svc.list_children(root.entry_id)]
        assert names2[0] == "خدمة أ"
        assert n1.id and n2.id and s2.id


def test_move_service_preserves_identity(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        ig = svc.create_node(name_ar="إنستغرام")
        likes = svc.create_node(name_ar="إعجابات", parent_entry_id=ig.entry_id)
        followers = svc.create_node(name_ar="متابعون", parent_entry_id=ig.entry_id)
        service = svc.create_service(
            name_ar="خدمة للنقل", parent_entry_id=followers.entry_id
        )
        sid = service.id
        moved = svc.move_service(sid, new_parent_entry_id=likes.entry_id)
        assert moved.id == sid
        assert moved.parent_entry_id == likes.entry_id
        assert moved.location_path == ["إنستغرام", "إعجابات"]


def test_move_node_moves_subtree(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        ig = svc.create_node(name_ar="إنستغرام")
        premium = svc.create_node(name_ar="مميز", parent_entry_id=ig.entry_id)
        followers = svc.create_node(name_ar="متابعون", parent_entry_id=ig.entry_id)
        economic = svc.create_node(name_ar="اقتصادي", parent_entry_id=followers.entry_id)
        s1 = svc.create_service(name_ar="أ", parent_entry_id=economic.entry_id)
        s2 = svc.create_service(name_ar="ب", parent_entry_id=economic.entry_id)
        svc.move_node(followers.id, new_parent_entry_id=premium.entry_id)
        s1b = svc.get_service(s1.id)
        s2b = svc.get_service(s2.id)
        assert s1b.id == s1.id and s2b.id == s2.id
        assert s1b.location_path[:3] == ["إنستغرام", "مميز", "متابعون"]


def test_invalid_move_into_descendant(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        a = svc.create_node(name_ar="أ")
        b = svc.create_node(name_ar="ب", parent_entry_id=a.entry_id)
        with pytest.raises(CatalogConflictError):
            svc.move_node(a.id, new_parent_entry_id=b.entry_id)


def test_cannot_place_under_service(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        root = svc.create_node(name_ar="جذر")
        service = svc.create_service(name_ar="ورقة", parent_entry_id=root.entry_id)
        with pytest.raises(CatalogValidationError):
            svc.create_node(name_ar="خطأ", parent_entry_id=service.entry_id)
        with pytest.raises(CatalogValidationError):
            svc.create_service(name_ar="خطأ2", parent_entry_id=service.entry_id)


def test_duplicate_placement_prevented(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        root = svc.create_node(name_ar="جذر")
        service = svc.create_service(name_ar="واحدة", parent_entry_id=root.entry_id)
        # Attempt second entry for same service via raw SQL should violate unique index
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO soldium_catalog_entries (
                    id, parent_entry_id, entry_type, node_id, service_id, sort_order
                ) VALUES ('ent_dup', NULL, 'service', NULL, ?, 99)
                """,
                (service.id,),
            )


def test_archive_and_restore_service(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="للأرشفة", status="active")
        archived = svc.archive_service(created.id)
        assert archived.status == "archived"
        assert archived.id == created.id
        restored = svc.restore_service(created.id, status="draft")
        assert restored.status == "draft"


def test_archive_node_with_children_blocked(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        root = svc.create_node(name_ar="أب")
        svc.create_service(name_ar="ابن", parent_entry_id=root.entry_id)
        with pytest.raises(CatalogConflictError):
            svc.archive_node(root.id)


def test_isolation_legacy_tables_untouched(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        svc.create_node(name_ar="جديد")
        svc.create_service(name_ar="خدمة جديدة")
        # legacy snapshots
        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == 1
        assert conn.execute("SELECT name_ar FROM smm_services").fetchone()[0] == "قديم"
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM provider_accounts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM catalog_nodes").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM catalog_services").fetchone()[0] == 1
        # new tables populated
        assert (
            conn.execute("SELECT COUNT(*) FROM soldium_catalog_services").fetchone()[0]
            == 1
        )


def test_app_registers_soldium_catalog_api():
    from main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/soldium-catalog/tree" in paths
    assert "/api/soldium-catalog/services" in paths
    assert "/catalog/structure" in paths
    assert "/catalog/services" in paths
    # old v2 API still absent
    assert not any(
        isinstance(p, str) and p.startswith("/api/catalog") for p in paths if p
    )


def test_ensure_migration_idempotent(catalog_db: Path):
    from catalog_core.schema import ensure_soldium_catalog_at_path

    ensure_soldium_catalog_at_path(catalog_db)
    ensure_soldium_catalog_at_path(catalog_db)
    conn = sqlite3.connect(catalog_db)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "soldium_catalog_services" in tables
        assert "soldium_catalog_nodes" in tables
        assert "soldium_catalog_entries" in tables
    finally:
        conn.close()
