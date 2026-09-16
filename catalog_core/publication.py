# -*- coding: utf-8 -*-
"""Phase 7 — Catalog publication (immutable snapshots).

Publication is independent of administrative status and derived readiness.
History rows are the source of truth: latest event_type publish|unpublish
determines effective publication status. Snapshots are never updated in place.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from catalog_core.errors import (
    CatalogNotFoundError,
    CatalogPublishError,
    CatalogValidationError,
)
from catalog_core.models import CatalogPrice, CatalogService, ExecutionSource
from catalog_core.pricing import format_dh_amount, pricing_mode_label_ar
from catalog_core.provider_mapping import ProviderMappingService
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.commercial import (
    fulfillment_mode_label_ar,
    normalize_fulfillment_mode,
    ordering_mode_label_ar,
    service_type_label_ar,
    validate_target_policy_for_publish,
)

logger = logging.getLogger("soldium.catalog.publication")

PublicationStatus = Literal["published", "unpublished"]
PublishOutcome = Literal["published", "no_change", "unpublished"]

# Deterministic latest-event order when published_at ties (insert-only history).
_PUBLICATION_ORDER_SQL = "published_at DESC, rowid DESC"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _path_json(path: list[str]) -> str:
    return json.dumps(list(path or []), ensure_ascii=False)


def _path_from_json(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x) for x in data]
    except json.JSONDecodeError:
        return []
    return []


def _fingerprint_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class PublicationRecord:
    id: str
    service_id: str
    event_type: str  # publish | unpublish
    name_ar: str | None = None
    note_ar: str | None = None
    service_type: str | None = None
    ordering_mode: str | None = None
    min_quantity: int | None = None
    max_quantity: int | None = None
    amount_millimes: int | None = None
    currency: str | None = None
    pricing_mode: str | None = None
    provider_slug: str | None = None
    provider_account_key: str | None = None
    external_service_id: str | None = None
    fulfillment_mode: str | None = None
    target_platform_key: str | None = None
    target_section_key: str | None = None
    target_subsection_key: str | None = None
    target_link_prompt_key: str | None = None
    target_link_type: str | None = None
    location_path: list[str] | None = None
    # Historical only — MUST NOT be used to walk the live Catalog tree.
    # Customer placement comes from location_path / location_path_json.
    parent_entry_id: str | None = None
    content_fingerprint: str | None = None
    published_at: str | None = None
    published_by: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        path = list(self.location_path or [])
        data: dict[str, Any] = {
            "id": self.id,
            "service_id": self.service_id,
            "event_type": self.event_type,
            "event_type_label_ar": "نشر" if self.event_type == "publish" else "إلغاء نشر",
            "published_at": self.published_at,
            "published_by": self.published_by,
            "created_at": self.created_at,
            "content_fingerprint": self.content_fingerprint,
        }
        if self.event_type == "publish":
            data.update(
                {
                    "name_ar": self.name_ar,
                    "note_ar": self.note_ar or "",
                    "service_type": self.service_type,
                    "service_type_label_ar": service_type_label_ar(self.service_type or ""),
                    "ordering_mode": self.ordering_mode,
                    "ordering_mode_label_ar": ordering_mode_label_ar(
                        self.ordering_mode or ""
                    ),
                    "min_quantity": self.min_quantity,
                    "max_quantity": self.max_quantity,
                    "amount_millimes": self.amount_millimes,
                    "amount_dh": format_dh_amount(int(self.amount_millimes or 0)),
                    "currency": self.currency,
                    "pricing_mode": self.pricing_mode,
                    "pricing_mode_label_ar": pricing_mode_label_ar(
                        self.pricing_mode or ""
                    ),
                    "provider_slug": self.provider_slug,
                    "provider_account_key": self.provider_account_key,
                    "external_service_id": self.external_service_id,
                    "fulfillment_mode": self.fulfillment_mode,
                    "fulfillment_mode_label_ar": fulfillment_mode_label_ar(
                        self.fulfillment_mode or ""
                    ),
                    "target_policy": {
                        "required": True,
                        "platform_key": self.target_platform_key,
                        "section_key": self.target_section_key,
                        "subsection_key": self.target_subsection_key,
                        "link_prompt_key": self.target_link_prompt_key,
                        "link_type": self.target_link_type,
                    },
                    "location_path": path,
                    "location_path_label_ar": " › ".join(path) if path else "الجذر",
                    "parent_entry_id": self.parent_entry_id,
                    "source_label_ar": (
                        f"{self.provider_slug} / {self.provider_account_key} / "
                        f"{self.external_service_id}"
                    ),
                }
            )
        return data


@dataclass
class PublicationPreview:
    service_id: str
    can_publish: bool
    message_ar: str
    blocking_code: str | None
    blocking_reasons: list[str]
    publication_status: PublicationStatus
    publication_status_label_ar: str
    has_unpublished_changes: bool
    would_change: bool
    readiness: dict[str, Any] | None
    snapshot: dict[str, Any] | None
    latest_publication: dict[str, Any] | None
    mapping_conflict: bool
    content_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "can_publish": self.can_publish,
            "message_ar": self.message_ar,
            "blocking_code": self.blocking_code,
            "blocking_reasons": list(self.blocking_reasons),
            "publication_status": self.publication_status,
            "publication_status_label_ar": self.publication_status_label_ar,
            "has_unpublished_changes": self.has_unpublished_changes,
            "would_change": self.would_change,
            "readiness": self.readiness,
            "snapshot": self.snapshot,
            "latest_publication": self.latest_publication,
            "mapping_conflict": self.mapping_conflict,
            "content_fingerprint": self.content_fingerprint,
        }


@dataclass
class PublishResult:
    outcome: PublishOutcome
    message_ar: str
    unchanged: bool
    publication: dict[str, Any] | None
    publication_status: PublicationStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "message_ar": self.message_ar,
            "unchanged": self.unchanged,
            "publication": self.publication,
            "publication_status": self.publication_status,
            "publication_status_label_ar": (
                "منشورة" if self.publication_status == "published" else "غير منشورة"
            ),
        }


class CatalogPublicationService:
    """Publish / unpublish with immutable history (Phase 7)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.repo = CatalogRepository(connection)
        self.mapping_svc = ProviderMappingService(connection)

    def get_publication_status(self, service_id: str) -> dict[str, Any]:
        state = self._publication_state(service_id)
        return {
            "publication_status": state["status"],
            "publication_status_label_ar": state["label"],
            "has_unpublished_changes": state["has_changes"],
            "customer_catalog_eligible": state["customer_catalog_eligible"],
            "latest_event": state["latest"].to_dict() if state["latest"] else None,
            "latest_publication": (
                state["latest_publish"].to_dict() if state["latest_publish"] else None
            ),
        }

    def get_latest_event(self, service_id: str) -> PublicationRecord | None:
        row = self.connection.execute(
            f"""
            SELECT * FROM soldium_catalog_publications
            WHERE service_id = ?
            ORDER BY {_PUBLICATION_ORDER_SQL}
            LIMIT 1
            """,
            (service_id,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def get_latest_publish(self, service_id: str) -> PublicationRecord | None:
        row = self.connection.execute(
            f"""
            SELECT * FROM soldium_catalog_publications
            WHERE service_id = ? AND event_type = 'publish'
            ORDER BY {_PUBLICATION_ORDER_SQL}
            LIMIT 1
            """,
            (service_id,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def list_latest_events(self) -> list[PublicationRecord]:
        """Latest history event for every service that has any publication row."""
        return [
            self._row_to_record(row)
            for row in self.repo.list_latest_publication_rows()
        ]

    def list_publications(
        self, service_id: str, *, limit: int = 50
    ) -> list[PublicationRecord]:
        if not self.repo.get_service(service_id):
            raise CatalogNotFoundError("الخدمة غير موجودة")
        rows = self.connection.execute(
            f"""
            SELECT * FROM soldium_catalog_publications
            WHERE service_id = ?
            ORDER BY {_PUBLICATION_ORDER_SQL}
            LIMIT ?
            """,
            (service_id, limit),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def preview(self, service_id: str) -> PublicationPreview:
        state = self._publication_state(service_id)
        try:
            snap, fp, readiness = self._validate_for_publish(service_id)
        except CatalogPublishError as exc:
            return PublicationPreview(
                service_id=service_id,
                can_publish=False,
                message_ar=exc.message,
                blocking_code=exc.code,
                blocking_reasons=list((exc.details or {}).get("reasons") or [exc.message]),
                publication_status=state["status"],
                publication_status_label_ar=state["label"],
                has_unpublished_changes=state["has_changes"],
                would_change=False,
                readiness=(exc.details or {}).get("readiness"),
                snapshot=(exc.details or {}).get("snapshot"),
                latest_publication=(
                    state["latest_publish"].to_dict() if state["latest_publish"] else None
                ),
                mapping_conflict=exc.code == "mapping_source_conflict",
                content_fingerprint=None,
            )

        latest_pub = state["latest_publish"]
        already_same = (
            state["status"] == "published"
            and latest_pub is not None
            and latest_pub.content_fingerprint == fp
        )
        return PublicationPreview(
            service_id=service_id,
            can_publish=True,
            message_ar=(
                "الحالة الحالية مطابقة لآخر نشر — لا يلزم نشر جديد."
                if already_same
                else "يمكن نشر الخدمة بالحالة المعروضة."
            ),
            blocking_code=None,
            blocking_reasons=[],
            publication_status=state["status"],
            publication_status_label_ar=state["label"],
            has_unpublished_changes=state["has_changes"],
            would_change=not already_same,
            readiness=readiness.to_dict(),
            snapshot=snap,
            latest_publication=latest_pub.to_dict() if latest_pub else None,
            mapping_conflict=False,
            content_fingerprint=fp,
        )

    def publish(
        self,
        service_id: str,
        *,
        published_by: str | None = None,
        expected_content_fingerprint: str | None = None,
    ) -> PublishResult:
        snap, fp, _readiness = self._validate_for_publish(service_id)

        if (
            expected_content_fingerprint is not None
            and expected_content_fingerprint.strip()
            and expected_content_fingerprint.strip() != fp
        ):
            raise CatalogPublishError(
                "تغيرت حالة الخدمة منذ المعاينة. حدّث الصفحة وراجع قبل النشر.",
                code="concurrent_change",
            )

        latest = self.get_latest_event(service_id)
        latest_pub = self.get_latest_publish(service_id)
        if (
            latest
            and latest.event_type == "publish"
            and latest_pub
            and latest_pub.content_fingerprint == fp
        ):
            return PublishResult(
                outcome="no_change",
                message_ar="الحالة الحالية مطابقة لآخر نشر — لم يُنشأ سجل جديد.",
                unchanged=True,
                publication=latest_pub.to_dict(),
                publication_status="published",
            )

        pub_id = _new_id("pub")
        self.connection.execute(
            """
            INSERT INTO soldium_catalog_publications (
                id, service_id, event_type,
                name_ar, note_ar, service_type, ordering_mode,
                min_quantity, max_quantity,
                amount_millimes, currency, pricing_mode,
                provider_slug, provider_account_key, external_service_id,
                fulfillment_mode,
                target_platform_key, target_section_key, target_subsection_key,
                target_link_prompt_key, target_link_type,
                location_path_json, parent_entry_id, content_fingerprint,
                published_by
            ) VALUES (
                ?, ?, 'publish',
                ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?
            )
            """,
            (
                pub_id,
                service_id,
                snap["name_ar"],
                snap["note_ar"],
                snap["service_type"],
                snap["ordering_mode"],
                snap["min_quantity"],
                snap["max_quantity"],
                snap["amount_millimes"],
                snap["currency"],
                snap["pricing_mode"],
                snap["provider_slug"],
                snap["provider_account_key"],
                snap["external_service_id"],
                snap["fulfillment_mode"],
                snap["target_platform_key"],
                snap["target_section_key"],
                snap.get("target_subsection_key"),
                snap.get("target_link_prompt_key"),
                snap.get("target_link_type"),
                _path_json(snap["location_path"]),
                snap.get("parent_entry_id"),
                fp,
                (published_by or "").strip() or None,
            ),
        )
        record = self._get_by_id(pub_id)
        assert record is not None
        return PublishResult(
            outcome="published",
            message_ar="تم نشر الخدمة.",
            unchanged=False,
            publication=record.to_dict(),
            publication_status="published",
        )

    def unpublish(
        self,
        service_id: str,
        *,
        published_by: str | None = None,
    ) -> PublishResult:
        if not self.repo.get_service(service_id):
            raise CatalogNotFoundError("الخدمة غير موجودة")

        latest = self.get_latest_event(service_id)
        if latest is None or latest.event_type == "unpublish":
            return PublishResult(
                outcome="no_change",
                message_ar="الخدمة غير منشورة بالفعل.",
                unchanged=True,
                publication=latest.to_dict() if latest else None,
                publication_status="unpublished",
            )

        pub_id = _new_id("pub")
        self.connection.execute(
            """
            INSERT INTO soldium_catalog_publications (
                id, service_id, event_type, location_path_json, published_by
            ) VALUES (?, ?, 'unpublish', '[]', ?)
            """,
            (pub_id, service_id, (published_by or "").strip() or None),
        )
        record = self._get_by_id(pub_id)
        assert record is not None
        return PublishResult(
            outcome="unpublished",
            message_ar="تم إلغاء نشر الخدمة.",
            unchanged=False,
            publication=record.to_dict(),
            publication_status="unpublished",
        )

    def _publication_state(self, service_id: str) -> dict[str, Any]:
        """Single source of truth for publication status / eligibility / drift."""
        service = self.repo.get_service(service_id)
        if not service:
            raise CatalogNotFoundError("الخدمة غير موجودة")

        latest = self.get_latest_event(service_id)
        latest_publish = self.get_latest_publish(service_id)
        status: PublicationStatus = (
            "published" if latest and latest.event_type == "publish" else "unpublished"
        )

        entry = self.repo.get_entry_for_service(service_id)
        if entry:
            service.entry_id = entry.id
            service.parent_entry_id = entry.parent_entry_id
            service.location_path = self.repo.breadcrumb_names(entry.parent_entry_id)
        source = self.repo.get_active_execution_source(service_id)
        price = self.repo.get_active_price(service_id)
        readiness = evaluate_service_readiness(
            self.repo, service, source=source, price=price
        )

        current_fp = None
        try:
            _snap, current_fp, _ = self._build_current_snapshot(service_id)
        except CatalogPublishError:
            current_fp = None

        has_changes = False
        if status == "published" and latest_publish and current_fp:
            has_changes = current_fp != latest_publish.content_fingerprint
        elif status == "published" and latest_publish and current_fp is None:
            has_changes = True

        eligible = (
            status == "published"
            and service.status != "archived"
            and bool(readiness.ready)
        )
        return {
            "service": service,
            "status": status,
            "label": "منشورة" if status == "published" else "غير منشورة",
            "has_changes": has_changes,
            "customer_catalog_eligible": eligible,
            "latest": latest,
            "latest_publish": latest_publish,
            "readiness": readiness,
        }

    def _validate_for_publish(
        self, service_id: str
    ) -> tuple[dict[str, Any], str, Any]:
        service = self.repo.get_service(service_id)
        if not service:
            raise CatalogNotFoundError("الخدمة غير موجودة")
        if service.status == "draft":
            raise CatalogPublishError(
                "لا يمكن نشر خدمة في حالة مسودة. فعّل الخدمة أولاً.",
                code="service_not_active",
                details={"reasons": ["الخدمة مسودة"]},
            )
        if service.status == "archived":
            raise CatalogPublishError(
                "لا يمكن نشر خدمة مؤرشفة.",
                code="service_archived",
                details={"reasons": ["الخدمة مؤرشفة"]},
            )
        if service.status != "active":
            raise CatalogPublishError(
                "لا يمكن نشر الخدمة إلا وهي نشطة.",
                code="service_not_active",
                details={"reasons": [f"حالة الخدمة: {service.status}"]},
            )

        snap, fp, readiness = self._build_current_snapshot(service_id)
        if not readiness.ready:
            reasons = [i.title for i in readiness.issues] or [readiness.state_label_ar]
            raise CatalogPublishError(
                "لا يمكن نشر الخدمة لأن الخدمة تحتاج إلى مراجعة.",
                code="readiness_failed",
                details={
                    "reasons": reasons,
                    "readiness": readiness.to_dict(),
                    "snapshot": snap,
                },
            )
        # Mapping optional; conflict with active source blocks publish.
        mapping = self.mapping_svc.get_active_mapping_for_soldium(service_id)
        if mapping is not None:
            if (
                mapping.provider_slug != snap["provider_slug"]
                or mapping.provider_account_key != snap["provider_account_key"]
                or str(mapping.external_service_id) != str(snap["external_service_id"])
            ):
                raise CatalogPublishError(
                    "يوجد تعارض بين الربط ومصدر التنفيذ. راجع مصدر التنفيذ قبل النشر.",
                    code="mapping_source_conflict",
                    details={
                        "reasons": [
                            "الربط النشط لا يطابق مصدر التنفيذ النشط",
                        ],
                        "snapshot": snap,
                        "readiness": readiness.to_dict(),
                    },
                )
        return snap, fp, readiness

    def _build_current_snapshot(
        self, service_id: str
    ) -> tuple[dict[str, Any], str, Any]:
        service = self.repo.get_service(service_id)
        if not service:
            raise CatalogNotFoundError("الخدمة غير موجودة")
        if service.status == "archived":
            raise CatalogPublishError(
                "لا يمكن نشر خدمة مؤرشفة.",
                code="service_archived",
                details={"reasons": ["الخدمة مؤرشفة"]},
            )

        entry = self.repo.get_entry_for_service(service_id)
        if entry:
            service.entry_id = entry.id
            service.parent_entry_id = entry.parent_entry_id
            service.location_path = self.repo.breadcrumb_names(entry.parent_entry_id)
        else:
            service.location_path = []

        source = self.repo.get_active_execution_source(service_id)
        price = self.repo.get_active_price(service_id)
        readiness = evaluate_service_readiness(
            self.repo, service, source=source, price=price
        )

        reasons: list[str] = []
        if not (service.name_ar or "").strip():
            reasons.append("اسم الخدمة غير صالح")
        if entry is None:
            reasons.append("مكان الخدمة في الهيكل غير موجود")
        if source is None:
            reasons.append("مصدر التنفيذ غير موجود")
        elif not str(source.external_service_id or "").strip():
            reasons.append("معرّف الخدمة لدى المورد فارغ")
        else:
            provider = self.repo.find_provider(source.provider_slug)
            if not provider:
                reasons.append("المورد غير صالح")
            else:
                account = self.repo.find_provider_account(
                    source.provider_slug, source.provider_account_key
                )
                if not account:
                    reasons.append("حساب المورد غير صالح")
        if price is None or int(price.amount_millimes) <= 0:
            reasons.append("السعر غير صالح")

        # Prefer readiness-derived messages when available, but keep explicit codes.
        if reasons and not readiness.ready:
            # Still build partial snapshot for preview when possible
            pass
        if reasons and (
            source is None
            or price is None
            or service.status == "archived"
            or not (service.name_ar or "").strip()
            or entry is None
        ):
            code = "validation_failed"
            if service.status == "archived":
                code = "service_archived"
            elif price is None or (price and int(price.amount_millimes) <= 0):
                code = "invalid_price"
            elif source is None:
                code = "missing_execution_source"
            elif entry is None:
                code = "invalid_placement"
            msg = "لا يمكن نشر الخدمة. " + "؛ ".join(reasons)
            if "السعر" in " ".join(reasons):
                msg = "لا يمكن نشر الخدمة لأن السعر غير صالح."
            elif "مصدر التنفيذ غير موجود" in reasons:
                msg = "لا يمكن نشر الخدمة لأن مصدر التنفيذ غير موجود."
            raise CatalogPublishError(
                msg,
                code=code,
                details={
                    "reasons": reasons,
                    "readiness": readiness.to_dict(),
                    "snapshot": self._partial_snapshot(service, source, price),
                },
            )

        assert source is not None and price is not None
        try:
            fulfillment_mode = normalize_fulfillment_mode(service.fulfillment_mode)
        except CatalogValidationError as exc:
            raise CatalogPublishError(
                str(exc.message),
                code="invalid_fulfillment_mode",
                details={"reasons": [str(exc.message)]},
            ) from exc

        try:
            validate_target_policy_for_publish(
                platform_key=service.target_platform_key,
                section_key=service.target_section_key,
            )
        except CatalogValidationError as exc:
            raise CatalogPublishError(
                str(exc.message),
                code="missing_target_policy",
                details={"reasons": [str(exc.message)]},
            ) from exc

        target_platform_key = str(service.target_platform_key or "").strip()
        target_section_key = str(service.target_section_key or "").strip()
        target_subsection_key = (
            str(service.target_subsection_key).strip()
            if service.target_subsection_key
            else None
        ) or None
        target_link_prompt_key = (
            str(service.target_link_prompt_key).strip()
            if service.target_link_prompt_key
            else None
        ) or None
        target_link_type = (
            str(service.target_link_type).strip()
            if service.target_link_type
            else None
        ) or None

        snap = {
            "service_id": service.id,
            "name_ar": (service.name_ar or "").strip(),
            "note_ar": (service.note_ar or "").strip(),
            "service_type": service.service_type,
            "service_type_label_ar": service_type_label_ar(service.service_type),
            "ordering_mode": service.ordering_mode,
            "ordering_mode_label_ar": ordering_mode_label_ar(service.ordering_mode),
            "min_quantity": int(service.min_quantity),
            "max_quantity": int(service.max_quantity),
            "amount_millimes": int(price.amount_millimes),
            "amount_dh": format_dh_amount(int(price.amount_millimes)),
            "currency": price.currency,
            "pricing_mode": price.pricing_mode,
            "pricing_mode_label_ar": pricing_mode_label_ar(price.pricing_mode),
            "provider_slug": source.provider_slug,
            "provider_account_key": source.provider_account_key,
            "external_service_id": str(source.external_service_id),
            "fulfillment_mode": fulfillment_mode,
            "fulfillment_mode_label_ar": fulfillment_mode_label_ar(fulfillment_mode),
            "target_platform_key": target_platform_key,
            "target_section_key": target_section_key,
            "target_subsection_key": target_subsection_key,
            "target_link_prompt_key": target_link_prompt_key,
            "target_link_type": target_link_type,
            "target_policy": {
                "required": True,
                "platform_key": target_platform_key,
                "section_key": target_section_key,
                "subsection_key": target_subsection_key,
                "link_prompt_key": target_link_prompt_key,
                "link_type": target_link_type,
            },
            "location_path": list(service.location_path or []),
            "location_path_label_ar": (
                " › ".join(service.location_path)
                if service.location_path
                else "الجذر"
            ),
            "parent_entry_id": service.parent_entry_id,  # historical only; not for live tree
            "source_label_ar": source.summary_label(),
        }
        fp = _fingerprint_payload(
            {
                "name_ar": snap["name_ar"],
                "note_ar": snap["note_ar"],
                "service_type": snap["service_type"],
                "ordering_mode": snap["ordering_mode"],
                "min_quantity": snap["min_quantity"],
                "max_quantity": snap["max_quantity"],
                "amount_millimes": snap["amount_millimes"],
                "currency": snap["currency"],
                "pricing_mode": snap["pricing_mode"],
                "provider_slug": snap["provider_slug"],
                "provider_account_key": snap["provider_account_key"],
                "external_service_id": snap["external_service_id"],
                "fulfillment_mode": snap["fulfillment_mode"],
                "target_platform_key": snap["target_platform_key"],
                "target_section_key": snap["target_section_key"],
                "target_subsection_key": snap["target_subsection_key"],
                "target_link_prompt_key": snap["target_link_prompt_key"],
                "target_link_type": snap["target_link_type"],
                "location_path": snap["location_path"],
                # parent_entry_id is historical context in the fingerprint only;
                # consumers must not walk the live tree with it.
                "parent_entry_id": snap["parent_entry_id"],
            }
        )
        return snap, fp, readiness

    def _partial_snapshot(
        self,
        service: CatalogService,
        source: ExecutionSource | None,
        price: CatalogPrice | None,
    ) -> dict[str, Any]:
        return {
            "service_id": service.id,
            "name_ar": service.name_ar,
            "note_ar": service.note_ar,
            "service_type": service.service_type,
            "ordering_mode": service.ordering_mode,
            "min_quantity": service.min_quantity,
            "max_quantity": service.max_quantity,
            "amount_millimes": price.amount_millimes if price else None,
            "currency": price.currency if price else None,
            "pricing_mode": price.pricing_mode if price else None,
            "provider_slug": source.provider_slug if source else None,
            "provider_account_key": source.provider_account_key if source else None,
            "external_service_id": (
                str(source.external_service_id) if source else None
            ),
            "fulfillment_mode": service.fulfillment_mode,
            "target_platform_key": service.target_platform_key,
            "target_section_key": service.target_section_key,
            "target_subsection_key": service.target_subsection_key,
            "location_path": list(service.location_path or []),
        }

    def _get_by_id(self, pub_id: str) -> PublicationRecord | None:
        row = self.connection.execute(
            "SELECT * FROM soldium_catalog_publications WHERE id = ?",
            (pub_id,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def _row_to_record(self, row: sqlite3.Row) -> PublicationRecord:
        keys = set(row.keys())

        def _opt(name: str) -> str | None:
            if name not in keys or row[name] is None:
                return None
            text = str(row[name]).strip()
            return text or None

        return PublicationRecord(
            id=str(row["id"]),
            service_id=str(row["service_id"]),
            event_type=str(row["event_type"]),
            name_ar=row["name_ar"],
            note_ar=row["note_ar"],
            service_type=row["service_type"],
            ordering_mode=row["ordering_mode"],
            min_quantity=row["min_quantity"],
            max_quantity=row["max_quantity"],
            amount_millimes=row["amount_millimes"],
            currency=row["currency"],
            pricing_mode=row["pricing_mode"],
            provider_slug=row["provider_slug"],
            provider_account_key=row["provider_account_key"],
            external_service_id=(
                str(row["external_service_id"])
                if row["external_service_id"] is not None
                else None
            ),
            fulfillment_mode=_opt("fulfillment_mode"),
            target_platform_key=_opt("target_platform_key"),
            target_section_key=_opt("target_section_key"),
            target_subsection_key=_opt("target_subsection_key"),
            target_link_prompt_key=_opt("target_link_prompt_key"),
            target_link_type=_opt("target_link_type"),
            location_path=_path_from_json(row["location_path_json"]),
            parent_entry_id=row["parent_entry_id"],
            content_fingerprint=row["content_fingerprint"],
            published_at=row["published_at"],
            published_by=row["published_by"],
            created_at=row["created_at"],
        )
