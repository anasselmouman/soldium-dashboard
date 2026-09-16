# -*- coding: utf-8 -*-
"""Soldium Catalog core — new domain (independent from Catalog v2)."""

from catalog_core.db import catalog_connection, catalog_transaction
from catalog_core.errors import (
    CatalogConflictError,
    CatalogError,
    CatalogNotFoundError,
    CatalogValidationError,
)
from catalog_core.schema import ensure_soldium_catalog_schema, ensure_soldium_catalog_at_path
from catalog_core.service import CatalogCoreService

__all__ = [
    "CatalogCoreService",
    "CatalogConflictError",
    "CatalogError",
    "CatalogNotFoundError",
    "CatalogValidationError",
    "catalog_connection",
    "catalog_transaction",
    "ensure_soldium_catalog_schema",
    "ensure_soldium_catalog_at_path",
]
