# -*- coding: utf-8 -*-
"""Sync SQLite connections for Catalog core (foreign_keys ON)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from catalog_core.schema import ensure_soldium_catalog_schema


def resolve_db_path(db_path: Path | str | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    from database_connector import DB_PATH

    return Path(DB_PATH)


@contextmanager
def catalog_connection(
    db_path: Path | str | None = None,
    *,
    ensure_schema: bool = True,
) -> Iterator[sqlite3.Connection]:
    path = resolve_db_path(db_path)
    connection = sqlite3.connect(str(path), timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        if ensure_schema:
            ensure_soldium_catalog_schema(connection)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def catalog_transaction(
    db_path: Path | str | None = None,
    *,
    ensure_schema: bool = True,
) -> Iterator[sqlite3.Connection]:
    path = resolve_db_path(db_path)
    connection = sqlite3.connect(str(path), timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        if ensure_schema:
            ensure_soldium_catalog_schema(connection)
            # DDL may open an implicit transaction; close it before BEGIN IMMEDIATE.
            connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def catalog_readonly_connection(
    db_path: Path | str | None = None,
) -> Iterator[sqlite3.Connection]:
    """Open the shared DB in SQLite ``mode=ro`` — no writes, no schema ensure.

    Used by Phase 8C legacy migration Dry Run. Never commits.
    """
    path = resolve_db_path(db_path).resolve()
    uri = path.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        pass
    try:
        yield connection
    finally:
        connection.close()
