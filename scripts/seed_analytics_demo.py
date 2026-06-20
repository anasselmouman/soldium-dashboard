#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""بيانات تجريبية لاختبار لوحة التحليلات المالية (إيداعات، سحوبات، طلبات)."""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT_DASH = Path(__file__).resolve().parents[1]
if str(_ROOT_DASH) not in sys.path:
    sys.path.insert(0, str(_ROOT_DASH))

from database_connector import DB_PATH  # noqa: E402

# مستخدمون تجريبيون (معرفات وهمية عالية لتجنب التصادم)
DEMO_USERS: tuple[tuple[int, str, float, float, float], ...] = (
    # user_id, telegram_name, balance, referral_balance, total_spent
    (990_001, "عميل تجريبي أ", 450.0, 25.0, 320.0),
    (990_002, "عميل تجريبي ب", 180.0, 0.0, 890.0),
    (990_003, "عميل VIP تجريبي", 1200.0, 80.0, 2450.0),
)

REFERRER_EARNED = 120.0  # يُضاف لأول مُحيل موجود إن وُجد


def _day_iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat() + " 12:00:00"


def _pick_service(conn: sqlite3.Connection) -> dict[str, str | float]:
    row = conn.execute(
        """
        SELECT service_id, COALESCE(local_item_id, service_id) AS local_item_id,
               COALESCE(name_ar, 'خدمة تجريبية') AS name_ar,
               COALESCE(provider_price_usd, 0) AS provider_price_usd,
               COALESCE(local_price_dh, 0) AS local_price_dh,
               COALESCE(category, '') AS category
        FROM smm_services
        WHERE COALESCE(provider_price_usd, 0) > 0
        ORDER BY service_id
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("لا توجد خدمات في smm_services — شغّل migrate_smm_services أولاً.")
    return dict(row)


def seed_analytics_demo() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        svc = _pick_service(conn)
        service_key = str(svc["local_item_id"] or svc["service_id"])
        service_name = str(svc["name_ar"])

        for user_id, tg_name, balance, ref_bal, spent in DEMO_USERS:
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
            conn.execute(
                """
                UPDATE users
                SET telegram_name = ?, balance = ?, total_spent = ?, referral_balance = ?
                WHERE user_id = ?
                """,
                (tg_name, balance, spent, ref_bal, user_id),
            )

        # عمولات إحالة مجمّعة لمستخدم موجود
        first_user = conn.execute("SELECT user_id FROM users ORDER BY user_id LIMIT 1").fetchone()
        if first_user:
            conn.execute(
                """
                UPDATE users
                SET referral_earned_total = COALESCE(referral_earned_total, 0) + ?
                WHERE user_id = ?
                """,
                (REFERRER_EARNED, int(first_user["user_id"])),
            )

        deposits: list[tuple] = [
            # user_id, amount, method, days_ago
            (990_001, 500.0, "Binance/Crypto", 28),
            (990_001, 200.0, "PayPal", 21),
            (990_002, 1000.0, "Binance/Crypto", 14),
            (990_003, 300.0, "CashPlus", 7),
            (990_003, 150.0, "PayPal", 2),
            (990_001, 75.0, "Binance/Crypto", 0),
        ]
        for user_id, amount, method, days_ago in deposits:
            conn.execute(
                """
                INSERT INTO deposit_transactions (user_id, deposit_method, amount, status, created_at)
                VALUES (?, ?, ?, 'completed', ?)
                """,
                (user_id, method, amount, _day_iso(days_ago)),
            )

        withdrawals: list[tuple] = [
            (990_002, 150.0, "Binance/Crypto", 25),
            (990_001, 80.0, "PayPal", 10),
            (990_003, 400.0, "Binance/Crypto", 3),
        ]
        for user_id, amount, method, days_ago in withdrawals:
            day = _day_iso(days_ago)
            conn.execute(
                """
                INSERT INTO withdrawals (
                    user_id, amount, method, details_json, status,
                    withdrawal_type, created_at, updated_at
                ) VALUES (?, ?, ?, '{}', 'completed', 'normal', ?, ?)
                """,
                (user_id, amount, method, day, day),
            )

        orders: list[dict] = [
            {
                "user_id": 990_001,
                "amount": 45.0,
                "quantity": 1000,
                "status": "completed",
                "refunded": 0.0,
                "referral": 4.5,
                "days_ago": 27,
            },
            {
                "user_id": 990_002,
                "amount": 120.0,
                "quantity": 1000,
                "status": "completed",
                "refunded": 0.0,
                "referral": 12.0,
                "days_ago": 15,
            },
            {
                "user_id": 990_003,
                "amount": 200.0,
                "quantity": 2000,
                "status": "partial",
                "refunded": 80.0,
                "referral": 6.0,
                "days_ago": 8,
                "provider_usd": 3.5,
            },
            {
                "user_id": 990_001,
                "amount": 30.0,
                "quantity": 500,
                "status": "completed",
                "refunded": 0.0,
                "referral": 0.0,
                "days_ago": 4,
            },
            {
                "user_id": 990_002,
                "amount": 60.0,
                "quantity": 1000,
                "status": "pending",
                "refunded": 0.0,
                "referral": 0.0,
                "days_ago": 1,
            },
            {
                "user_id": 990_003,
                "amount": 90.0,
                "quantity": 1000,
                "status": "canceled",
                "refunded": 0.0,
                "referral": 0.0,
                "days_ago": 5,
            },
        ]

        for spec in orders:
            created = _day_iso(spec["days_ago"])
            cur = conn.execute(
                """
                INSERT INTO orders (
                    user_id, service_name, service_id, link, quantity, amount,
                    total_price, status, refunded_amount, referral_commission_amount,
                    created_at
                ) VALUES (?, ?, ?, 'https://example.com/demo', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec["user_id"],
                    service_name,
                    service_key,
                    spec["quantity"],
                    spec["amount"],
                    spec["amount"],
                    spec["status"],
                    spec["refunded"],
                    spec["referral"],
                    created,
                ),
            )
            order_id = int(cur.lastrowid)
            if spec["status"] == "partial" and spec.get("provider_usd"):
                conn.execute(
                    """
                    INSERT INTO refund_audit_log (
                        order_id, user_id, refund_type, refund_amount_dh, actual_provider_usd
                    ) VALUES (?, ?, 'partial', ?, ?)
                    """,
                    (
                        order_id,
                        spec["user_id"],
                        spec["refunded"],
                        spec["provider_usd"],
                    ),
                )

        conn.commit()
    finally:
        conn.close()

    print(f"Analytics demo data written to: {DB_PATH}")
    print("- 3 demo users (990001-990003)")
    print("- 6 completed deposits (last 30 days)")
    print("- 3 completed withdrawals")
    print("- 6 new orders (completed / partial / pending / canceled)")
    print("Open the analytics dashboard and click refresh.")


if __name__ == "__main__":
    seed_analytics_demo()
