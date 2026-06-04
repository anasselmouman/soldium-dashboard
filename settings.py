"""Dashboard settings (defaults mirror soldium-bot/config.py)."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

MIN_DEPOSIT_DH = float(os.getenv("MIN_DEPOSIT_DH", "5.0"))
MAX_SINGLE_DEPOSIT_DH = float(os.getenv("MAX_SINGLE_DEPOSIT_DH", "50000"))
USDT_TO_DH_RATE = float(os.getenv("USDT_TO_DH_RATE", "10.0"))
MIN_CRYPTO_DEPOSIT_USDT = float(os.getenv("MIN_CRYPTO_DEPOSIT_USDT", "10.0"))
MIN_PAYPAL_DEPOSIT_USD = float(os.getenv("MIN_PAYPAL_DEPOSIT_USD", "5.0"))
