# -*- coding: utf-8 -*-
"""Phase 9G-A — read-only business decision pack tests (isolated fixtures)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalog_core.phase9c_audit import SENTINEL_MAX
from catalog_core.phase9d_audit import (
    CONFIDENCE_CONFIRMED,
    CONFIDENCE_PROBABLE,
    CONFIDENCE_SPECIAL,
    CONFIDENCE_UNKNOWN,
)
from catalog_core.phase9g_business_decision_pack import partition_unpublished


class _R:
    def __init__(self, **kw):
        self.has_execution_source = kw.get("has_execution_source", True)
        self.pricing_mode = kw.get("pricing_mode", "per_1000")
        self.platform_key = kw.get("platform_key", "instagram")
        self.section_key = kw.get("section_key", "followers")
        self.subsection_key = kw.get("subsection_key")
        self.max_quantity = kw.get("max_quantity", 1000)
        self.bridge_review_codes = kw.get("bridge_review_codes", [])
        self.service_type_confidence = kw.get(
            "service_type_confidence", CONFIDENCE_CONFIRMED
        )
        self.service_type = kw.get("service_type", "followers")
        self.target_special = kw.get("target_special", False)
        self.ordering_confidence = kw.get("ordering_confidence", CONFIDENCE_CONFIRMED)
        self.soldium_service_id = kw.get("soldium_service_id", "svc_x")


def test_partition_exclusive_and_priority():
    rows = [
        _R(soldium_service_id="miss", has_execution_source=False),
        _R(
            soldium_service_id="iptv",
            platform_key="subscriptions",
            section_key="iptv_panel",
            pricing_mode="per_unit",
        ),
        _R(
            soldium_service_id="sent",
            max_quantity=SENTINEL_MAX,
            service_type_confidence=CONFIDENCE_PROBABLE,
        ),
        _R(
            soldium_service_id="prob",
            service_type_confidence=CONFIDENCE_PROBABLE,
            section_key="spaces",
        ),
        _R(
            soldium_service_id="spec",
            service_type_confidence=CONFIDENCE_SPECIAL,
            service_type="other",
        ),
        _R(
            soldium_service_id="unk",
            service_type_confidence=CONFIDENCE_UNKNOWN,
            service_type="other",
        ),
        _R(soldium_service_id="ready"),
        _R(
            soldium_service_id="other",
            service_type="followers",
            target_special=True,
            service_type_confidence=CONFIDENCE_CONFIRMED,
        ),
    ]
    parts = partition_unpublished(rows)
    assert sum(len(v) for v in parts.values()) == len(rows)
    assert [r.soldium_service_id for r in parts["missing_execution"]] == ["miss"]
    assert [r.soldium_service_id for r in parts["iptv"]] == ["iptv"]
    assert [r.soldium_service_id for r in parts["sentinel"]] == ["sent"]
    assert [r.soldium_service_id for r in parts["probable"]] == ["prob"]
    assert {r.soldium_service_id for r in parts["special_unknown"]} == {"spec", "unk"}
    assert [r.soldium_service_id for r in parts["ready"]] == ["ready"]
    assert [r.soldium_service_id for r in parts["other_review"]] == ["other"]


def test_artifact_if_present():
    path = Path("scripts/out_phase9g_business_decision_pack.json")
    if not path.exists():
        pytest.skip("9G artifact missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["verdict"] == "BUSINESS DECISION PACK COMPLETE"
    assert data["mutations"].startswith("NONE")
    assert data["production_unchanged"] is True
    inv = data["cohort_inventory"]
    assert inv["sum"] == inv["remaining_unpublished"] == 240
    assert inv["ready"] == 85
    assert inv["probable"] == 24
    assert inv["special_unknown"] == 69
    assert inv["sentinel"] == 22
    assert inv["iptv"] == 7
    assert inv["missing_execution"] == 5
    assert inv["other_review"] == 28
    assert data["executive_summary"]["published"] == 13
    assert 10 <= data["next_expansion_cohort"]["recommended_size"] <= 20
    assert data["next_expansion_cohort"]["do_not_publish"] is True
    assert data["missing_execution"]["count"] == 5
    for s in data["missing_execution"]["services"]:
        assert s["recommendation"] == "OPS_ACTION_REQUIRED"
        assert s["do_not_invent_account"] is True
    for s in data["sentinel"]["services"]:
        assert s["recommendation"] == "BLOCK_UNTIL_VERIFIED"
        assert s["finite_max_supported_by_evidence"] is False
