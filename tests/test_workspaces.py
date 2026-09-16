# -*- coding: utf-8 -*-
"""Phase 1 — workspace architecture tests (no Catalog domain logic)."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from workspaces import (
    CATALOG_WORKSPACE,
    WORKSPACE_ADMIN,
    WORKSPACE_CATALOG,
    WORKSPACE_SWITCHER,
    get_workspace,
    is_catalog_workspace,
    page_context,
)

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


def _request(path: str = "/") -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
        }
    )


def _render(template_name: str, **ctx) -> str:
    template = _TEMPLATES.env.get_template(template_name)
    return template.render(request=_request(), **ctx)


def test_workspace_registry():
    assert get_workspace(None).id == WORKSPACE_ADMIN
    assert get_workspace("unknown").id == WORKSPACE_ADMIN
    assert get_workspace(WORKSPACE_CATALOG).id == WORKSPACE_CATALOG
    assert is_catalog_workspace(WORKSPACE_CATALOG) is True
    assert is_catalog_workspace(WORKSPACE_ADMIN) is False
    assert len(WORKSPACE_SWITCHER) == 2
    assert [w.id for w in WORKSPACE_SWITCHER] == [WORKSPACE_ADMIN, WORKSPACE_CATALOG]


def test_catalog_nav_is_arabic_and_namespaced():
    labels = [item.label for item in CATALOG_WORKSPACE.nav]
    hrefs = [item.href for item in CATALOG_WORKSPACE.nav]
    assert "الخدمات" in labels
    assert "التنظيم" in labels
    assert "مصادر التنفيذ" in labels
    assert "سجل التغييرات" in labels
    assert all(h == "/catalog" or h.startswith("/catalog/") for h in hrefs)
    assert CATALOG_WORKSPACE.home_path == "/catalog"
    assert CATALOG_WORKSPACE.label == "إدارة الكاتالوج"


def test_page_context_admin_defaults():
    ctx = page_context(
        active_nav="orders",
        page_title="t",
        page_heading="h",
    )
    assert ctx["workspace_id"] == WORKSPACE_ADMIN
    assert ctx["workspace_label"] == "لوحة الإدارة"
    assert ctx["active_nav"] == "orders"
    assert ctx["workspace_home"] == "/"


def test_page_context_catalog():
    ctx = page_context(
        workspace_id=WORKSPACE_CATALOG,
        active_nav="pricing",
        page_title="t",
        page_heading="التسعير",
    )
    assert ctx["workspace_id"] == WORKSPACE_CATALOG
    assert ctx["catalog_section"] == "pricing"
    assert ctx["workspace_label"] == "إدارة الكاتالوج"
    assert any(i.key == "pricing" for i in ctx["workspace_nav"])


def test_app_registers_catalog_and_admin_routes():
    from main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/" in paths
    assert "/orders" in paths
    assert "/services" in paths
    assert "/providers" in paths
    assert "/catalog" in paths
    assert "/catalog/services" in paths
    assert "/catalog/structure" in paths
    assert "/catalog/review" in paths
    assert "/catalog/pricing" in paths
    assert "/catalog/sources" in paths
    assert "/catalog/sync" in paths
    assert "/catalog/history" in paths
    assert not any(
        isinstance(getattr(r, "path", None), str)
        and str(r.path).startswith("/api/catalog")
        for r in app.routes
    )


def test_catalog_home_renders_workspace_shell():
    html = _render(
        "workspaces/catalog_home.html",
        **page_context(
            workspace_id=WORKSPACE_CATALOG,
            active_nav="home",
            catalog_section="home",
            page_title="إدارة الكاتالوج — سولديوم",
            page_heading="إدارة الكاتالوج",
            page_subheading="مساحة عمل مخصّصة",
        ),
    )
    assert "workspace-catalog" in html
    assert "إدارة الكاتالوج" in html
    assert "الخدمات" in html
    assert "التنظيم" in html
    assert "العودة إلى لوحة الإدارة" in html
    assert "إدارة الإيداعات" not in html
    assert "إدارة الطلبات" not in html
    assert 'id="workspace-switcher"' in html


def test_admin_home_hides_catalog_section_nav():
    html = _render(
        "index.html",
        **page_context(
            workspace_id=WORKSPACE_ADMIN,
            active_nav="home",
            page_title="لوحة تحكم سولديوم",
            page_heading="الإحصائيات الرئيسية",
            page_subheading="نظرة عامة",
        ),
    )
    assert "workspace-admin" in html
    assert "لوحة الإدارة" in html
    assert "إدارة الطلبات" in html
    assert 'href="/catalog/structure"' not in html
    assert "مساحات العمل" in html
    assert 'id="workspace-switcher"' in html


def test_catalog_placeholder_message():
    html = _render(
        "workspaces/catalog_placeholder.html",
        **page_context(
            workspace_id=WORKSPACE_CATALOG,
            active_nav="pricing",
            catalog_section="pricing",
            page_title="التسعير — إدارة الكاتالوج",
            page_heading="التسعير",
            page_subheading="قواعد وأسعار",
            placeholder_message="سيتم تنفيذ هذا القسم في مرحلة لاحقة",
        ),
    )
    assert "سيتم تنفيذ هذا القسم في مرحلة لاحقة" in html
    assert "التسعير" in html
    assert "workspace-catalog" in html


def test_legacy_services_stays_in_admin_workspace():
    html = _render(
        "catalog.html",
        **page_context(
            workspace_id=WORKSPACE_ADMIN,
            active_nav="services",
            page_title="أسعار الخدمات — سولديوم",
            page_heading="أسعار الخدمات",
            page_subheading="جميع الخدمات",
        ),
    )
    assert "workspace-admin" in html
    assert "أسعار الخدمات" in html


def test_workspace_templates_exist():
    root = Path(__file__).resolve().parent.parent / "templates"
    assert (root / "workspaces" / "catalog_home.html").is_file()
    assert (root / "workspaces" / "catalog_placeholder.html").is_file()
    assert (root / "workspaces" / "catalog_structure.html").is_file()
    assert (root / "workspaces" / "catalog_services.html").is_file()


def test_catalog_structure_responsive_rebuild():
    """Structure UI: desktop pane + mobile bottom sheet; single #inspector."""
    html = (
        Path(__file__).resolve().parent.parent
        / "templates"
        / "workspaces"
        / "catalog_structure.html"
    ).read_text(encoding="utf-8")
    js = (
        Path(__file__).resolve().parent.parent / "static" / "js" / "catalog_core_ui.js"
    ).read_text(encoding="utf-8")

    assert 'data-structure-ui="responsive-v2"' in html
    assert html.count('id="inspector"') == 1
    assert html.count('id="tree-root"') == 1
    assert 'id="structure-details-pane"' in html
    assert 'id="structure-sheet-backdrop"' in html
    assert 'id="structure-sheet-close"' in html
    assert "cat-structure-sheet-backdrop" in html
    assert "cat-structure-details-pane" in html

    # Mobile sheet — not sticky details in flow
    assert "translateY(110%)" in html
    assert ".cat-structure-details-pane.is-open" in html
    assert "body.cat-structure-sheet-open" in html

    # No legacy mobile CSS-order layout architecture
    assert "cat-structure-tree {\n    order:" not in html
    assert ".cat-structure-details {\n    order:" not in html
    assert "order: 2;" not in html

    # Desktop sticky details retained
    assert "@media (min-width: 1280px)" in html
    assert "position: sticky" in html
    assert "top: 5.5rem" in html

    # Overflow hygiene
    assert "min-width: 0" in html
    assert "overflow-wrap: anywhere" in html or "cat-tree-item-label" in html

    # JS sheet controls + scroll preserve
    assert "openStructureSheet" in js
    assert "closeStructureSheet" in js
    assert "withPreservedScroll" in js
    assert "cat-structure-sheet-open" in js
    assert "function mountStructure()" in js


def test_catalog_services_ui_declares_page_and_tree():
    """Regression: mountServices must declare pagination/tree locals (ReferenceError)."""
    import re

    js = (
        Path(__file__).resolve().parent.parent / "static" / "js" / "catalog_core_ui.js"
    ).read_text(encoding="utf-8")
    m = re.search(
        r"function mountServices\(\) \{(.*?)\n  function mountReview",
        js,
        re.S,
    )
    assert m, "mountServices not found"
    body = m.group(1)
    assert re.search(r"\blet\s+page\b", body), "mountServices missing let page"
    assert re.search(r"\blet\s+tree\b", body), "mountServices missing let tree"
    assert "URLSearchParams({ page" in body or "URLSearchParams({ page," in body
