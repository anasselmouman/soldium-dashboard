# -*- coding: utf-8 -*-
"""Phase 9T — rollback rehearsal tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalog_core.phase9t_rollback_rehearsal import run_phase9t
from catalog_core.storefront_gateway import (
    build_storefront,
    reinitialize_storefront_selection,
    resolve_storefront_backend_name,
    set_storefront_backend_override,
)
from catalog_core.target_validation import _service_requires_comment_link
from utils.order_execution_identity import GEN0_EXECUTION_IDENTITY_MISSING


def test_phase9t_rollback_rehearsal_pass(tmp_path: Path):
    report = run_phase9t(isolated_db=tmp_path / "iso.db")
    assert report["production_cutover_occurred"] is False
    assert report["production_unchanged"] is True
    assert report["verdict"] == "PHASE_9T_COMPLETE — ROLLBACK_REHEARSAL_PASS"
    assert all(m["result"] == "PASS" for m in report["test_matrix"])
    assert report["data_immutability"]["production_equal"] is True
    assert report["provider_safety"]["calls"] == 0
    assert report["order_safety"]["real_orders_created"] is False
    assert report["publication_immutability"]["unchanged"] is True
    assert report["next_step"] == "PROCEED_TO_CONTROLLED_PRODUCTION_PILOT"


def test_reinitialize_clears_stale_override():
    set_storefront_backend_override("catalog")
    reinitialize_storefront_selection()
    assert resolve_storefront_backend_name(None, environ={}) == "legacy"
    assert resolve_storefront_backend_name("catalog") == "catalog"


def test_gen0_and_9n_still_intact():
    assert GEN0_EXECUTION_IDENTITY_MISSING == "GEN0_EXECUTION_IDENTITY_MISSING"
    assert _service_requires_comment_link({"link_type": "comment"}) is True
    assert _service_requires_comment_link({"id": "4371"}) is False


def test_artifact_if_present():
    path = Path("scripts/out_phase9t_rollback_rehearsal.json")
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["verdict"].startswith("PHASE_9T_")
    assert data["production_cutover_occurred"] is False
