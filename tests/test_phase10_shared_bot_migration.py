"""Phase 10: config isolation + shared bot migration failure policy."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

import config as dashboard_config
from db_schema import (
    SharedBotMigrationError,
    _bot_import_isolation,
    _run_shared_bot_migrations,
    load_bot_database_module,
)


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
BOT_ROOT = DASHBOARD_ROOT.parent / "soldium-bot"


def test_bot_database_resolves_min_referral_despite_dashboard_config(monkeypatch, tmp_path):
    """Dashboard config is loaded first; bot database must still see bot config."""
    assert "config" in sys.modules
    assert sys.modules["config"] is dashboard_config
    assert not hasattr(dashboard_config, "MIN_REFERRAL_WITHDRAW_DH")

    dashboard_secret = dashboard_config.SECRET_KEY
    dashboard_alert_hours = dashboard_config.ALERT_OLD_DEPOSIT_HOURS

    bot_db = load_bot_database_module()
    assert hasattr(bot_db, "MIN_REFERRAL_WITHDRAW_DH")
    assert bot_db.MIN_REFERRAL_WITHDRAW_DH == 20.0

    # Dashboard config module identity and values must be restored / unchanged.
    assert sys.modules["config"] is dashboard_config
    assert dashboard_config.SECRET_KEY == dashboard_secret
    assert dashboard_config.ALERT_OLD_DEPOSIT_HOURS == dashboard_alert_hours
    assert not hasattr(dashboard_config, "MIN_REFERRAL_WITHDRAW_DH")

    # init_db under isolation must succeed on a temp DB (uses bot services late-imports).
    db_path = tmp_path / "phase10_users.db"
    monkeypatch.setattr("database_connector.DB_PATH", db_path)
    _run_shared_bot_migrations()
    assert db_path.is_file()

    assert sys.modules["config"] is dashboard_config
    assert dashboard_config.SECRET_KEY == dashboard_secret


def test_shared_bot_init_db_failure_is_not_swallowed(monkeypatch, tmp_path):
    """Failed bot init_db must raise SharedBotMigrationError (not a soft warning)."""
    from contextlib import contextmanager

    monkeypatch.setattr("database_connector.DB_PATH", tmp_path / "fail.db")
    real_isolation = _bot_import_isolation

    @contextmanager
    def isolation_with_failing_init(bot_root):
        with real_isolation(bot_root):
            import database as bot_db

            def _boom():
                raise RuntimeError("simulated init_db failure")

            monkeypatch.setattr(bot_db, "init_db", _boom)
            yield

    monkeypatch.setattr("db_schema._bot_import_isolation", isolation_with_failing_init)

    with pytest.raises(SharedBotMigrationError, match="simulated init_db failure"):
        _run_shared_bot_migrations()


def test_run_schema_migrations_aborts_on_shared_bot_failure(monkeypatch):
    """main._run_schema_migrations must re-raise SharedBotMigrationError."""
    import main as dashboard_main

    async def _failing_shared():
        raise SharedBotMigrationError("shared bot init_db failed")

    async def _ok():
        return None

    monkeypatch.setattr(dashboard_main, "ensure_shared_bot_schema", _failing_shared)
    monkeypatch.setattr(dashboard_main, "ensure_timed_announcements_tables", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_scheduled_deletions_table", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_admin_alerts_table", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_admin_notifications_table", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_scheduled_orders_tables", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_soldium_catalog_tables", _ok)

    with pytest.raises(SharedBotMigrationError, match="shared bot init_db failed"):
        asyncio.run(dashboard_main._run_schema_migrations())


def test_optional_migration_failure_still_logged_not_fatal(monkeypatch):
    """Non-shared migration failures remain warnings and do not abort startup."""
    import main as dashboard_main

    async def _ok():
        return None

    async def _optional_boom():
        raise RuntimeError("optional dashboard migration failed")

    monkeypatch.setattr(dashboard_main, "ensure_shared_bot_schema", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_timed_announcements_tables", _optional_boom)
    monkeypatch.setattr(dashboard_main, "ensure_scheduled_deletions_table", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_admin_alerts_table", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_admin_notifications_table", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_scheduled_orders_tables", _ok)
    monkeypatch.setattr(dashboard_main, "ensure_soldium_catalog_tables", _ok)

    asyncio.run(dashboard_main._run_schema_migrations())
