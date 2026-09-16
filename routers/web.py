"""Server-rendered dashboard pages."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import SESSION_COOKIE, verify_session_token
from workspaces import (
    WORKSPACE_ADMIN,
    WORKSPACE_CATALOG,
    page_context,
)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter(tags=["web"])

_CATALOG_PLACEHOLDER = "سيتم تنفيذ هذا القسم في مرحلة لاحقة"


def _admin_page(
    request: Request,
    template_name: str,
    *,
    active_nav: str,
    page_title: str,
    page_heading: str,
    page_subheading: str = "",
    **extra,
):
    return templates.TemplateResponse(
        request,
        template_name,
        page_context(
            workspace_id=WORKSPACE_ADMIN,
            active_nav=active_nav,
            page_title=page_title,
            page_heading=page_heading,
            page_subheading=page_subheading,
            **extra,
        ),
    )


def _catalog_placeholder(
    request: Request,
    *,
    section: str,
    page_heading: str,
    page_subheading: str,
):
    return templates.TemplateResponse(
        request,
        "workspaces/catalog_placeholder.html",
        page_context(
            workspace_id=WORKSPACE_CATALOG,
            active_nav=section,
            catalog_section=section,
            page_title=f"{page_heading} — إدارة الكاتالوج",
            page_heading=page_heading,
            page_subheading=page_subheading,
            placeholder_message=_CATALOG_PLACEHOLDER,
        ),
    )


@router.get("/login")
async def login_page(request: Request):
    if verify_session_token(request.cookies.get(SESSION_COOKIE)):
        next_path = request.query_params.get("next") or "/"
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"
        return RedirectResponse(url=next_path, status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"page_title": "تسجيل الدخول — سولديوم"},
    )


@router.get("/")
async def home(request: Request):
    return _admin_page(
        request,
        "index.html",
        active_nav="home",
        page_title="لوحة تحكم سولديوم",
        page_heading="الإحصائيات الرئيسية",
        page_subheading="نظرة عامة على نشاط المنصة",
    )


@router.get("/analytics")
async def analytics_page(request: Request):
    return _admin_page(
        request,
        "analytics.html",
        active_nav="analytics",
        page_title="مركز الإحصائيات — سولديوم",
        page_heading="مركز الإحصائيات الشامل",
        page_subheading="المرحلة ١ — السيولة المالية والمال الحر",
    )


@router.get("/deposits")
async def deposits_page(request: Request):
    return _admin_page(
        request,
        "deposits.html",
        active_nav="deposits",
        page_title="لوحة تحكم سولديوم",
        page_heading="إدارة الإيداعات",
        page_subheading="طلبات الإيداع المعلقة",
    )


@router.get("/dashboard/users/{user_id}")
async def user_profile_page(request: Request, user_id: int):
    return _admin_page(
        request,
        "user_profile.html",
        active_nav="users",
        page_title=f"الملف الشامل للعميل — {user_id}",
        page_heading="الملف الشامل للعميل",
        page_subheading="مراجعة مالية شاملة قبل اعتماد السحوبات",
        profile_user_id=user_id,
    )


@router.get("/users")
async def users_page(request: Request):
    return _admin_page(
        request,
        "users.html",
        active_nav="users",
        page_title="لوحة تحكم سولديوم",
        page_heading="إدارة المستخدمين",
        page_subheading="الرصيد، الإنفاق، والإحالات",
    )


@router.get("/orders")
async def orders_page(request: Request):
    return _admin_page(
        request,
        "orders.html",
        active_nav="orders",
        page_title="لوحة تحكم سولديوم",
        page_heading="إدارة الطلبات",
        page_subheading="عرض وتعديل حالات الطلبات",
    )


@router.get("/manual-orders")
async def manual_orders_page(request: Request):
    return _admin_page(
        request,
        "manual_orders.html",
        active_nav="manual_orders",
        page_title="لوحة تحكم سولديوم",
        page_heading="طلبات التنفيذ اليدوي",
        page_subheading="طابور الخدمات التي تحتاج معالجة يدوية من الإدارة",
    )


@router.get("/scheduled-orders")
async def scheduled_orders_page(request: Request):
    return _admin_page(
        request,
        "scheduled_orders.html",
        active_nav="scheduled_orders",
        page_title="لوحة تحكم سولديوم",
        page_heading="الطلبات المجدولة",
        page_subheading="تنفيذ دوري لطلبات مبنية على طلبات موجودة — كمية ثابتة أو عشوائية",
    )


@router.get("/withdrawals")
async def withdrawals_page(request: Request):
    return _admin_page(
        request,
        "withdrawals.html",
        active_nav="withdrawals",
        page_title="لوحة تحكم سولديوم",
        page_heading="إدارة السحوبات",
        page_subheading="طلبات السحب المعلقة",
    )


@router.get("/providers")
async def providers_page(request: Request):
    return _admin_page(
        request,
        "providers.html",
        active_nav="providers",
        page_title="المزوّدون — سولديوم",
        page_heading="إدارة المزوّدين",
        page_subheading="مزوّدو خدمات SMM وحسابات API",
    )


@router.get("/services")
async def services_page(request: Request):
    return _admin_page(
        request,
        "catalog.html",
        active_nav="services",
        page_title="أسعار الخدمات — سولديوم",
        page_heading="أسعار الخدمات",
        page_subheading="جميع الخدمات في قاعدة البيانات — تفعيل، إيقاف، وتعديل الأسعار",
    )


@router.get("/broadcast")
async def broadcast_page(request: Request):
    return _admin_page(
        request,
        "broadcast.html",
        active_nav="broadcast",
        page_title="نظام البث — سولديوم",
        page_heading="نظام البث",
        page_subheading="رسائل مخصّصة، بث جماعي، وإعلانات مؤقتة عبر تيليغرام",
    )


@router.get("/notifications")
async def notifications_page(request: Request):
    return _admin_page(
        request,
        "notifications.html",
        active_nav="notifications",
        page_title="إشعارات الأدمن — سولديوم",
        page_heading="مركز إشعارات الأدمن",
        page_subheading="سجل ما يُرسل إلى تيليغرام — طلبات، إيداعات، سحوبات، وتحذيرات",
    )


# ─── Catalog Management workspace (shell only — Phase 1) ───


@router.get("/catalog")
async def catalog_home(request: Request):
    return templates.TemplateResponse(
        request,
        "workspaces/catalog_home.html",
        page_context(
            workspace_id=WORKSPACE_CATALOG,
            active_nav="home",
            catalog_section="home",
            page_title="إدارة الكاتالوج — سولديوم",
            page_heading="إدارة الكاتالوج",
            page_subheading="مساحة عمل مخصّصة لإدارة هيكل الخدمات والعروض",
        ),
    )


@router.get("/catalog/services")
async def catalog_services_shell(request: Request):
    return templates.TemplateResponse(
        request,
        "workspaces/catalog_services.html",
        page_context(
            workspace_id=WORKSPACE_CATALOG,
            active_nav="services",
            catalog_section="services",
            page_title="الخدمات — إدارة الكاتالوج",
            page_heading="الخدمات",
            page_subheading="هوية خدمات سولديوم ومكانها في الهيكل",
        ),
    )


@router.get("/catalog/structure")
async def catalog_structure_shell(request: Request):
    return templates.TemplateResponse(
        request,
        "workspaces/catalog_structure.html",
        page_context(
            workspace_id=WORKSPACE_CATALOG,
            active_nav="structure",
            catalog_section="structure",
            page_title="التنظيم — إدارة الكاتالوج",
            page_heading="التنظيم",
            page_subheading="هيكل غير محدود العمق للأقسام والخدمات",
        ),
    )


@router.get("/catalog/review")
async def catalog_review_shell(request: Request):
    return templates.TemplateResponse(
        request,
        "workspaces/catalog_review.html",
        page_context(
            workspace_id=WORKSPACE_CATALOG,
            active_nav="review",
            catalog_section="review",
            page_title="المراجعة — إدارة الكاتالوج",
            page_heading="المراجعة",
            page_subheading="مراجعة جاهزية الخدمات قبل النشر",
        ),
    )


@router.get("/catalog/pricing")
async def catalog_pricing_shell(request: Request):
    return _catalog_placeholder(
        request,
        section="pricing",
        page_heading="التسعير",
        page_subheading="قواعد وأسعار خدمات الكاتالوج",
    )


@router.get("/catalog/sources")
async def catalog_sources_shell(request: Request):
    return templates.TemplateResponse(
        request,
        "workspaces/catalog_services.html",
        page_context(
            workspace_id=WORKSPACE_CATALOG,
            active_nav="sources",
            catalog_section="sources",
            page_title="مصادر التنفيذ — إدارة الكاتالوج",
            page_heading="مصادر التنفيذ",
            page_subheading="معرّف المزود أولًا · استبدال مصدر التنفيذ بأمان",
        ),
    )


@router.get("/catalog/sync")
async def catalog_sync_shell(request: Request):
    return _catalog_placeholder(
        request,
        section="sync",
        page_heading="المزامنة",
        page_subheading="مزامنة بيانات المزوّدين مع الكاتالوج",
    )


@router.get("/catalog/history")
async def catalog_history_shell(request: Request):
    return _catalog_placeholder(
        request,
        section="history",
        page_heading="سجل التغييرات",
        page_subheading="تتبع تعديلات الكاتالوج",
    )
