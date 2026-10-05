from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.redact import scrub_sensitive

DB_PATH = os.environ.get("CALLKETTLE_DB_PATH", "./callkettle.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    call_sid TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    from_number TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    turn_count INTEGER NOT NULL DEFAULT 0,
    outcome TEXT,
    transcript_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_sid TEXT,
    client_id TEXT NOT NULL,
    caller_name TEXT NOT NULL,
    caller_phone TEXT NOT NULL,
    service TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',
    UNIQUE(client_id, slot_start)
);

CREATE TABLE IF NOT EXISTS cancelled_bookings (
    id INTEGER PRIMARY KEY,
    call_sid TEXT,
    client_id TEXT NOT NULL,
    caller_name TEXT NOT NULL,
    caller_phone TEXT NOT NULL,
    service TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    created_at TEXT NOT NULL,
    uid TEXT,
    cancelled_at TEXT NOT NULL,
    cancelled_by_call_sid TEXT
);

CREATE TABLE IF NOT EXISTS booking_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL,
    client_id TEXT NOT NULL,
    event TEXT NOT NULL,            -- created | rescheduled | cancelled
    at TEXT NOT NULL,
    call_sid TEXT,
    old_start TEXT,
    new_start TEXT
);
CREATE INDEX IF NOT EXISTS booking_events_booking ON booking_events (booking_id);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    name TEXT NOT NULL,
    client_id TEXT,
    value REAL NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS metrics_name_at ON metrics (name, at);

CREATE TABLE IF NOT EXISTS intakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new'
);

CREATE TABLE IF NOT EXISTS private_demos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash TEXT NOT NULL UNIQUE,
    client_id TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    max_calls INTEGER NOT NULL,
    calls_used INTEGER NOT NULL DEFAULT 0,
    revoked_at TEXT,
    cleaned_at TEXT
);
CREATE TABLE IF NOT EXISTS private_demo_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    event TEXT NOT NULL,            -- created | code_ok | code_bad | locked | expired | revoked | exhausted | cleaned
    demo_id INTEGER,
    caller_hash TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS private_demo_audit_at ON private_demo_audit (event, at);

CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_sid TEXT,
    client_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    caller_phone TEXT,
    summary TEXT,
    created_at TEXT NOT NULL
);

-- Owner-marked "not a customer" caller IDs (app/spamtag.py): per business, digits only (last 10).
CREATE TABLE IF NOT EXISTS spam_numbers (
    client_id TEXT NOT NULL,
    number TEXT NOT NULL,
    marked_at TEXT NOT NULL,
    call_sid TEXT,
    prev_class TEXT,
    prev_attention INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (client_id, number)
);

-- Owner portal (app/owner_auth.py): one account per owner email, always bound to exactly one client.
CREATE TABLE IF NOT EXISTS owner_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,           -- scrypt$n$r$p$salt$hash (per-user random salt); never the password
    must_change INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    last_login_at REAL,
    failed_count INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0,
    disabled_at REAL
);
CREATE INDEX IF NOT EXISTS owner_users_client ON owner_users (client_id);
CREATE TABLE IF NOT EXISTS owner_sessions (
    token_hash TEXT PRIMARY KEY,     -- sha256 of the cookie value; the cookie value itself is never stored
    user_id INTEGER NOT NULL REFERENCES owner_users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS owner_login_failures (
    at REAL NOT NULL,
    ip_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS owner_login_failures_ip ON owner_login_failures (ip_hash, at);
"""


@contextmanager
def _conn(timeout: float = 10):
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# The live call path (transcript, outcome, versions, metrics) writes while a caller is waiting on a webhook that Twilio gives ~15 seconds.
# Waiting out the normal 10 second lock timeout on each of several writes would blow that deadline, so these writes give up quickly,
# and after one lock failure the rest are skipped for a few seconds (the record is lost, the call is not).
LIVE_BUSY_SECONDS = 1.5
LIVE_BREAKER_SECONDS = 3.0
_live_blocked_until = 0.0


@contextmanager
def _live_conn():
    global _live_blocked_until
    if time.monotonic() < _live_blocked_until:
        raise sqlite3.OperationalError("database busy: live-path writes paused briefly")
    try:
        with _conn(timeout=LIVE_BUSY_SECONDS) as conn:
            yield conn
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            _live_blocked_until = time.monotonic() + LIVE_BREAKER_SECONDS
        raise


def live_path_open() -> bool:
    """False while live-path writes are paused after a lock failure (see _live_conn)."""
    return time.monotonic() >= _live_blocked_until


def trip_live_breaker(exc: Exception) -> None:
    global _live_blocked_until
    if "locked" in str(exc) or "busy" in str(exc):
        _live_blocked_until = time.monotonic() + LIVE_BREAKER_SECONDS


LEGACY_DB_NAME = "%s.db" % ("desk" + "line")
LEGACY_CLIENT_IDS = {"%s_demo" % ("desk" + "line"): "callkettle_demo", "%s_sales" % ("desk" + "line"): "callkettle_sales"}


def migrate_legacy_database() -> str | None:
    """One-time rename of the database file from its old product name. The new file is built with SQLite's own backup API, checked table by table against
    the old one, and only then swapped in; the old file is kept (renamed) until someone deletes it. Any problem leaves the OLD file in use. Returns what it did."""
    global DB_PATH
    new = Path(DB_PATH)
    legacy = new.with_name(LEGACY_DB_NAME)
    if new.exists() or not legacy.exists() or legacy == new:
        return None
    tmp = new.with_name(new.name + ".migrating")
    try:
        tmp.unlink(missing_ok=True)
        src, dst = sqlite3.connect(legacy), sqlite3.connect(tmp)
        try:
            with dst:
                src.backup(dst)
            tables = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for t in tables:
                a, b = src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                if a != b:
                    raise RuntimeError(f"table {t}: {a} rows before, {b} after")
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("integrity check failed on the copy")
        finally:
            src.close()
            dst.close()
        os.replace(tmp, new)
        legacy.rename(new.with_name(new.stem + "-before-rename.db.bak"))
        for ext in ("-wal", "-shm"):
            new.with_name(LEGACY_DB_NAME + ext).unlink(missing_ok=True)
        return f"moved {legacy.name} -> {new.name}"
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        DB_PATH = str(legacy)                           # keep running on the old file rather than risk data
        return f"MIGRATION FAILED, still using {legacy.name}: {exc}"


def migrate_legacy_client_ids(conn) -> None:
    """The two internal demo/sales clients were renamed; carry their history over (a row that would collide is left where it is)."""
    for table, in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        if "client_id" not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            continue
        for old, new_id in LEGACY_CLIENT_IDS.items():
            conn.execute(f"UPDATE OR IGNORE {table} SET client_id = ? WHERE client_id = ?", (new_id, old))


def init_db() -> None:
    note = migrate_legacy_database()
    if note:
        import logging

        logging.getLogger("callkettle.storage").warning("Database: %s", note)
    with _conn() as conn:
        conn.executescript(_SCHEMA)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(calls)")}
        if "summary" not in cols:
            conn.execute("ALTER TABLE calls ADD COLUMN summary TEXT")
        bcols = {row[1] for row in conn.execute("PRAGMA table_info(bookings)")}
        if "uid" not in bcols:
            conn.execute("ALTER TABLE bookings ADD COLUMN uid TEXT")  # stable calendar-invite id, so changes update the owner's calendar
        # What happened (app/outcomes.py) and exactly which configuration, prompt and tool schema produced it.
        for col, decl in (("outcome_class", "TEXT"), ("needs_attention", "INTEGER NOT NULL DEFAULT 0"), ("attention_resolved_at", "TEXT"),
                          ("config_version", "TEXT"), ("prompt_version", "TEXT"), ("tool_version", "TEXT"), ("model_id", "TEXT")):
            if col not in cols:
                conn.execute(f"ALTER TABLE calls ADD COLUMN {col} {decl}")
        # Real usage per call, so unit economics are measured, not guessed.
        for col in ("input_tokens", "output_tokens", "model_calls", "model_ms", "tts_chars"):
            if col not in cols:
                conn.execute(f"ALTER TABLE calls ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        migrate_legacy_client_ids(conn)
    try:  # private cost ledger: additive, idempotent, must never block startup
        from app import cost_observability
        cost_observability.init_ledger(DB_PATH)
    except Exception:
        import logging
        logging.getLogger("callkettle.storage").exception("Could not initialise the private cost ledger")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_call_start(call_sid: str, client_id: str, from_number: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO calls (call_sid, client_id, from_number, started_at) "
            "VALUES (?, ?, ?, ?)",
            (call_sid, client_id, from_number, _now()),
        )


# --- private demos: a temporary, isolated demo for one prospect, reached with a code on the public demo menu ---
PRIVATE_DEMO_CODE_DIGITS = 6
MAX_BAD_CODES_PER_CALLER_HOUR = 5
MAX_BAD_CODES_GLOBAL_HOUR = 30


def _code_hash(code: str) -> str:
    import hashlib
    import hmac

    return hmac.new((os.environ.get("REPORT_KEY") or "callkettle").encode(), f"private-demo:{code}".encode(), hashlib.sha256).hexdigest()


def _caller_hash(number: str | None) -> str:
    import hashlib

    return hashlib.sha256(f"caller:{number or ''}".encode()).hexdigest()[:16]      # never the number itself


def _audit(conn, event: str, *, demo_id: int | None = None, caller: str | None = None, detail: str = "") -> None:
    conn.execute("INSERT INTO private_demo_audit (at, event, demo_id, caller_hash, detail) VALUES (?, ?, ?, ?, ?)", (_now(), event, demo_id, _caller_hash(caller), detail[:200]))


def create_private_demo(client_id: str, label: str, *, hours: int = 72, max_calls: int = 10) -> dict:
    """A random one-time code for one demo client. The plain code is returned ONCE (only its keyed hash is stored)."""
    import secrets

    hours, max_calls = max(1, min(int(hours), 24 * 14)), max(1, min(int(max_calls), 50))
    with _conn() as conn:
        for _ in range(20):
            code = f"{secrets.randbelow(10 ** PRIVATE_DEMO_CODE_DIGITS):0{PRIVATE_DEMO_CODE_DIGITS}d}"
            if not conn.execute("SELECT 1 FROM private_demos WHERE code_hash = ?", (_code_hash(code),)).fetchone():
                break
        else:  # pragma: no cover
            raise RuntimeError("could not find a free code")
        expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        cur = conn.execute(
            "INSERT INTO private_demos (code_hash, client_id, label, created_at, expires_at, max_calls) VALUES (?, ?, ?, ?, ?, ?)",
            (_code_hash(code), client_id, label[:120], _now(), expires, max_calls),
        )
        _audit(conn, "created", demo_id=cur.lastrowid, detail=f"{client_id} for {hours}h, {max_calls} calls")
    return {"code": code, "client_id": client_id, "expires_at": expires, "max_calls": max_calls, "demo_id": cur.lastrowid}


def claim_private_demo(code: str, caller: str | None) -> tuple[str, dict | None]:
    """Check a typed code. Statuses: ok | bad | locked | expired | revoked | exhausted. Wrong guesses are limited per caller and overall,
    and every attempt is audited (the caller is stored only as a hash)."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with _conn() as conn:
        mine = conn.execute("SELECT COUNT(*) FROM private_demo_audit WHERE event = 'code_bad' AND caller_hash = ? AND at >= ?", (_caller_hash(caller), hour_ago)).fetchone()[0]
        everyone = conn.execute("SELECT COUNT(*) FROM private_demo_audit WHERE event = 'code_bad' AND at >= ?", (hour_ago,)).fetchone()[0]
        if mine >= MAX_BAD_CODES_PER_CALLER_HOUR or everyone >= MAX_BAD_CODES_GLOBAL_HOUR:
            _audit(conn, "locked", caller=caller, detail="too many wrong codes")
            return "locked", None
        row = conn.execute(
            "SELECT id, client_id, label, expires_at, max_calls, calls_used, revoked_at FROM private_demos WHERE code_hash = ?", (_code_hash(code),)
        ).fetchone() if len(code) == PRIVATE_DEMO_CODE_DIGITS else None
        if row is None:
            _audit(conn, "code_bad", caller=caller)
            return "bad", None
        demo = dict(zip(("id", "client_id", "label", "expires_at", "max_calls", "calls_used", "revoked_at"), row))
        if demo["revoked_at"]:
            _audit(conn, "revoked", demo_id=demo["id"], caller=caller)
            return "revoked", None
        if demo["expires_at"] <= _now():
            _audit(conn, "expired", demo_id=demo["id"], caller=caller)
            return "expired", None
        if demo["calls_used"] >= demo["max_calls"]:
            _audit(conn, "exhausted", demo_id=demo["id"], caller=caller)
            return "exhausted", None
        conn.execute("UPDATE private_demos SET calls_used = calls_used + 1 WHERE id = ?", (demo["id"],))
        _audit(conn, "code_ok", demo_id=demo["id"], caller=caller)
    return "ok", demo


def revoke_private_demo(client_id: str) -> int:
    with _conn() as conn:
        rows = conn.execute("SELECT id FROM private_demos WHERE client_id = ? AND revoked_at IS NULL", (client_id,)).fetchall()
        conn.execute("UPDATE private_demos SET revoked_at = ? WHERE client_id = ? AND revoked_at IS NULL", (_now(), client_id))
        for (i,) in rows:
            _audit(conn, "revoked", demo_id=i, detail="by operator")
    return len(rows)


def list_private_demos() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT id, client_id, label, created_at, expires_at, max_calls, calls_used, revoked_at, cleaned_at FROM private_demos ORDER BY id DESC LIMIT 100").fetchall()
    keys = ("id", "client_id", "label", "created_at", "expires_at", "max_calls", "calls_used", "revoked_at", "cleaned_at")
    return [dict(zip(keys, r)) for r in rows]


def due_private_demo_cleanup() -> list[dict]:
    """Demos that have expired or been revoked and not yet cleaned up (their config is removed and their data scrubbed by ops)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, client_id FROM private_demos WHERE cleaned_at IS NULL AND (revoked_at IS NOT NULL OR expires_at <= ?)", (_now(),)
        ).fetchall()
    return [{"id": r[0], "client_id": r[1]} for r in rows]


def clean_private_demo(demo_id: int, client_id: str) -> dict:
    """After expiry: remove the demo's bookings and callback records, and wipe its call transcripts and summaries (the call rows stay, without words, so spend and the audit trail remain)."""
    with _conn() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(calls)")}
        workspace_scrub = (", owner_note = '', followup_state = 'none', followup_due_date = NULL, "
                           "followup_updated_at = NULL, needs_attention = 0, attention_resolved_at = NULL"
                           if "owner_note" in columns else "")
        out = {
            "bookings": conn.execute("DELETE FROM bookings WHERE client_id = ?", (client_id,)).rowcount,
            "cancelled": conn.execute("DELETE FROM cancelled_bookings WHERE client_id = ?", (client_id,)).rowcount,
            "escalations": conn.execute("DELETE FROM escalations WHERE client_id = ?", (client_id,)).rowcount,
            "calls_scrubbed": conn.execute("UPDATE calls SET transcript_json = '[]', summary = NULL" + workspace_scrub + " WHERE client_id = ?", (client_id,)).rowcount,
        }
        conn.execute("UPDATE private_demos SET cleaned_at = ? WHERE id = ?", (_now(), demo_id))
        _audit(conn, "cleaned", demo_id=demo_id, detail=str(out))
    return out


def sqlite_inspection(*, deep: bool = False) -> dict:
    """How the database is actually configured right now (read from SQLite itself, not from what the code intends), so a
    production problem such as locking can be diagnosed without a shell. `deep` also runs an integrity quick_check."""
    out: dict = {"sqlite_version": sqlite3.sqlite_version}
    with _conn() as conn:
        for name in ("journal_mode", "busy_timeout", "foreign_keys", "synchronous", "wal_autocheckpoint", "page_count", "page_size", "freelist_count"):
            try:
                out[name] = conn.execute(f"PRAGMA {name}").fetchone()[0]
            except sqlite3.Error as exc:  # pragma: no cover
                out[name] = f"error: {exc}"
        if deep:
            out["quick_check"] = conn.execute("PRAGMA quick_check").fetchone()[0]
    sizes = {}
    for suffix in ("", "-wal", "-shm"):
        path = DB_PATH + suffix
        sizes[suffix or "db"] = os.path.getsize(path) if os.path.exists(path) else None
    out["file_bytes"] = sizes
    return out


def set_call_versions(call_sid: str, *, config_version: str, prompt_version: str, tool_version: str, model_id: str) -> None:
    with _live_conn() as conn:
        conn.execute(
            "UPDATE calls SET config_version = ?, prompt_version = ?, tool_version = ?, model_id = ? WHERE call_sid = ?",
            (config_version, prompt_version, tool_version, model_id, call_sid),
        )


def classify_and_store(call_sid: str) -> tuple[str, bool] | None:
    """Work out what the call came to (from bookings, escalations and how it ended) and save it. Safe to call more than once."""
    from app import outcomes

    from app import spamtag

    with _conn() as conn:
        row = conn.execute("SELECT outcome, transcript_json FROM calls WHERE call_sid = ?", (call_sid,)).fetchone()
        if row is None:
            return None
        owner_of = conn.execute("SELECT client_id, from_number FROM calls WHERE call_sid = ?", (call_sid,)).fetchone()
        ended_mid_question = False
        try:
            turns = json.loads(row[1] or "[]")
            caller_turns = sum(1 for t in turns if t.get("role") == "caller")
            ended_mid_question = row[0] == "caller_hung_up" and bool(turns) and turns[-1].get("role") == "ai" and str(turns[-1].get("text", "")).rstrip().endswith("?")
        except ValueError:
            caller_turns = 0
        booked = conn.execute("SELECT COUNT(*) FROM bookings WHERE call_sid = ?", (call_sid,)).fetchone()[0]
        events = dict(conn.execute("SELECT event, COUNT(*) FROM booking_events WHERE call_sid = ? GROUP BY event", (call_sid,)).fetchall())
        reasons = [r[0] for r in conn.execute("SELECT reason FROM escalations WHERE call_sid = ?", (call_sid,)).fetchall()]
        cls, attention = outcomes.classify(
            outcome=row[0], caller_turns=caller_turns, booked=max(booked, events.get("created", 0)),
            rescheduled=events.get("rescheduled", 0), cancelled=events.get("cancelled", 0), reasons=reasons, ended_mid_question=ended_mid_question,
        )
        # A number this business marked "not a customer" is tagged SPAM and not queued, unless it was a possible emergency or it booked/changed a booking.
        if cls not in (outcomes.EMERGENCY_ESCALATED, outcomes.BOOKED, outcomes.RESCHEDULED, outcomes.CANCELLED) and spamtag.marked_in(conn, owner_of[0], owner_of[1]):
            cls, attention = outcomes.SPAM, False
        columns = {column[1] for column in conn.execute("PRAGMA table_info(calls)")}
        attention_value = ("CASE WHEN followup_state IS NOT NULL THEN needs_attention ELSE ? END"
                           if "followup_state" in columns else "?")
        conn.execute("UPDATE calls SET outcome_class = ?, needs_attention = " + attention_value + " WHERE call_sid = ?",
                     (cls, 1 if attention else 0, call_sid))
    return cls, attention


def get_call_class(call_sid: str) -> str | None:
    with _conn() as conn:
        row = conn.execute("SELECT outcome_class FROM calls WHERE call_sid = ?", (call_sid,)).fetchone()
    return row[0] if row else None


def resolve_attention(client_id: str, call_sid: str) -> bool:
    """The owner marked a follow-up as handled. Scoped to the client so one client's key can never touch another's call."""
    with _conn() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(calls)")}
        workspace_update = (", followup_state = 'handled', followup_due_date = NULL, followup_updated_at = ?"
                            if "followup_state" in columns else "")
        now = _now()
        params = (now, now, call_sid, client_id) if workspace_update else (now, call_sid, client_id)
        cur = conn.execute(
            "UPDATE calls SET attention_resolved_at = ?" + workspace_update +
            " WHERE call_sid = ? AND client_id = ? AND needs_attention = 1",
            params,
        )
        return cur.rowcount > 0


def reassign_call(call_sid: str, client_id: str) -> None:
    """Move a call to another client (the public demo menu hands the caller to the demo they chose, so each demo's spend is its own)."""
    with _conn() as conn:
        conn.execute("UPDATE calls SET client_id = ? WHERE call_sid = ?", (client_id, call_sid))


NOT_RECORDED = "[not recorded]"


def add_model_usage(call_sid: str, *, input_tokens: int, output_tokens: int, ms: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE calls SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ?, "
            "model_calls = model_calls + 1, model_ms = model_ms + ? WHERE call_sid = ?",
            (int(input_tokens), int(output_tokens), int(ms), call_sid),
        )


def log_turn(call_sid: str, role: str, text: str, *, store_text: bool = True) -> None:
    spoken_chars = len(text) if role == "ai" else 0  # billed by the voice provider even when the words aren't stored
    text = NOT_RECORDED if not store_text else scrub_sensitive(text)
    with _live_conn() as conn:
        row = conn.execute(
            "SELECT transcript_json, turn_count FROM calls WHERE call_sid = ?", (call_sid,)
        ).fetchone()
        if row is None:
            return
        transcript = json.loads(row[0])
        transcript.append({"role": role, "text": text, "at": _now()})
        conn.execute(
            "UPDATE calls SET transcript_json = ?, turn_count = ?, tts_chars = tts_chars + ? WHERE call_sid = ?",
            (json.dumps(transcript), row[1] + 1, spoken_chars, call_sid),
        )


def log_call_end(call_sid: str, outcome: str) -> None:
    with _live_conn() as conn:
        conn.execute(
            "UPDATE calls SET ended_at = ?, outcome = ? WHERE call_sid = ?",
            (_now(), outcome, call_sid),
        )


def log_call_end_if_open(call_sid: str, outcome: str) -> None:
    """For Twilio's status callback: record how a call ended only if the app
    didn't already record its own, more specific outcome (booked/transferred/...)."""
    with _live_conn() as conn:
        conn.execute(
            "UPDATE calls SET ended_at = ?, outcome = ? WHERE call_sid = ? AND ended_at IS NULL",
            (_now(), outcome, call_sid),
        )


def recent_call_count(client_id: str, from_number: str, *, minutes: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM calls WHERE client_id = ? AND from_number = ? AND started_at >= ?",
            (client_id, from_number, cutoff),
        ).fetchone()
    return int(row[0])


def open_call_count(client_id: str, *, within_seconds: int, exclude_sid: str = "") -> int:
    """Calls of this client that started in the last `within_seconds` and have not ended (in flight right now). The window
    keeps a call whose end was never recorded from counting forever."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within_seconds)).isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM calls WHERE client_id = ? AND ended_at IS NULL AND started_at >= ? AND call_sid != ?",
            (client_id, cutoff, exclude_sid),
        ).fetchone()
    return int(row[0])


def calls_this_month(client_id: str) -> int:
    """Calls started by this client's callers since the first of the month (UTC)."""
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM calls WHERE client_id = ? AND started_at >= ?", (client_id, start)
        ).fetchone()
    return int(row[0])


def record_metric(name: str, client_id: str | None = None, value: float = 1.0) -> None:
    """Count an operational event (failures, fallbacks, blocked claims...). Never raises: observability must not break a call."""
    try:
        with _live_conn() as conn:
            conn.execute("INSERT INTO metrics (at, name, client_id, value) VALUES (?, ?, ?, ?)", (_now(), name, client_id, value))
    except Exception:
        pass


def metric_percentiles(name: str, since_iso: str) -> dict:
    """Count and p50/p95 of a value-bearing metric (for example turn_server_ms) since a time. Empty -> count 0."""
    with _conn() as conn:
        values = sorted(r[0] for r in conn.execute("SELECT value FROM metrics WHERE name = ? AND at >= ?", (name, since_iso)))
    if not values:
        return {"count": 0}
    def pick(q: float) -> float:
        return round(values[min(len(values) - 1, int(q * len(values)))], 1)
    return {"count": len(values), "p50": pick(0.50), "p95": pick(0.95), "max": round(values[-1], 1)}


def metric_totals(since_iso: str) -> dict[str, float]:
    with _conn() as conn:
        rows = conn.execute("SELECT name, SUM(value) FROM metrics WHERE at >= ? GROUP BY name", (since_iso,)).fetchall()
    return {n: float(v) for n, v in rows}


def get_call(call_sid: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT call_sid, client_id, from_number, started_at, ended_at, turn_count, outcome, "
            "transcript_json, summary FROM calls WHERE call_sid = ?",
            (call_sid,),
        ).fetchone()
    if row is None:
        return None
    keys = ["call_sid", "client_id", "from_number", "started_at", "ended_at", "turn_count",
            "outcome", "transcript_json", "summary"]
    return dict(zip(keys, row))


def set_call_summary(call_sid: str, summary: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE calls SET summary = ? WHERE call_sid = ?", (summary, call_sid))


@dataclass
class BookingConflict(Exception):
    client_id: str
    slot_start: str


def _log_event(conn, booking_id: int, client_id: str, event: str, call_sid: str | None, old_start: str | None, new_start: str | None) -> None:
    conn.execute(
        "INSERT INTO booking_events (booking_id, client_id, event, at, call_sid, old_start, new_start) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (booking_id, client_id, event, _now(), call_sid, old_start, new_start),
    )


def booking_history(booking_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT event, at, call_sid, old_start, new_start FROM booking_events WHERE booking_id = ? ORDER BY id",
                            (booking_id,)).fetchall()
    return [dict(zip(("event", "at", "call_sid", "old_start", "new_start"), r)) for r in rows]


def get_booking_at(client_id: str, slot_start: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(f"SELECT {_BOOKING_COLS} FROM bookings WHERE client_id = ? AND slot_start = ? AND status = 'confirmed'",
                           (client_id, slot_start)).fetchone()
    return _row_to_booking(row) if row else None


def count_call_bookings(call_sid: str) -> int:
    """Bookings made during one call (a cap on what one phone call can book)."""
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM bookings WHERE call_sid = ? AND status != 'cancelled'", (call_sid,)).fetchone()[0]


def create_booking(
    *,
    call_sid: str | None,
    client_id: str,
    caller_name: str,
    caller_phone: str,
    service: str,
    slot_start: str,
    slot_end: str,
    uid: str | None = None,
) -> int:
    uid = uid or uuid.uuid4().hex
    try:
        with _conn() as conn:
            # One writer at a time, so two callers can't both pass the overlap
            # check and then both insert. Intervals overlap when each starts
            # before the other ends; that catches a 60-minute job booked at
            # 9:00 blocking a 9:30 slot, which the UNIQUE(slot_start) rule alone
            # would miss.
            conn.execute("BEGIN IMMEDIATE")
            clash = conn.execute(
                "SELECT 1 FROM bookings WHERE client_id = ? AND status = 'confirmed' "
                "AND slot_start < ? AND slot_end > ? LIMIT 1",
                (client_id, slot_end, slot_start),
            ).fetchone()
            if clash:
                raise BookingConflict(client_id=client_id, slot_start=slot_start)
            cur = conn.execute(
                "INSERT INTO bookings "
                "(call_sid, client_id, caller_name, caller_phone, service, slot_start, slot_end, created_at, uid) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (call_sid, client_id, caller_name, caller_phone, service, slot_start, slot_end, _now(), uid),
            )
            _log_event(conn, int(cur.lastrowid), client_id, "created", call_sid, None, slot_start)
            return int(cur.lastrowid)
    except sqlite3.IntegrityError as exc:
        raise BookingConflict(client_id=client_id, slot_start=slot_start) from exc


_BOOKING_COLS = "id, call_sid, client_id, caller_name, caller_phone, service, slot_start, slot_end, created_at, uid"


def _row_to_booking(row) -> dict:
    return dict(zip(_BOOKING_COLS.split(", "), row))


def get_booking(booking_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(f"SELECT {_BOOKING_COLS} FROM bookings WHERE id = ? AND status = 'confirmed'", (booking_id,)).fetchone()
    return _row_to_booking(row) if row else None


def _last10(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")[-10:]


def find_upcoming_bookings(client_id: str, phone: str, from_local_iso: str) -> list[dict]:
    """Confirmed future bookings made with this phone number (matched on the last 10 digits)."""
    want = _last10(phone)
    if len(want) < 10:
        return []
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT {_BOOKING_COLS} FROM bookings WHERE client_id = ? AND status = 'confirmed' AND slot_start >= ? ORDER BY slot_start",
            (client_id, from_local_iso),
        ).fetchall()
    return [b for b in map(_row_to_booking, rows) if _last10(b["caller_phone"]) == want]


def cancel_booking(booking_id: int, *, by_call_sid: str | None = None) -> dict | None:
    """Move a booking to cancelled_bookings (history kept, the slot becomes free again)."""
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(f"SELECT {_BOOKING_COLS} FROM bookings WHERE id = ? AND status = 'confirmed'", (booking_id,)).fetchone()
        if row is None:
            return None
        b = _row_to_booking(row)
        conn.execute(
            "INSERT INTO cancelled_bookings (id, call_sid, client_id, caller_name, caller_phone, service, slot_start, slot_end, "
            "created_at, uid, cancelled_at, cancelled_by_call_sid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (b["id"], b["call_sid"], b["client_id"], b["caller_name"], b["caller_phone"], b["service"], b["slot_start"],
             b["slot_end"], b["created_at"], b["uid"], _now(), by_call_sid),
        )
        conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
        _log_event(conn, b["id"], b["client_id"], "cancelled", by_call_sid, b["slot_start"], None)
        return b


def move_booking(booking_id: int, new_start: str, new_end: str, *, by_call_sid: str | None = None) -> dict:
    """Give an existing booking a new time. Raises BookingConflict if another booking overlaps it."""
    try:
        with _conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(f"SELECT {_BOOKING_COLS} FROM bookings WHERE id = ? AND status = 'confirmed'", (booking_id,)).fetchone()
            if row is None:
                raise LookupError(booking_id)
            b = _row_to_booking(row)
            clash = conn.execute(
                "SELECT 1 FROM bookings WHERE client_id = ? AND status = 'confirmed' AND id != ? AND slot_start < ? AND slot_end > ? LIMIT 1",
                (b["client_id"], booking_id, new_end, new_start),
            ).fetchone()
            if clash:
                raise BookingConflict(client_id=b["client_id"], slot_start=new_start)
            conn.execute("UPDATE bookings SET slot_start = ?, slot_end = ? WHERE id = ?", (new_start, new_end, booking_id))
            _log_event(conn, booking_id, b["client_id"], "rescheduled", by_call_sid, b["slot_start"], new_start)
            return {**b, "old_start": b["slot_start"], "old_end": b["slot_end"], "slot_start": new_start, "slot_end": new_end}
    except sqlite3.IntegrityError as exc:
        raise BookingConflict(client_id="", slot_start=new_start) from exc


def get_booked_intervals(client_id: str, day_prefix: str) -> list[tuple[str, str]]:
    """(start, end) ISO strings of confirmed bookings that touch this day."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT slot_start, slot_end FROM bookings WHERE client_id = ? AND status = 'confirmed' "
            "AND (slot_start LIKE ? OR slot_end LIKE ?)",
            (client_id, f"{day_prefix}%", f"{day_prefix}%"),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]


def get_booked_slots(client_id: str, day_prefix: str) -> set[str]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT slot_start FROM bookings WHERE client_id = ? AND slot_start LIKE ? AND status = 'confirmed'",
            (client_id, f"{day_prefix}%"),
        ).fetchall()
        return {r[0] for r in rows}


def log_escalation(
    *, call_sid: str | None, client_id: str, reason: str, caller_phone: str | None, summary: str
) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO escalations (call_sid, client_id, reason, caller_phone, summary, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (call_sid, client_id, reason, caller_phone, scrub_sensitive(summary), _now()),
        )


def create_intake(payload: dict) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO intakes (created_at, payload_json) VALUES (?, ?)", (_now(), json.dumps(payload))
        )
        return int(cur.lastrowid)


def list_intakes(limit: int = 50) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, status, payload_json FROM intakes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for id_, created_at, status, payload_json in rows:
        payload = json.loads(payload_json)
        out.append({"id": id_, "created_at": created_at, "status": status,
                    "business_name": payload.get("business_name"), "owner_name": payload.get("owner_name"),
                    "owner_phone": payload.get("owner_phone"), "trade": payload.get("trade")})
    return out


def get_intake(intake_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, created_at, status, payload_json FROM intakes WHERE id = ?", (intake_id,)
        ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "created_at": row[1], "status": row[2], "payload": json.loads(row[3])}


def set_intake_status(intake_id: int, status: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE intakes SET status = ? WHERE id = ?", (status, intake_id))
