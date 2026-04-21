from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from .config import load_settings
from .db import Base, build_engine, build_session_factory, utcnow
from .models import ListeningEvent
from .parsing import parse_webhook_payload
from .repository import (
    current_card,
    history_events,
    recent_events,
    stats_summary,
    store_event,
)
from .utils import PLACEHOLDER_ART_URL, serialize_event, serialize_state


def _resolve_token(request: Request, path_token: str | None = None) -> str | None:
    if path_token:
        return path_token.strip()
    headers = request.headers
    token = headers.get("x-recently-played-token") or headers.get("x-webhook-token")
    if not token:
        authorization = headers.get("authorization")
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1].strip()
    if not token:
        token = request.query_params.get("token")
    return token.strip() if token else None


def create_app(
    database_url: str | None = None,
    webhook_secret: str | None = None,
    app_name: str | None = None,
    display_timezone: str | None = None,
) -> FastAPI:
    settings = load_settings(
        database_url=database_url,
        webhook_secret=webhook_secret,
        app_name=app_name,
        display_timezone=display_timezone,
    )
    engine = None
    SessionLocal = None
    db_error = None

    if settings.database_url:
        try:
            engine = build_engine(settings.database_url)
            SessionLocal = build_session_factory(engine)
            Base.metadata.create_all(engine)
        except Exception as exc:
            db_error = str(exc)

    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
    app = FastAPI(title=settings.app_name)
    app.state.db_error = db_error

    def get_session():
        if SessionLocal is None:
            yield None
            return
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    def build_page_context(request: Request, session, title: str, description: str, active_page: str, **extra):
        if session is None:
            current = None
            recent = []
            stats = {"total_scrobbles": 0, "last_updated": "No activity yet", "top_artist": "No artist yet"}
        else:
            current = current_card(session, timezone_name=settings.display_timezone)
            recent = recent_events(session, limit=settings.home_recent_limit, timezone_name=settings.display_timezone)
            stats = stats_summary(session, timezone_name=settings.display_timezone)
        return {
            "request": request,
            "title": title,
            "description": description,
            "active_page": active_page,
            "app_name": settings.app_name,
            "placeholder_art_url": PLACEHOLDER_ART_URL,
            "current_card": current,
            "recent_tracks": recent,
            "stats": stats,
            "display_timezone": settings.display_timezone,
            "db_error": app.state.db_error,
            **extra,
        }

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, session=Depends(get_session)):
        context = build_page_context(
            request,
            session,
            title=settings.app_name,
            description="Live listening history powered by Web Scrobbler.",
            active_page="home",
        )
        return templates.TemplateResponse(request, "home.html", context)

    @app.get("/history", response_class=HTMLResponse)
    def history(
        request: Request,
        event: str = Query(default="all"),
        q: str | None = Query(default=None),
        artist: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        session=Depends(get_session),
    ):
        items = history_events(
            session,
            limit=limit,
            event_type=event,
            query=q,
            artist=artist,
            timezone_name=settings.display_timezone,
        )
        context = build_page_context(
            request,
            session,
            title=f"History - {settings.app_name}",
            description="Full track timeline with filters.",
            active_page="history",
            history_items=items,
            filters={"event": event, "q": q or "", "artist": artist or "", "limit": limit},
            total_items=len(items),
        )
        return templates.TemplateResponse(request, "history.html", context)

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request, session=Depends(get_session)):
        context = build_page_context(
            request,
            session,
            title=f"Status - {settings.app_name}",
            description="Webhook endpoint and API reference.",
            active_page="status",
        )
        return templates.TemplateResponse(request, "status.html", context)

    @app.get("/api/recent")
    def api_recent(limit: int = Query(default=settings.api_recent_limit, ge=1, le=20), session=Depends(get_session)):
        if session is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is not configured.")
        total_events = session.scalar(select(func.count()).select_from(ListeningEvent)) or 0
        return JSONResponse(
            {
                "ok": True,
                "items": recent_events(session, limit=limit, timezone_name=settings.display_timezone),
                "count": int(total_events),
                "limit": limit,
            }
        )

    @app.get("/api/now-playing")
    def api_now_playing(session=Depends(get_session)):
        if session is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is not configured.")
        card = current_card(session, timezone_name=settings.display_timezone)
        return JSONResponse({"ok": True, "item": card})

    @app.get("/api/status")
    def api_status(session=Depends(get_session)):
        if session is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is not configured.")
        payload = {
            "ok": True,
            "app_name": settings.app_name,
            "webhook_endpoint": "/api/webhook",
            "supported_events": ["nowplaying", "paused", "resumedplaying", "scrobble", "loved"],
            "recent_count": int(session.scalar(select(func.count()).select_from(ListeningEvent)) or 0),
            "now_playing": current_card(session, timezone_name=settings.display_timezone),
            "stats": stats_summary(session, timezone_name=settings.display_timezone),
        }
        return JSONResponse(payload)

    @app.post("/api/webhook")
    @app.post("/api/webhook/{path_token}")
    async def webhook(request: Request, path_token: str | None = None, session=Depends(get_session)):
        if SessionLocal is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is not configured.")
        token = _resolve_token(request, path_token=path_token)
        if not secrets.compare_digest(token or "", settings.webhook_secret):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid webhook token.")

        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request body must be valid JSON.") from exc

        try:
            parsed = parse_webhook_payload(payload)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        received_at = utcnow()
        event, deduped, state = store_event(session, parsed, received_at, max_events=settings.max_events)
        return JSONResponse(
            {
                "ok": True,
                "deduped": deduped,
                "stored_event": serialize_event(event, timezone_name=settings.display_timezone),
                "current": serialize_state(state, timezone_name=settings.display_timezone) if state else None,
            }
        )

    return app


app = create_app()
