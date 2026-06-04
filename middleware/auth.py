"""Protect dashboard pages and API routes behind admin session."""
from __future__ import annotations

from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from auth import SESSION_COOKIE, refresh_session_cookie, verify_session_token
from config import admin_credentials_configured

_PUBLIC_EXACT = frozenset({"/login"})
_PUBLIC_PREFIXES = ("/static/", "/assets/")
_AUTH_API_PREFIX = "/api/auth/"


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def _wants_json(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        if _is_public(path) or path.startswith(_AUTH_API_PREFIX):
            return await call_next(request)

        if not admin_credentials_configured():
            if _wants_json(request):
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": "لم يتم ضبط بيانات الدخول في ملف البيئة (.env).",
                    },
                )
            return RedirectResponse(url="/login", status_code=303)

        session_token = request.cookies.get(SESSION_COOKIE)
        username = verify_session_token(session_token)
        if username:
            request.state.admin_username = username
            response = await call_next(request)
            refresh_session_cookie(response, session_token)
            return response

        if _wants_json(request):
            return JSONResponse(
                status_code=401,
                content={"detail": "يجب تسجيل الدخول للوصول إلى هذه الواجهة."},
            )

        next_path = quote(path, safe="/")
        return RedirectResponse(url=f"/login?next={next_path}", status_code=303)
