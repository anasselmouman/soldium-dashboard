"""Dashboard summary statistics from the shared bot database."""
from __future__ import annotations

from typing import Any

from database_connector import get_db


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

    pending_actions = pending_deposits + pending_withdrawals
    return {
        "total_users": total_users,
        "total_revenue_dh": round(total_revenue, 2),
        "completed_orders": completed_orders,
        "pending_deposits": pending_deposits,
        "pending_withdrawals": pending_withdrawals,
        "pending_actions": pending_actions,
    }
