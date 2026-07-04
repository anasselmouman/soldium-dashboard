"""Shared definition of a «real» bot user (not start-only ghost)."""

from __future__ import annotations

# Expression evaluated on alias ``u`` (users row).
USER_IS_ACTIVE_EXPR = """
(
    EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.user_id)
    OR EXISTS (SELECT 1 FROM deposits d WHERE d.user_id = u.user_id)
    OR EXISTS (SELECT 1 FROM deposit_transactions dt WHERE dt.user_id = u.user_id)
    OR EXISTS (SELECT 1 FROM withdrawals w WHERE w.user_id = u.user_id)
    OR EXISTS (SELECT 1 FROM users i WHERE i.referred_by = u.user_id)
    OR ABS(COALESCE(u.balance, 0)) >= 0.01
    OR ABS(COALESCE(u.total_spent, 0)) >= 0.01
    OR ABS(COALESCE(u.referral_balance, 0)) >= 0.01
    OR ABS(COALESCE(u.referral_earned_total, 0)) >= 0.01
    OR COALESCE(u.referral_level, 1) > 1
)
"""

COUNT_ACTIVE_USERS_SQL = f"""
    SELECT COUNT(*)
    FROM users u
    WHERE {USER_IS_ACTIVE_EXPR}
"""
