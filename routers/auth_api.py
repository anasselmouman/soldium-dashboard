"""Admin login and logout API."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from auth import SESSION_COOKIE, credentials_valid, set_session_cookie
from config import admin_credentials_configured
from schemas import LoginRequest
from utils.messages_ar import INVALID_LOGIN, LOGIN_NOT_CONFIGURED

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
async def login(body: LoginRequest):
    if not admin_credentials_configured():
        raise HTTPException(status_code=503, detail=LOGIN_NOT_CONFIGURED)

    if not credentials_valid(body.username, body.password):
        raise HTTPException(status_code=401, detail=INVALID_LOGIN)

    response = JSONResponse(
        content={"ok": True, "message": "تم تسجيل الدخول بنجاح.", "redirect": "/"},
    )
    set_session_cookie(
        response,
        body.username.strip(),
        remember_me=body.remember_me,
    )
    return response


@router.post("/logout")
async def logout():
    response = JSONResponse(content={"ok": True, "message": "تم تسجيل الخروج."})
    response.delete_cookie(key=SESSION_COOKIE, path="/")
    return response


@router.get("/session")
async def session_status(request: Request):
    from auth import verify_session_token

    user = verify_session_token(request.cookies.get(SESSION_COOKIE))
    if not user:
        raise HTTPException(status_code=401, detail="غير مسجّل الدخول.")
    return {"ok": True, "username": user}
