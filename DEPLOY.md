# Deploying the backend to Fly.io

The Vercel deploy at `https://h-tracker-blue.vercel.app/` serves the static
frontend. The Flask backend (AI, SQLite sync, presence) runs on Fly.io and
the frontend automatically points at `https://h-tracker-api.fly.dev` when it
detects it's running on a `*.vercel.app` host.

## One-time setup (~10 minutes)

### 1. Install flyctl

```bash
# macOS
brew install flyctl

# Or via curl
curl -L https://fly.io/install.sh | sh
```

### 2. Sign up + log in (free tier, credit card required for verification but not charged)

```bash
flyctl auth signup     # opens browser; or `flyctl auth login` if you already have an account
```

### 3. Create the app and DB volume (without deploying yet)

```bash
cd /Users/defo/PycharmProjects/PythonProject4/h-tracker

flyctl launch --no-deploy --name h-tracker-api --region fra --copy-config
# When asked about Postgres, Redis, etc — say NO to all
# When asked to use the existing fly.toml — say YES

# Persistent volume for SQLite (1 GB free per app)
flyctl volumes create hfarm_data --size 1 --region fra --yes
```

### 4. Push your Gemini key as a Fly secret (never commit it!)

```bash
flyctl secrets set GEMINI_API_KEY="paste-your-key-here"
```

### 5. Deploy

```bash
flyctl deploy
```

You'll see Docker build + push + machine start. First deploy takes ~3 minutes;
subsequent deploys ~30 seconds.

### 6. Verify

```bash
# Health check should return {ok: true, key_set: true, ...}
curl https://h-tracker-api.fly.dev/api/health
```

Then visit `https://h-tracker-blue.vercel.app/` and the chat + AI button
should work — Vercel page → Fly API → Gemini.

## Day-to-day

* **Push a code change** — `git push` to GitHub triggers Vercel (frontend).
  For backend changes, also run `flyctl deploy` from this folder.
* **Rotate / update Gemini key** — `flyctl secrets set GEMINI_API_KEY=...`
  (automatically redeploys).
* **Check logs** — `flyctl logs` (live tail) or `flyctl logs --no-tail | tail`.
* **SSH into the machine** — `flyctl ssh console` (useful for inspecting the
  SQLite DB: `sqlite3 /data/h-tracker.db`).
* **Pause the app to save free-tier hours** — `flyctl scale count 0`.
  `flyctl scale count 1` to resume.

## Costs

Fly's free tier covers this app indefinitely at typical H-FARM usage:

* 3 shared-cpu-1x machines (we use 1)
* 3 GB persistent volume (we use 1)
* 160 GB/month outbound bandwidth (typical: <1 GB/month)
* Machine auto-stops when idle (`auto_stop_machines = "stop"` in fly.toml)
  so the free-tier hours go a long way.

If usage grows past free tier, expected cost is ~$5/month.

## Adding a new frontend origin (e.g. a second Vercel preview URL)

Update `HFARM_CORS_ORIGINS` and redeploy — no code change needed:

```bash
flyctl secrets set HFARM_CORS_ORIGINS="https://h-tracker-blue.vercel.app,https://h-tracker-preview-xyz.vercel.app,http://127.0.0.1:8000,http://localhost:8000"
```

## Why this setup

* **Flask stays Flask** — no rewriting routes as serverless functions.
* **SQLite stays SQLite** — one file on a persistent disk; backed up by
  `flyctl ssh console` + `scp` if you ever want a copy.
* **Vercel handles the static frontend** — fast global CDN, free, atomic
  deploys on every git push. Doesn't try to run Python.
* **CORS** — Flask is configured to accept requests from the Vercel domain
  and from localhost for dev. Override via `HFARM_CORS_ORIGINS`.
