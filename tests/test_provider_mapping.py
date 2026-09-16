# -*- coding: utf-8 -*-
"""Phase 6D — Provider ↔ SOLDIUM service mapping tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.errors import (
    CatalogConflictError,
    CatalogNotFoundError,
    CatalogValidationError,
)
from catalog_core.provider_discovery import (
    ProviderCatalogItem,
    discover_provider_catalog,
)
from catalog_core.provider_mapping import ProviderMappingService
from catalog_core.provider_review import list_provider_review_items
from catalog_core.provider_snapshot import (
    ProviderSnapshotRepository,
    compute_provider_catalog_diff,
)
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.schema import SOLDIUM_CATALOG_SCHEMA_VERSION, ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from services.provider_registry import ProviderAccountRecord, ProviderRecord


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "catalog_mapping_test.db"
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
                adapter_type TEXT NOT NULL DEFAULT 'gozibra_v2',
                is_active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO providers(slug, name, api_base_url)
            VALUES ('gozibra', 'Gozibra', 'https://example.test/api/v2');
            CREATE TABLE provider_accounts (
                id INTEGER PRIMARY KEY,
                provider_slug TEXT NOT NULL,
                account_key TEXT NOT NULL,
                api_key_env TEXT NOT NULL DEFAULT 'SMM_KEY_DEFAULT',
                display_name TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(provider_slug, account_key)
            );
            INSERT INTO provider_accounts(provider_slug, account_key, display_name)
            VALUES ('gozibra', 'default', 'Main');
            CREATE TABLE catalog_services (catalog_id TEXT PRIMARY KEY, name_ar TEXT);
            INSERT INTO catalog_services VALUES ('old', 'قديم');
            CREATE TABLE catalog_nodes (id TEXT PRIMARY KEY);
            CREATE TABLE service_provider_bindings (id INTEGER PRIMARY KEY);
            CREATE TABLE price_rules (id INTEGER PRIMARY KEY);
            CREATE TABLE provider_inventory (id INTEGER PRIMARY KEY);
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _item(eid: str, *, name: str = "Instagram Followers HQ", rate: float = 1.5) -> ProviderCatalogItem:
    return ProviderCatalogItem(
        provider_slug="gozibra",
        provider_account_key="default",
        external_service_id=eid,
        provider_service_name=name,
        provider_category="IG",
        provider_type="Default",
        provider_description=None,
        min_quantity=10,
        max_quantity=1000,
        provider_rate=rate,
        refill=None,
        cancel=None,
        dripfeed=None,
    )


def _snap(repo: ProviderSnapshotRepository, items, *, when: str):
    return repo.insert_successful_snapshot(
        provider_slug="gozibra",
        provider_account_key="default",
        discovered_at=when,
        items=items,
    )


def test_schema_version_6_and_mapping_table(catalog_db: Path):
    assert SOLDIUM_CATALOG_SCHEMA_VERSION == "10"
    with catalog_transaction(catalog_db) as conn:
        ver = conn.execute(
            "SELECT value FROM soldium_catalog_schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert ver == "10"
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='soldium_provider_service_mappings'"
        ).fetchone()
        assert row is not None


def test_create_retrieve_list_mapping(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn).create_service(name_ar="Instagram Followers")
        ms = ProviderMappingService(conn)
        result = ms.create_or_change_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="7890",
            soldium_service_id=svc.id,
        )
        assert result.unchanged is False
        assert result.current is not None
        assert result.current.external_service_id == "7890"
        assert isinstance(result.current.external_service_id, str)
        assert result.current.soldium_service_id == svc.id
        assert result.current.status == "active"

        got = ms.get_active_mapping_for_provider(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="7890",
        )
        assert got is not None
        assert got.id == result.current.id

        items, total = ms.list_active_mappings(provider_slug="gozibra")
        assert total == 1
        assert items[0].soldium_service_name_ar == "Instagram Followers"


def test_provider_uniqueness_change_preserves_history(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        a = CatalogCoreService(conn).create_service(name_ar="A")
        b = CatalogCoreService(conn).create_service(name_ar="B")
        ms = ProviderMappingService(conn)
        ms.create_or_change_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="100",
            soldium_service_id=a.id,
        )
        changed = ms.create_or_change_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="100",
            soldium_service_id=b.id,
        )
        assert changed.previous is not None
        active = ms.get_active_mapping_for_provider(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="100",
        )
        assert active is not None
        assert active.soldium_service_id == b.id
        hist = ms.list_mapping_history(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="100",
        )
        assert len(hist) == 2
        assert sum(1 for h in hist if h.status == "active") == 1
        assert sum(1 for h in hist if h.status == "historical") == 1


def test_soldium_uniqueness_one_active_mapping(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn).create_service(name_ar="Shared")
        ms = ProviderMappingService(conn)
        ms.create_or_change_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="1",
            soldium_service_id=svc.id,
        )
        with pytest.raises(CatalogConflictError):
            ms.create_or_change_mapping(
                provider_slug="gozibra",
                provider_account_key="default",
                external_service_id="2",
                soldium_service_id=svc.id,
            )


def test_unmap_preserves_history(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        a = CatalogCoreService(conn).create_service(name_ar="A")
        ms = ProviderMappingService(conn)
        ms.create_or_change_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="55",
            soldium_service_id=a.id,
        )
        ended = ms.end_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="55",
        )
        assert ended.current is None
        assert ended.previous is not None
        assert ended.previous.status == "historical"
        assert ended.previous.ended_at is not None
        hist = ms.list_mapping_history(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="55",
        )
        assert len(hist) == 1
        assert hist[0].status == "historical"


def test_validation_failures(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn).create_service(name_ar="Ok")
        ms = ProviderMappingService(conn)
        with pytest.raises(CatalogNotFoundError):
            ms.create_or_change_mapping(
                provider_slug="gozibra",
                provider_account_key="default",
                external_service_id="9",
                soldium_service_id="svc_missing",
            )
        with pytest.raises(CatalogValidationError):
            ms.create_or_change_mapping(
                provider_slug="nope",
                provider_account_key="default",
                external_service_id="9",
                soldium_service_id=svc.id,
            )
        with pytest.raises(CatalogValidationError):
            ms.create_or_change_mapping(
                provider_slug="gozibra",
                provider_account_key="wrong",
                external_service_id="9",
                soldium_service_id=svc.id,
            )
        with pytest.raises(CatalogValidationError):
            ms.create_or_change_mapping(
                provider_slug="gozibra",
                provider_account_key="default",
                external_service_id="  ",
                soldium_service_id=svc.id,
            )


def test_archived_soldium_rejected(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn).create_service(name_ar="Arch")
        CatalogCoreService(conn).archive_service(svc.id)
        ms = ProviderMappingService(conn)
        with pytest.raises(CatalogValidationError, match="مؤرشفة"):
            ms.create_or_change_mapping(
                provider_slug="gozibra",
                provider_account_key="default",
                external_service_id="9",
                soldium_service_id=svc.id,
            )


def test_mapping_does_not_touch_execution_price_commercial_placement_readiness(
    catalog_db: Path,
):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        node = core.create_node(name_ar="قسم")
        svc = core.create_service(
            name_ar="خدمة",
            parent_entry_id=node.entry_id,
            service_type="followers",
            ordering_mode="quantity_based",
            min_quantity=10,
            max_quantity=100,
        )
        core.change_execution_source(
            svc.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="exec-1",
        )
        core.change_price(svc.id, pricing_mode="per_1000", amount_dh="5")
        before = core.get_service(svc.id)
        before_src = core.get_execution_source(svc.id)
        before_price = core.get_price(svc.id)
        before_ready = evaluate_service_readiness(core.repo, before)
        entry_parent = conn.execute(
            "SELECT parent_entry_id FROM soldium_catalog_entries WHERE service_id=?",
            (svc.id,),
        ).fetchone()[0]

        ms = ProviderMappingService(conn)
        ms.create_or_change_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="7890",
            soldium_service_id=svc.id,
        )
        ms.end_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="7890",
        )

        after = core.get_service(svc.id)
        after_src = core.get_execution_source(svc.id)
        after_price = core.get_price(svc.id)
        after_ready = evaluate_service_readiness(core.repo, after)
        after_parent = conn.execute(
            "SELECT parent_entry_id FROM soldium_catalog_entries WHERE service_id=?",
            (svc.id,),
        ).fetchone()[0]

        assert after.name_ar == before.name_ar
        assert after.service_type == before.service_type
        assert after.min_quantity == before.min_quantity
        assert after_src is not None and before_src is not None
        assert after_src.external_service_id == before_src.external_service_id == "exec-1"
        assert after_price is not None and before_price is not None
        assert after_price.amount_millimes == before_price.amount_millimes
        assert after_parent == entry_parent
        assert after_ready.state == before_ready.state
        assert after_ready.ready == before_ready.ready


def test_missing_and_changed_do_not_auto_unmap(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn).create_service(name_ar="Keep")
        ms = ProviderMappingService(conn)
        ms.create_or_change_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="keep-1",
            soldium_service_id=svc.id,
        )
        repo = ProviderSnapshotRepository(conn)
        _snap(repo, [_item("keep-1", name="Old Name"), _item("other")], when="2026-01-01T00:00:00Z")
        _snap(repo, [_item("keep-1", name="New Name")], when="2026-01-02T00:00:00Z")
        still = ms.get_active_mapping_for_provider(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="keep-1",
        )
        assert still is not None
        assert still.soldium_service_id == svc.id
        items, _, _ = list_provider_review_items(conn)
        assert any(i.change_type == "changed" for i in items)
        assert any(i.change_type == "missing" for i in items)


def test_external_id_change_does_not_auto_create_or_remap(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn).create_service(name_ar="Stable")
        ms = ProviderMappingService(conn)
        ms.create_or_change_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="12345",
            soldium_service_id=svc.id,
        )
        before_count = conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_services"
        ).fetchone()[0]
        assert (
            ms.get_active_mapping_for_provider(
                provider_slug="gozibra",
                provider_account_key="default",
                external_service_id="67890",
            )
            is None
        )
        old = ms.get_active_mapping_for_provider(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="12345",
        )
        assert old is not None
        after_count = conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_services"
        ).fetchone()[0]
        assert after_count == before_count


def test_review_inbox_shows_mapping_status(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn).create_service(name_ar="Instagram Followers")
        repo = ProviderSnapshotRepository(conn)
        _snap(repo, [_item("1")], when="2026-01-01T00:00:00Z")
        _snap(repo, [_item("1"), _item("2", name="New")], when="2026-01-02T00:00:00Z")
        ms = ProviderMappingService(conn)
        ms.create_or_change_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="2",
            soldium_service_id=svc.id,
        )
        items, _, _ = list_provider_review_items(conn, change_type="new")
        assert len(items) == 1
        assert items[0].mapping_status["mapped"] is True
        assert "Instagram Followers" in items[0].soldium_link_note_ar


def test_unmapped_review_label(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        repo = ProviderSnapshotRepository(conn)
        _snap(repo, [], when="2026-01-01T00:00:00Z")
        _snap(repo, [_item("9")], when="2026-01-02T00:00:00Z")
        items, _, _ = list_provider_review_items(conn, change_type="new")
        assert items[0].soldium_link_note_ar == "غير مرتبط"
        assert items[0].mapping_status["mapped"] is False


def test_snapshot_history_intact_after_mapping(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        repo = ProviderSnapshotRepository(conn)
        prev = _snap(repo, [_item("1")], when="2026-01-01T00:00:00Z")
        cur = _snap(repo, [_item("1"), _item("2")], when="2026-01-02T00:00:00Z")
        snap_count = conn.execute(
            "SELECT COUNT(*) FROM soldium_provider_catalog_snapshots"
        ).fetchone()[0]
        item_count = conn.execute(
            "SELECT COUNT(*) FROM soldium_provider_catalog_snapshot_items"
        ).fetchone()[0]
        svc = CatalogCoreService(conn).create_service(name_ar="X")
        ProviderMappingService(conn).create_or_change_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="2",
            soldium_service_id=svc.id,
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_provider_catalog_snapshots"
            ).fetchone()[0]
            == snap_count
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_provider_catalog_snapshot_items"
            ).fetchone()[0]
            == item_count
        )
        diff = compute_provider_catalog_diff(
            provider_slug="gozibra",
            provider_account_key="default",
            previous_snapshot=prev,
            previous_items=repo.load_snapshot_items(
                prev.id, provider_slug="gozibra", provider_account_key="default"
            ),
            current_snapshot=cur,
            current_items=repo.load_snapshot_items(
                cur.id, provider_slug="gozibra", provider_account_key="default"
            ),
        )
        assert any(i.external_service_id == "2" for i in diff.new)


def test_no_legacy_mutations(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn).create_service(name_ar="X")
        ProviderMappingService(conn).create_or_change_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="9",
            soldium_service_id=svc.id,
        )
        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM catalog_services").fetchone()[0] == 1
        assert (
            conn.execute("SELECT name FROM providers WHERE slug='gozibra'").fetchone()[0]
            == "Gozibra"
        )


def test_api_mapping_routes_registered():
    from main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/soldium-catalog/provider-mappings" in paths
    assert "/api/soldium-catalog/provider-mappings/lookup" in paths
    assert "/api/soldium-catalog/provider-mappings/history" in paths
    assert "/api/soldium-catalog/provider-mappings/unmap" in paths


def test_duplicate_external_ids_rejected_in_discovery(monkeypatch):
    """Phase 6A smallest fix: duplicates → malformed, not last-wins."""
    provider = ProviderRecord(
        slug="gozibra",
        name="Gozibra",
        api_base_url="https://example.test/api/v2",
        adapter_type="gozibra_v2",
        is_active=True,
    )
    account = ProviderAccountRecord(
        provider_slug="gozibra",
        account_key="default",
        api_key_env="SMM_KEY_DEFAULT",
        is_active=True,
        display_name="Main",
    )
    monkeypatch.setattr(
        "catalog_core.provider_discovery.get_provider_record",
        lambda slug: provider if slug == "gozibra" else None,
    )
    monkeypatch.setattr(
        "catalog_core.provider_discovery.get_provider_account_record",
        lambda slug, key: account if slug == "gozibra" and key == "default" else None,
    )
    monkeypatch.setattr(
        "catalog_core.provider_discovery.fetch_provider_account_services",
        AsyncMock(
            return_value=[
                {
                    "service": 100,
                    "name": "A",
                    "type": "Default",
                    "rate": "1",
                    "min": "1",
                    "max": "10",
                },
                {
                    "service": 100,
                    "name": "B",
                    "type": "Default",
                    "rate": "2",
                    "min": "1",
                    "max": "10",
                },
            ]
        ),
    )
    result = asyncio.run(
        discover_provider_catalog(provider_slug="gozibra", account_key="default")
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "malformed"
    assert result.item_count == 0
