from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def build_client():
    app = create_app(
        database_url="sqlite+pysqlite:///:memory:",
        webhook_secret="test-secret",
        display_timezone="UTC",
    )
    return TestClient(app)


def test_homepage_loads():
    client = build_client()
    response = client.get("/")
    assert response.status_code == 200
    assert "Live listening diary" in response.text


def test_webhook_stores_and_dedupes():
    client = build_client()
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
    headers = {"X-Recently-Played-Token": "test-secret"}
    first = client.post("/api/webhook", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.json()["ok"] is True
    second = client.post("/api/webhook", json=payload, headers=headers)
    assert second.status_code == 200
    assert second.json()["deduped"] is True


def test_recent_endpoint_returns_items():
    client = build_client()
    headers = {"X-Recently-Played-Token": "test-secret"}
    client.post(
        "/api/webhook",
        json={
            "event": "nowplaying",
            "song": {
                "artist": "Kavinsky",
                "track": "Nightcall",
                "album": "OutRun",
            },
        },
        headers=headers,
    )
    response = client.get("/api/recent")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["items"][0]["artist"] == "Kavinsky"
