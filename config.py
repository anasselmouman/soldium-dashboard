"""Dashboard configuration from environment variables."""

from __future__ import annotations



import os

from pathlib import Path



from dotenv import load_dotenv



_DIR = Path(__file__).resolve().parent

load_dotenv(_DIR / ".env")

load_dotenv(_DIR.parent / "SOLDUIM" / ".env")



ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip()

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()

SECRET_KEY = os.getenv("SECRET_KEY", "").strip() or os.getenv(

    "ADMIN_PASSWORD",

    "change-me-in-production",

).strip()



SMM_KEY_INSTAGRAM = os.getenv("SMM_KEY_INSTAGRAM", "").strip()

SMM_KEY_FACEBOOK = os.getenv("SMM_KEY_FACEBOOK", "").strip()

SMM_KEY_TIKTOK = os.getenv("SMM_KEY_TIKTOK", "").strip()

SMM_KEY_DEFAULT = os.getenv("SMM_KEY_DEFAULT", "").strip()

API_URL = os.getenv("API_URL", "https://gozibra.com/api/v2").strip()



SMM_API_KEYS: dict[str, str] = {

    "instagram": SMM_KEY_INSTAGRAM,

    "facebook": SMM_KEY_FACEBOOK,

    "tiktok": SMM_KEY_TIKTOK,

    "default": SMM_KEY_DEFAULT,

}



SESSION_COOKIE_NAME = "soldium_admin_session"

# Default session: 30 days. With «remember me»: 90 days (overridable via .env).
SESSION_MAX_AGE_SECONDS = int(
    os.getenv("SESSION_MAX_AGE_SECONDS", str(30 * 24 * 3600)),
)
REMEMBER_ME_MAX_AGE_SECONDS = int(
    os.getenv("REMEMBER_ME_MAX_AGE_SECONDS", str(90 * 24 * 3600)),
)

RESET_TEST_DATA_TOKEN = os.getenv("RESET_TEST_DATA_TOKEN", "").strip()





def admin_credentials_configured() -> bool:

    return bool(ADMIN_USERNAME and ADMIN_PASSWORD and SECRET_KEY)

