# -*- coding: utf-8 -*-
"""Phase 6B — Provider Catalog snapshot & diff tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.errors import CatalogValidationError
from catalog_core.provider_discovery import (
    ProviderCatalogItem,
    ProviderDiscoveryError,
    ProviderDiscoveryResult,
)
from catalog_core.provider_snapshot import (
    ProviderCatalogSnapshot,
    ProviderSnapshotRepository,
    assert_unique_external_ids,
    compute_provider_catalog_diff,
    discover_snapshot_and_diff,
)
from catalog_core.schema import SOLDIUM_CATALOG_SCHEMA_VERSION, ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "catalog_snapshot_test.db"
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
            VALUES ('gozibra', 'default', 'افتراضي');
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


def _item(
    eid: str,
    *,
    name: str | None = "Svc",
    category: str | None = "Cat",
    rate: float | None = 1.0,
    min_q: int | None = 10,
    max_q: int | None = 1000,
    refill: bool | None = None,
    **extra,
) -> ProviderCatalogItem:
    return ProviderCatalogItem(
        provider_slug="gozibra",
        provider_account_key="default",
        external_service_id=eid,
        provider_service_name=name,
        provider_category=category,
        provider_type=extra.get("provider_type", "Default"),
        provider_description=extra.get("provider_description"),
        min_quantity=min_q,
        max_quantity=max_q,
        provider_rate=rate,
        refill=refill,
        cancel=extra.get("cancel"),
        dripfeed=extra.get("dripfeed"),
    )


def _ok_discovery(items: list[ProviderCatalogItem]) -> ProviderDiscoveryResult:
    return ProviderDiscoveryResult(
        provider_slug="gozibra",
        provider_name="Gozibra",
        provider_account_key="default",
        account_display_name="افتراضي",
        discovered_at="2026-01-01T12:00:00+00:00",
        ok=True,
        item_count=len(items),
        items=items,
        error=None,
    )


def _fail_discovery() -> ProviderDiscoveryResult:
    return ProviderDiscoveryResult(
        provider_slug="gozibra",
        provider_name="Gozibra",
        provider_account_key="default",
        account_display_name="افتراضي",
        discovered_at="2026-01-01T13:00:00+00:00",
        ok=False,
        item_count=0,
        items=[],
        error=ProviderDiscoveryError(
            code="api_failure",
            message_ar="فشل طلب كتالوج المزوّد.",
            detail="500",
        ),
    )


def _patch_discovery(monkeypatch, result: ProviderDiscoveryResult):
    async def _fake(*, provider_slug: str, account_key: str):
        return result

    monkeypatch.setattr(
        "catalog_core.provider_snapshot.discover_provider_catalog",
        _fake,
    )


def test_schema_version_6_and_tables(catalog_db: Path):
    assert SOLDIUM_CATALOG_SCHEMA_VERSION == "10"
    with catalog_transaction(catalog_db) as conn:
        ver = conn.execute(
            "SELECT value FROM soldium_catalog_schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert ver == "10"
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "soldium_provider_catalog_snapshots" in tables
        assert "soldium_provider_catalog_snapshot_items" in tables


def test_first_snapshot_is_baseline(catalog_db: Path, monkeypatch):
    items = [_item("A"), _item("B")]
    _patch_discovery(monkeypatch, _ok_discovery(items))
    with catalog_transaction(catalog_db) as conn:
        result = asyncio.run(
            discover_snapshot_and_diff(
                conn, provider_slug="gozibra", account_key="default"
            )
        )
        assert result.ok is True
        assert result.diff is not None
        assert result.diff.is_baseline is True
        assert result.diff.new == []
        assert result.diff.changed == []
        assert result.diff.missing == []
        assert result.diff.unchanged == []
        assert result.snapshot is not None
        assert result.snapshot.status == "success"
        assert result.snapshot.item_count == 2


def test_identical_second_snapshot_all_unchanged(catalog_db: Path, monkeypatch):
    items = [_item("A"), _item("B"), _item("C")]
    _patch_discovery(monkeypatch, _ok_discovery(items))
    with catalog_transaction(catalog_db) as conn:
        asyncio.run(
            discover_snapshot_and_diff(
                conn, provider_slug="gozibra", account_key="default"
            )
        )
    second = _ok_discovery(items)
    second.discovered_at = "2026-01-02T12:00:00+00:00"
    _patch_discovery(monkeypatch, second)
    with catalog_transaction(catalog_db) as conn:
        result = asyncio.run(
            discover_snapshot_and_diff(
                conn, provider_slug="gozibra", account_key="default"
            )
        )
        assert result.diff is not None
        assert result.diff.is_baseline is False
        assert len(result.diff.unchanged) == 3
        assert result.diff.new == []
        assert result.diff.changed == []
        assert result.diff.missing == []


def test_new_and_missing_and_changed(catalog_db: Path, monkeypatch):
    first = [_item("A", name="Alpha"), _item("B", name="Beta"), _item("C")]
    _patch_discovery(monkeypatch, _ok_discovery(first))
    with catalog_transaction(catalog_db) as conn:
        asyncio.run(
            discover_snapshot_and_diff(
                conn, provider_slug="gozibra", account_key="default"
            )
        )

    second_items = [
        _item("A", name="Alpha Renamed", rate=2.5),
        _item("C"),
        _item("D", name="Delta"),
    ]
    second = _ok_discovery(second_items)
    second.discovered_at = "2026-01-02T12:00:00+00:00"
    _patch_discovery(monkeypatch, second)
    with catalog_transaction(catalog_db) as conn:
        result = asyncio.run(
            discover_snapshot_and_diff(
                conn, provider_slug="gozibra", account_key="default"
            )
        )
        diff = result.diff
        assert diff is not None
        assert {i.external_service_id for i in diff.new} == {"D"}
        assert {i.external_service_id for i in diff.missing} == {"B"}
        assert {i.external_service_id for i in diff.unchanged} == {"C"}
        assert len(diff.changed) == 1
        ch = diff.changed[0]
        assert ch.current.external_service_id == "A"
        assert set(ch.changed_fields) == {"provider_service_name", "provider_rate"}


def test_order_independent(catalog_db: Path, monkeypatch):
    first = [_item("A"), _item("B"), _item("C")]
    _patch_discovery(monkeypatch, _ok_discovery(first))
    with catalog_transaction(catalog_db) as conn:
        asyncio.run(
            discover_snapshot_and_diff(
                conn, provider_slug="gozibra", account_key="default"
            )
        )
    reordered = [_item("C"), _item("A"), _item("B")]
    second = _ok_discovery(reordered)
    second.discovered_at = "2026-01-02T12:00:00+00:00"
    _patch_discovery(monkeypatch, second)
    with catalog_transaction(catalog_db) as conn:
        result = asyncio.run(
            discover_snapshot_and_diff(
                conn, provider_slug="gozibra", account_key="default"
            )
        )
        assert len(result.diff.unchanged) == 3
        assert result.diff.changed == []
        assert result.diff.new == []
        assert result.diff.missing == []


def test_empty_successful_catalog_marks_previous_missing(
    catalog_db: Path, monkeypatch
):
    first = [_item("A"), _item("B")]
    _patch_discovery(monkeypatch, _ok_discovery(first))
    with catalog_transaction(catalog_db) as conn:
        asyncio.run(
            discover_snapshot_and_diff(
                conn, provider_slug="gozibra", account_key="default"
            )
        )
    empty = _ok_discovery([])
    empty.discovered_at = "2026-01-02T12:00:00+00:00"
    _patch_discovery(monkeypatch, empty)
    with catalog_transaction(catalog_db) as conn:
        result = asyncio.run(
            discover_snapshot_and_diff(
                conn, provider_slug="gozibra", account_key="default"
            )
        )
        assert result.ok is True
        assert result.snapshot is not None
        assert result.snapshot.item_count == 0
        assert {i.external_service_id for i in result.diff.missing} == {"A", "B"}


def test_failed_discovery_no_missing_preserves_baseline(
    catalog_db: Path, monkeypatch
):
    first = [_item("A"), _item("B")]
    _patch_discovery(monkeypatch, _ok_discovery(first))
    with catalog_transaction(catalog_db) as conn:
        first_run = asyncio.run(
            discover_snapshot_and_diff(
                conn, provider_slug="gozibra", account_key="default"
            )
        )
        baseline_id = first_run.snapshot.id

    _patch_discovery(monkeypatch, _fail_discovery())
    with catalog_transaction(catalog_db) as conn:
        result = asyncio.run(
            discover_snapshot_and_diff(
                conn, provider_slug="gozibra", account_key="default"
            )
        )
        assert result.ok is False
        assert result.diff is None
        assert result.previous_snapshot_preserved is True
        repo = ProviderSnapshotRepository(conn)
        latest = repo.get_latest_successful_snapshot("gozibra", "default")
        assert latest is not None
        assert latest.id == baseline_id
        assert latest.item_count == 2
        history = repo.list_snapshots("gozibra", "default")
        assert any(s.status == "failed" for s in history)


def test_external_ids_opaque_and_distinct(catalog_db: Path):
    a = _item("123")
    b = _item("00123")
    assert a.external_service_id != b.external_service_id
    assert isinstance(a.external_service_id, str)
    with catalog_transaction(catalog_db) as conn:
        repo = ProviderSnapshotRepository(conn)
        snap = repo.insert_successful_snapshot(
            provider_slug="gozibra",
            provider_account_key="default",
            discovered_at="2026-01-01T00:00:00+00:00",
            items=[a, b],
        )
        loaded = repo.load_snapshot_items(
            snap.id, provider_slug="gozibra", provider_account_key="default"
        )
        ids = {i.external_service_id for i in loaded}
        assert ids == {"123", "00123"}


def test_duplicate_external_ids_rejected(catalog_db: Path):
    items = [_item("dup"), _item("dup", name="Other")]
    with pytest.raises(CatalogValidationError):
        assert_unique_external_ids(items)
    with catalog_transaction(catalog_db) as conn:
        repo = ProviderSnapshotRepository(conn)
        with pytest.raises(CatalogValidationError):
            repo.insert_successful_snapshot(
                provider_slug="gozibra",
                provider_account_key="default",
                discovered_at="2026-01-01T00:00:00+00:00",
                items=items,
            )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_provider_catalog_snapshots"
            ).fetchone()[0]
            == 0
        )


def test_failed_persist_rolls_back_incomplete_snapshot(catalog_db: Path):
    try:
        with catalog_transaction(catalog_db) as conn:
            repo = ProviderSnapshotRepository(conn)
            snap = repo.insert_successful_snapshot(
                provider_slug="gozibra",
                provider_account_key="default",
                discovered_at="2026-01-02T00:00:00+00:00",
                items=[_item("Z")],
            )
            conn.execute(
                "INSERT INTO soldium_provider_catalog_snapshot_items "
                "(id, snapshot_id, external_service_id) VALUES ('x2', ?, 'Z')",
                (snap.id,),
            )
    except sqlite3.IntegrityError:
        pass

    with catalog_transaction(catalog_db) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM soldium_provider_catalog_snapshots "
            "WHERE discovered_at = '2026-01-02T00:00:00+00:00'"
        ).fetchone()[0]
        assert rows == 0
        assert (
            ProviderSnapshotRepository(conn).get_latest_successful_snapshot(
                "gozibra", "default"
            )
            is None
        )


def test_history_kept_latest_deterministic(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        repo = ProviderSnapshotRepository(conn)
        s1 = repo.insert_successful_snapshot(
            provider_slug="gozibra",
            provider_account_key="default",
            discovered_at="2026-01-01T00:00:00+00:00",
            items=[_item("A")],
        )
        s2 = repo.insert_successful_snapshot(
            provider_slug="gozibra",
            provider_account_key="default",
            discovered_at="2026-01-02T00:00:00+00:00",
            items=[_item("A"), _item("B")],
        )
        latest = repo.get_latest_successful_snapshot("gozibra", "default")
        assert latest is not None
        assert latest.id == s2.id
        history = repo.list_snapshots("gozibra", "default")
        assert len(history) == 2
        assert {s1.id, s2.id} == {h.id for h in history}


def test_optional_fields_null_vs_false():
    prev = _item("A", refill=None)
    curr = _item("A", refill=False)
    prev_snap = ProviderCatalogSnapshot(
        id="p",
        provider_slug="gozibra",
        provider_account_key="default",
        status="success",
        discovered_at="t1",
        item_count=1,
    )
    curr_snap = ProviderCatalogSnapshot(
        id="c",
        provider_slug="gozibra",
        provider_account_key="default",
        status="success",
        discovered_at="t2",
        item_count=1,
    )
    diff = compute_provider_catalog_diff(
        provider_slug="gozibra",
        provider_account_key="default",
        previous_snapshot=prev_snap,
        previous_items=[prev],
        current_snapshot=curr_snap,
        current_items=[curr],
    )
    assert len(diff.changed) == 1
    assert diff.changed[0].changed_fields == ["refill"]


def test_metadata_not_compared():
    item = _item("A")
    prev_snap = ProviderCatalogSnapshot(
        id="snap_old",
        provider_slug="gozibra",
        provider_account_key="default",
        status="success",
        discovered_at="t1",
        item_count=1,
    )
    curr_snap = ProviderCatalogSnapshot(
        id="snap_new",
        provider_slug="gozibra",
        provider_account_key="default",
        status="success",
        discovered_at="t2",
        item_count=1,
    )
    diff = compute_provider_catalog_diff(
        provider_slug="gozibra",
        provider_account_key="default",
        previous_snapshot=prev_snap,
        previous_items=[item],
        current_snapshot=curr_snap,
        current_items=[_item("A")],
    )
    assert len(diff.unchanged) == 1
    assert diff.changed == []


def test_no_soldium_or_legacy_mutation(catalog_db: Path, monkeypatch):
    _patch_discovery(monkeypatch, _ok_discovery([_item("999")]))
    with catalog_transaction(catalog_db) as conn:
        before = {
            "svc": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_services"
            ).fetchone()[0],
            "src": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_execution_sources"
            ).fetchone()[0],
            "price": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_prices"
            ).fetchone()[0],
            "smm": conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0],
            "orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            "providers": conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0],
            "accounts": conn.execute(
                "SELECT COUNT(*) FROM provider_accounts"
            ).fetchone()[0],
            "legacy": conn.execute(
                "SELECT name_ar FROM catalog_services WHERE catalog_id='old'"
            ).fetchone()[0],
            "inv": conn.execute(
                "SELECT COUNT(*) FROM provider_inventory"
            ).fetchone()[0],
        }
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="خدمة موجودة")
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="keep",
        )
        svc.change_price(created.id, amount_dh="1", pricing_mode="per_1000")

    with catalog_transaction(catalog_db) as conn:
        result = asyncio.run(
            discover_snapshot_and_diff(
                conn, provider_slug="gozibra", account_key="default"
            )
        )
        assert result.ok is True
        assert (
            conn.execute("SELECT COUNT(*) FROM soldium_catalog_services").fetchone()[0]
            == before["svc"] + 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_execution_sources"
            ).fetchone()[0]
            == before["src"] + 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM soldium_catalog_prices").fetchone()[0]
            == before["price"] + 1
        )
        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == before[
            "smm"
        ]
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == before[
            "orders"
        ]
        assert conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0] == before[
            "providers"
        ]
        assert (
            conn.execute("SELECT COUNT(*) FROM provider_accounts").fetchone()[0]
            == before["accounts"]
        )
        assert (
            conn.execute(
                "SELECT name_ar FROM catalog_services WHERE catalog_id='old'"
            ).fetchone()[0]
            == before["legacy"]
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM provider_inventory").fetchone()[0]
            == before["inv"]
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_execution_sources "
                "WHERE external_service_id = '999'"
            ).fetchone()[0]
            == 0
        )


def test_api_routes_registered():
    from main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/soldium-catalog/provider-discovery/snapshot" in paths
    assert "/api/soldium-catalog/provider-snapshots/latest" in paths


def test_diff_does_not_compare_snapshot_to_itself(catalog_db: Path, monkeypatch):
    items = [_item("X")]
    _patch_discovery(monkeypatch, _ok_discovery(items))
    with catalog_transaction(catalog_db) as conn:
        r1 = asyncio.run(
            discover_snapshot_and_diff(
                conn, provider_slug="gozibra", account_key="default"
            )
        )
    second = _ok_discovery(items)
    second.discovered_at = "2026-01-03T00:00:00+00:00"
    _patch_discovery(monkeypatch, second)
    with catalog_transaction(catalog_db) as conn:
        r2 = asyncio.run(
            discover_snapshot_and_diff(
                conn, provider_slug="gozibra", account_key="default"
            )
        )
        assert r2.diff.previous_snapshot_id == r1.snapshot.id
        assert r2.diff.current_snapshot_id != r1.snapshot.id
        assert len(r2.diff.unchanged) == 1
