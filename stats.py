"""Dashboard summary statistics from the shared bot database."""
from __future__ import annotations

from typing import Any

from analytics import get_profit_chart
from admin_alerts import count_open_alerts, list_open_alerts
from admin_notifications import get_notifications_summary
from database_connector import get_db
from timed_announcements import list_active_timed_announcements


async def get_dashboard_stats() -> dict[str, Any]:
    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            users_row = await cursor.fetchone()
        total_users = int(users_row[0]) if users_row else 0

        async with db.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM orders
            WHERE LOWER(REPLACE(status, '_', ' ')) IN ('completed', 'partial')
            """
        ) as cursor:
            revenue_row = await cursor.fetchone()
        total_revenue = float(revenue_row[0] or 0.0)

        async with db.execute(
            """
            SELECT COUNT(*)
            FROM orders
            WHERE LOWER(REPLACE(status, '_', ' ')) = 'completed'
            """
        ) as cursor:
            completed_row = await cursor.fetchone()
        completed_orders = int(completed_row[0]) if completed_row else 0

        async with db.execute(
            "SELECT COUNT(*) FROM deposits WHERE status = 'pending'"
        ) as cursor:
            dep_row = await cursor.fetchone()
        pending_deposits = int(dep_row[0]) if dep_row else 0

        async with db.execute(
            "SELECT COUNT(*) FROM withdrawals WHERE status = 'pending'"
        ) as cursor:
            wd_row = await cursor.fetchone()
        pending_withdrawals = int(wd_row[0]) if wd_row else 0

        async with db.execute(
            """
            SELECT COUNT(*)
            FROM orders
            WHERE LOWER(REPLACE(status, '_', ' ')) = 'pending admin'
              AND COALESCE(fulfillment_mode, 'auto') = 'admin'
            """
        ) as cursor:
            mo_row = await cursor.fetchone()
        pending_manual_orders = int(mo_row[0]) if mo_row else 0

    active_timed_announcements = await list_active_timed_announcements()
    admin_alerts = await list_open_alerts(limit=20)
    alert_counts = await count_open_alerts()
    notif_summary = await get_notifications_summary()

    profit_totals: dict[str, float] = {
        "net_profit_dh": 0.0,
        "provider_costs_dh": 0.0,
    }
    try:
        profit_chart = await get_profit_chart()
        totals = profit_chart.get("totals") or {}
        profit_totals["net_profit_dh"] = float(totals.get("net_profit_dh") or 0.0)
        profit_totals["provider_costs_dh"] = float(totals.get("costs_dh") or 0.0)
    except Exception:
        pass

    pending_actions = pending_deposits + pending_withdrawals + pending_manual_orders
    return {
        "total_users": total_users,
        "total_revenue_dh": round(total_revenue, 2),
        "net_profit_dh": round(profit_totals["net_profit_dh"], 2),
        "provider_costs_dh": round(profit_totals["provider_costs_dh"], 2),
        "completed_orders": completed_orders,
        "pending_deposits": pending_deposits,
        "pending_withdrawals": pending_withdrawals,
        "pending_manual_orders": pending_manual_orders,
        "pending_actions": pending_actions,
        "active_timed_announcements_count": len(active_timed_announcements),
        "active_timed_announcements": active_timed_announcements,
        "admin_alerts_count": alert_counts["admin_alerts_count"],
        "admin_alerts_critical": alert_counts["admin_alerts_critical"],
        "admin_alerts": admin_alerts,
        "admin_notifications_unread": notif_summary["unread_count"],
        "admin_notifications_today": notif_summary["today_count"],
    }
