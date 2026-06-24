"""CRUD for providers and provider_accounts tables."""
from __future__ import annotations

import re
from typing import Any

from database_connector import db_transaction, get_db
from services.provider_registry import (
    GOZIBRA_ADAPTER,
    clear_provider_caches,
    default_display_name_for_account,
    get_provider_account_record,
    get_provider_record,
    list_all_providers,
    list_provider_accounts,
)

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,49}$")
_ACCOUNT_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,49}$")


class ProviderAdminError(Exception):
    pass


def _normalize_slug(slug: str) -> str:
    value = str(slug or "").strip().lower()
    if not value or not _SLUG_RE.match(value):
        raise ProviderAdminError("معرّف المزوّد غير صالح (حروف إنجليزية صغيرة وأرقام و - _).")
    return value


def _normalize_account_key(account_key: str) -> str:
    value = str(account_key or "").strip().lower() or "default"
    if not _ACCOUNT_KEY_RE.match(value):
        raise ProviderAdminError("معرّف الحساب غير صالح.")
    return value


def _provider_to_dict(record) -> dict[str, Any]:
    return {
        "slug": record.slug,
        "name": record.name,
        "api_base_url": record.api_base_url,
        "adapter_type": record.adapter_type,
        "is_active": record.is_active,
    }


def _account_to_dict(record) -> dict[str, Any]:
    return {
        "provider_slug": record.provider_slug,
        "account_key": record.account_key,
        "api_key_env": record.api_key_env,
        "display_name": record.display_name,
        "is_active": record.is_active,
        "env_configured": bool(
            record.api_key_env
            and __import__("os").environ.get(record.api_key_env, "").strip()
        ),
    }


async def list_providers_with_accounts() -> list[dict[str, Any]]:
    providers = list_all_providers()
    accounts = list_provider_accounts()
    by_slug: dict[str, list[dict[str, Any]]] = {}
    for account in accounts:
        by_slug.setdefault(account.provider_slug, []).append(_account_to_dict(account))
    return [
        {
            **_provider_to_dict(provider),
            "accounts": by_slug.get(provider.slug, []),
        }
        for provider in providers
    ]


async def create_provider(
    *,
    slug: str,
    name: str,
    api_base_url: str,
    adapter_type: str = GOZIBRA_ADAPTER,
    is_active: bool = True,
) -> dict[str, Any]:
    normalized = _normalize_slug(slug)
    base_url = str(api_base_url or "").strip()
    if not base_url:
        raise ProviderAdminError("رابط API مطلوب.")
    display_name = str(name or normalized).strip() or normalized
    adapter = str(adapter_type or GOZIBRA_ADAPTER).strip() or GOZIBRA_ADAPTER

    async with db_transaction() as db:
        async with db.execute(
            "SELECT slug FROM providers WHERE slug = ?",
            (normalized,),
        ) as cursor:
            exists = await cursor.fetchone()
        if exists:
            raise ProviderAdminError("المزوّد موجود مسبقاً.")
        await db.execute(
            """
            INSERT INTO providers (slug, name, api_base_url, adapter_type, is_active)
            VALUES (?, ?, ?, ?, ?)
            """,
            (normalized, display_name, base_url, adapter, 1 if is_active else 0),
        )
        await db.commit()

    clear_provider_caches()
    record = get_provider_record(normalized)
    if record is None:
        raise ProviderAdminError("تعذّر إنشاء المزوّد.")
    return {**_provider_to_dict(record), "accounts": []}


async def update_provider(
    slug: str,
    *,
    name: str | None = None,
    api_base_url: str | None = None,
    adapter_type: str | None = None,
    is_active: bool | None = None,
) -> dict[str, Any]:
    normalized = _normalize_slug(slug)
    fields: list[str] = []
    params: list[Any] = []
    if name is not None:
        fields.append("name = ?")
        params.append(str(name).strip()[:200])
    if api_base_url is not None:
        url = str(api_base_url).strip()
        if not url:
            raise ProviderAdminError("رابط API لا يمكن أن يكون فارغاً.")
        fields.append("api_base_url = ?")
        params.append(url)
    if adapter_type is not None:
        fields.append("adapter_type = ?")
        params.append(str(adapter_type).strip() or GOZIBRA_ADAPTER)
    if is_active is not None:
        fields.append("is_active = ?")
        params.append(1 if is_active else 0)
    if not fields:
        raise ProviderAdminError("لا توجد حقول للتحديث.")

    params.append(normalized)
    async with db_transaction() as db:
        cursor = await db.execute(
            f"UPDATE providers SET {', '.join(fields)} WHERE slug = ?",
            params,
        )
        if int(cursor.rowcount or 0) == 0:
            raise ProviderAdminError("المزوّد غير موجود.")
        await db.commit()

    clear_provider_caches()
    record = get_provider_record(normalized)
    if record is None:
        raise ProviderAdminError("المزوّد غير موجود.")
    accounts = [
        _account_to_dict(a)
        for a in list_provider_accounts(normalized)
    ]
    return {**_provider_to_dict(record), "accounts": accounts}


async def create_provider_account(
    provider_slug: str,
    *,
    account_key: str,
    api_key_env: str,
    display_name: str | None = None,
    is_active: bool = True,
) -> dict[str, Any]:
    slug = _normalize_slug(provider_slug)
    account = _normalize_account_key(account_key)
    env_name = str(api_key_env or "").strip()
    if not env_name:
        raise ProviderAdminError("اسم متغير البيئة لمفتاح API مطلوب.")
    label = str(display_name or "").strip() or default_display_name_for_account(account)

    async with db_transaction() as db:
        async with db.execute("SELECT slug FROM providers WHERE slug = ?", (slug,)) as cursor:
            if not await cursor.fetchone():
                raise ProviderAdminError("المزوّد غير موجود.")
        async with db.execute(
            """
            SELECT id FROM provider_accounts
            WHERE provider_slug = ? AND account_key = ?
            """,
            (slug, account),
        ) as cursor:
            if await cursor.fetchone():
                raise ProviderAdminError("الحساب موجود مسبقاً لهذا المزوّد.")
        await db.execute(
            """
            INSERT INTO provider_accounts
                (provider_slug, account_key, api_key_env, display_name, is_active)
            VALUES (?, ?, ?, ?, ?)
            """,
            (slug, account, env_name, label, 1 if is_active else 0),
        )
        await db.commit()

    clear_provider_caches()
    record = get_provider_account_record(slug, account)
    if record is None:
        raise ProviderAdminError("تعذّر إنشاء الحساب.")
    return _account_to_dict(record)


async def update_provider_account(
    provider_slug: str,
    account_key: str,
    *,
    api_key_env: str | None = None,
    display_name: str | None = None,
    is_active: bool | None = None,
) -> dict[str, Any]:
    slug = _normalize_slug(provider_slug)
    account = _normalize_account_key(account_key)
    fields: list[str] = []
    params: list[Any] = []
    if api_key_env is not None:
        env_name = str(api_key_env).strip()
        if not env_name:
            raise ProviderAdminError("اسم متغير البيئة لا يمكن أن يكون فارغاً.")
        fields.append("api_key_env = ?")
        params.append(env_name)
    if display_name is not None:
        fields.append("display_name = ?")
        params.append(str(display_name).strip()[:200])
    if is_active is not None:
        fields.append("is_active = ?")
        params.append(1 if is_active else 0)
    if not fields:
        raise ProviderAdminError("لا توجد حقول للتحديث.")

    params.extend([slug, account])
    async with db_transaction() as db:
        cursor = await db.execute(
            f"""
            UPDATE provider_accounts
            SET {', '.join(fields)}
            WHERE provider_slug = ? AND account_key = ?
            """,
            params,
        )
        if int(cursor.rowcount or 0) == 0:
            raise ProviderAdminError("الحساب غير موجود.")
        await db.commit()

    clear_provider_caches()
    record = get_provider_account_record(slug, account)
    if record is None:
        raise ProviderAdminError("الحساب غير موجود.")
    return _account_to_dict(record)


async def list_provider_options() -> list[dict[str, str]]:
    """خيارات المزوّدين للفلاتر."""
    async with get_db() as db:
        async with db.execute(
            """
            SELECT slug, name FROM providers
            WHERE is_active = 1
            ORDER BY slug ASC
            """
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {"slug": str(row["slug"]), "name": str(row["name"] or row["slug"])}
        for row in rows
    ]
