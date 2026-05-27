"""H-FARM College Global Partnerships Tracker — Flask backend.

Serves the static frontend (index.html + data/ + Logo/), proxies AI
drafting requests to Anthropic, and provides a thin REST layer backed
by SQLite for shared state (outreach log first; more to follow).

Run `python app.py` after installing requirements.txt and setting
ANTHROPIC_API_KEY in .env or environment. SQLite DB is auto-created at
`h-tracker.db` next to this file (override with HFARM_DB_PATH).
"""
import datetime as _dt
import json
import os
import re
import secrets
import sqlite3
import threading
from functools import wraps
from pathlib import Path

from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv
from flask import Flask, Response, g, jsonify, render_template, request, send_from_directory, session
from flask_cors import CORS

load_dotenv()

ROOT = Path(__file__).parent
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
DB_PATH = Path(os.environ.get("HFARM_DB_PATH", ROOT / "h-tracker.db"))

app = Flask(__name__, static_folder=str(ROOT), static_url_path="")

# ----------------------------------------------------------------------------
# Sessions & shared-password auth (Phase 5)
# ----------------------------------------------------------------------------
# HFARM_APP_PASSWORD — if set, every /api/* request (except health + auth)
#   requires a valid session cookie. If unset, auth is OFF (local dev default).
# HFARM_SECRET_KEY  — random hex string used to sign the session cookie.
#   MUST be set in production for sessions to survive restarts. If unset, we
#   generate an ephemeral one and log a warning (sessions reset on every boot).
# ----------------------------------------------------------------------------
APP_PASSWORD = os.environ.get("HFARM_APP_PASSWORD", "").strip()
_secret = os.environ.get("HFARM_SECRET_KEY", "").strip()
if not _secret:
    _secret = secrets.token_hex(32)
    if APP_PASSWORD:
        print(
            "⚠️  HFARM_SECRET_KEY not set — sessions will not survive restarts. "
            "Generate one with `python -c 'import secrets; print(secrets.token_hex(32))'` "
            "and set it via flyctl/Render dashboard."
        )
app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_NAME="hfarm_session",
    SESSION_COOKIE_HTTPONLY=True,
    # Cross-origin Vercel→Render needs SameSite=None + Secure.
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,
    PERMANENT_SESSION_LIFETIME=_dt.timedelta(days=30),
)

# CORS — the production frontend lives on Vercel while the API runs on
# Render, so the browser will make cross-origin requests for every /api/*.
# Whitelist deployed Vercel origins (override via HFARM_CORS_ORIGINS env var,
# comma-separated) and always allow localhost for dev. supports_credentials
# must be True so the session cookie travels with cross-origin requests.
_cors_default = (
    "https://h-tracker-blue.vercel.app,"
    "http://127.0.0.1:8000,"
    "http://localhost:8000"
)
_cors_origins = [o.strip() for o in os.environ.get("HFARM_CORS_ORIGINS", _cors_default).split(",") if o.strip()]
CORS(
    app,
    resources={r"/api/*": {"origins": _cors_origins}},
    allow_headers=["Content-Type", "X-Session-Id", "X-Display-Name"],
    expose_headers=["Content-Type"],
    supports_credentials=True,
    max_age=600,
)


@app.errorhandler(404)
def _json_404(_e):
    """Flask's default 404 is an HTML page, which makes the frontend's
    `await r.json()` blow up with a confusing 'Unexpected token <' error.
    For anything under /api/* (or any unmatched route) return JSON instead
    so the client can handle it gracefully."""
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found", "path": request.path}), 404
    return ("Not found", 404)


@app.errorhandler(500)
def _json_500(e):
    if request.path.startswith("/api/"):
        # Pull the original exception out of werkzeug's wrapper and include
        # its class + traceback in the response so the frontend can show
        # something more useful than "internal server error".
        import traceback as _tb
        orig = getattr(e, "original_exception", None) or e
        tb_str = "".join(_tb.format_exception(type(orig), orig, orig.__traceback__))
        # Always log the full traceback so it lands in Render logs.
        print(f"[500 on {request.method} {request.path}]\n{tb_str}")
        return jsonify({
            "error":      "internal server error",
            "detail":     str(orig),
            "exception_type": type(orig).__name__,
            # Last 8 lines of traceback are usually enough to debug without
            # leaking entire source paths.
            "traceback_tail": "\n".join(tb_str.splitlines()[-8:]),
            "path":       request.path,
        }), 500
    return ("Internal server error", 500)


def auth_required(fn):
    """Decorator that 401s unless the request has a valid session cookie.
    No-op when HFARM_APP_PASSWORD is unset (local dev default)."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if APP_PASSWORD and not session.get("authed"):
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


@app.route("/api/auth/status", methods=["GET"])
def auth_status():
    """Frontend probes this on boot to decide whether to show the login overlay."""
    return jsonify(
        {
            "auth_required": bool(APP_PASSWORD),
            "authed": (not APP_PASSWORD) or bool(session.get("authed")),
        }
    )


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    if not APP_PASSWORD:
        return jsonify({"ok": True, "auth_required": False})
    body = request.get_json(force=True, silent=True) or {}
    pw = (body.get("password") or "").strip()
    if not pw:
        return jsonify({"error": "password is required"}), 400
    # Constant-time compare — defends against timing side-channels even
    # though we're not exactly protecting nuclear codes here.
    if not secrets.compare_digest(pw, APP_PASSWORD):
        return jsonify({"error": "invalid password"}), 401
    session.permanent = True
    session["authed"] = True
    return jsonify({"ok": True})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
gemini_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

# Optional Google Sheets write-back. Set to the Apps Script Web App URL
# (looks like https://script.google.com/macros/s/AKfycb.../exec) and every
# editable-field change is mirrored to the source sheet. See SHEETS_SYNC.md.
WRITEBACK_URL = os.environ.get("HFARM_SHEETS_WRITEBACK_URL", "").strip()


# ============================================================================
# DATABASE — dual SQLite (local dev) / Postgres (production) support
# ----------------------------------------------------------------------------
# When DATABASE_URL is set (Render, Neon, any postgres://), psycopg connects
# to that. Otherwise we fall back to a local SQLite file. SQL stays mostly
# the same — we translate `?` placeholders to `%s` for Postgres and emit a
# small DDL difference for the auto-increment column. Everything else is
# identical, including INSERT...ON CONFLICT (supported in both).
# ============================================================================
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

try:
    import psycopg
    from psycopg.rows import dict_row as _pg_dict_row
    _HAS_PG = True
except ImportError:
    _HAS_PG = False

USE_PG = bool(DATABASE_URL) and _HAS_PG

_db_init_lock = threading.Lock()
_db_ready = False


def _q(sql):
    """Translate SQLite `?` placeholders to Postgres `%s`. No-op on SQLite."""
    return sql.replace("?", "%s") if USE_PG else sql


class _DB:
    """Thin connection wrapper so the rest of the code stays backend-agnostic.

    Both sqlite3.Connection and psycopg.Connection support `.execute()` that
    returns a Cursor with `.fetchall()` / `.fetchone()` / `.rowcount`. We only
    need to translate `?` placeholders before delegating.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        return self._conn.execute(_q(sql), params)

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()


def _connect():
    """Return a fresh connection to the configured backend, wrapped in _DB."""
    if USE_PG:
        # Neon sometimes hands out URLs with `postgres://`; psycopg only
        # accepts `postgresql://`. Normalise.
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        return _DB(psycopg.connect(url, row_factory=_pg_dict_row, autocommit=False))
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    # Better concurrency for an internal multi-user tool
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return _DB(conn)


_AUTOINC = "BIGSERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
_INT_DEFAULT_ZERO = "INTEGER NOT NULL DEFAULT 0"   # same syntax in both


def _ensure_schema():
    """Idempotent schema creation. Called lazily on first request."""
    global _db_ready
    if _db_ready:
        return
    with _db_init_lock:
        if _db_ready:
            return
        statements = [
            f"""CREATE TABLE IF NOT EXISTS outreach (
                    id          TEXT PRIMARY KEY,
                    entity_id   TEXT NOT NULL,
                    payload     TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    deleted     {_INT_DEFAULT_ZERO}
                )""",
            "CREATE INDEX IF NOT EXISTS idx_outreach_entity ON outreach(entity_id)",
            "CREATE INDEX IF NOT EXISTS idx_outreach_updated ON outreach(updated_at)",
            # Generic key/value bucket for simple per-entity overrides
            # (team assignments, kanban stage overrides, 2x2 positions, …).
            # One row per (namespace, key). Value is JSON-encoded.
            """CREATE TABLE IF NOT EXISTS kv_store (
                    namespace  TEXT NOT NULL,
                    key        TEXT NOT NULL,
                    value      TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (namespace, key)
                )""",
            "CREATE INDEX IF NOT EXISTS idx_kv_ns ON kv_store(namespace)",
            # Live sessions, refreshed via /api/presence/ping every ~30s.
            # Rows older than PRESENCE_TTL_SECONDS are considered offline.
            """CREATE TABLE IF NOT EXISTS presence (
                    session_id   TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    current_view TEXT,
                    last_seen    TEXT NOT NULL
                )""",
            "CREATE INDEX IF NOT EXISTS idx_presence_seen ON presence(last_seen)",
            # Append-only edit log so the "last edit by X, 12s ago" chip
            # can show what just changed. We could derive this from the
            # other tables' updated_at columns, but having one explicit
            # audit trail keeps the query trivial and survives deletes.
            f"""CREATE TABLE IF NOT EXISTS edit_log (
                    id           {_AUTOINC},
                    occurred_at  TEXT NOT NULL,
                    session_id   TEXT,
                    display_name TEXT,
                    resource     TEXT NOT NULL,
                    action       TEXT NOT NULL,
                    key          TEXT
                )""",
            "CREATE INDEX IF NOT EXISTS idx_edit_log_at ON edit_log(occurred_at)",
            # Partnership contracts — MoUs, NDAs, service agreements, etc.
            # One row per agreement. entity_id links to the partner entity
            # (matches UNIS array id from the CSV). attachments + programs
            # stored as JSON-encoded arrays for simplicity.
            """CREATE TABLE IF NOT EXISTS contracts (
                    id              TEXT PRIMARY KEY,
                    entity_id       TEXT NOT NULL,
                    type            TEXT,
                    status          TEXT,
                    signed_date     TEXT,
                    effective_date  TEXT,
                    expiry_date     TEXT,
                    annual_value_eur INTEGER,
                    term_months     INTEGER,
                    programs        TEXT,
                    hfarm_signer    TEXT,
                    partner_signer  TEXT,
                    notes           TEXT,
                    attachments     TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                )""",
            "CREATE INDEX IF NOT EXISTS idx_contracts_entity ON contracts(entity_id)",
            "CREATE INDEX IF NOT EXISTS idx_contracts_status ON contracts(status)",
            "CREATE INDEX IF NOT EXISTS idx_contracts_expiry ON contracts(expiry_date)",
            # Daily snapshots of each entity's position on every 2×2 map.
            # Fed by an opportunistic client trigger: first user of the day
            # POSTs the full list, idempotent via the composite PK so retries
            # within the same day are no-ops. 4-6 months of these rows back
            # the trajectory animation (Phase B).
            """CREATE TABLE IF NOT EXISTS entity_position_snapshots (
                    snapshot_date TEXT    NOT NULL,
                    entity_id     TEXT    NOT NULL,
                    axis_key      TEXT    NOT NULL,
                    x             REAL,
                    y             REAL,
                    z             REAL,
                    priority      TEXT,
                    PRIMARY KEY (snapshot_date, entity_id, axis_key)
                )""",
            "CREATE INDEX IF NOT EXISTS idx_snaps_date ON entity_position_snapshots(snapshot_date)",
            "CREATE INDEX IF NOT EXISTS idx_snaps_entity ON entity_position_snapshots(entity_id)",
            # Follow-ups — the prospective counterpart to the (retrospective)
            # outreach log. Each row = one concrete next step on one partner,
            # with a due date and an owner. Status flips between "open" and
            # "done"; done rows are kept for the activity timeline.
            """CREATE TABLE IF NOT EXISTS followups (
                    id            TEXT PRIMARY KEY,
                    entity_id     TEXT NOT NULL,
                    title         TEXT NOT NULL,
                    due_date      TEXT,
                    owner_handle  TEXT,
                    status        TEXT NOT NULL DEFAULT 'open',
                    notes         TEXT,
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL,
                    completed_at  TEXT,
                    created_by    TEXT
                )""",
            "CREATE INDEX IF NOT EXISTS idx_followups_entity ON followups(entity_id)",
            "CREATE INDEX IF NOT EXISTS idx_followups_status ON followups(status)",
            "CREATE INDEX IF NOT EXISTS idx_followups_due    ON followups(due_date)",
            "CREATE INDEX IF NOT EXISTS idx_followups_owner  ON followups(owner_handle)",
            # Career Day sponsors — separate from UNIS (partnership pipeline)
            # because the lifecycles are fundamentally different: sponsors are
            # annual transactional events, UNIS entities are multi-year
            # relationships. One row per (event_year, normalized_name) — a
            # company that sponsors 2025 + 2026 gets two rows so year-over-
            # year trajectory + renewal-cycle analytics are native.
            # `linked_entity_id` is an optional bridge to UNIS when the same
            # company also exists in the partner pipeline.
            """CREATE TABLE IF NOT EXISTS sponsors (
                    id                      TEXT PRIMARY KEY,
                    event_year              INTEGER NOT NULL,
                    event_name              TEXT NOT NULL DEFAULT 'Career Day',
                    company_name            TEXT NOT NULL,
                    normalized_name         TEXT NOT NULL,
                    industry_sector         TEXT,
                    sponsorship_tier        TEXT,
                    value_no_iva_eur        INTEGER,
                    value_with_iva_eur      INTEGER,
                    amount_paid_eur         INTEGER,
                    contract_signed_by_us   INTEGER DEFAULT 0,
                    contract_signed_by_them INTEGER DEFAULT 0,
                    invoice_no              TEXT,
                    invoice_date            TEXT,
                    payment_date            TEXT,
                    participation_days      TEXT,
                    attendee_count          INTEGER,
                    attendees               TEXT,
                    primary_contact_name    TEXT,
                    primary_contact_email   TEXT,
                    notes                   TEXT,
                    linked_entity_id        TEXT,
                    created_at              TEXT NOT NULL,
                    updated_at              TEXT NOT NULL,
                    created_by              TEXT
                )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sponsors_year_company ON sponsors(event_year, normalized_name)",
            "CREATE INDEX IF NOT EXISTS idx_sponsors_tier  ON sponsors(sponsorship_tier)",
            "CREATE INDEX IF NOT EXISTS idx_sponsors_year  ON sponsors(event_year)",
            "CREATE INDEX IF NOT EXISTS idx_sponsors_linked ON sponsors(linked_entity_id)",
            # ====================================================================
            # PROSPECT DISCOVERY — AI-surfaced partnership candidates.
            # The pipeline (UNIS) tracks what we know about; this is what we
            # SHOULD know about. Hallucination defense lives in app code, not
            # schema, but the `verification` JSON captures the result of each
            # check so the UI can show a confidence indicator.
            # ====================================================================
            """CREATE TABLE IF NOT EXISTS prospect_candidates (
                    id              TEXT PRIMARY KEY,
                    name            TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    type            TEXT,
                    country         TEXT,
                    region          TEXT,
                    primary_url     TEXT,
                    description     TEXT,
                    ai_reasoning    TEXT,
                    ai_fit_score    INTEGER,
                    source_urls     TEXT,
                    suggested_programs TEXT,
                    verification    TEXT,
                    discovered_at   TEXT NOT NULL,
                    discovered_via  TEXT,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    approved_entity_id TEXT,
                    search_run_id   TEXT
                )""",
            "CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospect_candidates(status)",
            "CREATE INDEX IF NOT EXISTS idx_prospects_norm   ON prospect_candidates(normalized_name)",
            "CREATE INDEX IF NOT EXISTS idx_prospects_run    ON prospect_candidates(search_run_id)",
            # Decisions feed the learning loop (yes / no / maybe + reason)
            """CREATE TABLE IF NOT EXISTS prospect_decisions (
                    id              TEXT PRIMARY KEY,
                    candidate_id    TEXT NOT NULL,
                    decision        TEXT NOT NULL,
                    reason          TEXT,
                    decided_at      TEXT NOT NULL,
                    decided_by      TEXT
                )""",
            "CREATE INDEX IF NOT EXISTS idx_decisions_candidate ON prospect_decisions(candidate_id)",
            # Audit + cost tracking per search run
            """CREATE TABLE IF NOT EXISTS prospect_search_runs (
                    id              TEXT PRIMARY KEY,
                    trigger_kind    TEXT NOT NULL,
                    query           TEXT,
                    criteria        TEXT,
                    raw_count       INTEGER,
                    filtered_count  INTEGER,
                    surfaced_count  INTEGER,
                    cost_estimate   REAL,
                    notes           TEXT,
                    run_at          TEXT NOT NULL,
                    run_by          TEXT
                )""",
            # Distilled user preference profile (rebuilt periodically from decisions)
            """CREATE TABLE IF NOT EXISTS prospect_user_profile (
                    user_handle     TEXT PRIMARY KEY,
                    profile_json    TEXT NOT NULL,
                    distilled_at    TEXT NOT NULL,
                    decision_count  INTEGER
                )""",
            # ====================================================================
            # ASPIRATION GOALS — "where I want this partner to be."
            # An operator drags a sphere on a 2×2 strategic map to a desired
            # position; we record current vs. target + the quadrant jump,
            # generate a Gemini-backed action plan, and watch for the entity
            # to actually arrive. Status flips automatically when the entity's
            # computed position reaches the target quadrant ("achieved") or
            # the 90-day TTL elapses ("expired"). Operators can manually
            # abandon a goal.
            #
            # This is fundamentally different from map2x2_override (kv_store):
            #   - override   = "the algorithm got the CURRENT position wrong"
            #   - aspiration = "I want this partner to MOVE to a new position"
            # They store on different axes of the same map without conflict.
            # ====================================================================
            """CREATE TABLE IF NOT EXISTS aspiration_goals (
                    id              TEXT PRIMARY KEY,
                    entity_id       TEXT NOT NULL,
                    axis_key        TEXT NOT NULL,
                    current_x       INTEGER NOT NULL,
                    current_y       INTEGER NOT NULL,
                    target_x        INTEGER NOT NULL,
                    target_y        INTEGER NOT NULL,
                    source_quadrant TEXT NOT NULL,
                    target_quadrant TEXT NOT NULL,
                    quadrant_jump   INTEGER NOT NULL,
                    feasibility     TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'active',
                    actions_json    TEXT,
                    note            TEXT,
                    linked_followup_ids TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL,
                    expires_at      TEXT NOT NULL,
                    achieved_at     TEXT,
                    abandoned_at    TEXT,
                    created_by      TEXT
                )""",
            "CREATE INDEX IF NOT EXISTS idx_aspirations_entity ON aspiration_goals(entity_id)",
            "CREATE INDEX IF NOT EXISTS idx_aspirations_status ON aspiration_goals(status)",
            "CREATE INDEX IF NOT EXISTS idx_aspirations_expires ON aspiration_goals(expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_aspirations_axis ON aspiration_goals(axis_key)",
        ]
        conn = _connect()
        try:
            cur = conn.cursor()
            for stmt in statements:
                cur.execute(stmt)
            conn.commit()
        finally:
            conn.close()
        _db_ready = True


def get_db():
    """Per-request connection; auto-closed in teardown."""
    _ensure_schema()
    if "db" not in g:
        g.db = _connect()
    return g.db


@app.teardown_appcontext
def _close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _now_iso():
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


PRESENCE_TTL_SECONDS = 90


def _record_edit(resource, action, key=None):
    """Best-effort audit-log write. Reads display_name from request headers
    (set by the frontend's fetch wrapper) so we don't have to plumb auth."""
    try:
        db = get_db()
        db.execute(
            """INSERT INTO edit_log
                   (occurred_at, session_id, display_name, resource, action, key)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                _now_iso(),
                request.headers.get("X-Session-Id", ""),
                request.headers.get("X-Display-Name", ""),
                resource,
                action,
                key,
            ),
        )
        db.commit()
    except Exception as e:  # noqa: BLE001
        # Audit failures must never break the underlying mutation
        print(f"[edit_log] write failed: {e}")


# ---------- Static file serving ----------
@app.route("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.route("/<path:p>")
def static_path(p):
    full = ROOT / p
    if full.is_dir() or not full.exists():
        return ("Not found", 404)
    return send_from_directory(ROOT, p)


# ---------- AI: draft outreach email ----------
SYSTEM_PROMPT = """You are a senior partnership manager at H-FARM College — an innovation campus near Treviso, Italy. You write warm, personalised outreach emails to potential academic partners (universities, agencies, schools, student organisations).

Voice rules:
- Professional but human. No salesy clichés. No "I hope this email finds you well." or "I wanted to reach out."
- Direct, specific, useful. Acknowledge something concrete about the partner (their focus, ranking, location, recent activity).
- 120-180 words for the body. Tight, scannable paragraphs.

Structure of the body:
1. Open with a specific, partner-relevant observation.
2. Propose the recommended H-FARM College offering that fits THEM (use the supplied programme/format).
3. Single clear call-to-action: a 20-min intro call with two concrete time options, or a specific next artefact.
4. Brief signature line with the sender team's role at H-FARM College.

If you do not have the recipient's first name, use "Dear [First Name]" as a placeholder. If you have title + last name, use "Dear Dr [Last Name]" or similar.

Subject line: short (under 70 chars), specific, no clickbait, no emoji.

Return ONLY a JSON object with two fields, no markdown fence, no commentary:
{"subject": "...", "body": "..."}"""


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify(
        {
            "ok": True,
            "model": MODEL,
            "key_set": bool(GEMINI_KEY),
            "provider": "gemini",
            "db_backend": "postgres" if USE_PG else "sqlite",
            "db": "neon/render" if USE_PG else str(DB_PATH.name),
            "auth_required": bool(APP_PASSWORD),
            "sheets_writeback": bool(WRITEBACK_URL),
        }
    )


# ============================================================================
# OUTREACH LOG — shared state for cross-browser sync
# ----------------------------------------------------------------------------
# Frontend uses the entry shape:
#   { id, entityId, date, channel, channels, status, subject, body,
#     recipientName, recipientEmail, senderTeamId, programs }
# We store the full entry as a JSON blob keyed by entry id, plus an
# updated_at timestamp for future "since=" delta polling.
# ============================================================================
@app.route("/api/state/outreach", methods=["GET"])
@auth_required
def outreach_list():
    """Return all entries grouped by entity_id — matches localStorage shape."""
    db = get_db()
    rows = db.execute(
        "SELECT entity_id, payload FROM outreach WHERE deleted = 0"
    ).fetchall()
    grouped = {}
    for r in rows:
        try:
            entry = json.loads(r["payload"])
        except json.JSONDecodeError:
            continue
        grouped.setdefault(r["entity_id"], []).append(entry)
    return jsonify(grouped)


@app.route("/api/state/outreach/<entry_id>", methods=["PUT"])
@auth_required
def outreach_upsert(entry_id):
    """Insert or update a single entry. Body is the full entry JSON."""
    entry = request.get_json(force=True) or {}
    if not isinstance(entry, dict):
        return jsonify({"error": "Body must be a JSON object."}), 400
    if entry.get("id") != entry_id:
        return jsonify({"error": "URL id and body id must match."}), 400
    entity_id = entry.get("entityId")
    if not entity_id:
        return jsonify({"error": "entityId is required."}), 400

    db = get_db()
    db.execute(
        """INSERT INTO outreach (id, entity_id, payload, updated_at, deleted)
                VALUES (?, ?, ?, ?, 0)
           ON CONFLICT(id) DO UPDATE SET
                entity_id  = excluded.entity_id,
                payload    = excluded.payload,
                updated_at = excluded.updated_at,
                deleted    = 0""",
        (entry_id, entity_id, json.dumps(entry), _now_iso()),
    )
    db.commit()
    _record_edit("outreach", "upsert", entry_id)
    return jsonify({"ok": True, "id": entry_id})


@app.route("/api/state/outreach/<entry_id>", methods=["DELETE"])
@auth_required
def outreach_delete(entry_id):
    """Soft-delete (keeps row so future sync logic can detect tombstones)."""
    db = get_db()
    cur = db.execute(
        "UPDATE outreach SET deleted = 1, updated_at = ? WHERE id = ?",
        (_now_iso(), entry_id),
    )
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "Not found."}), 404
    _record_edit("outreach", "delete", entry_id)
    return jsonify({"ok": True, "id": entry_id})


# ============================================================================
# GENERIC KEY/VALUE — shared per-entity overrides
# ----------------------------------------------------------------------------
# Namespaces in use:
#   team_assignment   — key=entity_id, value="team_id" (string)
#   stage_override    — key=entity_id, value="stage_id" (string)
#   map2x2_override   — key="axisKey::entity_id", value={x,y} (object)
#
# Allowed namespaces are whitelisted to avoid accidental sprawl. Add new
# namespaces here when Phase 3+ introduces new shared state buckets.
# ============================================================================
_KV_NAMESPACES = {"team_assignment", "stage_override", "map2x2_override", "kb_draft", "entity_override", "sponsor_packages"}


def _check_ns(ns):
    if ns not in _KV_NAMESPACES:
        return jsonify({"error": f"Unknown namespace '{ns}'."}), 404
    return None


@app.route("/api/state/kv/<ns>", methods=["GET"])
@auth_required
def kv_list(ns):
    """Return all { key: value } pairs in a namespace."""
    err = _check_ns(ns)
    if err:
        return err
    db = get_db()
    rows = db.execute(
        "SELECT key, value FROM kv_store WHERE namespace = ?", (ns,)
    ).fetchall()
    out = {}
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except json.JSONDecodeError:
            continue
    return jsonify(out)


@app.route("/api/state/kv/<ns>/<path:key>", methods=["PUT"])
@auth_required
def kv_put(ns, key):
    """Upsert a single key. Body is the value (any JSON shape)."""
    err = _check_ns(ns)
    if err:
        return err
    body = request.get_json(force=True, silent=True)
    if body is None:
        return jsonify({"error": "Body must be valid JSON."}), 400
    db = get_db()
    db.execute(
        """INSERT INTO kv_store (namespace, key, value, updated_at)
                VALUES (?, ?, ?, ?)
           ON CONFLICT(namespace, key) DO UPDATE SET
                value      = excluded.value,
                updated_at = excluded.updated_at""",
        (ns, key, json.dumps(body), _now_iso()),
    )
    db.commit()
    _record_edit(f"kv:{ns}", "upsert", key)
    return jsonify({"ok": True, "namespace": ns, "key": key})


@app.route("/api/state/kv/<ns>/<path:key>", methods=["DELETE"])
@auth_required
def kv_delete(ns, key):
    """Hard delete — these are mutable overrides, no audit trail needed."""
    err = _check_ns(ns)
    if err:
        return err
    db = get_db()
    cur = db.execute(
        "DELETE FROM kv_store WHERE namespace = ? AND key = ?", (ns, key)
    )
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"ok": True, "noop": True})  # idempotent
    _record_edit(f"kv:{ns}", "delete", key)
    return jsonify({"ok": True})


@app.route("/api/state/kv/<ns>/_import", methods=["POST"])
@auth_required
def kv_import(ns):
    """One-shot migration from a client's localStorage.

    Body shape: { key: value, ... }. Inserts only keys not already present
    (server wins on conflict), so concurrent migrations are safe.
    """
    err = _check_ns(ns)
    if err:
        return err
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Body must be a JSON object."}), 400

    db = get_db()
    existing = {
        r["key"] for r in db.execute(
            "SELECT key FROM kv_store WHERE namespace = ?", (ns,)
        ).fetchall()
    }
    now = _now_iso()
    inserted = 0
    skipped = 0
    for key, value in payload.items():
        if key in existing:
            skipped += 1
            continue
        db.execute(
            """INSERT INTO kv_store (namespace, key, value, updated_at)
                    VALUES (?, ?, ?, ?)""",
            (ns, key, json.dumps(value), now),
        )
        existing.add(key)
        inserted += 1
    db.commit()
    return jsonify({"ok": True, "inserted": inserted, "skipped": skipped})


@app.route("/api/state/outreach/import", methods=["POST"])
@auth_required
def outreach_import():
    """One-shot migration from a client's localStorage.

    Body shape (same as GET): { entity_id: [entry, ...] }.
    Inserts only entries whose id doesn't already exist — never overwrites,
    so refreshing or two clients migrating concurrently is safe.
    """
    payload = request.get_json(force=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Body must be a JSON object."}), 400

    db = get_db()
    existing = {r["id"] for r in db.execute("SELECT id FROM outreach").fetchall()}
    inserted = 0
    skipped = 0
    now = _now_iso()
    for entity_id, entries in payload.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            eid = entry.get("id")
            if not eid or eid in existing:
                skipped += 1
                continue
            entry.setdefault("entityId", entity_id)
            db.execute(
                """INSERT INTO outreach
                       (id, entity_id, payload, updated_at, deleted)
                   VALUES (?, ?, ?, ?, 0)""",
                (eid, entity_id, json.dumps(entry), now),
            )
            existing.add(eid)
            inserted += 1
    db.commit()
    _record_edit("outreach", "import")
    return jsonify({"ok": True, "inserted": inserted, "skipped": skipped})


# ============================================================================
# PRESENCE — who's online right now + most recent shared-state edits
# ----------------------------------------------------------------------------
# The frontend posts a heartbeat every ~30s. Rows older than
# PRESENCE_TTL_SECONDS are excluded from the live list (and lazily cleaned
# up on each query). No auth: display_name is self-claimed.
# ============================================================================
@app.route("/api/presence/ping", methods=["POST"])
@auth_required
def presence_ping():
    body = request.get_json(force=True, silent=True) or {}
    sid = (body.get("session_id") or "").strip()
    name = (body.get("display_name") or "").strip() or "Anonymous"
    view = (body.get("current_view") or "").strip() or None
    if not sid:
        return jsonify({"error": "session_id is required."}), 400

    db = get_db()
    db.execute(
        """INSERT INTO presence (session_id, display_name, current_view, last_seen)
                VALUES (?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
                display_name = excluded.display_name,
                current_view = excluded.current_view,
                last_seen    = excluded.last_seen""",
        (sid, name, view, _now_iso()),
    )
    # Lazy cleanup of stale rows
    cutoff = (
        _dt.datetime.utcnow() - _dt.timedelta(seconds=PRESENCE_TTL_SECONDS * 4)
    ).replace(microsecond=0).isoformat() + "Z"
    db.execute("DELETE FROM presence WHERE last_seen < ?", (cutoff,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/presence", methods=["GET"])
@auth_required
def presence_list():
    """Active sessions in the last PRESENCE_TTL_SECONDS."""
    db = get_db()
    cutoff = (
        _dt.datetime.utcnow() - _dt.timedelta(seconds=PRESENCE_TTL_SECONDS)
    ).replace(microsecond=0).isoformat() + "Z"
    rows = db.execute(
        """SELECT session_id, display_name, current_view, last_seen
             FROM presence
            WHERE last_seen >= ?
            ORDER BY last_seen DESC""",
        (cutoff,),
    ).fetchall()
    return jsonify(
        {
            "now": _now_iso(),
            "ttl_seconds": PRESENCE_TTL_SECONDS,
            "sessions": [dict(r) for r in rows],
        }
    )


@app.route("/api/presence/count", methods=["GET"])
def presence_count_public():
    """Public — returns just the number of currently-online operators
    (no names, no IDs, no display info — just an integer). Used by the
    pre-auth landing page so it can show a real live count instead of a
    hardcoded "5 operators online" placeholder. Safe to expose because
    it's an aggregate with no PII."""
    db = get_db()
    cutoff = (
        _dt.datetime.utcnow() - _dt.timedelta(seconds=PRESENCE_TTL_SECONDS)
    ).replace(microsecond=0).isoformat() + "Z"
    row = db.execute(
        "SELECT COUNT(*) AS n FROM presence WHERE last_seen >= ?",
        (cutoff,),
    ).fetchone()
    return jsonify({
        "count":       (row["n"] if row else 0) or 0,
        "ttl_seconds": PRESENCE_TTL_SECONDS,
        "now":         _now_iso(),
    })


@app.route("/api/edits/recent", methods=["GET"])
@auth_required
def edits_recent():
    """Most recent N edits across all shared-state buckets."""
    try:
        limit = max(1, min(100, int(request.args.get("limit", 10))))
    except ValueError:
        limit = 10
    db = get_db()
    rows = db.execute(
        """SELECT occurred_at, session_id, display_name, resource, action, key
             FROM edit_log
            ORDER BY id DESC
            LIMIT ?""",
        (limit,),
    ).fetchall()
    return jsonify({"now": _now_iso(), "edits": [dict(r) for r in rows]})


@app.route("/api/edits/entity/<entity_id>", methods=["GET"])
@auth_required
def edits_for_entity(entity_id):
    """Timeline of every recorded change touching a single entity.

    Aggregates rows where the edit's `key` directly identifies this entity
    (team / stage / entity-field overrides) or contains it as a suffix
    (2×2 position uses composite `axis::entity_id` keys), plus outreach
    upserts whose ID belongs to this entity. Sorted newest first.
    """
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50

    db = get_db()
    # `?` placeholders translate to `%s` for Postgres via _q() in the wrapper.
    rows = db.execute(
        """SELECT occurred_at, session_id, display_name, resource, action, key
             FROM edit_log
            WHERE (
                  resource IN ('kv:team_assignment', 'kv:stage_override', 'kv:entity_override')
                  AND key = ?
              )
               OR (
                  resource = 'kv:map2x2_override'
                  AND key LIKE '%::' || ?
              )
               OR (
                  resource = 'outreach'
                  AND key IN (SELECT id FROM outreach WHERE entity_id = ?)
              )
               OR (
                  resource = 'contract'
                  AND key IN (SELECT id FROM contracts WHERE entity_id = ?)
              )
            ORDER BY id DESC
            LIMIT ?""",
        (entity_id, entity_id, entity_id, entity_id, limit),
    ).fetchall()
    return jsonify(
        {
            "now": _now_iso(),
            "entity_id": entity_id,
            "edits": [dict(r) for r in rows],
        }
    )


@app.route("/api/edits/contract/<contract_id>", methods=["GET"])
@auth_required
def edits_for_contract(contract_id):
    """Activity timeline for a single contract — every upsert/delete touching it."""
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    db = get_db()
    rows = db.execute(
        """SELECT occurred_at, session_id, display_name, resource, action, key
             FROM edit_log
            WHERE resource = 'contract' AND key = ?
            ORDER BY id DESC
            LIMIT ?""",
        (contract_id, limit),
    ).fetchall()
    return jsonify(
        {
            "now": _now_iso(),
            "contract_id": contract_id,
            "edits": [dict(r) for r in rows],
        }
    )


@app.route("/api/draft-outreach", methods=["POST"])
@auth_required
def draft_outreach():
    if not GEMINI_KEY:
        return (
            jsonify(
                {
                    "error": "GEMINI_API_KEY is not set on the server. Add it to .env and restart Flask."
                }
            ),
            500,
        )

    data = request.get_json(force=True) or {}
    user_msg = _build_prompt(data)

    try:
        resp = gemini_client.models.generate_content(
            model=MODEL,
            contents=user_msg,
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                max_output_tokens=1024,
                temperature=0.7,
                # Force JSON so we don't have to defensively strip markdown
                response_mime_type="application/json",
                # Disable hidden "thinking" — for structured short outputs
                # the reasoning budget just eats max_output_tokens with no
                # quality gain. Keeps responses fast and predictable.
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            ),
        )
    except Exception as e:  # noqa: BLE001 — Gemini SDK surfaces several exception types
        return jsonify({"error": f"Gemini API error: {e}"}), 502

    text = (resp.text or "").strip()
    parsed = _parse_email_json(text)
    usage_md = getattr(resp, "usage_metadata", None)
    return jsonify(
        {
            "subject": parsed.get("subject", ""),
            "body": parsed.get("body", ""),
            "model": MODEL,
            "usage": {
                "input_tokens":  getattr(usage_md, "prompt_token_count",     0) if usage_md else 0,
                "output_tokens": getattr(usage_md, "candidates_token_count", 0) if usage_md else 0,
            },
            # Only echo the raw response when parsing failed, so the
            # frontend can show a debug fallback instead of a blank form.
            "raw": text if not parsed else None,
        }
    )


def _build_prompt(d):
    entity = d.get("entity") or {}
    kb = d.get("kb") or {}
    recommended = d.get("recommended") or {}
    team = d.get("team") or {}
    recipient = d.get("recipient") or {}
    sender = d.get("sender") or {}

    lines = ["## Partner"]
    lines.append(f"- Name: {entity.get('name', 'Unknown')}")
    lines.append(
        f"- Type: {entity.get('type_label', entity.get('type', 'university'))}"
    )
    if entity.get("country"):
        lines.append(f"- Country: {entity['country']}")
    if entity.get("focus_areas"):
        lines.append(f"- Focus / curriculum: {entity['focus_areas']}")
    if entity.get("strategic_tier"):
        lines.append(f"- Strategic tier: {entity['strategic_tier']}")
    if entity.get("priority"):
        lines.append(f"- Current pipeline priority: {entity['priority']}")
    if entity.get("partnership_readiness"):
        lines.append(f"- Readiness band: {entity['partnership_readiness']}")
    if entity.get("notes"):
        lines.append(f"- Internal notes: {entity['notes']}")

    lines += ["", "## Recommended H-FARM College offering to propose"]
    if recommended.get("name"):
        lines.append(f"- Name: {recommended['name']}")
        if recommended.get("topic"):
            lines.append(f"  Topic: {recommended['topic']}")
        if recommended.get("duration"):
            lines.append(f"  Duration: {recommended['duration']}")
        if recommended.get("group"):
            lines.append(f"  Group size: {recommended['group']}")
        if recommended.get("ideal"):
            lines.append(f"  Why a fit: {recommended['ideal']}")
        if recommended.get("case"):
            lines.append(f"  Reference case: {recommended['case']}")

    lines += ["", "## Sender (H-FARM College team)"]
    if team.get("name"):
        lines.append(f"- Team: {team['name']}")
    if team.get("remit"):
        lines.append(f"- Remit: {team['remit']}")
    # Per-user signer info from the roster — if set, the AI knows whose
    # name + email to use in the signature instead of a generic placeholder.
    if sender.get("name"):
        lines.append(f"- Signer name: {sender['name']}")
    if sender.get("role"):
        lines.append(f"- Signer role: {sender['role']}")
    if sender.get("email"):
        lines.append(f"- Signer email: {sender['email']}")

    lines += ["", "## Recipient"]
    if recipient.get("name"):
        lines.append(f"- Name: {recipient['name']}")
    if recipient.get("role"):
        lines.append(f"- Role: {recipient['role']}")
    if recipient.get("email"):
        lines.append(f"- Email: {recipient['email']}")

    lines += ["", "## H-FARM College context"]
    if kb.get("org_name"):
        lines.append(f"- {kb['org_name']} — {kb.get('location', 'Italy')}")
    if kb.get("website"):
        lines.append(f"- Website: {kb['website']}")

    lines += ["", "Draft the email now. Return JSON only — no markdown fence."]
    return "\n".join(lines)


def _parse_email_json(text):
    """Tolerant JSON extractor — strips code fences, falls back to regex."""
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


# ============================================================================
# SHEETS WRITE-BACK — proxy edits through to a Google Sheets Apps Script
# ----------------------------------------------------------------------------
# The Apps Script lives on the user's Sheet (see SHEETS_SYNC.md for the
# paste-in code + deploy steps). We just POST {entity_id, field, value}
# to its public Web App URL. Apps Script finds the row by `id` column and
# writes the cell. If HFARM_SHEETS_WRITEBACK_URL isn't set, the endpoint
# returns configured:false so the frontend can stay quiet.
# ============================================================================
import urllib.request as _urlreq
import urllib.error as _urlerr


@app.route("/api/sheets/writeback", methods=["POST"])
@auth_required
def sheets_writeback():
    if not WRITEBACK_URL:
        return jsonify({
            "ok": False,
            "configured": False,
            "error": "Sheets write-back not configured. Set HFARM_SHEETS_WRITEBACK_URL on the server (see SHEETS_SYNC.md).",
        }), 200

    body = request.get_json(force=True, silent=True) or {}
    entity_id = (body.get("entity_id") or "").strip()
    field = (body.get("field") or "").strip()
    if not entity_id or not field:
        return jsonify({"ok": False, "configured": True, "error": "entity_id and field are required"}), 400

    try:
        req = _urlreq.Request(
            WRITEBACK_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _urlreq.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        try:
            inner = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return jsonify({
                "ok": False, "configured": True,
                "error": "Apps Script returned non-JSON: " + raw[:200].decode("utf-8", errors="replace"),
            }), 502
        # Apps Script wraps its own ok/error — surface it as-is
        return jsonify({
            "ok": bool(inner.get("ok")),
            "configured": True,
            "apps_script": inner,
        }), (200 if inner.get("ok") else 502)
    except _urlerr.URLError as e:
        return jsonify({"ok": False, "configured": True, "error": "Network error: " + str(e)}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "configured": True, "error": "Unexpected: " + str(e)}), 500


# ============================================================================
# CONTRACTS — MoUs, NDAs, service agreements
# ----------------------------------------------------------------------------
# CRUD over the `contracts` table. Each row is one agreement attached to an
# entity (university/agency/etc.). Fields are mostly self-explanatory;
# `programs` and `attachments` are JSON-encoded arrays so the schema stays
# flat (Phase 2 may break attachments out to a separate table when file
# uploads land).
# ============================================================================
_CONTRACT_FIELDS = (
    "id", "entity_id", "type", "status",
    "signed_date", "effective_date", "expiry_date",
    "annual_value_eur", "term_months",
    "programs", "hfarm_signer", "partner_signer",
    "notes", "attachments",
    "created_at", "updated_at",
)


def _contract_row_to_dict(row):
    """Convert a sqlite3.Row / psycopg dict_row into a clean dict, parsing
    the JSON-encoded list columns. Defensive against malformed JSON."""
    out = {k: row[k] for k in _CONTRACT_FIELDS if k in row.keys()} if hasattr(row, "keys") else dict(row)
    for key in ("programs", "attachments"):
        raw = out.get(key)
        if raw:
            try:
                out[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                out[key] = []
        else:
            out[key] = []
    return out


@app.route("/api/contracts", methods=["GET"])
@auth_required
def contracts_list():
    """Return all contracts, sorted newest-edit first."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM contracts ORDER BY updated_at DESC"
    ).fetchall()
    return jsonify({"contracts": [_contract_row_to_dict(r) for r in rows]})


@app.route("/api/contracts/<contract_id>", methods=["GET"])
@auth_required
def contracts_get(contract_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM contracts WHERE id = ?", (contract_id,)
    ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(_contract_row_to_dict(row))


@app.route("/api/contracts/<contract_id>", methods=["PUT"])
@auth_required
def contracts_upsert(contract_id):
    """Insert or update a single contract. Body is the full record."""
    body = request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Body must be a JSON object."}), 400
    if body.get("id") != contract_id:
        return jsonify({"error": "URL id and body id must match."}), 400
    entity_id = (body.get("entity_id") or "").strip()
    if not entity_id:
        return jsonify({"error": "entity_id is required."}), 400

    # Serialise list fields. Coerce empty/null sensibly.
    programs = body.get("programs") or []
    attachments = body.get("attachments") or []
    if not isinstance(programs, list):    programs = []
    if not isinstance(attachments, list): attachments = []

    now = _now_iso()
    db = get_db()
    # Preserve created_at on update; only set on insert
    existing = db.execute(
        "SELECT created_at FROM contracts WHERE id = ?", (contract_id,)
    ).fetchone()
    created_at = (existing["created_at"] if existing else None) or now

    db.execute(
        """INSERT INTO contracts (
                id, entity_id, type, status,
                signed_date, effective_date, expiry_date,
                annual_value_eur, term_months,
                programs, hfarm_signer, partner_signer,
                notes, attachments,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
                entity_id        = excluded.entity_id,
                type             = excluded.type,
                status           = excluded.status,
                signed_date      = excluded.signed_date,
                effective_date   = excluded.effective_date,
                expiry_date      = excluded.expiry_date,
                annual_value_eur = excluded.annual_value_eur,
                term_months      = excluded.term_months,
                programs         = excluded.programs,
                hfarm_signer     = excluded.hfarm_signer,
                partner_signer   = excluded.partner_signer,
                notes            = excluded.notes,
                attachments      = excluded.attachments,
                updated_at       = excluded.updated_at""",
        (
            contract_id, entity_id,
            (body.get("type") or "").strip() or None,
            (body.get("status") or "").strip() or None,
            (body.get("signed_date") or "").strip() or None,
            (body.get("effective_date") or "").strip() or None,
            (body.get("expiry_date") or "").strip() or None,
            int(body["annual_value_eur"]) if str(body.get("annual_value_eur") or "").strip() else None,
            int(body["term_months"]) if str(body.get("term_months") or "").strip() else None,
            json.dumps(programs),
            (body.get("hfarm_signer") or "").strip() or None,
            (body.get("partner_signer") or "").strip() or None,
            (body.get("notes") or ""),
            json.dumps(attachments),
            created_at, now,
        ),
    )
    db.commit()
    _record_edit("contract", "upsert", contract_id)
    return jsonify({"ok": True, "id": contract_id})


@app.route("/api/contracts/<contract_id>", methods=["DELETE"])
@auth_required
def contracts_delete(contract_id):
    db = get_db()
    cur = db.execute("DELETE FROM contracts WHERE id = ?", (contract_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    _record_edit("contract", "delete", contract_id)
    return jsonify({"ok": True})


# ============================================================================
# ENTITY POSITION SNAPSHOTS — daily snapshots of every entity's coordinates
# on every 2×2 strategic map (x, y, z). Two endpoints:
#
#   GET  /api/snapshots/latest-date  → which day was most recently captured?
#   POST /api/snapshots/take         → bulk-insert today's positions; client
#                                       computes the coords (it owns the
#                                       formula) and POSTs the rows
#
# The opportunistic-trigger pattern: first user to load the app each day
# checks latest-date; if it's not today, fires take. Idempotent via the
# composite PK so concurrent triggers within the same day collapse to one
# logical write.
# ============================================================================
@app.route("/api/snapshots/latest-date", methods=["GET"])
@auth_required
def snapshots_latest_date():
    db = get_db()
    row = db.execute(
        "SELECT snapshot_date FROM entity_position_snapshots ORDER BY snapshot_date DESC LIMIT 1"
    ).fetchone()
    return jsonify({
        "latest_date": (dict(row).get("snapshot_date") if row else None),
        "today": _dt.datetime.utcnow().strftime("%Y-%m-%d"),
    })


@app.route("/api/snapshots/take", methods=["POST"])
@auth_required
def snapshots_take():
    """Bulk-insert today's position rows.

    Body shape:
        { "rows": [
            {"entity_id": "...", "axis_key": "effortFit", "x": 72.0,
             "y": 41.0, "z": 18.0, "priority": "Hot"},
            ...
        ]}

    Uses INSERT OR IGNORE so a same-day retry is silently absorbed by the PK.
    On Postgres we emulate via ON CONFLICT DO NOTHING (the _q() wrapper
    rewrites placeholders but not the syntax — keep both branches).
    """
    body = request.get_json(force=True, silent=True) or {}
    rows = body.get("rows") or []
    if not isinstance(rows, list):
        return jsonify({"error": "rows must be a list"}), 400
    today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
    db = get_db()
    inserted = 0
    # SQLite ≥3.24 and Postgres both accept the same ON CONFLICT DO NOTHING
    # syntax — no per-backend branch needed. The _q() wrapper handles ?→%s
    # placeholder translation for Postgres.
    insert_sql = (
        "INSERT INTO entity_position_snapshots "
        "(snapshot_date, entity_id, axis_key, x, y, z, priority) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (snapshot_date, entity_id, axis_key) DO NOTHING"
    )
    for r in rows:
        try:
            cur = db.execute(insert_sql, (
                today,
                str(r.get("entity_id") or "").strip(),
                str(r.get("axis_key") or "").strip(),
                float(r["x"]) if r.get("x") is not None else None,
                float(r["y"]) if r.get("y") is not None else None,
                float(r["z"]) if r.get("z") is not None else None,
                (r.get("priority") or None),
            ))
            if cur.rowcount:
                inserted += 1
        except Exception as e:
            # Skip malformed rows; don't fail the whole batch
            print(f"[snapshots] skip row {r}: {e}")
            continue
    db.commit()
    return jsonify({
        "ok": True,
        "snapshot_date": today,
        "rows_received": len(rows),
        "rows_inserted": inserted,
    })


@app.route("/api/snapshots/trajectory/<entity_id>", methods=["GET"])
@auth_required
def snapshots_trajectory(entity_id):
    """Per-entity engagement-depth time series. Picks one axis_key and
    returns its `z` (=depth) value for each snapshot date in the window.

    Query params:
      days — window size, default 90, max 365
      axis — which axis to read from; default 'effortFit'. Depth (z) is
             the same across all 3 axes per snapshot day so this is
             really just a stable choice for the dedupe key.

    Returns:
      { entity_id, days, axis, points: [{date, depth, priority}] }
    """
    try:
        days = int(request.args.get("days", "90"))
    except (TypeError, ValueError):
        days = 90
    days = max(7, min(365, days))
    axis = (request.args.get("axis") or "effortFit").strip()

    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    db = get_db()
    rows = db.execute(
        "SELECT snapshot_date, z, priority "
        "  FROM entity_position_snapshots "
        " WHERE entity_id = ? AND axis_key = ? AND snapshot_date >= ? "
        " ORDER BY snapshot_date ASC",
        (entity_id, axis, cutoff),
    ).fetchall()
    points = [
        {"date": r["snapshot_date"], "depth": (float(r["z"]) if r["z"] is not None else None), "priority": r["priority"]}
        for r in rows
    ]
    return jsonify({
        "entity_id": entity_id,
        "days": days,
        "axis": axis,
        "points": points,
        "count": len(points),
    })


@app.route("/api/snapshots/trajectories", methods=["GET"])
@auth_required
def snapshots_trajectories_bulk():
    """Batch trajectory endpoint — returns ALL entities' depth time series
    in one round trip. Used to render sparklines across the database view
    without firing 300 individual requests.

    Query params:
      days — window size, default 90, max 365
      axis — default 'effortFit' (depth is axis-invariant; we just pick
             one for the dedupe key)
      ids  — optional comma-separated subset, e.g. ?ids=ent-1,ent-2

    Returns:
      { days, axis, by_entity: { entity_id: [{date, depth, priority}, …] } }
    """
    try:
        days = int(request.args.get("days", "90"))
    except (TypeError, ValueError):
        days = 90
    days = max(7, min(365, days))
    axis = (request.args.get("axis") or "effortFit").strip()
    ids_param = (request.args.get("ids") or "").strip()
    ids_filter = [i.strip() for i in ids_param.split(",") if i.strip()] if ids_param else None

    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    db = get_db()
    if ids_filter:
        # SQLite parameter limit is 999, but our list will be at most a few
        # hundred entities so a single IN-clause is fine on both backends.
        placeholders = ",".join(["?"] * len(ids_filter))
        sql = (
            "SELECT entity_id, snapshot_date, z, priority "
            "  FROM entity_position_snapshots "
            " WHERE axis_key = ? AND snapshot_date >= ? "
            f"   AND entity_id IN ({placeholders}) "
            " ORDER BY entity_id ASC, snapshot_date ASC"
        )
        params = [axis, cutoff, *ids_filter]
    else:
        sql = (
            "SELECT entity_id, snapshot_date, z, priority "
            "  FROM entity_position_snapshots "
            " WHERE axis_key = ? AND snapshot_date >= ? "
            " ORDER BY entity_id ASC, snapshot_date ASC"
        )
        params = [axis, cutoff]
    rows = db.execute(sql, tuple(params)).fetchall()
    by_entity = {}
    for r in rows:
        by_entity.setdefault(r["entity_id"], []).append({
            "date": r["snapshot_date"],
            "depth": (float(r["z"]) if r["z"] is not None else None),
            "priority": r["priority"],
        })
    return jsonify({
        "days": days,
        "axis": axis,
        "entities_with_history": len(by_entity),
        "by_entity": by_entity,
    })


# ============================================================================
# FOLLOW-UPS — prospective task per partner ("Follow up with X by Friday").
# ----------------------------------------------------------------------------
# Complements the (retrospective) outreach log: outreach captures what we did,
# follow-ups capture what we'll do. Single owner per task, optional due date,
# status open|done. Done rows are retained so the activity timeline can show
# "Defne ticked off 'send brochure to CTU' on 2026-05-22".
# ============================================================================
def _followup_row_to_dict(row):
    """Postgres/sqlite row → dict, with consistent types for JSON."""
    if not row:
        return None
    return {
        "id":           row["id"],
        "entity_id":    row["entity_id"],
        "title":        row["title"],
        "due_date":     row["due_date"],
        "owner_handle": row["owner_handle"],
        "status":       row["status"] or "open",
        "notes":        row["notes"] or "",
        "created_at":   row["created_at"],
        "updated_at":   row["updated_at"],
        "completed_at": row["completed_at"],
        "created_by":   row["created_by"],
    }


@app.route("/api/followups", methods=["GET"])
@auth_required
def followups_list():
    """List follow-ups; optional filters: status, owner_handle, entity_id."""
    status  = (request.args.get("status") or "").strip()
    owner   = (request.args.get("owner")  or "").strip()
    entity  = (request.args.get("entity") or "").strip()
    clauses = []
    params  = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if owner:
        clauses.append("owner_handle = ?")
        params.append(owner)
    if entity:
        clauses.append("entity_id = ?")
        params.append(entity)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    # Sort: open first (open before done alphabetically),
    # then by due_date asc (NULLs last via a CASE trick), then created_at desc.
    sql = (
        "SELECT id, entity_id, title, due_date, owner_handle, status, "
        "       notes, created_at, updated_at, completed_at, created_by "
        "  FROM followups " + where + " "
        " ORDER BY status ASC, "
        "          CASE WHEN due_date IS NULL OR due_date = '' THEN 1 ELSE 0 END ASC, "
        "          due_date ASC, "
        "          created_at DESC"
    )
    db = get_db()
    rows = db.execute(sql, tuple(params)).fetchall()
    return jsonify({"followups": [_followup_row_to_dict(r) for r in rows]})


@app.route("/api/followups/<followup_id>", methods=["GET"])
@auth_required
def followups_get(followup_id):
    db = get_db()
    row = db.execute(
        "SELECT id, entity_id, title, due_date, owner_handle, status, "
        "       notes, created_at, updated_at, completed_at, created_by "
        "  FROM followups WHERE id = ?",
        (followup_id,),
    ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(_followup_row_to_dict(row))


@app.route("/api/followups/<followup_id>", methods=["PUT"])
@auth_required
def followups_upsert(followup_id):
    body = request.get_json(force=True, silent=True) or {}
    entity_id = (body.get("entity_id") or "").strip()
    title     = (body.get("title")     or "").strip()
    if not entity_id or not title:
        return jsonify({"error": "entity_id and title are required"}), 400

    now    = _now_iso()
    status = (body.get("status") or "open").strip().lower()
    if status not in ("open", "done"):
        status = "open"
    completed_at = (body.get("completed_at") or None) or (now if status == "done" else None)

    db = get_db()
    existing = db.execute(
        "SELECT created_at, created_by FROM followups WHERE id = ?",
        (followup_id,),
    ).fetchone()
    created_at = existing["created_at"] if existing else (body.get("created_at") or now)
    created_by = (existing["created_by"] if existing else None) or (request.headers.get("X-Display-Name") or "").strip() or None

    # If completing a previously-open followup, lock the completed_at to now
    # (don't trust the client to backdate). If re-opening, clear it.
    if existing:
        prev = db.execute("SELECT status, completed_at FROM followups WHERE id = ?", (followup_id,)).fetchone()
        if prev:
            if status == "done" and (prev["status"] or "open") != "done":
                completed_at = now
            elif status == "open":
                completed_at = None

    db.execute(
        "INSERT INTO followups "
        "(id, entity_id, title, due_date, owner_handle, status, notes, "
        " created_at, updated_at, completed_at, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET "
        "  entity_id    = excluded.entity_id, "
        "  title        = excluded.title, "
        "  due_date     = excluded.due_date, "
        "  owner_handle = excluded.owner_handle, "
        "  status       = excluded.status, "
        "  notes        = excluded.notes, "
        "  updated_at   = excluded.updated_at, "
        "  completed_at = excluded.completed_at",
        (
            followup_id, entity_id, title,
            (body.get("due_date")     or "").strip() or None,
            (body.get("owner_handle") or "").strip() or None,
            status,
            (body.get("notes") or ""),
            created_at, now, completed_at, created_by,
        ),
    )
    db.commit()
    _record_edit("followup", "upsert", followup_id)
    return jsonify({"ok": True, "id": followup_id})


@app.route("/api/followups/<followup_id>", methods=["DELETE"])
@auth_required
def followups_delete(followup_id):
    db = get_db()
    cur = db.execute("DELETE FROM followups WHERE id = ?", (followup_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    _record_edit("followup", "delete", followup_id)
    return jsonify({"ok": True})


# ============================================================================
# ASPIRATIONAL DRAG — partnership goal tracking.
# ----------------------------------------------------------------------------
# Operator drags a sphere on a 2×2 strategic map to a desired position. The
# system records (entity, axis, current, target, quadrant_jump) and asks
# Gemini for a 4-6 step action plan to actually move the partner there.
# Actions can be one-click promoted to follow-ups (closing the loop with
# the existing /api/followups infra). The aspiration auto-resolves when:
#   - the entity's computed position arrives in the target quadrant
#     ("achieved") — checked client-side on each render
#   - the 90-day TTL elapses ("expired") — checked nightly via the
#     /api/aspirations endpoint's lazy-expiry on read
#   - the operator hits Abandon ("abandoned")
# Killer feature: no CRM in market lets you drag a partner to "where you
# want it" and get back specific actions. See CLAUDE.md backlog #2.
# ============================================================================

# Allowed axes — matches STRAT_AXES on the frontend. Pinning the list
# server-side prevents typos / malicious axis names from creating
# orphaned aspirations.
_ASPIRATION_AXES = {"effortFit", "reachReadiness", "costRoi"}
_ASPIRATION_QUADRANTS = {"bl", "br", "tl", "tr"}
_ASPIRATION_STATUSES = {"active", "achieved", "abandoned", "expired"}
_ASPIRATION_FEASIBILITY = {"micro", "realistic", "ambitious", "transformational"}


def _aspiration_feasibility(jump, source_q, target_q):
    """Distance-based feasibility label. Same quadrant = micro tweak;
    adjacent = realistic (1 axis flip); diagonal = transformational
    (both axes flip). The label feeds the friction modal copy on the
    frontend so users don't drag every partner to Quick Win mindlessly."""
    if source_q == target_q:
        return "micro"
    if jump == 1:
        return "realistic"
    if jump == 2:
        # Diagonal opposite (e.g. bl → tr) — the hardest jump
        return "transformational"
    # Shouldn't happen with our 2×2 grid, but fall back gracefully
    return "ambitious"


def _aspiration_quadrant(x, y):
    """0-100 coords → BCG quadrant. Matches the frontend's quadrant
    assignment in _stratTopOpportunities exactly so the server and
    client never disagree on which quadrant a position falls in."""
    if y >= 50:
        return "tr" if x >= 50 else "tl"
    return "br" if x >= 50 else "bl"


def _aspiration_quadrant_jump(source_q, target_q):
    """Hamming-style distance between two quadrants on the 2×2.
    0 = same · 1 = one axis flipped · 2 = both axes flipped (diagonal)."""
    if source_q == target_q:
        return 0
    # Same row OR same column = 1; otherwise = 2
    same_row = source_q[0] == target_q[0]
    same_col = source_q[1] == target_q[1]
    return 1 if (same_row or same_col) else 2


def _aspiration_row_to_dict(row):
    """DB row → JSON-serialisable dict. Decodes the cached AI action plan
    and the linked-followup id list so the frontend gets real arrays/objects
    instead of stringified JSON."""
    if not row:
        return None
    d = dict(row)
    # actions_json → actions
    if d.get("actions_json"):
        try:
            d["actions"] = json.loads(d["actions_json"])
        except (ValueError, TypeError):
            d["actions"] = None
    d.pop("actions_json", None)
    # linked_followup_ids JSON string → real list
    if d.get("linked_followup_ids"):
        try:
            d["linked_followup_ids"] = json.loads(d["linked_followup_ids"])
        except (ValueError, TypeError):
            d["linked_followup_ids"] = []
    else:
        d["linked_followup_ids"] = []
    return d


@app.route("/api/aspirations", methods=["GET"])
@auth_required
def aspirations_list():
    """List aspirations. Optional filters: status, entity_id, axis_key.
    Also performs lazy TTL expiry — any active aspiration past expires_at
    is auto-flipped to 'expired' before we return."""
    db = get_db()
    # Lazy expiry: flip stale active rows to expired in one shot
    db.execute(
        "UPDATE aspiration_goals "
        "   SET status = 'expired', updated_at = ? "
        " WHERE status = 'active' AND expires_at < ?",
        (_now_iso(), _now_iso()),
    )
    db.commit()

    status = (request.args.get("status") or "").strip()
    entity = (request.args.get("entity_id") or "").strip()
    axis   = (request.args.get("axis_key") or "").strip()
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if entity:
        clauses.append("entity_id = ?")
        params.append(entity)
    if axis:
        clauses.append("axis_key = ?")
        params.append(axis)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT * FROM aspiration_goals " + where + " "
        " ORDER BY status ASC, created_at DESC"
    )
    rows = db.execute(sql, tuple(params)).fetchall()
    return jsonify({"aspirations": [_aspiration_row_to_dict(r) for r in rows]})


@app.route("/api/aspirations/<aspiration_id>", methods=["GET"])
@auth_required
def aspirations_get(aspiration_id):
    db = get_db()
    row = db.execute("SELECT * FROM aspiration_goals WHERE id = ?", (aspiration_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(_aspiration_row_to_dict(row))


@app.route("/api/aspirations", methods=["POST"])
@auth_required
def aspirations_create():
    """Create a new aspiration goal. The frontend sends the entity_id +
    axis_key + current/target coordinates; we derive quadrants + jump +
    feasibility server-side so they're computed consistently."""
    body = request.get_json(force=True, silent=True) or {}
    entity_id = (body.get("entity_id") or "").strip()
    axis_key  = (body.get("axis_key")  or "").strip()
    try:
        cur_x = int(body.get("current_x"))
        cur_y = int(body.get("current_y"))
        tgt_x = int(body.get("target_x"))
        tgt_y = int(body.get("target_y"))
    except (TypeError, ValueError):
        return jsonify({"error": "current_x, current_y, target_x, target_y must all be integers 0-100"}), 400
    if not entity_id:
        return jsonify({"error": "entity_id is required"}), 400
    if axis_key not in _ASPIRATION_AXES:
        return jsonify({"error": f"axis_key must be one of {sorted(_ASPIRATION_AXES)}"}), 400
    for v, name in ((cur_x, "current_x"), (cur_y, "current_y"), (tgt_x, "target_x"), (tgt_y, "target_y")):
        if v < 0 or v > 100:
            return jsonify({"error": f"{name}={v} out of range 0-100"}), 400

    src_q = _aspiration_quadrant(cur_x, cur_y)
    tgt_q = _aspiration_quadrant(tgt_x, tgt_y)
    jump  = _aspiration_quadrant_jump(src_q, tgt_q)
    feas  = _aspiration_feasibility(jump, src_q, tgt_q)

    aspiration_id = "asp-" + secrets.token_hex(6)
    now = _now_iso()
    # 90-day TTL — picked per CLAUDE.md backlog notes ("aspirations expire
    # at 90 days with a 'still chasing?' reminder"). Computed from now.
    expires = (
        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=90)
    ).isoformat().replace("+00:00", "Z")
    created_by = (request.headers.get("X-Display-Name") or "").strip() or None
    note = (body.get("note") or "").strip() or None

    db = get_db()
    db.execute(
        "INSERT INTO aspiration_goals "
        "(id, entity_id, axis_key, current_x, current_y, target_x, target_y, "
        " source_quadrant, target_quadrant, quadrant_jump, feasibility, "
        " status, note, created_at, updated_at, expires_at, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
        (
            aspiration_id, entity_id, axis_key,
            cur_x, cur_y, tgt_x, tgt_y,
            src_q, tgt_q, jump, feas,
            note, now, now, expires, created_by,
        ),
    )
    db.commit()
    _record_edit("aspiration", "create", aspiration_id)
    return jsonify({
        "ok": True,
        "id": aspiration_id,
        "source_quadrant": src_q,
        "target_quadrant": tgt_q,
        "quadrant_jump":   jump,
        "feasibility":     feas,
        "expires_at":      expires,
    })


@app.route("/api/aspirations/<aspiration_id>/status", methods=["PUT"])
@auth_required
def aspirations_set_status(aspiration_id):
    """Flip an aspiration's status — used for the operator-driven 'Abandon'
    button and the automatic 'Achieved' when the entity reaches the target
    quadrant. Frontend posts new status; we record the appropriate timestamp."""
    body = request.get_json(force=True, silent=True) or {}
    new_status = (body.get("status") or "").strip().lower()
    if new_status not in _ASPIRATION_STATUSES:
        return jsonify({"error": f"status must be one of {sorted(_ASPIRATION_STATUSES)}"}), 400
    db = get_db()
    row = db.execute("SELECT id, status FROM aspiration_goals WHERE id = ?", (aspiration_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    now = _now_iso()
    achieved_at  = now if new_status == "achieved"  else None
    abandoned_at = now if new_status == "abandoned" else None
    db.execute(
        "UPDATE aspiration_goals SET status = ?, updated_at = ?, "
        "  achieved_at = COALESCE(?, achieved_at), "
        "  abandoned_at = COALESCE(?, abandoned_at) "
        "WHERE id = ?",
        (new_status, now, achieved_at, abandoned_at, aspiration_id),
    )
    db.commit()
    _record_edit("aspiration", "status", aspiration_id)
    return jsonify({"ok": True})


@app.route("/api/aspirations/<aspiration_id>", methods=["DELETE"])
@auth_required
def aspirations_delete(aspiration_id):
    db = get_db()
    cur = db.execute("DELETE FROM aspiration_goals WHERE id = ?", (aspiration_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    _record_edit("aspiration", "delete", aspiration_id)
    return jsonify({"ok": True})


@app.route("/api/aspirations/<aspiration_id>", methods=["PUT"])
@auth_required
def aspirations_upsert(aspiration_id):
    """Upsert an aspiration with its original id — used by the frontend
    Undo flow to restore a deleted aspiration with all its history intact
    (actions_json, linked_followup_ids, created_at, expires_at). Mirrors
    the pattern followups + contracts use for their undo restoration."""
    body = request.get_json(force=True, silent=True) or {}
    entity_id = (body.get("entity_id") or "").strip()
    axis_key  = (body.get("axis_key")  or "").strip()
    if not entity_id or axis_key not in _ASPIRATION_AXES:
        return jsonify({"error": "entity_id + valid axis_key required"}), 400
    try:
        cur_x = int(body.get("current_x", 0))
        cur_y = int(body.get("current_y", 0))
        tgt_x = int(body.get("target_x", 0))
        tgt_y = int(body.get("target_y", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "xy coordinates must be integers"}), 400

    src_q = (body.get("source_quadrant") or _aspiration_quadrant(cur_x, cur_y))
    tgt_q = (body.get("target_quadrant") or _aspiration_quadrant(tgt_x, tgt_y))
    jump  = body.get("quadrant_jump")
    if not isinstance(jump, int):
        jump = _aspiration_quadrant_jump(src_q, tgt_q)
    feas  = body.get("feasibility") or _aspiration_feasibility(jump, src_q, tgt_q)
    status = (body.get("status") or "active").strip().lower()
    if status not in _ASPIRATION_STATUSES:
        status = "active"
    now = _now_iso()
    created_at  = body.get("created_at")  or now
    updated_at  = now
    expires_at  = body.get("expires_at")  or now
    created_by  = body.get("created_by")  or ((request.headers.get("X-Display-Name") or "").strip() or None)
    note        = (body.get("note") or "").strip() or None
    actions_json = json.dumps(body["actions"]) if body.get("actions") else (body.get("actions_json") or None)
    linked      = body.get("linked_followup_ids") or []
    linked_json = json.dumps(linked) if linked else None

    db = get_db()
    db.execute(
        "INSERT INTO aspiration_goals "
        "(id, entity_id, axis_key, current_x, current_y, target_x, target_y, "
        " source_quadrant, target_quadrant, quadrant_jump, feasibility, "
        " status, actions_json, note, linked_followup_ids, "
        " created_at, updated_at, expires_at, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET "
        "  entity_id = excluded.entity_id, axis_key = excluded.axis_key, "
        "  current_x = excluded.current_x, current_y = excluded.current_y, "
        "  target_x = excluded.target_x, target_y = excluded.target_y, "
        "  source_quadrant = excluded.source_quadrant, "
        "  target_quadrant = excluded.target_quadrant, "
        "  quadrant_jump = excluded.quadrant_jump, "
        "  feasibility = excluded.feasibility, "
        "  status = excluded.status, "
        "  actions_json = excluded.actions_json, "
        "  note = excluded.note, "
        "  linked_followup_ids = excluded.linked_followup_ids, "
        "  updated_at = excluded.updated_at",
        (
            aspiration_id, entity_id, axis_key,
            cur_x, cur_y, tgt_x, tgt_y,
            src_q, tgt_q, jump, feas,
            status, actions_json, note, linked_json,
            created_at, updated_at, expires_at, created_by,
        ),
    )
    db.commit()
    _record_edit("aspiration", "upsert", aspiration_id)
    return jsonify({"ok": True, "id": aspiration_id})


@app.route("/api/aspirations/<aspiration_id>/link-followup", methods=["POST"])
@auth_required
def aspirations_link_followup(aspiration_id):
    """Record that a follow-up was created from one of this aspiration's
    AI actions. Stored as a JSON array of {followup_id, action_index}
    so the UI can show 'X of N actions promoted to follow-ups'."""
    body = request.get_json(force=True, silent=True) or {}
    followup_id  = (body.get("followup_id")  or "").strip()
    action_index = body.get("action_index")
    if not followup_id:
        return jsonify({"error": "followup_id is required"}), 400
    if not isinstance(action_index, int):
        return jsonify({"error": "action_index must be an integer"}), 400
    db = get_db()
    row = db.execute(
        "SELECT linked_followup_ids FROM aspiration_goals WHERE id = ?",
        (aspiration_id,),
    ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    existing = []
    if row["linked_followup_ids"]:
        try:
            existing = json.loads(row["linked_followup_ids"])
        except (ValueError, TypeError):
            existing = []
    if not isinstance(existing, list):
        existing = []
    # Avoid duplicate links if user clicks twice
    if not any(
        isinstance(x, dict) and x.get("followup_id") == followup_id
        for x in existing
    ):
        existing.append({"followup_id": followup_id, "action_index": action_index})
    db.execute(
        "UPDATE aspiration_goals SET linked_followup_ids = ?, updated_at = ? WHERE id = ?",
        (json.dumps(existing), _now_iso(), aspiration_id),
    )
    db.commit()
    _record_edit("aspiration", "link-followup", aspiration_id)
    return jsonify({"ok": True, "linked": existing})


# Action-generator system prompt. Tight constraints — generic actions
# ("schedule a meeting") are worthless; specific actions ("Anna Sokolova
# at TalTech, warm intro via Defne's 2nd degree LinkedIn") are gold.
# We pass entity + contacts + score components + delta + KB; Gemini
# returns 4-6 actions as JSON.
ASPIRATION_SYSTEM_PROMPT = """You are a senior partnership strategist for H-FARM College — a hands-on, applied institution near Venice that runs summer programmes, sponsorship events, and exchange agreements with universities, agencies, schools, and student organisations.

The operator has DRAGGED a partner on a 2×2 strategic map from its current position to where they want it to be. Your job: return a concrete, specific action plan that would actually move the partner there. Not generic CRM advice — specific to THIS entity, using THIS data.

Hard rules:
1. Output 4-6 actions. No more, no less. Fewer for micro tweaks, more for transformational jumps.
2. Each action is ONE concrete step the operator can do in 1-30 days. Not a multi-month epic.
3. Reference SPECIFIC data the operator gave you: a contact name, an existing programme fit, the entity's country, a focus area, the gap implied by the drag. If contacts is empty, say "research a named contact at <department>" — DON'T invent names.
4. Order by impact × ease: fastest wins first, longer plays last.
5. NEVER invent: contact names, programme names, email addresses, or facts not in the data. If you don't know, say "research the X" or "verify Y."
6. Refuse generic actions: "schedule a meeting" alone is useless; "schedule a 30-min discovery call with <name from contacts> about <specific focus_area>" is useful.
7. Calibrate to the feasibility band:
   - micro:           1-2 light actions to nudge the position
   - realistic:       4-5 concrete actions in 90 days
   - ambitious:       5-6 actions, some longer-cycle
   - transformational: 6 actions including a "honest reality check" item flagging this as 12-18 month work

Return ONLY a JSON object with this exact shape, no markdown, no commentary:
{
  "headline": "1-sentence framing of what the operator is actually trying to do — not a restatement of the drag, but the strategic intent behind it.",
  "actions": [
    {
      "title":   "<5-12 word imperative action — e.g. 'Email Anna Sokolova about Q3 startup summer placement'>",
      "detail":  "<1-2 sentences explaining WHY this action moves the entity toward the target. Reference the entity's data.>",
      "owner_hint": "<role best suited to drive this — Marketing / Programmes / Executive / Operator>",
      "due_in_days": <integer 1-90>,
      "evidence": "<which entity field or score component justifies this — e.g. 'days_dormant=87' or 'no contacts on file'>"
    }
  ],
  "reality_check": "<OPTIONAL — only present for transformational jumps. 1-2 sentences acknowledging this is a long arc, not a sprint. Empty string otherwise.>"
}"""


@app.route("/api/aspirations/<aspiration_id>/generate-actions", methods=["POST"])
@auth_required
def aspirations_generate_actions(aspiration_id):
    """Gemini-backed AI action generator. The frontend POSTs the full entity
    context (the same _CHAT_FIELDS slim used by /api/chat-query, plus
    contacts + score components). We combine it with the aspiration's
    delta + feasibility and ask Gemini for 4-6 specific actions.

    Caches the result in actions_json so a re-open of the modal doesn't
    re-bill Gemini. Pass `force=true` to bust the cache (e.g. user wants
    a fresh suggestion after editing the target)."""
    if not GEMINI_KEY:
        return jsonify({"error": "GEMINI_API_KEY is not set on the server."}), 500
    body = request.get_json(force=True, silent=True) or {}
    force = bool(body.get("force"))
    entity = body.get("entity") or {}
    if not isinstance(entity, dict) or not entity.get("id"):
        return jsonify({"error": "entity payload (with id + slim fields) is required"}), 400

    db = get_db()
    asp_row = db.execute(
        "SELECT * FROM aspiration_goals WHERE id = ?",
        (aspiration_id,),
    ).fetchone()
    if not asp_row:
        return jsonify({"error": "aspiration not found"}), 404
    asp = dict(asp_row)

    # Cache hit: return the stored actions unless force=true
    if asp.get("actions_json") and not force:
        try:
            cached = json.loads(asp["actions_json"])
            return jsonify({"cached": True, **cached})
        except (ValueError, TypeError):
            pass  # fall through to regenerate if cache corrupted

    # Axis labels (so the prompt can talk about "Effort × Fit" not the
    # opaque key "effortFit"). Mirrors STRAT_AXES on the frontend.
    axis_labels = {
        "effortFit":      {"label": "Effort × Fit",
                           "x": "Effort to land",  "y": "Fit",
                           "quadrants": {"bl": "Easy Passes", "br": "Don't Bother", "tl": "Quick Wins", "tr": "Strategic Bets"}},
        "reachReadiness": {"label": "Reach × Readiness",
                           "x": "Reach",           "y": "Readiness",
                           "quadrants": {"bl": "Triage", "br": "Wake-up Calls", "tl": "Long Shots", "tr": "Active Wins"}},
        "costRoi":        {"label": "Cost × ROI",
                           "x": "Cost to develop", "y": "ROI",
                           "quadrants": {"bl": "Quick Wins", "br": "Money Pit", "tl": "Star Deals", "tr": "Worth Fighting"}},
    }
    axis_info = axis_labels.get(asp["axis_key"], {})

    # Slim the entity to the fields Gemini actually needs — saves tokens
    # and keeps the payload predictable.
    slim_entity = {k: entity.get(k) for k in (
        "id", "name", "type", "country", "continent", "city",
        "priority", "strategic_tier", "partnership_score",
        "partnership_readiness", "days_dormant", "last_contacted",
        "focus_areas", "top_program_id", "top_program_score",
        "notes", "website",
    ) if entity.get(k) not in (None, "")}
    contacts = entity.get("contacts") or []
    if isinstance(contacts, list):
        slim_contacts = [
            {k: c.get(k) for k in ("name", "role", "email") if c.get(k)}
            for c in contacts
            if isinstance(c, dict) and any(c.get(k) for k in ("name", "role", "email"))
        ]
        if slim_contacts:
            slim_entity["contacts"] = slim_contacts

    # Pull recent outreach + follow-ups so the action plan doesn't suggest
    # things the operator has already tried this quarter.
    # Outreach table columns are (id, entity_id, payload, updated_at,
    # deleted) — NOT entry_json / created_at. Tombstoned rows have
    # deleted=1 and should be excluded.
    recent_outreach = db.execute(
        "SELECT payload FROM outreach "
        " WHERE entity_id = ? AND (deleted = 0 OR deleted IS NULL) "
        " ORDER BY updated_at DESC LIMIT 5",
        (entity.get("id"),),
    ).fetchall()
    recent_outreach_summaries = []
    for r in recent_outreach:
        try:
            o = json.loads(r["payload"])
            recent_outreach_summaries.append({
                "kind":      o.get("kind", ""),
                "subject":   (o.get("subject") or "")[:120],
                "status":    o.get("status", ""),
                "date":      o.get("date", ""),
            })
        except (ValueError, TypeError):
            continue
    open_followups = db.execute(
        "SELECT title, due_date FROM followups "
        " WHERE entity_id = ? AND status = 'open' "
        " ORDER BY due_date ASC LIMIT 6",
        (entity.get("id"),),
    ).fetchall()
    open_followups_list = [
        {"title": r["title"], "due_date": r["due_date"]}
        for r in open_followups
    ]

    prompt_parts = [
        "## The map",
        f"Axis: **{axis_info.get('label', asp['axis_key'])}**",
        f"X-axis: {axis_info.get('x', '')} (low=0 → high=100)",
        f"Y-axis: {axis_info.get('y', '')} (low=0 → high=100)",
        f"Quadrant labels: BL={axis_info.get('quadrants', {}).get('bl', '')}, BR={axis_info.get('quadrants', {}).get('br', '')}, TL={axis_info.get('quadrants', {}).get('tl', '')}, TR={axis_info.get('quadrants', {}).get('tr', '')}",
        "",
        "## The aspirational drag",
        f"Current position: ({asp['current_x']}, {asp['current_y']}) — quadrant **{asp['source_quadrant'].upper()}** ({axis_info.get('quadrants', {}).get(asp['source_quadrant'], '')})",
        f"Target position:  ({asp['target_x']}, {asp['target_y']}) — quadrant **{asp['target_quadrant'].upper()}** ({axis_info.get('quadrants', {}).get(asp['target_quadrant'], '')})",
        f"Delta: ΔX = {asp['target_x'] - asp['current_x']:+d}, ΔY = {asp['target_y'] - asp['current_y']:+d}",
        f"Quadrant jump: {asp['quadrant_jump']} ({asp['feasibility']})",
        "",
        "## The entity",
        json.dumps(slim_entity, ensure_ascii=False),
        "",
        "## Recent outreach (last 5)",
        json.dumps(recent_outreach_summaries, ensure_ascii=False) if recent_outreach_summaries else "(none — partner has no logged outreach yet)",
        "",
        "## Open follow-ups",
        json.dumps(open_followups_list, ensure_ascii=False) if open_followups_list else "(none)",
        "",
        "Respond with JSON only.",
    ]
    if asp.get("note"):
        prompt_parts.insert(0, f"## Operator's note when creating this aspiration\n{asp['note']}\n")

    prompt = "\n".join(prompt_parts)

    try:
        resp = gemini_client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=ASPIRATION_SYSTEM_PROMPT,
                max_output_tokens=2048,
                temperature=0.5,
                response_mime_type="application/json",
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            ),
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Gemini API error: {e}"}), 502

    text = (resp.text or "").strip()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        # Tolerant retry — strip code fences if Gemini wrapped it
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
        try:
            parsed = json.loads(cleaned)
        except (ValueError, TypeError):
            return jsonify({"error": "Gemini returned non-JSON", "raw": text}), 502

    headline = (parsed.get("headline") or "").strip()
    actions  = parsed.get("actions") or []
    if not isinstance(actions, list):
        actions = []
    # Normalise + clamp 4-6 (defensive — sometimes Gemini overshoots)
    clean_actions = []
    for i, a in enumerate(actions[:6]):
        if not isinstance(a, dict):
            continue
        try:
            due_in = int(a.get("due_in_days", 14))
        except (ValueError, TypeError):
            due_in = 14
        due_in = max(1, min(90, due_in))
        clean_actions.append({
            "title":       (a.get("title") or "").strip()[:200],
            "detail":      (a.get("detail") or "").strip()[:600],
            "owner_hint":  (a.get("owner_hint") or "").strip()[:60],
            "due_in_days": due_in,
            "evidence":    (a.get("evidence") or "").strip()[:200],
        })
    reality_check = (parsed.get("reality_check") or "").strip()

    payload = {
        "headline":      headline,
        "actions":       clean_actions,
        "reality_check": reality_check,
        "generated_at":  _now_iso(),
        "model":         MODEL,
    }
    # Cache so re-opening the modal doesn't hit Gemini twice
    db.execute(
        "UPDATE aspiration_goals SET actions_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(payload), _now_iso(), aspiration_id),
    )
    db.commit()
    _record_edit("aspiration", "generate-actions", aspiration_id)

    usage_md = getattr(resp, "usage_metadata", None)
    return jsonify({
        **payload,
        "cached": False,
        "usage": {
            "input_tokens":  getattr(usage_md, "prompt_token_count",     0) if usage_md else 0,
            "output_tokens": getattr(usage_md, "candidates_token_count", 0) if usage_md else 0,
        },
    })


# ============================================================================
# SPONSORS — Career Day annual sponsorship records.
# ----------------------------------------------------------------------------
# One row per (event_year, normalized_name). A company that sponsors
# multiple years gets multiple rows so renewal-cycle analytics + year-
# over-year revenue read natively. Deliberately separate from UNIS
# (partnership pipeline) — different lifecycle, different stakeholders,
# different KPIs. Bridge to UNIS via linked_entity_id when the same
# company also exists in the partner database.
# ============================================================================
def _slug_for_sponsor(year: int, normalized_name: str) -> str:
    """Stable id: sponsor-<year>-<slug-of-normalized-name>. Lets us upsert
    by deterministic key without needing the client to coordinate."""
    slug = re.sub(r"[^a-z0-9]+", "-", (normalized_name or "").lower()).strip("-")
    return f"sponsor-{year}-{slug or 'unknown'}"


def _normalize_sponsor_name(raw: str) -> str:
    """Mirror of scripts/clean_sponsors.py's normalize_name(). Lowercase,
    strip Italian/German legal suffixes, collapse whitespace + punctuation.
    Keeps Group/Italia/SpA-like markers stripped consistently so the same
    company import twice still upserts."""
    if not raw:
        return ""
    s = str(raw).strip().lower()
    s = re.sub(r"\s+", " ", s)
    suffixes = [
        r"\bs\.p\.a\.?", r"\bspa\b", r"\bs\.r\.l\.?", r"\bsrl\b",
        r"\bscpa\b",     r"\bs\.c\.p\.a\.?", r"\bgmbh\b", r"\bsb\b",
    ]
    for pat in suffixes:
        s = re.sub(pat, "", s)
    s = re.sub(r"[.,'\"`]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _sponsor_row_to_dict(row):
    if not row:
        return None
    try:
        attendees = json.loads(row["attendees"]) if row["attendees"] else []
    except Exception:
        attendees = []
    return {
        "id":                      row["id"],
        "event_year":              row["event_year"],
        "event_name":              row["event_name"],
        "company_name":            row["company_name"],
        "normalized_name":         row["normalized_name"],
        "industry_sector":         row["industry_sector"] or "",
        "sponsorship_tier":        row["sponsorship_tier"] or "",
        "value_no_iva_eur":        row["value_no_iva_eur"],
        "value_with_iva_eur":      row["value_with_iva_eur"],
        "amount_paid_eur":         row["amount_paid_eur"],
        "contract_signed_by_us":   bool(row["contract_signed_by_us"]),
        "contract_signed_by_them": bool(row["contract_signed_by_them"]),
        "invoice_no":              row["invoice_no"] or "",
        "invoice_date":            row["invoice_date"] or "",
        "payment_date":            row["payment_date"] or "",
        "participation_days":      row["participation_days"] or "",
        "attendee_count":          row["attendee_count"],
        "attendees":               attendees,
        "primary_contact_name":    row["primary_contact_name"] or "",
        "primary_contact_email":   row["primary_contact_email"] or "",
        "notes":                   row["notes"] or "",
        "linked_entity_id":        row["linked_entity_id"] or "",
        "created_at":              row["created_at"],
        "updated_at":              row["updated_at"],
        "created_by":              row["created_by"] or "",
    }


@app.route("/api/sponsors", methods=["GET"])
@auth_required
def sponsors_list():
    """List sponsors; optional filters: year, tier, sector, status."""
    year   = request.args.get("year")
    tier   = (request.args.get("tier")   or "").strip()
    sector = (request.args.get("sector") or "").strip()
    status = (request.args.get("status") or "").strip()   # signed|unsigned|all
    clauses, params = [], []
    if year:
        try:
            clauses.append("event_year = ?")
            params.append(int(year))
        except ValueError:
            pass
    if tier:
        clauses.append("sponsorship_tier = ?")
        params.append(tier)
    if sector:
        clauses.append("industry_sector = ?")
        params.append(sector)
    if status == "signed":
        clauses.append("contract_signed_by_us = 1 AND contract_signed_by_them = 1")
    elif status == "unsigned":
        clauses.append("(contract_signed_by_us = 0 OR contract_signed_by_them = 0)")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT * FROM sponsors " + where + " "
        "ORDER BY event_year DESC, "
        "         CASE sponsorship_tier "
        "           WHEN 'Gold'   THEN 1 "
        "           WHEN 'Bronze' THEN 2 "
        "           WHEN 'Base'   THEN 3 "
        "           ELSE 9 END, "
        "         company_name ASC"
    )
    db = get_db()
    rows = db.execute(sql, tuple(params)).fetchall()
    return jsonify({"sponsors": [_sponsor_row_to_dict(r) for r in rows]})


@app.route("/api/sponsors/years", methods=["GET"])
@auth_required
def sponsors_years():
    """List distinct event_years for the year selector. Always include
    current year so the picker shows the bucket users can populate now."""
    db = get_db()
    rows = db.execute("SELECT DISTINCT event_year FROM sponsors ORDER BY event_year DESC").fetchall()
    years = [int(r["event_year"]) for r in rows]
    current = _dt.datetime.now(_dt.timezone.utc).year
    if current not in years:
        years.insert(0, current)
    return jsonify({"years": sorted(years, reverse=True)})


@app.route("/api/sponsors/<sponsor_id>", methods=["GET"])
@auth_required
def sponsors_get(sponsor_id):
    db = get_db()
    row = db.execute("SELECT * FROM sponsors WHERE id = ?", (sponsor_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(_sponsor_row_to_dict(row))


@app.route("/api/sponsors/<sponsor_id>", methods=["PUT"])
@auth_required
def sponsors_upsert(sponsor_id):
    body = request.get_json(force=True, silent=True) or {}
    event_year   = body.get("event_year")
    company_name = (body.get("company_name") or "").strip()
    if not event_year or not company_name:
        return jsonify({"error": "event_year and company_name are required"}), 400
    try:
        event_year = int(event_year)
    except (TypeError, ValueError):
        return jsonify({"error": "event_year must be an integer"}), 400

    normalized = (body.get("normalized_name") or "").strip().lower() or _normalize_sponsor_name(company_name)
    # If client didn't send an id matching our slug scheme, regenerate it.
    expected_id = _slug_for_sponsor(event_year, normalized)
    # We accept any id; this just protects against id-vs-key drift on the
    # import path.
    if not sponsor_id:
        sponsor_id = expected_id

    now = _now_iso()
    db  = get_db()
    existing = db.execute("SELECT created_at, created_by FROM sponsors WHERE id = ?", (sponsor_id,)).fetchone()
    created_at = existing["created_at"] if existing else (body.get("created_at") or now)
    created_by = (existing["created_by"] if existing else None) or (request.headers.get("X-Display-Name") or "").strip() or None

    attendees_json = json.dumps(body.get("attendees") or [])

    db.execute(
        "INSERT INTO sponsors "
        "(id, event_year, event_name, company_name, normalized_name, industry_sector, "
        " sponsorship_tier, value_no_iva_eur, value_with_iva_eur, amount_paid_eur, "
        " contract_signed_by_us, contract_signed_by_them, invoice_no, invoice_date, "
        " payment_date, participation_days, attendee_count, attendees, "
        " primary_contact_name, primary_contact_email, notes, linked_entity_id, "
        " created_at, updated_at, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET "
        "  event_year              = excluded.event_year, "
        "  event_name              = excluded.event_name, "
        "  company_name            = excluded.company_name, "
        "  normalized_name         = excluded.normalized_name, "
        "  industry_sector         = excluded.industry_sector, "
        "  sponsorship_tier        = excluded.sponsorship_tier, "
        "  value_no_iva_eur        = excluded.value_no_iva_eur, "
        "  value_with_iva_eur      = excluded.value_with_iva_eur, "
        "  amount_paid_eur         = excluded.amount_paid_eur, "
        "  contract_signed_by_us   = excluded.contract_signed_by_us, "
        "  contract_signed_by_them = excluded.contract_signed_by_them, "
        "  invoice_no              = excluded.invoice_no, "
        "  invoice_date            = excluded.invoice_date, "
        "  payment_date            = excluded.payment_date, "
        "  participation_days      = excluded.participation_days, "
        "  attendee_count          = excluded.attendee_count, "
        "  attendees               = excluded.attendees, "
        "  primary_contact_name    = excluded.primary_contact_name, "
        "  primary_contact_email   = excluded.primary_contact_email, "
        "  notes                   = excluded.notes, "
        "  linked_entity_id        = excluded.linked_entity_id, "
        "  updated_at              = excluded.updated_at",
        (
            sponsor_id, event_year,
            (body.get("event_name") or "Career Day").strip(),
            company_name, normalized,
            (body.get("industry_sector")  or "").strip() or None,
            (body.get("sponsorship_tier") or "").strip() or None,
            int(body["value_no_iva_eur"])   if str(body.get("value_no_iva_eur")   or "").strip() else None,
            int(body["value_with_iva_eur"]) if str(body.get("value_with_iva_eur") or "").strip() else None,
            int(body["amount_paid_eur"])    if str(body.get("amount_paid_eur")    or "").strip() else None,
            1 if body.get("contract_signed_by_us")   else 0,
            1 if body.get("contract_signed_by_them") else 0,
            (body.get("invoice_no")           or "").strip() or None,
            (body.get("invoice_date")         or "").strip() or None,
            (body.get("payment_date")         or "").strip() or None,
            (body.get("participation_days")   or "").strip() or None,
            int(body["attendee_count"]) if str(body.get("attendee_count") or "").strip() else None,
            attendees_json,
            (body.get("primary_contact_name")  or "").strip() or None,
            (body.get("primary_contact_email") or "").strip() or None,
            (body.get("notes") or ""),
            (body.get("linked_entity_id")  or "").strip() or None,
            created_at, now, created_by,
        ),
    )
    db.commit()
    _record_edit("sponsor", "upsert", sponsor_id)
    return jsonify({"ok": True, "id": sponsor_id})


@app.route("/api/sponsors/<sponsor_id>", methods=["DELETE"])
@auth_required
def sponsors_delete(sponsor_id):
    db = get_db()
    cur = db.execute("DELETE FROM sponsors WHERE id = ?", (sponsor_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    _record_edit("sponsor", "delete", sponsor_id)
    return jsonify({"ok": True})


@app.route("/api/sponsors/import", methods=["POST"])
@auth_required
def sponsors_import():
    """Bulk upsert from the canonical CSV produced by scripts/clean_sponsors.py.

    Accepts:
      - multipart/form-data with `file` field (the CSV)
      - OR application/json body { rows: [...] } for programmatic ingest

    Query params:
      - event_year (int, default = current UTC year)
      - dry_run   (1 = preview without writing)

    Returns:
      { inserted, updated, errors[], dry_run, rows_seen }
    """
    try:
        event_year = int(request.args.get("event_year") or _dt.datetime.now(_dt.timezone.utc).year)
    except ValueError:
        return jsonify({"error": "event_year must be int"}), 400
    dry_run = (request.args.get("dry_run") or "").strip() in ("1", "true", "yes")

    # Source rows: either multipart CSV or JSON body
    rows = []
    if "file" in request.files:
        f = request.files["file"]
        try:
            content = f.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            f.stream.seek(0)
            content = f.read().decode("latin-1")
        import csv as _csv
        reader = _csv.DictReader(content.splitlines())
        rows = list(reader)
    else:
        body = request.get_json(force=True, silent=True) or {}
        rows = body.get("rows") or []

    if not rows:
        return jsonify({"error": "no rows; send multipart 'file' or JSON {rows:[...]}"}), 400

    # Header normalization — map raw Italian Excel column names (and other
    # common variants) to the canonical names the downstream code uses.
    # Without this step, uploading the raw Sheet export errors with
    # "no company_name / display_name" on every row because the actual
    # header is "AZIENDA".
    # Aliases are applied only when the canonical key isn't already set,
    # so a hand-cleaned CSV with proper headers still works untouched.
    _HEADER_ALIASES = {
        # company name
        "AZIENDA":                                  "display_name",
        "Azienda":                                  "display_name",
        # tier
        "Sponsorship":                              "sponsorship_tier",
        "sponsorship":                              "sponsorship_tier",
        # money
        "Tot dovuto senza IVA":                     "value_no_iva_eur",
        "Tot dovuto con IVA":                       "value_with_iva_eur",
        "INCASSATO":                                "amount_paid_eur",
        "Incassato":                                "amount_paid_eur",
        # contracts (note typo "sponsorhip" in the source file)
        "Contratto sponsorship firmato da noi":     "contract_signed_by_us",
        "Contratto sponsorhip firmato da azienda":  "contract_signed_by_them",
        "Contratto sponsorship firmato da azienda": "contract_signed_by_them",
        # invoice
        "N. fattura / N. ricevuta":                 "invoice_no",
        "N. fattura":                               "invoice_no",
        "data fattura":                             "invoice_date",
        "Data fattura":                             "invoice_date",
        # dates / participation
        "DATA PAGAMENTO":                           "payment_date",
        "Data pagamento":                           "payment_date",
        "Partecipazione":                           "participation_days",
        # contact
        "Mail Ref Aziendale":                       "primary_contact_email",
        "Mail":                                     "primary_contact_email",
        # notes (both NOTE and Notes — user may add a second "Notes" column)
        "NOTE":                                     "notes",
        "Note":                                     "notes",
        "Notes":                                    "notes",
        # industry (also handled in the per-field fallback below — kept here
        # too so a single normalization pass covers everything)
        "Industry":                                 "industry_sector",
        "industry":                                 "industry_sector",
        "Sector":                                   "industry_sector",
        "sector":                                   "industry_sector",
    }
    for r in rows:
        if not isinstance(r, dict):
            continue
        for raw_key, canonical_key in _HEADER_ALIASES.items():
            if raw_key in r and not r.get(canonical_key):
                val = r[raw_key]
                # Strip currency symbols + spaces from money fields so the
                # downstream _int() helper can parse them. "€ 3.000,00" → "3000"
                if canonical_key in ("value_no_iva_eur", "value_with_iva_eur", "amount_paid_eur"):
                    if val:
                        s = str(val).replace("€", "").replace(" ", "").replace("\xa0", "")
                        # Italian number format: 3.000,00 → 3000.00 (US format)
                        if "," in s and "." in s:
                            s = s.replace(".", "").replace(",", ".")
                        elif "," in s:
                            s = s.replace(",", ".")
                        val = s
                r[canonical_key] = val

    db = get_db()
    inserted, updated, errors = 0, 0, []
    now = _now_iso()
    created_by = (request.headers.get("X-Display-Name") or "").strip() or None

    for i, r in enumerate(rows):
        try:
            company = (r.get("display_name") or r.get("company_name") or "").strip()
            if not company:
                errors.append({"row": i, "error": "no company_name / display_name"})
                continue
            normalized = (r.get("normalized_name") or "").strip().lower() or _normalize_sponsor_name(company)
            sid = _slug_for_sponsor(event_year, normalized)
            attendees = []
            if r.get("ospiti_names"):
                attendees = [a.strip() for a in str(r["ospiti_names"]).split("|") if a.strip()]
            # bool detection (Python True/False or "True"/"true"/"1")
            def _bool(v):
                if v is True or v is False:
                    return v
                return str(v or "").strip().lower() in ("true", "1", "yes")
            # int-or-none from possibly-float string
            def _int(v):
                s = str(v or "").strip()
                if not s:
                    return None
                try:
                    return int(float(s))
                except (TypeError, ValueError):
                    return None

            existing = db.execute("SELECT 1 FROM sponsors WHERE id = ?", (sid,)).fetchone()
            payload = (
                sid, event_year, "Career Day", company, normalized,
                # Accept any of: area_tematica (canonical Italian source),
                # industry_sector (cleaned column name), Industry, industry,
                # Sector, sector (common user-added column variants).
                (r.get("area_tematica") or r.get("industry_sector")
                 or r.get("Industry") or r.get("industry")
                 or r.get("Sector") or r.get("sector")
                 or "").strip() or None,
                (r.get("sponsorship_tier") or "").strip() or None,
                _int(r.get("value_no_iva_eur") or r.get("value_no_iva")),
                _int(r.get("value_with_iva_eur")),
                _int(r.get("amount_paid_eur") or r.get("incassato_eur") or r.get("incassato")),
                1 if _bool(r.get("signed_by_us") or r.get("contract_signed_by_us")) else 0,
                1 if _bool(r.get("signed_by_them") or r.get("contract_signed_by_them")) else 0,
                (r.get("fattura_no") or r.get("invoice_no") or "").strip() or None,
                (r.get("fattura_date") or r.get("invoice_date") or "").strip() or None,
                (r.get("payment_date") or "").strip() or None,
                (r.get("partecipazione") or r.get("participation_days") or "").strip() or None,
                _int(r.get("ospiti_count") or r.get("attendee_count")),
                json.dumps(attendees),
                (r.get("primary_contact") or r.get("primary_contact_name") or "").strip() or None,
                (r.get("primary_email")   or r.get("primary_contact_email") or "").strip() or None,
                (r.get("note") or r.get("notes") or ""),
                (r.get("linked_entity_id") or "").strip() or None,
                now if not existing else None,   # created_at: only on insert
                now,                              # updated_at: always
                created_by,
            )
            if dry_run:
                if existing:
                    updated += 1
                else:
                    inserted += 1
                continue
            # Upsert. On UPDATE: use COALESCE so empty (NULL) values in the
            # incoming row PRESERVE existing DB values instead of overwriting
            # them with NULL. Otherwise a CSV re-upload where some Industry
            # cells happen to be blank would silently wipe out previously-
            # populated sectors.
            # Required fields (event_year, event_name, company_name,
            # normalized_name, updated_at) always overwrite. Money/bool fields
            # likewise overwrite — if you cleared a value you meant to.
            # Text fields that operators populate manually (industry_sector,
            # notes, invoice_no, dates, attendees, contacts) use COALESCE.
            if existing:
                db.execute(
                    "UPDATE sponsors SET event_year=?, event_name=?, company_name=?, normalized_name=?, "
                    "industry_sector=COALESCE(NULLIF(?, ''), industry_sector), "
                    "sponsorship_tier=COALESCE(NULLIF(?, ''), sponsorship_tier), "
                    "value_no_iva_eur=COALESCE(?, value_no_iva_eur), "
                    "value_with_iva_eur=COALESCE(?, value_with_iva_eur), "
                    "amount_paid_eur=COALESCE(?, amount_paid_eur), "
                    "contract_signed_by_us=?, contract_signed_by_them=?, "
                    "invoice_no=COALESCE(NULLIF(?, ''), invoice_no), "
                    "invoice_date=COALESCE(NULLIF(?, ''), invoice_date), "
                    "payment_date=COALESCE(NULLIF(?, ''), payment_date), "
                    "participation_days=COALESCE(NULLIF(?, ''), participation_days), "
                    "attendee_count=COALESCE(?, attendee_count), "
                    "attendees=COALESCE(NULLIF(?, '[]'), attendees), "
                    "primary_contact_name=COALESCE(NULLIF(?, ''), primary_contact_name), "
                    "primary_contact_email=COALESCE(NULLIF(?, ''), primary_contact_email), "
                    "notes=COALESCE(NULLIF(?, ''), notes), "
                    "linked_entity_id=COALESCE(NULLIF(?, ''), linked_entity_id), "
                    "updated_at=? "
                    "WHERE id=?",
                    payload[1:13] + payload[13:22] + (payload[23],) + (sid,),
                )
                updated += 1
            else:
                db.execute(
                    "INSERT INTO sponsors "
                    "(id, event_year, event_name, company_name, normalized_name, industry_sector, "
                    " sponsorship_tier, value_no_iva_eur, value_with_iva_eur, amount_paid_eur, "
                    " contract_signed_by_us, contract_signed_by_them, invoice_no, invoice_date, "
                    " payment_date, participation_days, attendee_count, attendees, "
                    " primary_contact_name, primary_contact_email, notes, linked_entity_id, "
                    " created_at, updated_at, created_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    payload,
                )
                inserted += 1
        except Exception as e:
            errors.append({"row": i, "error": str(e), "company": r.get("display_name") or r.get("company_name") or "?"})
    if not dry_run:
        db.commit()
        _record_edit("sponsor", "bulk_import", f"year={event_year},rows={inserted + updated}")
    return jsonify({
        "ok": True,
        "event_year": event_year,
        "dry_run": dry_run,
        "rows_seen": len(rows),
        "inserted": inserted,
        "updated":  updated,
        "errors":   errors,
    })


# ============================================================================
# PROSPECT DISCOVERY — AI-surfaced partnership candidates.
# ----------------------------------------------------------------------------
# Workflow: Tavily web search → Gemini structured output → 6-layer
# hallucination filter → prospect_candidates table → user reviews + decides
# (yes/no/maybe) → approved ones become UNIS entries.
#
# Hallucination defense (the whole point of the feature):
#   1. Mandatory grounding — every candidate must cite ≥ 1 source URL
#   2. URL alive check — HEAD/GET 200 for at least one source URL
#   3. Authority cross-check — ROR API for universities (research org registry)
#   4. UNIS dedupe — skip anything fuzzy-matched ≥ 80 with existing entity
#   5. Past candidates dedupe — skip anything already in our prospect bucket
#   6. Conservative output — never pad to hit a target count
# ============================================================================
TAVILY_API_KEY      = os.environ.get("TAVILY_API_KEY", "").strip()
ROR_API             = "https://api.ror.org/organizations"
PROSPECT_FUZZY_THRESHOLD = 90   # rapidfuzz token_set_ratio
# Was 80 — too generous with token_set_ratio (subset-friendly metric);
# "Global International Education Consultancy" was matching anything in
# UNIS with "Global" / "International" / "Education" / "Consultancy" in
# the name (single common word triggered ≥80 easily). 90 still catches
# real near-dupes ("Univ. of Foo" vs "University of Foo" → ~95) without
# the noisy false positives.


_tavily_client = None
def _get_tavily():
    """Lazy-load the Tavily client — keeps cold-start cheap when nobody
    triggers discovery."""
    global _tavily_client
    if _tavily_client is None and TAVILY_API_KEY:
        from tavily import TavilyClient
        _tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
    return _tavily_client


def _normalize_prospect_name(raw):
    """Strip legal suffixes + multi-language education boilerplate so the
    fuzzy dedupe compares MEANINGFUL tokens (the distinctive brand name),
    not generic frazlar like "Yurtdışı Eğitim Danışmanlığı" that every
    Turkish outbound consultancy includes in its full legal name.

    Without this, "ATEC Yurtdışı Eğitim Danışmanlığı" and "Global
    Yurtdışı Eğitim Danışmanlığı" share 3-of-4 tokens and score 75+
    against each other (and against any pipeline entity with one of
    those words), creating false-positive dupe rejections.

    Keeps Bauli ↔ Bauli S.p.A. matchable; ATEC vs Global no longer
    collide on shared boilerplate."""
    if not raw:
        return ""
    s = str(raw).strip().lower()
    s = re.sub(r"\s+", " ", s)
    for pat in [
        # ---- Legal suffixes (corp form) ----
        r"\bs\.p\.a\.?", r"\bspa\b", r"\bs\.r\.l\.?", r"\bsrl\b",
        r"\bscpa\b", r"\bs\.c\.p\.a\.?", r"\bgmbh\b", r"\bsb\b",
        r"\bltd\.?", r"\blimited\b", r"\bllc\b", r"\binc\.?", r"\bincorporated\b",
        r"\bplc\b", r"\bcorp\.?", r"\bcorporation\b",
        # ---- Italian uni boilerplate ----
        r"\buniversity of\b",
        r"\buniversit[aàá] (di|del|della|degli)\b",
        # ---- Turkish education-agency boilerplate ----
        # "yurtdışı eğitim danışmanlığı/ı" = "abroad education consultancy"
        # Every Turkish outbound consultancy uses these — strip so distinctive
        # brand tokens (ATEC, NGGlobal, Atayurt, Sage, IDP, Mojo) drive matching.
        r"\byurt ?d[ıi]ş[ıi]\b",
        r"\beğ[ıi]t[ıi]m\b",
        r"\bdan[ıi]şmanl[ıi]ğ[ıi]\b",
        r"\bdan[ıi]şmanl[ıi]k\b",
        r"\bdanışmanı\b",
        # ---- Spanish education-agency boilerplate ----
        r"\bagencia (de )?(estudios|educaci[oó]n)( en el extranjero)?\b",
        r"\bconsultor[ií]a (de )?(educaci[oó]n|estudios)( internacional)?\b",
        r"\bestudios en el extranjero\b",
        # ---- French / German / Portuguese boilerplate ----
        r"\bagence (d')?(études|education)( à l'étranger)?\b",
        r"\bauslandsstudium beratung\b",
        r"\bag[eê]ncia de interc[âa]mbio\b",
        # ---- Generic English study-abroad boilerplate ----
        r"\bstudy abroad\b",
        r"\beducation consultancy\b",
        r"\beducational consultancy\b",
        r"\bstudent recruitment\b",
        r"\boverseas education\b",
        r"\binternational education( services)?\b",
    ]:
        s = re.sub(pat, "", s, flags=re.IGNORECASE)
    s = re.sub(r"[.,'\"`]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Safety net: if aggressive boilerplate stripping left less than 3 chars
    # (e.g. an entity whose distinctive name IS the boilerplate itself),
    # fall back to a minimally-cleaned original so we don't compare empty
    # strings or single letters. Without this, an entity literally named
    # "Agencia de Estudios en el Extranjero" would normalize to ''.
    if len(s) < 3:
        s = re.sub(r"[.,'\"`]", "", str(raw).strip().lower())
        s = re.sub(r"\s+", " ", s).strip()
    return s


def _check_url_alive(url, timeout=4):
    """Quick liveness probe. HEAD first, fall back to GET on 405/403. Returns
    True only if 200 ≤ status < 400 within the timeout."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    try:
        import requests as _r
        for method in ("head", "get"):
            try:
                resp = _r.request(method, url, timeout=timeout, allow_redirects=True,
                                  headers={"User-Agent": "h-tracker-prospect/1.0"})
                if 200 <= resp.status_code < 400:
                    return True
                if resp.status_code in (403, 405) and method == "head":
                    continue   # try GET
            except Exception:
                if method == "head":
                    continue
                return False
        return False
    except Exception:
        return False


def _check_ror_existence(name, country=None):
    """Query Research Organization Registry for a matching institution.
    Returns ROR id (e.g. 'https://ror.org/...') if found, else None.
    Free, public, no API key — but rate-limited so we cap usage."""
    if not name or len(name) < 3:
        return None
    try:
        import requests as _r
        params = {"query": name}
        resp = _r.get(ROR_API, params=params, timeout=5,
                      headers={"User-Agent": "h-tracker-prospect/1.0"})
        if not resp.ok:
            return None
        data = resp.json()
        items = data.get("items") or []
        if not items:
            return None
        # Take the top match (ROR ranks by relevance). Optional country filter.
        for item in items[:3]:
            top_name = (item.get("name") or "").lower()
            if name.lower() in top_name or top_name in name.lower():
                if country:
                    countries = [c.get("country", {}).get("country_code", "").upper()
                                 for c in item.get("country", []) if isinstance(c, dict)]
                    if countries and country.upper()[:2] not in [c[:2] for c in countries]:
                        continue
                return item.get("id")
        return None
    except Exception as e:
        print(f"[ror] check failed for {name!r}: {e}")
        return None


def _tavily_search(query, max_results=10):
    """Run a single Tavily search. Returns list of {url, content, title}.
    Returns [] on failure — caller decides whether to abort or proceed."""
    client = _get_tavily()
    if not client:
        return []
    try:
        result = client.search(query=query, search_depth="advanced",
                               max_results=max_results, include_answer=False)
        return result.get("results", [])
    except Exception as e:
        print(f"[tavily] search failed: {e}")
        return []


def _build_prospect_prompt(criteria, search_results, existing_names, profile_json):
    """Construct the Gemini prompt for structured candidate extraction.
    Hard constraint: candidates must cite source URLs from the search results."""
    crit_lines = []
    if criteria.get("type"):    crit_lines.append(f"- Entity type: {criteria['type']}")
    if criteria.get("country"): crit_lines.append(f"- Country / region: {criteria['country']}")
    if criteria.get("focus"):   crit_lines.append(f"- Focus: {criteria['focus']}")
    if criteria.get("query"):   crit_lines.append(f"- Free-text query: {criteria['query']}")
    crit_str = "\n".join(crit_lines) or "(no specific criteria)"

    # Trim source content to keep prompt cheap
    src_lines = []
    for i, r in enumerate(search_results[:15], 1):
        url   = r.get("url", "")
        title = (r.get("title") or "")[:140]
        snip  = (r.get("content") or "")[:600]
        src_lines.append(f"[{i}] {title}\n    URL: {url}\n    SNIPPET: {snip}")
    src_str = "\n\n".join(src_lines) or "(no search results)"

    exclude_str = ", ".join(existing_names[:60]) if existing_names else "(none)"

    profile_str = profile_json or "(no preference profile yet — first searches)"

    return f"""You are a partnership prospect analyst for H-FARM College, an Italian
private higher-education institution based in Roncade (Treviso). You
surface NEW prospective ACADEMIC / STUDENT-MOBILITY partners for the
global partnerships team to evaluate.

============================================================
DIRECTIONALITY — H-FARM College is the RECEIVING side.
============================================================
We are based in Italy. We want partners who can SEND students TO US (or
exchange students WITH US as peers). We do NOT want partners whose
business is bringing students INTO their own country — those are
COMPETITORS, not partners.

Concretely, for the country in the search criteria:
  ✅ OUTBOUND — agencies / schools / orgs that send LOCAL students
                 ABROAD to study at foreign universities. H-FARM College
                 wants to be on their list of recommended destinations.
                 (e.g. "yurtdışı eğitim danışmanlığı" in Turkey,
                 "agencia de estudios en el extranjero" in Spain,
                 "Auslandsstudium" consultancies in Germany.)
  ❌ INBOUND  — agencies / orgs whose business is bringing FOREIGN
                 students INTO their own country. Their interest is in
                 filling LOCAL universities, not sending students to
                 Italian ones. (e.g. "Study in Turkey", "Study in Spain",
                 "Apply to Turkish Universities", "Bienvenue en France"
                 student-recruitment portals.)
  ✅ BIDIRECTIONAL — peer universities and exchange partners are fine
                 regardless of direction (since we exchange students).

Quick test: if the agency's homepage call-to-action is "Study in
[search-country]" or "Apply to [search-country] universities", it is
INBOUND and must be rejected. If it is "Study abroad", "Apply to
European / US / UK universities from [search-country]", it is OUTBOUND
and is exactly what we want.

============================================================
SCOPE — these are the ONLY categories we want to see:
============================================================
  ✅ Universities — public AND private, degree-granting peers globally.
                     Both research-heavy and teaching-focused.
                     (Directionality flexible — peer exchanges welcome.)
  ✅ Higher-ed institutions — business schools, design schools,
                     polytechnics, conservatories, applied-science unis.
  ✅ Schools — secondary / high schools whose graduates go to
                     INTERNATIONAL universities (feeder schools — IB
                     programs, international schools, private schools
                     with strong abroad-placement records).
  ✅ Student organizations + alumni networks — ESN chapters, AEGEE,
                     subject-specific student associations, mobility-
                     focused alumni groups.
  ✅ Education agencies — OUTBOUND ONLY. Study-abroad consultancies,
                     university placement agents that send local
                     students to foreign universities, IELTS/TOEFL prep
                     schools that funnel students abroad.

============================================================
OUT OF SCOPE — REJECT these even if the search results return them:
============================================================
  ❌ INBOUND education agencies — "Study in [country]" portals,
     foreign-student recruitment offices serving local universities.
     These are competitors. THIS IS THE MOST COMMON FALSE POSITIVE.
  ❌ Government investment / FDI / business-development agencies
     (e.g. ICEX, trade boards, chambers of commerce, "Invest in X"
     entities). They serve business setup, not students.
  ❌ Startup accelerators / VC funds — UNLESS they have an explicit
     academic affiliation or programs FOR university students.
  ❌ Generic consulting firms / law firms / professional services.
  ❌ Industry employers / corporations / SaaS companies. (We track
     companies separately as Career Day sponsors — that is a different
     workflow, do NOT surface them here.)
  ❌ Banks, insurance, real-estate firms unless they run a structured
     academic-scholarship or research-collaboration program.

  ❌ TOP RESEARCH UNIVERSITIES — H-FARM College is an APPLIED,
     project-based teaching institution. Reviewer's mandate: "We
     cannot partner with universities which do deep research, because
     they are looking normally for deep research as well." NEVER
     surface (even if asked broadly):
       MIT, Harvard, Stanford, Princeton, Yale, Caltech, Berkeley,
       Columbia, Cornell, UCLA, Chicago, Johns Hopkins, UPenn,
       Carnegie Mellon, Oxford, Cambridge, Imperial, LSE, UCL,
       ETH Zürich, EPFL, Sorbonne, Sciences Po, INSEAD, HEC Paris,
       Tsinghua, Peking University, NUS, Tokyo University.
     If a search result describes a university as "research-intensive",
     "deep research", "PhD-focused", "doctoral-focused", or
     "primarily research", treat that as a soft signal to deprioritise
     (fit_score under 30) and explain the mismatch in fit_reasoning.

  ❌ ITALIAN UNIVERSITIES — H-FARM College is Italian; other Italian
     universities are GEOGRAPHIC COMPETITORS for the same students.
     NEVER surface ANY Italian university as a partnership prospect
     (Bocconi, LUISS, Sapienza, Politecnico Milano/Torino, Bologna,
     Padova, Cattolica, Ca' Foscari, and every other Italian uni).
     Non-university Italian partners (high schools, student orgs,
     agencies, sponsors) ARE in scope — only Italian UNIVERSITIES
     are excluded.

============================================================
H-FARM PARTNERSHIP FIT — what to LEAN INTO:
============================================================
H-FARM College is APPLIED, hands-on, industry-linked. Best partners
share that DNA. When you find a candidate, boost fit_score for any of:
  ✅ Applied / project-based curricula
  ✅ Strong internship / company-visit programs
  ✅ Polytechnics, business schools, design schools, applied-science
     universities
  ✅ Industry partnership / dual-education programs
  ✅ Hands-on / experiential learning emphasis

And the LANGUAGE rule for matching summer programmes:
  • For INTERNATIONAL (non-Italian) partners → suggest only
    English-language H-FARM programmes (Startup Summer (AD)Venture,
    Defending Digital Worlds, Designing Connected Futures, AI Fashion
    Lab, Brand Building, World of AI, Emerals IRL).
  • The Italian-language programmes (Out of the Box, Finanza per il
    tuo futuro) are RELEVANT ONLY for Italian-speaking partners.
    Suggesting them to a Czech polytechnic or a Turkish business
    school is a known false positive — don't.

If a search result is CLEARLY one of these (e.g. its URL or title plainly
identifies it as an inbound "Study in X" portal or a SaaS employer),
skip it. But if you're UNSURE whether something is in-scope or
out-of-scope, prefer to surface it with a LOW fit_score (20-40) and an
honest "borderline because…" note in fit_reasoning. Empty results force
the user to refine queries blindly; weak-but-real candidates they can
quickly reject are strictly better.

============================================================
SPECIAL GUIDANCE FOR TYPE = "agency"
============================================================
"Agency" in this app means OUTBOUND education agency — a firm whose
core business is sending students from their local country to study at
universities abroad. In Turkey these are "yurtdışı eğitim danışmanlığı"
companies (Atayurt, Sage Group, ARC Education, Mojo, IDP Türkiye, etc.).
In Spain these are "agencias de estudios en el extranjero" or "estudia
en el extranjero" consultancies. Many such companies have small web
footprints — their site alone is enough grounding, you don't need a
major news source. They tend to rank for native-language queries, so
weak English presence is normal.

DO NOT surface "Study in [country]" portals, even if they're well-known.
Those are inbound and serve local universities. They are competitors.

State-run scholarship boards (Türkiye Bursları, Ministerio de
Universidades) can be in-scope ONLY if their primary mission is
sending LOCAL students abroad — most are actually inbound (bringing
foreigners to local universities). Read carefully before surfacing.

============================================================
REQUIRED OUTPUT: a JSON array of candidate objects, NEVER more than what
the search results actually support. NEVER invent facts. NEVER include a
candidate whose existence isn't supported by at least one URL in the
search results below.

Aim to surface 3-5 candidates per search if any are remotely plausible.
Use fit_score to communicate confidence:
   • 80-100 — exact match, clearly in-scope, strong fit
   • 50-79  — good fit, in-scope, minor reservations
   • 20-49  — borderline / weak grounding / partially in-scope (let the
              user decide; explain the uncertainty in fit_reasoning)
   • <20    — only return at this score if the user explicitly asked for
              a broad / exploratory scan
============================================================

SEARCH CRITERIA:
{crit_str}

EXISTING PARTNERS (DO NOT SUGGEST any of these — we already have them):
{exclude_str}

USER PREFERENCE PROFILE (from past decisions — lean STRONGLY toward
"strongly_prefers" + "softly_prefers", actively AVOID "rejects"):
{profile_str}

WEB SEARCH RESULTS (your ONLY source of truth — cite indices [N] in your
reasoning so the user can verify):
{src_str}

For each genuinely new IN-SCOPE prospect you find, produce:
- name:             official organization name as it appears in the source
- type:             university | agency | school | student_organization
- country:          ISO country name
- region:           continent (Europe / Asia / North America / etc.)
- primary_url:      the most authoritative URL from the search results
- description:      1-sentence summary of who they are
- fit_score:        integer 0-100, your honest assessment of fit
- fit_reasoning:    2-3 sentences citing search-result indices [N] explaining
                    why this fits H-FARM College (or doesn't). Be specific.
- source_urls:      array of at least 2 URLs from the search results
- suggested_programs: array of 1-3 H-FARM College program names that might match
                    (Coding Academy, Data & AI, Game Design, Fashion AI Lab,
                    Startup Summer, etc.) — leave [] if unsure

Return {{"candidates": []}} ONLY when (a) the search results are entirely
empty, or (b) every single result is clearly off-scope (e.g. all results
are SaaS employers, all are government FDI bodies). If even ONE result
is plausibly in-scope, surface it — let the human reject if needed.
Do NOT use empty results as a way to be "safe"; the user is competent
to filter borderline cases.
"""


def _filter_candidates(raw_candidates, existing_unis_names, db):
    """6-layer hallucination filter. Returns (filtered_list,
    verification_meta_per_survivor, rejection_breakdown).

    The third return value is a count-by-reason dict so the discover
    endpoint can tell the user EXACTLY where its candidates died (no
    source URLs vs URL dead vs already-in-pipeline vs already-surfaced).
    Empty 'surfaced' lists used to feel like silent failures — now the
    user gets a one-line diagnostic explaining what happened."""
    from rapidfuzz import fuzz

    # Pre-fetch past candidate names for dedupe (single DB hit)
    past_rows = db.execute("SELECT normalized_name FROM prospect_candidates").fetchall()
    past_names = {(r["normalized_name"] or "").strip() for r in past_rows if r["normalized_name"]}

    existing_norm = [_normalize_prospect_name(n) for n in (existing_unis_names or []) if n]

    out, verifications = [], []
    # Counters for the breakdown returned to the frontend. Keys match the
    # warning strings we add below — keep them stable so the UI can render
    # human labels off them.
    rejected = {
        "no_source_urls":  0,
        "url_dead":        0,
        "dup_unis":        0,
        "dup_past":        0,
        "no_name":         0,
    }
    # Sample rejected names so the UI can show "(e.g. Foo Inc, Bar Univ)"
    # — helps the user see whether Gemini was finding real things that
    # the filter killed, vs Gemini finding nothing in the first place.
    rejected_samples = {k: [] for k in rejected.keys()}

    def _note(reason, name):
        rejected[reason] += 1
        if len(rejected_samples[reason]) < 3 and name:
            rejected_samples[reason].append(name)

    for c in raw_candidates:
        name = (c.get("name") or "").strip()
        if not name:
            _note("no_name", "")
            continue
        verification = {
            "has_source":    False,
            "url_alive":     False,
            "ror_match":     None,
            "dup_unis":      False,
            "dup_past":      False,
            "all_passed":    False,
            "warnings":      [],
        }
        # Layer 1 — mandatory grounding
        srcs = [u for u in (c.get("source_urls") or []) if u and u.startswith(("http://", "https://"))]
        if not srcs:
            verification["warnings"].append("no source URLs")
            _note("no_source_urls", name)
            continue
        verification["has_source"] = True

        # Layer 2 — URL alive
        any_alive = False
        for u in srcs[:3]:
            if _check_url_alive(u):
                any_alive = True
                break
        verification["url_alive"] = any_alive
        if not any_alive:
            verification["warnings"].append("no live source URL")
            _note("url_dead", name)
            continue

        # Layer 3 — ROR cross-check (only for universities, others get NA)
        if (c.get("type") or "").lower() == "university":
            ror = _check_ror_existence(name, c.get("country"))
            verification["ror_match"] = ror
            if not ror:
                verification["warnings"].append("not in ROR — may be hallucinated or obscure")
                # Don't reject outright (small/new universities may not be in ROR),
                # but UI will badge "unverified"

        # Layer 4 — UNIS dedupe
        norm = _normalize_prospect_name(name)
        matched_existing = None
        for existing in existing_norm:
            if existing and fuzz.token_set_ratio(norm, existing) >= PROSPECT_FUZZY_THRESHOLD:
                verification["dup_unis"] = True
                matched_existing = existing
                break
        if verification["dup_unis"]:
            verification["warnings"].append("already in UNIS")
            # Show "candidate → matched-existing" so the user can spot
            # false positives at a glance (e.g. "Atayurt Consultancy → Atatürk University"
            # would obviously be wrong, even if it scored 90+).
            _note("dup_unis", f"{name} → {matched_existing or '?'}")
            continue

        # Layer 5 — past candidates dedupe
        if norm in past_names:
            verification["dup_past"] = True
            verification["warnings"].append("already surfaced previously")
            _note("dup_past", name)
            continue

        # If we got here, all hard layers passed
        verification["all_passed"] = True
        out.append(c)
        verifications.append((name, verification))

    breakdown = {
        "rejected_counts":  rejected,
        "rejected_samples": rejected_samples,
        "total_rejected":   sum(rejected.values()),
    }
    return out, verifications, breakdown


# ============================================================================
# P3 — SMART LEARNING LOOP
# ----------------------------------------------------------------------------
# Every time a user decides yes/no/maybe on a candidate, we accumulate a
# signal. Periodically (every 5 decisions or on manual rebuild) we distill
# those decisions into a SHORT JSON preference profile and store it in
# `prospect_user_profile` keyed by user handle. The next discovery call
# pulls that profile and injects it into the Gemini prompt, biasing the
# model toward what the user has historically said YES to and away from
# REJECTED patterns. This is what makes the system "learn" — without it,
# every search starts from scratch and the user repeatedly rejects the
# same off-scope categories (e.g. government FDI agencies, accelerators).
# ============================================================================
_DISTILL_AUTO_EVERY_N = 5   # rebuild profile every N decisions for a user


def _distill_user_profile(handle, db):
    """Pull this user's recent decisions, call Gemini to extract a
    preference profile (strongly_prefers / softly_prefers / rejects /
    notes), and upsert into prospect_user_profile.

    Returns a dict with `ok` and either `profile` or `error`. Safe to call
    inline — Gemini call is cheap (single prompt, ~2k tokens out)."""
    if not handle:
        return {"ok": False, "error": "no user handle"}
    if not GEMINI_KEY:
        return {"ok": False, "error": "GEMINI_API_KEY not configured"}

    # Pull last 50 decisions for this user, joined with the candidate row
    # so the LLM sees the actual entity attributes (type / country / fit /
    # description) — not just yes/no flags.
    rows = db.execute(
        """SELECT d.decision, d.reason, d.decided_at,
                  c.name, c.type, c.country, c.region, c.ai_fit_score, c.description
             FROM prospect_decisions d
             JOIN prospect_candidates c ON c.id = d.candidate_id
            WHERE d.decided_by = ?
            ORDER BY d.decided_at DESC
            LIMIT 50""",
        (handle,),
    ).fetchall()
    if not rows:
        return {"ok": False, "error": "no decisions yet for this user"}

    decisions_payload = []
    for r in rows:
        decisions_payload.append({
            "name":        r["name"],
            "type":        r["type"] or "",
            "country":     r["country"] or "",
            "region":      r["region"] or "",
            "fit_score":   r["ai_fit_score"],
            "description": (r["description"] or "")[:200],
            "decision":    r["decision"],
            "reason":      r["reason"] or "",
        })

    prompt = f"""You analyse a user's past partnership-prospect decisions
for H-FARM College (Italian higher-education institution) and distil a
SHORT, ACTIONABLE preference profile in STRICT JSON.

The profile feeds back into FUTURE discovery prompts to bias what the
model surfaces. Be specific, not generic. Cite concrete attributes
(country, entity type, directionality, focus area, mission) — not
feel-good fluff.

============================================================
INSTITUTIONAL PARTNERSHIP POLICY (this OVERRIDES personal preference)
============================================================
The user is one operator on the global partnerships team — their past
decisions are signal, but H-FARM College has FIXED institutional
partnership criteria that ALWAYS apply. If a past YES decision
contradicts policy, treat it as noise (a learning operator, not a
mandate). If a past NO decision aligns with policy, generalise it
forcefully. The profile MUST reflect the policy below, not the
operator's habits where they conflict.

H-FARM College is an APPLIED, hands-on, project-based teaching
institution. Its students learn by building products, visiting
companies, and running real projects with industry. It is NOT a
research-output university. Therefore:

  ✅ STRONG FIT — partners whose curricula are applied / industry-
     linked / project-based: business schools, polytechnics, design
     schools, applied-science universities, schools running
     internship + company-visit programs, hands-on engineering
     programs. Signal keywords: "applied", "internship",
     "industry partnership", "project-based", "company visits",
     "hands-on", "experiential learning", "polytechnic",
     "business school".

  ❌ STRONG ANTI-FIT — top research-intensive universities. They
     look for research-output peers, not applied teaching partners.
     Reviewer's exact framing: "We cannot partner with universities
     which do deep research, because they are looking normally for
     deep research as well". Specifically NEVER recommend, even if
     a past decision said YES:
       MIT, Harvard, Stanford, Princeton, Yale, Caltech, Berkeley,
       Columbia, Cornell, UCLA, Chicago, Johns Hopkins, UPenn,
       Carnegie Mellon, Oxford, Cambridge, Imperial, LSE, UCL,
       ETH Zürich, EPFL, Sorbonne, Sciences Po, INSEAD, HEC Paris,
       Tsinghua, Peking, NUS, Tokyo. Generalise to "top global
       research universities" in the profile.

  ❌ STRONG ANTI-FIT — Italian universities. They are GEOGRAPHIC
     COMPETITORS for the same students. Reviewer: "not interesting
     to have other Italian uni like Bocconi LUISS etc". This covers
     EVERY Italian university (Bocconi, LUISS, La Sapienza,
     Politecnico Milano / Torino, Bologna, Padova, Cattolica,
     Ca' Foscari, and any other Italian uni in the sheet).
     Non-university Italian partners (agencies, student orgs,
     high schools, sponsors) ARE in scope — only Italian
     UNIVERSITIES are excluded.

  ✅ LANGUAGE FILTER — for INTERNATIONAL partners (i.e. not Italian),
     English-language H-FARM programmes are the ONLY relevant pitch.
     Italian-language programmes (Out of the Box · Italian, Finanza
     per il tuo futuro · Italian) MUST NOT be surfaced as a match
     for any non-Italian-speaking partner. Reviewer caught this
     specifically: "Out of the Box (Italian) was marked interesting
     for many international universities, which is not — same
     applies to Finance".

If the operator's past decisions include any of the above as YES,
treat them as noise — DO NOT inherit them into strongly_prefers.
Instead surface them in `rejects` with the policy-aligned framing,
even if it contradicts the operator's clicked YES.

============================================================
KEY CONCEPT — DIRECTIONALITY (read before analysing)
============================================================
H-FARM College is in Italy and is the RECEIVING side of student
mobility. The user almost certainly cares about WHICH DIRECTION a
prospective partner serves:
  • OUTBOUND  — entities that send LOCAL students ABROAD (e.g. Turkish
                study-abroad consultancy, Spanish "agencia de estudios
                en el extranjero", IB high school whose graduates go to
                European universities). These are GOOD partners.
  • INBOUND   — entities that bring FOREIGN students INTO their own
                country (e.g. "Study in Turkey" portals, "Apply to
                Spanish Universities" agencies, foreign-student offices
                of local universities). These are COMPETITORS.
  • BIDIRECTIONAL — peer universities, exchange networks, ESN chapters.

If you see a NO decision on something whose name/description sounds
INBOUND (contains "Study in [country]", "Apply to [country]
Universities", "international students in [country]", recruitment INTO
local universities, etc.), do NOT just list that specific entity —
GENERALISE the pattern as: "INBOUND education agencies (those
recruiting foreign students INTO the local country instead of sending
local students abroad)". This is the single most useful generalisation
you can make from a rejection.

Similarly, if a YES decision is on something clearly OUTBOUND, surface
that as a positive directional pattern, not just the specific entity.

============================================================
PAST DECISIONS (most recent first):
============================================================
{json.dumps(decisions_payload, indent=2, ensure_ascii=False)}

Produce STRICT JSON in exactly this shape:
{{
  "strongly_prefers": [
    "<concrete pattern derived from YES decisions. Prefer DIRECTIONAL
     framing, e.g. 'outbound study-abroad consultancies in non-EU
     markets' or 'public research universities in EU with active
     Erasmus+ programs and design schools'>"
  ],
  "softly_prefers":   [ "<weaker, less-certain preferences>" ],
  "rejects":          [
    "<concrete anti-pattern derived from NO decisions. Prefer
     DIRECTIONAL framing where applicable, e.g. 'INBOUND education
     agencies — Study in X / Apply to X portals — they recruit
     foreigners INTO their local country instead of sending students
     out' or 'government FDI / investment-promotion agencies
     (ICEX-style)' or 'startup accelerators with no academic affiliation'>"
  ],
  "notes":            "<1-2 sentence overall summary of what this user
                       cares about — used as a steering preamble. Mention
                       directionality if the decisions show a clear
                       outbound preference.>"
}}

Rules:
- Max 5 items per list; QUALITY over quantity.
- Each item must be derivable from at least one decision above.
- Quote the reason text where possible — the user's own words are gold.
- GENERALISE: don't list "rejected 'Study in Turkiye'" — instead infer
  the underlying pattern and list "rejected: INBOUND education agencies".
  One generalised pattern blocks many future false positives; one
  specific entity blocks only that exact name.
- **GEOGRAPHIC NEUTRALITY**: do NOT narrow the geography in the `notes`
  summary based on which countries happened to appear in the small
  decision sample. A search that happened to surface Asian universities
  doesn't mean the operator only wants Asia — it means Asia is where
  they searched that week. Keep geographic bias OUT of the `notes`
  preamble UNLESS the decisions clearly show the operator rejecting
  several entities from one region for region-specific reasons.
  Geographic mentions belong in `softly_prefers` (as a hint), not the
  summary or `strongly_prefers`.
- The `notes` summary should reflect H-FARM's APPLIED-fit mission +
  the directional/quality patterns from decisions — NOT specific
  countries or markets. Reviewer wants this AI to surface partners
  GLOBALLY where applied-fit + outbound conditions are met.
- If a category is empty, return [] (not null).
- No commentary outside the JSON object.
"""

    finish_reason = None
    try:
        from google.genai import types as _gtypes
        client_ai = genai.Client(api_key=GEMINI_KEY)
        resp = client_ai.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=_gtypes.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                # 8192 covers the richer institutional-policy preamble +
                # 5-item lists. Previous 4096 truncated to ~700 chars of
                # JSON because gemini-2.5-flash burned most of the budget
                # on internal "thinking" — see thinking_config below.
                max_output_tokens=8192,
                # gemini-2.5-flash enables thinking by default. For a
                # structured-JSON distillation task we don't need
                # internal chain-of-thought — disabling frees the entire
                # output budget for the actual JSON response. Same fix
                # already in /api/chat-query at line ~1015.
                thinking_config=_gtypes.ThinkingConfig(thinking_budget=0),
            ),
        )
        # Defensive multi-part concat (same trick as discover) + capture
        # finish_reason so the diagnostic can explain WHY it failed
        # (truncation vs safety vs recitation vs straight non-JSON).
        text_parts = []
        if getattr(resp, "candidates", None):
            for cand in resp.candidates:
                if finish_reason is None:
                    finish_reason = str(getattr(cand, "finish_reason", "") or "")
                content = getattr(cand, "content", None)
                parts = getattr(content, "parts", None) if content else None
                if parts:
                    for p in parts:
                        t = getattr(p, "text", None)
                        if t:
                            text_parts.append(t)
        concat_text = "".join(text_parts).strip()
        fallback_text = (resp.text or "").strip() if hasattr(resp, "text") else ""
        raw_text = concat_text if len(concat_text) >= len(fallback_text) else fallback_text
        print(f"[distill] Gemini: finish={finish_reason!r} text={len(raw_text)} chars; first 300: {raw_text[:300]!r}")
    except Exception as e:
        import traceback as _tb
        print(f"[distill] Gemini call failed: {e}\n{_tb.format_exc()}")
        return {"ok": False, "error": f"Gemini call failed: {e}",
                "exception_type": type(e).__name__}

    # Loose JSON parse — fenced, mixed-text, list-wrapped, or pure
    s = (raw_text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s).strip()
    profile = None
    parse_err = None
    try:
        profile = json.loads(s)
    except Exception as pe:
        parse_err = str(pe)
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            try:
                profile = json.loads(s[i:j+1])
                parse_err = None
            except Exception as pe2:
                parse_err = str(pe2)
    # Sometimes Gemini wraps the dict in a list — accept that too
    if isinstance(profile, list) and profile and isinstance(profile[0], dict):
        profile = profile[0]
    if not isinstance(profile, dict):
        # Hint at root cause based on finish_reason
        fr_upper = (finish_reason or "").upper()
        hint = ""
        if "MAX_TOKENS" in fr_upper:
            hint = "Output was truncated mid-JSON — max_output_tokens hit. Code-side fix needed."
        elif "SAFETY" in fr_upper:
            hint = "Safety filter blocked the output. Try removing the most recent NO reason from a decision and retry."
        elif "RECITATION" in fr_upper:
            hint = "Blocked for recitation risk — rephrase the most-quoted decision reason."
        elif not (raw_text or "").strip():
            hint = "Gemini returned EMPTY output. Could be a transient model glitch — try Rebuild again in a few seconds."
        else:
            hint = "Output wasn't valid JSON. Check the raw_preview below to see what Gemini actually returned."
        print(f"[distill] parse failed: parse_err={parse_err!r} finish={finish_reason!r} raw={raw_text[:600]!r}")
        return {"ok":            False,
                "error":         "Gemini returned non-JSON profile",
                "raw_preview":   (raw_text or "")[:600],
                "raw_length":    len(raw_text or ""),
                "parse_error":   parse_err,
                "finish_reason": finish_reason,
                "hint":          hint}

    # Normalise shape so downstream code never has to guess
    def _str_list(v):
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip()][:5]

    profile = {
        "strongly_prefers": _str_list(profile.get("strongly_prefers")),
        "softly_prefers":   _str_list(profile.get("softly_prefers")),
        "rejects":          _str_list(profile.get("rejects")),
        "notes":            str(profile.get("notes") or "").strip()[:400],
    }
    decision_count = len(decisions_payload)
    now = _now_iso()
    profile_json_str = json.dumps(profile, ensure_ascii=False)

    # Upsert (same ON CONFLICT syntax works in SQLite ≥3.24 and Postgres)
    db.execute(
        """INSERT INTO prospect_user_profile
               (user_handle, profile_json, distilled_at, decision_count)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_handle) DO UPDATE SET
               profile_json   = excluded.profile_json,
               distilled_at   = excluded.distilled_at,
               decision_count = excluded.decision_count""",
        (handle, profile_json_str, now, decision_count),
    )
    db.commit()
    return {
        "ok":             True,
        "user_handle":    handle,
        "profile":        profile,
        "distilled_at":   now,
        "decision_count": decision_count,
    }


def _prospect_row_to_dict(row):
    if not row:
        return None
    try:
        source_urls = json.loads(row["source_urls"]) if row["source_urls"] else []
    except Exception:
        source_urls = []
    try:
        suggested_programs = json.loads(row["suggested_programs"]) if row["suggested_programs"] else []
    except Exception:
        suggested_programs = []
    try:
        verification = json.loads(row["verification"]) if row["verification"] else {}
    except Exception:
        verification = {}
    return {
        "id":                  row["id"],
        "name":                row["name"],
        "normalized_name":     row["normalized_name"],
        "type":                row["type"] or "",
        "country":             row["country"] or "",
        "region":              row["region"] or "",
        "primary_url":         row["primary_url"] or "",
        "description":         row["description"] or "",
        "ai_reasoning":        row["ai_reasoning"] or "",
        "ai_fit_score":        row["ai_fit_score"],
        "source_urls":         source_urls,
        "suggested_programs":  suggested_programs,
        "verification":        verification,
        "discovered_at":       row["discovered_at"],
        "discovered_via":      row["discovered_via"] or "",
        "status":              row["status"] or "pending",
        "approved_entity_id":  row["approved_entity_id"] or "",
        "search_run_id":       row["search_run_id"] or "",
        # Surface the policy push date so the frontend can flag pre-policy
        # candidates that need re-evaluation. Comparing discovered_at <
        # POLICY_PUSH_AT tells the UI "this candidate's score was generated
        # before the May 2026 institutional-policy update".
        "policy_push_at":      POLICY_PUSH_AT,
        "is_pre_policy":       (row["discovered_at"] or "") < POLICY_PUSH_AT,
    }


@app.route("/api/prospects", methods=["GET"])
@auth_required
def prospects_list():
    """List candidates; filters: status, type, country, search_run_id."""
    status  = (request.args.get("status") or "").strip()
    ptype   = (request.args.get("type")   or "").strip()
    country = (request.args.get("country") or "").strip()
    run_id  = (request.args.get("run_id") or "").strip()
    clauses, params = [], []
    if status:  clauses.append("status = ?");        params.append(status)
    if ptype:   clauses.append("type = ?");          params.append(ptype)
    if country: clauses.append("country LIKE ?");    params.append(f"%{country}%")
    if run_id:  clauses.append("search_run_id = ?"); params.append(run_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT * FROM prospect_candidates " + where + " "
        "ORDER BY CASE status WHEN 'pending' THEN 1 WHEN 'maybe' THEN 2 "
        "                     WHEN 'approved' THEN 3 ELSE 4 END, "
        "         ai_fit_score DESC, discovered_at DESC"
    )
    db = get_db()
    rows = db.execute(sql, tuple(params)).fetchall()
    return jsonify({"prospects": [_prospect_row_to_dict(r) for r in rows]})


@app.route("/api/prospects/<prospect_id>", methods=["GET"])
@auth_required
def prospects_get(prospect_id):
    db = get_db()
    row = db.execute("SELECT * FROM prospect_candidates WHERE id = ?", (prospect_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    # Also fetch decisions for this candidate
    decisions = db.execute(
        "SELECT id, decision, reason, decided_at, decided_by "
        "FROM prospect_decisions WHERE candidate_id = ? "
        "ORDER BY decided_at DESC", (prospect_id,)
    ).fetchall()
    out = _prospect_row_to_dict(row)
    out["decisions"] = [dict(d) for d in decisions]
    return jsonify(out)


@app.route("/api/prospects/discover", methods=["POST"])
@auth_required
def prospects_discover():
    """Run a single discovery cycle. Body:
       { query: str, type?: str, country?: str, focus?: str, limit?: int,
         existing_entity_names?: [str] }
    Returns the search run + the candidates surfaced.
    """
    if not GEMINI_KEY:
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 500
    if not TAVILY_API_KEY:
        return jsonify({"error": "TAVILY_API_KEY not configured — set it in Render env vars"}), 500

    body = request.get_json(force=True, silent=True) or {}
    criteria = {
        "query":   (body.get("query")   or "").strip(),
        "type":    (body.get("type")    or "").strip(),
        "country": (body.get("country") or "").strip(),
        "focus":   (body.get("focus")   or "").strip(),
    }
    limit = max(1, min(15, int(body.get("limit") or 5)))
    existing_names = body.get("existing_entity_names") or []

    # Compose the search query — type + country + free-text combined.
    # CRITICAL for type=agency: bias hard toward OUTBOUND (local students
    # going abroad) — inbound agencies like "Study in Turkey" are
    # competitors, not partners. We do this two ways:
    #   1) English search terms include "study abroad" + "sending students"
    #   2) Add native-language search term for known countries — outbound
    #      consultancies often have weak English web presence.
    search_terms = []
    if criteria["type"] == "university":
        search_terms.append("universities")
    elif criteria["type"] == "agency":
        # Directional English (Tavily understands both English and local
        # language results — adding both terms broadens recall)
        search_terms.append('"study abroad" agencies sending students to European universities')
    elif criteria["type"] == "school":
        # Schools that send graduates to international universities —
        # IB / international / private schools are the strongest pipeline.
        search_terms.append("international high schools whose graduates study abroad")
    elif criteria["type"] == "student_organization":
        search_terms.append("student organizations international mobility Erasmus")
    else:
        search_terms.append("partnership prospects")

    if criteria["country"]:
        search_terms.append("in " + criteria["country"])

    # Native-language outbound-agency boost. Many top-tier outbound
    # consultancies in non-English markets only rank for native-language
    # search ("yurtdışı eğitim danışmanlığı" finds 100× more relevant
    # firms than "Turkish education agency"). Only fires for type=agency.
    if criteria["type"] == "agency" and criteria["country"]:
        native = {
            "turkey":   "yurtdışı eğitim danışmanlığı",
            "türkiye":  "yurtdışı eğitim danışmanlığı",
            "spain":    "agencia de estudios en el extranjero",
            "españa":   "agencia de estudios en el extranjero",
            "italy":    "agenzia studio all'estero",
            "italia":   "agenzia studio all'estero",
            "germany":  "Auslandsstudium Beratung",
            "deutschland": "Auslandsstudium Beratung",
            "france":   "agence études à l'étranger",
            "poland":   "agencja studiów za granicą",
            "polska":   "agencja studiów za granicą",
            "greece":   "πρακτορείο σπουδών στο εξωτερικό",
            "ελλάδα":   "πρακτορείο σπουδών στο εξωτερικό",
            "japan":    "留学エージェント",
            "china":    "留学中介",
            "korea":    "유학원",
            "south korea": "유학원",
            "vietnam":  "tư vấn du học",
            "thailand": "เอเจนซี่เรียนต่อต่างประเทศ",
            "indonesia": "konsultan pendidikan luar negeri",
            "brazil":   "intercâmbio agência",
            "brasil":   "intercâmbio agência",
            "mexico":   "agencia de intercambio educativo",
            "méxico":   "agencia de intercambio educativo",
        }.get((criteria["country"] or "").strip().lower())
        if native:
            search_terms.append(native)

    if criteria["focus"]:                    search_terms.append("focusing on " + criteria["focus"])
    if criteria["query"]:                    search_terms.append(criteria["query"])
    final_query = " ".join(search_terms).strip() or "international higher-education partnerships"

    # ----- Step 1: Tavily web search -----
    search_results = _tavily_search(final_query, max_results=max(8, limit * 2))
    raw_count = len(search_results)
    if not search_results:
        return jsonify({"ok": False, "error": "no search results — Tavily returned empty",
                        "criteria": criteria, "query": final_query}), 200

    # ----- Step 2: Gemini structured output (no fancy schema; ask for JSON) -----
    # Pull the user's preference profile if we have one
    user_handle = (request.headers.get("X-Display-Name") or "").strip()
    profile_json = None
    if user_handle:
        try:
            pr = get_db().execute(
                "SELECT profile_json FROM prospect_user_profile WHERE user_handle = ?",
                (user_handle,),
            ).fetchone()
            if pr and pr["profile_json"]:
                profile_json = pr["profile_json"]
        except Exception:
            pass

    prompt = _build_prospect_prompt(criteria, search_results, existing_names, profile_json)

    # Gemini call. We keep response_mime_type='application/json' as a soft
    # hint and rely on the defensive parser below for the actual contract.
    # Two layers of defense beyond that:
    #   (a) concat ALL parts of the response (resp.text occasionally only
    #       returns the first chunk on multi-part outputs — caused mid-
    #       string JSON truncation in the wild)
    #   (b) inspect finish_reason — STOP is the happy path; MAX_TOKENS
    #       means bump max_output_tokens; SAFETY / RECITATION need a
    #       different fix entirely. We surface this in the error JSON
    #       so the frontend can show it without a Render-log dive.
    try:
        from google.genai import types as _gtypes
        client_ai = genai.Client(api_key=GEMINI_KEY)
        resp = client_ai.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=_gtypes.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
                max_output_tokens=8192,   # was 4096 — bumped to be safe
            ),
        )
        # Manually concat every part across every candidate (works around
        # the SDK quirk where resp.text only returns the first chunk).
        text_parts = []
        finish_reason = None
        if getattr(resp, "candidates", None):
            for cand in resp.candidates:
                if finish_reason is None:
                    finish_reason = str(getattr(cand, "finish_reason", "") or "")
                content = getattr(cand, "content", None)
                parts = getattr(content, "parts", None) if content else None
                if parts:
                    for p in parts:
                        t = getattr(p, "text", None)
                        if t:
                            text_parts.append(t)
        concat_text = "".join(text_parts).strip()
        fallback_text = (resp.text or "").strip() if hasattr(resp, "text") else ""
        # Use whichever is longer — the parts concat almost always wins on
        # multi-part responses, but fall back to .text if parts came up empty.
        raw_text = concat_text if len(concat_text) >= len(fallback_text) else fallback_text
        print(f"[prospects] Gemini: finish={finish_reason!r} text={len(raw_text)} chars "
              f"(parts={len(concat_text)} / .text={len(fallback_text)}); first 300: {raw_text[:300]!r}")
    except Exception as e:
        import traceback as _tb
        print(f"[prospects] Gemini call failed: {e}\n{_tb.format_exc()}")
        return jsonify({"ok": False, "error": f"Gemini call failed: {e}",
                        "stage": "gemini_call",
                        "exception_type": type(e).__name__}), 500

    # Defensive JSON parsing — even with response_schema set, models
    # occasionally wrap output in markdown fences or prepend explanatory text.
    # We handle: pure JSON / fenced JSON / JSON inside mixed text /
    # plain-text "no candidates" fallback.
    def _parse_loose_json(text):
        if not text:
            return {"candidates": []}
        s = text.strip()
        # Strip ```json … ``` (or ``` … ```) fences
        if s.startswith("```"):
            s = re.sub(r"^```(?:json)?\s*", "", s)
            s = re.sub(r"\s*```\s*$", "", s)
            s = s.strip()
        # Direct parse
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
        # Find first { and last } and try the slice
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(s[i:j+1])
            except json.JSONDecodeError:
                pass
        # "No results" plain-text fallback
        low = s.lower()
        if any(p in low for p in ["no candidate", "no suitable", "no relevant", "no new", "no prospect", "could not find"]):
            return {"candidates": []}
        raise ValueError("could not extract JSON")

    try:
        parsed = _parse_loose_json(raw_text)
        # Gemini sometimes returns the array directly ([...]) and sometimes
        # the wrapper object ({"candidates": [...]}) — accept both.
        if isinstance(parsed, list):
            raw_candidates = parsed
        elif isinstance(parsed, dict):
            raw_candidates = parsed.get("candidates") or []
            # Or sometimes a single candidate object without the wrapper —
            # detect via the presence of a "name" field.
            if not raw_candidates and parsed.get("name"):
                raw_candidates = [parsed]
        else:
            raw_candidates = []
        if not isinstance(raw_candidates, list):
            raw_candidates = []
    except Exception as e:
        print(f"[prospects] JSON parse failed: {e}; finish={finish_reason!r}; raw: {raw_text[:600]}")
        # Friendlier hint when the model bailed early due to length/safety
        hint = ""
        fr = (finish_reason or "").upper()
        if "MAX_TOKENS" in fr:
            hint = "Model hit max_output_tokens — bump the cap in app.py or ask for fewer candidates."
        elif "SAFETY" in fr:
            hint = "Response was blocked by safety filter — try a less sensitive query."
        elif "RECITATION" in fr:
            hint = "Response blocked for recitation risk — rephrase the query."
        return jsonify({"ok": False,
                        "error":       "Gemini returned non-JSON output",
                        "raw_preview": raw_text[:600],
                        "raw_length":  len(raw_text),
                        "parse_error": str(e),
                        "finish_reason": finish_reason,
                        "hint":        hint or None}), 500

    # ----- Step 3: 6-layer hallucination filter -----
    db = get_db()
    filtered, verifications, filter_breakdown = _filter_candidates(raw_candidates, existing_names, db)
    filtered_count = len(filtered)

    # ----- Step 4: Persist surfaced candidates -----
    run_id = "run-" + _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    now    = _now_iso()
    user_label = user_handle or None
    via = "manual:" + (criteria["query"] or final_query)[:120]

    inserted_ids = []
    for c, (_n, ver) in zip(filtered, verifications):
        norm = _normalize_prospect_name(c.get("name") or "")
        cid  = "prospect-" + secrets.token_hex(6)
        db.execute(
            "INSERT INTO prospect_candidates "
            "(id, name, normalized_name, type, country, region, primary_url, description, "
            " ai_reasoning, ai_fit_score, source_urls, suggested_programs, verification, "
            " discovered_at, discovered_via, status, search_run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cid, c.get("name"), norm, (c.get("type") or "").lower(),
                c.get("country"), c.get("region"),
                c.get("primary_url"), c.get("description"),
                c.get("fit_reasoning"), int(c.get("fit_score") or 0),
                json.dumps(c.get("source_urls") or []),
                json.dumps(c.get("suggested_programs") or []),
                json.dumps(ver),
                now, via, "pending", run_id,
            ),
        )
        inserted_ids.append(cid)

    # ----- Step 5: Audit row for this run -----
    db.execute(
        "INSERT INTO prospect_search_runs "
        "(id, trigger_kind, query, criteria, raw_count, filtered_count, surfaced_count, "
        " cost_estimate, run_at, run_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, "manual", final_query, json.dumps(criteria),
         raw_count, len(raw_candidates), filtered_count,
         0.05,    # rough Tavily + Gemini cost estimate per run
         now, user_label),
    )
    db.commit()
    _record_edit("prospect", "discover", run_id)

    # Return the surfaced candidates
    surfaced = []
    for cid in inserted_ids:
        row = db.execute("SELECT * FROM prospect_candidates WHERE id = ?", (cid,)).fetchone()
        surfaced.append(_prospect_row_to_dict(row))
    # Build a human-readable diagnostic so the frontend can show WHY
    # `surfaced` may be lower than expected — especially when it's 0.
    # We bubble up: (a) the Tavily count, (b) what Gemini proposed,
    # (c) per-reason rejection counts from the filter, plus top-3
    # Tavily titles so the user can sanity-check the search itself.
    tavily_preview = [
        {"title": (r.get("title") or "")[:120],
         "url":   r.get("url", ""),
         "snippet": (r.get("content") or "")[:160]}
        for r in (search_results or [])[:5]
    ]
    return jsonify({
        "ok":               True,
        "run_id":           run_id,
        "query":            final_query,
        "raw_count":        raw_count,
        "ai_proposed":      len(raw_candidates),
        "surfaced":         filtered_count,
        "candidates":       surfaced,
        "filter_breakdown": filter_breakdown,
        "tavily_preview":   tavily_preview,
    })


@app.route("/api/prospects/<prospect_id>/decide", methods=["POST"])
@auth_required
def prospects_decide(prospect_id):
    """Record a yes/no/maybe decision + optional reason. Updates the
    candidate's status. Feeds the learning loop (profile distillation
    is built on top of these rows)."""
    body = request.get_json(force=True, silent=True) or {}
    decision = (body.get("decision") or "").strip().lower()
    if decision not in ("yes", "no", "maybe"):
        return jsonify({"error": "decision must be yes / no / maybe"}), 400
    reason = (body.get("reason") or "").strip()
    db = get_db()
    cand = db.execute("SELECT id FROM prospect_candidates WHERE id = ?", (prospect_id,)).fetchone()
    if not cand:
        return jsonify({"error": "candidate not found"}), 404
    decided_by = (request.headers.get("X-Display-Name") or "").strip() or None
    did = "decision-" + secrets.token_hex(5)
    now = _now_iso()
    db.execute(
        "INSERT INTO prospect_decisions (id, candidate_id, decision, reason, decided_at, decided_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (did, prospect_id, decision, reason or None, now, decided_by),
    )
    # Update candidate status. "yes" stays as "pending" until explicit /approve
    # creates the UNIS entry; we use a separate transition to be deliberate.
    new_status = {"yes": "maybe", "no": "rejected", "maybe": "maybe"}[decision]
    # If user said yes, mark as "approved-intent" via the maybe bucket until
    # they explicitly hit Approve; that way "yes" alone never auto-creates UNIS.
    if decision == "yes":
        new_status = "maybe"   # caller should follow up with /approve
    db.execute("UPDATE prospect_candidates SET status = ? WHERE id = ?", (new_status, prospect_id))
    db.commit()
    _record_edit("prospect", "decide", prospect_id)

    # P3 — auto-distil the user's preference profile every Nth decision.
    # We do this inline (no background worker) — Gemini call is cheap and
    # the user benefits immediately on their next discovery. If it fails,
    # we swallow the error so it never breaks the decision flow.
    distill_meta = None
    if decided_by:
        try:
            count_row = db.execute(
                "SELECT COUNT(*) AS n FROM prospect_decisions WHERE decided_by = ?",
                (decided_by,),
            ).fetchone()
            total = (count_row["n"] if count_row else 0) or 0
            if total and total % _DISTILL_AUTO_EVERY_N == 0:
                res = _distill_user_profile(decided_by, db)
                distill_meta = {
                    "triggered":     True,
                    "ok":            bool(res.get("ok")),
                    "decision_count": res.get("decision_count"),
                    "error":         res.get("error"),
                }
                print(f"[prospects] auto-distil for {decided_by!r} after {total} decisions: ok={res.get('ok')}")
        except Exception as e:
            print(f"[prospects] auto-distil failed silently: {e}")
            distill_meta = {"triggered": True, "ok": False, "error": str(e)}

    return jsonify({
        "ok": True,
        "decision_id": did,
        "new_status": new_status,
        "profile_distilled": distill_meta,
    })


# ============================================================================
# POLICY_PUSH_AT — UTC date of the May 2026 institutional-policy update.
# Prospect candidates discovered before this carry AI scoring that was
# done under the OLD (wrong) profile bias (research-intensive preference,
# Italian competitors not excluded, etc.). The /reeval endpoint lets the
# operator one-click re-score them with the current policy-aligned prompt.
# Frontend uses this same string to show a "pre-policy" warning badge.
# ============================================================================
POLICY_PUSH_AT = "2026-05-25"


@app.route("/api/prospects/<prospect_id>/reeval", methods=["POST"])
@auth_required
def prospects_reeval(prospect_id):
    """Re-score one prospect candidate with the CURRENT discovery prompt
    (which now carries the institutional policy preamble: applied-fit,
    no research-intensive, no Italian universities, English-language
    rule for international partners).

    Reads the candidate's existing name + description + source URLs out
    of prospect_candidates, builds a minimal prompt asking Gemini to
    return ONLY {fit_score, fit_reasoning, suggested_programs} for THIS
    one row, then updates the DB. Lighter than re-running a full
    discovery (no Tavily search, no candidate extraction loop)."""
    if not GEMINI_KEY:
        return jsonify({"ok": False, "error": "GEMINI_API_KEY not configured"}), 503
    db = get_db()
    row = db.execute(
        "SELECT * FROM prospect_candidates WHERE id = ?",
        (prospect_id,),
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "candidate not found"}), 404

    # Build a focused single-candidate prompt that carries the same
    # institutional policy as the full discovery prompt. We don't need
    # web search results — the candidate already exists in the DB with
    # its description + sources, so we score from those.
    name        = row["name"]
    rtype       = row["type"] or "university"
    country     = row["country"] or ""
    region      = row["region"] or ""
    description = row["description"] or ""
    primary_url = row["primary_url"] or ""
    try:
        existing_sources = json.loads(row["source_urls"] or "[]")
    except Exception:
        existing_sources = []
    src_str = "\n".join(f"- {u}" for u in (existing_sources[:5] or [primary_url]) if u) or "(none)"

    prompt = f"""You re-score ONE partnership prospect for H-FARM College using
the CURRENT institutional partnership policy. Older scores in the DB
were generated under a prior (incorrect) preference bias and need
refreshing.

============================================================
INSTITUTIONAL POLICY (the only thing that matters here)
============================================================
H-FARM College is an APPLIED, hands-on, project-based teaching
institution. Best fits share that DNA. NEVER recommend top research
universities — they look for research peers, not us. Specifically
AVOID even if surfaced:
  MIT, Harvard, Stanford, Princeton, Yale, Caltech, Berkeley,
  Columbia, Cornell, UCLA, Chicago, Johns Hopkins, UPenn, Carnegie
  Mellon, Oxford, Cambridge, Imperial, LSE, UCL, ETH Zürich, EPFL,
  Sorbonne, Sciences Po, INSEAD, HEC Paris, Tsinghua, Peking, NUS,
  Tokyo. If the candidate is in this group → fit_score 5-15 with
  reasoning explaining the policy mismatch.

NEVER recommend Italian universities (Bocconi, LUISS, Sapienza,
Politecnico Milano / Torino, Bologna, Padova, Cattolica, Ca' Foscari,
any other Italian uni) — geographic competitors. → fit_score 5-15.

For everyone else:
  ✅ APPLIED FIT boost: business schools, polytechnics, design
     schools, applied-science universities, schools with strong
     internship + company-visit + project-based programmes
  ✅ OUTBOUND-direction agencies (sending local students abroad)
  ❌ INBOUND-direction agencies ("Study in X" portals — competitors)

For international (non-Italian-speaking) partners, only English-
language H-FARM programmes are valid pitches. Italian-only
programmes (Out of the Box, Finanza per il tuo futuro) MUST NOT
be suggested for them.

============================================================
CANDIDATE TO RE-SCORE
============================================================
Name:        {name}
Type:        {rtype}
Country:     {country}
Region:      {region}
Description: {description}
Sources:
{src_str}

Return STRICT JSON in this exact shape (no commentary, no markdown):
{{
  "fit_score": <integer 0-100, your policy-aligned score>,
  "fit_reasoning": "<2-3 sentences explaining the score, citing the
                    policy where relevant. Be specific. If you're
                    downgrading from a previous high score, say so
                    and why.>",
  "suggested_programs": ["<H-FARM programme name>", ...]
}}

Fit-score guide under current policy:
  80-100 — strong applied fit, clear outbound direction, no policy violations
  50-79  — moderate fit, useful partner with some reservations
  20-49  — weak fit, policy concerns OR borderline relevance
  0-19   — policy violation (research-intensive top, Italian university,
           inbound agency, out-of-scope corporation)
"""

    try:
        from google.genai import types as _gtypes
        client_ai = genai.Client(api_key=GEMINI_KEY)
        resp = client_ai.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=_gtypes.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=2048,
                thinking_config=_gtypes.ThinkingConfig(thinking_budget=0),
            ),
        )
        text_parts = []
        if getattr(resp, "candidates", None):
            for cand in resp.candidates:
                content = getattr(cand, "content", None)
                parts = getattr(content, "parts", None) if content else None
                if parts:
                    for p in parts:
                        t = getattr(p, "text", None)
                        if t:
                            text_parts.append(t)
        raw_text = ("".join(text_parts) or getattr(resp, "text", "") or "").strip()
        if raw_text.startswith("```"):
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
            raw_text = re.sub(r"\s*```\s*$", "", raw_text).strip()
        data = json.loads(raw_text)
    except Exception as e:
        return jsonify({
            "ok":            False,
            "error":         f"Re-eval failed: {e}",
            "exception_type": type(e).__name__,
        }), 502

    new_score = int(data.get("fit_score") or 0)
    new_score = max(0, min(100, new_score))
    new_reasoning = str(data.get("fit_reasoning") or "").strip()[:2000]
    raw_programs = data.get("suggested_programs") or []
    if not isinstance(raw_programs, list):
        raw_programs = []
    new_programs = [str(p).strip() for p in raw_programs if str(p).strip()][:5]

    db.execute(
        "UPDATE prospect_candidates SET ai_fit_score = ?, ai_reasoning = ?, "
        "suggested_programs = ? WHERE id = ?",
        (new_score, new_reasoning, json.dumps(new_programs), prospect_id),
    )
    db.commit()
    _record_edit("prospect", "reeval", prospect_id)

    return jsonify({
        "ok": True,
        "id": prospect_id,
        "fit_score": new_score,
        "fit_reasoning": new_reasoning,
        "suggested_programs": new_programs,
        "policy_version": POLICY_PUSH_AT,
    })


@app.route("/api/prospects/profile/<handle>", methods=["GET"])
@auth_required
def prospects_profile_get(handle):
    """Read the current preference profile for a user. Returns 404 if
    the user has never had a profile distilled (i.e. <5 decisions)."""
    db = get_db()
    row = db.execute(
        "SELECT user_handle, profile_json, distilled_at, decision_count "
        "FROM prospect_user_profile WHERE user_handle = ?",
        (handle,),
    ).fetchone()
    if not row:
        # Tell the caller how many decisions are needed before the first
        # distil — UX uses this to show "Learning from 2/5 decisions".
        dc_row = db.execute(
            "SELECT COUNT(*) AS n FROM prospect_decisions WHERE decided_by = ?",
            (handle,),
        ).fetchone()
        return jsonify({
            "ok":             False,
            "exists":         False,
            "user_handle":    handle,
            "decision_count": (dc_row["n"] if dc_row else 0) or 0,
            "threshold":      _DISTILL_AUTO_EVERY_N,
        }), 200
    try:
        profile = json.loads(row["profile_json"])
    except Exception:
        profile = {}
    return jsonify({
        "ok":             True,
        "exists":         True,
        "user_handle":    row["user_handle"],
        "profile":        profile,
        "distilled_at":   row["distilled_at"],
        "decision_count": row["decision_count"],
        "threshold":      _DISTILL_AUTO_EVERY_N,
    })


@app.route("/api/prospects/profile/<handle>/distill", methods=["POST"])
@auth_required
def prospects_profile_distill(handle):
    """Manually rebuild the profile for a user (bypasses the every-N
    auto-trigger). Useful right after the user changes their mind or
    flips a lot of decisions in one sitting."""
    db = get_db()
    res = _distill_user_profile(handle, db)
    if not res.get("ok"):
        return jsonify(res), 400 if res.get("error") == "no decisions yet for this user" else 500
    _record_edit("prospect", "distill", handle)
    return jsonify(res)


def _build_provenance_notes(rec, user_handle):
    """Compose a human-readable provenance block that gets written into
    the entity's `notes` field on approval. This is what ends up in the
    Google Sheet so anyone looking at the row sees clearly that this
    was AI-discovered + when + by whom + with what evidence — not a
    human-entered partner.

    Format is deliberately greppable: a teammate scanning the Sheet for
    "AI-DISCOVERED" can filter all AI-sourced rows instantly."""
    when = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    src_urls = rec.get("source_urls") or []
    ver = rec.get("verification") or {}
    ver_bits = []
    if ver.get("all_passed"):    ver_bits.append("all hallucination checks passed")
    if ver.get("ror_match"):     ver_bits.append("ROR cross-checked")
    if ver.get("url_alive"):     ver_bits.append("source URLs live")
    ver_str = "; ".join(ver_bits) if ver_bits else "no verification metadata"
    lines = [
        "🤖 AI-DISCOVERED · approved by " + (user_handle or "an unknown user") + " on " + when,
        "Source: Prospect Discovery (Tavily web search + Gemini 2.5 Flash analysis)",
        "Verification: " + ver_str,
        "AI fit score: " + str(rec.get("ai_fit_score") or "?") + "/100",
        "",
        "AI reasoning:",
        (rec.get("ai_reasoning") or "(none)").strip(),
        "",
        "Source URLs:",
    ]
    if src_urls:
        for u in src_urls[:5]:
            lines.append("  • " + u)
    else:
        lines.append("  (none)")
    if rec.get("description"):
        lines.append("")
        lines.append("Description: " + rec["description"].strip())
    lines.append("")
    lines.append("---")
    lines.append("(Human-edited notes go below this divider)")
    return "\n".join(lines)


def _push_new_entity_to_sheets(entity_id, fields):
    """Append a new entity row to the linked Google Sheet via Apps Script
    `op: append`. Returns {ok, configured, error?, ...} — best-effort,
    never raises. Callers should surface the result to the user but NOT
    block their flow if Sheets is down/misconfigured.

    The Apps Script must be on the updated version (see SHEETS_SYNC.md
    for the doPost code that handles `op: append`)."""
    if not WRITEBACK_URL:
        return {"ok": False, "configured": False,
                "error": "Sheets write-back not configured — set HFARM_SHEETS_WRITEBACK_URL on the server."}
    payload = {"op": "append", "entity_id": entity_id, "fields": fields}
    try:
        req = _urlreq.Request(
            WRITEBACK_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _urlreq.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        try:
            inner = json.loads(raw.decode("utf-8"))
        except Exception:
            return {"ok": False, "configured": True,
                    "error": "Apps Script returned non-JSON: " + raw[:200].decode("utf-8", errors="replace")}
        return {
            "ok":          bool(inner.get("ok")),
            "configured":  True,
            "apps_script": inner,
            "error":       inner.get("error"),
        }
    except _urlerr.URLError as e:
        return {"ok": False, "configured": True, "error": "Network error reaching Apps Script: " + str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "configured": True, "error": "Unexpected: " + type(e).__name__ + ": " + str(e)}


@app.route("/api/prospects/<prospect_id>/approve", methods=["POST"])
@auth_required
def prospects_approve(prospect_id):
    """Move an approved candidate to the UNIS pipeline.
    Generates a new entity id, marks the candidate as approved+linked,
    AND (if WRITEBACK_URL is configured) appends a new row to the linked
    Google Sheet with a provenance block in the notes so the team can
    see who/when/how this came in.

    The frontend handles the in-memory UNIS push (since UNIS lives in
    the client). The Sheets push is best-effort — never blocks approval."""
    db = get_db()
    row = db.execute("SELECT * FROM prospect_candidates WHERE id = ?", (prospect_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404

    # Idempotency: if already approved, return the existing entity id
    # instead of minting a new one (otherwise a double-click would orphan
    # the first entity + push a second row to Sheets).
    if (row["status"] or "") == "approved" and row["approved_entity_id"]:
        rec = _prospect_row_to_dict(row)
        return jsonify({
            "ok":               True,
            "candidate":        rec,
            "new_entity_id":    row["approved_entity_id"],
            "already_approved": True,
            "sheets_push":      {"ok": False, "configured": bool(WRITEBACK_URL),
                                 "error": "skipped — already approved, would have created a duplicate Sheet row"},
        })

    new_entity_id = "ent-" + secrets.token_hex(6)
    db.execute(
        "UPDATE prospect_candidates SET status = ?, approved_entity_id = ? WHERE id = ?",
        ("approved", new_entity_id, prospect_id),
    )
    db.commit()
    _record_edit("prospect", "approve", prospect_id)

    rec = _prospect_row_to_dict(row)
    user_handle = (request.headers.get("X-Display-Name") or "").strip()

    # Build the field dict the Apps Script will write. Keys must match
    # the Sheet's column headers — Apps Script silently skips any key
    # whose header doesn't exist, so over-supplying is safe.
    notes_block = _build_provenance_notes(rec, user_handle)
    sheet_fields = {
        "id":                      new_entity_id,
        "name":                    rec.get("name") or "",
        "country":                 rec.get("country") or "",
        "country_canonical":       rec.get("country") or "",
        "continent":               rec.get("region") or "",
        "type":                    rec.get("type") or "university",
        "priority":                "Warm",
        "strategic_tier":          "",
        "partnership_readiness":   "Early",
        "pipeline_stage":          "identified",
        "focus_areas":             ", ".join(rec.get("suggested_programs") or []),
        "notes":                   notes_block,
        # Provenance flag column — Apps Script will write this only if a
        # `source` column exists in the sheet (otherwise silently skipped).
        # Teams who want filterable "show me all AI-sourced rows" can
        # add a `source` column to their Sheet; if they don't, the same
        # info is already in the notes block above.
        "source":                  "AI-discovered (Prospect Discovery)",
        "source_user":             user_handle or "",
        "source_date":             _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"),
        "ai_fit_score":            rec.get("ai_fit_score") or "",
    }

    # Best-effort push — never block approval on Sheets failure
    push_result = _push_new_entity_to_sheets(new_entity_id, sheet_fields)
    if not push_result.get("ok"):
        print(f"[approve] Sheets push for {new_entity_id} ({rec.get('name')!r}): {push_result.get('error')}")

    return jsonify({
        "ok":            True,
        "candidate":     rec,
        "new_entity_id": new_entity_id,
        "sheets_push":   push_result,
    })


@app.route("/api/prospects/<prospect_id>/push-to-sheets", methods=["POST"])
@auth_required
def prospects_push_to_sheets(prospect_id):
    """Re-push an already-approved prospect to Google Sheets. Useful when
    the first push during approval failed (e.g. Apps Script hadn't been
    updated to handle op:append yet) — instead of forcing the user to
    manually re-enter the row, they hit Retry once the script is fixed.

    Does NOT change candidate status or mint a new entity_id — uses the
    one already stored from the original approval. The Apps Script's
    own dedupe (`already_exists:true`) prevents accidental duplicates if
    the Sheet already has the row."""
    db = get_db()
    row = db.execute("SELECT * FROM prospect_candidates WHERE id = ?", (prospect_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    if (row["status"] or "") != "approved" or not row["approved_entity_id"]:
        return jsonify({"error": "candidate must be approved before pushing to sheets",
                        "current_status": row["status"]}), 400

    rec = _prospect_row_to_dict(row)
    user_handle = (request.headers.get("X-Display-Name") or "").strip()
    notes_block = _build_provenance_notes(rec, user_handle)
    sheet_fields = {
        "id":                      row["approved_entity_id"],
        "name":                    rec.get("name") or "",
        "country":                 rec.get("country") or "",
        "country_canonical":       rec.get("country") or "",
        "continent":               rec.get("region") or "",
        "type":                    rec.get("type") or "university",
        "priority":                "Warm",
        "strategic_tier":          "",
        "partnership_readiness":   "Early",
        "pipeline_stage":          "identified",
        "focus_areas":             ", ".join(rec.get("suggested_programs") or []),
        "notes":                   notes_block,
        "source":                  "AI-discovered (Prospect Discovery)",
        "source_user":             user_handle or "",
        "source_date":             _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"),
        "ai_fit_score":            rec.get("ai_fit_score") or "",
    }
    push_result = _push_new_entity_to_sheets(row["approved_entity_id"], sheet_fields)
    _record_edit("prospect", "push_sheets", prospect_id)
    return jsonify({
        "ok":          push_result.get("ok"),
        "entity_id":   row["approved_entity_id"],
        "name":        rec.get("name"),
        "sheets_push": push_result,
    })


@app.route("/api/prospects/<prospect_id>", methods=["DELETE"])
@auth_required
def prospects_delete(prospect_id):
    db = get_db()
    cur = db.execute("DELETE FROM prospect_candidates WHERE id = ?", (prospect_id,))
    db.execute("DELETE FROM prospect_decisions WHERE candidate_id = ?", (prospect_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    _record_edit("prospect", "delete", prospect_id)
    return jsonify({"ok": True})


# ============================================================================
# PARTNERSHIP BRIEF — server-side PDF rendering via WeasyPrint
# ----------------------------------------------------------------------------
# Frontend posts the entity payload (entity + contacts + outreach + contracts
# + computed engagement depth). Server renders Jinja2 template → WeasyPrint
# HTML→PDF → binary download. CSV is in the frontend's memory (loaded from
# Google Sheets), so the client is source-of-truth for entity data — server
# just renders.
#
# Why this isn't pure-JS (jsPDF):
#   - WeasyPrint does real text shaping (kerning, ligatures), embeds fonts,
#     produces searchable+selectable PDF text — not raster.
#   - One Jinja template + CSS gives FT/McKinsey typography that jsPDF can't
#     match without thousands of lines of imperative drawing code.
# ============================================================================
# Lazy-import WeasyPrint at first use — keeps the cold-start cheap when
# nobody hits /api/brief in a given session.
_weasyprint_cls = None
def _get_weasyprint():
    global _weasyprint_cls
    if _weasyprint_cls is None:
        from weasyprint import HTML as _WP_HTML  # type: ignore
        _weasyprint_cls = _WP_HTML
    return _weasyprint_cls


# Priority colour swatches mirror frontend PRIO_COLORS so the brief and
# the screen feel like the same artefact. Strategic tier colours likewise.
_BRIEF_PRIO_COLORS = {
    "Critical":       "#8B1A1A",
    "Hot":            "#D9534F",
    "Warm":           "#E0A93B",
    "Cold":           "#4A6B8A",
    "Cold-storage":   "#8E9AAB",
    "Up & Running":   "#2E7D5B",
    "Not interested": "#6E6E6E",
}
_BRIEF_TIER_COLORS = {
    "Digital Pioneer":     "#1F6FB4",
    "Prestige Hub":        "#C97A1A",
    "Applied Leader":      "#2E7D5B",
    "Established Partner": "#6B4C7D",
}
_BRIEF_TYPE_LABELS = {
    "university":          "University",
    "agency":              "Agency",
    "school":              "School",
    "student_organization": "Student org",
    "company":             "Company",
    "government":          "Government",
    "other":               "Other",
}


@app.route("/api/brief/<entity_id>", methods=["POST"])
@auth_required
def brief_pdf(entity_id):
    """Render a 1-page partnership brief as PDF.

    Expected body (frontend builds this from its in-memory state):
        {
          "entity": { id, name, country, continent, priority, ... },
          "depth":  { total, outreach, contacts, persistence },
          "programs": [ {name, topic, age, lang, score}, ... up to 3 ],
          "contacts": [ {name, role, email}, ... ],
          "outreach": [ {date, channel, subject, status}, ... ],
          "contracts": [ {type, status, signed_date, expiry_date,
                          annual_value_eur}, ... ],
          "action_html": "<strong>Reply within 48h.</strong> ...",
          "generated_by": "Defne Tuncer"   // optional
        }
    """
    body = request.get_json(force=True, silent=True) or {}
    entity = body.get("entity") or {}
    if not entity.get("name"):
        return jsonify({"error": "entity.name required"}), 400

    # Resolve display values + colour swatches
    priority      = entity.get("priority")
    tier          = entity.get("strategic_tier")
    etype         = entity.get("type")
    priority_color = _BRIEF_PRIO_COLORS.get(priority, "#6E6E6E")
    tier_color     = _BRIEF_TIER_COLORS.get(tier,     "#1A1A1A")
    type_label     = _BRIEF_TYPE_LABELS.get(etype, (etype or "").title())

    # Default depth if frontend didn't supply (so we never crash the template)
    depth = body.get("depth") or {"total": 0, "outreach": 0, "contacts": 0, "persistence": 0}

    # Cap outreach + contacts so the brief stays 1 page even for very
    # active entities. The brief is a 1-pager by design — full history
    # lives in the app.
    outreach  = (body.get("outreach")  or [])[:6]
    contacts  = (body.get("contacts")  or [])[:6]
    programs  = (body.get("programs")  or [])[:3]
    contracts = (body.get("contracts") or [])[:4]

    now = _dt.datetime.now(_dt.timezone.utc)
    generated_at = now.strftime("%Y-%m-%d · %H:%M UTC")

    html_str = render_template(
        "brief.html",
        entity=entity,
        depth=depth,
        programs=programs,
        contacts=contacts,
        outreach=outreach,
        contracts=contracts,
        priority_color=priority_color,
        tier_color=tier_color,
        entity_type_label=type_label,
        action_html=(body.get("action_html") or "Classify this entity to enter the active pipeline."),
        generated_at=generated_at,
        generated_by=(body.get("generated_by") or "").strip() or None,
    )

    try:
        WP_HTML = _get_weasyprint()
        pdf_bytes = WP_HTML(string=html_str).write_pdf()
    except ImportError:
        return jsonify({
            "error": "weasyprint not installed",
            "detail": "Run pip install -r requirements.txt or rebuild the Docker image.",
        }), 500
    except Exception as e:
        print(f"[brief] render failed for entity {entity_id}: {e}")
        return jsonify({"error": "pdf render failed", "detail": str(e)}), 500

    # Filename: slugified entity name + UTC date
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", entity.get("name") or "entity").strip("_")[:60]
    filename = f"brief_{safe}_{now.strftime('%Y%m%d')}.pdf"

    _record_edit("brief", "render", entity_id)

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================================================================
# CHAT ASSISTANT — read-only Q&A grounded on the live UNIS array
# ----------------------------------------------------------------------------
# Frontend posts: { user_text, history?, entities }
#   - entities: the full UNIS array (client is source of truth for filters)
#   - history: last N {role, text} turns for conversational continuity
# Gemini returns JSON: { intro, entity_ids[] }
# Server resolves entity_ids → result rows and returns them so the existing
# chat UI can render exactly as it does for the canned demo queries.
# ============================================================================
CHAT_SYSTEM_PROMPT = """You are a senior analyst assistant for the H-FARM College Global Partnerships tracker — a CRM-style tool for managing relationships with universities, agencies, schools and student organisations.

Your job: answer the user's question about the partner portfolio they've shown you, and surface the SPECIFIC entities that are relevant. You are READ-ONLY — you never propose data changes, never invent entities, never speculate beyond the data.

Entity fields you can rely on (per entity):
  id, name, type (university|agency|school|org), country, continent, city,
  priority (Critical|Hot|Warm|Cold|Cold-storage|Up & Running|Not interested),
  strategic_tier (Digital Pioneer|Prestige Hub|Applied Leader|Established Partner),
  partnership_score (0-100), partnership_readiness (Ready|Warming|Early|Cold|Dormant),
  days_dormant (int|null), last_contacted (date string|null),
  focus_areas (string), notes (string), website (string),
  top_program_id (id of best-fit H-FARM College offering), top_program_score (0-100),
  contacts (array of {name, role, email} — may be empty)

Rules:
- The `intro` field: 1-3 short sentences in plain English. ANSWER the user's actual question — don't just describe the entity. If they ask for a contact, give the contact (name, role, email). If they ask for a country breakdown, give numbers. If they ask "should I follow up?", weigh days_dormant + priority + readiness and give a verdict. No hedging, no "I would suggest", no "Based on the data".
- The `entity_ids` array: ids that match the user's question, ranked best-first. If the question is broad (e.g. "what should I focus on?"), return up to 8. If it's specific (e.g. "tell me about TalTech"), return just that one. If nothing matches, return an empty array — `intro` should say so plainly.
- When the user asks for a contact: pull name + role + email from `contacts` straight into the intro. If contacts is empty, say so plainly ("No contacts on file for X — you'd need to research this one") rather than describing the entity profile.
- Never invent ids, names, emails, or any field values. Only quote what's in the provided entities list.

Return ONLY a JSON object with this exact shape, no markdown, no commentary:
{"intro": "...", "entity_ids": ["...", "..."]}"""


# Fields we send to Gemini per entity — analytically rich but keeps the
# payload around 30-40KB for ~300 entities (well within Flash's window).
_CHAT_FIELDS = (
    "id", "name", "type", "country", "continent", "city",
    "priority", "strategic_tier", "partnership_score",
    "partnership_readiness", "days_dormant", "last_contacted",
    "focus_areas", "top_program_id", "top_program_score", "notes", "website",
)


def _slim_entities(entities):
    out = []
    if not isinstance(entities, list):
        return out
    for u in entities:
        if not isinstance(u, dict) or not u.get("id"):
            continue
        row = {k: u.get(k) for k in _CHAT_FIELDS if u.get(k) not in (None, "")}
        # Contacts are an array of {name, role, email}. Strip empty slots
        # before sending so we don't waste tokens on "contact_3 was blank".
        contacts = u.get("contacts") or []
        if isinstance(contacts, list):
            slim_contacts = [
                {k: c.get(k) for k in ("name", "role", "email") if c.get(k)}
                for c in contacts
                if isinstance(c, dict) and any(c.get(k) for k in ("name", "role", "email"))
            ]
            if slim_contacts:
                row["contacts"] = slim_contacts
        out.append(row)
    return out


@app.route("/api/chat-query", methods=["POST"])
@auth_required
def chat_query():
    if not GEMINI_KEY:
        return (
            jsonify({"error": "GEMINI_API_KEY is not set on the server. Add it to .env and restart Flask."}),
            500,
        )

    data = request.get_json(force=True) or {}
    user_text = (data.get("user_text") or "").strip()
    if not user_text:
        return jsonify({"error": "user_text is required."}), 400

    entities = _slim_entities(data.get("entities") or [])
    history = data.get("history") or []
    # Keep history short — last 6 turns is plenty of context
    history = history[-6:] if isinstance(history, list) else []

    # Build a single user-message payload. (Gemini supports multi-turn but
    # one-shot keeps the wire format simple, and we're already passing the
    # full entity context every turn so there's no caching to optimise here.)
    history_block = "\n".join(
        f"{'User' if m.get('role') == 'user' else 'Assistant'}: {m.get('text', '')}"
        for m in history if isinstance(m, dict)
    )
    prompt_parts = [
        "## Recent conversation",
        history_block or "(no prior turns)",
        "",
        "## Current question",
        user_text,
        "",
        "## Available entities (live snapshot from the user's filtered view)",
        json.dumps(entities, ensure_ascii=False),
        "",
        "Respond with JSON only.",
    ]
    prompt = "\n".join(prompt_parts)

    try:
        resp = gemini_client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=CHAT_SYSTEM_PROMPT,
                max_output_tokens=1024,
                temperature=0.4,
                response_mime_type="application/json",
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            ),
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Gemini API error: {e}"}), 502

    text = (resp.text or "").strip()
    parsed = _parse_email_json(text)   # tolerant JSON extractor — same shape
    intro = parsed.get("intro") or "I couldn't make sense of that question — try rephrasing?"
    ids = parsed.get("entity_ids") or []
    if not isinstance(ids, list):
        ids = []

    # Resolve ids → full result rows so the UI renders consistently. Drop
    # any hallucinated ids that aren't actually in the entities list.
    by_id = {u.get("id"): u for u in (data.get("entities") or []) if isinstance(u, dict)}
    results = []
    for eid in ids:
        u = by_id.get(eid)
        if not u:
            continue
        results.append({
            "id":   eid,
            "name": u.get("name", ""),
            "meta": " · ".join(filter(None, [
                u.get("country", ""),
                u.get("priority", ""),
                f"Score {u.get('partnership_score')}" if u.get("partnership_score") is not None else "",
            ])),
            "tag":  u.get("strategic_tier", ""),
        })

    usage_md = getattr(resp, "usage_metadata", None)
    return jsonify(
        {
            "intro": intro,
            "results": results,
            "model": MODEL,
            "usage": {
                "input_tokens":  getattr(usage_md, "prompt_token_count",     0) if usage_md else 0,
                "output_tokens": getattr(usage_md, "candidates_token_count", 0) if usage_md else 0,
            },
            "raw": text if not parsed else None,
        }
    )


if __name__ == "__main__":
    print(
        f"H-FARM College tracker backend starting on http://127.0.0.1:8000 (model: {MODEL})"
    )
    if not GEMINI_KEY:
        print(
            "⚠️  GEMINI_API_KEY not set — /api/draft-outreach and /api/chat-query will return 500. Add it to .env and restart."
        )
    app.run(host="127.0.0.1", port=8000, debug=False)
