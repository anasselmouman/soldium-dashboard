"""Scheduled / recurring order jobs for the admin dashboard."""
from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import aiosqlite

from admin_log import logger
from config import ADMIN_TELEGRAM_ID
from database_connector import (
    DatabaseLockedError,
    DatabaseWriteError,
    db_transaction,
    get_db,
)
from manual_orders import _lookup_service_provider_meta
from services.provider_registry import get_default_provider_slug
from services.smm_provider import ProviderUnavailableError, submit_provider_order
from smm_services import get_service
from utils.money import to_float
from utils.order_economics import compute_provider_cost_dh
from utils.active_link_guard import (
    ACTIVE_LINK_OCCUPIED_MESSAGE,
    ActiveLinkOccupiedError,
    find_active_order_for_link_async,
    is_active_link_order_status,
    is_active_link_unique_violation,
    normalize_order_link,
)

_log = logging.getLogger("soldium.scheduled_orders")

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_STOPPED = "stopped"

QUANTITY_FIXED = "fixed"
QUANTITY_RANGE = "range"

RUN_SUCCESS = "success"
RUN_FAILED = "failed"

POLL_INTERVAL_SECONDS = 60
MAX_CONSECUTIVE_FAILURES = 3


class ScheduledOrderValidationError(Exception):
    pass


class ScheduledOrderNotFoundError(Exception):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_str() -> str:
    return _utc_now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _advance_next_run(interval_days: int, *, from_dt: datetime | None = None) -> str:
    base = from_dt or _utc_now()
    days = max(1, int(interval_days))
    return _format_dt(base + timedelta(days=days))


def _quantity_label(job: dict[str, Any]) -> str:
    mode = str(job.get("quantity_mode") or QUANTITY_FIXED)
    if mode == QUANTITY_RANGE:
        return f"{job.get('quantity_min')}–{job.get('quantity_max')} (عشوائي)"
    return str(job.get("quantity_fixed") or "—")


def _status_label(status: str) -> str:
    labels = {
        STATUS_ACTIVE: "نشط",
        STATUS_PAUSED: "موقوف مؤقتاً",
        STATUS_STOPPED: "متوقف",
    }
    return labels.get(str(status), str(status))


def _row_to_job(row: aiosqlite.Row) -> dict[str, Any]:
    keys = set(row.keys())
    external_id = None
    if "external_service_id" in keys and row["external_service_id"] is not None:
        text = str(row["external_service_id"]).strip()
        external_id = text or None
    return {
        "id": int(row["id"]),
        "name": str(row["name"]) if row["name"] else None,
        "template_order_id": (
            int(row["template_order_id"]) if row["template_order_id"] is not None else None
        ),
        "user_id": int(row["user_id"]),
        "service_id": str(row["service_id"] or ""),
        "service_name": str(row["service_name"] or ""),
        "link": str(row["link"] or ""),
        "provider_slug": str(row["provider_slug"] or get_default_provider_slug()),
        "api_account": str(row["api_account"] or "default"),
        "fulfillment_mode": str(row["fulfillment_mode"] or "auto"),
        "external_service_id": external_id,
        "execution_generation": "gen1" if external_id else "gen0",
        "quantity_mode": str(row["quantity_mode"] or QUANTITY_FIXED),
        "quantity_fixed": (
            int(row["quantity_fixed"]) if row["quantity_fixed"] is not None else None
        ),
        "quantity_min": int(row["quantity_min"]) if row["quantity_min"] is not None else None,
        "quantity_max": int(row["quantity_max"]) if row["quantity_max"] is not None else None,
        "quantity_label": _quantity_label(dict(row)),
        "interval_days": int(row["interval_days"] or 1),
        "next_run_at": str(row["next_run_at"]),
        "last_run_at": str(row["last_run_at"]) if row["last_run_at"] else None,
        "last_created_order_id": (
            int(row["last_created_order_id"])
            if row["last_created_order_id"] is not None
            else None
        ),
        "runs_count": int(row["runs_count"] or 0),
        "consecutive_failures": int(row["consecutive_failures"] or 0),
        "status": str(row["status"] or STATUS_ACTIVE),
        "status_label": _status_label(str(row["status"] or STATUS_ACTIVE)),
        "created_at": str(row["created_at"]),
        "stopped_at": str(row["stopped_at"]) if row["stopped_at"] else None,
    }


async def _get_template_order(order_id: int) -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(
            f"{_TEMPLATE_ORDER_SELECT} WHERE o.id = ? LIMIT 1",
            (int(order_id),),
        ) as cursor:
            row = await cursor.fetchone()
    return _template_from_row(row) if row else None


def _template_from_row(row: aiosqlite.Row) -> dict[str, Any]:
    keys = set(row.keys())
    snap = None
    if "external_service_id_snapshot" in keys and row["external_service_id_snapshot"]:
        snap = str(row["external_service_id_snapshot"]).strip() or None
    catalog_id = None
    if "catalog_id" in keys and row["catalog_id"]:
        catalog_id = str(row["catalog_id"]).strip() or None
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "service_name": str(row["service_name"] or ""),
        "service_id": str(row["service_id"] or ""),
        "link": str(row["link"] or ""),
        "quantity": int(row["quantity"]),
        "amount": float(row["amount"] or 0.0),
        "provider_order_id": (
            str(row["provider_order_id"]) if row["provider_order_id"] else None
        ),
        "fulfillment_mode": str(row["fulfillment_mode"] or "auto"),
        "api_account": str(row["api_account"] or "default"),
        "provider_slug": str(row["provider_slug"] or get_default_provider_slug()),
        "external_service_id_snapshot": snap,
        "catalog_id": catalog_id,
    }


_TEMPLATE_ORDER_SELECT = """
    SELECT
        o.id,
        o.user_id,
        o.service_name,
        o.service_id,
        o.link,
        o.quantity,
        o.amount,
        o.provider_order_id,
        COALESCE(o.fulfillment_mode, 'auto') AS fulfillment_mode,
        COALESCE(o.api_account, 'default') AS api_account,
        COALESCE(o.provider_slug, 'gozibra') AS provider_slug,
        o.catalog_id,
        o.external_service_id_snapshot
    FROM orders o
"""


async def resolve_template_order(
    reference: str,
    *,
    _allow_internal_only: bool = False,
) -> dict[str, Any]:
    """Find order by provider ref (preferred) or internal id."""
    ref = str(reference or "").strip()
    if not ref:
        raise ScheduledOrderValidationError(
            "أدخل معرف المزوّد أو رقم الطلب الداخلي.",
        )

    async with get_db() as db:
        async with db.execute(
            f"{_TEMPLATE_ORDER_SELECT} WHERE o.provider_order_id = ? LIMIT 1",
            (ref,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is not None:
            return _template_from_row(row)

        if ref.isdigit():
            async with db.execute(
                f"{_TEMPLATE_ORDER_SELECT} WHERE o.id = ? LIMIT 1",
                (int(ref),),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                return _template_from_row(row)

        if not _allow_internal_only:
            like = f"%{ref}%"
            async with db.execute(
                f"""
                {_TEMPLATE_ORDER_SELECT}
                WHERE o.provider_order_id LIKE ?
                ORDER BY o.id DESC
                LIMIT 5
                """,
                (like,),
            ) as cursor:
                rows = await cursor.fetchall()
            if len(rows) == 1:
                return _template_from_row(rows[0])
            if len(rows) > 1:
                raise ScheduledOrderValidationError(
                    "عدة طلبات تطابق البحث — اختر واحداً من نتائج البحث.",
                )

    raise ScheduledOrderValidationError(
        "لم يُعثر على طلب بهذا المعرف. ابحث من قائمة الطلبات أو انسخ معرف المزوّد بالضبط.",
    )


async def search_template_orders(query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    from orders import list_orders

    q = str(query or "").strip()
    if not q:
        return []
    data = await list_orders(page=1, limit=max(1, min(limit, 20)), search=q)
    results: list[dict[str, Any]] = []
    for order in data.get("orders") or []:
        results.append(
            {
                "id": int(order["id"]),
                "provider_order_id": order.get("provider_order_id"),
                "service_name": str(order.get("service_name") or ""),
                "link": str(order.get("link") or ""),
                "quantity": int(order.get("quantity") or 0),
                "amount": float(order.get("amount") or 0.0),
                "status": str(order.get("status") or ""),
                "created_at": str(order.get("created_at") or ""),
            }
        )
    return results


async def _admin_user_exists(user_id: int) -> bool:
    async with get_db() as db:
        async with db.execute(
            "SELECT 1 FROM users WHERE user_id = ? LIMIT 1",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return row is not None


async def _lookup_service_limits(service_id: str) -> tuple[int, int] | None:
    item_id = str(service_id or "").strip()
    if not item_id:
        return None
    async with get_db() as db:
        try:
            async with db.execute(
                """
                SELECT min_qty, max_qty
                FROM smm_services
                WHERE catalog_id = ? OR local_item_id = ? OR service_id = ?
                LIMIT 1
                """,
                (item_id, item_id, item_id),
            ) as cursor:
                row = await cursor.fetchone()
        except sqlite3.OperationalError:
            return None
    if row is None:
        return None
    return int(row["min_qty"] or 1), int(row["max_qty"] or 1_000_000)


def _validate_quantity_settings(
    *,
    quantity_mode: str,
    quantity_fixed: int | None,
    quantity_min: int | None,
    quantity_max: int | None,
    service_id: str,
    limits: tuple[int, int] | None,
) -> None:
    mode = str(quantity_mode or QUANTITY_FIXED).strip().lower()
    if mode not in {QUANTITY_FIXED, QUANTITY_RANGE}:
        raise ScheduledOrderValidationError("نوع الكمية غير صالح.")

    def _check_qty(qty: int) -> None:
        if qty <= 0:
            raise ScheduledOrderValidationError("الكمية يجب أن تكون أكبر من صفر.")
        if limits:
            lo, hi = limits
            if qty < lo or qty > hi:
                raise ScheduledOrderValidationError(
                    f"الكمية {qty} خارج حدود الخدمة ({lo}–{hi}).",
                )

    if mode == QUANTITY_FIXED:
        if quantity_fixed is None:
            raise ScheduledOrderValidationError("حدّد الكمية الثابتة.")
        _check_qty(int(quantity_fixed))
        return

    if quantity_min is None or quantity_max is None:
        raise ScheduledOrderValidationError("حدّد الحد الأدنى والأقصى للكمية.")
    qmin = int(quantity_min)
    qmax = int(quantity_max)
    if qmin > qmax:
        raise ScheduledOrderValidationError("الحد الأدنى أكبر من الحد الأقصى.")
    _check_qty(qmin)
    _check_qty(qmax)


def _resolve_quantity(job: dict[str, Any]) -> int:
    mode = str(job.get("quantity_mode") or QUANTITY_FIXED)
    if mode == QUANTITY_RANGE:
        qmin = int(job["quantity_min"])
        qmax = int(job["quantity_max"])
        return random.randint(qmin, qmax)
    return int(job["quantity_fixed"])


def _order_total_price_dh(
    *,
    local_price_dh: float,
    quantity: int,
    price_per_unit: bool,
) -> float:
    price = Decimal(str(local_price_dh))
    qty = Decimal(str(max(int(quantity), 1)))
    if price_per_unit:
        return float(price * qty)
    return float(price * qty / Decimal(1000))


async def _compute_order_pricing(service_id: str, quantity: int) -> tuple[float, float]:
    """Return (amount_dh, provider_cost_dh)."""
    svc = await get_service(service_id)
    if svc is None:
        raise ScheduledOrderValidationError("الخدمة غير موجودة في الكتالوج.")

    local_price = float(svc.get("local_price_dh") or 0)
    if local_price <= 0:
        raise ScheduledOrderValidationError("سعر الخدمة غير مضبوط في الكتالوج.")

    price_per_unit = bool(svc.get("price_per_unit"))
    amount = _order_total_price_dh(
        local_price_dh=local_price,
        quantity=quantity,
        price_per_unit=price_per_unit,
    )
    cost = compute_provider_cost_dh(
        int(quantity),
        provider_price_usd=float(svc.get("provider_price_usd") or 0),
        local_price_dh=local_price,
        price_per_unit=price_per_unit,
    )
    return amount, cost


async def _resolve_external_service_id_at_create(
    template: dict[str, Any],
) -> str:
    """Freeze execution external ID once at schedule create (Gen-1).

    Prefer template order snapshot; otherwise one-time live lookup.
    Never invent or coerce to 0.
    """
    from utils.order_execution_identity import (
        InvalidProviderExternalServiceId,
        encode_provider_external_service_id_for_wire,
    )

    snap = str(template.get("external_service_id_snapshot") or "").strip()
    if snap:
        try:
            return encode_provider_external_service_id_for_wire(snap)
        except InvalidProviderExternalServiceId as exc:
            raise ScheduledOrderValidationError(
                f"معرّف التنفيذ المجمّد في الطلب المرجعي غير صالح: {exc}"
            ) from exc

    meta = await _lookup_service_provider_meta(str(template.get("service_id") or ""))
    if meta is None:
        raise ScheduledOrderValidationError(
            "تعذّر تجميد معرّف تنفيذ المزوّد لهذه المهمة. "
            "الطلب المرجعي بلا لقطة تنفيذ والخدمة غير قابلة للربط."
        )
    external = str(meta["external_service_id"]).strip()
    try:
        return encode_provider_external_service_id_for_wire(external)
    except InvalidProviderExternalServiceId as exc:
        raise ScheduledOrderValidationError(str(exc)) from exc


def _assert_frozen_execution_identity(
    *,
    provider_slug: str,
    api_account: str,
    external_service_id: str,
) -> str:
    """Validate Gen-1 frozen triple; return wire-ready external id. Fail closed."""
    from utils.order_execution_identity import (
        InvalidProviderExternalServiceId,
        encode_provider_external_service_id_for_wire,
    )

    slug = str(provider_slug or "").strip().lower()
    account = str(api_account or "").strip().lower() or "default"
    external = str(external_service_id or "").strip()
    if not slug:
        raise ScheduledOrderValidationError("معرّف المزوّد المجمّد مفقود.")
    if not account:
        raise ScheduledOrderValidationError("حساب المزوّد المجمّد مفقود.")
    if not external:
        raise ScheduledOrderValidationError("معرّف تنفيذ المزوّد المجمّد مفقود.")
    try:
        return encode_provider_external_service_id_for_wire(external)
    except InvalidProviderExternalServiceId as exc:
        raise ScheduledOrderValidationError(str(exc)) from exc


def _assert_provider_account_usable(*, provider_slug: str, api_account: str) -> None:
    """Fail closed if frozen provider/account cannot submit."""
    from services.provider_registry import (
        get_provider_account_record,
        get_provider_record,
        resolve_api_key,
    )

    slug = str(provider_slug or "").strip().lower()
    account = str(api_account or "").strip().lower() or "default"
    provider = get_provider_record(slug)
    if provider is None or not getattr(provider, "is_active", True):
        raise ScheduledOrderValidationError(
            f"المزوّد المجمّد غير متاح: {slug}"
        )
    acct = get_provider_account_record(slug, account)
    if acct is None:
        raise ScheduledOrderValidationError(
            f"حساب المزوّد المجمّد غير موجود: {slug}/{account}"
        )
    try:
        key = resolve_api_key(slug, account)
    except Exception as exc:  # noqa: BLE001 — surface as schedule failure
        raise ScheduledOrderValidationError(
            f"تعذّر استخدام حساب المزوّد المجمّد: {slug}/{account}"
        ) from exc
    if not str(key or "").strip():
        raise ScheduledOrderValidationError(
            f"مفتاح API لحساب المزوّد المجمّد غير مضبوط: {slug}/{account}"
        )


async def _create_order_with_balance_hold(
    *,
    user_id: int,
    service_name: str,
    service_id: str,
    link: str,
    quantity: int,
    amount: float,
    api_account: str,
    provider_slug: str,
    initial_status: str,
    fulfillment_mode: str,
    provider_cost_dh: float,
    catalog_id: str | None = None,
    external_service_id_snapshot: str | None = None,
) -> int:
    amount_money = round(to_float(amount), 6)
    account = str(api_account or "default").strip() or "default"
    slug = str(provider_slug or get_default_provider_slug()).strip().lower()
    status = str(initial_status or "pending").strip() or "pending"
    mode = str(fulfillment_mode or "auto").strip().lower() or "auto"
    if mode not in {"auto", "admin"}:
        mode = "auto"
    legacy_catalog_id = str(catalog_id or "").strip() or None
    external_snap = str(external_service_id_snapshot or "").strip() or None
    stored_link = str(link or "")
    normalized = normalize_order_link(stored_link)
    normalized_for_db = normalized or None

    try:
        async with db_transaction() as db:
            async with db.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (user_id,),
            ) as cursor:
                user_row = await cursor.fetchone()
            if user_row is None:
                raise ScheduledOrderValidationError("حساب الأدمن غير موجود في قاعدة البيانات.")

            balance = float(user_row["balance"] or 0.0)
            if balance < amount_money:
                raise ScheduledOrderValidationError(
                    f"رصيد حساب الأدمن ({user_id}) غير كافٍ: {balance:.2f} DH مطلوب {amount_money:.2f} DH.",
                )

            if normalized_for_db and is_active_link_order_status(status):
                occupied = await find_active_order_for_link_async(db, stored_link)
                if occupied is not None:
                    raise ActiveLinkOccupiedError(
                        existing_order_id=int(occupied["id"]),
                        normalized_link=normalized_for_db,
                    )

            balance_cursor = await db.execute(
                """
                UPDATE users
                SET
                    balance = ROUND(balance - ?, 6),
                    total_spent = ROUND(total_spent + ?, 6)
                WHERE user_id = ? AND balance >= ?
                """,
                (amount_money, amount_money, user_id, amount_money),
            )
            if balance_cursor.rowcount == 0:
                raise ScheduledOrderValidationError("تعذّر خصم الرصيد — رصيد غير كافٍ.")

            try:
                order_cursor = await db.execute(
                    """
                    INSERT INTO orders (
                        user_id, service_name, service_id, link, quantity, amount, total_price, status,
                        api_account, provider_slug, fulfillment_mode, provider_cost_dh,
                        catalog_id, external_service_id_snapshot, normalized_link
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        service_name,
                        service_id,
                        stored_link,
                        int(quantity),
                        amount_money,
                        amount_money,
                        status,
                        account,
                        slug,
                        mode,
                        round(to_float(provider_cost_dh), 6),
                        legacy_catalog_id,
                        external_snap,
                        normalized_for_db,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if normalized_for_db and is_active_link_unique_violation(exc):
                    raise ActiveLinkOccupiedError(
                        normalized_link=normalized_for_db,
                    ) from exc
                raise
            return int(order_cursor.lastrowid)
    except ActiveLinkOccupiedError as exc:
        raise ScheduledOrderValidationError(ACTIVE_LINK_OCCUPIED_MESSAGE) from exc


async def _persist_provider_order_id(order_id: int, provider_ref: str) -> None:
    ref = str(provider_ref or "").strip()
    if not ref:
        return
    async with db_transaction() as db:
        await db.execute(
            "UPDATE orders SET provider_order_id = ? WHERE id = ?",
            (ref, order_id),
        )


async def _submit_job_order(job: dict[str, Any], quantity: int) -> int:
    """Materialize a scheduled job into an order.

    Phase 9B.8 / 9O:
      Gen-1 (frozen external_service_id on job): use frozen execution identity;
      never re-lookup smm_services for the Provider SKU.
      Gen-0 (NULL external_service_id): fail closed — no live Legacy SKU rescue.

    Retail price may still be computed live (independent of execution identity).
    """
    from utils.order_execution_identity import (
        GEN0_EXECUTION_IDENTITY_MISSING,
        is_gen1_scheduled_job,
    )

    service_id = str(job["service_id"])
    link = str(job["link"])
    amount, provider_cost = await _compute_order_pricing(service_id, quantity)

    fulfillment = str(job.get("fulfillment_mode") or "auto").strip().lower() or "auto"
    if fulfillment not in {"auto", "admin"}:
        fulfillment = "auto"
    requires_admin = fulfillment == "admin"
    initial_status = "pending_admin" if requires_admin else "pending"

    svc = await get_service(service_id)
    legacy_catalog_id = None
    if svc is not None:
        legacy_catalog_id = str(svc.get("catalog_id") or "").strip() or None

    if not is_gen1_scheduled_job(job):
        # Phase 9O: Gen-0 must never resolve Provider SKU from live smm_services.
        logger.warning(
            "%s scheduled_job_id=%s service_id=%s gen=gen0",
            GEN0_EXECUTION_IDENTITY_MISSING,
            job.get("id"),
            service_id,
        )
        raise ScheduledOrderValidationError(
            "تعذّر تنفيذ المهمة المجدولة: معرّف تنفيذ المزوّد المجمّد مفقود "
            f"({GEN0_EXECUTION_IDENTITY_MISSING})."
        )

    provider_slug = str(job.get("provider_slug") or "").strip().lower()
    account = str(job.get("api_account") or "").strip().lower() or "default"
    external_text = str(job.get("external_service_id") or "").strip()
    wire_service = _assert_frozen_execution_identity(
        provider_slug=provider_slug,
        api_account=account,
        external_service_id=external_text,
    )
    _assert_provider_account_usable(
        provider_slug=provider_slug, api_account=account
    )

    order_id = await _create_order_with_balance_hold(
        user_id=int(job["user_id"]),
        service_name=str(job["service_name"]),
        service_id=service_id,
        link=link,
        quantity=quantity,
        amount=amount,
        api_account=account,
        provider_slug=provider_slug,
        initial_status=initial_status,
        fulfillment_mode=fulfillment,
        provider_cost_dh=provider_cost,
        catalog_id=legacy_catalog_id,
        external_service_id_snapshot=external_text,
    )

    provider_ref = await submit_provider_order(
        provider_slug=provider_slug,
        account_key=account,
        service_id=wire_service,
        link=link,
        quantity=quantity,
    )
    await _persist_provider_order_id(order_id, provider_ref)

    logger.info(
        "SCHEDULED_ORDER executed job_id=%s order_id=%s qty=%s provider_ref=%s gen=gen1",
        job["id"],
        order_id,
        quantity,
        provider_ref,
    )
    return order_id


async def _record_run(
    *,
    scheduled_order_id: int,
    order_id: int | None,
    quantity_used: int | None,
    status: str,
    error_message: str | None = None,
) -> None:
    async with db_transaction() as db:
        await db.execute(
            """
            INSERT INTO scheduled_order_runs (
                scheduled_order_id, order_id, quantity_used, status, error_message
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                scheduled_order_id,
                order_id,
                quantity_used,
                status,
                (error_message or "")[:500] or None,
            ),
        )


async def _mark_run_success(job_id: int, *, order_id: int, quantity: int) -> None:
    async with db_transaction() as db:
        async with db.execute(
            "SELECT interval_days FROM scheduled_orders WHERE id = ?",
            (job_id,),
        ) as cursor:
            row = await cursor.fetchone()
        interval_days = int(row["interval_days"]) if row else 1
        now = _utc_now_str()
        next_run = _advance_next_run(interval_days)
        await db.execute(
            """
            UPDATE scheduled_orders
            SET
                last_run_at = ?,
                last_created_order_id = ?,
                runs_count = runs_count + 1,
                consecutive_failures = 0,
                next_run_at = ?
            WHERE id = ?
            """,
            (now, order_id, next_run, job_id),
        )


async def _mark_run_failure(job_id: int, *, error_message: str) -> None:
    async with db_transaction() as db:
        async with db.execute(
            "SELECT interval_days, consecutive_failures FROM scheduled_orders WHERE id = ?",
            (job_id,),
        ) as cursor:
            row = await cursor.fetchone()
        interval_days = int(row["interval_days"]) if row else 1
        failures = int(row["consecutive_failures"] or 0) + 1 if row else 1
        now = _utc_now_str()
        next_run = _advance_next_run(interval_days)
        new_status = STATUS_PAUSED if failures >= MAX_CONSECUTIVE_FAILURES else STATUS_ACTIVE
        stopped_at = now if new_status == STATUS_PAUSED else None
        await db.execute(
            """
            UPDATE scheduled_orders
            SET
                last_run_at = ?,
                consecutive_failures = ?,
                next_run_at = ?,
                status = ?,
                stopped_at = COALESCE(?, stopped_at)
            WHERE id = ?
            """,
            (now, failures, next_run, new_status, stopped_at, job_id),
        )


async def get_scheduled_order(job_id: int) -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM scheduled_orders WHERE id = ?",
            (job_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_job(row) if row else None


async def list_scheduled_orders(*, status: str | None = None) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status and status.strip():
        clauses.append("status = ?")
        params.append(status.strip().lower())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    async with get_db() as db:
        async with db.execute(
            f"""
            SELECT * FROM scheduled_orders
            {where}
            ORDER BY
                CASE status
                    WHEN 'active' THEN 0
                    WHEN 'paused' THEN 1
                    ELSE 2
                END,
                next_run_at ASC
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_job(row) for row in rows]


async def list_scheduled_order_runs(
    job_id: int,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    async with get_db() as db:
        async with db.execute(
            """
            SELECT id, scheduled_order_id, order_id, quantity_used, status, error_message, ran_at
            FROM scheduled_order_runs
            WHERE scheduled_order_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (job_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "scheduled_order_id": int(row["scheduled_order_id"]),
            "order_id": int(row["order_id"]) if row["order_id"] is not None else None,
            "quantity_used": (
                int(row["quantity_used"]) if row["quantity_used"] is not None else None
            ),
            "status": str(row["status"]),
            "error_message": str(row["error_message"]) if row["error_message"] else None,
            "ran_at": str(row["ran_at"]),
        }
        for row in rows
    ]


async def create_scheduled_order(
    *,
    template_order_ref: str | None = None,
    template_order_id: int | None = None,
    quantity_mode: str,
    quantity_fixed: int | None = None,
    quantity_min: int | None = None,
    quantity_max: int | None = None,
    interval_days: int = 1,
    first_run_at: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    if ADMIN_TELEGRAM_ID <= 0:
        raise ScheduledOrderValidationError(
            "لم يُضبط ADMIN_ID في ملف البيئة — مطلوب لتنفيذ الطلبات المجدولة.",
        )

    if not await _admin_user_exists(ADMIN_TELEGRAM_ID):
        raise ScheduledOrderValidationError(
            f"حساب الأدمن ({ADMIN_TELEGRAM_ID}) غير موجود — سجّل الدخول للبوت أولاً.",
        )

    ref = str(template_order_ref or "").strip()
    if ref:
        template = await resolve_template_order(ref)
    elif template_order_id is not None:
        template = await _get_template_order(int(template_order_id))
        if template is None:
            raise ScheduledOrderValidationError("الطلب المرجعي غير موجود.")
    else:
        raise ScheduledOrderValidationError("اختر طلباً مرجعياً من البحث.")

    resolved_order_id = int(template["id"])

    if not str(template.get("link") or "").strip():
        raise ScheduledOrderValidationError("الطلب المرجعي لا يحتوي على رابط.")

    days = max(1, int(interval_days))
    limits = await _lookup_service_limits(str(template["service_id"]))
    _validate_quantity_settings(
        quantity_mode=quantity_mode,
        quantity_fixed=quantity_fixed,
        quantity_min=quantity_min,
        quantity_max=quantity_max,
        service_id=str(template["service_id"]),
        limits=limits,
    )

    if first_run_at and str(first_run_at).strip():
        first_dt = _parse_dt(first_run_at)
        if first_dt is None:
            raise ScheduledOrderValidationError("صيغة وقت التنفيذ الأول غير صالحة.")
        if first_dt <= _utc_now():
            raise ScheduledOrderValidationError("وقت التنفيذ الأول يجب أن يكون في المستقبل.")
        next_run = _format_dt(first_dt)
    else:
        next_run = _utc_now_str()

    label = (name or "").strip() or None

    frozen_external = await _resolve_external_service_id_at_create(template)
    fulfillment = str(template["fulfillment_mode"] or "auto").strip().lower() or "auto"
    if fulfillment not in {"auto", "admin"}:
        raise ScheduledOrderValidationError("وضع التنفيذ يجب أن يكون auto أو admin.")
    provider_slug = str(template["provider_slug"] or get_default_provider_slug()).strip().lower()
    api_account = str(template["api_account"] or "default").strip().lower() or "default"
    _assert_provider_account_usable(
        provider_slug=provider_slug, api_account=api_account
    )

    async with db_transaction() as db:
        cursor = await db.execute(
            """
            INSERT INTO scheduled_orders (
                name, template_order_id, user_id, service_id, service_name, link,
                provider_slug, api_account, fulfillment_mode, external_service_id,
                quantity_mode, quantity_fixed, quantity_min, quantity_max,
                interval_days, next_run_at, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                label,
                resolved_order_id,
                ADMIN_TELEGRAM_ID,
                str(template["service_id"]),
                str(template["service_name"]),
                str(template["link"]),
                provider_slug,
                api_account,
                fulfillment,
                frozen_external,
                str(quantity_mode).strip().lower(),
                quantity_fixed,
                quantity_min,
                quantity_max,
                days,
                next_run,
                STATUS_ACTIVE,
            ),
        )
        job_id = int(cursor.lastrowid)

    job = await get_scheduled_order(job_id)
    assert job is not None
    logger.info(
        "SCHEDULED_ORDER created id=%s template=%s interval_days=%s qty_mode=%s gen=gen1 external=%s",
        job_id,
        resolved_order_id,
        days,
        quantity_mode,
        frozen_external,
    )
    return job


async def pause_scheduled_order(job_id: int) -> dict[str, Any]:
    job = await get_scheduled_order(job_id)
    if job is None:
        raise ScheduledOrderNotFoundError("المهمة المجدولة غير موجودة.")
    if job["status"] == STATUS_STOPPED:
        raise ScheduledOrderValidationError("المهمة متوقفة نهائياً.")
    async with db_transaction() as db:
        await db.execute(
            """
            UPDATE scheduled_orders
            SET status = ?, stopped_at = ?
            WHERE id = ?
            """,
            (STATUS_PAUSED, _utc_now_str(), job_id),
        )
    updated = await get_scheduled_order(job_id)
    assert updated is not None
    return updated


async def resume_scheduled_order(job_id: int) -> dict[str, Any]:
    job = await get_scheduled_order(job_id)
    if job is None:
        raise ScheduledOrderNotFoundError("المهمة المجدولة غير موجودة.")
    if job["status"] == STATUS_STOPPED:
        raise ScheduledOrderValidationError("المهمة محذوفة/متوقفة — أنشئ مهمة جديدة.")
    next_run = job["next_run_at"]
    parsed = _parse_dt(next_run)
    if parsed is None or parsed <= _utc_now():
        next_run = _utc_now_str()
    async with db_transaction() as db:
        await db.execute(
            """
            UPDATE scheduled_orders
            SET status = ?, consecutive_failures = 0, next_run_at = ?, stopped_at = NULL
            WHERE id = ?
            """,
            (STATUS_ACTIVE, next_run, job_id),
        )
    updated = await get_scheduled_order(job_id)
    assert updated is not None
    return updated


async def stop_scheduled_order(job_id: int) -> dict[str, Any]:
    job = await get_scheduled_order(job_id)
    if job is None:
        raise ScheduledOrderNotFoundError("المهمة المجدولة غير موجودة.")
    async with db_transaction() as db:
        await db.execute(
            """
            UPDATE scheduled_orders
            SET status = ?, stopped_at = ?
            WHERE id = ?
            """,
            (STATUS_STOPPED, _utc_now_str(), job_id),
        )
    updated = await get_scheduled_order(job_id)
    assert updated is not None
    logger.info("SCHEDULED_ORDER stopped id=%s", job_id)
    return updated


async def delete_scheduled_order(job_id: int) -> None:
    job = await get_scheduled_order(job_id)
    if job is None:
        raise ScheduledOrderNotFoundError("المهمة المجدولة غير موجودة.")
    async with db_transaction() as db:
        await db.execute(
            "DELETE FROM scheduled_order_runs WHERE scheduled_order_id = ?",
            (job_id,),
        )
        await db.execute("DELETE FROM scheduled_orders WHERE id = ?", (job_id,))


async def execute_scheduled_order(job_id: int, *, force: bool = False) -> dict[str, Any]:
    async with get_db() as db:
        if force:
            sql = "SELECT * FROM scheduled_orders WHERE id = ? AND status != ?"
            params: tuple[Any, ...] = (job_id, STATUS_STOPPED)
        else:
            sql = (
                "SELECT * FROM scheduled_orders WHERE id = ? "
                "AND status = ? AND next_run_at <= ?"
            )
            params = (job_id, STATUS_ACTIVE, _utc_now_str())
        async with db.execute(sql, params) as cursor:
            row = await cursor.fetchone()

    if row is None:
        job = await get_scheduled_order(job_id)
        if job is None:
            raise ScheduledOrderNotFoundError("المهمة المجدولة غير موجودة.")
        raise ScheduledOrderValidationError("المهمة غير مستحقة التنفيذ أو غير نشطة.")

    job = _row_to_job(row)
    quantity = _resolve_quantity(job)

    try:
        order_id = await _submit_job_order(job, quantity)
    except Exception as exc:
        message = str(exc) or "خطأ غير معروف"
        await _record_run(
            scheduled_order_id=job_id,
            order_id=None,
            quantity_used=quantity,
            status=RUN_FAILED,
            error_message=message,
        )
        await _mark_run_failure(job_id, error_message=message)
        _log.warning("Scheduled order job_id=%s failed: %s", job_id, message)
        return {
            "ok": False,
            "job_id": job_id,
            "quantity": quantity,
            "error": message,
        }

    await _record_run(
        scheduled_order_id=job_id,
        order_id=order_id,
        quantity_used=quantity,
        status=RUN_SUCCESS,
    )
    await _mark_run_success(job_id, order_id=order_id, quantity=quantity)
    updated = await get_scheduled_order(job_id)
    return {
        "ok": True,
        "job_id": job_id,
        "order_id": order_id,
        "quantity": quantity,
        "job": updated,
    }


async def process_due_scheduled_orders() -> int:
    now = _utc_now_str()
    async with get_db() as db:
        async with db.execute(
            """
            SELECT id FROM scheduled_orders
            WHERE status = ? AND next_run_at <= ?
            ORDER BY next_run_at ASC
            LIMIT 20
            """,
            (STATUS_ACTIVE, now),
        ) as cursor:
            rows = await cursor.fetchall()

    processed = 0
    for row in rows:
        job_id = int(row["id"])
        try:
            await execute_scheduled_order(job_id)
            processed += 1
        except ScheduledOrderValidationError:
            continue
        except (DatabaseLockedError, sqlite3.OperationalError, DatabaseWriteError) as exc:
            _log.warning("Scheduled order job_id=%s db error: %s", job_id, exc)
        except Exception as exc:
            _log.exception("Scheduled order job_id=%s unexpected error", job_id)
    return processed


async def run_scheduled_orders_worker() -> None:
    """Background worker — executes due scheduled orders."""
    interval = max(30, POLL_INTERVAL_SECONDS)
    while True:
        try:
            count = await process_due_scheduled_orders()
            if count:
                _log.info("Scheduled orders worker processed %s job(s)", count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.exception("Scheduled orders worker error: %s", exc)
        await asyncio.sleep(interval)
