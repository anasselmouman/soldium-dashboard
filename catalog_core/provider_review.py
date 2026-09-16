# -*- coding: utf-8 -*-
"""Phase 6C — Provider Catalog Review Inbox (observation only).

Derived from Phase 6B successful snapshots + deterministic diff.
Does not call Provider APIs. Does not mutate SOLDIUM Catalog.
Does not persist administrative review state in this phase.
Failed discoveries never produce missing review items.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from catalog_core.provider_discovery import ProviderCatalogItem
from catalog_core.provider_snapshot import (
    COMPARED_FIELDS,
    ProviderCatalogChangedItem,
    ProviderCatalogDiff,
    ProviderCatalogSnapshot,
    ProviderSnapshotRepository,
    compute_provider_catalog_diff,
)

ChangeType = Literal["new", "changed", "missing"]

CHANGE_TYPE_LABELS_AR: dict[ChangeType, str] = {
    "new": "جديدة",
    "changed": "متغيرة",
    "missing": "مفقودة",
}

FIELD_LABELS_AR: dict[str, str] = {
    "provider_service_name": "اسم الخدمة لدى المزود",
    "provider_category": "التصنيف",
    "provider_type": "النوع",
    "provider_description": "الوصف",
    "min_quantity": "الحد الأدنى",
    "max_quantity": "الحد الأقصى",
    "provider_rate": "سعر المزود",
    "refill": "إعادة التعبئة",
    "cancel": "الإلغاء",
    "dripfeed": "التوزيع التدريجي",
}


def _format_field_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "نعم" if value else "لا"
    return str(value)


@dataclass
class ProviderReviewFieldChange:
    field: str
    label_ar: str
    previous_value: Any
    current_value: Any
    previous_display: str | None
    current_display: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "label_ar": self.label_ar,
            "previous_value": self.previous_value,
            "current_value": self.current_value,
            "previous_display": self.previous_display,
            "current_display": self.current_display,
        }


@dataclass
class ProviderReviewItem:
    """One Inbox row — Provider Catalog change, not a SOLDIUM service."""

    change_type: ChangeType
    change_type_label_ar: str
    provider_slug: str
    provider_account_key: str
    external_service_id: str
    current_snapshot_id: str
    previous_snapshot_id: str
    current_discovered_at: str
    previous_discovered_at: str
    item: ProviderCatalogItem | None = None  # current for new/changed
    previous_item: ProviderCatalogItem | None = None  # previous for changed/missing
    field_changes: list[ProviderReviewFieldChange] = field(default_factory=list)
    soldium_link_note_ar: str = "لا يوجد ربط بخدمة Soldium"
    mapping_status: dict[str, Any] | None = None
    note_ar: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_type": self.change_type,
            "change_type_label_ar": self.change_type_label_ar,
            "provider_slug": self.provider_slug,
            "provider_account_key": self.provider_account_key,
            "external_service_id": self.external_service_id,
            "current_snapshot_id": self.current_snapshot_id,
            "previous_snapshot_id": self.previous_snapshot_id,
            "current_discovered_at": self.current_discovered_at,
            "previous_discovered_at": self.previous_discovered_at,
            "item": self.item.to_dict() if self.item else None,
            "previous_item": self.previous_item.to_dict() if self.previous_item else None,
            "field_changes": [f.to_dict() for f in self.field_changes],
            "soldium_link_note_ar": self.soldium_link_note_ar,
            "mapping_status": self.mapping_status,
            "note_ar": self.note_ar,
        }


@dataclass
class ProviderReviewAccountContext:
    provider_slug: str
    provider_account_key: str
    has_comparison: bool
    is_baseline_only: bool
    current_snapshot_id: str | None = None
    previous_snapshot_id: str | None = None
    current_discovered_at: str | None = None
    previous_discovered_at: str | None = None
    message_ar: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_slug": self.provider_slug,
            "provider_account_key": self.provider_account_key,
            "has_comparison": self.has_comparison,
            "is_baseline_only": self.is_baseline_only,
            "current_snapshot_id": self.current_snapshot_id,
            "previous_snapshot_id": self.previous_snapshot_id,
            "current_discovered_at": self.current_discovered_at,
            "previous_discovered_at": self.previous_discovered_at,
            "message_ar": self.message_ar,
        }


@dataclass
class ProviderReviewSummary:
    needs_review: int
    new_count: int
    changed_count: int
    missing_count: int
    accounts_with_comparison: int
    accounts_baseline_only: int
    accounts: list[ProviderReviewAccountContext] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "needs_review": self.needs_review,
            "new_count": self.new_count,
            "changed_count": self.changed_count,
            "missing_count": self.missing_count,
            "accounts_with_comparison": self.accounts_with_comparison,
            "accounts_baseline_only": self.accounts_baseline_only,
            "accounts": [a.to_dict() for a in self.accounts],
        }


def _field_changes_for(
    changed: ProviderCatalogChangedItem,
) -> list[ProviderReviewFieldChange]:
    out: list[ProviderReviewFieldChange] = []
    for name in changed.changed_fields:
        if name not in COMPARED_FIELDS:
            continue
        prev_v = getattr(changed.previous, name)
        curr_v = getattr(changed.current, name)
        out.append(
            ProviderReviewFieldChange(
                field=name,
                label_ar=FIELD_LABELS_AR.get(name, name),
                previous_value=prev_v,
                current_value=curr_v,
                previous_display=_format_field_value(prev_v),
                current_display=_format_field_value(curr_v),
            )
        )
    return out


def build_review_items_from_diff(diff: ProviderCatalogDiff) -> list[ProviderReviewItem]:
    """Map Phase 6B diff buckets to Inbox items (unchanged excluded)."""
    if diff.is_baseline or not diff.previous_snapshot_id or not diff.current_snapshot_id:
        return []

    items: list[ProviderReviewItem] = []
    for item in diff.new:
        items.append(
            ProviderReviewItem(
                change_type="new",
                change_type_label_ar=CHANGE_TYPE_LABELS_AR["new"],
                provider_slug=diff.provider_slug,
                provider_account_key=diff.provider_account_key,
                external_service_id=item.external_service_id,
                current_snapshot_id=diff.current_snapshot_id,
                previous_snapshot_id=diff.previous_snapshot_id,
                current_discovered_at=diff.current_discovered_at or "",
                previous_discovered_at=diff.previous_discovered_at or "",
                item=item,
                previous_item=None,
                note_ar="خدمة مزود جديدة ظهرت في آخر اكتشاف ناجح.",
            )
        )
    for ch in diff.changed:
        items.append(
            ProviderReviewItem(
                change_type="changed",
                change_type_label_ar=CHANGE_TYPE_LABELS_AR["changed"],
                provider_slug=diff.provider_slug,
                provider_account_key=diff.provider_account_key,
                external_service_id=ch.current.external_service_id,
                current_snapshot_id=diff.current_snapshot_id,
                previous_snapshot_id=diff.previous_snapshot_id,
                current_discovered_at=diff.current_discovered_at or "",
                previous_discovered_at=diff.previous_discovered_at or "",
                item=ch.current,
                previous_item=ch.previous,
                field_changes=_field_changes_for(ch),
                note_ar="تغيّرت بيانات هذه الخدمة لدى المزود بين اكتشافين ناجحين.",
            )
        )
    for item in diff.missing:
        items.append(
            ProviderReviewItem(
                change_type="missing",
                change_type_label_ar=CHANGE_TYPE_LABELS_AR["missing"],
                provider_slug=diff.provider_slug,
                provider_account_key=diff.provider_account_key,
                external_service_id=item.external_service_id,
                current_snapshot_id=diff.current_snapshot_id,
                previous_snapshot_id=diff.previous_snapshot_id,
                current_discovered_at=diff.current_discovered_at or "",
                previous_discovered_at=diff.previous_discovered_at or "",
                item=None,
                previous_item=item,
                note_ar=(
                    "هذه الخدمة كانت موجودة في آخر مزامنة ناجحة سابقة، "
                    "لكنها لم تعد موجودة في آخر اكتشاف ناجح."
                ),
            )
        )
    return items


def _diff_for_account(
    repo: ProviderSnapshotRepository,
    provider_slug: str,
    provider_account_key: str,
) -> tuple[ProviderReviewAccountContext, ProviderCatalogDiff | None]:
    snaps = repo.get_latest_successful_snapshots(
        provider_slug, provider_account_key, limit=2
    )
    if not snaps:
        ctx = ProviderReviewAccountContext(
            provider_slug=provider_slug,
            provider_account_key=provider_account_key,
            has_comparison=False,
            is_baseline_only=False,
            message_ar="لا توجد لقطة ناجحة بعد.",
        )
        return ctx, None

    current = snaps[0]
    if len(snaps) < 2:
        ctx = ProviderReviewAccountContext(
            provider_slug=provider_slug,
            provider_account_key=provider_account_key,
            has_comparison=False,
            is_baseline_only=True,
            current_snapshot_id=current.id,
            current_discovered_at=current.discovered_at,
            message_ar="لا توجد مقارنة سابقة — تم إنشاء اللقطة الأساسية فقط.",
        )
        return ctx, None

    previous = snaps[1]
    current_items = repo.load_snapshot_items(
        current.id,
        provider_slug=provider_slug,
        provider_account_key=provider_account_key,
    )
    previous_items = repo.load_snapshot_items(
        previous.id,
        provider_slug=provider_slug,
        provider_account_key=provider_account_key,
    )
    diff = compute_provider_catalog_diff(
        provider_slug=provider_slug,
        provider_account_key=provider_account_key,
        previous_snapshot=previous,
        previous_items=previous_items,
        current_snapshot=current,
        current_items=current_items,
    )
    ctx = ProviderReviewAccountContext(
        provider_slug=provider_slug,
        provider_account_key=provider_account_key,
        has_comparison=True,
        is_baseline_only=False,
        current_snapshot_id=current.id,
        previous_snapshot_id=previous.id,
        current_discovered_at=current.discovered_at,
        previous_discovered_at=previous.discovered_at,
        message_ar="مقارنة بين آخر اكتشافين ناجحين.",
    )
    return ctx, diff


def _matches_search(item: ProviderReviewItem, search: str) -> bool:
    q = search.strip().lower()
    if not q:
        return True
    if q in item.external_service_id.lower():
        return True
    for src in (item.item, item.previous_item):
        if src is None:
            continue
        name = (src.provider_service_name or "").lower()
        if q in name:
            return True
    return False


def build_provider_review_summary(
    connection: sqlite3.Connection,
    *,
    provider_slug: str | None = None,
    provider_account_key: str | None = None,
) -> ProviderReviewSummary:
    repo = ProviderSnapshotRepository(connection)
    accounts = repo.list_accounts_with_successful_snapshots()
    if provider_slug:
        slug = provider_slug.strip().lower()
        accounts = [a for a in accounts if a[0] == slug]
    if provider_account_key:
        key = provider_account_key.strip().lower()
        accounts = [a for a in accounts if a[1] == key]

    contexts: list[ProviderReviewAccountContext] = []
    new_n = changed_n = missing_n = 0
    compared = 0
    baseline_only = 0

    for slug, account in accounts:
        ctx, diff = _diff_for_account(repo, slug, account)
        contexts.append(ctx)
        if ctx.is_baseline_only:
            baseline_only += 1
            continue
        if not ctx.has_comparison or diff is None:
            continue
        compared += 1
        review_items = build_review_items_from_diff(diff)
        new_n += sum(1 for i in review_items if i.change_type == "new")
        changed_n += sum(1 for i in review_items if i.change_type == "changed")
        missing_n += sum(1 for i in review_items if i.change_type == "missing")

    return ProviderReviewSummary(
        needs_review=new_n + changed_n + missing_n,
        new_count=new_n,
        changed_count=changed_n,
        missing_count=missing_n,
        accounts_with_comparison=compared,
        accounts_baseline_only=baseline_only,
        accounts=contexts,
    )


def list_provider_review_items(
    connection: sqlite3.Connection,
    *,
    provider_slug: str | None = None,
    provider_account_key: str | None = None,
    change_type: ChangeType | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ProviderReviewItem], int, list[ProviderReviewAccountContext]]:
    """List Inbox items from successful snapshot pairs only (no Provider API)."""
    if change_type is not None and change_type not in CHANGE_TYPE_LABELS_AR:
        raise ValueError("invalid change_type")

    from catalog_core.provider_mapping import ProviderMappingService

    repo = ProviderSnapshotRepository(connection)
    mapping_svc = ProviderMappingService(connection)
    accounts = repo.list_accounts_with_successful_snapshots()
    if provider_slug:
        slug = provider_slug.strip().lower()
        accounts = [a for a in accounts if a[0] == slug]
    if provider_account_key:
        key = provider_account_key.strip().lower()
        accounts = [a for a in accounts if a[1] == key]

    contexts: list[ProviderReviewAccountContext] = []
    all_items: list[ProviderReviewItem] = []

    for slug, account in accounts:
        ctx, diff = _diff_for_account(repo, slug, account)
        contexts.append(ctx)
        if not ctx.has_comparison or diff is None:
            continue
        for item in build_review_items_from_diff(diff):
            if change_type and item.change_type != change_type:
                continue
            if search and not _matches_search(item, search):
                continue
            status = mapping_svc.mapping_status_payload(
                provider_slug=item.provider_slug,
                provider_account_key=item.provider_account_key,
                external_service_id=item.external_service_id,
            )
            item.mapping_status = status
            item.soldium_link_note_ar = status["label_ar"]
            all_items.append(item)

    # Stable ordering for paging
    order = {"new": 0, "changed": 1, "missing": 2}
    all_items.sort(
        key=lambda i: (
            i.provider_slug,
            i.provider_account_key,
            order.get(i.change_type, 9),
            i.external_service_id,
        )
    )
    total = len(all_items)
    page = all_items[offset : offset + max(1, limit)]
    return page, total, contexts
