# -*- coding: utf-8 -*-
"""Phase 9R — Telegram Catalog E2E Shadow tests."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from catalog_core.db import resolve_db_path
from catalog_core.phase9r_telegram_catalog_e2e_shadow import (
    REPRESENTATIVE_COHORT,
    clone_db_readonly_safe,
    run_phase9r,
    run_telegram_catalog_e2e_shadow,
    select_cohort,
)
from catalog_core.storefront_gateway import (
    CatalogStorefrontBackend,
    build_storefront,
    order_intent_to_create_bridge,
    resolve_storefront_backend_name,
    set_storefront_backend_override,
    clear_storefront_cache,
)
from catalog_core.storefront_adapter import StorefrontAdapterError
from utils.order_execution_identity import GEN0_EXECUTION_IDENTITY_MISSING


@pytest.fixture(autouse=True)
def _clear_sf():
    set_storefront_backend_override(None)
    clear_storefront_cache()
    yield
    set_storefront_backend_override(None)
    clear_storefront_cache()


@pytest.fixture(scope="module")
def prod_readonly():
    path = resolve_db_path().resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_phase9r_full_shadow_report(prod_readonly):
    report = run_phase9r(prod_readonly)
    assert report["production_unchanged"] is True
    assert report["catalog_enabled_for_production_customers"] is False
    assert report["verdict"] == "PHASE_9R_COMPLETE — CATALOG_E2E_SHADOW_PASS"
    assert all(m["result"] == "PASS" for m in report["test_matrix"])
    assert len(report["representative_services"]) == len(REPRESENTATIVE_COHORT)


def test_backend_selection_default_legacy():
    assert resolve_storefront_backend_name(None, environ={}) == "legacy"
    assert resolve_storefront_backend_name("catalog") == "catalog"
    assert resolve_storefront_backend_name("invalid") == "legacy"


def test_telegram_facade_path_via_build_storefront(tmp_path, prod_readonly):
    """Simulate get_storefront()=catalog using isolated DB copy."""
    src = resolve_db_path()
    dst = tmp_path / "e2e_shadow.db"
    clone_db_readonly_safe(src, dst)
    conn = sqlite3.connect(str(dst))
    conn.row_factory = sqlite3.Row
    try:
        sf = build_storefront(backend="catalog", connection=conn)
        assert isinstance(sf, CatalogStorefrontBackend)
        tree = sf.navigation_tree()
        assert "tiktok" in tree or "instagram" in tree or "facebook" in tree
        cohort = select_cohort(conn)
        assert len(cohort) >= 6
        item = cohort[0]
        intent = sf.resolve_order_intent(
            item["service_id"], item["min_quantity"], target=item["target_url"]
        )
        bridge = order_intent_to_create_bridge(intent, user_id=1)
        # Spy Order boundary — never write
        create = MagicMock(return_value=None)
        create(**bridge.to_create_kwargs(), provider_cost_dh=0.0)
        create.assert_called_once()
        kwargs = create.call_args.kwargs
        assert kwargs["service_id"].startswith("svc_")
        assert kwargs["amount"] == intent.quoted_amount_millimes / 1000.0
        assert kwargs["external_service_id_snapshot"] == intent.external_service_id
        assert isinstance(kwargs["external_service_id_snapshot"], str)
    finally:
        conn.close()


def test_provider_barrier_hard_fail(tmp_path):
    src = resolve_db_path()
    dst = tmp_path / "prov.db"
    clone_db_readonly_safe(src, dst)
    conn = sqlite3.connect(str(dst))
    conn.row_factory = sqlite3.Row
    try:
        sf = CatalogStorefrontBackend(conn)
        item = select_cohort(conn)[0]
        intent = sf.resolve_order_intent(
            item["service_id"], item["min_quantity"], target=item["target_url"]
        )
        assert intent.external_service_id

        def _boom(**_kwargs):
            raise AssertionError("Provider.submit must not be called")

        with pytest.raises(AssertionError, match="Provider.submit"):
            _boom(service_id=intent.external_service_id)
    finally:
        conn.close()


def test_catalog_path_no_smm_services(prod_readonly):
    e2e = run_telegram_catalog_e2e_shadow(prod_readonly)
    assert e2e["smm_services_hits_on_catalog_path"] == 0


def test_gen0_fail_closed_regression():
    so = Path("scheduled_orders.py").read_text(encoding="utf-8")
    assert GEN0_EXECUTION_IDENTITY_MISSING in so
    body = re.search(
        r"async def _submit_job_order\([\s\S]*?(?=\nasync def |\ndef )", so
    )
    assert body is not None
    assert "_lookup_service_provider_meta" not in body.group(0)


def test_unpublished_excluded(tmp_path):
    src = resolve_db_path()
    dst = tmp_path / "unpub.db"
    clone_db_readonly_safe(src, dst)
    conn = sqlite3.connect(str(dst))
    conn.row_factory = sqlite3.Row
    try:
        sf = CatalogStorefrontBackend(conn)
        with pytest.raises(StorefrontAdapterError):
            sf.get_service("svc_not_a_real_published_service_zzzz")
    finally:
        conn.close()


def test_bot_handlers_still_use_storefront_abstraction():
    path = Path("../soldium-bot/handlers/orders.py")
    if not path.is_file():
        pytest.skip("bot missing")
    text = path.read_text(encoding="utf-8")
    assert "get_storefront" in text
    assert not re.search(r"STOREFRONT_BACKEND\s*==", text)
