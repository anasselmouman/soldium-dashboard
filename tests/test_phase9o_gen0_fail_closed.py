# -*- coding: utf-8 -*-
"""Phase 9O — Gen-0 fail-closed regression tests."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import database_connector as db_conn
from catalog_core.target_validation import (
    _service_requires_comment_link,
    validate_order_target,
)
from db_schema import ensure_scheduled_orders_tables
from scheduled_orders import execute_scheduled_order
from services.provider_registry import clear_provider_caches
from utils.order_execution_identity import (
    GEN0_EXECUTION_IDENTITY_MISSING,
    InvalidProviderExternalServiceId,
    encode_provider_external_service_id_for_wire,
    is_gen1_execution_order,
)


def _seed_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                balance REAL NOT NULL DEFAULT 1000,
                total_spent REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE providers (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                api_base_url TEXT NOT NULL DEFAULT 'https://example.test',
                adapter_type TEXT NOT NULL DEFAULT 'gozibra_v2',
                is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE provider_accounts (
                id INTEGER PRIMARY KEY,
                provider_slug TEXT NOT NULL,
                account_key TEXT NOT NULL,
                api_key_env TEXT NOT NULL DEFAULT 'TEST_PROVIDER_KEY',
                is_active INTEGER NOT NULL DEFAULT 1,
                display_name TEXT NOT NULL DEFAULT '',
                UNIQUE(provider_slug, account_key)
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                service_name TEXT NOT NULL DEFAULT 'Test Service',
                service_id TEXT NOT NULL DEFAULT 'svc-1',
                link TEXT NOT NULL DEFAULT 'https://example.com/post',
                quantity INTEGER NOT NULL DEFAULT 100,
                amount REAL NOT NULL DEFAULT 10,
                total_price REAL NOT NULL DEFAULT 10,
                status TEXT NOT NULL DEFAULT 'completed',
                provider_order_id TEXT,
                fulfillment_mode TEXT NOT NULL DEFAULT 'auto',
                provider_slug TEXT NOT NULL DEFAULT 'gozibra',
                api_account TEXT NOT NULL DEFAULT 'default',
                provider_cost_dh REAL NOT NULL DEFAULT 0,
                catalog_id TEXT,
                external_service_id_snapshot TEXT,
                normalized_link TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_active_normalized_link
            ON orders (normalized_link)
            WHERE normalized_link IS NOT NULL
              AND TRIM(normalized_link) != ''
              AND LOWER(REPLACE(status, '_', ' ')) IN (
                  'pending', 'pending admin', 'submitted', 'in progress', 'processing'
              );
            CREATE TABLE smm_services (
                service_id TEXT PRIMARY KEY,
                catalog_id TEXT,
                local_item_id TEXT,
                external_service_id TEXT,
                name_ar TEXT NOT NULL DEFAULT '',
                provider_price_usd REAL NOT NULL DEFAULT 1,
                local_price_dh REAL NOT NULL DEFAULT 14,
                min_qty INTEGER NOT NULL DEFAULT 10,
                max_qty INTEGER NOT NULL DEFAULT 10000,
                is_active INTEGER NOT NULL DEFAULT 1,
                provider_slug TEXT NOT NULL DEFAULT 'gozibra',
                provider_api_account TEXT NOT NULL DEFAULT 'default',
                fulfillment_mode TEXT NOT NULL DEFAULT 'auto'
            );
            INSERT INTO users (user_id, balance) VALUES (999001, 500.0);
            INSERT INTO providers (slug, name, api_base_url, is_active)
            VALUES ('gozibra', 'Gozibra', 'https://example.test', 1);
            INSERT INTO provider_accounts
                (provider_slug, account_key, api_key_env, is_active)
            VALUES ('gozibra', 'default', 'TEST_PROVIDER_KEY', 1);
            INSERT INTO orders (
                user_id, service_name, service_id, link, quantity, amount, total_price,
                status, provider_order_id, fulfillment_mode, provider_slug, api_account,
                catalog_id, external_service_id_snapshot
            ) VALUES (
                123, 'Test', 'svc-1', 'https://example.com/x', 50, 5.0, 5.0,
                'completed', 'PROV-999', 'auto', 'gozibra', 'default',
                'svc-1', '12345'
            );
            INSERT INTO smm_services (
                service_id, catalog_id, local_item_id, external_service_id,
                provider_price_usd, local_price_dh, min_qty, max_qty,
                provider_slug, provider_api_account, fulfillment_mode
            ) VALUES (
                'gozibra-1', 'svc-1', 'svc-1', '99999',
                1.0, 14.0, 10, 10000, 'gozibra', 'default', 'auto'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "users.db"
    _seed_db(db_path)
    monkeypatch.setattr(db_conn, "DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_ID", "999001")
    monkeypatch.setenv("TEST_PROVIDER_KEY", "test-key-not-placeholder")
    monkeypatch.setenv("GOZIBRA_API_KEY", "test-key-not-placeholder")
    import config as dashboard_config
    import scheduled_orders as so_module

    monkeypatch.setattr(dashboard_config, "ADMIN_TELEGRAM_ID", 999001)
    monkeypatch.setattr(so_module, "ADMIN_TELEGRAM_ID", 999001)
    clear_provider_caches()
    yield db_path
    clear_provider_caches()


@pytest.fixture
def order_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "orders9o.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            balance REAL NOT NULL DEFAULT 1000,
            total_spent REAL NOT NULL DEFAULT 0,
            telegram_name TEXT
        );
        INSERT INTO users(user_id, balance, telegram_name) VALUES (1, 1000, 't');
        CREATE TABLE providers (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            api_base_url TEXT NOT NULL DEFAULT 'https://example.test',
            adapter_type TEXT NOT NULL DEFAULT 'gozibra_v2'
        );
        INSERT INTO providers(slug, name) VALUES ('gozibra', 'G');
        CREATE TABLE provider_accounts (
            provider_slug TEXT NOT NULL,
            account_key TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            api_key_env TEXT NOT NULL DEFAULT 'GOZIBRA_API_KEY',
            is_active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(provider_slug, account_key)
        );
        INSERT INTO provider_accounts(provider_slug, account_key, api_key_env)
        VALUES ('gozibra', 'default', 'GOZIBRA_API_KEY');
        CREATE TABLE smm_services (
            catalog_id TEXT PRIMARY KEY,
            external_service_id TEXT NOT NULL,
            service_id TEXT NOT NULL DEFAULT '',
            local_item_id TEXT NOT NULL DEFAULT '',
            provider_slug TEXT NOT NULL DEFAULT 'gozibra',
            provider_api_account TEXT,
            name_ar TEXT NOT NULL DEFAULT '',
            local_price_dh REAL NOT NULL DEFAULT 1,
            provider_price_usd REAL NOT NULL DEFAULT 0.1,
            min_qty INTEGER NOT NULL DEFAULT 1,
            max_qty INTEGER NOT NULL DEFAULT 10000,
            is_active INTEGER NOT NULL DEFAULT 1,
            platform_key TEXT NOT NULL DEFAULT 'tiktok',
            section_key TEXT,
            subsection_key TEXT,
            category TEXT NOT NULL DEFAULT '',
            fulfillment_mode TEXT NOT NULL DEFAULT 'auto'
        );
        INSERT INTO smm_services (
            catalog_id, external_service_id, service_id, local_item_id,
            provider_api_account, name_ar, local_price_dh
        ) VALUES (
            'cat-100', '555', '100', '100',
            'default', 'خدمة', 10.0
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            service_id TEXT NOT NULL,
            link TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            total_price REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            provider_order_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            service_name TEXT NOT NULL DEFAULT '',
            amount REAL NOT NULL DEFAULT 0.0,
            api_account TEXT NOT NULL DEFAULT 'default',
            fulfillment_mode TEXT NOT NULL DEFAULT 'auto',
            provider_cost_dh REAL NOT NULL DEFAULT 0,
            provider_slug TEXT NOT NULL DEFAULT 'gozibra',
            catalog_id TEXT,
            external_service_id_snapshot TEXT,
            refunded_amount REAL NOT NULL DEFAULT 0,
            status_note TEXT,
            normalized_link TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_active_normalized_link
        ON orders (normalized_link)
        WHERE normalized_link IS NOT NULL
          AND TRIM(normalized_link) != ''
          AND LOWER(REPLACE(status, '_', ' ')) IN (
              'pending', 'pending admin', 'submitted', 'in progress', 'processing'
          );
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("SOLDIUM_DB_PATH", str(path))
    monkeypatch.setenv("GOZIBRA_API_KEY", "test-key-not-placeholder")
    import database_connector as dc

    monkeypatch.setattr(dc, "DB_PATH", path)
    return path


def _past() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _insert_scheduled_job(
    db: Path,
    *,
    external_service_id: str | None,
    provider_slug: str = "gozibra",
) -> int:
    conn = sqlite3.connect(db)
    try:
        job_id = conn.execute(
            """
            INSERT INTO scheduled_orders (
                template_order_id, user_id, service_id, service_name, link,
                provider_slug, api_account, fulfillment_mode, external_service_id,
                quantity_mode, quantity_fixed, interval_days, next_run_at, status
            ) VALUES (
                1, 999001, 'svc-1', 'Test', 'https://example.com/x',
                ?, 'default', 'auto', ?,
                'fixed', 50, 1, ?, 'active'
            )
            """,
            (provider_slug, external_service_id, _past()),
        ).lastrowid
        conn.commit()
        return int(job_id)
    finally:
        conn.close()


# --- A / B / G / K: Gen-1 frozen TEXT path ---


def test_a_b_gen1_uses_frozen_text_no_legacy_lookup(temp_db: Path) -> None:
    async def _run() -> None:
        await ensure_scheduled_orders_tables()
        job_id = _insert_scheduled_job(temp_db, external_service_id="0012345")
        mock_svc = {
            "catalog_id": "svc-1",
            "local_price_dh": 14.0,
            "provider_price_usd": 1.0,
            "price_per_unit": False,
        }
        lookup = AsyncMock(side_effect=AssertionError("must not lookup"))
        submit = AsyncMock(return_value="prov-1")
        with patch("scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)), patch(
            "scheduled_orders._lookup_service_provider_meta", new=lookup
        ), patch("scheduled_orders.submit_provider_order", new=submit):
            result = await execute_scheduled_order(job_id)
        assert result["ok"] is True
        kwargs = submit.await_args.kwargs
        assert kwargs["service_id"] == "0012345"
        assert isinstance(kwargs["service_id"], str)
        lookup.assert_not_called()

    asyncio.run(_run())


def test_g_gen1_retry_keeps_frozen_identity(temp_db: Path) -> None:
    async def _run() -> None:
        await ensure_scheduled_orders_tables()
        job_id = _insert_scheduled_job(temp_db, external_service_id="7788")
        mock_svc = {
            "catalog_id": "svc-1",
            "local_price_dh": 14.0,
            "provider_price_usd": 1.0,
            "price_per_unit": False,
        }
        submit = AsyncMock(return_value="prov-a")
        with patch("scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)), patch(
            "scheduled_orders._lookup_service_provider_meta",
            new=AsyncMock(side_effect=AssertionError("no")),
        ), patch("scheduled_orders.submit_provider_order", new=submit):
            assert (await execute_scheduled_order(job_id))["ok"] is True
        conn = sqlite3.connect(temp_db)
        try:
            # Free the target link before the next materialization (active-link guard).
            conn.execute(
                """
                UPDATE orders SET status = 'completed'
                WHERE link = 'https://example.com/x'
                  AND LOWER(REPLACE(status, '_', ' ')) IN (
                      'pending', 'pending admin', 'submitted',
                      'in progress', 'processing'
                  )
                """
            )
            conn.execute(
                "UPDATE scheduled_orders SET next_run_at = ?, status = 'active' WHERE id = ?",
                (_past(), job_id),
            )
            conn.commit()
        finally:
            conn.close()
        submit2 = AsyncMock(return_value="prov-b")
        with patch("scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)), patch(
            "scheduled_orders._lookup_service_provider_meta",
            new=AsyncMock(side_effect=AssertionError("retry no lookup")),
        ), patch("scheduled_orders.submit_provider_order", new=submit2):
            result = await execute_scheduled_order(job_id, force=True)
        assert result["ok"] is True
        assert submit2.await_args.kwargs["service_id"] == "7788"

    asyncio.run(_run())


# --- C / D / E / F / H: Gen-0 fail-closed ---


def test_c_d_e_f_gen0_fails_closed_no_lookup_no_submit_no_conversion(
    temp_db: Path,
) -> None:
    async def _run() -> None:
        await ensure_scheduled_orders_tables()
        job_id = _insert_scheduled_job(temp_db, external_service_id=None)
        mock_svc = {
            "catalog_id": "svc-1",
            "local_price_dh": 14.0,
            "provider_price_usd": 1.0,
            "price_per_unit": False,
        }
        lookup = AsyncMock(
            return_value={
                "external_service_id": "1001",
                "provider_slug": "gozibra",
                "account_key": "default",
            }
        )
        submit = AsyncMock(return_value="should-not")
        with patch("scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)), patch(
            "scheduled_orders._lookup_service_provider_meta", new=lookup
        ), patch("scheduled_orders.submit_provider_order", new=submit):
            result = await execute_scheduled_order(job_id)
        assert result["ok"] is False
        assert GEN0_EXECUTION_IDENTITY_MISSING in str(result.get("error") or "")
        lookup.assert_not_called()
        submit.assert_not_called()
        conn = sqlite3.connect(temp_db)
        try:
            # F: no automatic Gen-1 conversion of the job
            ext = conn.execute(
                "SELECT external_service_id FROM scheduled_orders WHERE id = ?",
                (job_id,),
            ).fetchone()[0]
            assert ext is None
            assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        finally:
            conn.close()

    asyncio.run(_run())


def test_h_gen0_retry_fails_closed(temp_db: Path) -> None:
    async def _run() -> None:
        await ensure_scheduled_orders_tables()
        job_id = _insert_scheduled_job(temp_db, external_service_id=None)
        mock_svc = {
            "catalog_id": "svc-1",
            "local_price_dh": 14.0,
            "provider_price_usd": 1.0,
            "price_per_unit": False,
        }
        lookup = AsyncMock(
            return_value={
                "external_service_id": "1001",
                "provider_slug": "gozibra",
                "account_key": "default",
            }
        )
        submit = AsyncMock()
        with patch("scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)), patch(
            "scheduled_orders._lookup_service_provider_meta", new=lookup
        ), patch("scheduled_orders.submit_provider_order", new=submit):
            r1 = await execute_scheduled_order(job_id)
            r2 = await execute_scheduled_order(job_id, force=True)
        assert r1["ok"] is False
        assert r2["ok"] is False
        lookup.assert_not_called()
        submit.assert_not_called()

    asyncio.run(_run())


def test_i_j_empty_and_whitespace_provider_id_fail_closed(temp_db: Path) -> None:
    async def _run() -> None:
        await ensure_scheduled_orders_tables()
        mock_svc = {
            "catalog_id": "svc-1",
            "local_price_dh": 14.0,
            "provider_price_usd": 1.0,
            "price_per_unit": False,
        }
        for value in ("", "   "):
            job_id = _insert_scheduled_job(temp_db, external_service_id=value)
            lookup = AsyncMock(
                return_value={
                    "external_service_id": "1001",
                    "provider_slug": "gozibra",
                    "account_key": "default",
                }
            )
            submit = AsyncMock()
            with patch(
                "scheduled_orders.get_service", new=AsyncMock(return_value=mock_svc)
            ), patch(
                "scheduled_orders._lookup_service_provider_meta", new=lookup
            ), patch("scheduled_orders.submit_provider_order", new=submit):
                result = await execute_scheduled_order(job_id)
            assert result["ok"] is False
            assert GEN0_EXECUTION_IDENTITY_MISSING in str(result.get("error") or "")
            lookup.assert_not_called()
            submit.assert_not_called()

    asyncio.run(_run())


def test_k_provider_id_remains_opaque_text():
    assert encode_provider_external_service_id_for_wire("0042") == "0042"
    assert isinstance(encode_provider_external_service_id_for_wire("42"), str)
    with pytest.raises(InvalidProviderExternalServiceId):
        encode_provider_external_service_id_for_wire("")
    with pytest.raises(InvalidProviderExternalServiceId):
        encode_provider_external_service_id_for_wire("   ")
    with pytest.raises(InvalidProviderExternalServiceId):
        encode_provider_external_service_id_for_wire(None)


# --- Manual order paths ---


def test_manual_gen1_uses_frozen_no_lookup(order_db: Path) -> None:
    conn = sqlite3.connect(order_db)
    conn.execute(
        """
        INSERT INTO orders (
            user_id, service_name, service_id, link, quantity, amount, total_price,
            status, api_account, provider_slug, fulfillment_mode,
            catalog_id, external_service_id_snapshot
        ) VALUES (
            1, 'خدمة', '100', 'https://x.test', 10, 1.0, 1.0,
            'pending_admin', 'default', 'gozibra', 'admin',
            'cat-100', '777'
        )
        """
    )
    conn.execute(
        "UPDATE smm_services SET external_service_id='999' WHERE catalog_id='cat-100'"
    )
    conn.commit()
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    conn.close()

    captured: dict = {}

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return "REF"

    async def _run():
        with patch(
            "manual_orders.submit_provider_order", AsyncMock(side_effect=fake_submit)
        ), patch(
            "manual_orders._lookup_service_provider_meta",
            AsyncMock(side_effect=AssertionError("must not lookup")),
        ):
            from manual_orders import ensure_provider_order_ref, get_manual_order

            order = await get_manual_order(oid)
            assert is_gen1_execution_order(order)
            ref = await ensure_provider_order_ref(order)
            assert ref == "REF"
            assert captured["service_id"] == "777"

    asyncio.run(_run())


def test_manual_gen0_fails_closed_no_lookup_no_submit(order_db: Path) -> None:
    conn = sqlite3.connect(order_db)
    conn.execute(
        """
        INSERT INTO orders (
            user_id, service_name, service_id, link, quantity, amount, total_price,
            status, api_account, provider_slug, fulfillment_mode
        ) VALUES (
            1, 'خدمة', '100', 'https://x.test', 10, 1.0, 1.0,
            'pending_admin', 'default', 'gozibra', 'admin'
        )
        """
    )
    conn.commit()
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    conn.close()

    async def _run():
        with patch("manual_orders.submit_provider_order", AsyncMock()) as mock_sub, patch(
            "manual_orders._lookup_service_provider_meta",
            AsyncMock(side_effect=AssertionError("must not lookup")),
        ):
            from manual_orders import ensure_provider_order_ref, get_manual_order

            order = await get_manual_order(oid)
            assert not is_gen1_execution_order(order)
            assert await ensure_provider_order_ref(order) is None
            mock_sub.assert_not_called()
            # F: order remains Gen-0
            order2 = await get_manual_order(oid)
            assert order2.get("external_service_id_snapshot") in (None, "")

    asyncio.run(_run())


def test_manual_gen0_whitespace_snapshot_fails_closed(order_db: Path) -> None:
    conn = sqlite3.connect(order_db)
    conn.execute(
        """
        INSERT INTO orders (
            user_id, service_name, service_id, link, quantity, amount, total_price,
            status, api_account, provider_slug, fulfillment_mode,
            external_service_id_snapshot
        ) VALUES (
            1, 'خدمة', '100', 'https://x.test', 10, 1.0, 1.0,
            'pending_admin', 'default', 'gozibra', 'admin',
            '   '
        )
        """
    )
    conn.commit()
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    conn.close()

    async def _run():
        with patch("manual_orders.submit_provider_order", AsyncMock()) as mock_sub, patch(
            "manual_orders._lookup_service_provider_meta",
            AsyncMock(side_effect=AssertionError("must not")),
        ):
            from manual_orders import ensure_provider_order_ref, get_manual_order

            order = await get_manual_order(oid)
            assert await ensure_provider_order_ref(order) is None
            mock_sub.assert_not_called()

    asyncio.run(_run())


# --- L: Phase 9N link_type intact ---


def test_l_phase9n_link_type_comment_intact():
    assert _service_requires_comment_link({"link_type": "comment"}) is True
    assert _service_requires_comment_link({"id": "4371"}) is False
    ok, _ = validate_order_target(
        "https://www.tiktok.com/@u/video/1?comment_id=9",
        platform_key="tiktok",
        section_key="likes",
        link_type="comment",
    )
    assert ok is True


# --- M: Catalog adapter/intent contract intact ---


def test_m_catalog_adapter_execution_identity_contract():
    from catalog_core.models import ExecutionSource
    from catalog_core.storefront_adapter import StorefrontAdapter
    from catalog_core.storefront_projection import PublishedStorefrontProjection

    src = ExecutionSource(
        id="src_test",
        service_id="svc_test",
        provider_slug="gozibra",
        provider_account_key="default",
        external_service_id="1154",
    )
    assert isinstance(src.external_service_id, str)
    assert src.external_service_id == "1154"
    assert hasattr(StorefrontAdapter, "resolve_order_intent")
    assert hasattr(PublishedStorefrontProjection, "build")
    assert hasattr(PublishedStorefrontProjection, "get_service")


# --- Static: _submit_job_order must not call live lookup ---


def test_static_submit_job_order_has_no_live_lookup():
    text = Path("scheduled_orders.py").read_text(encoding="utf-8")
    # Isolate _submit_job_order body until next top-level async def
    m = re.search(
        r"async def _submit_job_order\([\s\S]*?(?=\nasync def |\ndef )",
        text,
    )
    assert m is not None
    body = m.group(0)
    assert "_lookup_service_provider_meta" not in body
    assert GEN0_EXECUTION_IDENTITY_MISSING in body


def test_constant_stable():
    assert GEN0_EXECUTION_IDENTITY_MISSING == "GEN0_EXECUTION_IDENTITY_MISSING"
