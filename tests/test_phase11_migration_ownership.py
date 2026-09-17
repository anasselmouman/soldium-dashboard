"""Phase 11: migration ownership boundary (bot vs dashboard vs catalog)."""
from __future__ import annotations

import asyncio
import inspect
import sqlite3
from pathlib import Path

import pytest

from db_schema import (
    RequiredBotSchemaError,
    SharedBotMigrationError,
    _run_shared_bot_migrations,
    ensure_scheduled_orders_tables,
    ensure_shared_bot_schema,
    ensure_smm_services_table,
    ensure_soldium_catalog_tables,
    list_pending_bot_migrations,
    verify_required_bot_schema,
)


def _minimal_incomplete_orders_db(path: Path) -> None:
    """DB with orders present but missing a bot-owned column (provider_cost_dh)."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                balance REAL NOT NULL DEFAULT 0,
                total_spent REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                service_id TEXT NOT NULL DEFAULT '',
                link TEXT NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL DEFAULT 1,
                total_price REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_ensure_smm_no_longer_contains_bot_owned_alters():
    """Dashboard must not ship duplicate orders/smm/providers ALTER logic."""
    source = inspect.getsource(ensure_smm_services_table)
    assert "ALTER TABLE orders" not in source
    assert "ALTER TABLE smm_services" not in source
    assert "ALTER TABLE provider_accounts" not in source
    assert "PROVIDERS_DDL" not in source
    assert "SMM_SERVICES_DDL" not in source
    shared_source = inspect.getsource(ensure_shared_bot_schema)
    assert "ALTER TABLE orders" not in shared_source
    assert "await db.execute" not in shared_source


def test_complete_schema_skips_bot_init_db(monkeypatch, tmp_path):
    db_path = tmp_path / "complete.db"
    monkeypatch.setattr("database_connector.DB_PATH", db_path)
    _run_shared_bot_migrations()
    assert list_pending_bot_migrations() == []

    bridge_calls: list[str] = []
    verify_calls: list[str] = []
    real_bridge = _run_shared_bot_migrations
    real_verify = verify_required_bot_schema

    def _tracking_bridge(*_a, **_k):
        bridge_calls.append("init_db")
        return real_bridge(*_a, **_k)

    def _tracking_verify():
        verify_calls.append("verify")
        return real_verify()

    monkeypatch.setattr("db_schema._run_shared_bot_migrations", _tracking_bridge)
    monkeypatch.setattr("db_schema.verify_required_bot_schema", _tracking_verify)
    asyncio.run(ensure_shared_bot_schema())
    assert bridge_calls == []
    assert verify_calls == ["verify"]


def test_incomplete_schema_bridge_then_verify(monkeypatch, tmp_path):
    db_path = tmp_path / "incomplete.db"
    monkeypatch.setattr("database_connector.DB_PATH", db_path)
    assert not db_path.exists()
    assert list_pending_bot_migrations()

    calls: list[str] = []
    real = _run_shared_bot_migrations

    def _tracking(*_args, **_kwargs):
        calls.append("init_db")
        return real(*_args, **_kwargs)

    monkeypatch.setattr("db_schema._run_shared_bot_migrations", _tracking)
    asyncio.run(ensure_shared_bot_schema())
    assert calls == ["init_db"]
    assert list_pending_bot_migrations() == []
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        cols = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
    finally:
        conn.close()
    assert "orders" in tables
    assert "smm_services" in tables
    assert "provider_cost_dh" in cols


def test_dashboard_does_not_alter_orders_when_bridge_skipped(monkeypatch, tmp_path):
    """If pending check is bypassed, dashboard must still not ALTER orders itself."""
    db_path = tmp_path / "no_alter.db"
    _minimal_incomplete_orders_db(db_path)
    monkeypatch.setattr("database_connector.DB_PATH", db_path)
    monkeypatch.setattr("db_schema.list_pending_bot_migrations", lambda: [])
    monkeypatch.setattr(
        "db_schema._run_shared_bot_migrations",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("init_db must not run")),
    )
    asyncio.run(ensure_smm_services_table())
    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
    finally:
        conn.close()
    assert "provider_cost_dh" not in cols


def test_verify_required_bot_schema_fails_clearly(monkeypatch, tmp_path):
    db_path = tmp_path / "missing.db"
    _minimal_incomplete_orders_db(db_path)
    monkeypatch.setattr("database_connector.DB_PATH", db_path)
    with pytest.raises(RequiredBotSchemaError, match="Pending migrations"):
        verify_required_bot_schema()


def test_run_schema_migrations_aborts_on_required_schema_error(monkeypatch):
    import main as dashboard_main

    async def _failing_shared():
        raise RequiredBotSchemaError("missing bot schema")

    async def _ok():
        return None

    monkeypatch.setattr(dashboard_main, "ensure_shared_bot_schema", _failing_shared)
    monkeypatch.setattr(dashboard_main, "ensure_timed_announcements_tables", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_scheduled_deletions_table", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_admin_alerts_table", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_admin_notifications_table", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_scheduled_orders_tables", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_soldium_catalog_tables", _ok)

    with pytest.raises(RequiredBotSchemaError, match="missing bot schema"):
        asyncio.run(dashboard_main._run_schema_migrations())


def test_dashboard_owned_and_catalog_migrations_still_run(monkeypatch, tmp_path):
    db_path = tmp_path / "dash_owned.db"
    monkeypatch.setattr("database_connector.DB_PATH", db_path)
    # Bot schema first so get_db() has a file.
    _run_shared_bot_migrations()

    ran: list[str] = []

    async def _track_scheduled():
        ran.append("scheduled_orders")
        await ensure_scheduled_orders_tables()

    async def _track_catalog():
        ran.append("soldium_catalog")
        await ensure_soldium_catalog_tables()

    import main as dashboard_main

    async def _ok_shared():
        ran.append("shared_bot_schema")

    async def _ok():
        return None

    monkeypatch.setattr(dashboard_main, "ensure_shared_bot_schema", _ok_shared)
    monkeypatch.setattr(dashboard_main, "ensure_timed_announcements_tables", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_scheduled_deletions_table", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_admin_alerts_table", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_admin_notifications_table", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_scheduled_orders_tables", _track_scheduled)
    monkeypatch.setattr(dashboard_main, "ensure_soldium_catalog_tables", _track_catalog)

    asyncio.run(dashboard_main._run_schema_migrations())
    assert ran == ["shared_bot_schema", "scheduled_orders", "soldium_catalog"]

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert "scheduled_orders" in tables
    assert any(name.startswith("soldium_catalog_") for name in tables)


def test_shared_bot_init_db_failure_still_not_swallowed(monkeypatch, tmp_path):
    from contextlib import contextmanager

    from db_schema import _bot_import_isolation

    monkeypatch.setattr("database_connector.DB_PATH", tmp_path / "fail.db")
    real_isolation = _bot_import_isolation

    @contextmanager
    def isolation_with_failing_init(bot_root):
        with real_isolation(bot_root):
            import database as bot_db

            def _boom(*_a, **_k):
                raise RuntimeError("simulated init_db failure")

            monkeypatch.setattr(bot_db, "init_db", _boom)
            monkeypatch.setattr(bot_db, "_init_db_under_migration_lock", _boom)
            yield

    monkeypatch.setattr("db_schema._bot_import_isolation", isolation_with_failing_init)
    monkeypatch.setattr(
        "db_schema.list_pending_bot_migrations",
        lambda: ["orders_add_column:provider_cost_dh"],
    )

    with pytest.raises(SharedBotMigrationError, match="simulated init_db failure"):
        asyncio.run(ensure_shared_bot_schema())
