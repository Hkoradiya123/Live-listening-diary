from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import ListeningEvent, PlaybackState
from .parsing import ParsedWebhookEvent
from .utils import serialize_event, serialize_state


def _normalized_text(value: str | None) -> str:
    return (value or "").strip().lower()


def build_event_hash(parsed: ParsedWebhookEvent) -> str:
    payload = "|".join(
        [
            parsed.event_type,
            _normalized_text(parsed.artist),
            _normalized_text(parsed.track),
            _normalized_text(parsed.album),
            parsed.event_timestamp.isoformat() if parsed.event_timestamp else "",
            "1" if parsed.loved else "0",
            "1" if parsed.is_now_playing else "0",
            "1" if parsed.is_paused else "0",
            _normalized_text(parsed.artwork_url),
            _normalized_text(parsed.track_url),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_or_create_state(session: Session, create: bool = True) -> PlaybackState | None:
    state = session.scalar(select(PlaybackState).order_by(PlaybackState.id.asc()).limit(1))
    if state is None:
        if not create:
            return None
        state = PlaybackState()
        session.add(state)
        session.flush()
    return state


def _apply_current_state(
    state: PlaybackState,
    event: ListeningEvent,
    parsed: ParsedWebhookEvent,
    received_at: datetime,
    replace_display: bool,
) -> None:
    if replace_display:
        state.current_event_id = event.id
        state.status = parsed.event_type
        state.artist = event.artist
        state.track = event.track
        state.album = event.album
        state.artwork_url = event.artwork_url
        state.artist_url = event.artist_url
        state.track_url = event.track_url
        state.album_url = event.album_url
        state.event_timestamp = event.event_timestamp
        state.received_at = received_at
        state.is_active = parsed.is_now_playing
        state.is_paused = parsed.is_paused
        state.loved = parsed.loved or event.loved
    else:
        state.loved = state.loved or parsed.loved or event.loved
    state.last_event_id = event.id


def store_event(
    session: Session,
    parsed: ParsedWebhookEvent,
    received_at: datetime,
    max_events: int,
) -> tuple[ListeningEvent, bool, PlaybackState | None]:
    event_hash = build_event_hash(parsed)
    existing = session.scalar(select(ListeningEvent).where(ListeningEvent.event_hash == event_hash))
    state = get_or_create_state(session)
    assert state is not None

    if existing is not None:
        state.last_event_id = existing.id
        session.commit()
        session.refresh(state)
        return existing, True, state

    event = ListeningEvent(
        event_hash=event_hash,
        event_type=parsed.event_type,
        artist=parsed.artist,
        track=parsed.track,
        album=parsed.album,
        artwork_url=parsed.artwork_url,
        artist_url=parsed.artist_url,
        track_url=parsed.track_url,
        album_url=parsed.album_url,
        event_timestamp=parsed.event_timestamp,
        received_at=received_at,
        loved=parsed.loved,
        is_now_playing=parsed.is_now_playing,
        is_paused=parsed.is_paused,
    )
    session.add(event)
    session.flush()

    replace_display = False
    current_display_matches = bool(
        state.current_event_id
        and state.artist
        and state.track
        and _normalized_text(state.artist) == _normalized_text(parsed.artist)
        and _normalized_text(state.track) == _normalized_text(parsed.track)
    )

    if parsed.is_now_playing or parsed.is_paused:
        replace_display = True
    elif parsed.event_type == "loved":
        replace_display = not state.current_event_id
    elif parsed.event_type == "scrobble":
        replace_display = not state.current_event_id and not state.is_active and not state.is_paused

    _apply_current_state(state, event, parsed, received_at, replace_display)

    if parsed.event_type == "loved" and current_display_matches:
        state.loved = True

    keep_ids = select(ListeningEvent.id).order_by(ListeningEvent.received_at.desc(), ListeningEvent.id.desc()).limit(max_events)
    session.execute(delete(ListeningEvent).where(~ListeningEvent.id.in_(keep_ids)))
    session.commit()
    session.refresh(event)
    session.refresh(state)
    return event, False, state


def recent_events(session: Session, limit: int = 8, timezone_name: str = "UTC") -> list[dict]:
    rows = session.scalars(
        select(ListeningEvent).order_by(ListeningEvent.received_at.desc(), ListeningEvent.id.desc()).limit(limit)
    ).all()
    return [serialize_event(row, timezone_name=timezone_name) for row in rows]


def history_events(
    session: Session,
    limit: int = 100,
    event_type: str | None = None,
    query: str | None = None,
    artist: str | None = None,
    timezone_name: str = "UTC",
) -> list[dict]:
    statement = select(ListeningEvent).order_by(ListeningEvent.received_at.desc(), ListeningEvent.id.desc())
    if event_type and event_type != "all":
        statement = statement.where(ListeningEvent.event_type == event_type)
    if artist:
        statement = statement.where(ListeningEvent.artist.ilike(f"%{artist}%"))
    if query:
        pattern = f"%{query}%"
        statement = statement.where(
            (ListeningEvent.artist.ilike(pattern))
            | (ListeningEvent.track.ilike(pattern))
            | (ListeningEvent.album.ilike(pattern))
        )
    rows = session.scalars(statement.limit(limit)).all()
    return [serialize_event(row, timezone_name=timezone_name) for row in rows]


def current_card(session: Session, timezone_name: str = "UTC") -> dict | None:
    state = get_or_create_state(session, create=False)
    if state is not None and state.current_event_id and state.artist and state.track:
        return serialize_state(state, timezone_name=timezone_name)
    latest = session.scalar(select(ListeningEvent).order_by(ListeningEvent.received_at.desc(), ListeningEvent.id.desc()).limit(1))
    return serialize_event(latest, timezone_name=timezone_name) if latest else None


def latest_state(session: Session) -> dict | None:
    state = get_or_create_state(session, create=False)
    if state is not None and state.current_event_id and state.artist and state.track:
        return {
            "current_event_id": state.current_event_id,
            "last_event_id": state.last_event_id,
            "status": state.status,
            "artist": state.artist,
            "track": state.track,
            "album": state.album,
            "loved": state.loved,
            "is_active": state.is_active,
            "is_paused": state.is_paused,
            "received_at": state.received_at,
        }
    return None


def stats_summary(session: Session, timezone_name: str = "UTC") -> dict:
    total_scrobbles = session.scalar(
        select(func.count()).select_from(ListeningEvent).where(ListeningEvent.event_type == "scrobble")
    ) or 0
    last_event = session.scalar(select(ListeningEvent).order_by(ListeningEvent.received_at.desc(), ListeningEvent.id.desc()).limit(1))
    top_artist_row = session.execute(
        select(ListeningEvent.artist, func.count().label("event_count"))
        .where(ListeningEvent.event_type == "scrobble")
        .group_by(ListeningEvent.artist)
        .order_by(func.count().desc(), ListeningEvent.artist.asc())
        .limit(1)
    ).first()
    top_artist = top_artist_row[0] if top_artist_row else None
    if top_artist is None and last_event is not None:
        top_artist = last_event.artist
    return {
        "total_scrobbles": int(total_scrobbles),
        "last_updated": serialize_event(last_event, timezone_name=timezone_name)["received_at_exact"] if last_event else "No activity yet",
        "top_artist": top_artist or "No artist yet",
    }
