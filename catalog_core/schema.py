# -*- coding: utf-8 -*-
"""New Soldium Catalog core schema (NOT Catalog v2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SOLDIUM_CATALOG_SERVICES_DDL = """
CREATE TABLE IF NOT EXISTS soldium_catalog_services (
    id TEXT PRIMARY KEY,
    name_ar TEXT NOT NULL DEFAULT '',
    note_ar TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'active', 'archived')),
    service_type TEXT NOT NULL DEFAULT 'other',
    ordering_mode TEXT NOT NULL DEFAULT 'quantity_based'
        CHECK (ordering_mode IN ('quantity_based', 'package_based')),
    min_quantity INTEGER NOT NULL DEFAULT 1,
    max_quantity INTEGER NOT NULL DEFAULT 1000000,
    -- Phase 9B.7 — order behavior / target policy (draft); frozen at publication.
    fulfillment_mode TEXT NOT NULL DEFAULT 'auto'
        CHECK (fulfillment_mode IN ('auto', 'admin')),
    target_platform_key TEXT,
    target_section_key TEXT,
    target_subsection_key TEXT,
    target_link_prompt_key TEXT,
    target_link_type TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

SOLDIUM_CATALOG_NODES_DDL = """
CREATE TABLE IF NOT EXISTS soldium_catalog_nodes (
    id TEXT PRIMARY KEY,
    name_ar TEXT NOT NULL DEFAULT '',
    note_ar TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

SOLDIUM_CATALOG_ENTRIES_DDL = """
CREATE TABLE IF NOT EXISTS soldium_catalog_entries (
    id TEXT PRIMARY KEY,
    parent_entry_id TEXT,
    entry_type TEXT NOT NULL CHECK (entry_type IN ('node', 'service')),
    node_id TEXT,
    service_id TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (parent_entry_id) REFERENCES soldium_catalog_entries(id),
    FOREIGN KEY (node_id) REFERENCES soldium_catalog_nodes(id),
    FOREIGN KEY (service_id) REFERENCES soldium_catalog_services(id),
    CHECK (
        (entry_type = 'node' AND node_id IS NOT NULL AND service_id IS NULL)
        OR (entry_type = 'service' AND service_id IS NOT NULL AND node_id IS NULL)
    )
);
"""

SOLDIUM_CATALOG_EXECUTION_SOURCES_DDL = """
CREATE TABLE IF NOT EXISTS soldium_catalog_execution_sources (
    id TEXT PRIMARY KEY,
    service_id TEXT NOT NULL,
    provider_slug TEXT NOT NULL,
    provider_account_key TEXT NOT NULL,
    external_service_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'historical')),
    assigned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (service_id) REFERENCES soldium_catalog_services(id),
    CHECK (length(trim(external_service_id)) > 0),
    CHECK (
        (status = 'active' AND ended_at IS NULL)
        OR (status = 'historical' AND ended_at IS NOT NULL)
    )
);
"""

# Phase 9G.1 — append-only audit events for execution-source replacements.
# Complements soldium_catalog_execution_sources history (active/historical rows).
SOLDIUM_CATALOG_EXECUTION_SOURCE_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS soldium_catalog_execution_source_events (
    id TEXT PRIMARY KEY,
    service_id TEXT NOT NULL,
    operation TEXT NOT NULL DEFAULT 'change_execution_source',
    previous_provider_slug TEXT,
    previous_provider_account_key TEXT,
    previous_external_service_id TEXT,
    new_provider_slug TEXT NOT NULL,
    new_provider_account_key TEXT NOT NULL,
    new_external_service_id TEXT NOT NULL,
    actor TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (service_id) REFERENCES soldium_catalog_services(id),
    CHECK (length(trim(new_external_service_id)) > 0)
);
"""

SOLDIUM_CATALOG_PRICES_DDL = """
CREATE TABLE IF NOT EXISTS soldium_catalog_prices (
    id TEXT PRIMARY KEY,
    service_id TEXT NOT NULL,
    amount_millimes INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'MAD',
    pricing_mode TEXT NOT NULL
        CHECK (pricing_mode IN ('per_1000', 'per_unit', 'fixed_package')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'historical')),
    effective_from TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    effective_to TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (service_id) REFERENCES soldium_catalog_services(id),
    CHECK (amount_millimes > 0),
    CHECK (currency = 'MAD'),
    CHECK (
        (status = 'active' AND effective_to IS NULL)
        OR (status = 'historical' AND effective_to IS NOT NULL)
    )
);
"""

# Phase 6B — Provider Catalog snapshots (NOT SOLDIUM Catalog services).
# Provider discovery failure must never be treated as a successful empty catalog.
# external_service_id is an opaque Provider identifier, not a SOLDIUM Service id.
SOLDIUM_PROVIDER_CATALOG_SNAPSHOTS_DDL = """
CREATE TABLE IF NOT EXISTS soldium_provider_catalog_snapshots (
    id TEXT PRIMARY KEY,
    provider_slug TEXT NOT NULL,
    provider_account_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    discovered_at TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message_ar TEXT,
    error_detail TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (status = 'success' AND error_code IS NULL)
        OR (status = 'failed' AND error_code IS NOT NULL)
    )
);
"""

SOLDIUM_PROVIDER_CATALOG_SNAPSHOT_ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS soldium_provider_catalog_snapshot_items (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    external_service_id TEXT NOT NULL,
    provider_service_name TEXT,
    provider_category TEXT,
    provider_type TEXT,
    provider_description TEXT,
    min_quantity INTEGER,
    max_quantity INTEGER,
    provider_rate REAL,
    refill INTEGER,
    cancel INTEGER,
    dripfeed INTEGER,
    FOREIGN KEY (snapshot_id) REFERENCES soldium_provider_catalog_snapshots(id)
        ON DELETE CASCADE,
    CHECK (length(trim(external_service_id)) > 0),
    CHECK (refill IS NULL OR refill IN (0, 1)),
    CHECK (cancel IS NULL OR cancel IN (0, 1)),
    CHECK (dripfeed IS NULL OR dripfeed IN (0, 1)),
    UNIQUE (snapshot_id, external_service_id)
);
"""

# Phase 6D — explicit Provider ↔ SOLDIUM service mapping (NOT execution source).
# Mapping is an administrative relationship, not identity and not fulfillment routing.
SOLDIUM_PROVIDER_SERVICE_MAPPINGS_DDL = """
CREATE TABLE IF NOT EXISTS soldium_provider_service_mappings (
    id TEXT PRIMARY KEY,
    provider_slug TEXT NOT NULL,
    provider_account_key TEXT NOT NULL,
    external_service_id TEXT NOT NULL,
    soldium_service_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'historical')),
    mapped_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (soldium_service_id) REFERENCES soldium_catalog_services(id),
    CHECK (length(trim(external_service_id)) > 0),
    CHECK (
        (status = 'active' AND ended_at IS NULL)
        OR (status = 'historical' AND ended_at IS NOT NULL)
    )
);
"""

# Phase 7 — immutable publication history (source of truth for published snapshots).
# event_type=publish stores the customer-facing snapshot; unpublish ends eligibility
# without deleting prior publish rows. No stored readiness flag.
#
# parent_entry_id (optional on publish rows) is historical context only.
# Customer consumers MUST resolve placement from location_path_json, never by
# walking the live Catalog tree via parent_entry_id.
SOLDIUM_CATALOG_PUBLICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS soldium_catalog_publications (
    id TEXT PRIMARY KEY,
    service_id TEXT NOT NULL,
    event_type TEXT NOT NULL
        CHECK (event_type IN ('publish', 'unpublish')),
    name_ar TEXT,
    note_ar TEXT,
    service_type TEXT,
    ordering_mode TEXT,
    min_quantity INTEGER,
    max_quantity INTEGER,
    amount_millimes INTEGER,
    currency TEXT,
    pricing_mode TEXT,
    provider_slug TEXT,
    provider_account_key TEXT,
    external_service_id TEXT,
    -- Phase 9B.7 — frozen order behavior / target policy (NULL on historical rows).
    fulfillment_mode TEXT,
    target_platform_key TEXT,
    target_section_key TEXT,
    target_subsection_key TEXT,
    target_link_prompt_key TEXT,
    target_link_type TEXT,
    location_path_json TEXT NOT NULL DEFAULT '[]',
    -- Historical only; NOT authoritative for live tree structure (use location_path_json).
    parent_entry_id TEXT,
    content_fingerprint TEXT,
    published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_by TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (service_id) REFERENCES soldium_catalog_services(id),
    CHECK (
        event_type = 'unpublish'
        OR (
            event_type = 'publish'
            AND name_ar IS NOT NULL
            AND amount_millimes IS NOT NULL
            AND amount_millimes > 0
            AND currency IS NOT NULL
            AND pricing_mode IS NOT NULL
            AND provider_slug IS NOT NULL
            AND provider_account_key IS NOT NULL
            AND external_service_id IS NOT NULL
            AND length(trim(external_service_id)) > 0
            AND content_fingerprint IS NOT NULL
            AND length(trim(content_fingerprint)) > 0
        )
    )
);
"""

SOLDIUM_CATALOG_INDEXES = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_soldium_catalog_entries_node
    ON soldium_catalog_entries (node_id)
    WHERE node_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_soldium_catalog_entries_service
    ON soldium_catalog_entries (service_id)
    WHERE service_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_catalog_entries_parent_sort
    ON soldium_catalog_entries (parent_entry_id, sort_order, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_catalog_services_status_name
    ON soldium_catalog_services (status, name_ar)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_catalog_services_type
    ON soldium_catalog_services (service_type)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_catalog_services_ordering_mode
    ON soldium_catalog_services (ordering_mode)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_catalog_nodes_status_name
    ON soldium_catalog_nodes (status, name_ar)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_soldium_catalog_exec_active_service
    ON soldium_catalog_execution_sources (service_id)
    WHERE status = 'active'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_catalog_exec_service_assigned
    ON soldium_catalog_execution_sources (service_id, assigned_at DESC, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_catalog_exec_external_id
    ON soldium_catalog_execution_sources (external_service_id, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_catalog_exec_events_service
    ON soldium_catalog_execution_source_events (service_id, created_at DESC, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_catalog_exec_provider
    ON soldium_catalog_execution_sources (provider_slug, provider_account_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_catalog_exec_external
    ON soldium_catalog_execution_sources (external_service_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_soldium_catalog_prices_active_service
    ON soldium_catalog_prices (service_id)
    WHERE status = 'active'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_catalog_prices_service_from
    ON soldium_catalog_prices (service_id, effective_from DESC, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_catalog_prices_mode
    ON soldium_catalog_prices (pricing_mode)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_pcs_account_discovered
    ON soldium_provider_catalog_snapshots (
        provider_slug, provider_account_key, discovered_at DESC, id DESC
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_pcs_status_account
    ON soldium_provider_catalog_snapshots (
        provider_slug, provider_account_key, status, discovered_at DESC, id DESC
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_pcs_items_snapshot_ext
    ON soldium_provider_catalog_snapshot_items (snapshot_id, external_service_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_soldium_psm_active_provider_identity
    ON soldium_provider_service_mappings (
        provider_slug, provider_account_key, external_service_id
    )
    WHERE status = 'active'
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_soldium_psm_active_soldium_service
    ON soldium_provider_service_mappings (soldium_service_id)
    WHERE status = 'active'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_psm_soldium_history
    ON soldium_provider_service_mappings (soldium_service_id, mapped_at DESC, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_psm_provider_history
    ON soldium_provider_service_mappings (
        provider_slug, provider_account_key, external_service_id, mapped_at DESC, id
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_publications_service_time
    ON soldium_catalog_publications (service_id, published_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_publications_service_event
    ON soldium_catalog_publications (service_id, event_type, published_at DESC)
    """,
)

# Marker for migration bookkeeping (optional meta — not catalog_meta v2).
SOLDIUM_CATALOG_META_DDL = """
CREATE TABLE IF NOT EXISTS soldium_catalog_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

# Phase 8D — legacy → Catalog migration bridges (parallel catalog; legacy untouched).
SOLDIUM_CATALOG_LEGACY_BRIDGE_DDL = """
CREATE TABLE IF NOT EXISTS soldium_catalog_legacy_bridge (
    legacy_catalog_id TEXT PRIMARY KEY,
    legacy_local_item_id TEXT NOT NULL,
    legacy_service_id TEXT NOT NULL,
    soldium_service_id TEXT NOT NULL UNIQUE,
    provider_slug TEXT,
    external_service_id TEXT,
    provider_api_account TEXT,
    legacy_fulfillment_mode TEXT,
    classification TEXT NOT NULL,
    review_codes TEXT NOT NULL DEFAULT '[]',
    migration_batch_id TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (soldium_service_id) REFERENCES soldium_catalog_services(id)
);
"""

SOLDIUM_CATALOG_LEGACY_NODE_BRIDGE_DDL = """
CREATE TABLE IF NOT EXISTS soldium_catalog_legacy_node_bridge (
    legacy_node_key TEXT PRIMARY KEY,
    soldium_node_id TEXT NOT NULL UNIQUE,
    soldium_entry_id TEXT NOT NULL UNIQUE,
    migration_batch_id TEXT NOT NULL,
    FOREIGN KEY (soldium_node_id) REFERENCES soldium_catalog_nodes(id),
    FOREIGN KEY (soldium_entry_id) REFERENCES soldium_catalog_entries(id)
);
"""

SOLDIUM_CATALOG_LEGACY_MIGRATION_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS soldium_catalog_legacy_migration_runs (
    migration_batch_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    plan_fingerprint TEXT NOT NULL,
    report_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

SOLDIUM_CATALOG_LEGACY_BRIDGE_INDEXES = (
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_legacy_bridge_soldium
    ON soldium_catalog_legacy_bridge (soldium_service_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_legacy_bridge_batch
    ON soldium_catalog_legacy_bridge (migration_batch_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_legacy_bridge_local
    ON soldium_catalog_legacy_bridge (legacy_local_item_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_soldium_legacy_node_bridge_batch
    ON soldium_catalog_legacy_node_bridge (migration_batch_id)
    """,
)

SOLDIUM_CATALOG_SCHEMA_VERSION = "10"

# Phase 4A / 9B.7 — additive columns for DBs created before commercial / order contract.
_SOLDIUM_CATALOG_SERVICE_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("service_type", "TEXT NOT NULL DEFAULT 'other'"),
    ("ordering_mode", "TEXT NOT NULL DEFAULT 'quantity_based'"),
    ("min_quantity", "INTEGER NOT NULL DEFAULT 1"),
    ("max_quantity", "INTEGER NOT NULL DEFAULT 1000000"),
    ("fulfillment_mode", "TEXT NOT NULL DEFAULT 'auto'"),
    ("target_platform_key", "TEXT"),
    ("target_section_key", "TEXT"),
    ("target_subsection_key", "TEXT"),
    ("target_link_prompt_key", "TEXT"),
    ("target_link_type", "TEXT"),
)

_SOLDIUM_CATALOG_PUBLICATION_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("fulfillment_mode", "TEXT"),
    ("target_platform_key", "TEXT"),
    ("target_section_key", "TEXT"),
    ("target_subsection_key", "TEXT"),
    ("target_link_prompt_key", "TEXT"),
    ("target_link_type", "TEXT"),
)


def _ensure_table_columns(
    connection: sqlite3.Connection,
    table: str,
    migrations: tuple[tuple[str, str], ...],
) -> None:
    existing = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, col_ddl in migrations:
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_ddl}")


def _ensure_soldium_catalog_service_commercial_columns(
    connection: sqlite3.Connection,
) -> None:
    _ensure_table_columns(
        connection,
        "soldium_catalog_services",
        _SOLDIUM_CATALOG_SERVICE_COLUMN_MIGRATIONS,
    )


def _ensure_soldium_catalog_publication_order_contract_columns(
    connection: sqlite3.Connection,
) -> None:
    _ensure_table_columns(
        connection,
        "soldium_catalog_publications",
        _SOLDIUM_CATALOG_PUBLICATION_COLUMN_MIGRATIONS,
    )


def ensure_soldium_catalog_schema(connection: sqlite3.Connection) -> None:
    """Create Catalog core tables. Never touches Catalog-v2 / smm_services / orders / providers."""
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(SOLDIUM_CATALOG_SERVICES_DDL)
    connection.execute(SOLDIUM_CATALOG_NODES_DDL)
    connection.execute(SOLDIUM_CATALOG_ENTRIES_DDL)
    connection.execute(SOLDIUM_CATALOG_EXECUTION_SOURCES_DDL)
    connection.execute(SOLDIUM_CATALOG_EXECUTION_SOURCE_EVENTS_DDL)
    connection.execute(SOLDIUM_CATALOG_PRICES_DDL)
    connection.execute(SOLDIUM_PROVIDER_CATALOG_SNAPSHOTS_DDL)
    connection.execute(SOLDIUM_PROVIDER_CATALOG_SNAPSHOT_ITEMS_DDL)
    connection.execute(SOLDIUM_PROVIDER_SERVICE_MAPPINGS_DDL)
    connection.execute(SOLDIUM_CATALOG_PUBLICATIONS_DDL)
    connection.execute(SOLDIUM_CATALOG_META_DDL)
    connection.execute(SOLDIUM_CATALOG_LEGACY_BRIDGE_DDL)
    connection.execute(SOLDIUM_CATALOG_LEGACY_NODE_BRIDGE_DDL)
    connection.execute(SOLDIUM_CATALOG_LEGACY_MIGRATION_RUNS_DDL)
    _ensure_soldium_catalog_service_commercial_columns(connection)
    _ensure_soldium_catalog_publication_order_contract_columns(connection)
    for ddl in SOLDIUM_CATALOG_INDEXES:
        connection.execute(ddl)
    for ddl in SOLDIUM_CATALOG_LEGACY_BRIDGE_INDEXES:
        connection.execute(ddl)
    connection.execute(
        """
        INSERT INTO soldium_catalog_schema_meta(key, value)
        VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (SOLDIUM_CATALOG_SCHEMA_VERSION,),
    )


def ensure_soldium_catalog_at_path(db_path: Path | str) -> None:
    path = Path(db_path)
    connection = sqlite3.connect(str(path))
    try:
        connection.row_factory = sqlite3.Row
        ensure_soldium_catalog_schema(connection)
        connection.commit()
    finally:
        connection.close()
