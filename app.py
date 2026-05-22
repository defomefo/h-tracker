"""H-FARM Global Partnerships Tracker — Flask backend.

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
        return jsonify({"error": "internal server error", "detail": str(e)}), 500
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
2. Propose the recommended H-FARM offering that fits THEM (use the supplied programme/format).
3. Single clear call-to-action: a 20-min intro call with two concrete time options, or a specific next artefact.
4. Brief signature line with the sender team's role at H-FARM.

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
_KV_NAMESPACES = {"team_assignment", "stage_override", "map2x2_override", "kb_draft", "entity_override"}


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

    lines += ["", "## Recommended H-FARM offering to propose"]
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

    lines += ["", "## Sender (H-FARM team)"]
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

    lines += ["", "## H-FARM context"]
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
                (r.get("area_tematica") or r.get("industry_sector") or "").strip() or None,
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
            # Upsert (same SQL as PUT endpoint, but with proper created_at handling)
            if existing:
                db.execute(
                    "UPDATE sponsors SET event_year=?, event_name=?, company_name=?, normalized_name=?, "
                    "industry_sector=?, sponsorship_tier=?, value_no_iva_eur=?, value_with_iva_eur=?, "
                    "amount_paid_eur=?, contract_signed_by_us=?, contract_signed_by_them=?, invoice_no=?, "
                    "invoice_date=?, payment_date=?, participation_days=?, attendee_count=?, attendees=?, "
                    "primary_contact_name=?, primary_contact_email=?, notes=?, linked_entity_id=?, updated_at=? "
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
PROSPECT_FUZZY_THRESHOLD = 80   # rapidfuzz token_set_ratio


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
    """Same normalization rules as sponsors — strip legal suffixes, lowercase,
    collapse whitespace. Keeps Bauli ↔ Bauli S.p.A. matchable."""
    if not raw:
        return ""
    s = str(raw).strip().lower()
    s = re.sub(r"\s+", " ", s)
    for pat in [r"\bs\.p\.a\.?", r"\bspa\b", r"\bs\.r\.l\.?", r"\bsrl\b",
                r"\bscpa\b", r"\bs\.c\.p\.a\.?", r"\bgmbh\b", r"\bsb\b",
                r"\buniversity of\b", r"\buniversit[aàá] (di|del|della|degli)\b"]:
        s = re.sub(pat, "", s)
    s = re.sub(r"[.,'\"`]", "", s)
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
private higher-education institution. You are surfacing NEW prospective
partners (universities, agencies, schools, or student organizations) for
the global partnerships team to evaluate.

REQUIRED OUTPUT: a JSON array of candidate objects, NEVER more than what
the search results actually support. If only 2 candidates are well-grounded,
return 2. NEVER invent facts. NEVER include a candidate whose existence
isn't supported by at least one URL in the search results below.

SEARCH CRITERIA:
{crit_str}

EXISTING PARTNERS (DO NOT SUGGEST any of these — we already have them):
{exclude_str}

USER PREFERENCE PROFILE (from past decisions, lean toward this):
{profile_str}

WEB SEARCH RESULTS (your ONLY source of truth — cite indices [N] in your
reasoning so the user can verify):
{src_str}

For each genuinely new prospect you find in the search results, produce:
- name:             official organization name as it appears in the source
- type:             university | agency | school | student_organization
- country:          ISO country name
- region:           continent (Europe / Asia / North America / etc.)
- primary_url:      the most authoritative URL from the search results
- description:      1-sentence summary of who they are
- fit_score:        integer 0-100, your honest assessment of fit
- fit_reasoning:    2-3 sentences citing search-result indices [N] explaining
                    why this fits H-FARM (or doesn't). Be specific.
- source_urls:      array of at least 2 URLs from the search results
- suggested_programs: array of 1-3 H-FARM program names that might match
                    (Coding Academy, Data & AI, Game Design, Fashion AI Lab,
                    Startup Summer, etc.) — leave [] if unsure

If you can't find any genuinely new well-grounded prospects, return {{"candidates": []}}.
"""


def _filter_candidates(raw_candidates, existing_unis_names, db):
    """6-layer hallucination filter. Returns (filtered_list, verification_meta_per_id)."""
    from rapidfuzz import fuzz

    # Pre-fetch past candidate names for dedupe (single DB hit)
    past_rows = db.execute("SELECT normalized_name FROM prospect_candidates").fetchall()
    past_names = {(r["normalized_name"] or "").strip() for r in past_rows if r["normalized_name"]}

    existing_norm = [_normalize_prospect_name(n) for n in (existing_unis_names or []) if n]

    out, verifications = [], []
    for c in raw_candidates:
        name = (c.get("name") or "").strip()
        if not name:
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
        for existing in existing_norm:
            if existing and fuzz.token_set_ratio(norm, existing) >= PROSPECT_FUZZY_THRESHOLD:
                verification["dup_unis"] = True
                break
        if verification["dup_unis"]:
            verification["warnings"].append("already in UNIS")
            continue

        # Layer 5 — past candidates dedupe
        if norm in past_names:
            verification["dup_past"] = True
            verification["warnings"].append("already surfaced previously")
            continue

        # If we got here, all hard layers passed
        verification["all_passed"] = True
        out.append(c)
        verifications.append((name, verification))
    return out, verifications


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

    # Compose the search query — type + country + free-text combined
    search_terms = []
    if criteria["type"] == "university":     search_terms.append("universities")
    elif criteria["type"] == "agency":       search_terms.append("education agencies")
    elif criteria["type"] == "school":       search_terms.append("high schools")
    elif criteria["type"] == "student_organization": search_terms.append("student organizations")
    else:                                     search_terms.append("partnership prospects")
    if criteria["country"]:                  search_terms.append("in " + criteria["country"])
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

    try:
        from google.genai import types as _gtypes
        client_ai = genai.Client(api_key=GEMINI_KEY)
        resp = client_ai.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=_gtypes.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
                max_output_tokens=4096,
            ),
        )
        raw_text = (resp.text or "").strip()
    except Exception as e:
        print(f"[prospects] Gemini failed: {e}")
        return jsonify({"ok": False, "error": f"Gemini call failed: {e}"}), 500

    try:
        parsed = json.loads(raw_text)
        raw_candidates = parsed.get("candidates") or []
        if not isinstance(raw_candidates, list):
            raw_candidates = []
    except Exception as e:
        print(f"[prospects] JSON parse failed: {e}; raw: {raw_text[:400]}")
        return jsonify({"ok": False, "error": "Gemini returned non-JSON output",
                        "raw_preview": raw_text[:400]}), 500

    # ----- Step 3: 6-layer hallucination filter -----
    db = get_db()
    filtered, verifications = _filter_candidates(raw_candidates, existing_names, db)
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
    return jsonify({
        "ok":           True,
        "run_id":       run_id,
        "query":        final_query,
        "raw_count":    raw_count,
        "ai_proposed":  len(raw_candidates),
        "surfaced":     filtered_count,
        "candidates":   surfaced,
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
    return jsonify({"ok": True, "decision_id": did, "new_status": new_status})


@app.route("/api/prospects/<prospect_id>/approve", methods=["POST"])
@auth_required
def prospects_approve(prospect_id):
    """Move an approved candidate to the UNIS pipeline.
    Generates a new entity id, marks the candidate as approved+linked, and
    returns the seed payload the frontend can use to push into UNIS.
    The frontend handles the actual UNIS push (since UNIS lives in the
    client's in-memory state — server doesn't own it)."""
    db = get_db()
    row = db.execute("SELECT * FROM prospect_candidates WHERE id = ?", (prospect_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    new_entity_id = "ent-" + secrets.token_hex(6)
    db.execute(
        "UPDATE prospect_candidates SET status = ?, approved_entity_id = ? WHERE id = ?",
        ("approved", new_entity_id, prospect_id),
    )
    db.commit()
    _record_edit("prospect", "approve", prospect_id)
    rec = _prospect_row_to_dict(row)
    return jsonify({
        "ok": True,
        "candidate":     rec,
        "new_entity_id": new_entity_id,
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
  top_program_id (id of best-fit H-FARM offering), top_program_score (0-100),
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
        f"H-FARM tracker backend starting on http://127.0.0.1:8000 (model: {MODEL})"
    )
    if not GEMINI_KEY:
        print(
            "⚠️  GEMINI_API_KEY not set — /api/draft-outreach and /api/chat-query will return 500. Add it to .env and restart."
        )
    app.run(host="127.0.0.1", port=8000, debug=False)
