# Deploying the backend to Render + Neon

The Vercel deploy at `https://h-tracker-blue.vercel.app/` serves the static
frontend. The Flask backend runs on **Render.com** (free tier, no credit card)
and the database is **Neon Postgres** (free tier, no credit card).

When the frontend detects it's running on a `*.vercel.app` host, it
automatically points its `/api/*` calls at `https://h-tracker-api.onrender.com`.

## One-time setup (~15 minutes)

### 1. Create the Neon Postgres database

1. Go to <https://neon.tech> → "Sign up with GitHub" (or email)
2. Free tier: 0.5 GB storage, 100 hours/month compute — more than enough
3. After signup, you'll see a default project. Click the **Connection details**
   panel on the right.
4. Make sure **Pooled connection** is selected (not Direct) — this gives the
   shorter idle reconnect time Render needs.
5. Copy the connection string. It looks like:
   ```
   postgresql://user:password@ep-xxx-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require
   ```
6. **Save it somewhere safe** — you'll paste it into Render in step 3.

### 2. Push the new code to GitHub

```bash
cd /Users/defo/PycharmProjects/PythonProject4/h-tracker
git add app.py index.html requirements.txt render.yaml DEPLOY.md
git commit -m "Add Postgres support + Render deploy config"
git push
```

### 3. Create the Render web service

1. Go to <https://render.com> → "Sign up with GitHub"
2. After authorizing GitHub, click **New +** → **Blueprint**
3. Pick the `h-tracker` repo. Render reads `render.yaml` and shows the service it will create.
4. Click **Apply**. Render starts the first build (~2-3 minutes).
5. While it builds, click into the new `h-tracker-api` service → **Environment** tab.
6. Add two secrets (these are marked `sync: false` in render.yaml so they're not in git):
   * **`DATABASE_URL`** = paste the Neon connection string from step 1
   * **`GEMINI_API_KEY`** = paste your Gemini key
7. Click **Save Changes**. Render will rebuild + redeploy with the secrets.

### 4. Verify

Once the service shows **Live** in the Render dashboard:

```bash
curl https://h-tracker-api.onrender.com/api/health
# → {"db":"neon/render","db_backend":"postgres","key_set":true,"model":"gemini-2.5-flash","ok":true,"provider":"gemini"}
```

Then open `https://h-tracker-blue.vercel.app/` and hard-refresh (Cmd+Shift+R).
- Chat answers free-form questions
- "Draft with AI" loses the SOON badge

## Day-to-day

* **Frontend changes** — `git push` triggers Vercel.
* **Backend changes** — `git push` triggers Render (autoDeploy is on).
* **Rotate Gemini key / change CORS** — Render dashboard → Environment → edit → Save Changes (redeploys).
* **Check logs** — Render dashboard → service → Logs tab. Live streamed.
* **Check the database** — Neon dashboard → SQL Editor. Query `SELECT * FROM outreach;` to inspect data.

## Free-tier caveats

* **Render free instance sleeps after 15 min of inactivity.** First request
  after sleep takes ~30-50 seconds to wake the container. Acceptable for an
  internal team tool; if someone's actively using it the instance stays warm.
  To eliminate cold starts, upgrade to Render Starter ($7/mo) which never sleeps.
* **Neon free tier auto-pauses after 5 min idle** but wakes in <1 second
  on the next query. Effectively invisible.
* **Neon free storage is 0.5 GB.** This app's data will be <10 MB even with
  thousands of outreach entries; you'll never hit this.

## Local development (still works exactly the same)

Don't set `DATABASE_URL` locally. With no `DATABASE_URL`, the app uses
SQLite at `h-tracker.db` exactly as before:

```bash
cd /Users/defo/PycharmProjects/PythonProject4/h-tracker
source venv/bin/activate
python app.py
# → uses local sqlite, available at http://127.0.0.1:8000
```

If you want to test against the production Postgres locally, put the Neon
connection string in your local `.env`:

```bash
echo 'DATABASE_URL=postgresql://...' >> .env
python app.py
# → now uses Neon Postgres
```

## Adding a new frontend origin

Update `HFARM_CORS_ORIGINS` in Render's Environment tab (comma-separated)
and click Save Changes. No code change needed.

## Why this setup

* **Render hosts Flask** — free tier, no card, autoDeploys on git push.
* **Neon hosts Postgres** — free tier, no card, serverless (scales to zero).
* **Vercel hosts the static frontend** — free, global CDN, atomic deploys.
* **Dual SQLite/Postgres in app.py** — local dev stays simple (no Postgres
  install needed), production uses real Postgres.
