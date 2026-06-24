"""Bridge to bot provider price sync from the dashboard."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


async def refresh_catalog_prices(
    *,
    provider_slug: str | None = None,
    active_only: bool = False,
) -> dict[str, Any]:
    from database_connector import DB_PATH

    bot_root = Path(__file__).resolve().parent.parent / "soldium-bot"
    root_str = str(bot_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    import database as bot_db
    import services.provider_price_sync as price_sync

    db_path = Path(DB_PATH)
    bot_db.DB_PATH = db_path
    price_sync.DB_PATH = db_path

    result = await price_sync.refresh_provider_prices(
        active_only=active_only,
        provider_slug=provider_slug,
        db_path=db_path,
    )
    return {
        "price_sync_updated": result.updated,
        "price_sync_unchanged": result.unchanged,
        "price_sync_missing": result.missing_in_api,
        "price_sync_skipped_admin": result.skipped_admin,
        "price_sync_errors": result.errors,
    }
