from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import parse_qs

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from .auth import (
    SESSION_COOKIE_NAME,
    generate_token,
    hash_password,
    hash_token,
    normalize_email,
    session_expires_at,
    verify_password,
)
from .config import load_settings
from .db import Base, build_engine, build_session_factory, utcnow
from .models import ListeningEvent, UserAccount, UserSession
from .parsing import parse_webhook_payload
from .repository import (
    current_card,
    history_events,
    recent_events,
    stats_summary,
    store_event,
)
from .utils import PLACEHOLDER_ART_URL, serialize_event, serialize_state
import requests


logger = logging.getLogger("uvicorn.error")
load_dotenv()


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


def _flatten_query_params(values: dict[str, list[str]]) -> dict[str, str | list[str]]:
    flattened: dict[str, str | list[str]] = {}
    for key, items in values.items():
        if not items:
            continue
        flattened[key] = items[0] if len(items) == 1 else items
    return flattened


def _assign_nested_key(target: dict, path: list[str], value):
    cursor = target
    for key in path[:-1]:
        existing = cursor.get(key)
        if not isinstance(existing, dict):
            existing = {}
            cursor[key] = existing
        cursor = existing
    cursor[path[-1]] = value


def _expand_form_keys(flat_values: dict[str, str | list[str]]) -> dict:
    expanded: dict = {}
    for raw_key, value in flat_values.items():
        key = (raw_key or "").strip()
        if not key:
            continue

        if "[" in key and key.endswith("]"):
            head, tail = key.split("[", 1)
            parts = [head] + [segment for segment in tail.rstrip("]").split("][") if segment]
        elif "." in key:
            parts = [segment for segment in key.split(".") if segment]
        else:
            parts = [key]

        if not parts:
            continue
        _assign_nested_key(expanded, parts, value)
    return expanded


async def _read_webhook_payload(request: Request):
    raw_body = await request.body()
    if not raw_body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request body cannot be empty.")

    body_text = raw_body.decode("utf-8", errors="replace").strip()
    content_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()

    if content_type in {"application/json", "text/json", ""} or body_text[:1] in {"{", "["}:
        try:
            return json.loads(body_text)
        except json.JSONDecodeError:
            pass

    form_values = _flatten_query_params(parse_qs(body_text, keep_blank_values=True))
    payload = form_values.get("payload")
    if isinstance(payload, str):
        payload_text = payload.strip()
        if payload_text[:1] in {"{", "["}:
            try:
                return json.loads(payload_text)
            except json.JSONDecodeError:
                pass
    if form_values:
        expanded_values = _expand_form_keys(form_values)
        if isinstance(expanded_values.get("payload"), dict):
            return expanded_values["payload"]
        if expanded_values:
            return expanded_values
        return form_values

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request body must be valid JSON or form data.")


async def _read_form_fields(request: Request) -> dict[str, str]:
    raw_body = await request.body()
    if not raw_body:
        return {}
    body_text = raw_body.decode("utf-8", errors="replace").strip()
    flattened = _flatten_query_params(parse_qs(body_text, keep_blank_values=True))
    fields: dict[str, str] = {}
    for key, value in flattened.items():
        if isinstance(value, list):
            fields[key] = value[0] if value else ""
        else:
            fields[key] = value
    return fields


def _payload_preview(payload, max_chars: int = 1500) -> str:
    try:
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        serialized = str(payload)
    if len(serialized) <= max_chars:
        return serialized
    return f"{serialized[:max_chars]}... <truncated {len(serialized) - max_chars} chars>"


def _has_meaningful_text(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    return normalized not in {"", "unknown artist", "untitled track"}


def _build_payload_match_report(payload: dict, parsed) -> dict:
    payload_keys = sorted(str(key) for key in payload.keys())
    return {
        "payload_keys": payload_keys,
        "expected_fields": {
            "event": parsed.event_type,
            "artist": parsed.artist,
            "track": parsed.track,
            "album": parsed.album,
        },
        "matched": {
            "event": bool(parsed.event_type),
            "artist": _has_meaningful_text(parsed.artist),
            "track": _has_meaningful_text(parsed.track),
            "album": bool(parsed.album),
        },
    }


def _display_name_from_email(email: str) -> str:
    local_part = (email or "").split("@", 1)[0].strip()
    if not local_part:
        return "Listener"
    parts = [segment for segment in local_part.replace(".", " ").replace("_", " ").split() if segment]
    return " ".join(piece.capitalize() for piece in parts) if parts else "Listener"


def _absolute_base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _make_auth_context(request: Request, app_name: str, **extra) -> dict:
    return {
        "request": request,
        "title": f"Sign in - {app_name}",
        "description": "Sign in to your private listening dashboard.",
        "active_page": "auth",
        "app_name": app_name,
        "placeholder_art_url": PLACEHOLDER_ART_URL,
        **extra,
    }


def create_app(
    database_url: str | None = None,
    app_name: str | None = None,
    display_timezone: str | None = None,
) -> FastAPI:
    settings = load_settings(
        database_url=database_url,
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

    def resolve_current_user(request: Request, session):
        if session is None:
            return None
        session_token = request.cookies.get(SESSION_COOKIE_NAME)
        if not session_token:
            return None
        token_hash = hash_token(session_token)
        now = utcnow()
        user_session = session.scalar(
            select(UserSession).where(
                UserSession.session_token_hash == token_hash,
                UserSession.expires_at > now,
            )
        )
        if user_session is None:
            return None
        user = session.get(UserAccount, user_session.user_id)
        if user is None:
            return None
        return user

    def create_user_session(session, user: UserAccount) -> str:
        token = generate_token()
        session.add(
            UserSession(
                user_id=user.id,
                session_token_hash=hash_token(token),
                expires_at=session_expires_at(),
            )
        )
        session.commit()
        return token

    def delete_user_session(session, request: Request) -> None:
        session_token = request.cookies.get(SESSION_COOKIE_NAME)
        if not session_token:
            return
        token_hash = hash_token(session_token)
        session.execute(delete(UserSession).where(UserSession.session_token_hash == token_hash))
        session.commit()

    def build_auth_context(request: Request, **extra):
        return _make_auth_context(
            request,
            app_name=settings.app_name,
            db_error=app.state.db_error,
            **extra,
        )

    def build_user_context(request: Request, session, user: UserAccount, title: str, description: str, active_page: str, **extra):
        if session is None:
            current = None
            recent = []
            stats = {"total_scrobbles": 0, "last_updated": "No activity yet", "top_artist": "No artist yet"}
        else:
            try:
                current = current_card(session, user.id, timezone_name=settings.display_timezone)
                recent = recent_events(
                    session,
                    user.id,
                    limit=settings.home_recent_limit,
                    timezone_name=settings.display_timezone,
                    event_type="scrobble",
                )
                stats = stats_summary(session, user.id, timezone_name=settings.display_timezone)
            except SQLAlchemyError as exc:
                app.state.db_error = str(exc)
                current = None
                recent = []
                stats = {"total_scrobbles": 0, "last_updated": "Database unavailable", "top_artist": "Database unavailable"}
        webhook_endpoint = f"{_absolute_base_url(request)}/api/webhook/{user.webhook_token}"
        public_api_endpoint = f"{_absolute_base_url(request)}/api/public/{user.webhook_token}"
        return {
            "request": request,
            "title": title,
            "description": description,
            "active_page": active_page,
            "app_name": settings.app_name,
            "placeholder_art_url": PLACEHOLDER_ART_URL,
            "current_user": user,
            "current_card": current,
            "recent_tracks": recent,
            "stats": stats,
            "display_timezone": settings.display_timezone,
            "db_error": app.state.db_error,
            "webhook_endpoint": webhook_endpoint,
            "public_api_endpoint": public_api_endpoint,
            "webhook_token": user.webhook_token,
            "webhook_header_example": user.webhook_token,
            "api_base_url": _absolute_base_url(request),
            **extra,
        }

    def _set_session_cookie(response: Response, token: str, secure: bool) -> None:
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            httponly=True,
            samesite="lax",
            secure=secure,
            path="/",
            max_age=int(60 * 60 * 24 * 30),
        )

    def _redirect_with_session(request: Request, url: str, token: str) -> RedirectResponse:
        response = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
        forwarded_proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",", 1)[0].strip().lower()
        _set_session_cookie(response, token, secure=forwarded_proto == "https")
        return response

    def _login_or_register_user(
        session,
        email: str,
        password: str,
        display_name: str | None = None,
    ) -> tuple[UserAccount | None, str | None, str | None]:
        normalized_email = normalize_email(email)
        if not normalized_email or not password:
            return None, None, "Email and password are required."
        if len(password) < 8:
            return None, None, "Password must be at least 8 characters."
        if display_name:
            display_name = display_name.strip()
        if not display_name:
            display_name = _display_name_from_email(normalized_email)
        existing = session.scalar(select(UserAccount).where(UserAccount.email == normalized_email))
        if existing is not None:
            return None, None, "That email is already registered."
        user = UserAccount(
            email=normalized_email,
            display_name=display_name,
            password_hash=hash_password(password),
            webhook_token=generate_token(24),
        )
        session.add(user)
        session.flush()
        token = create_user_session(session, user)
        session.commit()
        return user, token, None

    def _authenticate_user(session, email: str, password: str) -> tuple[UserAccount | None, str | None]:
        normalized_email = normalize_email(email)
        user = session.scalar(select(UserAccount).where(UserAccount.email == normalized_email))
        if user is None or not verify_password(password, user.password_hash):
            return None, "Invalid email or password."
        token = create_user_session(session, user)
        return user, token

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, session=Depends(get_session)):
        user = resolve_current_user(request, session)
        if user is not None:
            return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        context = build_auth_context(request, auth_error=None, email_value="", mode="login")
        return templates.TemplateResponse(request, "auth.html", context)

    @app.post("/login")
    async def login(request: Request, session=Depends(get_session)):
        if session is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is not configured.")
        form = await _read_form_fields(request)
        email = form.get("email", "")
        password = form.get("password", "")
        try:
            user, token_or_error = _authenticate_user(session, email, password)
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database error: {exc}") from exc
        if user is None:
            context = build_auth_context(request, auth_error=token_or_error, email_value=email, mode="login")
            return templates.TemplateResponse(request, "auth.html", context, status_code=status.HTTP_400_BAD_REQUEST)
        return _redirect_with_session(request, "/", token_or_error or "")

    @app.post("/register")
    async def register(request: Request, session=Depends(get_session)):
        if session is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is not configured.")
        form = await _read_form_fields(request)
        email = form.get("email", "")
        password = form.get("password", "")
        display_name = form.get("display_name", "")
        try:
            user, token, error = _login_or_register_user(session, email, password, display_name=display_name)
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database error: {exc}") from exc
        if user is None or token is None:
            context = build_auth_context(request, auth_error=error, email_value=email, mode="register")
            return templates.TemplateResponse(request, "auth.html", context, status_code=status.HTTP_400_BAD_REQUEST)
        return _redirect_with_session(request, "/", token)

    @app.post("/logout")
    def logout(request: Request, session=Depends(get_session)):
        if session is not None:
            delete_user_session(session, request)
        response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, session=Depends(get_session)):
        user = resolve_current_user(request, session)
        if user is None:
            context = build_auth_context(request, auth_error=None, email_value="", mode="login")
            return templates.TemplateResponse(request, "auth.html", context)
        context = build_user_context(
            request,
            session,
            user,
            title=settings.app_name,
            description="Private listening dashboard powered by your account.",
            active_page="home",
            new_webhook_token=None,
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
        user = resolve_current_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        items = history_events(
            session,
            user.id,
            limit=limit,
            event_type=event,
            query=q,
            artist=artist,
            timezone_name=settings.display_timezone,
        )
        context = build_user_context(
            request,
            session,
            user,
            title=f"History - {settings.app_name}",
            description="Full track timeline with filters.",
            active_page="history",
            history_items=items,
            filters={"event": event, "q": q or "", "artist": artist or "", "limit": limit},
            total_items=len(items),
            new_webhook_token=None,
        )
        return templates.TemplateResponse(request, "history.html", context)

    @app.get("/account", response_class=HTMLResponse)
    @app.get("/status", response_class=HTMLResponse)
    def account(request: Request, session=Depends(get_session)):
        user = resolve_current_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        context = build_user_context(
            request,
            session,
            user,
            title=f"Account - {settings.app_name}",
            description="Manage your profile, webhook token, and integration details.",
            active_page="account",
            new_webhook_token=None,
        )
        return templates.TemplateResponse(request, "status.html", context)

    @app.get("/api/recent")
    def api_recent(
        request: Request,
        limit: int = Query(default=settings.api_recent_limit, ge=1, le=20),
        event: str = Query(default="all"),
        session=Depends(get_session),
    ):
        if session is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is not configured.")
        user = resolve_current_user(request, session)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
        try:
            total_events = session.scalar(
                select(func.count()).select_from(ListeningEvent).where(ListeningEvent.user_id == user.id)
            ) or 0
            return JSONResponse(
                {
                    "ok": True,
                    "items": recent_events(
                        session,
                        user.id,
                        limit=limit,
                        timezone_name=settings.display_timezone,
                        event_type=event,
                    ),
                    "count": int(total_events),
                    "limit": limit,
                    "event": event,
                }
            )
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database error: {exc}") from exc

    @app.get("/api/now-playing")
    def api_now_playing(request: Request, session=Depends(get_session)):
        if session is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is not configured.")
        user = resolve_current_user(request, session)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
        try:
            card = current_card(session, user.id, timezone_name=settings.display_timezone)
            return JSONResponse({"ok": True, "item": card})
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database error: {exc}") from exc

    @app.get("/api/status")
    def api_status(request: Request, session=Depends(get_session)):
        if session is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is not configured.")
        user = resolve_current_user(request, session)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
        try:
            payload = {
                "ok": True,
                "app_name": settings.app_name,
                "user": {
                    "email": user.email,
                    "display_name": user.display_name,
                },
                "webhook_endpoint": f"{_absolute_base_url(request)}/api/webhook/{user.webhook_token}",
                "public_api_endpoint": f"{_absolute_base_url(request)}/api/public/{user.webhook_token}",
                "supported_events": ["nowplaying", "paused", "resumedplaying", "scrobble", "loved"],
                "recent_count": int(session.scalar(select(func.count()).select_from(ListeningEvent).where(ListeningEvent.user_id == user.id)) or 0),
                "now_playing": current_card(session, user.id, timezone_name=settings.display_timezone),
                "stats": stats_summary(session, user.id, timezone_name=settings.display_timezone),
            }
            return JSONResponse(payload)
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database error: {exc}") from exc

    @app.get("/api/public")
    @app.get("/api/public/{path_token}")
    def api_webhook_public_read(
        request: Request,
        path_token: str | None = None,
        limit: int = Query(default=settings.api_recent_limit, ge=1, le=50),
        event: str = Query(default="scrobble"),
        session=Depends(get_session),
    ):
        if session is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is not configured.")

        token = _resolve_token(request, path_token=path_token)
        if not token:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid webhook token.")

        user = session.scalar(select(UserAccount).where(UserAccount.webhook_token == token))
        if user is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid webhook token.")

        try:
            total_events = session.scalar(
                select(func.count()).select_from(ListeningEvent).where(ListeningEvent.user_id == user.id)
            ) or 0
            return JSONResponse(
                {
                    "ok": True,
                    "user": {
                        "display_name": user.display_name,
                    },
                    "now_playing": current_card(session, user.id, timezone_name=settings.display_timezone),
                    "recent": recent_events(
                        session,
                        user.id,
                        limit=limit,
                        timezone_name=settings.display_timezone,
                        event_type=event,
                    ),
                    "stats": stats_summary(session, user.id, timezone_name=settings.display_timezone),
                    "count": int(total_events),
                    "limit": limit,
                    "event": event,
                }
            )
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database error: {exc}") from exc

    def get_base64_image(url: str | None) -> str:
        if not url:
            return PLACEHOLDER_ART_URL
        if isinstance(url, str) and url.startswith("data:image"):
            return url
        try:
            response = requests.get(url, timeout=3)
            if response.status_code == 200 and response.content:
                content_type = response.headers.get("Content-Type", "image/jpeg").split(";", 1)[0]
                encoded = base64.b64encode(response.content).decode("utf-8")
                return f"data:{content_type};base64,{encoded}"
        except Exception:
            pass
        return PLACEHOLDER_ART_URL


    @app.get("/api/public/now-playing.svg/{token}")
    def now_playing_svg_public(token: str, session=Depends(get_session)):
        if session is None:
            return Response("Database not configured", status_code=503)

        # 🔐 token lookup (no cookies)
        user = session.scalar(
            select(UserAccount).where(UserAccount.webhook_token == token)
        )
        if user is None:
            return Response("Invalid token", status_code=404)

        current = current_card(session, user.id, timezone_name=settings.display_timezone)

        if not current:
            svg = """
            <svg width="450" height="130" xmlns="http://www.w3.org/2000/svg">
            <rect width="100%" height="100%" rx="18" fill="#0f172a"/>
            <text x="20" y="70" fill="#94a3b8" font-size="16">🎧 Nothing playing</text>
            </svg>
            """
            return Response(content=svg, media_type="image/svg+xml")

        title = escape(str(current.get("track", "Unknown")))
        artist = escape(str(current.get("artist", "Unknown")))
        cover_url = current.get("artwork_url")
        track_url = current.get("track_url")

        # 🔥 FIX: convert to base64 (GitHub-safe)
        cover_b64 = get_base64_image(cover_url)
        cover_attr = escape(cover_b64, quote=True)

        track_href = escape(str(track_url), quote=True) if track_url else ""
        card_open = f'<a href="{track_href}" target="_blank">' if track_href else ""
        card_close = "</a>" if track_href else ""

        svg = f"""
        <svg width="450" height="130" xmlns="http://www.w3.org/2000/svg">
        <defs>
            <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stop-color="#0f172a"/>
            <stop offset="100%" stop-color="#1e293b"/>
            </linearGradient>
        </defs>

        <rect width="100%" height="100%" rx="18" fill="url(#bg)"/>

        {card_open}
        <!-- Cover -->
        <image href="{cover_attr}" x="15" y="25" width="80" height="80" preserveAspectRatio="xMidYMid slice"/>

        <!-- Text -->
        <text x="110" y="50" fill="#22c55e" font-size="13">🎧 Now Playing</text>

        <text x="110" y="75" fill="#ffffff" font-size="17" font-weight="bold">
            {title}
        </text>

        <text x="110" y="100" fill="#94a3b8" font-size="14">
            {artist}
        </text>
        {card_close}
        </svg>
            """

        return Response(
            content=svg,
            media_type="image/svg+xml",
            headers={"Cache-Control": "s-maxage=60"}
        )
                
                

    @app.get("/api/public/recent.svg/{token}")
    def recent_svg(token: str, session=Depends(get_session)):
        if session is None:
            return Response("DB not ready", status_code=503)

        user = session.scalar(
            select(UserAccount).where(UserAccount.webhook_token == token)
        )
        if not user:
            return Response("Invalid token", status_code=404)

        tracks = recent_events(
            session,
            user.id,
            limit=3,
            timezone_name=settings.display_timezone,
            event_type="scrobble"
        )

        cards = ""
        x = 20

        for t in tracks:
            title = t.get("track", "Unknown")
            artist = t.get("artist", "Unknown")
            image_url = t.get("artwork_url") or PLACEHOLDER_ART_URL
            time = t.get("received_at_human", "just now")
            track_url = t.get("track_url")
    
            image_base64 = get_base64_image(image_url)
            title_text = escape(str(title[:22]))
            artist_text = escape(str(artist[:22]))
            time_text = escape(str(time))
            image_attr = escape(str(image_base64), quote=True)
            track_href = escape(str(track_url), quote=True) if track_url else ""
            card_open = f'<a href="{track_href}" target="_blank">' if track_href else ""
            card_close = "</a>" if track_href else ""

            cards += f"""
            <g transform="translate({x},40)">
            {card_open}
            <!-- Card -->
            <rect width="260" height="100" rx="16" fill="#0f172a"/>

            <!-- Blurred background -->
            <image href="{image_attr}" x="0" y="0" width="260" height="100"
                    opacity="0.25" filter="url(#blur)" preserveAspectRatio="xMidYMid slice"/>

            <!-- Cover -->
            <image href="{image_attr}" x="10" y="15" width="70" height="70" rx="10"/>

            <!-- Title -->
            <text x="90" y="40" fill="#ffffff" font-size="14" font-weight="bold">
                {title_text}
            </text>

            <!-- Artist -->
            <text x="90" y="60" fill="#cbd5f5" font-size="12">
                {artist_text}
            </text>

            <!-- Time -->
            <text x="90" y="80" fill="#a78bfa" font-size="11">
                {time_text}
            </text>

            <!-- Play button -->
            <circle cx="230" cy="50" r="14" fill="#1e293b"/>
            <polygon points="225,43 225,57 238,50" fill="#e2e8f0"/>
            {card_close}
            </g>
            """

            x += 280

        svg = f"""
        <svg width="900" height="160" xmlns="http://www.w3.org/2000/svg">
        <defs>
            <filter id="blur">
            <feGaussianBlur stdDeviation="12"/>
            </filter>
        </defs>

        <rect width="100%" height="100%" fill="#020617"/>

        <!-- Header -->
        <text x="20" y="25" fill="#e2e8f0" font-size="18" font-weight="bold">
            Recently Played
        </text>

        {cards}
        </svg>
            """

        return Response(
            content=svg,
            media_type="image/svg+xml",
            headers={"Cache-Control": "s-maxage=60"}
        )
    @app.get("/api/public/stats.svg/{token}")
    def stats_svg(token: str, session=Depends(get_session)):
        user = session.scalar(select(UserAccount).where(UserAccount.webhook_token == token))
        stats = stats_summary(session, user.id)

        svg = f"""
        <svg width="420" height="120" xmlns="http://www.w3.org/2000/svg">
        <rect width="100%" height="100%" fill="#0f172a" rx="12"/>
        <text x="20" y="40" fill="#22c55e">📊 Stats</text>
        <text x="20" y="70" fill="#fff">Total: {stats['total_scrobbles']}</text>
        <text x="20" y="95" fill="#94a3b8">Top: {stats['top_artist']}</text>
        </svg>
        """

        return Response(svg, media_type="image/svg+xml")

    @app.post("/api/webhook")
    @app.post("/api/webhook/{path_token}")
    async def webhook(request: Request, path_token: str | None = None, session=Depends(get_session)):
        if SessionLocal is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is not configured.")
        token = _resolve_token(request, path_token=path_token)
        if not token:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid webhook token.")

        user = session.scalar(select(UserAccount).where(UserAccount.webhook_token == token))
        if user is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid webhook token.")

        try:
            payload = await _read_webhook_payload(request)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unable to read webhook payload.") from exc

        # logger.info(
        #     "Webhook incoming payload: content_type=%s payload=%s",
        #     request.headers.get("content-type", ""),
        #     _payload_preview(payload),
        # )

        try:
            parsed = parse_webhook_payload(payload)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        # logger.info(
        #     "Webhook payload match report: %s",
        #     _payload_preview(_build_payload_match_report(payload, parsed), max_chars=2000),
        # )

        received_at = utcnow()
        try:
            event, deduped, state = store_event(session, user, parsed, received_at, max_events=settings.max_events)
            return JSONResponse(
                {
                    "ok": True,
                    "user": {
                        "email": user.email,
                        "display_name": user.display_name,
                    },
                    "deduped": deduped,
                    "stored_event": serialize_event(event, timezone_name=settings.display_timezone),
                    "current": serialize_state(state, timezone_name=settings.display_timezone) if state else None,
                }
            )
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database error: {exc}") from exc

    return app


def _create_default_app() -> FastAPI:
    try:
        return create_app()
    except RuntimeError as exc:
        logger.warning("App startup fallback: %s", exc)
        fallback = FastAPI(title="Recently Played (unconfigured)")

        @fallback.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
        def _config_error(path: str):
            return JSONResponse(
                {
                    "ok": False,
                    "error": str(exc),
                    "hint": "Set DATABASE_URL in your environment or .env file, then restart server.",
                },
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return fallback


# Export a concrete ASGI application instance for uvicorn targets like:
#   uvicorn app.main:app --reload
app = _create_default_app()
