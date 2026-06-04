"""Signed cookie session authentication for the admin dashboard."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.responses import Response

from config import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    REMEMBER_ME_MAX_AGE_SECONDS,
    SECRET_KEY,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    admin_credentials_configured,
)

SESSION_COOKIE = SESSION_COOKIE_NAME


def credentials_valid(username: str, password: str) -> bool:
    if not admin_credentials_configured():
        return False
    user_ok = secrets.compare_digest(username.strip(), ADMIN_USERNAME)
    pass_ok = secrets.compare_digest(password, ADMIN_PASSWORD)
    return user_ok and pass_ok


def session_max_age_seconds(*, remember_me: bool) -> int:
    if remember_me:
        return REMEMBER_ME_MAX_AGE_SECONDS
    return SESSION_MAX_AGE_SECONDS


def _sign(payload_b64: str) -> str:
    key = SECRET_KEY.encode("utf-8")
    return hmac.new(key, payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_token(
    username: str,
    *,
    max_age_seconds: int | None = None,
    remember_me: bool = False,
) -> str:
    age = max_age_seconds if max_age_seconds is not None else SESSION_MAX_AGE_SECONDS
    payload: dict[str, Any] = {
        "sub": username,
        "exp": int(time.time()) + age,
        "rem": bool(remember_me),
    }
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    ).decode("ascii")
    return f"{payload_b64}.{_sign(payload_b64)}"


def _decode_session_payload(token: str) -> dict[str, Any] | None:
    parts = token.split(".", 1)
    if len(parts) != 2:
        return None
    payload_b64, signature = parts
    if not secrets.compare_digest(_sign(payload_b64), signature):
        return None
    try:
        raw = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def verify_session_token(token: str | None) -> str | None:
    if not token or not SECRET_KEY:
        return None
    data = _decode_session_payload(token)
    if not data:
        return None
    exp = int(data.get("exp") or 0)
    if exp < int(time.time()):
        return None
    sub = str(data.get("sub") or "").strip()
    if not sub or sub != ADMIN_USERNAME:
        return None
    return sub


def session_remember_from_token(token: str | None) -> bool:
    if not token:
        return True
    data = _decode_session_payload(token)
    if not data:
        return True
    return bool(data.get("rem", True))


def set_session_cookie(
    response: Response,
    username: str,
    *,
    remember_me: bool = True,
) -> None:
    """Attach a persistent signed session cookie (survives browser restarts)."""
    max_age = session_max_age_seconds(remember_me=remember_me)
    token = create_session_token(
        username,
        max_age_seconds=max_age,
        remember_me=remember_me,
    )
    expires = datetime.now(timezone.utc) + timedelta(seconds=max_age)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=max_age,
        expires=expires,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


def refresh_session_cookie(response: Response, token: str | None) -> None:
    """Extend session expiry on each authenticated request (sliding session)."""
    username = verify_session_token(token)
    if not username:
        return
    set_session_cookie(
        response,
        username,
        remember_me=session_remember_from_token(token),
    )
