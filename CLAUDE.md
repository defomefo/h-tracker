# H-Tracker — Context for Claude Code

This is the H-FARM College Global Partnerships Tracker. Internal CRM-style
tool for ~5–15 users managing partnerships with universities, agencies,
schools, and student organisations. Single-page web app + small Flask
backend. **Read this file end-to-end before making changes** — it captures
architectural decisions and intentional non-goals that aren't obvious from
the code.

---

## What it is

A 285-entity (and growing) partnership pipeline with:

- D3 orthographic globe with country dots + cinematic country-detail zoom
- 8-stage kanban (drag entities between stages)
- 3 strategic 2×2 maps (Effort × Fit, Reach × Readiness, Cost × ROI)
- Conversion funnel + Monte Carlo 90-day signing forecast
- Per-entity detail panel with editable profile + activity timeline + outreach log
- AI chat assistant ("ask the platform") + AI-drafted outreach emails (Gemini)
- Shared multi-user state, real edit attribution, "live activity" feed
- 7-template outreach library with `{{entity.name}}`-style variable substitution
- Per-entity field edits sync back to Google Sheets via Apps Script
- 60-second Undo toast on destructive operations

---

## Architecture (production)

```
   Users
     │
     ▼
   ┌──────────────────────────────────────┐
   │ Vercel (static frontend)             │  h-tracker-blue.vercel.app
   │   index.html + data/*.json           │
   │   vercel.json rewrites /api/* →      │
   └──────────┬───────────────────────────┘
              │
              ▼
   ┌──────────────────────────────────────┐
   │ Render (Flask + gunicorn)            │  h-tracker-api.onrender.com
   │   app.py, auth, kv_store, REST       │
   │   free-tier — sleeps after 15m idle  │
   └──────┬──────────────┬────────────────┘
          │              │
          ▼              ▼
   ┌─────────────┐  ┌──────────────────────┐
   │ Gemini 2.5  │  │ Neon Postgres        │
   │ Flash       │  │   (eu-central-1)     │
   └─────────────┘  └──────────────────────┘
                            ▲
                            │
   ┌──────────────────────────────────────┐
   │ Google Sheets (source of truth)      │
   │   CSV pub URL → read on app boot     │
   │   Apps Script Web App → write-back   │
   └──────────────────────────────────────┘
```

**Why the Vercel proxy:** browsers block cross-site cookies on `vercel.app`
→ `onrender.com`. `vercel.json` rewrites `/api/*` so the session cookie
stays first-party. Don't undo this — auth breaks without it.

**Why Render free + Neon free:** zero infra cost, no card-on-file (unlike
Fly). Tradeoff: Render instance sleeps after 15 min idle → first request
after sleep takes ~30–50 s. Acceptable for an internal tool.

---

## Key files

| File | Lines | Role |
|---|---:|---|
| `index.html` | ~8000 | Entire SPA. D3 globe, kanban, 2×2, funnel, database, outreach, admin, chat, identity. No build step — vanilla JS + D3 + PapaParse + jsPDF (CDN). |
| `app.py` | ~1000 | Flask backend. Auth, kv_store, outreach, presence, edits, sheets writeback, Gemini proxy. Dual SQLite (local) / Postgres (prod). |
| `data/programs.json` | — | H-FARM summer programmes catalogue |
| `data/teams.json` | — | H-FARM internal teams (Executive, Marketing, etc.) |
| `data/collab_formats.json` | — | Partnership offering formats |
| `data/brochures.json` | — | Drive links to PDFs |
| `data/templates.json` | — | 7 starter outreach email templates |
| `data/users.json` | — | Operator roster (handle, name, role, email) — Tier-1 identity |
| `data/bachelors.json` etc. | — | Programme catalogues by track |
| `H-FARM_Global_Partnerships_DATABASE.csv` | — | Local-dev fallback CSV. In prod, the app fetches a Google Sheets published-as-CSV URL the user pastes. |
| `requirements.txt` | — | flask, flask-cors, gunicorn, google-genai, psycopg, python-dotenv |
| `vercel.json` | — | Rewrites `/api/:path*` → `https://h-tracker-api.onrender.com/api/:path*` |
| `render.yaml` | — | Render Blueprint: Python web service, free plan, frankfurt region |
| `Dockerfile` | — | Backend container (Python 3.12-slim, gunicorn) |
| `DEPLOY.md` | — | Render + Neon setup walkthrough |
| `SHEETS_SYNC.md` | — | Apps Script for bidirectional Google Sheets sync |

---

## Local dev

```bash
cd /Users/defo/PycharmProjects/PythonProject4/h-tracker
source venv/bin/activate
pip install -r requirements.txt
python app.py
# → http://127.0.0.1:8000
```

**Local `.env`** (not committed — `.gitignore`):

```
GEMINI_API_KEY=AIza...
# Optional locally — when unset, auth is disabled (local dev convenience)
HFARM_APP_PASSWORD=...
HFARM_SECRET_KEY=...
# Optional — when unset, uses local SQLite (./h-tracker.db)
DATABASE_URL=postgresql://... (Neon pooled connection)
# Optional — when unset, Sheets write-back is silently skipped
HFARM_SHEETS_WRITEBACK_URL=https://script.google.com/macros/s/.../exec
```

Server detects which mode it's in via env vars — same `app.py` runs local
and prod.

---

## Deploy

```bash
git push  # triggers both Vercel and Render
```

- Vercel: rebuilds static frontend in ~20 s. Auto-deploys on push to `main`.
- Render: rebuilds Docker image in ~2 min. Auto-deploys on push to `main`.
- Neon: zero config — DB is always live. Sleeps after 5 min idle, wakes in
  <1 s.

Secrets live in Render dashboard → Environment tab. Never committed.

---

## What's been built (current state, in build order)

**Sync infrastructure (Phase 1–4)**

1. **Outreach log** in SQLite/Postgres (`outreach` table, REST endpoints).
2. **Stage / 2×2 / team overrides** via generic `kv_store` table
   (namespace + key + JSON value). Namespaces in use:
   `team_assignment`, `stage_override`, `map2x2_override`, `kb_draft`,
   `entity_override`.
3. **KB drafts** sync via `kv_store` (`kb_draft` namespace).
4. **Multi-user presence**: `presence` table refreshed via 30-s heartbeat;
   `edit_log` table captures who edited what when. Frontend chip shows
   "N online · last edit by X · 12s ago".

**Auth (Phase 5)**

5. **Shared-password gate** via `HFARM_APP_PASSWORD`. Flask session cookie
   (HttpOnly + SameSite=None + Secure + 30-day rolling). `@auth_required`
   decorator on all 14 protected `/api/*` routes.

**Hosting**

6. Initially Fly.io plan (saved in `fly.toml` for reference, not used).
7. Migrated to Render + Neon Postgres after Fly required a card.

**UX features**

- **AI chat assistant** (`/api/chat-query`) — sends entity scope + question
  to Gemini 2.5 Flash, returns `{intro, entity_ids}`. Resolver maps ids to
  result rows. Read-only by design, contacts included in payload.
- **AI email drafting** (`/api/draft-outreach`) — Gemini drafts a tailored
  email using entity + recipient + recommended format + sender identity.
- **Inline entity editing**: priority, strategic_tier, type, focus_areas,
  notes. Saves via `kv_store` namespace `entity_override`. `↶ Revert
  edits` per entity restores from `ENTITY_ORIGINALS` snapshot.
  `recomputeEntityDerived()` re-runs score / readiness / pipeline_stage
  after each edit.
- **Per-entity activity timeline** (`/api/edits/entity/<id>`) — joins
  edit_log + outreach to surface "who did what to this entity, when".
- **Home redesign**:
  - Morning briefing banner (time-aware greeting + 1–2 insights)
  - KPI tiles open inline expansion (top 8 entities in that priority)
    instead of jumping to globe
  - Live activity stream replaces "Reach by region" duplicate
- **Outreach templates library**: 7 starter templates + admin CRUD via
  the KB schema. `{{entity.name}}` / `{{recipient.firstName}}` /
  `{{sender.email}}` etc. substitution via `fillTemplate()`.
- **Sheets write-back**: every editable-field change POSTs to an Apps
  Script Web App URL that updates the matching row by `id` column.
  `/api/sheets/writeback` proxies. Toast feedback.
- **Tier-1 identity roster**: `data/users.json` lists operators. After
  login, picker assigns name + role + email to localStorage. Audit log
  + presence chip + briefing greeting all use the chosen identity.
  Outreach modal "Sending as: …" line + `📧 Save + open in mail client`
  button (generates mailto: URL).
- **60 s Undo toast**: covers `Reset stage overrides`, `Reset 2×2
  overrides` (single axis + all), `Delete outreach entry`, `KB Reset to
  file`. Snapshot → toast → `undoLastReset()`.
- **Misc fixes**: 3rd-party cookie issue (Vercel proxy), HTML 404 JSON
  parse bug (Flask JSON error handlers), stuck globe tooltip on redraw,
  detail panel re-opening itself after close, browser autofill ghost
  text in search inputs (type="search" + autocomplete="off"), kanban
  "+N more" expand toggle (was placeholder alert), recommended-team
  PRIMARY chip overflow.

---

## Patterns to follow (when extending)

**`kv_store` for any new per-key shared state.** Don't add new tables for
single-value-per-entity stuff. Pattern: pick a namespace name, add it to
`_KV_NAMESPACES` whitelist in `app.py`, write a thin frontend wrapper
matching the existing `syncTeamAssignmentsFromServer` / `setAssignment`
shape. `kv:put` and `kv:delete` automatically appear in the activity
timeline.

**Optimistic local + server mirror.** Every mutation writes to
localStorage first (snappy UI), then mirrors via `kvStore.put` /
`apiFetch`. Failure path: keep local, log warning, retry on next focus
sync. This is why the app feels instant even when Render cold-starts.

**One-shot migration on first sync.** Each sync function checks a
`hfarm_<thing>_migrated_v1` localStorage flag. If unset, POSTs any
pre-existing local data to `/_import` once, sets flag. Lets new
deployments inherit user data from before sync was added.

**Undo for destructive ops.** Single snapshot at a time
(`_lastReset`). 60-second toast (`showResetUndoToast`) with
`undoLastReset()` handler that knows each `kind`. If you add a new
destructive bulk operation, snapshot + emit toast in the same pattern.

**Identity flows through headers.** Every mutation sends
`X-Session-Id` and `X-Display-Name`. Server's `_record_edit()` reads
these and writes the audit log. Never trust the client to invent
identities, but for this trust-based shared-password model it's fine.

**Template substitution context.** `fillTemplate(text, ctx)` does
`{{path.to.value}}` replacement. Context shape: `{entity, recipient,
recommended, team, sender, kb}`. Both `applyOutreachTemplate` (frontend)
and `_build_prompt` (backend AI) use the same shape — don't drift.

**SQL placeholders.** Always `?` in SQL strings. `_q()` translates to
`%s` on Postgres. The `_DB` wrapper handles both backends transparently.

**Frontend `apiFetch` wrapper.** Use `apiFetch("/api/...")` for every
own-API call. It adds `credentials: "include"` and shows the login
overlay on confirmed 401. Don't bypass it.

---

## Intentional non-goals (don't auto-implement these)

| Idea | Why deferred | Trigger to revisit |
|---|---|---|
| Per-user passwords (Tier 2) | Shared password is honest for current trust level; multi-user identity already works via roster | Team grows past ~20 or external auditors require it |
| Google Workspace SSO (Tier 3) | Premature before platform is approved by H-FARM IT | Platform officially adopted |
| Mobile responsive cleanup | Desktop-first; sales team usage from phone not validated | Field-staff feedback shows phone use |
| Contracts view with real MoU model | Placeholder is fine for v1 | Legal team wants signed-date + expiry tracking in-app |
| Calendar / reminders | Out of scope for v1 | "Why didn't we follow up?" complaints |
| Bulk actions (multi-select in database) | Existing per-row flow is fast enough for current volume | Pipeline >500 entities or batch-assign requests |
| Three classification systems consolidation | `priority` and `strategic_tier` are genuinely orthogonal; `partnership_readiness` is already computed | User confusion reports |

If a teammate asks for one of these, point them at this section first.

---

## Production URLs

- **Frontend**: <https://h-tracker-blue.vercel.app/>
- **Backend**: <https://h-tracker-api.onrender.com/api/health>
- **GitHub**: <https://github.com/defomefo/h-tracker>
- **Vercel dashboard**: <https://vercel.com/>
- **Render dashboard**: <https://dashboard.render.com/>
- **Neon dashboard**: <https://console.neon.tech/>
- **Google Sheet** (source CSV): published via Sheet → File → Share → Publish to web

---

## New-session checklist

When you (the next Claude Code session, or a new developer) open this
project, do these first:

1. **Read this file.** You're here. Skim the rest.
2. **Check the deploy is healthy.**
   ```bash
   curl -s https://h-tracker-api.onrender.com/api/health | python3 -m json.tool
   ```
   Expect: `{"ok": true, "auth_required": true, "key_set": true,
   "db_backend": "postgres", "sheets_writeback": true, ...}`.
3. **If working locally, start the backend.**
   ```bash
   source venv/bin/activate && python app.py
   # http://127.0.0.1:8000 — auth is OFF locally unless HFARM_APP_PASSWORD set
   ```
4. **If touching the deploy guide, read `DEPLOY.md`.**
5. **If touching Sheets sync, read `SHEETS_SYNC.md`.**

---

## Conventions / gotchas

- **No build step.** Edit `index.html` directly; Vercel serves it as-is.
- **PyCharm auto-commits** on save — recent commit messages often say
  "Update website". This is fine.
- **`.env` is gitignored** — never commit secrets. Render env vars are
  the source of truth in prod.
- **The Apps Script must be re-deployed** when you edit it. URL stays
  the same across re-deployments.
- **Don't use `alert()` for confirmations.** Use `confirm()` for
  destructive + add an Undo toast.
- **All edit attribution depends on localStorage.** If a user clears
  their browser data, they become "Anonymous" until they pick from
  the roster again.
- **Render free instance cold-start is ~30-50 s.** First request after
  idle is slow; design for that (apiFetch handles 401 gracefully).

---

_Last meaningful update: this file was generated after 23 features +
several rounds of polish. Edit it when architecture or non-goals
change, not for every feature added._
