from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def build_client():
    app = create_app(
        database_url="sqlite+pysqlite:///:memory:",
        display_timezone="UTC",
    )
    return TestClient(app)


def register_user(client: TestClient, email: str, password: str, display_name: str) -> str:
    response = client.post(
        "/register",
        data={
            "email": email,
            "password": password,
            "display_name": display_name,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    status_response = client.get("/api/status")
    assert status_response.status_code == 200
    return status_response.json()["webhook_endpoint"].rsplit("/", 1)[-1]


def extract_current_artist(response_json: dict) -> str:
    item = response_json.get("item") or {}
    return item.get("artist", "")


def test_homepage_loads_auth_screen_for_anonymous_users():
    client = build_client()
    response = client.get("/")
    assert response.status_code == 200
    assert "Sign in to your listening diary" in response.text


def test_register_login_and_logout_flow():
    client = build_client()

    register_response = client.post(
        "/register",
        data={
            "email": "alice@example.com",
            "password": "password123",
            "display_name": "Alice",
        },
        follow_redirects=False,
    )
    assert register_response.status_code == 303

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Hi, Alice" in dashboard.text

    logout_response = client.post("/logout", follow_redirects=False)
    assert logout_response.status_code == 303

    login_response = client.post(
        "/login",
        data={
            "email": "alice@example.com",
            "password": "password123",
        },
        follow_redirects=False,
    )
    assert login_response.status_code == 303


def test_webhook_stores_and_dedupes_per_user():
    client = build_client()
    webhook_token = register_user(client, "alice@example.com", "password123", "Alice")

    payload = {
        "event": "scrobble",
        "song": {
            "artist": "Daft Punk",
            "track": "Digital Love",
            "album": "Discovery",
            "artwork": "https://example.com/art.jpg",
            "url": "https://example.com/track",
            "timestamp": "2026-04-21T18:14:00Z",
        },
    }

    first = client.post(f"/api/webhook/{webhook_token}", json=payload)
    assert first.status_code == 200
    assert first.json()["ok"] is True

    second = client.post(f"/api/webhook/{webhook_token}", json=payload)
    assert second.status_code == 200
    assert second.json()["deduped"] is True


def test_paused_events_do_not_replace_the_live_card():
    client = build_client()
    webhook_token = register_user(client, "alice@example.com", "password123", "Alice")

    client.post(
        f"/api/webhook/{webhook_token}",
        json={
            "event": "nowplaying",
            "song": {
                "artist": "UNIYAL and Soumya Rawat",
                "track": "Vartmaan",
            },
        },
    )
    client.post(
        f"/api/webhook/{webhook_token}",
        json={
            "event": "paused",
            "song": {
                "artist": "UNIYAL and Soumya Rawat",
                "track": "Vartmaan",
            },
        },
    )

    now_playing = client.get("/api/now-playing")
    assert now_playing.status_code == 200
    item = now_playing.json()["item"]
    assert item["artist"] == "UNIYAL and Soumya Rawat"
    assert item["track"] == "Vartmaan"
    assert item["badge_label"] == "Now Playing"


def test_new_nowplaying_retires_previous_track_to_played():
    client = build_client()
    webhook_token = register_user(client, "alice@example.com", "password123", "Alice")

    client.post(
        f"/api/webhook/{webhook_token}",
        json={
            "event": "nowplaying",
            "song": {
                "artist": "Artist A",
                "track": "Track A",
            },
        },
    )
    client.post(
        f"/api/webhook/{webhook_token}",
        json={
            "event": "nowplaying",
            "song": {
                "artist": "Artist B",
                "track": "Track B",
            },
        },
    )

    history = client.get("/history?limit=2")
    assert history.status_code == 200
    assert "Track B" in history.text
    assert "Track A" in history.text
    assert "badge--played" in history.text


def test_history_collapses_consecutive_duplicate_tracks():
    client = build_client()
    webhook_token = register_user(client, "alice@example.com", "password123", "Alice")

    client.post(
        f"/api/webhook/{webhook_token}",
        json={
            "event": "nowplaying",
            "song": {
                "artist": "THE 9TEEN and Sandesh Shandilya",
                "track": "Aankhon Mein Doob Jaane Ko",
            },
        },
    )
    client.post(
        f"/api/webhook/{webhook_token}",
        json={
            "event": "paused",
            "song": {
                "artist": "THE 9TEEN and Sandesh Shandilya",
                "track": "Aankhon Mein Doob Jaane Ko",
            },
        },
    )

    history = client.get("/history?limit=10")
    assert history.status_code == 200
    assert history.text.count('class="track-row"') == 1


def test_accounts_are_isolated_from_each_other():
    app = create_app(
        database_url="sqlite+pysqlite:///:memory:",
        display_timezone="UTC",
    )
    client_a = TestClient(app)
    client_b = TestClient(app)

    token_a = register_user(client_a, "alice@example.com", "password123", "Alice")
    token_b = register_user(client_b, "bob@example.com", "password123", "Bob")

    assert token_a != token_b

    client_a.post(
        f"/api/webhook/{token_a}",
        json={
            "event": "nowplaying",
            "song": {
                "artist": "Artist A",
                "track": "Track A",
            },
        },
    )
    client_b.post(
        f"/api/webhook/{token_b}",
        json={
            "event": "nowplaying",
            "song": {
                "artist": "Artist B",
                "track": "Track B",
            },
        },
    )

    now_a = client_a.get("/api/now-playing")
    now_b = client_b.get("/api/now-playing")
    assert now_a.status_code == 200
    assert now_b.status_code == 200
    assert extract_current_artist(now_a.json()) == "Artist A"
    assert extract_current_artist(now_b.json()) == "Artist B"

    recent_a = client_a.get("/api/recent")
    recent_b = client_b.get("/api/recent")
    assert recent_a.status_code == 200
    assert recent_b.status_code == 200
    assert recent_a.json()["items"][0]["artist"] == "Artist A"
    assert recent_b.json()["items"][0]["artist"] == "Artist B"


def test_api_requires_login_for_private_data():
    client = build_client()
    response = client.get("/api/recent")
    assert response.status_code == 401


def test_account_page_exposes_webhook_details():
    client = build_client()
    webhook_token = register_user(client, "alice@example.com", "password123", "Alice")

    account_response = client.get("/account")
    assert account_response.status_code == 200
    assert webhook_token in account_response.text
    assert "/api/webhook/" in account_response.text


def test_public_webhook_read_endpoint_fetches_data_without_login():
    app = create_app(
        database_url="sqlite+pysqlite:///:memory:",
        display_timezone="UTC",
    )
    owner_client = TestClient(app)
    public_client = TestClient(app)

    token = register_user(owner_client, "alice@example.com", "password123", "Alice")
    owner_client.post(
        f"/api/webhook/{token}",
        json={
            "event": "scrobble",
            "song": {
                "artist": "Seedhe Maut",
                "track": "Raat Ki Rani",
                "album": "SHAKTI",
            },
        },
    )

    response = public_client.get(f"/api/webhook/{token}?event=scrobble&limit=3")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["user"]["display_name"] == "Alice"
    assert payload["event"] == "scrobble"
    assert payload["recent"]
    assert payload["recent"][0]["artist"] == "Seedhe Maut"


def test_public_webhook_read_endpoint_rejects_invalid_token():
    client = build_client()
    response = client.get("/api/webhook/not-a-valid-token")
    assert response.status_code == 403
