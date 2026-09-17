"""Shared SQLite schema helpers for the admin dashboard."""
from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from database_connector import get_db

logger = logging.getLogger(__name__)

# Top-level short names that exist in both soldium-bot and soldium-dashboard.
# Loading bot.database while dashboard modules occupy these names shadows bot config/utils.
_BOT_COLLIDING_TOP_LEVEL = frozenset({"config", "utils", "services", "database", "storefront"})


class SharedBotMigrationError(RuntimeError):
    """Required shared bot ``init_db()`` migration failed — startup must not continue."""


class RequiredBotSchemaError(RuntimeError):
    """Shared bot-owned schema is missing or incomplete; dashboard cannot start safely."""


def _is_bot_colliding_module(name: str) -> bool:
    return name.split(".", 1)[0] in _BOT_COLLIDING_TOP_LEVEL


@contextmanager
def _bot_import_isolation(bot_root: Path) -> Iterator[None]:
    """Prefer bot-root imports for colliding short names, then restore dashboard modules.

    Stashes and restores only known colliding packages so dashboard ``config`` / ``utils``
    remain available after the shared migration. Bot ``database`` binds its config constants
    at import time inside this window; callers should invoke ``init_db()`` while isolated.
    """
    root_str = str(bot_root.resolve())
    saved: dict[str, object] = {}
    for name in list(sys.modules):
        if _is_bot_colliding_module(name):
            saved[name] = sys.modules.pop(name)

    path_inserted = False
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
        path_inserted = True
    try:
        yield
    finally:
        for name in list(sys.modules):
            if _is_bot_colliding_module(name):
                sys.modules.pop(name, None)
        sys.modules.update(saved)
        if path_inserted:
            try:
                sys.path.remove(root_str)
            except ValueError:
                pass


def load_bot_database_module():
    """Import soldium-bot ``database`` without dashboard ``config`` shadowing bot config."""
    bot_root = Path(__file__).resolve().parent.parent / "soldium-bot"
    if not bot_root.is_dir():
        raise SharedBotMigrationError(f"soldium-bot root not found: {bot_root}")
    with _bot_import_isolation(bot_root):
        import database as bot_db  # intentional: load bot package under isolation

        return bot_db


# Legacy DDL strings retained for test fixtures / docs. Bot ``init_db()`` owns these
# tables at runtime; dashboard startup no longer executes PROVIDERS_*/SMM_* DDL.
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

SCHEDULED_ORDERS_DDL = """
CREATE TABLE IF NOT EXISTS scheduled_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    template_order_id INTEGER,
    user_id INTEGER NOT NULL,
    service_id TEXT NOT NULL,
    service_name TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL,
    provider_slug TEXT NOT NULL DEFAULT 'gozibra',
    api_account TEXT NOT NULL DEFAULT 'default',
    fulfillment_mode TEXT NOT NULL DEFAULT 'auto',
    -- Phase 9B.8: NULL = Gen-0 (live SKU lookup); non-NULL = Gen-1 frozen identity.
    external_service_id TEXT,
    quantity_mode TEXT NOT NULL DEFAULT 'fixed',
    quantity_fixed INTEGER,
    quantity_min INTEGER,
    quantity_max INTEGER,
    interval_days INTEGER NOT NULL DEFAULT 1,
    next_run_at TEXT NOT NULL,
    last_run_at TEXT,
    last_created_order_id INTEGER,
    runs_count INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    stopped_at TEXT
);
"""

SCHEDULED_ORDERS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_scheduled_orders_due
ON scheduled_orders (status, next_run_at);
"""

_SCHEDULED_ORDERS_EXTERNAL_ID_MIGRATION = (
    "ALTER TABLE scheduled_orders ADD COLUMN external_service_id TEXT"
)

SCHEDULED_ORDER_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS scheduled_order_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scheduled_order_id INTEGER NOT NULL,
    order_id INTEGER,
    quantity_used INTEGER,
    status TEXT NOT NULL,
    error_message TEXT,
    ran_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (scheduled_order_id) REFERENCES scheduled_orders(id)
);
"""

SCHEDULED_ORDER_RUNS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_scheduled_order_runs_job
ON scheduled_order_runs (scheduled_order_id, ran_at DESC);
"""


async def _table_columns(db, table: str) -> set[str]:
    async with db.execute(f"PRAGMA table_info([{table}])") as cursor:
        rows = await cursor.fetchall()
    return {str(row[1]) for row in rows}


def list_pending_bot_migrations() -> list[str]:
    """Read-only list of pending bot-owned ``init_db`` migrations for the shared DB."""
    from database_connector import DB_PATH

    bot_root = Path(__file__).resolve().parent.parent / "soldium-bot"
    if not bot_root.is_dir():
        raise RequiredBotSchemaError(f"soldium-bot root not found: {bot_root}")
    with _bot_import_isolation(bot_root):
        import database as bot_db  # intentional: load bot package under isolation

        return list(bot_db.pending_init_db_migrations(db_path=Path(DB_PATH)))


def verify_required_bot_schema() -> None:
    """Fail closed when required bot-owned schema is missing (read-only; no mutations)."""
    from database_connector import DB_PATH

    pending = list_pending_bot_migrations()
    if not pending:
        return
    preview = ", ".join(pending[:12])
    more = f" (+{len(pending) - 12} more)" if len(pending) > 12 else ""
    raise RequiredBotSchemaError(
        "Required bot-owned schema is incomplete for shared database "
        f"{DB_PATH}. Pending migrations: {preview}{more}. "
        "Start soldium-bot once (or ensure bot database.init_db() has run) "
        "before starting the dashboard. Dashboard no longer ALTERs bot tables itself."
    )


def _load_bot_migration_lock_module():
    """Load soldium-bot ``migration_lock`` by path (no short-name config collision)."""
    import importlib.util

    bot_root = Path(__file__).resolve().parent.parent / "soldium-bot"
    module_path = bot_root / "migration_lock.py"
    if not module_path.is_file():
        raise SharedBotMigrationError(f"migration_lock module not found: {module_path}")
    # Unique module name so dashboard ``sys.modules`` stays clean across reloads.
    mod_name = "soldium_bot_migration_lock"
    existing = sys.modules.get(mod_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    if spec is None or spec.loader is None:
        raise SharedBotMigrationError(f"Unable to load migration lock from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run_shared_bot_migrations(*, already_holding_migration_lock: bool = False) -> None:
    """Apply shared soldium-bot ``init_db()`` on the shared users.db.

    Phase 11: bot owns these migrations. Dashboard invokes this only as an explicit
    operational bridge when the shared DB is incomplete and bot-first startup is not
    guaranteed.

    When ``already_holding_migration_lock`` is True (Phase 12 dashboard path), call the
    unlocked critical section so we do not deadlock on a second import of
    ``migration_lock`` under bot import isolation.
    """
    from database_connector import DB_PATH

    try:
        bot_root = Path(__file__).resolve().parent.parent / "soldium-bot"
        if not bot_root.is_dir():
            raise SharedBotMigrationError(f"soldium-bot root not found: {bot_root}")
        with _bot_import_isolation(bot_root):
            import database as bot_db  # intentional: load bot package under isolation

            bot_db.DB_PATH = Path(DB_PATH)
            if already_holding_migration_lock:
                bot_db._init_db_under_migration_lock()
            else:
                bot_db.init_db()
    except SharedBotMigrationError:
        raise
    except Exception as exc:
        logger.exception(
            "CRITICAL: shared bot database migration (init_db) failed for %s",
            DB_PATH,
        )
        raise SharedBotMigrationError(
            f"Shared bot init_db() failed for {DB_PATH}: {exc}"
        ) from exc


async def ensure_shared_bot_schema() -> None:
    """Ensure bot-owned shared schema exists without dashboard duplicating bot ALTERs.

    Ownership: soldium-bot ``database.init_db()`` owns users/orders/deposits/smm_services/
    providers and related tables. Dashboard does not independently ALTER those tables.

    Phase 12: when pending migrations are detected, acquire the shared migration lock,
    **re-check** pending state, then bridge to bot ``init_db()`` only if still needed.

    When nothing is pending, still run read-only :func:`verify_required_bot_schema`
    so an under-reported detector cannot leave startup looking healthy.
    """
    from database_connector import DB_PATH

    pending = list_pending_bot_migrations()
    if not pending:
        logger.info("Shared bot schema already complete; skipping bot init_db()")
        verify_required_bot_schema()
        return

    logger.warning(
        "Shared bot schema incomplete (%s pending); waiting for migration lock "
        "before bridge. Pending sample: %s",
        len(pending),
        pending[:8],
    )
    ml = _load_bot_migration_lock_module()
    try:
        with ml.migration_lock(
            db_path=Path(DB_PATH),
            holder="soldium-dashboard",
        ):
            pending_after = list_pending_bot_migrations()
            if not pending_after:
                logger.info(
                    "Shared bot schema became complete while waiting for migration "
                    "lock; skipping bot init_db()"
                )
                verify_required_bot_schema()
                return
            logger.warning(
                "Migration lock held; applying bot init_db() bridge "
                "(%s still pending). Sample: %s",
                len(pending_after),
                pending_after[:8],
            )
            _run_shared_bot_migrations(already_holding_migration_lock=True)
            verify_required_bot_schema()
    except ml.MigrationLockTimeout as exc:
        logger.error("Dashboard could not acquire migration lock: %s", exc)
        raise SharedBotMigrationError(str(exc)) from exc


async def ensure_smm_services_table() -> None:
    """Compatibility alias: bot owns ``smm_services`` / providers / orders schema.

    Historically this function CREATEd/ALTERed bot tables from the dashboard. That
    duplication is removed; callers now go through :func:`ensure_shared_bot_schema`.
    """
    await ensure_shared_bot_schema()


async def ensure_timed_announcements_tables() -> None:
    """Dashboard ensure for timed announcement UI tables (CREATE IF NOT EXISTS only).

    Column upgrades such as ``auto_delete_seconds`` are bot-owned via ``init_db()`` /
    :func:`ensure_shared_bot_schema` — dashboard does not ALTER them here.
    """
    async with get_db() as db:
        await db.execute(TIMED_ANNOUNCEMENTS_DDL)
        await db.execute(TIMED_ANNOUNCEMENT_DISMISSALS_DDL)
        await db.execute(TIMED_ANNOUNCEMENTS_INDEX)
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


async def ensure_scheduled_orders_tables() -> None:
    async with get_db() as db:
        await db.execute(SCHEDULED_ORDERS_DDL)
        await db.execute(SCHEDULED_ORDERS_INDEX)
        await db.execute(SCHEDULED_ORDER_RUNS_DDL)
        await db.execute(SCHEDULED_ORDER_RUNS_INDEX)
        job_cols = await _table_columns(db, "scheduled_orders")
        if "external_service_id" not in job_cols:
            await db.execute(_SCHEDULED_ORDERS_EXTERNAL_ID_MIGRATION)
        await db.commit()


async def ensure_soldium_catalog_tables() -> None:
    """Phase 2–4A Catalog core tables (soldium_catalog_*). Does not touch Catalog v2 / providers."""
    from catalog_core.schema import ensure_soldium_catalog_at_path
    from database_connector import DB_PATH

    # Sync ensure is idempotent: creates tables, additive commercial columns, indexes, schema_version.
    ensure_soldium_catalog_at_path(DB_PATH)
