# -*- coding: utf-8 -*-
"""Phase 9H Wave 1 — controlled expansion tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalog_core.phase9gb_business_decisions_apply import FUTURE_PILOT_LEGACY_IDS
from catalog_core.phase9h_wave1 import (
    PUBLISHED_BY,
    WAVE1_CANDIDATE_LEGACY_IDS,
    _classify_row,
)


class _R:
    def __init__(self, **kw):
        self.has_execution_source = kw.get("has_execution_source", True)
        self.pricing_mode = kw.get("pricing_mode", "per_1000")
        self.platform_key = kw.get("platform_key", "instagram")
        self.section_key = kw.get("section_key", "followers")
        self.max_quantity = kw.get("max_quantity", 1000)
        self.bridge_review_codes = kw.get("bridge_review_codes", [])
        self.service_type_confidence = kw.get("service_type_confidence", "CONFIRMED")
        self.service_type = kw.get("service_type", "followers")
        self.target_special = kw.get("target_special", False)
        self.ordering_confidence = kw.get("ordering_confidence", "CONFIRMED")


def test_wave1_pool_matches_9gb_future_pilots():
    assert WAVE1_CANDIDATE_LEGACY_IDS == FUTURE_PILOT_LEGACY_IDS
    assert len(WAVE1_CANDIDATE_LEGACY_IDS) == 15
    assert PUBLISHED_BY == "phase9h_wave1"


def test_classify_standard_vs_blocked():
    from catalog_core.phase9d_audit import (
        CONFIDENCE_CONFIRMED,
        CONFIDENCE_PROBABLE,
        CONFIDENCE_SPECIAL,
    )
    from catalog_core.phase9c_audit import SENTINEL_MAX

    assert (
        _classify_row(
            _R(
                service_type_confidence=CONFIDENCE_CONFIRMED,
                service_type="followers",
            )
        )
        == "STANDARD"
    )
    assert (
        _classify_row(_R(service_type_confidence=CONFIDENCE_PROBABLE)) == "PROBABLE"
    )
    assert _classify_row(_R(service_type_confidence=CONFIDENCE_SPECIAL)) == "SPECIAL"
    assert _classify_row(_R(max_quantity=SENTINEL_MAX)) == "SENTINEL"
    assert _classify_row(_R(has_execution_source=False)) == "MISSING_EXECUTION"
    assert (
        _classify_row(
            _R(
                platform_key="subscriptions",
                section_key="iptv_panel",
                pricing_mode="per_unit",
            )
        )
        == "IPTV"
    )


def test_artifact_wave1_complete():
    path = Path("scripts/out_phase9h_wave1.json")
    if not path.exists():
        pytest.skip("9H artifact missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["verdict"] == "PHASE 9H WAVE 1 COMPLETE — CONTROLLED EXPANSION VERIFIED"
    assert data["published"] is True
    assert data["published_by"] == "phase9h_wave1"
    sel = data["selection_report"]
    assert sel["wave_size"] == 15
    assert set(sel["legacy_ids"]) == set(FUTURE_PILOT_LEGACY_IDS)
    assert data["gate"]["all_fifteen_passed"] is True
    assert data["gate"]["excluded"] == []
    ver = data["verification"]
    assert ver["projection_count"] == 28
    assert ver["publications"] == 38
    assert ver["existing_thirteen_unchanged"] is True
    assert ver["shadow"]["dangerous_count"] == 0
    assert ver["shadow"]["catalog_count"] == 28
    assert ver["shadow"]["catalog_only_count"] == 0
    assert ver["shadow"]["legacy_count"] == 253
    assert ver["shadow"]["correlated_count"] == 28
    assert data["idempotency_second_pass"]["noop_or_no_change"] is True
    # Provider Service ID first in operational report rows
    for row in ver["provider_service_ids"]:
        assert "provider_service_id" in row
        assert isinstance(row["provider_service_id"], str)
        assert row["provider_service_id"]  # opaque TEXT non-empty
    # No blocked cohort silently included
    for c in data["gate"]["selected_wave"]:
        assert c["classification"] == "STANDARD"
        assert c["gate_passed"] is True
