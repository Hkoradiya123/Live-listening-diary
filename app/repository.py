from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import ListeningEvent, PlaybackState, UserAccount
from .parsing import ParsedWebhookEvent
from .utils import serialize_event, serialize_state


def _normalized_text(value: str | None) -> str:
    return (value or "").strip().lower()


def _is_placeholder_event(artist: str | None, track: str | None) -> bool:
    return _normalized_text(artist) == "unknown artist" and _normalized_text(track) == "untitled track"


def _event_identity(event: ListeningEvent) -> tuple[str, str]:
    return _normalized_text(event.artist), _normalized_text(event.track)


def _collapse_key(event: ListeningEvent) -> tuple[str, str] | None:
    # Collapse consecutive rows for the same song regardless of event type.
    # This keeps nowplaying/paused/scrobble transitions for one song as a single row in recent lists.
    return _event_identity(event)


def _collapse_consecutive_events(events: list[ListeningEvent]) -> list[ListeningEvent]:
    collapsed: list[ListeningEvent] = []
    previous_key: tuple[str, str] | None = None
    for event in events:
        collapse_key = _collapse_key(event)
        if collapse_key is not None and collapse_key == previous_key:
            continue
        collapsed.append(event)
        previous_key = collapse_key
    return collapsed


def _retire_previous_current_event(session: Session, state: PlaybackState) -> None:
    if not state.current_event_id:
        return
    previous_event = session.get(ListeningEvent, state.current_event_id)
    if previous_event is None or previous_event.user_id != state.user_id:
        return
    previous_event.event_type = "played"
    previous_event.is_now_playing = False
    previous_event.is_paused = False


def build_event_hash(user_id: int, parsed: ParsedWebhookEvent, received_at: datetime) -> str:
    event_time_component = parsed.event_timestamp.isoformat() if parsed.event_timestamp else ""
    if parsed.event_type == "scrobble" and not event_time_component:
        # Some clients omit a track timestamp; use receipt time so repeated plays are not deduped forever.
        event_time_component = received_at.isoformat()

    payload = "|".join(
        [
            str(user_id),
            parsed.event_type,
            _normalized_text(parsed.artist),
            _normalized_text(parsed.track),
            _normalized_text(parsed.album),
            event_time_component,
            "1" if parsed.loved else "0",
            "1" if parsed.is_now_playing else "0",
            "1" if parsed.is_paused else "0",
            _normalized_text(parsed.artwork_url),
            _normalized_text(parsed.track_url),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_or_create_state(session: Session, user_id: int, create: bool = True) -> PlaybackState | None:
    state = session.scalar(
        select(PlaybackState)
        .where(PlaybackState.user_id == user_id)
        .order_by(PlaybackState.id.asc())
        .limit(1)
    )
    if state is None:
        if not create:
            return None
        state = PlaybackState(user_id=user_id)
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
    user: UserAccount,
    parsed: ParsedWebhookEvent,
    received_at: datetime,
    max_events: int,
) -> tuple[ListeningEvent, bool, PlaybackState | None]:
    # Skip storing paused events. Update state only if needed, don't save to DB.
    if parsed.event_type == "paused":
        state = get_or_create_state(session, user.id)
        assert state is not None
        # Still track paused state in playback but don't create event row.
        state.is_paused = True
        session.commit()
        session.refresh(state)
        # Return empty/placeholder event to avoid breaking webhook response.
        placeholder = ListeningEvent(
            event_hash="",
            user_id=user.id,
            event_type="paused",
            artist="",
            track="",
        )
        return placeholder, True, state

    event_hash = build_event_hash(user.id, parsed, received_at)
    existing = session.scalar(
        select(ListeningEvent).where(
            ListeningEvent.user_id == user.id,
            ListeningEvent.event_hash == event_hash,
        )
    )
    state = get_or_create_state(session, user.id)
    assert state is not None

    if existing is not None:
        state.last_event_id = existing.id
        session.commit()
        session.refresh(state)
        return existing, True, state

    if parsed.event_type == "scrobble":
        candidate_statement = (
            select(ListeningEvent)
            .where(
                ListeningEvent.user_id == user.id,
                ListeningEvent.event_type.in_(["played", "nowplaying", "resumedplaying"]),
                ListeningEvent.artist == parsed.artist,
                ListeningEvent.track == parsed.track,
            )
            .order_by(ListeningEvent.received_at.desc(), ListeningEvent.id.desc())
            .limit(1)
        )
        if parsed.album:
            candidate_statement = candidate_statement.where(ListeningEvent.album == parsed.album)
        candidate = session.scalar(candidate_statement)
        if candidate is not None:
            candidate.event_hash = event_hash
            candidate.event_type = "scrobble"
            candidate.album = parsed.album
            candidate.artwork_url = parsed.artwork_url
            candidate.artist_url = parsed.artist_url
            candidate.track_url = parsed.track_url
            candidate.album_url = parsed.album_url
            candidate.event_timestamp = parsed.event_timestamp
            candidate.received_at = received_at
            candidate.loved = parsed.loved or candidate.loved
            candidate.is_now_playing = False
            candidate.is_paused = False

            state.last_event_id = candidate.id
            if state.current_event_id == candidate.id:
                state.status = "scrobble"
                state.received_at = received_at
                state.event_timestamp = parsed.event_timestamp
                state.is_active = False
                state.is_paused = False
                state.loved = parsed.loved or state.loved

            session.commit()
            session.refresh(candidate)
            session.refresh(state)
            return candidate, True, state

    event = ListeningEvent(
        event_hash=event_hash,
        user_id=user.id,
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

    if parsed.is_now_playing:
        _retire_previous_current_event(session, state)
        replace_display = True
    elif parsed.event_type == "loved":
        replace_display = not state.current_event_id
    elif parsed.event_type == "scrobble":
        replace_display = not state.current_event_id and not state.is_active and not state.is_paused

    if replace_display and _is_placeholder_event(parsed.artist, parsed.track):
        replace_display = False

    _apply_current_state(state, event, parsed, received_at, replace_display)

    if parsed.event_type == "loved" and current_display_matches:
        state.loved = True

    keep_ids = (
        select(ListeningEvent.id)
        .where(ListeningEvent.user_id == user.id)
        .order_by(ListeningEvent.received_at.desc(), ListeningEvent.id.desc())
        .limit(max_events)
    )
    session.execute(
        delete(ListeningEvent).where(
            ListeningEvent.user_id == user.id,
            ~ListeningEvent.id.in_(keep_ids),
        )
    )
    session.commit()
    session.refresh(event)
    session.refresh(state)
    return event, False, state


def recent_events(
    session: Session,
    user_id: int,
    limit: int = 8,
    timezone_name: str = "UTC",
    event_type: str | None = None,
) -> list[dict]:
    statement = (
        select(ListeningEvent)
        .where(
            ListeningEvent.user_id == user_id,
            ListeningEvent.event_type != "paused",
            ~(
                (ListeningEvent.artist == "Unknown artist")
                & (ListeningEvent.track == "Untitled track")
            )
        )
        .order_by(ListeningEvent.received_at.desc(), ListeningEvent.id.desc())
    )
    if event_type and event_type != "all":
        statement = statement.where(ListeningEvent.event_type == event_type)
    fetch_limit = max(limit * 4, limit + 10)
    rows = session.scalars(statement.limit(fetch_limit)).all()
    rows = _collapse_consecutive_events(rows)
    rows = rows[:limit]
    return [serialize_event(row, timezone_name=timezone_name) for row in rows]


def history_events(
    session: Session,
    user_id: int,
    limit: int = 100,
    event_type: str | None = None,
    query: str | None = None,
    artist: str | None = None,
    timezone_name: str = "UTC",
) -> list[dict]:
    statement = (
        select(ListeningEvent)
        .where(
            ListeningEvent.user_id == user_id,
            ListeningEvent.event_type != "paused",
        )
        .order_by(ListeningEvent.received_at.desc(), ListeningEvent.id.desc())
    )
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
    fetch_limit = max(limit * 4, limit + 10)
    rows = session.scalars(statement.limit(fetch_limit)).all()
    rows = _collapse_consecutive_events(rows)
    rows = rows[:limit]
    return [serialize_event(row, timezone_name=timezone_name) for row in rows]


def current_card(session: Session, user_id: int, timezone_name: str = "UTC") -> dict | None:
    state = get_or_create_state(session, user_id, create=False)
    if (
        state is not None
        and state.is_active
        and not state.is_paused
        and state.current_event_id
        and state.artist
        and state.track
        and not _is_placeholder_event(state.artist, state.track)
    ):
        return serialize_state(state, timezone_name=timezone_name)
    latest = session.scalar(
        select(ListeningEvent)
        .where(
            ListeningEvent.user_id == user_id,
            ListeningEvent.is_paused.is_(False),
            ListeningEvent.event_type != "played",
            ~(
                (ListeningEvent.artist == "Unknown artist")
                & (ListeningEvent.track == "Untitled track")
            )
        )
        .order_by(ListeningEvent.received_at.desc(), ListeningEvent.id.desc())
        .limit(1)
    )
    return serialize_event(latest, timezone_name=timezone_name) if latest else None


def latest_state(session: Session, user_id: int) -> dict | None:
    state = get_or_create_state(session, user_id, create=False)
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


def stats_summary(session: Session, user_id: int, timezone_name: str = "UTC") -> dict:
    total_scrobbles = session.scalar(
        select(func.count())
        .select_from(ListeningEvent)
        .where(ListeningEvent.user_id == user_id, ListeningEvent.event_type == "scrobble")
    ) or 0
    last_event = session.scalar(
        select(ListeningEvent)
        .where(ListeningEvent.user_id == user_id)
        .order_by(ListeningEvent.received_at.desc(), ListeningEvent.id.desc())
        .limit(1)
    )
    top_artist_row = session.execute(
        select(ListeningEvent.artist, func.count().label("event_count"))
        .where(ListeningEvent.user_id == user_id, ListeningEvent.event_type == "scrobble")
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
