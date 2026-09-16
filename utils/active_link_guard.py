# -*- coding: utf-8 -*-
"""Active-link order protection — mirror of soldium-bot/utils/active_link_guard.py.

Keep in sync with the bot module. ``orders`` remains the single source of truth.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

ACTIVE_LINK_ORDER_STATUS_KEYS: frozenset[str] = frozenset(
    {
        "pending",
        "pending admin",
        "submitted",
        "in progress",
        "processing",
    }
)

TERMINAL_LINK_ORDER_STATUS_KEYS: frozenset[str] = frozenset(
    {
        "completed",
        "partial",
        "canceled",
        "failed",
        "refunded",
    }
)

ACTIVE_LINK_ORDER_STATUS_SQL_VALUES: tuple[str, ...] = (
    "pending",
    "pending_admin",
    "pending admin",
    "submitted",
    "in progress",
    "in_progress",
    "processing",
)

ACTIVE_LINK_OCCUPIED_MESSAGE = (
    "⚠️ <b>الرابط مشغول حالياً</b>\n\n"
    "هذا الرابط لديه طلب قيد التنفيذ حالياً، لذلك لا يمكن إرسال طلب جديد "
    "لنفس الرابط قبل انتهاء الطلب الحالي.\n\n"
    "انتظر حتى يكتمل الطلب أو يكتمل جزئياً أو يُلغى أو يفشل، "
    "ثم يمكنك إرسال طلب جديد."
)


def is_active_link_unique_violation(exc: BaseException) -> bool:
    """True only for the active-link partial unique index (not other UNIQUE errors)."""
    text = str(exc or "").lower()
    if "idx_orders_active_normalized_link" in text:
        return True
    # SQLite often reports the column, not the index name.
    return "unique" in text and "normalized_link" in text


class ActiveLinkOccupiedError(Exception):
    """Raised when an active order already occupies the target link."""

    def __init__(
        self,
        message: str = ACTIVE_LINK_OCCUPIED_MESSAGE,
        *,
        existing_order_id: int | None = None,
        normalized_link: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.existing_order_id = existing_order_id
        self.normalized_link = normalized_link


def normalize_order_status_for_link_guard(raw_status: object) -> str:
    status = str(raw_status or "").strip().lower().replace("_", " ")
    if status == "cancelled":
        return "canceled"
    return status


def is_active_link_order_status(raw_status: object) -> bool:
    return normalize_order_status_for_link_guard(raw_status) in ACTIVE_LINK_ORDER_STATUS_KEYS


def is_terminal_link_order_status(raw_status: object) -> bool:
    return normalize_order_status_for_link_guard(raw_status) in TERMINAL_LINK_ORDER_STATUS_KEYS


def normalize_order_link(link: object) -> str:
    """Conservative occupancy key for a customer target.

    Preserves meaningful path/query/fragment differences. For http(s) URLs:
    - trim whitespace
    - lowercase scheme and host
    - strip a single leading ``www.`` from the host
    - drop default ports
    - drop a single trailing slash on a non-root path

    Non-URL targets (``@user``, free text, multi-line payloads) are only trimmed.
    Empty / whitespace-only input yields ``\"\"`` (must not participate in uniqueness).
    """
    raw = str(link or "").strip()
    if not raw:
        return ""

    first = raw.splitlines()[0].strip() if "\n" in raw or "\r" in raw else raw
    lowered = first[:12].lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return raw

    try:
        parts = urlsplit(first)
    except ValueError:
        return raw

    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if not host:
        return raw
    if host.startswith("www."):
        host = host[4:]

    netloc = host
    port = parts.port
    if port is not None:
        if not (
            (scheme == "http" and port == 80)
            or (scheme == "https" and port == 443)
        ):
            netloc = f"{host}:{port}"

    path = parts.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    canonical = urlunsplit((scheme, netloc, path, parts.query, parts.fragment))
    if "\n" in raw or "\r" in raw:
        rest = raw.splitlines()[1:]
        rest_joined = "\n".join(line.rstrip() for line in rest).strip("\n")
        if rest_joined:
            return f"{canonical}\n{rest_joined}"
    return canonical


def active_link_status_sql_predicate(column: str = "status") -> str:
    return (
        f"LOWER(REPLACE({column}, '_', ' ')) IN "
        f"('pending', 'pending admin', 'submitted', 'in progress', 'processing')"
    )


def find_active_order_for_link(
    connection: Any,
    link: object,
) -> dict[str, Any] | None:
    key = normalize_order_link(link)
    if not key:
        return None

    status_pred = active_link_status_sql_predicate("status")
    row = connection.execute(
        f"""
        SELECT id, user_id, service_id, link, status, normalized_link, created_at
        FROM orders
        WHERE normalized_link = ?
          AND {status_pred}
        ORDER BY id ASC
        LIMIT 1
        """,
        (key,),
    ).fetchone()
    if row is not None:
        return _row_to_active_hit(row)

    rows = connection.execute(
        f"""
        SELECT id, user_id, service_id, link, status, normalized_link, created_at
        FROM orders
        WHERE (normalized_link IS NULL OR normalized_link = '')
          AND link IS NOT NULL
          AND TRIM(link) != ''
          AND {status_pred}
        ORDER BY id ASC
        """
    ).fetchall()
    for candidate in rows:
        stored = candidate["link"] if hasattr(candidate, "keys") else candidate[3]
        if normalize_order_link(stored) == key:
            return _row_to_active_hit(candidate)
    return None


async def find_active_order_for_link_async(
    db: Any,
    link: object,
) -> dict[str, Any] | None:
    """Async variant for aiosqlite connections (dashboard / scheduled jobs)."""
    key = normalize_order_link(link)
    if not key:
        return None

    status_pred = active_link_status_sql_predicate("status")
    async with db.execute(
        f"""
        SELECT id, user_id, service_id, link, status, normalized_link, created_at
        FROM orders
        WHERE normalized_link = ?
          AND {status_pred}
        ORDER BY id ASC
        LIMIT 1
        """,
        (key,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is not None:
        return _row_to_active_hit(row)

    async with db.execute(
        f"""
        SELECT id, user_id, service_id, link, status, normalized_link, created_at
        FROM orders
        WHERE (normalized_link IS NULL OR normalized_link = '')
          AND link IS NOT NULL
          AND TRIM(link) != ''
          AND {status_pred}
        ORDER BY id ASC
        """
    ) as cursor:
        rows = await cursor.fetchall()
    for candidate in rows:
        stored = candidate["link"] if hasattr(candidate, "keys") else candidate[3]
        if normalize_order_link(stored) == key:
            return _row_to_active_hit(candidate)
    return None


def _row_to_active_hit(row: Any) -> dict[str, Any]:
    get = row.__getitem__
    keys = set(row.keys()) if hasattr(row, "keys") else None

    def _col(name: str, idx: int) -> Any:
        if keys is not None:
            return get(name)
        return get(idx)

    return {
        "id": int(_col("id", 0)),
        "user_id": int(_col("user_id", 1)),
        "service_id": str(_col("service_id", 2) or ""),
        "link": str(_col("link", 3) or ""),
        "status": str(_col("status", 4) or ""),
        "normalized_link": (
            str(_col("normalized_link", 5) or "")
            if keys is None or "normalized_link" in keys
            else ""
        ),
        "created_at": str(_col("created_at", 6) or ""),
    }
