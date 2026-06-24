"""Admin dashboard alerts — detection, persistence, and scan loop."""
from __future__ import annotations

import asyncio
import html
import json
import logging
from dataclasses import dataclass
from typing import Any

from config import (
    ALERT_OLD_DEPOSIT_HOURS,
    ALERT_OLD_MANUAL_HOURS,
    ALERT_OLD_WITHDRAWAL_HOURS,
    ALERT_PROVIDER_BALANCE_MIN_USD,
    ALERT_SCAN_INTERVAL_MINUTES,
    ALERT_STALE_PRICES_DAYS,
    ALERT_STUCK_EXECUTION_HOURS,
    ALERT_STUCK_SUBMITTED_HOURS,
    ALERT_TELEGRAM_ON_CRITICAL,
)
from database_connector import get_db
from utils.order_alert_format import (
    ORDER_ALERT_CATALOG_JOIN,
    ORDER_ALERT_SELECT_EXTRA,
    catalog_fields_from_row,
    format_order_alert_messages,
)
from utils.order_ref import display_order_ref
from utils.order_status import normalize_order_status_key, status_label_ar

logger = logging.getLogger("soldium.admin_alerts")

_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
_schema_ensured = False
_schema_lock = asyncio.Lock()


async def ensure_admin_alerts_schema() -> None:
    """Create admin_alerts table if missing (safe before any query)."""
    global _schema_ensured
    if _schema_ensured:
        return
    async with _schema_lock:
        if _schema_ensured:
            return
        from db_schema import ensure_admin_alerts_table

        await ensure_admin_alerts_table()
        _schema_ensured = True


@dataclass(frozen=True)
class AlertCandidate:
    alert_type: str
    severity: str
    entity_type: str
    entity_id: str | None
    title: str
    message: str
    fingerprint: str
    payload: dict[str, Any]


def _row_to_alert(row) -> dict[str, Any]:
    payload: dict[str, Any] | None = None
    raw = row["payload_json"]
    if raw:
        try:
            payload = json.loads(str(raw))
        except json.JSONDecodeError:
            payload = None
    return {
        "id": int(row["id"]),
        "alert_type": str(row["alert_type"]),
        "severity": str(row["severity"]),
        "entity_type": str(row["entity_type"]),
        "entity_id": row["entity_id"],
        "title": str(row["title"]),
        "message": str(row["message"]),
        "message_html": (payload or {}).get("message_html") if payload else None,
        "payload": payload,
        "fingerprint": str(row["fingerprint"]),
        "first_seen_at": str(row["first_seen_at"]),
        "last_seen_at": str(row["last_seen_at"]),
        "status": str(row["status"]),
        "action_url": _action_url_for_alert(
            str(row["alert_type"]),
            str(row["entity_type"]),
            row["entity_id"],
            payload,
        ),
    }


def _action_url_for_alert(
    alert_type: str,
    entity_type: str,
    entity_id: object,
    payload: dict[str, Any] | None,
) -> str | None:
    if entity_type == "order" and entity_id is not None:
        return f"/orders?highlight={entity_id}"
    if alert_type == "old_manual_order":
        return "/manual-orders"
    if alert_type in {"old_deposit", "stale_deposit"}:
        return "/deposits"
    if alert_type in {"old_withdrawal", "stale_withdrawal"}:
        return "/withdrawals"
    if alert_type in {"low_provider_balance", "provider_balance_error"}:
        return "/providers"
    if alert_type == "stale_prices":
        return "/services"
    if payload and payload.get("action_url"):
        return str(payload["action_url"])
    return None


async def list_open_alerts(*, limit: int = 50) -> list[dict[str, Any]]:
    await ensure_admin_alerts_schema()
    async with get_db() as db:
        async with db.execute(
            """
            SELECT id, alert_type, severity, entity_type, entity_id, title, message,
                   payload_json, fingerprint, first_seen_at, last_seen_at, status
            FROM admin_alerts
            WHERE status = 'open'
            ORDER BY
                CASE severity
                    WHEN 'critical' THEN 0
                    WHEN 'warning' THEN 1
                    ELSE 2
                END,
                last_seen_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ) as cursor:
            rows = await cursor.fetchall()
    alerts = [_row_to_alert(row) for row in rows]
    alerts.sort(key=lambda item: (_SEVERITY_ORDER.get(item["severity"], 9), item["last_seen_at"]))
    return alerts


async def count_open_alerts() -> dict[str, int]:
    await ensure_admin_alerts_schema()
    async with get_db() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM admin_alerts WHERE status = 'open'"
        ) as cursor:
            total_row = await cursor.fetchone()
        async with db.execute(
            """
            SELECT COUNT(*) FROM admin_alerts
            WHERE status = 'open' AND severity = 'critical'
            """
        ) as cursor:
            critical_row = await cursor.fetchone()
    return {
        "admin_alerts_count": int(total_row[0] if total_row else 0),
        "admin_alerts_critical": int(critical_row[0] if critical_row else 0),
    }


async def dismiss_alert(alert_id: int) -> bool:
    await ensure_admin_alerts_schema()
    async with get_db() as db:
        cursor = await db.execute(
            """
            UPDATE admin_alerts
            SET status = 'dismissed', dismissed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'open'
            """,
            (alert_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def _upsert_candidates(candidates: list[AlertCandidate]) -> list[AlertCandidate]:
    """Persist alerts; return newly created critical candidates for Telegram."""
    new_critical: list[AlertCandidate] = []
    active_fps = {c.fingerprint for c in candidates}

    async with get_db() as db:
        for candidate in candidates:
            payload_json = json.dumps(candidate.payload, ensure_ascii=False)
            await db.execute(
                """
                INSERT INTO admin_alerts (
                    alert_type, severity, entity_type, entity_id, title, message,
                    payload_json, fingerprint, status, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', CURRENT_TIMESTAMP)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    severity = excluded.severity,
                    title = excluded.title,
                    message = excluded.message,
                    payload_json = excluded.payload_json,
                    entity_id = excluded.entity_id,
                    last_seen_at = CURRENT_TIMESTAMP,
                    status = CASE
                        WHEN admin_alerts.status = 'dismissed' THEN 'dismissed'
                        ELSE 'open'
                    END
                """,
                (
                    candidate.alert_type,
                    candidate.severity,
                    candidate.entity_type,
                    candidate.entity_id,
                    candidate.title,
                    candidate.message,
                    payload_json,
                    candidate.fingerprint,
                ),
            )
            async with db.execute(
                """
                SELECT id, telegram_notified, status
                FROM admin_alerts
                WHERE fingerprint = ?
                """,
                (candidate.fingerprint,),
            ) as cursor:
                row = await cursor.fetchone()
            if (
                row
                and int(row["telegram_notified"] or 0) == 0
                and str(row["status"]) == "open"
                and candidate.severity == "critical"
            ):
                new_critical.append(candidate)
                await db.execute(
                    "UPDATE admin_alerts SET telegram_notified = 1 WHERE id = ?",
                    (int(row["id"]),),
                )

        if active_fps:
            placeholders = ",".join("?" for _ in active_fps)
            await db.execute(
                f"""
                UPDATE admin_alerts
                SET status = 'resolved'
                WHERE status = 'open'
                  AND fingerprint NOT IN ({placeholders})
                """,
                tuple(active_fps),
            )
        else:
            await db.execute(
                """
                UPDATE admin_alerts
                SET status = 'resolved'
                WHERE status = 'open'
                """
            )
        await db.commit()

    return new_critical


def _order_ref_text(provider_order_id: str | None, order_id: int) -> str:
    ref = display_order_ref(provider_order_id)
    return ref if ref != "—" else str(order_id)


def _build_order_alert_candidate(
    *,
    alert_type: str,
    severity: str,
    order_id: int,
    row: Any,
    title_prefix: str,
    headline: str,
    extra_plain: list[str] | None = None,
    extra_html: list[str] | None = None,
    payload_extra: dict[str, Any] | None = None,
) -> AlertCandidate:
    ref = _order_ref_text(row["provider_order_id"], order_id)
    message, message_html = format_order_alert_messages(
        row,
        headline=headline,
        extra_plain=extra_plain,
        extra_html=extra_html,
    )
    platform, provider, provider_slug = catalog_fields_from_row(row)
    payload: dict[str, Any] = {
        "order_id": order_id,
        "user_id": int(row["user_id"]),
        "provider_order_id": row["provider_order_id"],
        "service_name": row["service_name"],
        "platform": platform,
        "provider": provider,
        "provider_slug": provider_slug,
        "message_html": message_html,
    }
    if payload_extra:
        payload.update(payload_extra)
    return AlertCandidate(
        alert_type=alert_type,
        severity=severity,
        entity_type="order",
        entity_id=str(order_id),
        title=f"{title_prefix} — {ref}",
        message=message,
        fingerprint=f"{alert_type}:{order_id}",
        payload=payload,
    )


async def _scan_stuck_execution_orders() -> list[AlertCandidate]:
    hours = ALERT_STUCK_EXECUTION_HOURS
    async with get_db() as db:
        async with db.execute(
            f"""
            SELECT o.id, o.user_id, o.service_name, o.service_id, o.link, o.quantity, o.amount,
                   o.status, o.provider_order_id, o.provider_slug, o.api_account, o.start_count,
                   o.created_at, o.status_changed_at,
                   {ORDER_ALERT_SELECT_EXTRA}
            FROM orders o
            {ORDER_ALERT_CATALOG_JOIN}
            WHERE LOWER(REPLACE(o.status, '_', ' ')) IN ('in progress', 'processing')
              AND datetime(COALESCE(o.status_changed_at, o.created_at))
                  <= datetime('now', ?)
            ORDER BY COALESCE(o.status_changed_at, o.created_at) ASC
            LIMIT 100
            """,
            (f"-{hours} hours",),
        ) as cursor:
            rows = await cursor.fetchall()

    candidates: list[AlertCandidate] = []
    for row in rows:
        order_id = int(row["id"])
        status_ar = status_label_ar(normalize_order_status_key(str(row["status"])))
        since = str(row["status_changed_at"] or row["created_at"])
        qty = int(row["quantity"])
        amount = float(row["amount"])
        candidates.append(
            _build_order_alert_candidate(
                alert_type="stuck_execution",
                severity="critical",
                order_id=order_id,
                row=row,
                title_prefix="طلب عالق في التنفيذ",
                headline=f"الطلب في حالة «{status_ar}» منذ أكثر من {hours} ساعة.",
                extra_plain=[
                    f"الكمية: {qty} · المبلغ: {amount:.2f} درهم",
                    f"منذ: {since}",
                ],
                extra_html=[
                    f"الكمية: <code>{qty}</code> · المبلغ: <code>{amount:.2f}</code> درهم",
                    f"منذ: <code>{html.escape(since)}</code>",
                ],
                payload_extra={
                    "link": row["link"],
                    "quantity": qty,
                    "amount_dh": amount,
                    "status": row["status"],
                    "start_count": row["start_count"],
                    "since": since,
                    "hours_threshold": hours,
                },
            )
        )
    return candidates


async def _scan_stale_submitted_orders() -> list[AlertCandidate]:
    hours = ALERT_STUCK_SUBMITTED_HOURS
    async with get_db() as db:
        async with db.execute(
            f"""
            SELECT o.id, o.user_id, o.service_name, o.service_id, o.quantity, o.amount, o.status,
                   o.provider_order_id, o.provider_slug, o.api_account,
                   o.created_at, o.status_changed_at,
                   {ORDER_ALERT_SELECT_EXTRA}
            FROM orders o
            {ORDER_ALERT_CATALOG_JOIN}
            WHERE o.provider_order_id IS NOT NULL
              AND TRIM(o.provider_order_id) != ''
              AND LOWER(REPLACE(o.status, '_', ' ')) IN ('submitted', 'pending')
              AND datetime(COALESCE(o.status_changed_at, o.created_at))
                  <= datetime('now', ?)
            ORDER BY COALESCE(o.status_changed_at, o.created_at) ASC
            LIMIT 100
            """,
            (f"-{hours} hours",),
        ) as cursor:
            rows = await cursor.fetchall()

    candidates: list[AlertCandidate] = []
    for row in rows:
        order_id = int(row["id"])
        since = str(row["status_changed_at"] or row["created_at"])
        candidates.append(
            _build_order_alert_candidate(
                alert_type="stale_submitted",
                severity="warning",
                order_id=order_id,
                row=row,
                title_prefix="طلب مُرسَل دون تحرك",
                headline=(
                    f"الطلب ما زال «قيد الانتظار» لدى المزوّد منذ أكثر من {hours} ساعة "
                    "دون انتقال للتنفيذ."
                ),
                extra_plain=[f"منذ: {since}"],
                extra_html=[f"منذ: <code>{html.escape(since)}</code>"],
                payload_extra={
                    "amount_dh": float(row["amount"]),
                    "since": since,
                    "hours_threshold": hours,
                },
            )
        )
    return candidates


async def _scan_old_manual_orders() -> list[AlertCandidate]:
    hours = ALERT_OLD_MANUAL_HOURS
    async with get_db() as db:
        async with db.execute(
            f"""
            SELECT o.id, o.user_id, o.service_name, o.service_id, o.quantity, o.amount,
                   o.provider_order_id, o.provider_slug, o.api_account,
                   o.created_at, o.status_changed_at,
                   {ORDER_ALERT_SELECT_EXTRA}
            FROM orders o
            {ORDER_ALERT_CATALOG_JOIN}
            WHERE LOWER(REPLACE(o.status, '_', ' ')) = 'pending admin'
              AND COALESCE(o.fulfillment_mode, 'auto') = 'admin'
              AND datetime(COALESCE(o.status_changed_at, o.created_at))
                  <= datetime('now', ?)
            ORDER BY COALESCE(o.status_changed_at, o.created_at) ASC
            LIMIT 100
            """,
            (f"-{hours} hours",),
        ) as cursor:
            rows = await cursor.fetchall()

    candidates: list[AlertCandidate] = []
    for row in rows:
        order_id = int(row["id"])
        candidates.append(
            _build_order_alert_candidate(
                alert_type="old_manual_order",
                severity="warning",
                order_id=order_id,
                row=row,
                title_prefix="طلب يدوي بانتظار التنفيذ",
                headline=f"الطلب اليدوي بانتظار الإدارة منذ أكثر من {hours} ساعة.",
                extra_plain=[f"المبلغ: {float(row['amount']):.2f} درهم"],
                extra_html=[f"المبلغ: <code>{float(row['amount']):.2f}</code> درهم"],
                payload_extra={
                    "amount_dh": float(row["amount"]),
                    "hours_threshold": hours,
                },
            )
        )
    return candidates


async def _scan_old_deposits() -> list[AlertCandidate]:
    hours = ALERT_OLD_DEPOSIT_HOURS
    async with get_db() as db:
        async with db.execute(
            """
            SELECT id, user_id, amount, created_at
            FROM deposits
            WHERE status = 'pending'
              AND datetime(created_at) <= datetime('now', ?)
            ORDER BY created_at ASC
            LIMIT 50
            """,
            (f"-{hours} hours",),
        ) as cursor:
            rows = await cursor.fetchall()

    return [
        AlertCandidate(
            alert_type="old_deposit",
            severity="warning",
            entity_type="deposit",
            entity_id=str(int(row["id"])),
            title=f"إيداع معلق — #{int(row['id'])}",
            message=(
                f"إيداع بقيمة {float(row['amount']):.2f} درهم للمستخدم "
                f"{int(row['user_id'])} معلق منذ أكثر من {hours} ساعة."
            ),
            fingerprint=f"old_deposit:{int(row['id'])}",
            payload={
                "deposit_id": int(row["id"]),
                "user_id": int(row["user_id"]),
                "amount_dh": float(row["amount"]),
                "created_at": str(row["created_at"]),
                "hours_threshold": hours,
            },
        )
        for row in rows
    ]


async def _scan_old_withdrawals() -> list[AlertCandidate]:
    hours = ALERT_OLD_WITHDRAWAL_HOURS
    async with get_db() as db:
        async with db.execute(
            """
            SELECT id, user_id, amount, created_at
            FROM withdrawals
            WHERE status = 'pending'
              AND datetime(created_at) <= datetime('now', ?)
            ORDER BY created_at ASC
            LIMIT 50
            """,
            (f"-{hours} hours",),
        ) as cursor:
            rows = await cursor.fetchall()

    return [
        AlertCandidate(
            alert_type="old_withdrawal",
            severity="warning",
            entity_type="withdrawal",
            entity_id=str(int(row["id"])),
            title=f"سحب معلق — #{int(row['id'])}",
            message=(
                f"طلب سحب بقيمة {float(row['amount']):.2f} درهم للمستخدم "
                f"{int(row['user_id'])} معلق منذ أكثر من {hours} ساعة."
            ),
            fingerprint=f"old_withdrawal:{int(row['id'])}",
            payload={
                "withdrawal_id": int(row["id"]),
                "user_id": int(row["user_id"]),
                "amount_dh": float(row["amount"]),
                "created_at": str(row["created_at"]),
                "hours_threshold": hours,
            },
        )
        for row in rows
    ]


async def _scan_low_provider_balance() -> list[AlertCandidate]:
    from services.smm_provider import fetch_provider_balance

    threshold = ALERT_PROVIDER_BALANCE_MIN_USD
    try:
        data = await fetch_provider_balance()
    except Exception as exc:
        logger.warning("Provider balance scan failed: %s", exc)
        return [
            AlertCandidate(
                alert_type="provider_balance_error",
                severity="warning",
                entity_type="provider",
                entity_id=None,
                title="تعذّر فحص رصيد المزوّد",
                message=f"لم يُكتمل فحص أرصدة المزوّدين: {exc}",
                fingerprint="provider_balance_error:global",
                payload={"error": str(exc)},
            )
        ]

    if not data.get("ok"):
        err = str(data.get("error") or "خطأ غير معروف")
        return [
            AlertCandidate(
                alert_type="provider_balance_error",
                severity="warning",
                entity_type="provider",
                entity_id=None,
                title="تعذّر فحص رصيد المزوّد",
                message=err,
                fingerprint="provider_balance_error:global",
                payload={"error": err},
            )
        ]

    candidates: list[AlertCandidate] = []
    accounts = data.get("accounts") or []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        balance = float(account.get("balance_usd") or account.get("balance") or 0.0)
        err = account.get("error")
        slug = str(account.get("provider_slug") or account.get("slug") or "")
        key = str(account.get("account_key") or account.get("account") or "default")
        label = f"{slug}/{key}" if slug else key
        if err:
            candidates.append(
                AlertCandidate(
                    alert_type="provider_balance_error",
                    severity="warning",
                    entity_type="provider",
                    entity_id=label,
                    title=f"خطأ رصيد المزوّد — {label}",
                    message=str(err),
                    fingerprint=f"provider_balance_error:{label}",
                    payload={"account": label, "error": str(err)},
                )
            )
            continue
        if balance < threshold:
            candidates.append(
                AlertCandidate(
                    alert_type="low_provider_balance",
                    severity="critical",
                    entity_type="provider",
                    entity_id=label,
                    title=f"رصيد مزوّد منخفض — {label}",
                    message=(
                        f"رصيد حساب {label} هو {balance:.2f} USD "
                        f"(الحد الأدنى: {threshold:.2f} USD)."
                    ),
                    fingerprint=f"low_provider_balance:{label}",
                    payload={
                        "account": label,
                        "balance_usd": balance,
                        "threshold_usd": threshold,
                    },
                )
            )
    return candidates


async def _scan_stale_prices() -> list[AlertCandidate]:
    days = ALERT_STALE_PRICES_DAYS
    async with get_db() as db:
        async with db.execute(
            """
            SELECT COUNT(*) AS stale_count
            FROM smm_services
            WHERE is_active = 1
              AND (
                    provider_price_updated_at IS NULL
                    OR datetime(provider_price_updated_at) <= datetime('now', ?)
                  )
            """,
            (f"-{days} days",),
        ) as cursor:
            row = await cursor.fetchone()
    stale_count = int(row["stale_count"] if row else 0)
    if stale_count <= 0:
        return []
    return [
        AlertCandidate(
            alert_type="stale_prices",
            severity="info",
            entity_type="catalog",
            entity_id=None,
            title="أسعار مزوّد قديمة",
            message=(
                f"{stale_count} خدمة نشطة لم يُحدَّث سعر المزوّد لها منذ أكثر من {days} يوم."
            ),
            fingerprint="stale_prices:global",
            payload={"stale_count": stale_count, "days_threshold": days},
        )
    ]


async def scan_all_alerts() -> dict[str, int]:
    """Run all scanners, upsert alerts, optionally notify Telegram."""
    await ensure_admin_alerts_schema()
    candidates: list[AlertCandidate] = []
    scanners = (
        _scan_stuck_execution_orders,
        _scan_stale_submitted_orders,
        _scan_old_manual_orders,
        _scan_old_deposits,
        _scan_old_withdrawals,
        _scan_low_provider_balance,
        _scan_stale_prices,
    )
    for scanner in scanners:
        try:
            candidates.extend(await scanner())
        except Exception as exc:
            logger.warning("Alert scanner %s failed: %s", scanner.__name__, exc, exc_info=True)

    new_critical = await _upsert_candidates(candidates)

    if ALERT_TELEGRAM_ON_CRITICAL and new_critical:
        from notifier import notify_admin_system_alert

        for candidate in new_critical:
            try:
                message_html = None
                if candidate.payload:
                    message_html = candidate.payload.get("message_html")
                await notify_admin_system_alert(
                    candidate.title,
                    candidate.message,
                    message_html=message_html,
                )
            except Exception as exc:
                logger.warning(
                    "Telegram alert notify failed for %s: %s",
                    candidate.fingerprint,
                    exc,
                )

    counts = await count_open_alerts()
    logger.info(
        "Admin alert scan: active=%s open=%s critical=%s",
        len(candidates),
        counts["admin_alerts_count"],
        counts["admin_alerts_critical"],
    )
    return {
        "scanned": len(candidates),
        **counts,
    }


async def run_alert_scan_loop() -> None:
    """Background worker — periodic alert scans."""
    interval = max(60, ALERT_SCAN_INTERVAL_MINUTES * 60)
    while True:
        try:
            await scan_all_alerts()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Admin alert scan loop error: %s", exc)
        await asyncio.sleep(interval)
