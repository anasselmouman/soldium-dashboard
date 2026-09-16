# -*- coding: utf-8 -*-
"""Catalog core domain errors (Arabic messages for API/UI)."""

from __future__ import annotations


class CatalogError(Exception):
    """Base domain error."""

    def __init__(self, message: str = "حدث خطأ في الكاتالوج") -> None:
        self.message = message
        super().__init__(message)


class CatalogNotFoundError(CatalogError):
    def __init__(self, message: str = "العنصر غير موجود") -> None:
        super().__init__(message)


class CatalogValidationError(CatalogError):
    def __init__(self, message: str = "البيانات غير صالحة") -> None:
        super().__init__(message)


class CatalogConflictError(CatalogError):
    def __init__(self, message: str = "تعارض في هيكل الكاتالوج") -> None:
        super().__init__(message)


class CatalogApplyError(CatalogError):
    """Structured Apply rejection (Phase 6E)."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


class CatalogPublishError(CatalogError):
    """Structured publication rejection (Phase 7)."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}
