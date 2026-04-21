from __future__ import annotations

from dataclasses import dataclass
import os


DEFAULT_LOCAL_DATABASE_URL = "sqlite+pysqlite:///./recently_played.db"
DEFAULT_WEBHOOK_SECRET = "local-development-secret"


@dataclass(frozen=True)
class Settings:
    app_name: str
    database_url: str
    webhook_secret: str
    display_timezone: str
    max_events: int = 100
    home_recent_limit: int = 8
    api_recent_limit: int = 10


def load_settings(
    database_url: str | None = None,
    webhook_secret: str | None = None,
    app_name: str | None = None,
    display_timezone: str | None = None,
) -> Settings:
    resolved_database_url = (
        database_url
        or os.getenv("DATABASE_URL")
        or (DEFAULT_LOCAL_DATABASE_URL if os.getenv("VERCEL") != "1" else "")
    )
    resolved_webhook_secret = webhook_secret or os.getenv("WEBHOOK_SECRET") or DEFAULT_WEBHOOK_SECRET
    resolved_app_name = app_name or os.getenv("APP_NAME") or "Live listening diary"
    resolved_timezone = display_timezone or os.getenv("DISPLAY_TIMEZONE") or "UTC"

    if not resolved_database_url:
        raise RuntimeError("DATABASE_URL is required when running on Vercel.")

    return Settings(
        app_name=resolved_app_name,
        database_url=resolved_database_url,
        webhook_secret=resolved_webhook_secret,
        display_timezone=resolved_timezone,
    )
