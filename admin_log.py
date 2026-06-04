"""Structured logging for admin actions on deposits."""
from __future__ import annotations

import logging
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent / "logs"
_LOG_FILE = _LOG_DIR / "admin_actions.log"

logger = logging.getLogger("soldium.admin")


def setup_admin_logging() -> None:
    if logger.handlers:
        return
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    db_logger = logging.getLogger("soldium.db")
    if not db_logger.handlers:
        db_console = logging.StreamHandler()
        db_console.setFormatter(formatter)
        db_logger.addHandler(db_console)
        db_logger.setLevel(logging.INFO)

    notify_logger = logging.getLogger("soldium.notifier")
    if not notify_logger.handlers:
        notify_console = logging.StreamHandler()
        notify_console.setFormatter(formatter)
        notify_logger.addHandler(notify_console)
        notify_logger.setLevel(logging.INFO)
        notify_logger.propagate = False
