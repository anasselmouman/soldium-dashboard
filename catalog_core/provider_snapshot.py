# -*- coding: utf-8 -*-
"""Phase 6B — Provider Catalog snapshots and deterministic diff.

Invariants:
- Provider discovery failure must never be interpreted as a successful empty catalog.
- Provider external_service_id is an opaque Provider identifier, not a SOLDIUM Service id.
- Snapshots never create or modify SOLDIUM Catalog entities.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any

from catalog_core.errors import CatalogValidationError
from catalog_core.provider_discovery import (
    ProviderCatalogItem,
    ProviderDiscoveryResult,
    discover_provider_catalog,
)

logger = logging.getLogger("soldium.catalog.provider_snapshot")

# Fields compared for "changed" (identity keys excluded).
COMPARED_FIELDS: tuple[str, ...] = (
    "provider_service_name",
    "provider_category",
    "provider_type",
    "provider_description",
    "min_quantity",
    "max_quantity",
    "provider_rate",
    "refill",
    "cancel",
    "dripfeed",
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _bool_to_db(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def _bool_from_db(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(int(value))


def assert_unique_external_ids(items: list[ProviderCatalogItem]) -> None:
    """Reject duplicate opaque external IDs — never silently overwrite."""
    seen: set[str] = set()
    for item in items:
        eid = str(item.external_service_id)
        if eid in seen:
            raise CatalogValidationError(
                "اكتشاف المزوّد يحتوي على معرّفات خدمة مكررة ولا يمكن حفظ اللقطة."
            )
        seen.add(eid)


def changed_field_names(
    previous: ProviderCatalogItem, current: ProviderCatalogItem
) -> list[str]:
    changed: list[str] = []
    for name in COMPARED_FIELDS:
        if getattr(previous, name) != getattr(current, name):
            changed.append(name)
    return changed


@dataclass
class ProviderCatalogSnapshot:
    id: str
    provider_slug: str
    provider_account_key: str
    status: str  # success | failed
    discovered_at: str
    item_count: int
    error_code: str | None = None
    error_message_ar: str | None = None
    error_detail: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider_slug": self.provider_slug,
            "provider_account_key": self.provider_account_key,
            "status": self.status,
            "discovered_at": self.discovered_at,
            "item_count": self.item_count,
            "error_code": self.error_code,
            "error_message_ar": self.error_message_ar,
            "error_detail": self.error_detail,
            "created_at": self.created_at,
        }


@dataclass
class ProviderCatalogChangedItem:
    previous: ProviderCatalogItem
    current: ProviderCatalogItem
    changed_fields: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "external_service_id": self.current.external_service_id,
            "previous": self.previous.to_dict(),
            "current": self.current.to_dict(),
            "changed_fields": list(self.changed_fields),
        }


@dataclass
class ProviderCatalogDiff:
    provider_slug: str
    provider_account_key: str
    previous_snapshot_id: str | None
    current_snapshot_id: str | None
    previous_discovered_at: str | None
    current_discovered_at: str | None
    is_baseline: bool
    unchanged: list[ProviderCatalogItem] = field(default_factory=list)
    new: list[ProviderCatalogItem] = field(default_factory=list)
    changed: list[ProviderCatalogChangedItem] = field(default_factory=list)
    missing: list[ProviderCatalogItem] = field(default_factory=list)

    def summary_dict(self) -> dict[str, int | bool]:
        return {
            "is_baseline": self.is_baseline,
            "unchanged": len(self.unchanged),
            "new": len(self.new),
            "changed": len(self.changed),
            "missing": len(self.missing),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_slug": self.provider_slug,
            "provider_account_key": self.provider_account_key,
            "previous_snapshot_id": self.previous_snapshot_id,
            "current_snapshot_id": self.current_snapshot_id,
            "previous_discovered_at": self.previous_discovered_at,
            "current_discovered_at": self.current_discovered_at,
            "is_baseline": self.is_baseline,
            "unchanged": [i.to_dict() for i in self.unchanged],
            "new": [i.to_dict() for i in self.new],
            "changed": [c.to_dict() for c in self.changed],
            "missing": [i.to_dict() for i in self.missing],
            "summary": self.summary_dict(),
        }


@dataclass
class ProviderSnapshotRunResult:
    """Result of discover → snapshot → diff for one Provider Account."""

    ok: bool
    discovery: ProviderDiscoveryResult
    snapshot: ProviderCatalogSnapshot | None = None
    diff: ProviderCatalogDiff | None = None
    previous_snapshot_preserved: bool = False
    message_ar: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "operation": "discover_snapshot_diff",
            "message_ar": self.message_ar,
            "discovery": self.discovery.to_dict(),
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
            "diff": self.diff.to_dict() if self.diff else None,
            "previous_snapshot_preserved": self.previous_snapshot_preserved,
        }


def compute_provider_catalog_diff(
    *,
    provider_slug: str,
    provider_account_key: str,
    previous_snapshot: ProviderCatalogSnapshot | None,
    previous_items: list[ProviderCatalogItem],
    current_snapshot: ProviderCatalogSnapshot,
    current_items: list[ProviderCatalogItem],
) -> ProviderCatalogDiff:
    """Deterministic, order-independent diff of two successful catalogs."""
    if previous_snapshot is None:
        return ProviderCatalogDiff(
            provider_slug=provider_slug,
            provider_account_key=provider_account_key,
            previous_snapshot_id=None,
            current_snapshot_id=current_snapshot.id,
            previous_discovered_at=None,
            current_discovered_at=current_snapshot.discovered_at,
            is_baseline=True,
            unchanged=[],
            new=[],
            changed=[],
            missing=[],
        )

    prev_map = {i.external_service_id: i for i in previous_items}
    curr_map = {i.external_service_id: i for i in current_items}

    unchanged: list[ProviderCatalogItem] = []
    new_items: list[ProviderCatalogItem] = []
    changed: list[ProviderCatalogChangedItem] = []
    missing: list[ProviderCatalogItem] = []

    for eid, curr in curr_map.items():
        prev = prev_map.get(eid)
        if prev is None:
            new_items.append(curr)
            continue
        fields = changed_field_names(prev, curr)
        if fields:
            changed.append(
                ProviderCatalogChangedItem(
                    previous=prev, current=curr, changed_fields=fields
                )
            )
        else:
            unchanged.append(curr)

    for eid, prev in prev_map.items():
        if eid not in curr_map:
            missing.append(prev)

    unchanged.sort(key=lambda i: i.external_service_id)
    new_items.sort(key=lambda i: i.external_service_id)
    changed.sort(key=lambda c: c.current.external_service_id)
    missing.sort(key=lambda i: i.external_service_id)

    return ProviderCatalogDiff(
        provider_slug=provider_slug,
        provider_account_key=provider_account_key,
        previous_snapshot_id=previous_snapshot.id,
        current_snapshot_id=current_snapshot.id,
        previous_discovered_at=previous_snapshot.discovered_at,
        current_discovered_at=current_snapshot.discovered_at,
        is_baseline=False,
        unchanged=unchanged,
        new=new_items,
        changed=changed,
        missing=missing,
    )


class ProviderSnapshotRepository:
    """Persistence for Provider Catalog snapshots (isolated from SOLDIUM Catalog)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get_latest_successful_snapshot(
        self, provider_slug: str, provider_account_key: str
    ) -> ProviderCatalogSnapshot | None:
        snaps = self.get_latest_successful_snapshots(
            provider_slug, provider_account_key, limit=1
        )
        return snaps[0] if snaps else None

    def get_latest_successful_snapshots(
        self,
        provider_slug: str,
        provider_account_key: str,
        *,
        limit: int = 2,
    ) -> list[ProviderCatalogSnapshot]:
        """Latest successful snapshots only — failed attempts never appear here."""
        rows = self.connection.execute(
            """
            SELECT id, provider_slug, provider_account_key, status, discovered_at,
                   item_count, error_code, error_message_ar, error_detail, created_at
            FROM soldium_provider_catalog_snapshots
            WHERE provider_slug = ?
              AND provider_account_key = ?
              AND status = 'success'
            ORDER BY discovered_at DESC, id DESC
            LIMIT ?
            """,
            (provider_slug, provider_account_key, max(1, int(limit))),
        ).fetchall()
        return [self._row_to_snapshot(r) for r in rows]

    def list_accounts_with_successful_snapshots(self) -> list[tuple[str, str]]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT provider_slug, provider_account_key
            FROM soldium_provider_catalog_snapshots
            WHERE status = 'success'
            ORDER BY provider_slug ASC, provider_account_key ASC
            """
        ).fetchall()
        return [
            (str(r["provider_slug"]), str(r["provider_account_key"])) for r in rows
        ]

    def list_snapshots(
        self,
        provider_slug: str,
        provider_account_key: str,
        *,
        limit: int = 50,
    ) -> list[ProviderCatalogSnapshot]:
        rows = self.connection.execute(
            """
            SELECT id, provider_slug, provider_account_key, status, discovered_at,
                   item_count, error_code, error_message_ar, error_detail, created_at
            FROM soldium_provider_catalog_snapshots
            WHERE provider_slug = ?
              AND provider_account_key = ?
            ORDER BY discovered_at DESC, id DESC
            LIMIT ?
            """,
            (provider_slug, provider_account_key, limit),
        ).fetchall()
        return [self._row_to_snapshot(r) for r in rows]

    def load_snapshot_items(
        self,
        snapshot_id: str,
        *,
        provider_slug: str,
        provider_account_key: str,
    ) -> list[ProviderCatalogItem]:
        rows = self.connection.execute(
            """
            SELECT external_service_id, provider_service_name, provider_category,
                   provider_type, provider_description, min_quantity, max_quantity,
                   provider_rate, refill, cancel, dripfeed
            FROM soldium_provider_catalog_snapshot_items
            WHERE snapshot_id = ?
            ORDER BY external_service_id ASC
            """,
            (snapshot_id,),
        ).fetchall()
        items: list[ProviderCatalogItem] = []
        for row in rows:
            items.append(
                ProviderCatalogItem(
                    provider_slug=provider_slug,
                    provider_account_key=provider_account_key,
                    external_service_id=str(row["external_service_id"]),
                    provider_service_name=row["provider_service_name"],
                    provider_category=row["provider_category"],
                    provider_type=row["provider_type"],
                    provider_description=row["provider_description"],
                    min_quantity=row["min_quantity"],
                    max_quantity=row["max_quantity"],
                    provider_rate=row["provider_rate"],
                    refill=_bool_from_db(row["refill"]),
                    cancel=_bool_from_db(row["cancel"]),
                    dripfeed=_bool_from_db(row["dripfeed"]),
                )
            )
        return items

    def insert_failed_attempt(
        self,
        *,
        provider_slug: str,
        provider_account_key: str,
        discovered_at: str,
        error_code: str,
        error_message_ar: str,
        error_detail: str | None = None,
    ) -> ProviderCatalogSnapshot:
        snapshot_id = _new_id("pcs")
        self.connection.execute(
            """
            INSERT INTO soldium_provider_catalog_snapshots (
                id, provider_slug, provider_account_key, status, discovered_at,
                item_count, error_code, error_message_ar, error_detail
            ) VALUES (?, ?, ?, 'failed', ?, 0, ?, ?, ?)
            """,
            (
                snapshot_id,
                provider_slug,
                provider_account_key,
                discovered_at,
                error_code,
                error_message_ar,
                error_detail,
            ),
        )
        row = self.connection.execute(
            """
            SELECT id, provider_slug, provider_account_key, status, discovered_at,
                   item_count, error_code, error_message_ar, error_detail, created_at
            FROM soldium_provider_catalog_snapshots WHERE id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        return self._row_to_snapshot(row)

    def insert_successful_snapshot(
        self,
        *,
        provider_slug: str,
        provider_account_key: str,
        discovered_at: str,
        items: list[ProviderCatalogItem],
    ) -> ProviderCatalogSnapshot:
        """Atomically persist a successful snapshot + items (caller owns transaction)."""
        assert_unique_external_ids(items)
        snapshot_id = _new_id("pcs")
        self.connection.execute(
            """
            INSERT INTO soldium_provider_catalog_snapshots (
                id, provider_slug, provider_account_key, status, discovered_at,
                item_count, error_code, error_message_ar, error_detail
            ) VALUES (?, ?, ?, 'success', ?, ?, NULL, NULL, NULL)
            """,
            (
                snapshot_id,
                provider_slug,
                provider_account_key,
                discovered_at,
                len(items),
            ),
        )
        for item in items:
            self.connection.execute(
                """
                INSERT INTO soldium_provider_catalog_snapshot_items (
                    id, snapshot_id, external_service_id,
                    provider_service_name, provider_category, provider_type,
                    provider_description, min_quantity, max_quantity, provider_rate,
                    refill, cancel, dripfeed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_id("pcsi"),
                    snapshot_id,
                    item.external_service_id,
                    item.provider_service_name,
                    item.provider_category,
                    item.provider_type,
                    item.provider_description,
                    item.min_quantity,
                    item.max_quantity,
                    item.provider_rate,
                    _bool_to_db(item.refill),
                    _bool_to_db(item.cancel),
                    _bool_to_db(item.dripfeed),
                ),
            )
        row = self.connection.execute(
            """
            SELECT id, provider_slug, provider_account_key, status, discovered_at,
                   item_count, error_code, error_message_ar, error_detail, created_at
            FROM soldium_provider_catalog_snapshots WHERE id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        return self._row_to_snapshot(row)

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> ProviderCatalogSnapshot:
        return ProviderCatalogSnapshot(
            id=str(row["id"]),
            provider_slug=str(row["provider_slug"]),
            provider_account_key=str(row["provider_account_key"]),
            status=str(row["status"]),
            discovered_at=str(row["discovered_at"]),
            item_count=int(row["item_count"] or 0),
            error_code=row["error_code"],
            error_message_ar=row["error_message_ar"],
            error_detail=row["error_detail"],
            created_at=row["created_at"],
        )


async def discover_snapshot_and_diff(
    connection: sqlite3.Connection,
    *,
    provider_slug: str,
    account_key: str,
) -> ProviderSnapshotRunResult:
    """
    Discover → (on success) snapshot → diff against previous successful snapshot.

    Failed discovery:
    - may record a failed attempt
    - never replaces the latest successful baseline
    - never produces missing classifications
    """
    repo = ProviderSnapshotRepository(connection)
    discovery = await discover_provider_catalog(
        provider_slug=provider_slug,
        account_key=account_key,
    )
    slug = discovery.provider_slug
    account = discovery.provider_account_key

    if not discovery.ok:
        failed = None
        if slug:
            err = discovery.error
            failed = repo.insert_failed_attempt(
                provider_slug=slug,
                provider_account_key=account,
                discovered_at=discovery.discovered_at,
                error_code=(err.code if err else "api_failure"),
                error_message_ar=(
                    err.message_ar if err else "فشل اكتشاف كتالوج المزوّد."
                ),
                error_detail=err.detail if err else None,
            )
        logger.warning(
            "Provider snapshot skipped — discovery failed %s/%s", slug, account
        )
        return ProviderSnapshotRunResult(
            ok=False,
            discovery=discovery,
            snapshot=failed,
            diff=None,
            previous_snapshot_preserved=True,
            message_ar=(
                discovery.error.message_ar
                if discovery.error
                else "فشل الاكتشاف؛ تم الإبقاء على آخر لقطة ناجحة."
            ),
        )

    # Load PREVIOUS successful baseline BEFORE inserting the new snapshot.
    previous = repo.get_latest_successful_snapshot(slug, account)
    previous_items: list[ProviderCatalogItem] = []
    if previous is not None:
        previous_items = repo.load_snapshot_items(
            previous.id,
            provider_slug=slug,
            provider_account_key=account,
        )

    try:
        assert_unique_external_ids(discovery.items)
        current = repo.insert_successful_snapshot(
            provider_slug=slug,
            provider_account_key=account,
            discovered_at=discovery.discovered_at,
            items=discovery.items,
        )
    except CatalogValidationError as exc:
        return ProviderSnapshotRunResult(
            ok=False,
            discovery=discovery,
            snapshot=None,
            diff=None,
            previous_snapshot_preserved=True,
            message_ar=exc.message,
        )

    diff = compute_provider_catalog_diff(
        provider_slug=slug,
        provider_account_key=account,
        previous_snapshot=previous,
        previous_items=previous_items,
        current_snapshot=current,
        current_items=discovery.items,
    )

    if diff.is_baseline:
        message_ar = "تم إنشاء أول لقطة أساسية لكتالوج المزوّد."
    else:
        message_ar = "تم حفظ اللقطة وحساب الفروقات."

    return ProviderSnapshotRunResult(
        ok=True,
        discovery=discovery,
        snapshot=current,
        diff=diff,
        previous_snapshot_preserved=False,
        message_ar=message_ar,
    )
