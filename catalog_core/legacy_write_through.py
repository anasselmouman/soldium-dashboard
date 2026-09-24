# -*- coding: utf-8 -*-
"""Catalog Admin → Legacy ``smm_services`` write-through for bridged services.

Uses ``soldium_catalog_legacy_bridge`` as the sole identity correlation.
Never invents legacy rows. Never fuzzy-matches. Runs in the caller's SQLite
transaction so Catalog + Legacy commit or roll back together.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from catalog_core.errors import CatalogConflictError
from catalog_core.legacy_migration import LEGACY_SERVICE_BRIDGE_TABLE
from catalog_core.pricing import format_dh_amount, millimes_to_dh_decimal

logger = logging.getLogger("soldium.catalog.legacy_write_through")

SMM_TABLE = "smm_services"

# Arabic message for UNIQUE(provider_slug, external_service_id) on smm_services.
LEGACY_PROVIDER_EXTERNAL_UNIQUE_MESSAGE = (
    "معرّف الخدمة لدى المورد مستخدم بالفعل مع خدمة أخرى."
)

# Catalog status → Legacy is_active (customer-facing visibility).
# draft must never appear in Telegram (loader filters is_active=1).
STATUS_TO_IS_ACTIVE: dict[str, int] = {
    "active": 1,
    "archived": 0,
    "draft": 0,
}


def is_legacy_provider_external_unique_violation(exc: BaseException) -> bool:
    """True only for UNIQUE(provider_slug, external_service_id) on smm_services.

    Matches table UNIQUE and the named unique index used in production.
    Unrelated IntegrityError messages must return False.
    """
    text = str(exc).lower()
    if "unique" not in text:
        return False
    if "idx_smm_services_provider_external" in text:
        return True
    return "provider_slug" in text and "external_service_id" in text


WriteThroughOutcome = Literal[
    "applied",
    "skipped_no_bridge",
    "skipped_no_changes",
    "skipped_unchanged",
]


@dataclass(frozen=True)
class WriteThroughResult:
    outcome: WriteThroughOutcome
    soldium_service_id: str
    legacy_catalog_id: str | None = None
    columns_updated: tuple[str, ...] = ()
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "applied": self.outcome == "applied",
            "soldium_service_id": self.soldium_service_id,
            "legacy_catalog_id": self.legacy_catalog_id,
            "columns_updated": list(self.columns_updated),
            "message": self.message,
            "has_legacy_counterpart": self.outcome == "applied"
            or (
                self.outcome not in {"skipped_no_bridge"}
                and self.legacy_catalog_id is not None
            ),
        }


def catalog_status_to_is_active(status: str) -> int:
    key = str(status or "").strip().lower()
    if key not in STATUS_TO_IS_ACTIVE:
        raise CatalogConflictError(f"حالة كتالوج غير معروفة للمزامنة: {status}")
    return STATUS_TO_IS_ACTIVE[key]


def millimes_to_local_price_dh(millimes: int) -> float:
    """Convert Catalog millimes → Legacy ``local_price_dh`` without float math.

    Uses Decimal → canonical DH text → float for the REAL column, matching
    Catalog pricing precision (≤3 decimal places).
    """
    text = format_dh_amount(int(millimes))
    return float(Decimal(text))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _smm_columns(conn: sqlite3.Connection) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({SMM_TABLE})")}


def resolve_legacy_catalog_id(
    conn: sqlite3.Connection, soldium_service_id: str
) -> str | None:
    """Bridge lookup only — no fuzzy matching."""
    if not _table_exists(conn, LEGACY_SERVICE_BRIDGE_TABLE):
        return None
    row = conn.execute(
        f"""
        SELECT legacy_catalog_id
        FROM {LEGACY_SERVICE_BRIDGE_TABLE}
        WHERE soldium_service_id = ?
        LIMIT 1
        """,
        (soldium_service_id,),
    ).fetchone()
    if not row:
        return None
    legacy_id = str(row["legacy_catalog_id"] or "").strip()
    return legacy_id or None


def _require_legacy_row(conn: sqlite3.Connection, legacy_catalog_id: str) -> None:
    if not _table_exists(conn, SMM_TABLE):
        raise CatalogConflictError(
            "جدول smm_services غير موجود — تعذرت مزامنة الخدمة التقليدية"
        )
    cols = _smm_columns(conn)
    if "catalog_id" not in cols:
        raise CatalogConflictError(
            "عمود catalog_id مفقود في smm_services — تعذرت المزامنة"
        )
    row = conn.execute(
        f"SELECT 1 FROM {SMM_TABLE} WHERE catalog_id = ? LIMIT 1",
        (legacy_catalog_id,),
    ).fetchone()
    if row is None:
        raise CatalogConflictError(
            "الخدمة مربوطة بالجسر التقليدي لكن صف smm_services غير موجود "
            f"(catalog_id={legacy_catalog_id}). لم تُحفظ تغييرات الكتالوج."
        )


def write_through_legacy_fields(
    conn: sqlite3.Connection,
    soldium_service_id: str,
    *,
    name_ar: str | None = None,
    min_quantity: int | None = None,
    max_quantity: int | None = None,
    local_price_dh: float | None = None,
    provider_slug: str | None = None,
    provider_account_key: str | None = None,
    external_service_id: str | None = None,
    fulfillment_mode: str | None = None,
    catalog_status: str | None = None,
) -> WriteThroughResult:
    """Apply Catalog field changes onto the bridged ``smm_services`` row.

    Only columns explicitly passed (not None) are updated.
    Missing bridge → skip (Catalog-only service).
    Bridge present but legacy row missing → raise (forces transaction rollback).
    """
    sid = str(soldium_service_id or "").strip()
    if not sid:
        raise CatalogConflictError("معرّف خدمة الكتالوج مطلوب للمزامنة")

    sets: list[str] = []
    params: list[Any] = []
    columns: list[str] = []

    if name_ar is not None:
        sets.append("name_ar = ?")
        params.append(str(name_ar)[:500])
        columns.append("name_ar")
    if min_quantity is not None:
        sets.append("min_qty = ?")
        params.append(int(min_quantity))
        columns.append("min_qty")
    if max_quantity is not None:
        sets.append("max_qty = ?")
        params.append(int(max_quantity))
        columns.append("max_qty")
    if local_price_dh is not None:
        sets.append("local_price_dh = ?")
        params.append(float(local_price_dh))
        columns.append("local_price_dh")
    if provider_slug is not None:
        sets.append("provider_slug = ?")
        params.append(str(provider_slug).strip().lower())
        columns.append("provider_slug")
    if provider_account_key is not None:
        # Catalog execution source is authoritative — never invent via discovery.
        sets.append("provider_api_account = ?")
        params.append(str(provider_account_key).strip())
        columns.append("provider_api_account")
    if external_service_id is not None:
        sets.append("external_service_id = ?")
        params.append(str(external_service_id).strip())
        columns.append("external_service_id")
    if fulfillment_mode is not None:
        mode = str(fulfillment_mode).strip().lower()
        if mode not in {"auto", "admin"}:
            mode = "auto"
        sets.append("fulfillment_mode = ?")
        params.append(mode)
        columns.append("fulfillment_mode")
    if catalog_status is not None:
        sets.append("is_active = ?")
        params.append(catalog_status_to_is_active(catalog_status))
        columns.append("is_active")

    if not sets:
        return WriteThroughResult(
            outcome="skipped_no_changes",
            soldium_service_id=sid,
            message="لا توجد حقول للمزامنة",
        )

    legacy_id = resolve_legacy_catalog_id(conn, sid)
    if legacy_id is None:
        logger.info(
            "legacy_write_through skip: no bridge for %s (fields=%s)",
            sid,
            columns,
        )
        return WriteThroughResult(
            outcome="skipped_no_bridge",
            soldium_service_id=sid,
            message="لا يوجد مقابل تقليدي — الخدمة تبقى في الكتالوج فقط",
        )

    _require_legacy_row(conn, legacy_id)

    cols = _smm_columns(conn)
    missing = [c for c in columns if c not in cols]
    if missing:
        raise CatalogConflictError(
            "أعمدة smm_services مفقودة للمزامنة: " + ", ".join(missing)
        )

    params.append(legacy_id)
    try:
        cur = conn.execute(
            f"UPDATE {SMM_TABLE} SET {', '.join(sets)} WHERE catalog_id = ?",
            params,
        )
    except sqlite3.IntegrityError as exc:
        # Only map the expected provider+external unique conflict; re-raise others.
        touching_routing = provider_slug is not None or external_service_id is not None
        if touching_routing and is_legacy_provider_external_unique_violation(exc):
            raise CatalogConflictError(LEGACY_PROVIDER_EXTERNAL_UNIQUE_MESSAGE) from exc
        raise
    if int(cur.rowcount or 0) < 1:
        raise CatalogConflictError(
            "فشلت مزامنة smm_services (لم يُحدَّث أي صف) — "
            f"catalog_id={legacy_id}"
        )

    logger.info(
        "legacy_write_through applied service=%s legacy=%s columns=%s",
        sid,
        legacy_id,
        columns,
    )
    return WriteThroughResult(
        outcome="applied",
        soldium_service_id=sid,
        legacy_catalog_id=legacy_id,
        columns_updated=tuple(columns),
        message="تمت مزامنة الخدمة التقليدية",
    )


def write_through_active_price(
    conn: sqlite3.Connection,
    soldium_service_id: str,
    *,
    amount_millimes: int,
) -> WriteThroughResult:
    """Sync Catalog active price millimes → ``smm_services.local_price_dh``."""
    # Validate round-trip stays within Catalog millime precision.
    _ = millimes_to_dh_decimal(int(amount_millimes))
    return write_through_legacy_fields(
        conn,
        soldium_service_id,
        local_price_dh=millimes_to_local_price_dh(int(amount_millimes)),
    )


def write_through_execution_source(
    conn: sqlite3.Connection,
    soldium_service_id: str,
    *,
    provider_slug: str,
    provider_account_key: str,
    external_service_id: str,
) -> WriteThroughResult:
    """Sync Catalog active execution source → Legacy routing columns.

    Account comes only from Catalog execution source (no inventory lookup).
    """
    return write_through_legacy_fields(
        conn,
        soldium_service_id,
        provider_slug=provider_slug,
        provider_account_key=provider_account_key,
        external_service_id=external_service_id,
    )
