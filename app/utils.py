from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .models import ListeningEvent, PlaybackState


PLACEHOLDER_ART_URL = (
    "data:image/svg+xml;charset=UTF-8,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' width='640' height='640' viewBox='0 0 640 640'%3E"
    "%3Cdefs%3E"
    "%3ClinearGradient id='g' x1='0%25' x2='100%25' y1='0%25' y2='100%25'%3E"
    "%3Cstop offset='0%25' stop-color='%23151c35'/%3E"
    "%3Cstop offset='100%25' stop-color='%23343d6f'/%3E"
    "%3C/linearGradient%3E"
    "%3C/defs%3E"
    "%3Crect width='640' height='640' rx='72' fill='url(%23g)'/%3E"
    "%3Ccircle cx='320' cy='320' r='180' fill='rgba(255,255,255,0.08)'/%3E"
    "%3Cpath d='M280 190h120v28H280zM280 238h120v28H280zM280 286h120v28H280z' fill='%23b7c5ff' opacity='0.75'/%3E"
    "%3Ccircle cx='242' cy='430' r='34' fill='%23e9efff'/%3E"
    "%3Cpath d='M276 430V226h32v156c0 29-23 52-52 52s-52-23-52-52 23-52 52-52c8 0 16 2 22 5z' fill='%23e9efff'/%3E"
    "%3Cpath d='M372 430V244h32v136c0 29-23 52-52 52s-52-23-52-52 23-52 52-52c8 0 16 2 22 5z' fill='%23e9efff'/%3E"
    "%3C/svg%3E"
)


def event_badge_label(event_type: str | None) -> str:
    labels = {
        "nowplaying": "Now Playing",
        "resumedplaying": "Now Playing",
        "paused": "Paused",
        "played": "Played",
        "scrobble": "Scrobbled",
        "loved": "Loved",
    }
    return labels.get((event_type or "").lower(), (event_type or "Event").title())


def event_badge_class(event_type: str | None, loved: bool = False, is_active: bool = False) -> str:
    event_type = (event_type or "").lower()
    if loved:
        return "badge badge--loved"
    if event_type in {"nowplaying", "resumedplaying"} or is_active:
        return "badge badge--live"
    if event_type == "paused":
        return "badge badge--paused"
    if event_type == "played":
        return "badge badge--played"
    if event_type == "scrobble":
        return "badge badge--scrobble"
    return "badge"


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_relative_time(value: datetime | None, now: datetime | None = None) -> str:
    value = _normalize_datetime(value)
    if value is None:
        return "Unknown time"
    now = _normalize_datetime(now or datetime.now(timezone.utc)) or datetime.now(timezone.utc)
    delta = max(0, int((now - value).total_seconds()))
    if delta < 10:
        return "just now"
    if delta < 60:
        return f"{delta}s ago"
    minutes = delta // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks}w ago"
    return format_absolute_time(value)


def format_absolute_time(value: datetime | None, timezone_name: str = "UTC") -> str:
    value = _normalize_datetime(value)
    if value is None:
        return "Unknown"
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = timezone.utc
    local = value.astimezone(tz)
    time_part = local.strftime("%I:%M %p").lstrip("0")
    return f"{local.strftime('%b')} {local.day}, {local.year} at {time_part}"


def serialize_event(event: ListeningEvent, timezone_name: str = "UTC") -> dict:
    received_at = _normalize_datetime(event.received_at)
    return {
        "id": event.id,
        "event_type": event.event_type,
        "badge_label": event_badge_label(event.event_type),
        "badge_class": event_badge_class(event.event_type, event.loved, event.is_now_playing),
        "artist": event.artist,
        "track": event.track,
        "album": event.album,
        "artwork_url": event.artwork_url or PLACEHOLDER_ART_URL,
        "artist_url": event.artist_url,
        "track_url": event.track_url,
        "album_url": event.album_url,
        "event_timestamp": event.event_timestamp.isoformat() if event.event_timestamp else None,
        "received_at": received_at.isoformat() if received_at else None,
        "received_at_human": format_relative_time(received_at),
        "received_at_exact": format_absolute_time(received_at, timezone_name=timezone_name),
        "loved": event.loved,
        "is_now_playing": event.is_now_playing,
        "is_paused": event.is_paused,
    }


def serialize_state(state: PlaybackState | None, timezone_name: str = "UTC") -> dict | None:
    if state is None or not state.artist or not state.track:
        return None
    received_at = _normalize_datetime(state.received_at)
    return {
        "id": state.id,
        "event_type": state.status,
        "badge_label": event_badge_label(state.status),
        "badge_class": event_badge_class(state.status, state.loved, state.is_active),
        "artist": state.artist,
        "track": state.track,
        "album": state.album,
        "artwork_url": state.artwork_url or PLACEHOLDER_ART_URL,
        "artist_url": state.artist_url,
        "track_url": state.track_url,
        "album_url": state.album_url,
        "event_timestamp": state.event_timestamp.isoformat() if state.event_timestamp else None,
        "received_at": received_at.isoformat() if received_at else None,
        "received_at_human": format_relative_time(received_at),
        "received_at_exact": format_absolute_time(received_at, timezone_name=timezone_name),
        "loved": state.loved,
        "is_now_playing": state.is_active,
        "is_paused": state.is_paused,
        "status": state.status,
    }
