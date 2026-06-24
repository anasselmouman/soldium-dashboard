# -*- coding: utf-8 -*-
"""سجل المزوّدين للوحة التحكم — يقرأ من users.db المشتركة."""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from functools import lru_cache

from database_connector import DB_PATH

logger = logging.getLogger("soldium.provider_registry")

GOZIBRA_ADAPTER = "gozibra_v2"
LEGACY_GOZIBRA_SLUG = "gozibra"

DEFAULT_ACCOUNT_DISPLAY_NAMES: dict[str, str] = {
    "tiktok": "تيك توك",
    "instagram": "انستغرام",
    "facebook": "فيسبوك",
    "default": "افتراضي",
    "youtube": "يوتيوب",
    "telegram": "تيليغرام",
    "x": "إكس",
}


def default_display_name_for_account(account_key: str) -> str:
    key = str(account_key or "").strip().lower() or "default"
    return DEFAULT_ACCOUNT_DISPLAY_NAMES.get(key, key)


@dataclass(frozen=True)
class ProviderRecord:
    slug: str
    name: str
    api_base_url: str
    adapter_type: str
    is_active: bool


@dataclass(frozen=True)
class ProviderAccountRecord:
    provider_slug: str
    account_key: str
    api_key_env: str
    is_active: bool
    display_name: str = ""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


@lru_cache(maxsize=1)
def get_default_provider_slug() -> str:
    try:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT slug FROM providers
                WHERE is_active = 1
                ORDER BY slug ASC
                LIMIT 1
                """
            ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is not None:
        return str(row["slug"])
    return LEGACY_GOZIBRA_SLUG


def clear_provider_caches() -> None:
    get_default_provider_slug.cache_clear()
    get_provider_record.cache_clear()
    get_provider_account_record.cache_clear()


@lru_cache(maxsize=64)
def get_provider_record(slug: str) -> ProviderRecord | None:
    normalized = str(slug or get_default_provider_slug()).strip().lower()
    try:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT slug, name, api_base_url, adapter_type, is_active
                FROM providers WHERE slug = ?
                """,
                (normalized,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    base_url = str(row["api_base_url"] or "").strip()
    if not base_url:
        return None
    return ProviderRecord(
        slug=str(row["slug"]),
        name=str(row["name"] or row["slug"]),
        api_base_url=base_url,
        adapter_type=str(row["adapter_type"] or GOZIBRA_ADAPTER),
        is_active=bool(int(row["is_active"] or 0)),
    )


@lru_cache(maxsize=128)
def get_provider_account_record(provider_slug: str, account_key: str) -> ProviderAccountRecord | None:
    slug = str(provider_slug or get_default_provider_slug()).strip().lower()
    account = str(account_key or "default").strip().lower() or "default"
    try:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT provider_slug, account_key, api_key_env, is_active, display_name
                FROM provider_accounts
                WHERE provider_slug = ? AND account_key = ?
                """,
                (slug, account),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    display = str(row["display_name"] or "").strip()
    if not display:
        display = default_display_name_for_account(str(row["account_key"]))
    return ProviderAccountRecord(
        provider_slug=str(row["provider_slug"]),
        account_key=str(row["account_key"]),
        api_key_env=str(row["api_key_env"] or ""),
        is_active=bool(int(row["is_active"] or 0)),
        display_name=display,
    )


def resolve_api_key(provider_slug: str, account_key: str) -> str:
    slug = str(provider_slug or get_default_provider_slug()).strip().lower()
    account = str(account_key or "default").strip().lower() or "default"
    record = get_provider_account_record(slug, account)
    if record is None or not record.is_active or not record.api_key_env:
        raise RuntimeError(f"No active API account for provider={slug} account={account}")
    value = os.environ.get(record.api_key_env, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {record.api_key_env} is not set")
    return value


def list_active_provider_accounts(
    provider_slug: str | None = None,
) -> list[tuple[str, str]]:
    slug_filter = str(provider_slug or "").strip().lower() or None
    pairs: list[tuple[str, str]] = []
    try:
        with _connect() as conn:
            if slug_filter:
                rows = conn.execute(
                    """
                    SELECT pa.provider_slug, pa.account_key
                    FROM provider_accounts pa
                    JOIN providers p ON p.slug = pa.provider_slug
                    WHERE p.is_active = 1 AND pa.is_active = 1
                      AND pa.provider_slug = ?
                    ORDER BY pa.provider_slug, pa.account_key
                    """,
                    (slug_filter,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT pa.provider_slug, pa.account_key
                    FROM provider_accounts pa
                    JOIN providers p ON p.slug = pa.provider_slug
                    WHERE p.is_active = 1 AND pa.is_active = 1
                    ORDER BY pa.provider_slug, pa.account_key
                    """
                ).fetchall()
            pairs = [(str(r["provider_slug"]), str(r["account_key"])) for r in rows]
    except sqlite3.OperationalError:
        return []
    return pairs


def list_all_providers() -> list[ProviderRecord]:
    records: list[ProviderRecord] = []
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT slug FROM providers ORDER BY slug ASC"
            ).fetchall()
    except sqlite3.OperationalError:
        return records
    for row in rows:
        record = get_provider_record(str(row["slug"]))
        if record is not None:
            records.append(record)
    return records


def list_provider_accounts(provider_slug: str | None = None) -> list[ProviderAccountRecord]:
    slug_filter = str(provider_slug or "").strip().lower() or None
    accounts: list[ProviderAccountRecord] = []
    try:
        with _connect() as conn:
            if slug_filter:
                rows = conn.execute(
                    """
                    SELECT provider_slug, account_key
                    FROM provider_accounts
                    WHERE provider_slug = ?
                    ORDER BY account_key ASC
                    """,
                    (slug_filter,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT provider_slug, account_key
                    FROM provider_accounts
                    ORDER BY provider_slug ASC, account_key ASC
                    """
                ).fetchall()
    except sqlite3.OperationalError:
        return accounts
    for row in rows:
        record = get_provider_account_record(
            str(row["provider_slug"]),
            str(row["account_key"]),
        )
        if record is not None:
            accounts.append(record)
    return accounts
