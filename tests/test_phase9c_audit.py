# -*- coding: utf-8 -*-
"""Phase 9C — Commercial & target readiness audit / safe authoring tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.order_contract_republish import PILOT_SERVICE_IDS
from catalog_core.phase9c_audit import (
    REVIEW_EXECUTION_SOURCE_MISSING,
    REVIEW_QUANTITY_SENTINEL,
    REVIEW_SERVICE_TYPE_UNKNOWN,
    REVIEW_TARGET_SPECIAL_RULE,
    SENTINEL_MAX,
    ServiceAuditRow,
    apply_safe_authoring,
    classify_service,
    proposed_cohorts,
)
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_shadow import compare_storefronts


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "phase9c.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
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
                platform_key TEXT,
                section_key TEXT,
                subsection_key TEXT,
                fulfillment_mode TEXT DEFAULT 'auto',
                is_active INTEGER DEFAULT 1,
                local_price_dh REAL,
                min_qty INTEGER,
                max_qty INTEGER,
                provider_slug TEXT,
                provider_api_account TEXT,
                external_service_id TEXT
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                service_id TEXT NOT NULL
            );
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _base_row(**overrides) -> ServiceAuditRow:
    data = dict(
        soldium_service_id="svc_test_1",
        legacy_catalog_id="9001",
        name_ar="اختبار",
        status="active",
        platform_key="tiktok",
        section_key="likes",
        subsection_key=None,
        structural_platform="tiktok",
        structural_section="likes",
        structural_subsection=None,
        service_type="other",
        ordering_mode="quantity_based",
        min_quantity=100,
        max_quantity=10000,
        amount_millimes=5000,
        currency="MAD",
        pricing_mode="per_1000",
        catalog_fulfillment_mode="auto",
        legacy_fulfillment_mode="auto",
        target_platform_key=None,
        target_section_key=None,
        target_subsection_key=None,
        has_execution_source=True,
        exec_provider_slug="gozibra",
        exec_account_key="tiktok",
        exec_external_id="12345",
        bridge_classification="migrated",
        bridge_review_codes=[],
    )
    data.update(overrides)
    return ServiceAuditRow(**data)


def test_classify_service_type_unknown_and_authorable_keys():
    row = classify_service(_base_row())
    assert REVIEW_SERVICE_TYPE_UNKNOWN in row.review_codes
    assert row.authorable_fulfillment == "auto"
    assert row.authorable_target == {
        "platform_key": "tiktok",
        "section_key": "likes",
        "subsection_key": None,
    }
    assert row.contract_ready is True
    assert "Contract-ready" in row.categories


def test_classify_quantity_sentinel_preserved_as_review():
    row = classify_service(_base_row(max_quantity=SENTINEL_MAX))
    assert REVIEW_QUANTITY_SENTINEL in row.review_codes
    assert "Quantity-review" in row.categories
    assert row.contract_ready is False
    assert row.max_quantity == SENTINEL_MAX


def test_classify_special_target_rule_telegram():
    row = classify_service(
        _base_row(
            platform_key="telegram",
            section_key="members",
            structural_platform="telegram",
            structural_section="members",
        )
    )
    assert REVIEW_TARGET_SPECIAL_RULE in row.review_codes
    assert row.target_policy_status == "special_rule_review"
    assert row.contract_ready is False
    assert row.authorable_target is not None


def test_classify_missing_execution_blocked():
    row = classify_service(_base_row(has_execution_source=False, exec_external_id=None))
    assert REVIEW_EXECUTION_SOURCE_MISSING in row.review_codes
    assert "Blocked" in row.categories
    assert row.contract_ready is False


def test_classify_admin_fulfillment_authorable():
    row = classify_service(
        _base_row(
            legacy_fulfillment_mode="admin",
            catalog_fulfillment_mode="auto",
            platform_key="subscriptions",
            section_key="iptv_panel",
            structural_platform="subscriptions",
            structural_section="iptv_panel",
        )
    )
    assert row.authorable_fulfillment == "admin"
    assert "fulfillment_admin" in row.review_codes
    assert REVIEW_TARGET_SPECIAL_RULE in row.review_codes


def test_proposed_cohorts_exclude_special_and_sentinel():
    rows = [
        classify_service(_base_row(soldium_service_id="a")),
        classify_service(
            _base_row(
                soldium_service_id="b",
                max_quantity=SENTINEL_MAX,
            )
        ),
        classify_service(
            _base_row(
                soldium_service_id="c",
                platform_key="telegram",
                section_key="members",
                structural_platform="telegram",
                structural_section="members",
            )
        ),
    ]
    cohorts = proposed_cohorts(rows)
    assert len(cohorts) == 1
    assert cohorts[0]["service_count"] == 1
    assert cohorts[0]["proposed"]["service_type"] == "KEEP_other_pending_review"


def test_safe_authoring_idempotent_preserves_price_exec_placement(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        core = CatalogCoreService(conn)
        node = core.create_node(name_ar="تيك توك")
        svc = core.create_service(
            name_ar="إعجابات",
            parent_entry_id=node.entry_id,
            service_type="other",
            ordering_mode="quantity_based",
            min_quantity=50,
            max_quantity=5000,
            status="active",
            fulfillment_mode="auto",
        )
        core.change_price(
            svc.id, amount_dh="2.5", pricing_mode="per_1000", currency="MAD"
        )
        core.change_execution_source(
            svc.id,
            provider_slug="gozibra",
            provider_account_key="tiktok",
            external_service_id="9999",
        )
        conn.execute(
            """
            INSERT INTO smm_services(
              catalog_id, name_ar, platform_key, section_key, subsection_key,
              fulfillment_mode, is_active, min_qty, max_qty
            ) VALUES ('L1', 'إعجابات', 'tiktok', 'likes', NULL, 'auto', 1, 50, 5000)
            """
        )
        conn.execute(
            """
            INSERT INTO soldium_catalog_legacy_bridge(
              legacy_catalog_id, legacy_local_item_id, legacy_service_id,
              soldium_service_id, legacy_fulfillment_mode,
              classification, review_codes, migration_batch_id
            ) VALUES ('L1', 'L1', 'L1', ?, 'auto', 'migrated', '[]', 'phase9c-test')
            """,
            (svc.id,),
        )

        row = classify_service(
            _base_row(
                soldium_service_id=svc.id,
                legacy_catalog_id="L1",
                catalog_fulfillment_mode="auto",
                legacy_fulfillment_mode="auto",
                structural_platform="tiktok",
                structural_section="likes",
                target_platform_key=None,
                amount_millimes=2500,
                max_quantity=5000,
                min_quantity=50,
            )
        )
        first = apply_safe_authoring(conn, [row], dry_run=False)
        assert first["updated"] == 1
        after = core.get_service(svc.id)
        assert after.target_platform_key == "tiktok"
        assert after.target_section_key == "likes"
        assert after.fulfillment_mode == "auto"
        assert after.service_type == "other"
        assert after.min_quantity == 50
        assert after.max_quantity == 5000
        price = core.repo.get_active_price(svc.id)
        assert price is not None and int(price.amount_millimes) == 2500
        src = core.repo.get_active_execution_source(svc.id)
        assert src is not None and str(src.external_service_id) == "9999"
        entry = core.repo.get_entry_for_service(svc.id)
        assert entry is not None and entry.parent_entry_id == node.entry_id

        row2 = classify_service(
            _base_row(
                soldium_service_id=svc.id,
                catalog_fulfillment_mode="auto",
                legacy_fulfillment_mode="auto",
                structural_platform="tiktok",
                structural_section="likes",
                target_platform_key="tiktok",
                target_section_key="likes",
                amount_millimes=2500,
                max_quantity=5000,
                min_quantity=50,
            )
        )
        second = apply_safe_authoring(conn, [row2], dry_run=False)
        assert second["updated"] == 0
        assert second["skipped"] == 1


def test_safe_authoring_skips_pilots(catalog_db: Path):
    pilot = next(iter(PILOT_SERVICE_IDS))
    row = classify_service(
        _base_row(
            soldium_service_id=pilot,
            target_platform_key=None,
            structural_platform="instagram",
            structural_section="views",
            platform_key="instagram",
            section_key="views",
        )
    )
    with catalog_transaction(catalog_db) as conn:
        result = apply_safe_authoring(conn, [row], dry_run=False)
        assert result["updated"] == 0
        assert result["skipped"] == 1


def test_no_keyword_service_type_inference():
    # Ambiguous name must NOT force service_type.
    row = classify_service(
        _base_row(name_ar="متابعون ولايكات ومشاهدات", service_type="other")
    )
    assert row.service_type == "other"
    assert REVIEW_SERVICE_TYPE_UNKNOWN in row.review_codes


def test_shadow_baseline_no_publish_required(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        # Empty catalog projection is fine; shadow must not publish.
        result = compare_storefronts(conn)
        d = result.to_dict()
        assert d.get("publication_count", 0) == 0 or d.get("catalog_count") == 0
        pubs = conn.execute(
            "SELECT COUNT(*) AS c FROM soldium_catalog_publications"
        ).fetchone()["c"]
        assert pubs == 0
