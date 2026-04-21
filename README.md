# Live listening diary

Dark, responsive music-scrobbling dashboard for Web Scrobbler.

## Stack

- FastAPI
- Jinja2 server-side rendering
- SQLAlchemy 2
- PostgreSQL on Neon or Supabase
- Vercel Python Functions

## Environment Variables

Set these in Vercel:

- `DATABASE_URL` - PostgreSQL connection string
- `WEBHOOK_SECRET` - secret token used to protect the webhook
- `DISPLAY_TIMEZONE` - optional display timezone, defaults to `UTC`
- `APP_NAME` - optional display name, defaults to `Live listening diary`

## Routes

- `/` - public homepage
- `/history` - full timeline with filters
- `/status` - webhook/API documentation
- `/api/webhook` - protected webhook ingestion endpoint
- `/api/recent` - recent listens JSON
- `/api/now-playing` - current track JSON
- `/api/status` - API status JSON

## Webhook

Send JSON POST requests to `/api/webhook` with the header:

`X-Recently-Played-Token: <WEBHOOK_SECRET>`

You can also POST to `/api/webhook/<WEBHOOK_SECRET>` if your sender prefers path-based auth.

Supported event types:

- `nowplaying`
- `paused`
- `resumedplaying`
- `scrobble`
- `loved`

## Deploy

1. Create a PostgreSQL database on Neon or Supabase.
2. Set the environment variables above in Vercel.
3. Deploy the repository to Vercel.
4. Configure Web Scrobbler to POST to your webhook endpoint.

## Local Development

Install dependencies and run a local ASGI server:

```bash
pip install -r requirements.txt
uvicorn api.index:app --reload
```
