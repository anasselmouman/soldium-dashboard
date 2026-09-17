"""Phase 12: dashboard uses the same migration lock + post-lock re-check."""
from __future__ import annotations

import asyncio
import multiprocessing
import time
from pathlib import Path

import pytest

from db_schema import (
    SharedBotMigrationError,
    _run_shared_bot_migrations,
    ensure_shared_bot_schema,
    list_pending_bot_migrations,
)


def test_dashboard_bridge_uses_migration_lock(monkeypatch, tmp_path):
    db_path = tmp_path / "dash.db"
    monkeypatch.setattr("database_connector.DB_PATH", db_path)
    assert list_pending_bot_migrations()

    holders: list[str] = []
    from db_schema import _load_bot_migration_lock_module

    ml = _load_bot_migration_lock_module()
    real = ml.migration_lock

    def _tracking(*, db_path, holder, timeout_seconds=None):
        holders.append(holder)
        return real(db_path=db_path, holder=holder, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(ml, "migration_lock", _tracking)
    asyncio.run(ensure_shared_bot_schema())
    assert holders == ["soldium-dashboard"]
    assert list_pending_bot_migrations() == []


def test_recheck_after_lock_skips_duplicate_bridge(monkeypatch, tmp_path):
    """Another process migrates between pre-check and post-lock re-check."""
    db_path = tmp_path / "recheck.db"
    monkeypatch.setattr("database_connector.DB_PATH", db_path)

    # Pre-check sees pending (DB missing).
    assert list_pending_bot_migrations()

    bridge_calls: list[str] = []

    def _bridge(**_kwargs):
        bridge_calls.append("bridge")

    monkeypatch.setattr(
        "db_schema._run_shared_bot_migrations",
        _bridge,
    )

    # While "waiting" for lock, simulate peer migration completing the schema.
    from db_schema import _load_bot_migration_lock_module

    ml = _load_bot_migration_lock_module()
    real_lock = ml.migration_lock

    def _lock_then_peer_migrates(*, db_path, holder, timeout_seconds=None):
        # Peer migrates before we enter the critical section body.
        _run_shared_bot_migrations()
        assert list_pending_bot_migrations() == []
        return real_lock(db_path=db_path, holder=holder, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(ml, "migration_lock", _lock_then_peer_migrates)
    asyncio.run(ensure_shared_bot_schema())
    assert bridge_calls == []


def _child_hold_migrate_lock(db_path: str, ready_file: str, release_file: str) -> None:
    import importlib.util
    from pathlib import Path as P

    # .../soldium-dashboard/tests/this_file.py → repo root is parents[2]
    bot_root = P(__file__).resolve().parents[2] / "soldium-bot"
    mod_path = bot_root / "migration_lock.py"
    spec = importlib.util.spec_from_file_location("child_ml", mod_path)
    assert spec and spec.loader
    ml = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ml)
    with ml.migration_lock(db_path=db_path, holder="child", timeout_seconds=5.0):
        P(ready_file).write_text("ready", encoding="utf-8")
        deadline = time.time() + 30
        while time.time() < deadline:
            if P(release_file).exists():
                break
            time.sleep(0.05)


def test_dashboard_lock_timeout_fails_closed(monkeypatch, tmp_path):
    db_path = tmp_path / "timeout.db"
    monkeypatch.setattr("database_connector.DB_PATH", db_path)
    # Force pending path without needing a real incomplete schema dance:
    monkeypatch.setattr(
        "db_schema.list_pending_bot_migrations",
        lambda: ["orders_add_column:provider_cost_dh"],
    )

    ready = tmp_path / "ready.txt"
    release = tmp_path / "release.txt"
    proc = multiprocessing.Process(
        target=_child_hold_migrate_lock,
        args=(str(db_path), str(ready), str(release)),
    )
    proc.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not ready.exists():
            time.sleep(0.05)
        assert ready.exists()

        from db_schema import _load_bot_migration_lock_module

        ml = _load_bot_migration_lock_module()
        real = ml.migration_lock

        def _short(*, db_path, holder, timeout_seconds=None):
            return real(db_path=db_path, holder=holder, timeout_seconds=0.3)

        monkeypatch.setattr(ml, "migration_lock", _short)

        with pytest.raises(SharedBotMigrationError, match="Timed out"):
            asyncio.run(ensure_shared_bot_schema())
    finally:
        release.write_text("go", encoding="utf-8")
        proc.join(timeout=10)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
