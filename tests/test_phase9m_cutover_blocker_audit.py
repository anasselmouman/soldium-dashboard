# -*- coding: utf-8 -*-
"""Phase 9M — cutover blocker audit tests (no production writes)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from catalog_core.phase9i_expansion_validation import audit_4371
from catalog_core.phase9m_cutover_blocker_audit import (
    EXPECTED,
    REMEDIATION_PHASES,
    audit_b1_4371,
    audit_b2_gen0_identity,
    audit_b3_pricing,
    audit_b4_telegram,
    audit_b5_parity,
    audit_b6_rollback,
    build_blocker_register,
    capture_baseline,
    run_phase9m,
)
from catalog_core.schema import ensure_soldium_catalog_schema


def test_expected_baseline_constants():
    assert EXPECTED == {
        "services": 253,
        "published_services": 43,
        "publications": 53,
        "nodes": 59,
        "entries": 312,
        "prices": 253,
        "execution_sources": 248,
        "mappings": 0,
        "orders": 44,
        "scheduled_orders": 0,
        "smm_services": 2069,
    }


def test_audit_4371_runtime_hardcode_cleared_after_9n():
    from_i = audit_4371()
    assert from_i["present"] is False
    assert from_i["status"] == "CLEARED"
    b1 = audit_b1_4371()
    assert b1["evidence"]["target_validation_hardcode"] is False
    assert b1["evidence"]["audit_4371"]["present"] is False
    assert b1["remediation_status"] == "CLEARED"


def test_blocker_register_builder_unit():
    baseline = {"published_services": 43, "services": 253, "scheduled_orders": 0}
    register = build_blocker_register(
        baseline=baseline,
        b1=audit_b1_4371(),
        b2=audit_b2_gen0_identity(),
        b3=audit_b3_pricing(),
        b4=audit_b4_telegram(),
        b5=audit_b5_parity(baseline),
        b6=audit_b6_rollback(),
    )
    ids = [r["id"] for r in register]
    assert ids == ["B1", "B2", "B3", "B4", "B5", "B6"]
    by_id = {r["id"]: r for r in register}
    assert by_id["B1"]["remediation_status"] == "CLEARED"
    for bid in ("B2", "B3", "B4", "B5", "B6"):
        assert by_id[bid]["remediation_status"] == "OPEN"
        assert by_id[bid]["severity"]
        assert by_id[bid]["evidence"]
        assert by_id[bid]["risk"]
    assert any(p["phase"] == "9N" for p in REMEDIATION_PHASES)
    assert any(p["phase"] == "9U" for p in REMEDIATION_PHASES)
    assert all(p["execute"] is False for p in REMEDIATION_PHASES)


def test_tmp_db_baseline_fails_or_skips(tmp_path: Path):
    path = tmp_path / "phase9m.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE providers (
                slug TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE provider_accounts (
                id INTEGER PRIMARY KEY,
                provider_slug TEXT NOT NULL,
                account_key TEXT NOT NULL
            );
            CREATE TABLE smm_services (catalog_id TEXT PRIMARY KEY);
            CREATE TABLE orders (id INTEGER PRIMARY KEY);
            CREATE TABLE scheduled_orders (id INTEGER PRIMARY KEY);
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
        before = capture_baseline(conn)
        assert before["ok"] is False
        report = run_phase9m(conn)
        assert report["verdict"] == "PHASE_9M_BLOCKED — BASELINE_MISMATCH"
        assert report["mutations"] == "NONE"
    finally:
        conn.close()


def test_artifact_if_present():
    path = Path("scripts/out_phase9m_cutover_blocker_audit.json")
    if not path.exists():
        pytest.skip("9M artifact missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["verdict"] in {
        "CUTOVER_REMEDIATION_PLAN_READY",
        "CUTOVER_REMEDIATION_PLAN_REQUIRES_REVIEW",
    }
    assert data["mutations"] == "NONE"
    assert data["production_unchanged"] is True
    assert data["production_baseline"]["scheduled_orders"] == 0
    assert data["production_baseline"]["published_services"] == 43
    ids = {r["id"] for r in data["blocker_register"]}
    assert ids == {"B1", "B2", "B3", "B4", "B5", "B6"}
    blob = json.dumps(data, ensure_ascii=False).lower()
    for claim in (
        "wrote to production",
        "update soldium",
        "delete from soldium",
        "insert into soldium",
        "mutations applied",
    ):
        assert claim not in blob
    assert data["next_step"] == "ARCHITECTURAL_REVIEW_REQUIRED"
    assert data["decision"]["do_not"] == (
        "START_REMEDIATION without architectural review"
    )
