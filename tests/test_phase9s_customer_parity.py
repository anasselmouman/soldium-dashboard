# -*- coding: utf-8 -*-
"""Phase 9S — customer parity read-only tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from catalog_core.db import resolve_db_path
from catalog_core.phase9s_customer_parity import run_phase9s


def test_phase9s_cohort_cutover_safe():
    path = resolve_db_path().resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = run_phase9s(conn)
    finally:
        conn.close()

    assert report["production_unchanged"] is True
    assert report["production_cutover_occurred"] is False
    assert report["read_only"] is True
    assert report["verdict"] == "PHASE_9S_COMPLETE — 43_COHORT_CUTOVER_SAFE"
    counts = report["cohort_43_analysis"]["counts"]
    assert counts["customer_breaking"] == 0
    assert counts["commercial_breaking"] == 0
    assert counts["execution_breaking"] == 0
    assert counts["safety_breaking"] == 0
    assert report["pricing_parity"]["changed"] == 0
    assert report["pricing_parity"]["equal"] == 43
    assert report["legacy_only_210_accounting"]["sum_ok"] is True
    assert report["coverage_253_analysis"]["full_replacement_ready"] is False
    assert report["next_step"] == "START_PHASE_9T_ROLLBACK_REHEARSAL"


def test_artifact_if_present():
    path = Path("scripts/out_phase9s_customer_parity.json")
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["verdict"].startswith("PHASE_9S_")
    assert data["production_cutover_occurred"] is False
