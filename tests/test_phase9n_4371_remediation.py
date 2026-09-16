# -*- coding: utf-8 -*-
"""Phase 9N — 4371 remediation tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

from catalog_core.target_validation import (
    _service_requires_comment_link,
    validate_order_target,
    validate_platform_link,
)


def test_comment_policy_without_provider_id():
    svc = {"link_type": "comment"}
    assert _service_requires_comment_link(svc) is True
    assert _service_requires_comment_link({"target_link_type": "comment"}) is True
    assert _service_requires_comment_link({"id": "4371"}) is False
    ok, _ = validate_platform_link(
        "https://www.tiktok.com/@u/video/1?comment_id=2",
        "tiktok",
        section_key="likes",
        service=svc,
    )
    assert ok is True
    bad, _ = validate_platform_link(
        "https://www.tiktok.com/@u/video/1",
        "tiktok",
        section_key="likes",
        service=svc,
    )
    assert bad is False


def test_legacy_id_alone_does_not_force_comment_rules():
    ok, _ = validate_platform_link(
        "https://www.tiktok.com/@u/video/1",
        "tiktok",
        section_key="likes",
        service={"id": "4371"},
        service_id="4371",
    )
    assert ok is True


def test_validate_order_target_uses_link_type():
    ok, _ = validate_order_target(
        "https://www.tiktok.com/@u/video/1?comment_id=9",
        platform_key="tiktok",
        section_key="likes",
        link_type="comment",
    )
    assert ok is True
    bad, _ = validate_order_target(
        "https://www.tiktok.com/@u/video/1",
        platform_key="tiktok",
        section_key="likes",
        link_type="comment",
    )
    assert bad is False


def test_runtime_modules_have_no_4371_service_id_branch():
    pattern = re.compile(
        r"""str\(service_id[^)]*\)\s*==\s*["']4371["']|service_id\s*==\s*["']4371["']"""
    )
    paths = [
        Path("catalog_core/target_validation.py"),
        Path("../soldium-bot/utils/order_flow.py"),
    ]
    for path in paths:
        assert path.is_file(), f"missing {path}"
        text = path.read_text(encoding="utf-8")
        assert pattern.search(text) is None, f"hardcode remains in {path}"


def test_artifact_if_present():
    path = Path("scripts/out_phase9n_4371_remediation.json")
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["verdict"] == "PHASE_9N_COMPLETE — 4371 FALLBACK REMOVED"
    assert data["production_unchanged"] is True
    assert data["references_scan"]["runtime_hardcode_count"] == 0
    assert data["production_baseline"]["orders"] == 44
    assert data["next_step"] == "START_PHASE_9O_GEN0_FAIL_CLOSED"
