"""Shared SQLite schema helpers for the admin dashboard."""
from __future__ import annotations

from database_connector import get_db

PROVIDERS_DDL = """
CREATE TABLE IF NOT EXISTS providers (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    api_base_url TEXT NOT NULL DEFAULT '',
    adapter_type TEXT NOT NULL DEFAULT 'gozibra_v2',
    is_active INTEGER NOT NULL DEFAULT 1
);
"""

PROVIDER_ACCOUNTS_DDL = """
CREATE TABLE IF NOT EXISTS provider_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_slug TEXT NOT NULL,
    account_key TEXT NOT NULL DEFAULT 'default',
    api_key_env TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(provider_slug, account_key),
    FOREIGN KEY(provider_slug) REFERENCES providers(slug)
);
"""

PROVIDER_ACCOUNTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_provider_accounts_slug
ON provider_accounts (provider_slug, is_active);
"""

SMM_SERVICES_DDL = """
CREATE TABLE IF NOT EXISTS smm_services (
    service_id TEXT PRIMARY KEY,
    category TEXT NOT NULL DEFAULT '',
    name_ar TEXT NOT NULL DEFAULT '',
    provider_price_usd REAL NOT NULL DEFAULT 0,
    local_price_dh REAL NOT NULL DEFAULT 0,
    min_qty INTEGER NOT NULL DEFAULT 1,
    max_qty INTEGER NOT NULL DEFAULT 1000000,
    is_active INTEGER NOT NULL DEFAULT 1,
    platform_key TEXT NOT NULL DEFAULT '',
    section_key TEXT,
    subsection_key TEXT,
    local_item_id TEXT NOT NULL DEFAULT '',
    platform_title TEXT NOT NULL DEFAULT '',
    section_title TEXT,
    subsection_title TEXT,
    fulfillment_mode TEXT NOT NULL DEFAULT 'auto',
    provider_api_account TEXT,
    provider_price_updated_at TEXT,
    provider_slug TEXT NOT NULL DEFAULT 'gozibra'
);
"""

SMM_SERVICES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_smm_services_active
ON smm_services (is_active, platform_key);
"""

TIMED_ANNOUNCEMENTS_DDL = """
CREATE TABLE IF NOT EXISTS timed_announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_html TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    launched_at TEXT,
    stopped_at TEXT
);
"""

TIMED_ANNOUNCEMENT_DISMISSALS_DDL = """
CREATE TABLE IF NOT EXISTS timed_announcement_dismissals (
    announcement_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    dismissed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (announcement_id, user_id),
    FOREIGN KEY (announcement_id) REFERENCES timed_announcements(id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
"""

TIMED_ANNOUNCEMENTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_timed_announcements_status_ends
ON timed_announcements (status, ends_at);
"""

SCHEDULED_DELETIONS_DDL = """
CREATE TABLE IF NOT EXISTS scheduled_message_deletions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    delete_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

SCHEDULED_DELETIONS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_scheduled_deletions_delete_at
ON scheduled_message_deletions (delete_at);
"""

ADMIN_ALERTS_DDL = """
CREATE TABLE IF NOT EXISTS admin_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'warning',
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT,
    fingerprint TEXT NOT NULL UNIQUE,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'open',
    dismissed_at TEXT,
    telegram_notified INTEGER NOT NULL DEFAULT 0
);
"""

ADMIN_ALERTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_admin_alerts_status
ON admin_alerts (status, severity, last_seen_at);
"""

ADMIN_NOTIFICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS admin_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    title TEXT NOT NULL,
    body_html TEXT NOT NULL DEFAULT '',
    body_plain TEXT NOT NULL DEFAULT '',
    entity_type TEXT,
    entity_id TEXT,
    user_id INTEGER,
    source TEXT NOT NULL DEFAULT 'bot',
    channel TEXT NOT NULL DEFAULT 'telegram',
    telegram_sent INTEGER NOT NULL DEFAULT 0,
    telegram_error TEXT,
    is_read INTEGER NOT NULL DEFAULT 0,
    read_at TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

ADMIN_NOTIFICATIONS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_admin_notifications_created
ON admin_notifications (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_notifications_unread
ON admin_notifications (is_read, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_notifications_category
ON admin_notifications (category, created_at DESC);
"""

_ORDERS_STATUS_CHANGED_MIGRATION = (
    "ALTER TABLE orders ADD COLUMN status_changed_at TEXT"
)

_ORDERS_PROVIDER_COST_MIGRATION = (
    "ALTER TABLE orders ADD COLUMN provider_cost_dh REAL NOT NULL DEFAULT 0"
)

_SMM_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
    (
        "fulfillment_mode",
        "ALTER TABLE smm_services ADD COLUMN fulfillment_mode TEXT NOT NULL DEFAULT 'auto'",
    ),
    ("provider_api_account", "ALTER TABLE smm_services ADD COLUMN provider_api_account TEXT"),
    (
        "provider_price_updated_at",
        "ALTER TABLE smm_services ADD COLUMN provider_price_updated_at TEXT",
    ),
    (
        "provider_slug",
        "ALTER TABLE smm_services ADD COLUMN provider_slug TEXT NOT NULL DEFAULT 'gozibra'",
    ),
)

_ORDERS_PROVIDER_SLUG_MIGRATION = (
    "ALTER TABLE orders ADD COLUMN provider_slug TEXT NOT NULL DEFAULT 'gozibra'"
)


async def _table_columns(db, table: str) -> set[str]:
    async with db.execute(f"PRAGMA table_info([{table}])") as cursor:
        rows = await cursor.fetchall()
    return {str(row[1]) for row in rows}


async def ensure_smm_services_table() -> None:
    async with get_db() as db:
        await db.execute(PROVIDERS_DDL)
        await db.execute(PROVIDER_ACCOUNTS_DDL)
        await db.execute(PROVIDER_ACCOUNTS_INDEX)
        pa_cols = await _table_columns(db, "provider_accounts")
        if "display_name" not in pa_cols:
            await db.execute(
                "ALTER TABLE provider_accounts ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"
            )
        await db.execute(SMM_SERVICES_DDL)
        await db.execute(SMM_SERVICES_INDEX)
        cols = await _table_columns(db, "smm_services")
        for col_name, ddl in _SMM_COLUMN_MIGRATIONS:
            if col_name not in cols:
                await db.execute(ddl)
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_smm_services_provider
            ON smm_services (provider_slug, is_active)
            """
        )
        order_cols = await _table_columns(db, "orders")
        if "provider_cost_dh" not in order_cols:
            try:
                await db.execute(_ORDERS_PROVIDER_COST_MIGRATION)
            except Exception:
                pass
        if "provider_slug" not in order_cols:
            try:
                await db.execute(_ORDERS_PROVIDER_SLUG_MIGRATION)
            except Exception:
                pass
        if "status_changed_at" not in order_cols:
            try:
                await db.execute(_ORDERS_STATUS_CHANGED_MIGRATION)
                await db.execute(
                    "UPDATE orders SET status_changed_at = created_at "
                    "WHERE status_changed_at IS NULL"
                )
            except Exception:
                pass
        await _seed_default_gozibra(db)
        await _backfill_account_display_names(db)
        await db.commit()

    _run_shared_bot_migrations()


def _run_shared_bot_migrations() -> None:
    """يطبّق ترحيلات البوت (بما فيها catalog_id) على users.db المشتركة."""
    import sys
    from pathlib import Path

    from database_connector import DB_PATH

    bot_root = Path(__file__).resolve().parent.parent / "soldium-bot"
    root_str = str(bot_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    import database as bot_db

    bot_db.DB_PATH = Path(DB_PATH)
    bot_db.init_db()


async def _seed_default_gozibra(db) -> None:
    import os

    api_url = os.environ.get("API_URL", "https://gozibra.com/api/v2").strip()
    if not api_url:
        return
    has_key = any(
        os.environ.get(name, "").strip()
        for name in (
            "SMM_KEY_DEFAULT",
            "SMM_KEY_INSTAGRAM",
            "SMM_KEY_FACEBOOK",
            "SMM_KEY_TIKTOK",
        )
    )
    if not has_key:
        return

    await db.execute(
        """
        INSERT OR IGNORE INTO providers (slug, name, api_base_url, adapter_type, is_active)
        VALUES ('gozibra', 'Gozibra', ?, 'gozibra_v2', 1)
        """,
        (api_url,),
    )
    for account_key, env_name in (
        ("default", "SMM_KEY_DEFAULT"),
        ("instagram", "SMM_KEY_INSTAGRAM"),
        ("facebook", "SMM_KEY_FACEBOOK"),
        ("tiktok", "SMM_KEY_TIKTOK"),
    ):
        if not os.environ.get(env_name, "").strip():
            continue
        await db.execute(
            """
            INSERT OR IGNORE INTO provider_accounts
                (provider_slug, account_key, api_key_env, display_name, is_active)
            VALUES ('gozibra', ?, ?, ?, 1)
            """,
            (account_key, env_name, _default_account_label(account_key)),
        )


def _default_account_label(account_key: str) -> str:
    from services.provider_registry import default_display_name_for_account

    return default_display_name_for_account(account_key)


async def _backfill_account_display_names(db) -> None:
    async with db.execute(
        "SELECT id, account_key, display_name FROM provider_accounts"
    ) as cursor:
        rows = await cursor.fetchall()
    for row in rows:
        current = str(row["display_name"] or "").strip()
        if current:
            continue
        label = _default_account_label(str(row["account_key"]))
        await db.execute(
            "UPDATE provider_accounts SET display_name = ? WHERE id = ?",
            (label, int(row["id"])),
        )


async def ensure_timed_announcements_tables() -> None:
    async with get_db() as db:
        await db.execute(TIMED_ANNOUNCEMENTS_DDL)
        await db.execute(TIMED_ANNOUNCEMENT_DISMISSALS_DDL)
        await db.execute(TIMED_ANNOUNCEMENTS_INDEX)
        ta_cols = await _table_columns(db, "timed_announcements")
        if "auto_delete_seconds" not in ta_cols:
            await db.execute(
                "ALTER TABLE timed_announcements ADD COLUMN auto_delete_seconds INTEGER"
            )
        await db.execute(SCHEDULED_DELETIONS_DDL)
        await db.execute(SCHEDULED_DELETIONS_INDEX)
        await db.commit()


async def ensure_admin_alerts_table() -> None:
    async with get_db() as db:
        await db.execute(ADMIN_ALERTS_DDL)
        await db.execute(ADMIN_ALERTS_INDEX)
        await db.commit()


async def ensure_admin_notifications_table() -> None:
    async with get_db() as db:
        await db.execute(ADMIN_NOTIFICATIONS_DDL)
        await db.executescript(ADMIN_NOTIFICATIONS_INDEXES)
        await db.commit()
