# -*- coding: utf-8 -*-
"""Phase 6E — Apply mapping → Execution Source tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.errors import CatalogApplyError, CatalogNotFoundError
from catalog_core.provider_apply import ProviderApplyService
from catalog_core.provider_discovery import ProviderCatalogItem
from catalog_core.provider_mapping import ProviderMappingService
from catalog_core.provider_snapshot import ProviderSnapshotRepository
from catalog_core.schema import SOLDIUM_CATALOG_SCHEMA_VERSION, ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "catalog_apply_test.db"
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
            INSERT INTO providers(slug, name, api_base_url)
            VALUES ('other', 'Other', 'https://other.test/api/v2');
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
            INSERT INTO provider_accounts(provider_slug, account_key, display_name)
            VALUES ('other', 'main', 'Main');
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


def _item(eid: str, *, name: str = "IG HQ", rate: float = 1.5) -> ProviderCatalogItem:
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


def _snap(conn, items, *, when: str = "2026-01-02T00:00:00Z", slug="gozibra", account="default"):
    return ProviderSnapshotRepository(conn).insert_successful_snapshot(
        provider_slug=slug,
        provider_account_key=account,
        discovered_at=when,
        items=items,
    )


def _ready_without_source(core: CatalogCoreService, name: str = "Instagram Followers"):
    """Commercial + price + placement + target OK; no execution source yet."""
    svc = core.create_service(
        name_ar=name,
        service_type="followers",
        ordering_mode="quantity_based",
        min_quantity=100,
        max_quantity=10000,
        status="active",
        fulfillment_mode="auto",
        target_platform_key="instagram",
        target_section_key="followers",
    )
    core.change_price(svc.id, amount_dh="2", pricing_mode="per_1000", currency="MAD")
    return core.get_service(svc.id)


def _map(conn, service_id: str, external: str = "7890"):
    return ProviderMappingService(conn).create_or_change_mapping(
        provider_slug="gozibra",
        provider_account_key="default",
        external_service_id=external,
        soldium_service_id=service_id,
    ).current


def test_schema_unchanged_at_6():
    assert SOLDIUM_CATALOG_SCHEMA_VERSION == "10"


def test_apply_creates_source_for_ready_service(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_without_source(core)
        _snap(conn, [_item("7890")])
        mapping = _map(conn, svc.id, "7890")
        assert core.get_execution_source(svc.id) is None

        result = ProviderApplyService(conn).apply(mapping.id)
        assert result.outcome == "applied"
        assert result.unchanged is False
        src = core.get_execution_source(svc.id)
        assert src is not None
        assert src.provider_slug == "gozibra"
        assert src.provider_account_key == "default"
        assert src.external_service_id == "7890"
        assert isinstance(src.external_service_id, str)
        assert src.external_service_id != 7890


def test_apply_opaque_numeric_looking_id(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_without_source(core)
        _snap(conn, [_item("00123")])
        mapping = _map(conn, svc.id, "00123")
        ProviderApplyService(conn).apply(mapping.id)
        src = core.get_execution_source(svc.id)
        assert src.external_service_id == "00123"


def test_apply_idempotent_no_duplicate_history(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_without_source(core)
        _snap(conn, [_item("111")])
        mapping = _map(conn, svc.id, "111")
        ProviderApplyService(conn).apply(mapping.id)
        hist1 = core.list_execution_source_history(svc.id)
        again = ProviderApplyService(conn).apply(mapping.id)
        assert again.outcome == "no_change"
        assert again.unchanged is True
        hist2 = core.list_execution_source_history(svc.id)
        assert len(hist2) == len(hist1) == 1


def test_mapping_change_does_not_auto_apply(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_without_source(core)
        _snap(conn, [_item("111"), _item("222", name="B")])
        m1 = _map(conn, svc.id, "111")
        ProviderApplyService(conn).apply(m1.id)
        ProviderMappingService(conn).end_mapping_for_soldium(svc.id)
        ProviderMappingService(conn).create_or_change_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="222",
            soldium_service_id=svc.id,
        )
        src = core.get_execution_source(svc.id)
        assert src is not None
        assert src.external_service_id == "111"  # still old until Apply


def test_apply_after_remap_switches_source(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_without_source(core)
        _snap(conn, [_item("111"), _item("222")])
        m1 = _map(conn, svc.id, "111")
        ProviderApplyService(conn).apply(m1.id)
        ProviderMappingService(conn).end_mapping_for_soldium(svc.id)
        m2 = ProviderMappingService(conn).create_or_change_mapping(
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="222",
            soldium_service_id=svc.id,
        ).current
        result = ProviderApplyService(conn).apply(m2.id)
        assert result.outcome == "applied"
        assert result.previous_source["external_service_id"] == "111"
        assert result.current_source["external_service_id"] == "222"
        hist = core.list_execution_source_history(svc.id)
        assert len(hist) == 2
        assert sum(1 for h in hist if h.status == "active") == 1
        assert any(
            h.status == "historical" and h.external_service_id == "111" for h in hist
        )


def test_apply_needs_review_fails(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        # No price → needs_review even with proposed source
        svc = core.create_service(
            name_ar="ناقصة",
            service_type="followers",
            ordering_mode="quantity_based",
            min_quantity=1,
            max_quantity=100,
            status="active",
        )
        _snap(conn, [_item("9")])
        mapping = _map(conn, svc.id, "9")
        with pytest.raises(CatalogApplyError) as ei:
            ProviderApplyService(conn).apply(mapping.id)
        assert ei.value.code == "readiness_failed"


def test_apply_archived_fails(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_without_source(core)
        _snap(conn, [_item("9")])
        mapping = _map(conn, svc.id, "9")
        core.archive_service(svc.id)
        with pytest.raises(CatalogApplyError) as ei:
            ProviderApplyService(conn).apply(mapping.id)
        assert ei.value.code == "service_archived"


def test_apply_missing_mapping_fails(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        with pytest.raises(CatalogNotFoundError):
            ProviderApplyService(conn).apply("psm_missing")


def test_apply_missing_from_latest_snapshot_blocks(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_without_source(core)
        _snap(conn, [_item("keep")], when="2026-01-01T00:00:00Z")
        mapping = _map(conn, svc.id, "gone")
        # Latest snapshot does not include "gone"
        _snap(conn, [_item("keep")], when="2026-01-02T00:00:00Z")
        with pytest.raises(CatalogApplyError) as ei:
            ProviderApplyService(conn).apply(mapping.id)
        assert ei.value.code == "provider_identity_not_in_latest_snapshot"
        assert "آخر اكتشاف ناجح" in ei.value.message
        # Mapping preserved; no source created
        assert (
            ProviderMappingService(conn).get_active_mapping_for_provider(
                provider_slug="gozibra",
                provider_account_key="default",
                external_service_id="gone",
            )
            is not None
        )
        assert core.get_execution_source(svc.id) is None


def test_empty_successful_snapshot_blocks_apply(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_without_source(core)
        _snap(conn, [_item("9")], when="2026-01-01T00:00:00Z")
        mapping = _map(conn, svc.id, "9")
        _snap(conn, [], when="2026-01-02T00:00:00Z")
        with pytest.raises(CatalogApplyError) as ei:
            ProviderApplyService(conn).apply(mapping.id)
        assert ei.value.code == "provider_identity_not_in_latest_snapshot"


def test_failed_discovery_does_not_erase_successful_baseline(catalog_db: Path):
    """Failed discovery never becomes a successful snapshot — Apply still uses last success."""
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_without_source(core)
        _snap(conn, [_item("9")])
        mapping = _map(conn, svc.id, "9")
        # Insert a failed snapshot row (status != success) — should not affect Apply
        conn.execute(
            """
            INSERT INTO soldium_provider_catalog_snapshots (
                id, provider_slug, provider_account_key, status, discovered_at,
                item_count, error_code, error_message_ar, error_detail
            ) VALUES ('pcs_fail', 'gozibra', 'default', 'failed',
                      '2026-01-99T00:00:00Z', 0, 'api_failure', 'فشل', NULL)
            """
        )
        result = ProviderApplyService(conn).apply(mapping.id)
        assert result.outcome == "applied"


def test_changed_provider_metadata_does_not_block_apply(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_without_source(core)
        _snap(conn, [_item("9", name="Old")], when="2026-01-01T00:00:00Z")
        mapping = _map(conn, svc.id, "9")
        _snap(conn, [_item("9", name="New Name", rate=9.9)], when="2026-01-02T00:00:00Z")
        result = ProviderApplyService(conn).apply(mapping.id)
        assert result.outcome == "applied"
        after = core.get_service(svc.id)
        assert after.name_ar == "Instagram Followers"  # not copied from provider


def test_apply_does_not_mutate_mapping_snapshots_price_commercial_placement(
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
            status="active",
            fulfillment_mode="auto",
            target_platform_key="instagram",
            target_section_key="followers",
        )
        core.change_price(svc.id, amount_dh="5", pricing_mode="per_1000")
        _snap(conn, [_item("7890")])
        mapping = _map(conn, svc.id, "7890")
        map_count = conn.execute(
            "SELECT COUNT(*) FROM soldium_provider_service_mappings"
        ).fetchone()[0]
        snap_count = conn.execute(
            "SELECT COUNT(*) FROM soldium_provider_catalog_snapshots"
        ).fetchone()[0]
        item_count = conn.execute(
            "SELECT COUNT(*) FROM soldium_provider_catalog_snapshot_items"
        ).fetchone()[0]
        before_price = core.get_price(svc.id)
        before_parent = conn.execute(
            "SELECT parent_entry_id FROM soldium_catalog_entries WHERE service_id=?",
            (svc.id,),
        ).fetchone()[0]
        before_svc = core.get_service(svc.id)

        ProviderApplyService(conn).apply(mapping.id)

        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_provider_service_mappings"
            ).fetchone()[0]
            == map_count
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
        assert mapping.status == "active"
        after = core.get_service(svc.id)
        assert after.name_ar == before_svc.name_ar
        assert after.service_type == before_svc.service_type
        assert after.min_quantity == before_svc.min_quantity
        assert core.get_price(svc.id).amount_millimes == before_price.amount_millimes
        assert (
            conn.execute(
                "SELECT parent_entry_id FROM soldium_catalog_entries WHERE service_id=?",
                (svc.id,),
            ).fetchone()[0]
            == before_parent
        )
        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM catalog_services").fetchone()[0] == 1


def test_preview_is_readonly(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_without_source(core)
        _snap(conn, [_item("7890")])
        mapping = _map(conn, svc.id, "7890")
        preview = ProviderApplyService(conn).preview(mapping.id)
        assert preview.can_apply is True
        assert preview.would_change is True
        assert core.get_execution_source(svc.id) is None
        assert preview.readiness["ready"] is True


def test_preview_blocks_when_not_ready(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = core.create_service(name_ar="بلا سعر", status="active")
        _snap(conn, [_item("1")])
        mapping = _map(conn, svc.id, "1")
        preview = ProviderApplyService(conn).preview(mapping.id)
        assert preview.can_apply is False
        assert preview.blocking_code == "readiness_failed"


def test_concurrent_change_rejected(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_without_source(core)
        _snap(conn, [_item("7890"), _item("9999")])
        mapping = _map(conn, svc.id, "7890")
        # Someone applied a different source directly
        core.change_execution_source(
            svc.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="9999",
        )
        with pytest.raises(CatalogApplyError) as ei:
            ProviderApplyService(conn).apply(
                mapping.id,
                expect_no_current_source=True,
            )
        assert ei.value.code == "concurrent_change"


def test_apply_does_not_call_provider_api(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_without_source(core)
        _snap(conn, [_item("7890")])
        mapping = _map(conn, svc.id, "7890")
        with patch(
            "catalog_core.provider_discovery.fetch_provider_account_services"
        ) as fetch:
            ProviderApplyService(conn).apply(mapping.id)
            fetch.assert_not_called()


def test_one_active_source_after_apply(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        svc = _ready_without_source(core)
        _snap(conn, [_item("1"), _item("2")])
        core.change_execution_source(
            svc.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="1",
        )
        mapping = _map(conn, svc.id, "2")
        ProviderApplyService(conn).apply(mapping.id)
        active = [
            h
            for h in core.list_execution_source_history(svc.id)
            if h.status == "active"
        ]
        assert len(active) == 1
        assert active[0].external_service_id == "2"


def test_api_apply_routes_registered():
    from main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/soldium-catalog/provider-mappings/{mapping_id}/apply-preview" in paths
    assert "/api/soldium-catalog/provider-mappings/{mapping_id}/apply" in paths
