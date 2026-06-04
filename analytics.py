"""Financial liquidity and business intelligence analytics from the shared bot database."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from database_connector import get_db
from utils.order_status import normalize_order_status_key

_CASHFLOW_DAYS = 30
_USD_TO_MAD = 10.0

# Per-order profit expressions (MAD / DH) — joined with smm_services for provider cost.
_ORDER_NET_SALES = (
    "MAX(0, COALESCE(o.amount, 0) - COALESCE(o.refunded_amount, 0))"
)
_ORDER_PROVIDER_COST = (
    f"(COALESCE(o.quantity, 0) / 1000.0) * COALESCE(s.provider_price_usd, 0) * {_USD_TO_MAD}"
)
_ORDER_REFERRAL_COST = "COALESCE(o.referral_commission_amount, 0)"
_ORDER_TOTAL_COSTS = f"({_ORDER_PROVIDER_COST} + {_ORDER_REFERRAL_COST})"
_ORDER_NET_PROFIT = f"({_ORDER_NET_SALES} - {_ORDER_TOTAL_COSTS})"

_PROFIT_ORDERS_FROM = """
    FROM orders AS o
    LEFT JOIN smm_services AS s
        ON TRIM(CAST(o.service_id AS TEXT)) = TRIM(CAST(s.service_id AS TEXT))
"""

_ORDER_STATUS_BUCKETS: tuple[tuple[str, frozenset[str], str], ...] = (
    ("completed", frozenset({"completed"}), "مكتمل"),
    ("partial", frozenset({"partial"}), "مكتمل جزئياً"),
    (
        "canceled_refunded",
        frozenset({"canceled", "refunded", "failed"}),
        "ملغي / مسترد",
    ),
    (
        "pending",
        frozenset(
            {
                "submitted",
                "pending",
                "in progress",
                "processing",
            }
        ),
        "قيد الانتظار",
    ),
)

_GATEWAY_LABELS_AR: dict[str, str] = {
    "Binance/Crypto": "عملات رقمية (بينانس)",
    "PayPal": "باي بال",
}


def _float_or_zero(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _label_gateway(method: str) -> str:
    key = (method or "").strip()
    if not key:
        return "غير محدد"
    return _GATEWAY_LABELS_AR.get(key, key)


def _last_n_day_keys(n: int) -> list[str]:
    """Inclusive range of the last ``n`` calendar days (oldest → newest)."""
    today = date.today()
    return [(today - timedelta(days=offset)).isoformat() for offset in range(n - 1, -1, -1)]


async def _query_daily_totals(
    db: Any,
    *,
    table: str,
    amount_column: str,
    date_expr: str,
    status_filter: str,
) -> dict[str, float]:
    sql = f"""
        SELECT {date_expr} AS day_key,
               COALESCE(SUM({amount_column}), 0) AS day_total
        FROM {table}
        WHERE {status_filter}
          AND {date_expr} >= DATE('now', 'localtime', '-{_CASHFLOW_DAYS - 1} days')
        GROUP BY day_key
        ORDER BY day_key
    """
    async with db.execute(sql) as cursor:
        rows = await cursor.fetchall()
    return {str(row[0]): _float_or_zero(row[1]) for row in rows if row[0]}


async def get_analytics_summary() -> dict[str, float]:
    """KPI totals for treasury summary cards."""
    metrics = await get_liquidity_metrics()
    return {
        "total_deposited_dh": metrics["total_deposited_dh"],
        "total_liabilities_dh": metrics["total_liabilities_dh"],
        "total_free_money_dh": metrics["total_free_money_dh"],
    }


async def get_cashflow_chart() -> dict[str, list[Any]]:
    """
    Daily deposits vs withdrawals for the last 30 days (SQLite local date).
    """
    deposit_date = "DATE(created_at)"
    withdrawal_date = "DATE(COALESCE(updated_at, created_at))"

    async with get_db() as db:
        deposits_by_day = await _query_daily_totals(
            db,
            table="deposit_transactions",
            amount_column="amount",
            date_expr=deposit_date,
            status_filter="LOWER(TRIM(status)) = 'completed'",
        )
        withdrawals_by_day = await _query_daily_totals(
            db,
            table="withdrawals",
            amount_column="amount",
            date_expr=withdrawal_date,
            status_filter="LOWER(TRIM(status)) = 'completed'",
        )

    day_keys = _last_n_day_keys(_CASHFLOW_DAYS)
    return {
        "dates": day_keys,
        "deposits": [round(deposits_by_day.get(d, 0.0), 2) for d in day_keys],
        "withdrawals": [round(withdrawals_by_day.get(d, 0.0), 2) for d in day_keys],
    }


async def get_gateways_chart() -> dict[str, list[Any]]:
    """Completed deposit volume grouped by payment gateway (deposit_method)."""
    async with get_db() as db:
        async with db.execute(
            """
            SELECT deposit_method, COALESCE(SUM(amount), 0) AS total
            FROM deposit_transactions
            WHERE LOWER(TRIM(status)) = 'completed'
            GROUP BY deposit_method
            ORDER BY total DESC
            """
        ) as cursor:
            rows = await cursor.fetchall()

    methods: list[str] = []
    totals: list[float] = []
    for row in rows:
        methods.append(_label_gateway(str(row[0] or "")))
        totals.append(round(_float_or_zero(row[1]), 2))

    return {"methods": methods, "totals": totals}


def _bucket_order_status(raw_status: object) -> str:
    key = normalize_order_status_key(raw_status)
    for bucket_id, keys, _label in _ORDER_STATUS_BUCKETS:
        if key in keys:
            return bucket_id
    return "pending"


async def get_profit_chart() -> dict[str, list[Any]]:
    """
    Phase 3 — محرك الأرباح الصافية (آخر 30 يومًا).

    Per order: net sales − provider cost (USD×10 MAD) − referral commission.
    """
    sql = f"""
        SELECT DATE(o.created_at) AS day_key,
               COALESCE(SUM({_ORDER_NET_SALES}), 0) AS daily_sales,
               COALESCE(SUM({_ORDER_TOTAL_COSTS}), 0) AS daily_costs,
               COALESCE(SUM({_ORDER_NET_PROFIT}), 0) AS daily_net_profit
        {_PROFIT_ORDERS_FROM}
        WHERE DATE(o.created_at) >= DATE('now', 'localtime', '-{_CASHFLOW_DAYS - 1} days')
        GROUP BY day_key
        ORDER BY day_key
    """
    async with get_db() as db:
        async with db.execute(sql) as cursor:
            rows = await cursor.fetchall()

    sales_by_day = {str(r[0]): _float_or_zero(r[1]) for r in rows if r[0]}
    costs_by_day = {str(r[0]): _float_or_zero(r[2]) for r in rows if r[0]}
    profit_by_day = {str(r[0]): _float_or_zero(r[3]) for r in rows if r[0]}

    day_keys = _last_n_day_keys(_CASHFLOW_DAYS)
    sales = [round(sales_by_day.get(d, 0.0), 2) for d in day_keys]
    costs = [round(costs_by_day.get(d, 0.0), 2) for d in day_keys]
    net_profit = [round(profit_by_day.get(d, 0.0), 2) for d in day_keys]

    return {
        "dates": day_keys,
        "sales": sales,
        "costs": costs,
        "net_profit": net_profit,
    }


async def get_orders_status_chart() -> dict[str, list[Any]]:
    """Phase 4 — توزيع حالات الطلبات."""
    async with get_db() as db:
        async with db.execute("SELECT status FROM orders") as cursor:
            rows = await cursor.fetchall()

    counts = {bucket_id: 0 for bucket_id, _, _ in _ORDER_STATUS_BUCKETS}
    for row in rows:
        bucket = _bucket_order_status(row[0])
        counts[bucket] = counts.get(bucket, 0) + 1

    labels: list[str] = []
    series: list[int] = []
    for bucket_id, _keys, label_ar in _ORDER_STATUS_BUCKETS:
        count = counts.get(bucket_id, 0)
        if count <= 0:
            continue
        labels.append(label_ar)
        series.append(count)

    if not labels:
        labels = ["لا توجد طلبات"]
        series = [0]

    return {"labels": labels, "series": series}


async def get_leaderboards() -> dict[str, list[dict[str, Any]]]:
    """Phase 4 — أفضل الخدمات وكبار العملاء."""
    async with get_db() as db:
        async with db.execute(
            """
            SELECT
                TRIM(COALESCE(service_name, '')) AS service_name,
                COUNT(*) AS order_count
            FROM orders
            WHERE TRIM(COALESCE(service_name, '')) != ''
            GROUP BY TRIM(COALESCE(service_name, ''))
            ORDER BY order_count DESC
            LIMIT 5
            """
        ) as svc_cursor:
            service_rows = await svc_cursor.fetchall()

        async with db.execute(
            """
            SELECT
                user_id,
                TRIM(COALESCE(telegram_name, '')) AS telegram_name,
                COALESCE(total_spent, 0) AS total_spent
            FROM users
            ORDER BY COALESCE(total_spent, 0) DESC
            LIMIT 10
            """
        ) as user_cursor:
            user_rows = await user_cursor.fetchall()

    top_services: list[dict[str, Any]] = []
    for row in service_rows:
        name = str(row[0] or "").strip() or "خدمة غير معرّفة"
        top_services.append(
            {
                "name": name,
                "order_count": int(row[1] or 0),
            }
        )

    top_vip_users: list[dict[str, Any]] = []
    for row in user_rows:
        user_id = int(row[0])
        tg_name = str(row[1] or "").strip()
        top_vip_users.append(
            {
                "user_id": user_id,
                "display_name": tg_name if tg_name else str(user_id),
                "total_spent_dh": round(_float_or_zero(row[2]), 2),
            }
        )

    return {
        "top_services": top_services,
        "top_vip_users": top_vip_users,
    }


async def get_liquidity_metrics() -> dict[str, float]:
    """
    Phase 1 — السيولة المالية والمال الحر (full metrics bundle).
    """
    async with get_db() as db:
        async with db.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM deposit_transactions
            WHERE LOWER(TRIM(status)) = 'completed'
            """
        ) as cursor:
            deposited_row = await cursor.fetchone()

        async with db.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM withdrawals
            WHERE LOWER(TRIM(status)) = 'completed'
            """
        ) as cursor:
            withdrawn_row = await cursor.fetchone()

        async with db.execute(
            """
            SELECT COALESCE(SUM(COALESCE(balance, 0) + COALESCE(referral_balance, 0)), 0)
            FROM users
            """
        ) as cursor:
            liabilities_row = await cursor.fetchone()

        async with db.execute(
            """
            SELECT COALESCE(SUM(COALESCE(total_spent, 0)), 0)
            FROM users
            """
        ) as cursor:
            free_money_row = await cursor.fetchone()

        async with db.execute(
            """
            SELECT COALESCE(SUM(COALESCE(referral_earned_total, 0)), 0)
            FROM users
            """
        ) as cursor:
            referral_row = await cursor.fetchone()

    total_free_money = max(0.0, _float_or_zero(free_money_row[0] if free_money_row else 0))

    return {
        "total_deposited_dh": round(_float_or_zero(deposited_row[0] if deposited_row else 0), 2),
        "total_withdrawn_dh": round(_float_or_zero(withdrawn_row[0] if withdrawn_row else 0), 2),
        "total_liabilities_dh": round(_float_or_zero(liabilities_row[0] if liabilities_row else 0), 2),
        "total_free_money_dh": round(total_free_money, 2),
        "referral_payouts_dh": round(_float_or_zero(referral_row[0] if referral_row else 0), 2),
    }
