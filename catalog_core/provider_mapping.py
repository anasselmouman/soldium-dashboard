# -*- coding: utf-8 -*-
"""Phase 6D — explicit Provider ↔ SOLDIUM service mapping.

Mapping is an administrative relationship, NOT identity and NOT execution source.
Creating/changing a mapping never modifies soldium_catalog_execution_sources,
prices, placement, commercial profile, or readiness.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from catalog_core.errors import (
    CatalogConflictError,
    CatalogNotFoundError,
    CatalogValidationError,
)
from catalog_core.repository import CatalogRepository

logger = logging.getLogger("soldium.catalog.provider_mapping")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass
class ProviderServiceMapping:
    id: str
    provider_slug: str
    provider_account_key: str
    external_service_id: str
    soldium_service_id: str
    status: str  # active | historical
    mapped_at: str | None = None
    ended_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # display (hydrated)
    soldium_service_name_ar: str = ""
    soldium_service_status: str = ""
    provider_name: str = ""
    account_display_name: str = ""
    seen_in_latest_snapshot: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider_slug": self.provider_slug,
            "provider_account_key": self.provider_account_key,
            "external_service_id": self.external_service_id,
            "soldium_service_id": self.soldium_service_id,
            "status": self.status,
            "mapped_at": self.mapped_at,
            "ended_at": self.ended_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "soldium_service_name_ar": self.soldium_service_name_ar,
            "soldium_service_status": self.soldium_service_status,
            "provider_name": self.provider_name,
            "account_display_name": self.account_display_name,
            "seen_in_latest_snapshot": self.seen_in_latest_snapshot,
            "status_label_ar": "نشط" if self.status == "active" else "سابق",
        }


@dataclass
class ChangeMappingResult:
    unchanged: bool
    message: str
    current: ProviderServiceMapping | None
    previous: ProviderServiceMapping | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "unchanged": self.unchanged,
            "message": self.message,
            "current": self.current.to_dict() if self.current else None,
            "previous": self.previous.to_dict() if self.previous else None,
        }


class ProviderMappingService:
    """CRUD for Provider ↔ SOLDIUM mappings. Never touches execution sources."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.catalog_repo = CatalogRepository(connection)

    def get_mapping_by_id(self, mapping_id: str) -> ProviderServiceMapping | None:
        return self._get_by_id(mapping_id)

    def get_active_mapping_for_provider(
        self,
        *,
        provider_slug: str,
        provider_account_key: str,
        external_service_id: str,
    ) -> ProviderServiceMapping | None:
        slug = provider_slug.strip().lower()
        account = provider_account_key.strip().lower()
        external = str(external_service_id).strip()
        row = self.connection.execute(
            """
            SELECT m.*, s.name_ar AS soldium_name, s.status AS soldium_status
            FROM soldium_provider_service_mappings m
            JOIN soldium_catalog_services s ON s.id = m.soldium_service_id
            WHERE m.provider_slug = ?
              AND m.provider_account_key = ?
              AND m.external_service_id = ?
              AND m.status = 'active'
            LIMIT 1
            """,
            (slug, account, external),
        ).fetchone()
        return self._hydrate(row) if row else None

    def get_active_mapping_for_soldium(
        self, soldium_service_id: str
    ) -> ProviderServiceMapping | None:
        row = self.connection.execute(
            """
            SELECT m.*, s.name_ar AS soldium_name, s.status AS soldium_status
            FROM soldium_provider_service_mappings m
            JOIN soldium_catalog_services s ON s.id = m.soldium_service_id
            WHERE m.soldium_service_id = ?
              AND m.status = 'active'
            LIMIT 1
            """,
            (soldium_service_id,),
        ).fetchone()
        return self._hydrate(row) if row else None

    def list_active_mappings(
        self,
        *,
        provider_slug: str | None = None,
        provider_account_key: str | None = None,
        soldium_service_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[ProviderServiceMapping], int]:
        clauses = ["m.status = 'active'"]
        params: list[Any] = []
        if provider_slug:
            clauses.append("m.provider_slug = ?")
            params.append(provider_slug.strip().lower())
        if provider_account_key:
            clauses.append("m.provider_account_key = ?")
            params.append(provider_account_key.strip().lower())
        if soldium_service_id:
            clauses.append("m.soldium_service_id = ?")
            params.append(soldium_service_id)
        where = " AND ".join(clauses)
        total = self.connection.execute(
            f"SELECT COUNT(*) FROM soldium_provider_service_mappings m WHERE {where}",
            params,
        ).fetchone()[0]
        rows = self.connection.execute(
            f"""
            SELECT m.*, s.name_ar AS soldium_name, s.status AS soldium_status
            FROM soldium_provider_service_mappings m
            JOIN soldium_catalog_services s ON s.id = m.soldium_service_id
            WHERE {where}
            ORDER BY m.mapped_at DESC, m.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        return [self._hydrate(r) for r in rows], int(total)

    def list_mapping_history(
        self,
        *,
        provider_slug: str | None = None,
        provider_account_key: str | None = None,
        external_service_id: str | None = None,
        soldium_service_id: str | None = None,
        limit: int = 100,
    ) -> list[ProviderServiceMapping]:
        clauses: list[str] = []
        params: list[Any] = []
        if soldium_service_id:
            clauses.append("m.soldium_service_id = ?")
            params.append(soldium_service_id)
        if provider_slug:
            clauses.append("m.provider_slug = ?")
            params.append(provider_slug.strip().lower())
        if provider_account_key:
            clauses.append("m.provider_account_key = ?")
            params.append(provider_account_key.strip().lower())
        if external_service_id is not None:
            clauses.append("m.external_service_id = ?")
            params.append(str(external_service_id).strip())
        if not clauses:
            raise CatalogValidationError("معايير سجل الربط مطلوبة")
        where = " AND ".join(clauses)
        rows = self.connection.execute(
            f"""
            SELECT m.*, s.name_ar AS soldium_name, s.status AS soldium_status
            FROM soldium_provider_service_mappings m
            JOIN soldium_catalog_services s ON s.id = m.soldium_service_id
            WHERE {where}
            ORDER BY m.mapped_at DESC, m.id DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        return [self._hydrate(r) for r in rows]

    def create_or_change_mapping(
        self,
        *,
        provider_slug: str,
        provider_account_key: str,
        external_service_id: str,
        soldium_service_id: str,
    ) -> ChangeMappingResult:
        slug, account, external = self._validate_provider_identity(
            provider_slug, provider_account_key, external_service_id
        )
        soldium = self._require_mappable_soldium_service(soldium_service_id)

        current_for_provider = self.get_active_mapping_for_provider(
            provider_slug=slug,
            provider_account_key=account,
            external_service_id=external,
        )
        if (
            current_for_provider
            and current_for_provider.soldium_service_id == soldium.id
        ):
            return ChangeMappingResult(
                unchanged=True,
                message="الربط المحدد مستخدم بالفعل",
                current=current_for_provider,
                previous=None,
            )

        current_for_soldium = self.get_active_mapping_for_soldium(soldium.id)
        if (
            current_for_soldium
            and (
                current_for_soldium.provider_slug != slug
                or current_for_soldium.provider_account_key != account
                or current_for_soldium.external_service_id != external
            )
        ):
            raise CatalogConflictError(
                "خدمة Soldium مرتبطة بالفعل بخدمة مزود أخرى. أنهِ الربط الحالي أولاً."
            )

        previous = current_for_provider
        if previous:
            self._end_mapping(previous.id)

        mapping_id = _new_id("psm")
        try:
            self.connection.execute(
                """
                INSERT INTO soldium_provider_service_mappings (
                    id, provider_slug, provider_account_key, external_service_id,
                    soldium_service_id, status, mapped_at, ended_at
                ) VALUES (?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP, NULL)
                """,
                (mapping_id, slug, account, external, soldium.id),
            )
        except sqlite3.IntegrityError as exc:
            raise CatalogConflictError(
                "تعذر إنشاء الربط بسبب تعارض في البيانات"
            ) from exc

        current = self.get_active_mapping_for_provider(
            provider_slug=slug,
            provider_account_key=account,
            external_service_id=external,
        )
        return ChangeMappingResult(
            unchanged=False,
            message="تم حفظ الربط" if previous is None else "تم تغيير الربط",
            current=current,
            previous=previous,
        )

    def end_mapping(
        self,
        *,
        provider_slug: str,
        provider_account_key: str,
        external_service_id: str,
    ) -> ChangeMappingResult:
        slug, account, external = self._validate_provider_identity(
            provider_slug, provider_account_key, external_service_id
        )
        current = self.get_active_mapping_for_provider(
            provider_slug=slug,
            provider_account_key=account,
            external_service_id=external,
        )
        if current is None:
            raise CatalogNotFoundError("لا يوجد ربط نشط لهذه الخدمة لدى المزود")
        self._end_mapping(current.id)
        ended = self._get_by_id(current.id)
        return ChangeMappingResult(
            unchanged=False,
            message="تم إنهاء الربط",
            current=None,
            previous=ended,
        )

    def end_mapping_for_soldium(self, soldium_service_id: str) -> ChangeMappingResult:
        current = self.get_active_mapping_for_soldium(soldium_service_id)
        if current is None:
            raise CatalogNotFoundError("لا يوجد ربط نشط لهذه الخدمة")
        self._end_mapping(current.id)
        ended = self._get_by_id(current.id)
        return ChangeMappingResult(
            unchanged=False,
            message="تم إنهاء الربط",
            current=None,
            previous=ended,
        )

    def mapping_status_payload(
        self,
        *,
        provider_slug: str,
        provider_account_key: str,
        external_service_id: str,
    ) -> dict[str, Any]:
        mapping = self.get_active_mapping_for_provider(
            provider_slug=provider_slug,
            provider_account_key=provider_account_key,
            external_service_id=external_service_id,
        )
        if mapping is None:
            return {
                "mapped": False,
                "label_ar": "غير مرتبط",
                "mapping": None,
                "applied": False,
                "apply_label_ar": None,
                "current_source": None,
            }
        current_src = self.catalog_repo.get_active_execution_source(
            mapping.soldium_service_id
        )
        applied = (
            current_src is not None
            and current_src.provider_slug == mapping.provider_slug
            and current_src.provider_account_key == mapping.provider_account_key
            and str(current_src.external_service_id)
            == str(mapping.external_service_id)
        )
        return {
            "mapped": True,
            "label_ar": f"مرتبط بـ {mapping.soldium_service_name_ar or mapping.soldium_service_id}",
            "mapping": mapping.to_dict(),
            "applied": applied,
            "apply_label_ar": "مطبق" if applied else "غير مطبق",
            "current_source": current_src.to_dict() if current_src else None,
            "current_source_label_ar": (
                current_src.summary_label() if current_src else "بدون مصدر"
            ),
        }

    def _end_mapping(self, mapping_id: str) -> None:
        self.connection.execute(
            """
            UPDATE soldium_provider_service_mappings
            SET status = 'historical',
                ended_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'active'
            """,
            (mapping_id,),
        )

    def _validate_provider_identity(
        self,
        provider_slug: str,
        provider_account_key: str,
        external_service_id: str,
    ) -> tuple[str, str, str]:
        slug = (provider_slug or "").strip().lower()
        account = (provider_account_key or "").strip().lower()
        external = str(external_service_id if external_service_id is not None else "").strip()
        if not slug:
            raise CatalogValidationError("المزود مطلوب")
        if not account:
            raise CatalogValidationError("حساب المزود مطلوب")
        if not external:
            raise CatalogValidationError("معرّف الخدمة لدى المزود مطلوب")

        provider = self.catalog_repo.find_provider(slug)
        if not provider:
            raise CatalogValidationError("المزود غير موجود")
        acc = self.catalog_repo.find_provider_account(slug, account)
        if not acc:
            other = self.connection.execute(
                "SELECT provider_slug FROM provider_accounts WHERE account_key = ? LIMIT 1",
                (account,),
            ).fetchone()
            if other and str(other["provider_slug"]) != slug:
                raise CatalogValidationError(
                    "الحساب المحدد لا ينتمي إلى المزود المختار"
                )
            raise CatalogValidationError("حساب المزود غير موجود")
        return slug, account, external

    def _require_mappable_soldium_service(self, soldium_service_id: str):
        svc = self.catalog_repo.get_service(soldium_service_id)
        if not svc:
            raise CatalogNotFoundError("خدمة Soldium غير موجودة")
        if svc.status == "archived":
            raise CatalogValidationError(
                "لا يمكن ربط خدمة مزود بخدمة Soldium مؤرشفة."
            )
        return svc

    def _seen_in_latest_snapshot(
        self, slug: str, account: str, external: str
    ) -> bool | None:
        row = self.connection.execute(
            """
            SELECT s.id
            FROM soldium_provider_catalog_snapshots s
            WHERE s.provider_slug = ?
              AND s.provider_account_key = ?
              AND s.status = 'success'
            ORDER BY s.discovered_at DESC, s.id DESC
            LIMIT 1
            """,
            (slug, account),
        ).fetchone()
        if row is None:
            return None
        found = self.connection.execute(
            """
            SELECT 1 FROM soldium_provider_catalog_snapshot_items
            WHERE snapshot_id = ? AND external_service_id = ?
            LIMIT 1
            """,
            (row["id"], external),
        ).fetchone()
        return found is not None

    def _get_by_id(self, mapping_id: str) -> ProviderServiceMapping | None:
        row = self.connection.execute(
            """
            SELECT m.*, s.name_ar AS soldium_name, s.status AS soldium_status
            FROM soldium_provider_service_mappings m
            JOIN soldium_catalog_services s ON s.id = m.soldium_service_id
            WHERE m.id = ?
            """,
            (mapping_id,),
        ).fetchone()
        return self._hydrate(row) if row else None

    def _hydrate(self, row: sqlite3.Row) -> ProviderServiceMapping:
        slug = str(row["provider_slug"])
        account = str(row["provider_account_key"])
        external = str(row["external_service_id"])
        provider = self.catalog_repo.find_provider(slug)
        acc = self.catalog_repo.find_provider_account(slug, account)
        return ProviderServiceMapping(
            id=str(row["id"]),
            provider_slug=slug,
            provider_account_key=account,
            external_service_id=external,
            soldium_service_id=str(row["soldium_service_id"]),
            status=str(row["status"]),
            mapped_at=row["mapped_at"],
            ended_at=row["ended_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            soldium_service_name_ar=str(row["soldium_name"] or ""),
            soldium_service_status=str(row["soldium_status"] or ""),
            provider_name=(provider[1] if provider else slug),
            account_display_name=(acc[2] if acc else account),
            seen_in_latest_snapshot=self._seen_in_latest_snapshot(
                slug, account, external
            ),
        )
