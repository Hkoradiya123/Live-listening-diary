# Live listening diary

Dark, responsive music-scrobbling dashboard for Web Scrobbler.

## Stack

- FastAPI
- Jinja2 server-side rendering
- SQLAlchemy 2
- PostgreSQL on Supabase
- Vercel Python Functions

## Environment Variables

Set these in Vercel:

- `DATABASE_URL` - Supabase PostgreSQL connection string
- `DISPLAY_TIMEZONE` - optional display timezone, defaults to `UTC`
- `APP_NAME` - optional display name, defaults to `Live listening diary`

## Routes

- `/login` - sign in page
- `/` - private dashboard for the logged-in account
- `/history` - full private timeline with filters
- `/account` - webhook and integration details
- `/api/webhook` - per-user webhook ingestion endpoint
- `/api/recent` - recent listens JSON for the logged-in account
- `/api/now-playing` - current track JSON for the logged-in account
- `/api/status` - account status JSON

## Webhook

Each account gets its own webhook token. Open `/account` after signing in to copy the token and endpoint.

Send JSON POST requests to `/api/webhook/<TOKEN>` with the header:

`X-Recently-Played-Token: <TOKEN>`

You can also send `Authorization: Bearer <TOKEN>` or post directly to `/api/webhook/<TOKEN>`.

Supported event types:

- `nowplaying`
- `paused`
- `resumedplaying`
- `scrobble`
- `loved`

## Deploy

1. Create a PostgreSQL database on Supabase.
2. Set the environment variables above in Vercel.
3. Deploy the repository to Vercel.
4. Create an account, copy the webhook token from `/account`, and configure Web Scrobbler to POST there.

## Local Development

Set `DATABASE_URL` to your Supabase PostgreSQL connection string, then run a local ASGI server:

```bash
$env:DATABASE_URL="postgresql+psycopg://USER:PASSWORD@HOST:5432/postgres"
pip install -r requirements.txt
uvicorn api.index:app --reload
```
