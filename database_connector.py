"""
Shared SQLite access for the admin dashboard.

Connects to the Telegram bot database (users.db) with settings that reduce
locking conflicts when the bot is running concurrently.

Catalog table ``smm_services`` is created via ``db_schema.ensure_smm_services_table``
(on app startup). Seed it once with ``python scripts/migrate_smm_services.py``.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("soldium.db")

# Absolute path to shared bot DB — stable regardless of process cwd.
_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_DB = os.path.abspath(os.path.join(_DIR, "..", "soldium-bot", "users.db"))
DB_PATH = os.path.abspath(os.getenv("SOLDIUM_DB_PATH", _DEFAULT_DB))

# Seconds to wait on SQLITE_BUSY before failing (matches bot-side tolerance).
CONNECT_TIMEOUT = float(os.getenv("SOLDIUM_DB_TIMEOUT", "30.0"))
BUSY_TIMEOUT_MS = 30000


class DatabaseLockedError(Exception):
    """Raised when SQLite is busy beyond the configured timeout."""


class DatabaseWriteError(Exception):
    """Raised when a write or commit fails."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause


async def _configure_connection(db: aiosqlite.Connection) -> None:
    """Apply pragmas that play well with a concurrently running bot writer."""
    await db.execute("PRAGMA foreign_keys=ON;")
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS};")


async def _commit(db: aiosqlite.Connection) -> None:
    """Commit with logging on operational failures."""
    try:
        await db.commit()
    except sqlite3.OperationalError as exc:
        logger.error(
            "SQLite commit failed on %s: %s",
            DB_PATH,
            exc,
            exc_info=True,
        )
        raise DatabaseWriteError(str(exc), cause=exc) from exc


@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """
    Yield an async SQLite connection. Use as:

        async with get_db() as db:
            async with db.execute("SELECT ...") as cursor:
                ...
    """
    if not os.path.isfile(DB_PATH):
        from utils.messages_ar import database_not_found

        raise FileNotFoundError(database_not_found(DB_PATH))

    db = await aiosqlite.connect(
        DB_PATH,
        timeout=CONNECT_TIMEOUT,
        isolation_level=None,
    )
    db.row_factory = aiosqlite.Row
    try:
        await _configure_connection(db)
        yield db
    finally:
        await db.close()


@asynccontextmanager
async def db_transaction() -> AsyncIterator[aiosqlite.Connection]:
    """
    Short exclusive transaction (BEGIN IMMEDIATE) for financial writes.
    Mirrors the bot's finalize_approved_deposit locking strategy.
    """
    async with get_db() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            yield db
            await _commit(db)
        except sqlite3.OperationalError as exc:
            try:
                await db.rollback()
            except sqlite3.OperationalError:
                pass
            logger.error(
                "SQLite operational error on %s: %s",
                DB_PATH,
                exc,
                exc_info=True,
            )
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise DatabaseLockedError(str(exc)) from exc
            raise DatabaseWriteError(str(exc), cause=exc) from exc
        except Exception:
            try:
                await db.rollback()
            except sqlite3.OperationalError:
                pass
            raise


async def count_users() -> int:
    """Quick read to verify dashboard can access the bot database."""
    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row[0])
