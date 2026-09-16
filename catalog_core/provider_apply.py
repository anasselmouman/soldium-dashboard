# -*- coding: utf-8 -*-
"""Phase 6E — explicit Apply of Provider mapping → Execution Source.

Mapping alone never changes execution sources. Only Apply does, via the
canonical Phase 3 ``change_execution_source()`` path.

Readiness gate: evaluate Phase 5 readiness with the *proposed* source from
the active mapping (so first Apply is possible when the only source gap is
the missing execution source itself). No override, no live Provider API.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from catalog_core.errors import (
    CatalogApplyError,
    CatalogNotFoundError,
)
from catalog_core.models import ExecutionSource
from catalog_core.provider_mapping import ProviderMappingService, ProviderServiceMapping
from catalog_core.readiness import ReadinessResult, evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.service import CatalogCoreService

logger = logging.getLogger("soldium.catalog.provider_apply")

ApplyOutcome = Literal["applied", "no_change"]


def _source_identity(src: ExecutionSource | None) -> tuple[str, str, str] | None:
    if src is None:
        return None
    return (
        str(src.provider_slug),
        str(src.provider_account_key),
        str(src.external_service_id),
    )


def _source_dict(src: ExecutionSource | None) -> dict[str, Any] | None:
    return src.to_dict() if src else None


def _source_label(src: ExecutionSource | None) -> str:
    if src is None:
        return "بدون مصدر"
    return src.summary_label()


@dataclass
class ApplyPreview:
    mapping_id: str
    can_apply: bool
    would_change: bool
    outcome_label_ar: str
    message_ar: str
    blocking_code: str | None = None
    soldium_service_id: str = ""
    soldium_service_name_ar: str = ""
    mapping: dict[str, Any] | None = None
    current_source: dict[str, Any] | None = None
    proposed_source: dict[str, Any] | None = None
    current_source_label_ar: str = ""
    proposed_source_label_ar: str = ""
    readiness: dict[str, Any] | None = None
    applied_already: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping_id": self.mapping_id,
            "can_apply": self.can_apply,
            "would_change": self.would_change,
            "outcome_label_ar": self.outcome_label_ar,
            "message_ar": self.message_ar,
            "blocking_code": self.blocking_code,
            "soldium_service_id": self.soldium_service_id,
            "soldium_service_name_ar": self.soldium_service_name_ar,
            "mapping": self.mapping,
            "current_source": self.current_source,
            "proposed_source": self.proposed_source,
            "current_source_label_ar": self.current_source_label_ar,
            "proposed_source_label_ar": self.proposed_source_label_ar,
            "readiness": self.readiness,
            "applied_already": self.applied_already,
        }


@dataclass
class ApplyResult:
    outcome: ApplyOutcome
    message_ar: str
    mapping_id: str
    soldium_service_id: str
    unchanged: bool
    current_source: dict[str, Any] | None = None
    previous_source: dict[str, Any] | None = None
    readiness_after: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "message_ar": self.message_ar,
            "mapping_id": self.mapping_id,
            "soldium_service_id": self.soldium_service_id,
            "unchanged": self.unchanged,
            "current_source": self.current_source,
            "previous_source": self.previous_source,
            "readiness_after": self.readiness_after,
        }


@dataclass
class _ValidatedApplyContext:
    mapping: ProviderServiceMapping
    service: Any
    current_source: ExecutionSource | None
    proposed: ExecutionSource
    readiness: ReadinessResult
    would_change: bool
    applied_already: bool


class ProviderApplyService:
    """Explicit Apply: active mapping → Execution Source (Phase 3)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.repo = CatalogRepository(connection)
        self.mapping_svc = ProviderMappingService(connection)
        self.catalog = CatalogCoreService(connection)

    def preview(self, mapping_id: str) -> ApplyPreview:
        try:
            ctx = self._validate_for_apply(mapping_id, for_mutation=False)
        except CatalogApplyError as exc:
            return self._preview_from_error(mapping_id, exc)
        except CatalogNotFoundError as exc:
            return ApplyPreview(
                mapping_id=mapping_id,
                can_apply=False,
                would_change=False,
                outcome_label_ar="غير متاح",
                message_ar=exc.message,
                blocking_code="mapping_not_found",
            )

        if ctx.applied_already:
            return ApplyPreview(
                mapping_id=ctx.mapping.id,
                can_apply=True,
                would_change=False,
                outcome_label_ar="لا يوجد تغيير",
                message_ar="مصدر التنفيذ الحالي مطابق للربط — لا يلزم تطبيق.",
                soldium_service_id=ctx.mapping.soldium_service_id,
                soldium_service_name_ar=ctx.mapping.soldium_service_name_ar,
                mapping=ctx.mapping.to_dict(),
                current_source=_source_dict(ctx.current_source),
                proposed_source=_source_dict(ctx.proposed),
                current_source_label_ar=_source_label(ctx.current_source),
                proposed_source_label_ar=_source_label(ctx.proposed),
                readiness=ctx.readiness.to_dict(),
                applied_already=True,
            )

        return ApplyPreview(
            mapping_id=ctx.mapping.id,
            can_apply=True,
            would_change=True,
            outcome_label_ar="سيتم تغيير مصدر التنفيذ"
            if ctx.current_source
            else "سيتم تعيين مصدر التنفيذ",
            message_ar=(
                "سيتم تغيير المصدر الحالي لهذه الخدمة إلى المصدر المحدد في الربط."
                if ctx.current_source
                else "سيتم تعيين مصدر التنفيذ من الربط النشط."
            ),
            soldium_service_id=ctx.mapping.soldium_service_id,
            soldium_service_name_ar=ctx.mapping.soldium_service_name_ar,
            mapping=ctx.mapping.to_dict(),
            current_source=_source_dict(ctx.current_source),
            proposed_source=_source_dict(ctx.proposed),
            current_source_label_ar=_source_label(ctx.current_source),
            proposed_source_label_ar=_source_label(ctx.proposed),
            readiness=ctx.readiness.to_dict(),
            applied_already=False,
        )

    def apply(
        self,
        mapping_id: str,
        *,
        expect_no_current_source: bool = False,
        expected_current_provider_slug: str | None = None,
        expected_current_provider_account_key: str | None = None,
        expected_current_external_service_id: str | None = None,
    ) -> ApplyResult:
        """Mutate Execution Source only — never mapping/snapshots/prices."""
        ctx = self._validate_for_apply(mapping_id, for_mutation=True)

        check_concurrency = expect_no_current_source or any(
            x is not None
            for x in (
                expected_current_provider_slug,
                expected_current_provider_account_key,
                expected_current_external_service_id,
            )
        )
        if check_concurrency:
            actual = _source_identity(ctx.current_source)
            if expect_no_current_source or (
                not expected_current_provider_slug
                and not expected_current_provider_account_key
                and not expected_current_external_service_id
            ):
                if actual is not None:
                    raise CatalogApplyError(
                        "تغير مصدر التنفيذ منذ المعاينة. حدّث الصفحة وراجع الحالة.",
                        code="concurrent_change",
                    )
            else:
                expected = (
                    (expected_current_provider_slug or "").strip(),
                    (expected_current_provider_account_key or "").strip(),
                    str(expected_current_external_service_id or "").strip(),
                )
                if actual is None or actual != expected:
                    raise CatalogApplyError(
                        "تغير مصدر التنفيذ منذ المعاينة. حدّث الصفحة وراجع الحالة.",
                        code="concurrent_change",
                    )

        if ctx.applied_already:
            return ApplyResult(
                outcome="no_change",
                message_ar="مصدر التنفيذ الحالي مطابق للربط — لا يلزم تطبيق.",
                mapping_id=ctx.mapping.id,
                soldium_service_id=ctx.mapping.soldium_service_id,
                unchanged=True,
                current_source=_source_dict(ctx.current_source),
                previous_source=None,
                readiness_after=ctx.readiness.to_dict(),
            )

        result = self.catalog.change_execution_source(
            ctx.mapping.soldium_service_id,
            provider_slug=ctx.mapping.provider_slug,
            provider_account_key=ctx.mapping.provider_account_key,
            external_service_id=ctx.mapping.external_service_id,
        )

        readiness_after = self.catalog.get_service_readiness(
            ctx.mapping.soldium_service_id
        )
        return ApplyResult(
            outcome="no_change" if result.unchanged else "applied",
            message_ar=result.message
            if result.unchanged
            else "تم تطبيق مصدر التنفيذ من الربط.",
            mapping_id=ctx.mapping.id,
            soldium_service_id=ctx.mapping.soldium_service_id,
            unchanged=result.unchanged,
            current_source=_source_dict(result.current),
            previous_source=_source_dict(result.previous),
            readiness_after=readiness_after.to_dict(),
        )

    def _preview_from_error(
        self, mapping_id: str, exc: CatalogApplyError
    ) -> ApplyPreview:
        details = exc.details or {}
        return ApplyPreview(
            mapping_id=mapping_id,
            can_apply=False,
            would_change=False,
            outcome_label_ar="غير متاح",
            message_ar=exc.message,
            blocking_code=exc.code,
            soldium_service_id=str(details.get("soldium_service_id") or ""),
            soldium_service_name_ar=str(details.get("soldium_service_name_ar") or ""),
            mapping=details.get("mapping"),
            current_source=details.get("current_source"),
            proposed_source=details.get("proposed_source"),
            current_source_label_ar=str(details.get("current_source_label_ar") or ""),
            proposed_source_label_ar=str(details.get("proposed_source_label_ar") or ""),
            readiness=details.get("readiness"),
            applied_already=False,
        )

    def _validate_for_apply(
        self, mapping_id: str, *, for_mutation: bool
    ) -> _ValidatedApplyContext:
        mapping = self.mapping_svc.get_mapping_by_id(mapping_id)
        if mapping is None:
            raise CatalogNotFoundError("الربط غير موجود")
        if mapping.status != "active":
            raise CatalogApplyError(
                "هذا الربط غير نشط ولا يمكن تطبيقه.",
                code="mapping_not_active",
                details={"mapping": mapping.to_dict()},
            )

        # Re-validate provider/account from registry (authoritative).
        try:
            self.mapping_svc._validate_provider_identity(
                mapping.provider_slug,
                mapping.provider_account_key,
                mapping.external_service_id,
            )
        except Exception as exc:
            code = "validation_failed"
            msg = getattr(exc, "message", str(exc))
            if "المزود غير موجود" in msg:
                code = "provider_not_found"
            elif "لا ينتمي" in msg:
                code = "invalid_account"
            elif "حساب المزود" in msg:
                code = "account_not_found"
            raise CatalogApplyError(msg, code=code) from exc

        service = self.repo.get_service(mapping.soldium_service_id)
        if not service:
            raise CatalogApplyError(
                "خدمة Soldium غير موجودة",
                code="service_not_found",
            )
        if service.status == "archived":
            raise CatalogApplyError(
                "لا يمكن تطبيق مصدر التنفيذ على خدمة مؤرشفة.",
                code="service_archived",
                details={
                    "soldium_service_id": service.id,
                    "soldium_service_name_ar": service.name_ar,
                },
            )

        # Snapshot presence — persisted data only, no live Provider API.
        seen = mapping.seen_in_latest_snapshot
        if seen is not True:
            raise CatalogApplyError(
                "لم تعد الخدمة موجودة في آخر اكتشاف ناجح للمزود، "
                "لذلك لا يمكن تطبيقها كمصدر تنفيذ حاليًا.",
                code="provider_identity_not_in_latest_snapshot",
                details={
                    "soldium_service_id": service.id,
                    "soldium_service_name_ar": service.name_ar,
                    "mapping": mapping.to_dict(),
                },
            )

        proposed = ExecutionSource(
            id="proposed",
            service_id=service.id,
            provider_slug=mapping.provider_slug,
            provider_account_key=mapping.provider_account_key,
            external_service_id=str(mapping.external_service_id),
            status="active",
            provider_name=mapping.provider_name,
            account_display_name=mapping.account_display_name,
        )

        entry = self.repo.get_entry_for_service(service.id)
        if entry:
            service.entry_id = entry.id
            service.parent_entry_id = entry.parent_entry_id

        current_source = self.repo.get_active_execution_source(service.id)
        price = self.repo.get_active_price(service.id)

        # Readiness with proposed source (Phase 5 engine — not a second validator).
        readiness = evaluate_service_readiness(
            self.repo, service, source=proposed, price=price
        )
        if not readiness.ready:
            issues = [i.title for i in readiness.issues]
            detail_msg = "؛ ".join(issues) if issues else readiness.state_label_ar
            raise CatalogApplyError(
                f"لا يمكن تطبيق المصدر لأن الخدمة تحتاج إلى مراجعة. {detail_msg}",
                code="readiness_failed",
                details={
                    "soldium_service_id": service.id,
                    "soldium_service_name_ar": service.name_ar,
                    "mapping": mapping.to_dict(),
                    "current_source": _source_dict(current_source),
                    "proposed_source": _source_dict(proposed),
                    "current_source_label_ar": _source_label(current_source),
                    "proposed_source_label_ar": _source_label(proposed),
                    "readiness": readiness.to_dict(),
                },
            )

        applied_already = _source_identity(current_source) == (
            mapping.provider_slug,
            mapping.provider_account_key,
            str(mapping.external_service_id),
        )
        would_change = not applied_already

        return _ValidatedApplyContext(
            mapping=mapping,
            service=service,
            current_source=current_source,
            proposed=proposed,
            readiness=readiness,
            would_change=would_change,
            applied_already=applied_already,
        )
