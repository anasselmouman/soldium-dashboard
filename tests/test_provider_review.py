# -*- coding: utf-8 -*-
"""Phase 6C — Provider Catalog Review Inbox tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.provider_discovery import ProviderCatalogItem
from catalog_core.provider_review import (
    build_provider_review_summary,
    build_review_items_from_diff,
    list_provider_review_items,
)
from catalog_core.provider_snapshot import (
    ProviderSnapshotRepository,
    compute_provider_catalog_diff,
)
from catalog_core.schema import SOLDIUM_CATALOG_SCHEMA_VERSION, ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "catalog_provider_review_test.db"
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
            VALUES
              ('gozibra', 'default', 'افتراضي'),
              ('gozibra', 'instagram', 'انستغرام'),
              ('other', 'main', 'رئيسي');
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
    rate: float | None = 1.0,
    max_q: int | None = 1000,
    slug: str = "gozibra",
    account: str = "default",
) -> ProviderCatalogItem:
    return ProviderCatalogItem(
        provider_slug=slug,
        provider_account_key=account,
        external_service_id=eid,
        provider_service_name=name,
        provider_category="Cat",
        provider_type="Default",
        min_quantity=10,
        max_quantity=max_q,
        provider_rate=rate,
    )


def _snap(
    repo: ProviderSnapshotRepository,
    items: list[ProviderCatalogItem],
    *,
    when: str,
    slug: str = "gozibra",
    account: str = "default",
):
    return repo.insert_successful_snapshot(
        provider_slug=slug,
        provider_account_key=account,
        discovered_at=when,
        items=items,
    )


def test_schema_unchanged_for_phase_6c():
    assert SOLDIUM_CATALOG_SCHEMA_VERSION == "10"


def test_summary_new_changed_missing(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        repo = ProviderSnapshotRepository(conn)
        _snap(
            repo,
            [_item("A", name="Alpha"), _item("B"), _item("C")],
            when="2026-01-01T00:00:00+00:00",
        )
        _snap(
            repo,
            [
                _item("A", name="Alpha2", rate=2.0),
                _item("C"),
                _item("D", name="Delta"),
            ],
            when="2026-01-02T00:00:00+00:00",
        )
        summary = build_provider_review_summary(conn)
        assert summary.new_count == 1
        assert summary.changed_count == 1
        assert summary.missing_count == 1
        assert summary.needs_review == 3
        assert summary.accounts_with_comparison == 1


def test_unchanged_excluded_from_inbox(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        repo = ProviderSnapshotRepository(conn)
        items = [_item("A"), _item("B")]
        _snap(repo, items, when="2026-01-01T00:00:00+00:00")
        _snap(repo, items, when="2026-01-02T00:00:00+00:00")
        listed, total, _ = list_provider_review_items(conn)
        assert total == 0
        assert listed == []
        summary = build_provider_review_summary(conn)
        assert summary.needs_review == 0


def test_first_baseline_no_review_queue(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        repo = ProviderSnapshotRepository(conn)
        _snap(repo, [_item("A"), _item("B")], when="2026-01-01T00:00:00+00:00")
        summary = build_provider_review_summary(conn)
        assert summary.needs_review == 0
        assert summary.accounts_baseline_only == 1
        assert summary.accounts_with_comparison == 0
        assert "مقارنة سابقة" in summary.accounts[0].message_ar
        items, total, _ = list_provider_review_items(conn)
        assert total == 0


def test_new_item_has_current_data(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        repo = ProviderSnapshotRepository(conn)
        _snap(repo, [_item("A")], when="2026-01-01T00:00:00+00:00")
        _snap(
            repo,
            [_item("A"), _item("NEW", name="Brand New")],
            when="2026-01-02T00:00:00+00:00",
        )
        items, _, _ = list_provider_review_items(conn, change_type="new")
        assert len(items) == 1
        assert items[0].external_service_id == "NEW"
        assert items[0].item is not None
        assert items[0].item.provider_service_name == "Brand New"
        assert items[0].previous_item is None


def test_changed_fields_previous_current(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        repo = ProviderSnapshotRepository(conn)
        _snap(
            repo,
            [_item("A", name="Old", rate=0.5, max_q=100000)],
            when="2026-01-01T00:00:00+00:00",
        )
        _snap(
            repo,
            [_item("A", name="New", rate=0.6, max_q=200000)],
            when="2026-01-02T00:00:00+00:00",
        )
        items, _, _ = list_provider_review_items(conn, change_type="changed")
        assert len(items) == 1
        fields = {f.field: f for f in items[0].field_changes}
        assert set(fields) >= {
            "provider_service_name",
            "provider_rate",
            "max_quantity",
        }
        assert fields["provider_rate"].previous_value == 0.5
        assert fields["provider_rate"].current_value == 0.6
        assert fields["max_quantity"].previous_display == "100000"
        assert fields["max_quantity"].current_display == "200000"


def test_missing_uses_previous_data(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        repo = ProviderSnapshotRepository(conn)
        _snap(repo, [_item("GONE", name="Was Here")], when="2026-01-01T00:00:00+00:00")
        _snap(repo, [], when="2026-01-02T00:00:00+00:00")
        items, _, _ = list_provider_review_items(conn, change_type="missing")
        assert len(items) == 1
        assert items[0].previous_item is not None
        assert items[0].previous_item.provider_service_name == "Was Here"
        assert items[0].item is None
        assert "لم تعد موجودة" in items[0].note_ar
        assert "حُذفت" not in items[0].note_ar


def test_failed_discovery_no_missing_review(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        repo = ProviderSnapshotRepository(conn)
        _snap(repo, [_item("A"), _item("B")], when="2026-01-01T00:00:00+00:00")
        _snap(repo, [_item("A"), _item("B")], when="2026-01-02T00:00:00+00:00")
        # Failed attempt after successful pair — must not create missing items
        repo.insert_failed_attempt(
            provider_slug="gozibra",
            provider_account_key="default",
            discovered_at="2026-01-03T00:00:00+00:00",
            error_code="api_failure",
            error_message_ar="فشل",
        )
        items, total, _ = list_provider_review_items(conn)
        assert total == 0
        assert not any(i.change_type == "missing" for i in items)
        latest = repo.get_latest_successful_snapshot("gozibra", "default")
        assert latest is not None
        assert latest.item_count == 2


def test_empty_successful_produces_missing(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        repo = ProviderSnapshotRepository(conn)
        _snap(repo, [_item("A"), _item("B")], when="2026-01-01T00:00:00+00:00")
        _snap(repo, [], when="2026-01-02T00:00:00+00:00")
        summary = build_provider_review_summary(conn)
        assert summary.missing_count == 2
        assert summary.new_count == 0


def test_filters_and_search(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        repo = ProviderSnapshotRepository(conn)
        _snap(
            repo,
            [_item("1", name="Alpha"), _item("2", name="Beta")],
            when="2026-01-01T00:00:00+00:00",
            slug="gozibra",
            account="default",
        )
        _snap(
            repo,
            [_item("1", name="Alpha"), _item("3", name="Gamma Search")],
            when="2026-01-02T00:00:00+00:00",
            slug="gozibra",
            account="default",
        )
        _snap(
            repo,
            [_item("X", name="Other", slug="other", account="main")],
            when="2026-01-01T00:00:00+00:00",
            slug="other",
            account="main",
        )
        _snap(
            repo,
            [
                _item("X", name="Other", slug="other", account="main"),
                _item("Y", name="Extra", slug="other", account="main"),
            ],
            when="2026-01-02T00:00:00+00:00",
            slug="other",
            account="main",
        )

        by_prov, n1, _ = list_provider_review_items(conn, provider_slug="other")
        assert n1 == 1
        assert all(i.provider_slug == "other" for i in by_prov)

        by_type, n2, _ = list_provider_review_items(
            conn, provider_slug="gozibra", change_type="new"
        )
        assert n2 == 1
        assert by_type[0].external_service_id == "3"

        by_id, n3, _ = list_provider_review_items(conn, search="3")
        assert n3 == 1
        assert by_id[0].external_service_id == "3"

        by_name, n4, _ = list_provider_review_items(conn, search="gamma")
        assert n4 == 1


def test_no_mapping_note(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        repo = ProviderSnapshotRepository(conn)
        _snap(repo, [], when="2026-01-01T00:00:00+00:00")
        _snap(repo, [_item("99", name="Solo")], when="2026-01-02T00:00:00+00:00")
        items, _, _ = list_provider_review_items(conn)
        assert items[0].soldium_link_note_ar == "غير مرتبط"


def test_no_mutations_and_no_snapshot_rewrite(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        repo = ProviderSnapshotRepository(conn)
        s1 = _snap(repo, [_item("A")], when="2026-01-01T00:00:00+00:00")
        s2 = _snap(repo, [_item("A"), _item("B")], when="2026-01-02T00:00:00+00:00")
        before_items = conn.execute(
            "SELECT COUNT(*) FROM soldium_provider_catalog_snapshot_items"
        ).fetchone()[0]
        before_svc = conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_services"
        ).fetchone()[0]
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="خدمة")
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="default",
            external_service_id="keep",
        )
        svc.change_price(created.id, amount_dh="1", pricing_mode="per_1000")

        list_provider_review_items(conn)
        build_provider_review_summary(conn)

        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soldium_provider_catalog_snapshot_items"
            ).fetchone()[0]
            == before_items
        )
        assert (
            conn.execute(
                "SELECT item_count FROM soldium_provider_catalog_snapshots WHERE id = ?",
                (s1.id,),
            ).fetchone()[0]
            == s1.item_count
        )
        assert (
            conn.execute(
                "SELECT item_count FROM soldium_provider_catalog_snapshots WHERE id = ?",
                (s2.id,),
            ).fetchone()[0]
            == s2.item_count
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM soldium_catalog_services").fetchone()[0]
            == before_svc + 1
        )
        assert conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT name_ar FROM catalog_services WHERE catalog_id='old'"
            ).fetchone()[0]
            == "قديم"
        )


def test_diff_reuse_excludes_unchanged():
    prev = [_item("A"), _item("B")]
    curr = [_item("A"), _item("C")]
    from catalog_core.provider_snapshot import ProviderCatalogSnapshot

    prev_snap = ProviderCatalogSnapshot(
        id="p",
        provider_slug="gozibra",
        provider_account_key="default",
        status="success",
        discovered_at="t1",
        item_count=2,
    )
    curr_snap = ProviderCatalogSnapshot(
        id="c",
        provider_slug="gozibra",
        provider_account_key="default",
        status="success",
        discovered_at="t2",
        item_count=2,
    )
    diff = compute_provider_catalog_diff(
        provider_slug="gozibra",
        provider_account_key="default",
        previous_snapshot=prev_snap,
        previous_items=prev,
        current_snapshot=curr_snap,
        current_items=curr,
    )
    review = build_review_items_from_diff(diff)
    types = {i.change_type for i in review}
    assert types == {"new", "missing"}
    assert all(i.change_type != "unchanged" for i in review)


def test_api_and_ui_smoke():
    from main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/soldium-catalog/provider-review" in paths
    assert "/api/soldium-catalog/provider-review/summary" in paths
    # Phase 5 readiness routes remain
    assert "/api/soldium-catalog/review" in paths
    assert "/api/soldium-catalog/review/summary" in paths

    root = Path(__file__).resolve().parent.parent
    html = (root / "templates" / "workspaces" / "catalog_review.html").read_text(
        encoding="utf-8"
    )
    js = (root / "static" / "js" / "catalog_core_ui.js").read_text(encoding="utf-8")
    assert "جاهزية الخدمات" in html
    assert "تغييرات كتالوج المزود" in html
    assert "provider-review" in js
    assert "prompt(" not in js
    assert "confirm(" not in js
    assert "alert(" not in js
