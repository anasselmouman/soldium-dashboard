# -*- coding: utf-8 -*-
"""Catalog service readiness — derived validation (Phase 5).

Readiness is computed from current Catalog data. It is never stored as a flag
and cannot be manually overridden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from catalog_core.commercial import (
    FULFILLMENT_MODE_CODES,
    normalize_fulfillment_mode,
    validate_commercial_profile,
    validate_target_policy_for_publish,
)
from catalog_core.errors import CatalogValidationError
from catalog_core.models import CatalogPrice, CatalogService, ExecutionSource
from catalog_core.pricing import PRICING_MODE_CODES, SUPPORTED_CURRENCIES
from catalog_core.repository import CatalogRepository

Severity = Literal["error", "warning"]
ReadinessState = Literal["ready", "needs_review"]

CHECK_BASIC = "basic"
CHECK_PLACEMENT = "placement"
CHECK_COMMERCIAL = "commercial"
CHECK_SOURCE = "source"
CHECK_PRICE = "price"
CHECK_TARGET = "target"
CHECK_FULFILLMENT = "fulfillment"

CHECK_LABELS_AR: dict[str, str] = {
    CHECK_BASIC: "المعلومات الأساسية",
    CHECK_PLACEMENT: "التنظيم",
    CHECK_COMMERCIAL: "البيانات التجارية",
    CHECK_SOURCE: "مصدر التنفيذ",
    CHECK_PRICE: "السعر",
    CHECK_TARGET: "متطلبات الرابط / الهدف",
    CHECK_FULFILLMENT: "طريقة التنفيذ",
}


@dataclass
class ReadinessIssue:
    code: str
    severity: Severity
    title: str
    message: str
    check: str
    fix_action: str | None = None  # UI: source | price | commercial | placement | target | fulfillment

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "message": self.message,
            "check": self.check,
            "check_label_ar": CHECK_LABELS_AR.get(self.check, self.check),
            "fix_action": self.fix_action,
        }


@dataclass
class CheckResult:
    check: str
    ok: bool
    issues: list[ReadinessIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "label_ar": CHECK_LABELS_AR.get(self.check, self.check),
            "ok": self.ok,
            "issues": [i.to_dict() for i in self.issues],
        }


@dataclass
class ReadinessResult:
    service_id: str
    ready: bool
    state: ReadinessState
    state_label_ar: str
    checks: list[CheckResult]
    issues: list[ReadinessIssue]
    warnings: list[ReadinessIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "ready": self.ready,
            "state": self.state,
            "state_label_ar": self.state_label_ar,
            "checks": [c.to_dict() for c in self.checks],
            "issues": [i.to_dict() for i in self.issues],
            "warnings": [w.to_dict() for w in self.warnings],
        }

    def summary_dict(self) -> dict[str, Any]:
        """Compact payload for list rows."""
        return {
            "ready": self.ready,
            "state": self.state,
            "state_label_ar": self.state_label_ar,
            "issue_titles": [i.title for i in self.issues],
            "issue_count": len(self.issues),
        }


def _issue(
    *,
    code: str,
    title: str,
    message: str,
    check: str,
    severity: Severity = "error",
    fix_action: str | None = None,
) -> ReadinessIssue:
    return ReadinessIssue(
        code=code,
        severity=severity,
        title=title,
        message=message,
        check=check,
        fix_action=fix_action,
    )


def check_basic_service(service: CatalogService | None) -> CheckResult:
    issues: list[ReadinessIssue] = []
    if service is None:
        issues.append(
            _issue(
                code="service_missing",
                title="الخدمة غير موجودة",
                message="تعذر العثور على الخدمة في الكاتالوج.",
                check=CHECK_BASIC,
            )
        )
        return CheckResult(check=CHECK_BASIC, ok=False, issues=issues)

    if not (service.name_ar or "").strip():
        issues.append(
            _issue(
                code="missing_name",
                title="اسم الخدمة فارغ",
                message="يجب إدخال اسم واضح للخدمة.",
                check=CHECK_BASIC,
                fix_action="commercial",
            )
        )

    if service.status == "archived":
        issues.append(
            _issue(
                code="service_archived",
                title="الخدمة مؤرشفة",
                message="الخدمة مؤرشفة ولا يمكن اعتبارها جاهزة للنشر.",
                check=CHECK_BASIC,
            )
        )

    return CheckResult(check=CHECK_BASIC, ok=not issues, issues=issues)


def check_catalog_placement(
    repo: CatalogRepository, service: CatalogService
) -> CheckResult:
    issues: list[ReadinessIssue] = []
    entry = repo.get_entry_for_service(service.id)
    if entry is None:
        issues.append(
            _issue(
                code="missing_placement",
                title="لا يوجد مكان في الهيكل",
                message="يجب وضع الخدمة داخل هيكل الكاتالوج.",
                check=CHECK_PLACEMENT,
                fix_action="placement",
            )
        )
        return CheckResult(check=CHECK_PLACEMENT, ok=False, issues=issues)

    if entry.entry_type != "service" or entry.service_id != service.id:
        issues.append(
            _issue(
                code="invalid_placement",
                title="مكان الخدمة غير صالح",
                message="سجل التنظيم المرتبط بالخدمة غير متسق.",
                check=CHECK_PLACEMENT,
                fix_action="placement",
            )
        )
        return CheckResult(check=CHECK_PLACEMENT, ok=False, issues=issues)

    parent_id = entry.parent_entry_id
    if parent_id is not None:
        parent = repo.get_entry(parent_id)
        if parent is None:
            issues.append(
                _issue(
                    code="broken_parent",
                    title="المكان الأب غير موجود",
                    message="الخدمة تشير إلى قسم غير موجود في الهيكل.",
                    check=CHECK_PLACEMENT,
                    fix_action="placement",
                )
            )
        elif parent.entry_type != "node":
            issues.append(
                _issue(
                    code="parent_not_node",
                    title="المكان الأب غير صالح",
                    message="لا يمكن وضع خدمة تحت خدمة أخرى.",
                    check=CHECK_PLACEMENT,
                    fix_action="placement",
                )
            )
        else:
            # Detect cycles / broken ancestor chain.
            seen: set[str] = set()
            current = parent_id
            while current:
                if current in seen:
                    issues.append(
                        _issue(
                            code="placement_cycle",
                            title="هيكل غير صالح",
                            message="تم اكتشاف حلقة في هيكل الكاتالوج.",
                            check=CHECK_PLACEMENT,
                            fix_action="placement",
                        )
                    )
                    break
                seen.add(current)
                row = repo.get_entry(current)
                if row is None:
                    issues.append(
                        _issue(
                            code="broken_ancestor",
                            title="مسار الهيكل مكسور",
                            message="أحد أقسام المسار غير موجود.",
                            check=CHECK_PLACEMENT,
                            fix_action="placement",
                        )
                    )
                    break
                current = row.parent_entry_id

    return CheckResult(check=CHECK_PLACEMENT, ok=not issues, issues=issues)


def check_commercial_profile(service: CatalogService) -> CheckResult:
    issues: list[ReadinessIssue] = []
    try:
        min_q = int(service.min_quantity)
        max_q = int(service.max_quantity)
    except (TypeError, ValueError):
        return CheckResult(
            check=CHECK_COMMERCIAL,
            ok=False,
            issues=[
                _issue(
                    code="invalid_commercial_quantities",
                    title="حدود الكمية غير صالحة",
                    message="حدود الكمية يجب أن تكون أعداداً صحيحة.",
                    check=CHECK_COMMERCIAL,
                    fix_action="commercial",
                )
            ],
        )

    try:
        validate_commercial_profile(
            name_ar=(service.name_ar or "").strip() or "x",
            service_type=service.service_type,
            ordering_mode=service.ordering_mode,
            min_quantity=min_q,
            max_quantity=max_q,
        )
    except CatalogValidationError as exc:
        msg = str(exc.message)
        if "اسم" in msg:
            pass  # covered by basic check
        elif "نوع" in msg:
            issues.append(
                _issue(
                    code="invalid_service_type",
                    title="نوع الخدمة غير صالح",
                    message="اختر نوع خدمة معتمداً من القائمة.",
                    check=CHECK_COMMERCIAL,
                    fix_action="commercial",
                )
            )
        elif "طريقة الطلب" in msg:
            issues.append(
                _issue(
                    code="invalid_ordering_mode",
                    title="طريقة الطلب غير صالحة",
                    message="اختر طريقة طلب معتمدة.",
                    check=CHECK_COMMERCIAL,
                    fix_action="commercial",
                )
            )
        else:
            issues.append(
                _issue(
                    code="invalid_commercial_quantities",
                    title="حدود الكمية غير صالحة",
                    message=msg,
                    check=CHECK_COMMERCIAL,
                    fix_action="commercial",
                )
            )

    return CheckResult(check=CHECK_COMMERCIAL, ok=not issues, issues=issues)


def check_execution_source(
    repo: CatalogRepository,
    service: CatalogService,
    source: ExecutionSource | None = None,
) -> CheckResult:
    issues: list[ReadinessIssue] = []
    active = source if source is not None else repo.get_active_execution_source(service.id)
    if active is None:
        issues.append(
            _issue(
                code="missing_execution_source",
                title="مصدر التنفيذ غير موجود",
                message="يجب تحديد مورد وحساب ومعرّف خدمة لدى المورد.",
                check=CHECK_SOURCE,
                fix_action="source",
            )
        )
        return CheckResult(check=CHECK_SOURCE, ok=False, issues=issues)

    if not (active.external_service_id or "").strip():
        issues.append(
            _issue(
                code="empty_external_service_id",
                title="معرّف الخدمة لدى المورد فارغ",
                message="أدخل معرّف الخدمة لدى المورد.",
                check=CHECK_SOURCE,
                fix_action="source",
            )
        )

    provider = repo.find_provider(active.provider_slug)
    if not provider:
        issues.append(
            _issue(
                code="invalid_provider",
                title="المورد غير صالح",
                message="المورد المرتبط بمصدر التنفيذ غير موجود.",
                check=CHECK_SOURCE,
                fix_action="source",
            )
        )
    else:
        account = repo.find_provider_account(
            active.provider_slug, active.provider_account_key
        )
        if not account:
            # Distinguish mismatch vs missing
            other = None
            try:
                other = repo.connection.execute(
                    "SELECT provider_slug FROM provider_accounts WHERE account_key = ? LIMIT 1",
                    (active.provider_account_key,),
                ).fetchone()
            except Exception:
                other = None
            if other and str(other["provider_slug"]) != active.provider_slug:
                issues.append(
                    _issue(
                        code="provider_account_mismatch",
                        title="الحساب لا ينتمي للمورد",
                        message="حساب المورد المحدد لا يتوافق مع المورد.",
                        check=CHECK_SOURCE,
                        fix_action="source",
                    )
                )
            else:
                issues.append(
                    _issue(
                        code="invalid_provider_account",
                        title="حساب المورد غير صالح",
                        message="حساب المورد المرتبط بمصدر التنفيذ غير موجود.",
                        check=CHECK_SOURCE,
                        fix_action="source",
                    )
                )

    return CheckResult(check=CHECK_SOURCE, ok=not issues, issues=issues)


def check_price(
    service: CatalogService,
    price: CatalogPrice | None = None,
    *,
    repo: CatalogRepository | None = None,
) -> CheckResult:
    issues: list[ReadinessIssue] = []
    active = price
    if active is None and repo is not None:
        active = repo.get_active_price(service.id)
    if active is None:
        issues.append(
            _issue(
                code="missing_price",
                title="السعر غير محدد",
                message="يجب تحديد سعر بيع للخدمة.",
                check=CHECK_PRICE,
                fix_action="price",
            )
        )
        return CheckResult(check=CHECK_PRICE, ok=False, issues=issues)

    if int(active.amount_millimes) <= 0:
        issues.append(
            _issue(
                code="invalid_price_amount",
                title="السعر غير صالح",
                message="سعر البيع يجب أن يكون أكبر من صفر.",
                check=CHECK_PRICE,
                fix_action="price",
            )
        )
    if active.pricing_mode not in PRICING_MODE_CODES:
        issues.append(
            _issue(
                code="invalid_pricing_mode",
                title="طريقة التسعير غير صالحة",
                message="اختر طريقة تسعير معتمدة.",
                check=CHECK_PRICE,
                fix_action="price",
            )
        )
    currency = str(active.currency or "").strip().upper()
    if currency in {"DH"}:
        currency = "MAD"
    if currency not in SUPPORTED_CURRENCIES:
        issues.append(
            _issue(
                code="invalid_currency",
                title="العملة غير مدعومة",
                message="عملة السعر يجب أن تكون الدرهم.",
                check=CHECK_PRICE,
                fix_action="price",
            )
        )

    return CheckResult(check=CHECK_PRICE, ok=not issues, issues=issues)


def check_target_policy(service: CatalogService) -> CheckResult:
    """Publish-aligned: platform_key + section_key required for readiness."""
    issues: list[ReadinessIssue] = []
    try:
        validate_target_policy_for_publish(
            platform_key=service.target_platform_key,
            section_key=service.target_section_key,
        )
    except CatalogValidationError as exc:
        msg = str(exc.message)
        code = "missing_target_policy"
        if "المنصة" in msg:
            code = "missing_target_platform_key"
        elif "القسم" in msg:
            code = "missing_target_section_key"
        issues.append(
            _issue(
                code=code,
                title="متطلبات الرابط غير مكتملة",
                message=msg,
                check=CHECK_TARGET,
                fix_action="target",
            )
        )
    return CheckResult(check=CHECK_TARGET, ok=not issues, issues=issues)


def check_fulfillment_mode(service: CatalogService) -> CheckResult:
    issues: list[ReadinessIssue] = []
    raw = service.fulfillment_mode
    try:
        normalize_fulfillment_mode(raw)
    except CatalogValidationError as exc:
        issues.append(
            _issue(
                code="invalid_fulfillment_mode",
                title="طريقة التنفيذ غير صالحة",
                message=str(exc.message),
                check=CHECK_FULFILLMENT,
                fix_action="fulfillment",
            )
        )
        return CheckResult(check=CHECK_FULFILLMENT, ok=False, issues=issues)

    text = str(raw or "").strip().lower()
    if text and text not in FULFILLMENT_MODE_CODES:
        issues.append(
            _issue(
                code="invalid_fulfillment_mode",
                title="طريقة التنفيذ غير صالحة",
                message="اختر طريقة تنفيذ معتمدة (تلقائي أو يدوي).",
                check=CHECK_FULFILLMENT,
                fix_action="fulfillment",
            )
        )
    return CheckResult(check=CHECK_FULFILLMENT, ok=not issues, issues=issues)


def evaluate_service_readiness(
    repo: CatalogRepository,
    service: CatalogService | None,
    *,
    source: ExecutionSource | None = None,
    price: CatalogPrice | None = None,
) -> ReadinessResult:
    """Pure evaluation — does not write to the database."""
    service_id = service.id if service else ""
    checks: list[CheckResult] = []

    basic = check_basic_service(service)
    checks.append(basic)

    if service is None:
        issues = list(basic.issues)
        return ReadinessResult(
            service_id=service_id,
            ready=False,
            state="needs_review",
            state_label_ar="تحتاج مراجعة",
            checks=checks,
            issues=issues,
            warnings=[],
        )

    # Still run remaining checks so the UI shows all problems together.
    checks.append(check_catalog_placement(repo, service))
    checks.append(check_commercial_profile(service))
    checks.append(check_fulfillment_mode(service))
    checks.append(check_target_policy(service))
    checks.append(check_execution_source(repo, service, source=source))
    checks.append(check_price(service, price=price, repo=repo if price is None else None))

    all_issues = [i for c in checks for i in c.issues if i.severity == "error"]
    warnings = [i for c in checks for i in c.issues if i.severity == "warning"]
    ready = len(all_issues) == 0
    return ReadinessResult(
        service_id=service.id,
        ready=ready,
        state="ready" if ready else "needs_review",
        state_label_ar="جاهزة" if ready else "تحتاج مراجعة",
        checks=checks,
        issues=all_issues,
        warnings=warnings,
    )
