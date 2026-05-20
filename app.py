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
from flask import Flask, g, jsonify, request, send_from_directory, session
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
            ORDER BY id DESC
            LIMIT ?""",
        (entity_id, entity_id, entity_id, limit),
    ).fetchall()
    return jsonify(
        {
            "now": _now_iso(),
            "entity_id": entity_id,
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
