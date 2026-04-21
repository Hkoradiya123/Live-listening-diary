from __future__ import annotations

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow


class ListeningEvent(Base):
    __tablename__ = "listening_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    artist: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    track: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    album: Mapped[str | None] = mapped_column(String(255), nullable=True)
    artwork_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    artist_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    track_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    album_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_timestamp: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow, index=True, nullable=False)
    loved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_now_playing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class PlaybackState(Base):
    __tablename__ = "playback_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    current_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="idle", nullable=False)
    artist: Mapped[str | None] = mapped_column(String(255), nullable=True)
    track: Mapped[str | None] = mapped_column(String(255), nullable=True)
    album: Mapped[str | None] = mapped_column(String(255), nullable=True)
    artwork_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    artist_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    track_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    album_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_timestamp: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    loved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

