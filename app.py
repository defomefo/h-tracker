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
import sqlite3
import threading
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from flask import Flask, g, jsonify, request, send_from_directory

load_dotenv()

ROOT = Path(__file__).parent
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
DB_PATH = Path(os.environ.get("HFARM_DB_PATH", ROOT / "h-tracker.db"))

app = Flask(__name__, static_folder=str(ROOT), static_url_path="")
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))


# ---------- SQLite plumbing ----------
_db_init_lock = threading.Lock()
_db_ready = False


def _connect():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    # Better concurrency for an internal multi-user tool
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_schema():
    """Idempotent schema creation. Called lazily on first request."""
    global _db_ready
    if _db_ready:
        return
    with _db_init_lock:
        if _db_ready:
            return
        with _connect() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS outreach (
                    id          TEXT PRIMARY KEY,
                    entity_id   TEXT NOT NULL,
                    payload     TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    deleted     INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_outreach_entity
                    ON outreach(entity_id);
                CREATE INDEX IF NOT EXISTS idx_outreach_updated
                    ON outreach(updated_at);

                -- Generic key/value bucket for simple per-entity overrides
                -- (team assignments, kanban stage overrides, 2x2 positions, …).
                -- One row per (namespace, key). Value is JSON-encoded.
                CREATE TABLE IF NOT EXISTS kv_store (
                    namespace  TEXT NOT NULL,
                    key        TEXT NOT NULL,
                    value      TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (namespace, key)
                );
                CREATE INDEX IF NOT EXISTS idx_kv_ns
                    ON kv_store(namespace);
                """
            )
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
            "key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "db": str(DB_PATH.name),
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
    return jsonify({"ok": True, "id": entry_id})


@app.route("/api/state/outreach/<entry_id>", methods=["DELETE"])
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
_KV_NAMESPACES = {"team_assignment", "stage_override", "map2x2_override", "kb_draft"}


def _check_ns(ns):
    if ns not in _KV_NAMESPACES:
        return jsonify({"error": f"Unknown namespace '{ns}'."}), 404
    return None


@app.route("/api/state/kv/<ns>", methods=["GET"])
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
    return jsonify({"ok": True, "namespace": ns, "key": key})


@app.route("/api/state/kv/<ns>/<path:key>", methods=["DELETE"])
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
    return jsonify({"ok": True})


@app.route("/api/state/kv/<ns>/_import", methods=["POST"])
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
    return jsonify({"ok": True, "inserted": inserted, "skipped": skipped})


@app.route("/api/draft-outreach", methods=["POST"])
def draft_outreach():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return (
            jsonify(
                {
                    "error": "ANTHROPIC_API_KEY is not set on the server. Add it to .env and restart Flask."
                }
            ),
            500,
        )

    data = request.get_json(force=True) or {}
    user_msg = _build_prompt(data)

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
    except anthropic.APIError as e:
        return jsonify({"error": f"Anthropic API error: {e}"}), 502

    text = resp.content[0].text.strip()
    parsed = _parse_email_json(text)
    return jsonify(
        {
            "subject": parsed.get("subject", ""),
            "body": parsed.get("body", ""),
            "model": MODEL,
            "usage": {
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
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


if __name__ == "__main__":
    print(
        f"H-FARM tracker backend starting on http://127.0.0.1:8000 (model: {MODEL})"
    )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "⚠️  ANTHROPIC_API_KEY not set — /api/draft-outreach will return 500. Add it to .env and restart."
        )
    app.run(host="127.0.0.1", port=8000, debug=False)
