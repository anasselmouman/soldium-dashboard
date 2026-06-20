"""Server-rendered dashboard pages."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import SESSION_COOKIE, verify_session_token

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter(tags=["web"])


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
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "page_title": "لوحة تحكم سولديوم",
            "active_nav": "home",
            "page_heading": "الإحصائيات الرئيسية",
            "page_subheading": "نظرة عامة على نشاط المنصة",
        },
    )


@router.get("/analytics")
async def analytics_page(request: Request):
    return templates.TemplateResponse(
        request,
        "analytics.html",
        {
            "page_title": "مركز الإحصائيات — سولديوم",
            "active_nav": "analytics",
            "page_heading": "مركز الإحصائيات الشامل",
            "page_subheading": "المرحلة ١ — السيولة المالية والمال الحر",
        },
    )


@router.get("/deposits")
async def deposits_page(request: Request):
    return templates.TemplateResponse(
        request,
        "deposits.html",
        {
            "page_title": "لوحة تحكم سولديوم",
            "active_nav": "deposits",
            "page_heading": "إدارة الإيداعات",
            "page_subheading": "طلبات الإيداع المعلقة",
        },
    )


@router.get("/dashboard/users/{user_id}")
async def user_profile_page(request: Request, user_id: int):
    return templates.TemplateResponse(
        request,
        "user_profile.html",
        {
            "page_title": f"الملف الشامل للعميل — {user_id}",
            "active_nav": "users",
            "page_heading": "الملف الشامل للعميل",
            "page_subheading": "مراجعة مالية شاملة قبل اعتماد السحوبات",
            "profile_user_id": user_id,
        },
    )


@router.get("/users")
async def users_page(request: Request):
    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "page_title": "لوحة تحكم سولديوم",
            "active_nav": "users",
            "page_heading": "إدارة المستخدمين",
            "page_subheading": "الرصيد، الإنفاق، والإحالات",
        },
    )


@router.get("/orders")
async def orders_page(request: Request):
    return templates.TemplateResponse(
        request,
        "orders.html",
        {
            "page_title": "لوحة تحكم سولديوم",
            "active_nav": "orders",
            "page_heading": "إدارة الطلبات",
            "page_subheading": "عرض وتعديل حالات الطلبات",
        },
    )


@router.get("/manual-orders")
async def manual_orders_page(request: Request):
    return templates.TemplateResponse(
        request,
        "manual_orders.html",
        {
            "page_title": "لوحة تحكم سولديوم",
            "active_nav": "manual_orders",
            "page_heading": "طلبات التنفيذ اليدوي",
            "page_subheading": "طابور الخدمات التي تحتاج معالجة يدوية من الإدارة",
        },
    )


@router.get("/withdrawals")
async def withdrawals_page(request: Request):
    return templates.TemplateResponse(
        request,
        "withdrawals.html",
        {
            "page_title": "لوحة تحكم سولديوم",
            "active_nav": "withdrawals",
            "page_heading": "إدارة السحوبات",
            "page_subheading": "طلبات السحب المعلقة",
        },
    )


@router.get("/services")
async def services_page(request: Request):
    return templates.TemplateResponse(
        request,
        "catalog.html",
        {
            "page_title": "أسعار الخدمات — سولديوم",
            "active_nav": "services",
            "page_heading": "أسعار الخدمات",
            "page_subheading": "جميع الخدمات في قاعدة البيانات — تفعيل، إيقاف، وتعديل الأسعار",
        },
    )


@router.get("/broadcast")
async def broadcast_page(request: Request):
    return templates.TemplateResponse(
        request,
        "broadcast.html",
        {
            "page_title": "نظام البث — سولديوم",
            "active_nav": "broadcast",
            "page_heading": "نظام البث",
            "page_subheading": "رسائل مخصّصة، بث جماعي، وإعلانات مؤقتة عبر تيليغرام",
        },
    )
