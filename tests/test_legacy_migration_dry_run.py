# -*- coding: utf-8 -*-
"""Phase 8C — read-only legacy migration Dry Run tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from catalog_core.legacy_migration import (
    LEGACY_MIGRATION_UUID_NAMESPACE,
    ReadOnlyConnection,
    dry_run_report_json,
    is_max_qty_sentinel,
    legacy_node_key_platform,
    legacy_node_key_section,
    legacy_node_key_subsection,
    plan_legacy_migration,
    plan_legacy_migration_at_path,
    planned_soldium_service_id,
)
from catalog_core.pricing import dh_to_millimes
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService


def _create_base(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE providers (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO providers(slug, name) VALUES ('gozibra', 'Gozibra');
        CREATE TABLE provider_accounts (
            id INTEGER PRIMARY KEY,
            provider_slug TEXT NOT NULL,
            account_key TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            UNIQUE(provider_slug, account_key)
        );
        INSERT INTO provider_accounts(provider_slug, account_key, display_name)
        VALUES
            ('gozibra', 'tiktok', 'TikTok'),
            ('gozibra', 'facebook', 'Facebook');
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            service_id TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO orders(id, service_id) VALUES (1, 'keep-me');
        CREATE TABLE smm_services (
            catalog_id TEXT PRIMARY KEY,
            external_service_id TEXT NOT NULL,
            provider_slug TEXT NOT NULL DEFAULT 'gozibra',
            category TEXT NOT NULL DEFAULT '',
            name_ar TEXT NOT NULL DEFAULT '',
            provider_price_usd REAL NOT NULL DEFAULT 0,
            local_price_dh REAL NOT NULL DEFAULT 0,
            min_qty INTEGER NOT NULL DEFAULT 1,
            max_qty INTEGER NOT NULL DEFAULT 1000000,
            is_active INTEGER NOT NULL DEFAULT 1,
            platform_key TEXT NOT NULL DEFAULT '',
            section_key TEXT,
            subsection_key TEXT,
            local_item_id TEXT NOT NULL DEFAULT '',
            platform_title TEXT NOT NULL DEFAULT '',
            section_title TEXT,
            subsection_title TEXT,
            fulfillment_mode TEXT NOT NULL DEFAULT 'auto',
            provider_api_account TEXT,
            provider_price_updated_at TEXT,
            service_id TEXT NOT NULL DEFAULT ''
        );
        """
    )
    ensure_soldium_catalog_schema(conn)


def _insert_service(
    conn: sqlite3.Connection,
    *,
    catalog_id: str,
    name_ar: str = "خدمة",
    local_price_dh: float = 1.5,
    min_qty: int = 100,
    max_qty: int = 10000,
    is_active: int = 1,
    platform_key: str = "tiktok",
    platform_title: str = "تيك توك",
    section_key: str | None = "likes",
    section_title: str | None = "لايكات",
    subsection_key: str | None = None,
    subsection_title: str | None = None,
    category: str = "path › ignored",
    provider_slug: str = "gozibra",
    provider_api_account: str | None = "tiktok",
    external_service_id: str = "1001",
    fulfillment_mode: str = "auto",
    local_item_id: str | None = None,
    service_id: str | None = None,
) -> None:
    lid = local_item_id if local_item_id is not None else catalog_id
    sid = service_id if service_id is not None else external_service_id
    conn.execute(
        """
        INSERT INTO smm_services (
            catalog_id, external_service_id, provider_slug, category, name_ar,
            provider_price_usd, local_price_dh, min_qty, max_qty, is_active,
            platform_key, section_key, subsection_key, local_item_id,
            platform_title, section_title, subsection_title,
            fulfillment_mode, provider_api_account, service_id
        ) VALUES (?, ?, ?, ?, ?, 0.5, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            catalog_id,
            external_service_id,
            provider_slug,
            category,
            name_ar,
            local_price_dh,
            min_qty,
            max_qty,
            is_active,
            platform_key,
            section_key,
            subsection_key,
            lid,
            platform_title,
            section_title,
            subsection_title,
            fulfillment_mode,
            provider_api_account,
            sid,
        ),
    )


@pytest.fixture
def dry_db(tmp_path: Path) -> Path:
    path = tmp_path / "legacy_dry_run.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _create_base(conn)
        _insert_service(conn, catalog_id="1001", name_ar="آمن", external_service_id="1001")
        _insert_service(
            conn,
            catalog_id="1002",
            name_ar="نفس الاسم",
            external_service_id="1002",
            local_price_dh=2.0,
        )
        _insert_service(
            conn,
            catalog_id="1003",
            name_ar="نفس الاسم",
            external_service_id="1003",
            local_price_dh=3.0,
            section_key="views",
            section_title="مشاهدات",
        )
        _insert_service(
            conn,
            catalog_id="2001",
            name_ar="بدون حساب",
            provider_api_account=None,
            external_service_id="2001",
        )
        _insert_service(
            conn,
            catalog_id="2002",
            name_ar="حساب خاطئ",
            provider_api_account="nope",
            external_service_id="2002",
        )
        _insert_service(
            conn,
            catalog_id="3001",
            name_ar="وحدة",
            category="per_unit",
            local_price_dh=42.0,
            min_qty=1,
            max_qty=1,
            fulfillment_mode="admin",
            platform_key="subscriptions",
            platform_title="اشتراكات",
            section_key="iptv",
            section_title="IPTV",
            external_service_id="3001",
            provider_api_account="tiktok",
        )
        _insert_service(
            conn,
            catalog_id="4001",
            name_ar="سنتينل",
            max_qty=2147483647,
            external_service_id="4001",
        )
        _insert_service(
            conn,
            catalog_id="5001",
            name_ar="كمية باطلة",
            min_qty=10,
            max_qty=5,
            external_service_id="5001",
        )
        _insert_service(
            conn,
            catalog_id="5002",
            name_ar="سعر باطل",
            local_price_dh=0,
            external_service_id="5002",
        )
        _insert_service(
            conn,
            catalog_id="5003",
            name_ar="دقة سعر",
            local_price_dh=24.8791,
            external_service_id="5003",
        )
        _insert_service(
            conn,
            catalog_id="9001",
            name_ar="مخزون خامد",
            is_active=0,
            platform_key="",
            platform_title="",
            section_key=None,
            external_service_id="9001",
        )
        _insert_service(
            conn,
            catalog_id="6001",
            name_ar="منصة فقط",
            section_key=None,
            section_title=None,
            subsection_key=None,
            external_service_id="6001",
        )
        _insert_service(
            conn,
            catalog_id="6002",
            name_ar="مع فرعي",
            section_key="a",
            section_title="أ",
            subsection_key="b",
            subsection_title="ب",
            external_service_id="6002",
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _plan(path: Path):
    return plan_legacy_migration_at_path(path)


def _by_id(report, catalog_id: str):
    return next(s for s in report.services if s.legacy_catalog_id == catalog_id)


def test_candidate_selection_excludes_inactive(dry_db: Path):
    report = _plan(dry_db)
    ids = {s.legacy_catalog_id for s in report.services}
    assert "9001" not in ids
    assert report.inactive_inventory_count >= 1
    assert report.candidate_count == len(report.services)
    assert all(s.platform_key for s in report.services)


def test_uuid5_deterministic_and_distinct(dry_db: Path):
    a1 = planned_soldium_service_id("1001")
    a2 = planned_soldium_service_id("1001")
    b = planned_soldium_service_id("1002")
    assert a1 == a2
    assert a1.startswith("svc_")
    assert a1 != b
    assert str(LEGACY_MIGRATION_UUID_NAMESPACE)
    r1 = _plan(dry_db)
    r2 = _plan(dry_db)
    assert _by_id(r1, "1001").planned_soldium_service_id == a1
    assert _by_id(r2, "1001").planned_soldium_service_id == a1


def test_duplicate_names_do_not_merge(dry_db: Path):
    report = _plan(dry_db)
    twins = [s for s in report.services if s.name_ar == "نفس الاسم"]
    assert len(twins) == 2
    assert twins[0].planned_soldium_service_id != twins[1].planned_soldium_service_id


def test_missing_and_invalid_account_review_no_source(dry_db: Path):
    report = _plan(dry_db)
    missing = _by_id(report, "2001")
    assert missing.classification == "REQUIRES_REVIEW"
    assert "missing_provider_account" in missing.review_codes
    assert missing.planned_execution_source == "omitted"
    assert "create_execution_source" not in missing.planned_actions
    assert "create_service" in missing.planned_actions

    invalid = _by_id(report, "2002")
    assert invalid.classification == "REQUIRES_REVIEW"
    assert "invalid_provider_account" in invalid.review_codes
    assert invalid.planned_execution_source == "omitted"


def test_valid_account_plans_execution_source(dry_db: Path):
    report = _plan(dry_db)
    safe = _by_id(report, "1001")
    assert safe.classification == "SAFE"
    assert safe.planned_execution_source == "present"
    assert "create_execution_source" in safe.planned_actions


def test_per_unit_and_admin_review(dry_db: Path):
    report = _plan(dry_db)
    row = _by_id(report, "3001")
    assert row.classification == "REQUIRES_REVIEW"
    assert "per_unit" in row.review_codes
    assert "fulfillment_admin" in row.review_codes
    assert row.pricing_mode == "per_unit"
    assert "fixed_package" not in (row.pricing_mode or "")


def test_sentinel_max_preserved(dry_db: Path):
    assert is_max_qty_sentinel(2147483647)
    report = _plan(dry_db)
    row = _by_id(report, "4001")
    assert row.max_quantity == 2147483647
    assert "max_qty_sentinel" in row.review_codes
    assert row.classification == "REQUIRES_REVIEW"


def test_invalid_qty_and_price_blocked(dry_db: Path):
    report = _plan(dry_db)
    bad_qty = _by_id(report, "5001")
    assert bad_qty.classification == "BLOCKED"
    assert "invalid_quantity_range" in bad_qty.blocking_codes
    assert bad_qty.planned_actions == ["no_catalog_write", "report_only"]

    bad_price = _by_id(report, "5002")
    assert bad_price.classification == "BLOCKED"
    assert "invalid_price" in bad_price.blocking_codes


def test_price_normalization_not_blocked(dry_db: Path):
    """>3 decimal places normalize via ROUND_HALF_UP — not blocked for precision."""
    from catalog_core.legacy_migration import normalize_legacy_price_dh

    report = _plan(dry_db)
    row = _by_id(report, "5003")
    assert "price_precision" not in row.blocking_codes
    assert row.classification != "BLOCKED" or "invalid_price" in row.blocking_codes
    # 24.8791 → 24.879 → importable SAFE (has valid account)
    assert row.classification == "SAFE"
    assert row.rounding_applied is True
    assert row.price_normalized is True
    assert row.legacy_price_dh == "24.8791" or row.local_price_dh_source.startswith("24.8791")
    assert row.normalized_price_dh == "24.879"
    assert row.amount_millimes == 24879
    # Legacy DB value untouched — Dry Run only
    _, text, applied = normalize_legacy_price_dh("24.8791")
    assert text == "24.879" and applied is True


def test_price_uses_catalog_semantics(dry_db: Path):
    report = _plan(dry_db)
    row = _by_id(report, "1001")
    assert row.amount_millimes == dh_to_millimes(row.normalized_price_dh or row.local_price_dh_source)
    assert row.pricing_mode == "per_1000"


def test_structure_keys_ignore_category(dry_db: Path):
    report = _plan(dry_db)
    plat_only = _by_id(report, "6001")
    assert plat_only.planned_parent_node_key == legacy_node_key_platform("tiktok")
    assert plat_only.structure_path_keys == [legacy_node_key_platform("tiktok")]

    deep = _by_id(report, "6002")
    assert deep.planned_parent_node_key == legacy_node_key_subsection("tiktok", "a", "b")
    assert legacy_node_key_section("tiktok", "a") in deep.structure_path_keys
    # category must not appear in structure keys
    assert all("›" not in k and "path" not in k for k in deep.structure_path_keys)

    node_keys = {n.legacy_node_key for n in report.nodes}
    assert legacy_node_key_platform("tiktok") in node_keys
    assert legacy_node_key_subsection("tiktok", "a", "b") in node_keys


def test_ordering_deterministic(dry_db: Path):
    r1 = _plan(dry_db)
    r2 = _plan(dry_db)
    assert [n.legacy_node_key for n in r1.nodes] == [n.legacy_node_key for n in r2.nodes]
    assert [n.sort_order for n in r1.nodes] == [n.sort_order for n in r2.nodes]
    s1 = [(s.legacy_catalog_id, s.planned_sort_order) for s in r1.services]
    s2 = [(s.legacy_catalog_id, s.planned_sort_order) for s in r2.services]
    assert s1 == s2


def test_dry_run_twice_equivalent_except_generated_at(dry_db: Path):
    d1 = dry_run_report_json(dry_db)
    d2 = dry_run_report_json(dry_db)
    d1.pop("generated_at")
    d2.pop("generated_at")
    assert d1 == d2


def test_zero_writes_and_counts_unchanged(dry_db: Path):
    conn = sqlite3.connect(dry_db)
    conn.row_factory = sqlite3.Row
    try:
        before = {
            "svc": conn.execute("SELECT COUNT(*) FROM soldium_catalog_services").fetchone()[0],
            "node": conn.execute("SELECT COUNT(*) FROM soldium_catalog_nodes").fetchone()[0],
            "ent": conn.execute("SELECT COUNT(*) FROM soldium_catalog_entries").fetchone()[0],
            "price": conn.execute("SELECT COUNT(*) FROM soldium_catalog_prices").fetchone()[0],
            "src": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_execution_sources"
            ).fetchone()[0],
            "pub": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_publications"
            ).fetchone()[0],
            "map": conn.execute(
                "SELECT COUNT(*) FROM soldium_provider_service_mappings"
            ).fetchone()[0],
            "smm": conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0],
            "ord": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        }
    finally:
        conn.close()

    report = _plan(dry_db)
    assert report.publication_count == 0
    assert report.provider_mapping_count == 0
    assert report.legacy_write_count == 0
    assert report.safety["fixed_package_inferred"] == 0
    assert report.bridge_state in ("not_created", "empty")

    conn = sqlite3.connect(dry_db)
    try:
        after = {
            "svc": conn.execute("SELECT COUNT(*) FROM soldium_catalog_services").fetchone()[0],
            "node": conn.execute("SELECT COUNT(*) FROM soldium_catalog_nodes").fetchone()[0],
            "ent": conn.execute("SELECT COUNT(*) FROM soldium_catalog_entries").fetchone()[0],
            "price": conn.execute("SELECT COUNT(*) FROM soldium_catalog_prices").fetchone()[0],
            "src": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_execution_sources"
            ).fetchone()[0],
            "pub": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_publications"
            ).fetchone()[0],
            "map": conn.execute(
                "SELECT COUNT(*) FROM soldium_provider_service_mappings"
            ).fetchone()[0],
            "smm": conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0],
            "ord": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        }
    finally:
        conn.close()
    assert before == after


def test_readonly_wrapper_rejects_writes(dry_db: Path):
    conn = sqlite3.connect(f"file:{dry_db.resolve().as_uri().removeprefix('file:')}?mode=ro", uri=True)
    # simpler: use plan path's wrapper via open normal then wrap
    conn.close()
    conn = sqlite3.connect(dry_db)
    ro = ReadOnlyConnection(conn)
    with pytest.raises(sqlite3.OperationalError, match="forbids write"):
        ro.execute("INSERT INTO orders(service_id) VALUES ('x')")
    conn.close()


def test_existing_catalog_detection_no_mutate(dry_db: Path):
    # Seed a Catalog service matching planned id for 1001
    planned = planned_soldium_service_id("1001")
    conn = sqlite3.connect(dry_db)
    conn.row_factory = sqlite3.Row
    try:
        ensure_soldium_catalog_schema(conn)
        svc = CatalogCoreService(conn)
        # create with random id then we need the planned id — insert directly
        conn.execute(
            """
            INSERT INTO soldium_catalog_services(id, name_ar, status)
            VALUES (?, 'موجود', 'active')
            """,
            (planned,),
        )
        conn.execute(
            """
            INSERT INTO soldium_catalog_entries(
                id, parent_entry_id, entry_type, node_id, service_id, sort_order
            ) VALUES ('ent_test', NULL, 'service', NULL, ?, 0)
            """,
            (planned,),
        )
        conn.commit()
        before = conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_services"
        ).fetchone()[0]
    finally:
        conn.close()

    report = _plan(dry_db)
    row = _by_id(report, "1001")
    assert row.existing_catalog["service_exists"] is True
    assert "bridge" in row.existing_catalog["state"]

    conn = sqlite3.connect(dry_db)
    try:
        after = conn.execute(
            "SELECT COUNT(*) FROM soldium_catalog_services"
        ).fetchone()[0]
    finally:
        conn.close()
    assert before == after


def test_provider_not_found_blocked(tmp_path: Path):
    path = tmp_path / "no_provider.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _create_base(conn)
        _insert_service(
            conn,
            catalog_id="7701",
            provider_slug="missingpanel",
            external_service_id="7701",
        )
        conn.commit()
    finally:
        conn.close()
    row = _by_id(_plan(path), "7701")
    assert row.classification == "BLOCKED"
    assert "provider_not_found" in row.blocking_codes


def test_counts_and_groups_present(dry_db: Path):
    report = _plan(dry_db)
    assert report.planned_service_count == report.safe_count + report.review_count
    assert report.planned_bridge_count == report.planned_service_count
    assert report.planned_price_count == report.planned_service_count
    assert report.planned_total_entry_count == (
        report.planned_node_entry_count + report.planned_service_entry_count
    )
    assert "by_review_code" in report.groups
    assert "by_review_code_combination" in report.groups
    assert report.safety["category_used_as_structure"] is False
    assert report.safety["provider_api_calls"] == 0
    assert report.price_normalization["rounding_mode"] == "ROUND_HALF_UP"


def test_normalize_legacy_price_rounding_table():
    from catalog_core.legacy_migration import normalize_legacy_price_dh
    from catalog_core.pricing import dh_to_millimes

    cases = [
        ("24.8791", "24.879", True, 24879),
        ("24.8794", "24.879", True, 24879),
        ("24.8795", "24.880", True, 24880),
        ("24.8799", "24.880", True, 24880),
        ("0.0001", "0.000", True, None),  # zero after norm → invalid for Catalog sell
        ("0.0005", "0.001", True, 1),
        ("0.0009", "0.001", True, 1),
        ("0.2468", "0.247", True, 247),
        ("1.500", "1.500", False, 1500),
    ]
    for raw, expected_text, expected_applied, expected_m in cases:
        _dec, text, applied = normalize_legacy_price_dh(raw)
        assert text == expected_text, raw
        assert applied is expected_applied, raw
        if expected_m is None:
            with pytest.raises(Exception):
                dh_to_millimes(text)
        else:
            assert dh_to_millimes(text) == expected_m


def test_every_candidate_has_parent(dry_db: Path):
    report = _plan(dry_db)
    for s in report.services:
        assert s.deepest_parent_type in {"platform", "section", "subsection"}
    assert report.placement["missing_parent_count"] == 0


def test_entry_counts_separated(dry_db: Path):
    report = _plan(dry_db)
    assert report.planned_node_entry_count == report.planned_node_count
    assert report.planned_service_entry_count == report.planned_service_count
    assert report.planned_total_entry_count == (
        report.planned_node_count + report.planned_service_count
    )
