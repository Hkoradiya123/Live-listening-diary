from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse


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
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                nested = _first_non_empty(*(_as_text(item.get(key)) for key in keys))
                if nested:
                    return nested
        return None
    if not isinstance(value, dict):
        return _as_text(value)
    return _first_non_empty(*(_as_text(value.get(key)) for key in keys))


def _normalize_key_name(key: str) -> str:
    return "".join(ch for ch in key.lower() if ch.isalnum())


def _deep_find_text(value: Any, normalized_key_candidates: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _normalize_key_name(str(key)) in normalized_key_candidates:
                direct = _as_text(item)
                if direct:
                    return direct
                nested = _nested_text(item, "name", "title", "text")
                if nested:
                    return nested
            discovered = _deep_find_text(item, normalized_key_candidates)
            if discovered:
                return discovered
    elif isinstance(value, (list, tuple)):
        for item in value:
            discovered = _deep_find_text(item, normalized_key_candidates)
            if discovered:
                return discovered
    return None


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


def _is_valid_youtube_id(value: str | None) -> bool:
    if not value:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", value))


def _extract_youtube_video_id(value: Any) -> str | None:
    text = _as_text(value)
    if not text:
        return None

    if _is_valid_youtube_id(text):
        return text

    try:
        parsed = urlparse(text)
    except Exception:
        return None

    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").strip("/")

    if "youtu.be" in host:
        candidate = path.split("/", 1)[0]
        return candidate if _is_valid_youtube_id(candidate) else None

    if "youtube.com" in host or "music.youtube.com" in host:
        if path == "watch":
            candidate = parse_qs(parsed.query).get("v", [None])[0]
            return candidate if _is_valid_youtube_id(candidate) else None
        if path.startswith("shorts/"):
            candidate = path.split("/", 1)[1].split("/", 1)[0]
            return candidate if _is_valid_youtube_id(candidate) else None
        if path.startswith("embed/"):
            candidate = path.split("/", 1)[1].split("/", 1)[0]
            return candidate if _is_valid_youtube_id(candidate) else None

    return None


def _youtube_thumbnail_url(video_id: str | None) -> str | None:
    if not _is_valid_youtube_id(video_id):
        return None
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def _maybe_json_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text[:1] not in {"{", "["}:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _pick_song_object(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("song", "track", "data", "payload", "current", "currentTrack", "metadata", "media"):
        candidate = _maybe_json_dict(payload.get(key))
        if candidate:
            return candidate
    return payload


def normalize_event_type(raw_value: Any) -> str:
    event_type = (_as_text(raw_value) or "scrobble").lower()
    return EVENT_ALIASES.get(event_type, event_type)


def parse_webhook_payload(payload: dict[str, Any]) -> ParsedWebhookEvent:
    if not isinstance(payload, dict):
        raise ValueError("Webhook payload must be a JSON object.")

    song = _pick_song_object(payload)
    event_type = normalize_event_type(
        _first_non_empty(
            payload.get("event"),
            payload.get("eventName"),
            payload.get("type"),
            payload.get("action"),
            _nested_text(payload.get("data"), "event", "eventName", "type", "action"),
        )
    )

    artist = _first_non_empty(
        _as_text(song.get("artist")),
        _as_text(song.get("artistName")),
        _as_text(song.get("artist_name")),
        _as_text(payload.get("artist")),
        _as_text(payload.get("artistName")),
        _as_text(payload.get("artist_name")),
        _nested_text(song.get("artist"), "name", "title", "text"),
        _nested_text(payload.get("artist"), "name", "title", "text"),
        _nested_text(song.get("artists"), "name", "title", "text"),
        _nested_text(payload.get("artists"), "name", "title", "text"),
        _deep_find_text(song, {"artist", "artistname", "artisttitle", "performer", "creator", "author", "band", "artists"}),
        _deep_find_text(payload, {"artist", "artistname", "artisttitle", "performer", "creator", "author", "band", "artists"}),
    )
    track = _first_non_empty(
        _as_text(song.get("track")),
        _as_text(song.get("title")),
        _as_text(song.get("name")),
        _as_text(song.get("song")),
        _as_text(song.get("trackName")),
        _as_text(song.get("track_name")),
        _as_text(payload.get("track")),
        _as_text(payload.get("title")),
        _as_text(payload.get("name")),
        _as_text(payload.get("song")),
        _as_text(payload.get("trackName")),
        _as_text(payload.get("track_name")),
        _deep_find_text(song, {"track", "trackname", "title", "song", "songname", "video", "videotitle"}),
        _deep_find_text(payload, {"track", "trackname", "title", "song", "songname", "video", "videotitle"}),
    )
    album = _first_non_empty(
        _as_text(song.get("album")),
        _as_text(song.get("album_name")),
        _as_text(payload.get("album")),
        _as_text(payload.get("album_name")),
        _deep_find_text(song, {"album", "albumname", "record", "release"}),
        _deep_find_text(payload, {"album", "albumname", "record", "release"}),
    )
    if (not artist or artist == "Unknown artist") and track and " - " in track:
        possible_artist, possible_track = [segment.strip() for segment in track.split(" - ", 1)]
        if possible_artist and possible_track:
            artist = possible_artist
            track = possible_track
    if not artist:
        artist = "Unknown artist"
    if not track:
        track = "Untitled track"

    artwork_url = _first_non_empty(
        _extract_artwork(song.get("artwork")),
        _extract_artwork(song.get("image")),
        _extract_artwork(song.get("images")),
        _extract_artwork(song.get("cover")),
        _as_text(song.get("trackArt")),
        _as_text(song.get("trackArtUrl")),
        _extract_artwork(payload.get("artwork")),
        _extract_artwork(payload.get("image")),
        _as_text(payload.get("trackArt")),
        _as_text(payload.get("trackArtUrl")),
        _deep_find_text(song, {"trackart", "trackarturl", "artwork", "image", "thumbnail", "thumb", "cover", "coverurl"}),
        _deep_find_text(payload, {"trackart", "trackarturl", "artwork", "image", "thumbnail", "thumb", "cover", "coverurl"}),
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
        _as_text(song.get("originUrl")),
        _as_text(payload.get("track_url")),
        _as_text(payload.get("url")),
        _as_text(payload.get("originUrl")),
        _deep_find_text(song, {"trackurl", "originurl", "url", "video", "videourl"}),
        _deep_find_text(payload, {"trackurl", "originurl", "url", "video", "videourl"}),
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

    if not artwork_url:
        youtube_video_id = _first_non_empty(
            _extract_youtube_video_id(track_url),
            _extract_youtube_video_id(song.get("uniqueID")),
            _extract_youtube_video_id(song.get("originUrl")),
            _extract_youtube_video_id(payload.get("originUrl")),
            _extract_youtube_video_id(_deep_find_text(song, {"uniqueid", "originurl", "videoid", "identifier", "url"})),
            _extract_youtube_video_id(_deep_find_text(payload, {"uniqueid", "originurl", "videoid", "identifier", "url"})),
        )
        artwork_url = _youtube_thumbnail_url(youtube_video_id)

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
