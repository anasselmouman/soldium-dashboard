# -*- coding: utf-8 -*-
"""Shared storefront target/link validation (Legacy-aligned, Telegram-free).

Extracted from soldium-bot ``utils.order_flow`` / ``utils.notices`` so
StorefrontAdapter and Legacy can share the same rules without Adapter →
Telegram handler coupling.
"""

from __future__ import annotations

import re
from html import escape
from typing import Any
from urllib.parse import urlparse

_COMMA_SPLIT_RE = re.compile(r"[,،]\s*")
_X_USERNAME_RE = re.compile(r"^@([A-Za-z0-9_]{1,15})$")

PLATFORM_HOST_RULES: dict[str, tuple[str, ...]] = {
    "instagram": ("instagram.com", "instagr.am"),
    "facebook": ("facebook.com", "fb.watch", "fb.com"),
    "tiktok": ("tiktok.com", "vt.tiktok.com"),
    "youtube": ("youtube.com", "youtu.be"),
    "telegram": ("t.me", "telegram.me"),
    "x": ("x.com", "twitter.com", "mobile.twitter.com"),
}

PLATFORM_DISPLAY_NAMES: dict[str, str] = {
    "instagram": "إنستغرام",
    "facebook": "فيسبوك",
    "tiktok": "تيك توك",
    "youtube": "يوتيوب",
    "telegram": "تيليجرام",
    "x": "X (تويتر)",
}

_LINK_PROMPTS: dict[str, tuple[str, bool]] = {
    "x_live_broadcast": (
        "يرجى إرسال رابط البث المباشر (Live Link) الجاري حالياً.",
        False,
    ),
    "x_direct_messages": (
        "أرسل نص الطلب على سطرين:\n\n"
        "السطر 1 (LINK): رابط حسابك على X أو @yourusername\n\n"
        "السطر 2 (USERNAMES): من 5 إلى 10 يوزرات مفصولة بفواصل. مثال:\n"
        "<code>elonmusk, dogecoin, Nike, tesla</code>\n\n"
        "تجنّب الحسابات المقفلة خصوصياً (Private).",
        False,
    ),
    "x_spaces": (
        "أرسل رابط السبيس فقط. يجب أن يحتوي الرابط على كلمة <code>spaces</code>.",
        False,
    ),
}


def resolve_link_prompt(
    platform_key: str,
    section_key: str | None,
    subsection_key: str | None = None,
    *,
    service: dict | None = None,
) -> tuple[str | None, bool, bool]:
    """Prompt text + allow_username + allow_free_text (Legacy parity)."""
    svc = service or {}
    pk = str(platform_key or "").strip()
    sk = str(section_key or "").strip()
    if pk == "subscriptions" and sk in {"iptv_wc2026", "iptv_panel"}:
        return (
            "✅ في مكان الرابط اكتب أي شيء: اسمك أو دولتك.",
            False,
            True,
        )
    key = str(svc.get("link_prompt_key") or "").strip()
    if key and key in _LINK_PROMPTS:
        text, allow_username = _LINK_PROMPTS[key]
        return text, allow_username, False

    sk = sk or None
    ssk = str(subsection_key or "").strip() or None

    if pk == "telegram" and ssk == "future_posts":
        return (
            "أرسل رابط <b>آخر منشور</b> في القناة (مثال: https://t.me/username/123). "
            "لن تُطبَّق المشاهدات على هذا المنشور، بل على المنشورات القادمة فقط.",
            False,
            False,
        )
    if pk == "telegram" and ssk == "past_posts":
        return (
            "أرسل رابط <b>القناة العام</b> فقط (مثال: https://t.me/username أو @username). "
            "لا تضع رابط منشور فردي.",
            True,
            False,
        )
    if pk == "telegram" and sk in {"members", "channel_members"}:
        return "أرسل رابط القناة أو المجموعة التي تريد إضافة الأعضاء إليها.", True, False
    if pk == "telegram" and sk == "post_share":
        return "أرسل رابط المنشور الذي تريد زيادة المشاركات عليه.", False, False
    if pk == "telegram" and sk == "start_bot":
        return "أرسل رابط البوت الذي تريد تشغيل الخدمة عليه.", False, False
    if pk == "telegram" and sk == "automatic_interactions":
        return (
            "أرسل رابط آخر منشور في القناة (مثال: https://t.me/username/123) لتحديد الحساب. "
            "لن تُطبَّق المشاهدات على هذا المنشور، بل على المنشورات الجديدة فقط.",
            True,
            False,
        )
    if pk == "x" and sk == "followers":
        return "أرسل رابط حسابك على X (تويتر) أو المعرف الخاص بك.", True, False
    if pk == "x" and sk == "likes":
        return "أرسل رابط التغريدة التي تريد زيادة الإعجابات عليها.", False, False
    if pk == "x" and sk in {"views", "video_views"}:
        return "أرسل رابط التغريدة التي تحتوي على الفيديو المطلوب زيادة مشاهداته.", False, False
    if pk == "x" and sk == "mentions":
        return (
            "أرسل رابط التغريدة لتنفيذ خدمة المنشن عليها. "
            "يجب أن يحتوي الرابط على <code>/status/</code>."
        ), False, False
    if pk == "x" and sk == "direct_messages":
        text, allow = _LINK_PROMPTS["x_direct_messages"]
        return text, allow, False
    if pk == "x" and sk == "spaces":
        text, allow = _LINK_PROMPTS["x_spaces"]
        return text, allow, False
    if pk == "x" and sk == "live_broadcast":
        text, allow = _LINK_PROMPTS["x_live_broadcast"]
        return text, allow, False

    return None, False, False


def _platform_mismatch_error(platform_key: str) -> str:
    name = PLATFORM_DISPLAY_NAMES.get(platform_key, platform_key)
    return f"الرابط غير صحيح لهذه الخدمة. أرسل رابطاً تابعاً لمنصة <b>{name}</b>."


def _link_matches_platform(raw: str, platform_key: str) -> bool:
    lowered = raw.strip().lower()
    if not lowered:
        return False

    keywords = PLATFORM_HOST_RULES.get(platform_key, ())
    if keywords and any(keyword in lowered for keyword in keywords):
        return True

    if platform_key == "telegram" and re.fullmatch(r"@[A-Za-z0-9_]{3,}", raw.strip()):
        return True
    if platform_key == "x" and _X_USERNAME_RE.fullmatch(raw.strip()):
        return True

    return not keywords


def _is_http_url(text: str) -> bool:
    lowered = text.strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _validate_x_direct_messages(raw: str) -> tuple[bool, str]:
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 2:
        return (
            False,
            "أرسل الطلب على <b>سطرين</b>:\n"
            "السطر 1: رابط حسابك على X أو @yourusername\n"
            "السطر 2: من 5 إلى 10 يوزرات مفصولة بفواصل.",
        )
    first_line = lines[0]
    if not (_link_matches_platform(first_line, "x") or _X_USERNAME_RE.fullmatch(first_line)):
        return False, "السطر الأول يجب أن يكون رابط X أو @username لحسابك."
    usernames = [u.strip().lstrip("@") for u in _COMMA_SPLIT_RE.split(lines[1]) if u.strip()]
    if len(usernames) < 5 or len(usernames) > 10:
        return (
            False,
            "السطر الثاني يجب أن يحتوي على <b>من 5 إلى 10</b> يوزرات مفصولة بفاصلة.",
        )
    for user in usernames:
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", user):
            return False, f"يوزر غير صالح: <code>{escape(user)}</code>"
    return True, ""


def _is_x_live_broadcast_url(lowered: str) -> bool:
    return "/i/broadcasts/" in lowered or "/i/broadcast/" in lowered


def _telegram_path_parts(raw: str) -> list[str] | None:
    first = raw.splitlines()[0].strip()
    if first.startswith("@"):
        user = first.lstrip("@")
        return [user] if re.fullmatch(r"[A-Za-z0-9_]{3,}", user) else None
    if not _is_http_url(first):
        return None
    lowered = first.lower()
    if "t.me" not in lowered and "telegram.me" not in lowered:
        return None
    path = urlparse(first).path.strip("/")
    return [p for p in path.split("/") if p] or None


def _is_telegram_post_url(raw: str) -> bool:
    parts = _telegram_path_parts(raw)
    if not parts:
        return False
    if parts[0] == "c":
        return len(parts) >= 3 and parts[-1].isdigit()
    return len(parts) >= 2 and parts[-1].isdigit()


def _is_telegram_channel_url(raw: str) -> bool:
    parts = _telegram_path_parts(raw)
    if not parts:
        return False
    if parts[0] == "c":
        return len(parts) == 2 and parts[1].isdigit()
    if len(parts) == 1:
        return not parts[0].isdigit()
    return False


def _telegram_link_mode(section_key: str | None, subsection_key: str | None) -> str | None:
    sk = str(section_key or "").strip()
    ssk = str(subsection_key or "").strip()
    if sk == "automatic_interactions":
        return "post"
    if sk == "post_share":
        return "post"
    if ssk == "future_posts":
        return "post"
    if ssk == "past_posts":
        return "channel"
    return None


def _validate_section_link_rules(
    raw: str,
    platform_key: str,
    section_key: str | None,
    subsection_key: str | None = None,
    *,
    service: dict | None = None,
    service_id: str | None = None,
) -> tuple[bool, str]:
    sk = str(section_key or "").strip()
    ssk = str(subsection_key or "").strip()
    svc = service or {}
    _ = service_id  # API compatibility only — never Provider/Legacy ID for target semantics

    if platform_key == "telegram":
        link_mode = _telegram_link_mode(sk or None, ssk or None)
        if link_mode == "post" and not _is_telegram_post_url(raw):
            return (
                False,
                "أرسل <b>رابط منشور</b> بصيغة <code>https://t.me/username/123</code> "
                "(أو <code>https://t.me/c/...</code> للقنوات الخاصة). "
                "رابط القناة وحده غير كافٍ لهذه الخدمة.",
            )
        if link_mode == "channel" and not _is_telegram_channel_url(raw):
            return (
                False,
                "أرسل <b>رابط القناة العام</b> فقط (مثال: <code>https://t.me/username</code>) "
                "وليس رابط منشور فردي.",
            )

    if platform_key == "x" and sk == "direct_messages":
        return _validate_x_direct_messages(raw)

    if platform_key == "x" and sk == "mentions":
        if "/status/" not in raw.lower():
            return (
                False,
                "يجب أن يحتوي الرابط على <code>/status/</code> (رابط التغريدة وليس الحساب فقط).",
            )

    if platform_key == "x" and sk == "spaces":
        if "spaces" not in raw.lower():
            return False, "أرسل رابط السبيس فقط. يجب أن يحتوي الرابط على كلمة <code>spaces</code>."

    if platform_key == "x" and sk == "live_broadcast":
        lowered = raw.lower()
        if not _is_x_live_broadcast_url(lowered):
            return (
                False,
                "أرسل رابط البث المباشر الجاري. يجب أن يحتوي على <code>/i/broadcasts/</code> أو مسار بث صالح.",
            )

    needs_comment_link = _service_requires_comment_link(svc)
    if needs_comment_link:
        lowered = raw.lower()
        if "/comment" not in lowered and "comment_id" not in lowered:
            return (
                False,
                "⚠️ ضع <b>رابط التعليق</b> وليس رابط الفيديو. "
                "من المتصفح على الحاسوب: اضغط تاريخ التعليق وانسخ الرابط.",
            )

    return True, ""


def _service_requires_comment_link(service: dict | None) -> bool:
    """Target semantics from authored link_type / target_link_type — never Provider IDs."""
    if not isinstance(service, dict):
        return False
    for key in ("link_type", "target_link_type"):
        if str(service.get(key) or "").strip().lower() == "comment":
            return True
    return False

def validate_platform_link(
    link: str,
    platform_key: str,
    section_key: str | None = None,
    subsection_key: str | None = None,
    *,
    service: dict | None = None,
    service_id: str | None = None,
    allow_username: bool = False,
) -> tuple[bool, str]:
    """Validate link by platform/section (Legacy parity)."""
    _ = subsection_key
    raw = (link or "").strip()
    if not raw:
        return False, _platform_mismatch_error(platform_key)

    sk = str(section_key or "").strip()
    if platform_key == "subscriptions" and sk in {"iptv_wc2026", "iptv_panel"}:
        return True, ""
    is_x_dm = platform_key == "x" and sk == "direct_messages"

    if not is_x_dm:
        first_line = raw.splitlines()[0].strip()
        if allow_username:
            if not (
                _is_http_url(first_line)
                or (platform_key == "telegram" and re.fullmatch(r"@[A-Za-z0-9_]{3,}", first_line))
                or (platform_key == "x" and _X_USERNAME_RE.fullmatch(first_line))
            ):
                return (
                    False,
                    "أرسل رابطاً يبدأ بـ <code>http://</code> أو <code>https://</code> "
                    "أو معرفاً بصيغة <code>@username</code>.",
                )
        elif not _is_http_url(first_line):
            return (
                False,
                "أرسل الرابط كاملاً، ويجب أن يبدأ بـ <code>http://</code> أو <code>https://</code>.",
            )

    check_text = raw.splitlines()[0].strip() if is_x_dm else raw
    if not _link_matches_platform(check_text, platform_key):
        return False, _platform_mismatch_error(platform_key)

    return _validate_section_link_rules(
        raw,
        platform_key,
        section_key,
        subsection_key,
        service=service,
        service_id=service_id or (str(service.get("id", "")) if service else None),
    )


def validate_order_target(
    target: str | None,
    *,
    platform_key: str,
    section_key: str | None,
    subsection_key: str | None = None,
    link_prompt_key: str | None = None,
    link_type: str | None = None,
    required: bool = True,
) -> tuple[bool, str]:
    """Storefront/Legacy composition: resolve prompt flags then validate link."""
    raw = "" if target is None else str(target).strip()
    if required and not raw:
        return False, "الهدف مطلوب لهذه الخدمة."

    if not required and not raw:
        return True, ""

    service: dict[str, Any] = {}
    if link_prompt_key:
        service["link_prompt_key"] = str(link_prompt_key).strip()
    if link_type:
        service["link_type"] = str(link_type).strip()

    _, allow_username, allow_free_text = resolve_link_prompt(
        platform_key,
        section_key,
        subsection_key,
        service=service,
    )
    if allow_free_text:
        return (True, "") if raw else (False, "اكتب اسمك أو دولتك في خانة الرابط.")
    return validate_platform_link(
        raw,
        platform_key,
        section_key=section_key,
        subsection_key=subsection_key,
        service=service,
        allow_username=allow_username,
    )
