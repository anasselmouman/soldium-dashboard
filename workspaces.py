# -*- coding: utf-8 -*-
"""Admin platform workspaces — navigation shells only (no Catalog domain logic)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


WORKSPACE_ADMIN = "admin"
WORKSPACE_CATALOG = "catalog"

_CATALOG_UI_JS = (
    Path(__file__).resolve().parent / "static" / "js" / "catalog_core_ui.js"
)


def catalog_ui_asset_version() -> str:
    """Cache-bust token for catalog_core_ui.js (mtime-size)."""
    try:
        st = _CATALOG_UI_JS.stat()
        return f"{int(st.st_mtime)}-{st.st_size}"
    except OSError:
        return "1"


@dataclass(frozen=True)
class WorkspaceNavItem:
    key: str
    label: str
    href: str


@dataclass(frozen=True)
class Workspace:
    id: str
    label: str
    home_path: str
    nav: tuple[WorkspaceNavItem, ...]


ADMIN_WORKSPACE = Workspace(
    id=WORKSPACE_ADMIN,
    label="لوحة الإدارة",
    home_path="/",
    nav=(),  # rendered from existing base.html admin sections
)

CATALOG_WORKSPACE = Workspace(
    id=WORKSPACE_CATALOG,
    label="إدارة الكاتالوج",
    home_path="/catalog",
    nav=(
        WorkspaceNavItem("home", "الرئيسية", "/catalog"),
        WorkspaceNavItem("services", "الخدمات", "/catalog/services"),
        WorkspaceNavItem("structure", "التنظيم", "/catalog/structure"),
        WorkspaceNavItem("review", "المراجعة", "/catalog/review"),
        WorkspaceNavItem("pricing", "التسعير", "/catalog/pricing"),
        WorkspaceNavItem("sources", "مصادر التنفيذ", "/catalog/sources"),
        WorkspaceNavItem("sync", "المزامنة", "/catalog/sync"),
        WorkspaceNavItem("history", "سجل التغييرات", "/catalog/history"),
    ),
)

WORKSPACES: dict[str, Workspace] = {
    WORKSPACE_ADMIN: ADMIN_WORKSPACE,
    WORKSPACE_CATALOG: CATALOG_WORKSPACE,
}

WORKSPACE_SWITCHER: tuple[Workspace, ...] = (ADMIN_WORKSPACE, CATALOG_WORKSPACE)


def get_workspace(workspace_id: str | None) -> Workspace:
    if workspace_id and workspace_id in WORKSPACES:
        return WORKSPACES[workspace_id]
    return ADMIN_WORKSPACE


def is_catalog_workspace(workspace_id: str | None) -> bool:
    return get_workspace(workspace_id).id == WORKSPACE_CATALOG


def page_context(
    *,
    workspace_id: str = WORKSPACE_ADMIN,
    active_nav: str,
    page_title: str,
    page_heading: str,
    page_subheading: str = "",
    catalog_section: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build TemplateResponse context with workspace identity + navigation."""
    workspace = get_workspace(workspace_id)
    ctx: dict[str, Any] = {
        "workspace_id": workspace.id,
        "workspace_label": workspace.label,
        "workspace_home": workspace.home_path,
        "workspace_nav": workspace.nav,
        "workspaces": WORKSPACE_SWITCHER,
        "active_nav": active_nav,
        "page_title": page_title,
        "page_heading": page_heading,
        "page_subheading": page_subheading,
        "catalog_ui_asset_v": catalog_ui_asset_version(),
    }
    if catalog_section is not None:
        ctx["catalog_section"] = catalog_section
    elif workspace.id == WORKSPACE_CATALOG:
        ctx["catalog_section"] = active_nav
    ctx.update(extra)
    return ctx
