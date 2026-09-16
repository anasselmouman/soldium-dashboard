# -*- coding: utf-8 -*-
"""Phase 9F — isolated audit tests (no production mutation)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalog_core.phase9f_architecture_review import cutover_matrix, expansion_options


def test_expansion_options_are_non_executing():
    opts = expansion_options(80, ["decide PROBABLE"])
    assert {o["option"] for o in opts} == {"A", "B", "C", "D"}
    assert all("prerequisites" in o for o in opts)


def test_cutover_matrix_statuses():
    rows = cutover_matrix({"dangerous": 0})
    assert any(r["status"] == "BLOCKED" for r in rows)
    assert any("4371" in r["item"] for r in rows)


def test_artifact_if_present():
    path = Path("scripts/out_phase9f_architecture_review.json")
    if not path.exists():
        pytest.skip("9F artifact missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("mutations", "").startswith("NONE")
    assert data.get("production_unchanged") is True
    assert data["immutability_summary"]["published_count"] == 13
    assert data["immutability_summary"]["immutability_ok"] == 13
    assert data["shadow"]["dangerous_count"] == 0
    assert data["shadow"]["catalog_count"] == 13
    assert data["remaining_unpublished"]["remaining_count"] == 240
    assert data["recommendation"] in {
        "EXPANSION RECOMMENDED",
        "BUSINESS DECISIONS REQUIRED BEFORE EXPANSION",
        "CUTOVER PREPARATION RECOMMENDED",
        "ARCHITECTURAL BLOCKER FOUND",
    }
    assert data["inventory"]["publications_rows"] == 23
    assert data["inventory"]["orders"] == 44
