# -*- coding: utf-8 -*-
"""Phase 9J — Wave 2 candidate audit tests (isolated fixtures + no mutation)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.phase9j_wave2_candidate_audit import (
    TARGET_WAVE_SIZE,
    decide,
    draft_legacy_preview,
    select_wave2,
)
from catalog_core.schema import ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "phase9j.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE providers (
                slug TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
                api_base_url TEXT NOT NULL DEFAULT '', is_active INTEGER DEFAULT 1
            );
            INSERT INTO providers(slug, name) VALUES ('gozibra', 'Gozibra');
            CREATE TABLE provider_accounts (
                id INTEGER PRIMARY KEY,
                provider_slug TEXT NOT NULL,
                account_key TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                UNIQUE(provider_slug, account_key)
            );
            INSERT INTO provider_accounts(provider_slug, account_key)
            VALUES ('gozibra', 'a1');
            CREATE TABLE smm_services (
                catalog_id TEXT PRIMARY KEY,
                name_ar TEXT,
                platform_key TEXT,
                section_key TEXT,
                subsection_key TEXT,
                is_active INTEGER DEFAULT 1,
                local_price_dh REAL,
                min_qty INTEGER,
                max_qty INTEGER,
                provider_slug TEXT,
                provider_api_account TEXT,
                external_service_id TEXT,
                fulfillment_mode TEXT
            );
            CREATE TABLE orders (id INTEGER PRIMARY KEY, service_id TEXT);
            CREATE TABLE scheduled_orders (
                id INTEGER PRIMARY KEY,
                external_service_id TEXT
            );
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _make_ready(conn, *, legacy_id: str, external: str, platform: str, section: str, stype: str):
    core = CatalogCoreService(conn)
    svc = core.create_service(
        name_ar=f"خدمة {legacy_id}",
        status="active",
        service_type=stype,
        ordering_mode="quantity_based",
        min_quantity=10,
        max_quantity=1000,
        fulfillment_mode="auto",
        target_platform_key=platform,
        target_section_key=section,
    )
    core.change_execution_source(
        svc.id,
        provider_slug="gozibra",
        provider_account_key="a1",
        external_service_id=external,
    )
    core.change_price(svc.id, amount_dh="2", pricing_mode="per_1000")
    conn.execute(
        """
        INSERT INTO smm_services(
            catalog_id, name_ar, platform_key, section_key, local_price_dh,
            min_qty, max_qty, provider_slug, provider_api_account,
            external_service_id, fulfillment_mode
        ) VALUES (?, ?, ?, ?, 2.0, 10, 1000, 'gozibra', 'a1', ?, 'auto')
        """,
        (legacy_id, f"خدمة {legacy_id}", platform, section, external),
    )
    conn.execute(
        """
        INSERT INTO soldium_catalog_legacy_bridge(
            legacy_catalog_id, legacy_local_item_id, legacy_service_id,
            soldium_service_id, provider_slug, external_service_id,
            provider_api_account, legacy_fulfillment_mode, classification,
            review_codes, migration_batch_id
        ) VALUES (?, ?, ?, ?, 'gozibra', ?, 'a1', 'auto', 'STANDARD', '[]', 'phase9j_test')
        """,
        (legacy_id, legacy_id, legacy_id, svc.id, external),
    )
    return {
        "legacy_catalog_id": legacy_id,
        "soldium_service_id": svc.id,
        "provider_service_id": external,
        "provider_slug": "gozibra",
        "provider_account_key": "a1",
        "platform": platform,
        "section": section,
        "service_type": stype,
        "fulfillment_mode": "auto",
        "price_amount_millimes": 2000,
        "risk_level": "LOW",
    }


def test_select_wave2_deterministic_and_diverse():
    pool = [
        {
            "soldium_service_id": f"svc_{i}",
            "legacy_catalog_id": f"L{i:03d}",
            "platform": ["instagram", "tiktok", "youtube", "twitter"][i % 4],
            "section": f"sec_{i % 6}",
            "service_type": ["likes", "views", "followers", "members"][i % 4],
        }
        for i in range(40)
    ]
    a = select_wave2(pool)
    b = select_wave2(pool)
    assert [c["soldium_service_id"] for c in a] == [
        c["soldium_service_id"] for c in b
    ]
    assert 10 <= len(a) <= TARGET_WAVE_SIZE
    platforms = [c["platform"] for c in a]
    assert len(set(platforms)) >= 3


def test_draft_preview_and_decide_no_dangerous(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        c = _make_ready(
            conn,
            legacy_id="LEG1",
            external="111",
            platform="instagram",
            section="likes",
            stype="likes",
        )
        preview = draft_legacy_preview(conn, c)
        assert preview["correlated"] is True
        assert preview["dangerous"] == 0
        assert preview["provider_service_id"] == "111"
        assert preview["معرّف_المزود"] == "111"
        wave = [c] * 10
        previews = [preview] * 10
        verdict = decide(wave, previews, True)
        assert verdict in {
            "WAVE_2_CANDIDATES_READY",
            "WAVE_2_CANDIDATES_READY_WITH_REVIEW",
        }


def test_audit_functions_do_not_mutate_production_tables(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        c = _make_ready(
            conn,
            legacy_id="LEG2",
            external="222",
            platform="tiktok",
            section="views",
            stype="views",
        )
        before = {
            "services": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_services"
            ).fetchone()[0],
            "prices": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_prices"
            ).fetchone()[0],
            "sources": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_execution_sources"
            ).fetchone()[0],
            "pubs": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_publications"
            ).fetchone()[0],
            "smm": conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0],
            "orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            "sched": conn.execute(
                "SELECT COUNT(*) FROM scheduled_orders"
            ).fetchone()[0],
        }
        draft_legacy_preview(conn, c)
        select_wave2([c])
        decide([c], [draft_legacy_preview(conn, c)], True)
        after = {
            "services": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_services"
            ).fetchone()[0],
            "prices": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_prices"
            ).fetchone()[0],
            "sources": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_execution_sources"
            ).fetchone()[0],
            "pubs": conn.execute(
                "SELECT COUNT(*) FROM soldium_catalog_publications"
            ).fetchone()[0],
            "smm": conn.execute("SELECT COUNT(*) FROM smm_services").fetchone()[0],
            "orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            "sched": conn.execute(
                "SELECT COUNT(*) FROM scheduled_orders"
            ).fetchone()[0],
        }
        assert before == after
        assert after["pubs"] == 0


def test_insufficient_wave_decision():
    assert decide([], [], True) == "WAVE_2_CANDIDATES_INSUFFICIENT"
    assert decide([{"x": 1}] * 5, [{"dangerous": 0}] * 5, True) == (
        "WAVE_2_CANDIDATES_INSUFFICIENT"
    )
    assert decide([{"x": 1}] * 10, [{"dangerous": 1}] * 10, True) == (
        "WAVE_2_CANDIDATES_BLOCKED"
    )
    assert decide([], [], False) == "WAVE_2_CANDIDATES_BLOCKED"


def test_provider_id_first_in_artifact_if_present():
    path = Path("scripts/out_phase9j_wave2_candidate_audit.json")
    if not path.exists():
        pytest.skip("9J artifact missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["verdict"] in {
        "WAVE_2_CANDIDATES_READY",
        "WAVE_2_CANDIDATES_READY_WITH_REVIEW",
        "WAVE_2_CANDIDATES_INSUFFICIENT",
        "WAVE_2_CANDIDATES_BLOCKED",
    }
    assert data["mutations"].startswith("NONE")
    assert data["production_unchanged"] is True
    assert data["production_baseline"]["published_services"] == 28
    assert data["production_baseline"]["publications"] == 38
    assert data["production_baseline"]["services"] == 253
    wave = data["RECOMMENDED_WAVE_2"]
    if wave:
        first_keys = list(wave[0].keys())
        assert first_keys[0] == "معرّف_المزود"
        for row in wave:
            assert row["معرّف_المزود"] is not None
            assert isinstance(row["معرّف_المزود"], str)
            assert row["provider_service_id"] == row["معرّف_المزود"]
            assert row["risk_level"] == "LOW"
            assert row["readiness_ready"] is True
    shadow = data["candidate_shadow_preview"]
    assert shadow["dangerous_total"] == 0
    assert shadow["candidate_count"] == shadow["correlated_candidates"]
    assert data["partition"]["current_counts"]["ready"] == data["ready_pool"]["count"]
