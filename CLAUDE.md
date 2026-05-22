# H-Tracker — Context for Claude Code

This is the H-FARM College Global Partnerships Tracker. Internal CRM-style
tool for ~5–15 users managing partnerships with universities, agencies,
schools, and student organisations. Single-page web app + small Flask
backend. **Read this file end-to-end before making changes** — it captures
architectural decisions and intentional non-goals that aren't obvious from
the code.

---

## What it is

A 285-entity (and growing) partnership pipeline + adjacent workflows:

- D3 orthographic globe with country dots + cinematic country-detail zoom
- 8-stage kanban (drag entities between stages)
- 3 strategic 2×2 maps (Effort × Fit, Reach × Readiness, Cost × ROI)
  with engagement-depth Z axis, animated 2D↔3D toggle, narrated
  storytelling, decision-layer card, and a Three.js cinematic
  presentation mode (lazy-loaded, executive-facing)
- Conversion funnel + Monte Carlo 90-day signing forecast
- Per-entity detail panel with editable profile + activity timeline +
  outreach log + contracts + follow-ups + engagement-depth breakdown
- AI chat assistant ("ask the platform") + AI-drafted outreach emails (Gemini)
- Shared multi-user state, real edit attribution, "live activity" feed
- 7-template outreach library with `{{entity.name}}`-style variable substitution
- Per-entity field edits sync back to Google Sheets via Apps Script
- 60-second Undo toast on destructive operations
- **Contracts** — MoU/NDA tracker with alarms (stuck negotiating 60+ d,
  expiring 30/60/90 d, expired), per-entity inline create, activity timeline
- **Follow-ups** — prospective "next step per partner" with due date +
  owner, grouped action queue, Home briefing alarms, undo
- **Sponsors** — *separate* annual sponsorship pipeline (Career Day),
  one row per (event_year, company), Gold-at-risk decision layer,
  CSV import flow
- **PDF brief generation** — WeasyPrint server-side typeset 1-pager
  per partner (FT/McKinsey style, real text, embeddable in decks)

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
| `index.html` | ~13,000 | Entire SPA. D3 globe, kanban, 3×2×2 maps (with Three.js Present mode lazy-loaded), funnel, database, outreach, admin, chat, identity, contracts, follow-ups, sponsors. No build step — vanilla JS + D3 + PapaParse + jsPDF + Three.js (all via CDN). |
| `app.py` | ~2,100 | Flask backend. Auth, kv_store, outreach, presence, edits, sheets writeback, Gemini proxy, contracts, snapshots, follow-ups, sponsors, brief PDF render. Dual SQLite (local) / Postgres (prod). |
| `templates/brief.html` | ~250 | WeasyPrint Jinja template for the typeset 1-page partnership PDF. |
| `scripts/clean_sponsors.py` | ~370 | One-shot, re-runnable Career Day Excel ingest pipeline (3-sheet xlsx → canonical CSV + review CSV). Uses rapidfuzz for variant matching. Outputs to `scripts/output/` (gitignored). |
| `data/programs.json` | — | H-FARM College summer programmes catalogue |
| `data/teams.json` | — | H-FARM College internal teams (Executive, Marketing, etc.) |
| `data/collab_formats.json` | — | Partnership offering formats |
| `data/brochures.json` | — | Drive links to PDFs |
| `data/templates.json` | — | 7 starter outreach email templates |
| `data/users.json` | — | Operator roster (handle, name, role, email) — Tier-1 identity |
| `data/bachelors.json` etc. | — | Programme catalogues by track |
| `H-FARM College_Global_Partnerships_DATABASE.csv` | — | Local-dev fallback CSV. In prod, the app fetches a Google Sheets published-as-CSV URL the user pastes. |
| `requirements.txt` | — | flask, flask-cors, gunicorn, google-genai, psycopg, python-dotenv, weasyprint, jinja2 |
| `Dockerfile` | — | Backend container (Python 3.12-slim, gunicorn) — includes Cairo + Pango + fonts for WeasyPrint |
| `vercel.json` | — | Rewrites `/api/:path*` → `https://h-tracker-api.onrender.com/api/:path*` |
| `render.yaml` | — | Render Blueprint: Python web service, free plan, frankfurt region |
| `DEPLOY.md` | — | Render + Neon setup walkthrough |
| `SHEETS_SYNC.md` | — | Apps Script for bidirectional Google Sheets sync |
| `design.md` | — | H-FARM College brand design system (uploaded to Stitch + used by future designers) |

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

**Phase 6+ — major features shipped after CLAUDE.md v1**

- **Contracts view** (`contracts` table + 4 REST + per-entity tab):
  MoU/NDA/Service Agreement tracker. Alarms by band: expired,
  expiring 30/60/90 d, stuck in negotiation 60+ d. Each contract
  has type / status / dates / value / programs / signers / notes /
  attachments. Per-entity inline create. Activity timeline +
  linked outreach inside detail modal. `_record_edit("contract", ...)`
  feeds the entity Activity tab.

- **3D strategic maps** (in 3 phases, `entity_position_snapshots`
  table backing all of it):
  - *Phase A* — `engagementDepth(u)` client-side formula
    (40 outreach + 30 contacts + 30 persistence). Surfaced in
    detail panel as a transparent breakdown. Daily client-side
    opportunistic snapshot to `entity_position_snapshots` so
    trajectory analytics has historical data 4-6 months out.
  - *Phase B* — 2D/3D toggle on each Strategic Map. Axonometric
    JS projection (no library), floor + grid + axes + per-entity
    tower (rod + sphere + shadow). Hero rings + persistent labels
    on Top-N opportunities. Per-axis "What the Z axis reveals"
    overlay (sunk-cost trap / strategic neglect / hidden goldmine).
    Smooth 2D↔3D transition animation (rAF bubble interp +
    CSS fade staggers + stem-grow).
  - *Phase C* — Three.js cinematic Present mode. Fullscreen
    overlay, lazy-loaded from `esm.sh/three@0.160`. Real spheres
    with phong material, UnrealBloom pass on hero rings, camera
    intro tween (1.6s easeOutQuart), OrbitControls (drag rotate +
    wheel zoom + autoRotate), HTML axis labels projected from
    world space. Operational SVG view stays 0 KB extra; Three.js
    only downloads when the user clicks ◉ Present.

- **PDF brief generation** (`/api/brief/<id>` + `templates/brief.html`):
  WeasyPrint server-side typeset 1-page brief. Real text (not
  raster), embedded fonts, FT/McKinsey aesthetic. Pulls full
  context (entity + depth + programs + contacts + outreach +
  contracts + action_html) and produces a downloadable PDF.
  Dockerfile installs Cairo + Pango + Pango-FT2 + GDK-Pixbuf +
  DejaVu fonts.

- **Follow-ups** (`followups` table + 4 REST + per-entity tab +
  standalone view at `/followups`): prospective counterpart to
  the outreach log. Title + due_date + owner_handle + status
  (open/done) + notes. Standalone view groups by Overdue / Today /
  This week / Later / No date / Done. KPI tiles, status + owner
  filter pills, debounced search. Home briefing surfaces overdue
  + due today + due this week. Undo on delete. `_record_edit
  ("followup", ...)` feeds activity timeline.

- **Sponsors** (`sponsors` table + 5 REST + standalone view at
  `/sponsors`): annual Career Day sponsorship pipeline. *Separate
  from UNIS by design* — different lifecycle (annual transactional)
  + stakeholders (Marketing/Events) + KPIs (revenue + contract
  close rate). One row per (`event_year`, `normalized_name`) so a
  returning sponsor has multiple rows + year-over-year + renewal
  analytics read natively. Year selector + KPI tiles (revenue /
  paid / unsigned / Gold-at-risk) + tier pills (Gold/Bronze/Base)
  + status filter + sector dropdown + search. Decision-layer alert
  calls out unsigned Gold-tier contracts by name. Detail modal +
  edit modal + **CSV import modal** (multipart upload of the
  `scripts/clean_sponsors.py` canonical output, dry-run preview,
  idempotent by natural key).
  - `scripts/clean_sponsors.py` is the matched ingest tool:
    reads the raw event Excel, drops H-FARM College internal entries
    (Staff/Studenti HFC), fuzzy-matches variants (rapidfuzz
    token_set_ratio ≥ 85), outputs `sponsors_canonical_YYYY.csv`
    + `sponsors_review_YYYY.csv`. Output dir is gitignored.
  - `linked_entity_id` field on `sponsors` rows is a bridge to
    UNIS.id when the same company also lives in the partner DB;
    UI exposure deferred (no current overlap is large enough to
    justify).

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

**Natural-key idempotent upsert** (contracts, followups, sponsors).
Each of these tables uses an `ON CONFLICT (id) DO UPDATE SET ...`
pattern with the id derived from a natural key (e.g. sponsors:
`sponsor-{year}-{slug}`). This makes bulk import scripts safely
re-runnable + means client-driven retries don't create duplicates.
Both SQLite ≥ 3.24 and Postgres support the same syntax — no
per-backend branch needed.

**Debounced + focus-restoring search inputs.** Standalone views
(Follow-ups, Sponsors) that re-render the entire view on every
input keystroke would destroy + recreate the `<input>`, losing
focus + Shift modifier state mid-keypress (capital letters land
wrong). Fix: 220 ms debounce + after-render focus restore +
caret-pinned-to-end. See `_followupsSearchInput` / `_sponsorsSearchInput`.

**Annual-event tables use composite natural keys.** Sponsors are
one row per (year, normalized_name) so the same company in 2025
+ 2026 lives in two rows. Year-over-year + renewal-cycle analytics
read off this naturally; no need for a "campaigns" join table.
Future events (Open Day, Innovation Summit) can reuse this pattern
by adding `event_name` discrimination — already in the schema.

---

## Intentional non-goals (don't auto-implement these)

| Idea | Why deferred | Trigger to revisit |
|---|---|---|
| Per-user passwords (Tier 2) | Shared password is honest for current trust level; multi-user identity already works via roster | Team grows past ~20 or external auditors require it |
| Google Workspace SSO (Tier 3) | Premature before platform is approved by H-FARM College IT | Platform officially adopted |
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

## Strategic backlog (parked options)

Captured during a strategy session after shipping Strategic Maps
Phase C (Three.js presentation mode). Defne wanted to revisit later
with fresh context.

### Open strategic question — pick a direction

The roadmap forks two ways. They're not mutually exclusive but they
emphasize different muscles:

- **Direction A — Decision intelligence platform.** Double down on
  what no CRM does well: portfolio thinking, trajectory analytics
  (snapshots already collect the data), counterfactual scenarios,
  AI advisory output. The cinematic presentation mode is in this
  spirit. Differentiation via insight, not coverage.

- **Direction B — Operational ease.** Close the CRM gaps so people
  actually use the tool daily without friction. Email integration is
  the headline gap (without it, outreach logging decays → data goes
  stale → A's analytics become garbage-in-garbage-out). Then
  follow-ups, digest, pre-meeting briefs.

**Honest take:** without some Direction B (specifically email
integration), the long-term Direction A vision rots. But A is what
makes the tool *special* vs Salesforce. Sequence: ship the
single highest-ROI B feature (follow-ups + email auto-log) first,
then go heavy on A.

### Parked features — ranked by strategic value

| Rank | Feature | Est. | Notes |
|---:|---|---:|---|
| ~~1~~ | ~~**Follow-ups** (full feature)~~ | ~~5-6 h~~ | ✅ **SHIPPED** (commit `038488e`). Standalone view + per-entity tab + Home briefing alarms + Undo. Now bootstraps Aspirational drag below. |
| 2 | **Aspirational drag** (depends on Follow-ups, now ready) | 7-8 h | **Next up.** Drag a sphere on the 2D strategic map to a desired position → modal opens with AI-generated action list to actually move the entity there. Saves as "aspiration goal" (not an override). Actions can be one-click added to Follow-ups. This is the **killer Direction-A feature** — no CRM does this. See risks below. |
| 3 | **Partner Health Trajectory** | 3-4 h | Sparkline next to every entity showing 30/60/90 day engagement_depth trend. Auto-alert when a Hot partner cools. Snapshot infra (`engagement_depth_snapshots` table) already collects the daily data — just needs a renderer + threshold detector. |
| 4 | **AI-generated weekly digest email** | 4-5 h | Every Monday, each roster member gets a personalized email: what changed, what's stuck, what to action. Snapshots + Gemini = doable. Solves "I have to remember to open the tool" problem. |
| 5 | **Pre-meeting briefing card** | 5-6 h | Calendar OAuth + AI brief = 30 seconds before any partner meeting, get a card with state, last 3 interactions, suggested agenda. PDF brief skeleton already exists. |
| 6 | **Email integration** (Gmail OAuth + auto-log) | 10-12 h | Standard CRM table stakes. Without it, manual outreach logging eventually decays. Strategic prerequisite for trajectory + digest accuracy. |
| 7 | **Inverse programme matchmaking** | 2-3 h | "Show me the 47 partners who fit Coding Academy" — score function already computes both directions, just needs a programme-centric view. Lives inside About H-FARM College (no new sidebar item). |
| 8 | **Cmd+K command palette** | 4-5 h | Type "Robert College" → enter → opens entity; type "MoU stuck" → contracts alarm. Solves navigation without bloating sidebar to 17 items. |
| 9 | **Decision audit trail** | 3-4 h | Every priority/tier override captures a free-text reasoning note + searchable later. Pairs naturally with aspirational drag (every aspiration is a decision). |
| 10 | **Counterfactual scenario planning** | 5-6 h | "If I sign these 3 partners in Q3, what does the forecast look like?" Extends the existing Monte Carlo in the Funnel view. |
| 11 | **Templates promote** (admin → sidebar) | 30 min | Pure surface-area move, not a new feature. Templates already exist in admin. |
| 12 | **Partner 360 deep-link + full-screen** | 1 h | Detail panel ↗ expand + URL deep-link (`?entity=ent-42`). Solves "Partner 360 isn't discoverable" without building a new view. The detail panel already IS Partner 360. |
| 13 | **Three.js cinematic drag** (depends on #2) | 3-4 h after #2 | Add raycaster + drag-plane projection to Present mode so the aspirational drag also works in the cinematic view. Mostly for board-room demos, not daily ops. |

### Things to NOT build (re-evaluated)

- **Partner 360 as separate sidebar view** — already exists as the
  slide-out detail panel. Discoverability problem, not feature gap.
- **Standalone Templates view** — already in admin. Promotion to
  sidebar is fine; building a "new" templates feature is wasted work.
- **Data Health as a separate page** — nobody opens "compliance
  dashboards". Surface the 2-3 worst data issues as a Home briefing
  strip instead.
- **Sidebar with 17 items** — current 12 is already at the upper
  bound for cognitive load. Use Cmd+K for navigation, not more nav.

### Risks to address before building Aspirational drag (#2)

1. **Action quality** — generic actions ("schedule a meeting") are
   worthless. Specific actions ("Anna Sokolova at TalTech, warm
   intro via Defne's LinkedIn 2nd degree") are gold. Requires the
   Gemini prompt to receive entity + contacts + KB + score components
   as context. Ask-the-platform chat has this infra; extend it.

2. **Wishful thinking trap** — users drag every partner to Quick Win,
   modal returns impossible 8-step list, tool becomes a toy.
   Mitigation: distance-based feasibility scoring. >2 quadrant jumps
   trigger a "this is a 3-year transformation" friction modal.

3. **Aspiration noise** — drag-and-forget creates orphaned goals.
   Mitigation: 90-day TTL with "still chasing?" reminder; auto-close
   when the partner actually reaches the aspired quadrant.

### Suggested next-session sequence

Follow-ups + Sponsors are done. The remaining killer-feature loop:

1. ~~**Follow-ups**~~ ✅ shipped (commit `038488e`) — system now has
   somewhere for tasks to land
2. **Aspirational drag** (7-8 h) — the killer feature. Drag a sphere
   on the 2D strategic map → AI generates 4-6 specific action items
   → one-click each into Follow-ups. Saves as an "aspiration goal"
   (not a position override) with 90-day TTL.
3. **Partner Health Trajectory** (3-4 h) — sparkline + velocity arrow
   per entity from the snapshot data we've been collecting since
   Phase A. Auto-alert when a Hot partner cools.

Together: ~10-12 hours / 2 focused sessions. Closes the end-to-end
"see current state → aspire to a better state → ship the actions →
watch the trajectory respond" loop. No CRM in the market has it.

### Adjacent feature shipped outside the backlog

**Sponsors** (commit `fc062c8`) — wasn't in the original backlog
because the Career Day workflow surfaced during a strategy chat,
not the original strategic planning session. Kept deliberately
*separate* from UNIS (different lifecycle, stakeholders, KPIs).
Year-keyed natural-key schema means 2026 + 2027 Career Days drop
in via the Import CSV button without any new code.

---

_Last meaningful update: extended after shipping Contracts (P1+P2),
3D strategic maps (Phases A-C incl. Three.js Present mode), PDF
brief generation, Follow-ups (full feature), and Sponsors (separate
annual pipeline + CSV ingest). Roughly tripled the codebase from
the original v1 snapshot. Edit when architecture or non-goals change,
not for every feature added._
