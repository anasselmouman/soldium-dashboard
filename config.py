"""Dashboard configuration from environment variables."""

from __future__ import annotations



import os

from pathlib import Path



from dotenv import load_dotenv



_DIR = Path(__file__).resolve().parent

load_dotenv(_DIR / ".env")

load_dotenv(_DIR.parent / "soldium-bot" / ".env")



ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip()

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()

SECRET_KEY = os.getenv("SECRET_KEY", "").strip() or os.getenv(
    "ADMIN_PASSWORD",
    "change-me-in-production",
).strip()


SESSION_COOKIE_NAME = "soldium_admin_session"

# Default session: 30 days. With «remember me»: 90 days (overridable via .env).
SESSION_MAX_AGE_SECONDS = int(
    os.getenv("SESSION_MAX_AGE_SECONDS", str(30 * 24 * 3600)),
)
REMEMBER_ME_MAX_AGE_SECONDS = int(
    os.getenv("REMEMBER_ME_MAX_AGE_SECONDS", str(90 * 24 * 3600)),
)

# ─── Admin alerts (bot health / stuck orders) ───
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


ADMIN_TELEGRAM_ID = _env_int("ADMIN_ID", 0)
ALERT_STUCK_EXECUTION_HOURS = _env_int("ALERT_STUCK_EXECUTION_HOURS", 24)
ALERT_STUCK_SUBMITTED_HOURS = _env_int("ALERT_STUCK_SUBMITTED_HOURS", 12)
ALERT_OLD_MANUAL_HOURS = _env_int("ALERT_OLD_MANUAL_HOURS", 24)
ALERT_OLD_DEPOSIT_HOURS = _env_int("ALERT_OLD_DEPOSIT_HOURS", 48)
ALERT_OLD_WITHDRAWAL_HOURS = _env_int("ALERT_OLD_WITHDRAWAL_HOURS", 48)
ALERT_PROVIDER_BALANCE_MIN_USD = _env_float("ALERT_PROVIDER_BALANCE_MIN_USD", 50.0)
ALERT_STALE_PRICES_DAYS = _env_int("ALERT_STALE_PRICES_DAYS", 7)
ALERT_SCAN_INTERVAL_MINUTES = _env_int("ALERT_SCAN_INTERVAL_MINUTES", 5)
ALERT_TELEGRAM_ON_CRITICAL = _env_bool("ALERT_TELEGRAM_ON_CRITICAL", True)




def admin_credentials_configured() -> bool:

    return bool(ADMIN_USERNAME and ADMIN_PASSWORD and SECRET_KEY)

