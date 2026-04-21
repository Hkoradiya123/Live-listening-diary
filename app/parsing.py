from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


EVENT_ALIASES = {
    "play": "nowplaying",
    "playing": "nowplaying",
    "resume": "resumedplaying",
    "resumed": "resumedplaying",
    "love": "loved",
    "liked": "loved",
}


@dataclass(frozen=True)
class ParsedWebhookEvent:
    event_type: str
    artist: str
    track: str
    album: str | None
    artwork_url: str | None
    artist_url: str | None
    track_url: str | None
    album_url: str | None
    event_timestamp: datetime | None
    loved: bool
    is_now_playing: bool
    is_paused: bool


def _first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "loved", "liked"}:
            return True
        if normalized in {"0", "false", "no", "off", "unloved"}:
            return False
    return None


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1_000_000_000_000:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if text.isdigit():
                return _coerce_datetime(int(text))
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _nested_text(value: Any, *keys: str) -> str | None:
    if not isinstance(value, dict):
        return _as_text(value)
    return _first_non_empty(*(_as_text(value.get(key)) for key in keys))


def _extract_artwork(value: Any) -> str | None:
    if isinstance(value, str):
        return _as_text(value)
    if isinstance(value, dict):
        return _first_non_empty(
            _as_text(value.get("url")),
            _as_text(value.get("src")),
            _as_text(value.get("href")),
        )
    if isinstance(value, (list, tuple)):
        for item in value:
            extracted = _extract_artwork(item)
            if extracted:
                return extracted
    return None


def _pick_song_object(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("song", "track", "data", "payload"):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            return candidate
    return payload


def normalize_event_type(raw_value: Any) -> str:
    event_type = (_as_text(raw_value) or "scrobble").lower()
    return EVENT_ALIASES.get(event_type, event_type)


def parse_webhook_payload(payload: dict[str, Any]) -> ParsedWebhookEvent:
    if not isinstance(payload, dict):
        raise ValueError("Webhook payload must be a JSON object.")

    song = _pick_song_object(payload)
    event_type = normalize_event_type(_first_non_empty(payload.get("event"), payload.get("type"), payload.get("action")))

    artist = _first_non_empty(
        _as_text(song.get("artist")),
        _as_text(payload.get("artist")),
        _nested_text(song.get("artist"), "name", "title", "text"),
        _nested_text(payload.get("artist"), "name", "title", "text"),
    )
    track = _first_non_empty(
        _as_text(song.get("track")),
        _as_text(song.get("title")),
        _as_text(payload.get("track")),
        _as_text(payload.get("title")),
        _as_text(payload.get("name")),
    )
    album = _first_non_empty(
        _as_text(song.get("album")),
        _as_text(song.get("album_name")),
        _as_text(payload.get("album")),
        _as_text(payload.get("album_name")),
    )
    if not artist or not track:
        raise ValueError("Webhook payload is missing artist or track information.")

    artwork_url = _first_non_empty(
        _extract_artwork(song.get("artwork")),
        _extract_artwork(song.get("image")),
        _extract_artwork(song.get("images")),
        _extract_artwork(song.get("cover")),
        _extract_artwork(payload.get("artwork")),
        _extract_artwork(payload.get("image")),
    )
    artist_url = _first_non_empty(
        _as_text(song.get("artist_url")),
        _as_text(song.get("artistUrl")),
        _as_text(song.get("artist_link")),
        _nested_text(song.get("artist"), "url", "href"),
        _as_text(payload.get("artist_url")),
    )
    track_url = _first_non_empty(
        _as_text(song.get("track_url")),
        _as_text(song.get("trackUrl")),
        _as_text(song.get("url")),
        _as_text(payload.get("track_url")),
        _as_text(payload.get("url")),
    )
    album_url = _first_non_empty(
        _as_text(song.get("album_url")),
        _as_text(song.get("albumUrl")),
        _as_text(song.get("album_link")),
        _as_text(payload.get("album_url")),
    )
    event_timestamp = _first_non_empty(
        _coerce_datetime(song.get("timestamp")),
        _coerce_datetime(song.get("played_at")),
        _coerce_datetime(song.get("date")),
        _coerce_datetime(payload.get("timestamp")),
        _coerce_datetime(payload.get("played_at")),
        _coerce_datetime(payload.get("date")),
    )
    loved = _coerce_bool(_first_non_empty(song.get("loved"), song.get("liked"), payload.get("loved"), payload.get("liked"))) or False

    is_now_playing = event_type in {"nowplaying", "resumedplaying"}
    is_paused = event_type == "paused"

    return ParsedWebhookEvent(
        event_type=event_type,
        artist=artist,
        track=track,
        album=album,
        artwork_url=artwork_url,
        artist_url=artist_url,
        track_url=track_url,
        album_url=album_url,
        event_timestamp=event_timestamp,
        loved=loved or event_type == "loved",
        is_now_playing=is_now_playing,
        is_paused=is_paused,
    )
