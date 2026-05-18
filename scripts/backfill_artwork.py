from __future__ import annotations

import argparse
import base64
from io import BytesIO
import sys
from pathlib import Path
import time

from dotenv import load_dotenv
from PIL import Image
import requests
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.db import build_engine, build_session_factory, session_scope
from app.models import ListeningEvent, PlaybackState


ARTWORK_SIZE = (96, 96)
REQUEST_TIMEOUT_SECONDS = 3


def _is_data_uri(value: str | None) -> bool:
    return bool(value) and value.startswith("data:image")


def _fetch_data_uri(url: str) -> str | None:
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    except Exception:
        return None
    if response.status_code != 200 or not response.content:
        return None
    try:
        image = Image.open(BytesIO(response.content))
        image = image.convert("RGB").resize(ARTWORK_SIZE, Image.LANCZOS)
        output = BytesIO()
        image.save(output, format="JPEG", quality=85, optimize=True)
        encoded = base64.b64encode(output.getvalue()).decode("utf-8")
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        return None


def _backfill_table(session, model, dry_run: bool, limit: int | None) -> tuple[int, int]:
    updated = 0
    skipped = 0
    stmt = select(model).where(model.artwork_url.is_not(None))
    if limit:
        stmt = stmt.limit(limit)
    rows = session.scalars(stmt).all()
    for row in rows:
        if _is_data_uri(row.artwork_url):
            skipped += 1
            continue
        data_uri = _fetch_data_uri(row.artwork_url)
        if not data_uri:
            skipped += 1
            continue
        updated += 1
        if not dry_run:
            row.artwork_url = data_uri
    if not dry_run:
        session.flush()
    return updated, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill artwork URLs into data URIs.")
    parser.add_argument("--limit", type=int, default=None, help="Max rows per table to process.")
    parser.add_argument("--dry-run", action="store_true", help="Scan without writing changes.")
    args = parser.parse_args()

    load_dotenv()
    settings = load_settings()

    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)

    started = time.time()
    with session_scope(session_factory) as session:
        events_updated, events_skipped = _backfill_table(session, ListeningEvent, args.dry_run, args.limit)
        state_updated, state_skipped = _backfill_table(session, PlaybackState, args.dry_run, args.limit)

    elapsed = time.time() - started
    mode = "DRY RUN" if args.dry_run else "APPLIED"
    print(
        f"{mode}: ListeningEvent updated={events_updated} skipped={events_skipped}; "
        f"PlaybackState updated={state_updated} skipped={state_skipped}; "
        f"elapsed={elapsed:.2f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
