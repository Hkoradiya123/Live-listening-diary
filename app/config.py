from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    app_name: str
    database_url: str
    display_timezone: str
    max_events: int = 100
    home_recent_limit: int = 3
    api_recent_limit: int = 10


def load_settings(
    database_url: str | None = None,
    app_name: str | None = None,
    display_timezone: str | None = None,
) -> Settings:
    resolved_database_url = database_url or os.getenv("DATABASE_URL")
    if not resolved_database_url:
        raise RuntimeError("DATABASE_URL is required and must point to PostgreSQL.")
    resolved_app_name = app_name or os.getenv("APP_NAME") or "Live listening diary"
    resolved_timezone = display_timezone or os.getenv("DISPLAY_TIMEZONE") or "UTC"

    return Settings(
        app_name=resolved_app_name,
        database_url=resolved_database_url,
        display_timezone=resolved_timezone,
    )
