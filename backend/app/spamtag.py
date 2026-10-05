"""Owner-driven 'not a customer' (spam / robocall) tagging.

The owner taps once on a callback that is not a real customer. That call becomes SPAM, leaves the follow-up queue, and the caller ID is remembered FOR THIS
BUSINESS ONLY, so later calls from it are tagged SPAM and do not page the owner. Nothing is blocked, no caller is contacted, and the receptionist still
answers (this changes who gets alerted, not what a caller hears). Emergencies are never suppressed. Undo restores the call. Caller IDs are stored as
digits only (last 10), so nothing hostile can be stored or rendered. Every query is scoped by client_id; the id always comes from the signed-in session.
"""
from __future__ import annotations

import os
import re
import sqlite3

from app import storage

_TABLE = ("CREATE TABLE IF NOT EXISTS spam_numbers (client_id TEXT NOT NULL, number TEXT NOT NULL, marked_at TEXT NOT NULL, "
          "call_sid TEXT, prev_class TEXT, prev_attention INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (client_id, number))")


def enabled() -> bool:
    """Operator opt-in; absent, false and unrecognized values stay disabled."""
    return os.getenv("CALLKETTLE_SPAM_TAGGING_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def key(number: str | None) -> str:
    digits = re.sub(r"\D", "", number or "")
    return digits[-10:] if len(digits) >= 10 else ""


def ensure_table(conn) -> None:
    conn.execute(_TABLE)


def marked_in(conn, client_id: str, number: str | None) -> bool:
    """For use inside an open connection (classification). False on any problem: failing open means the owner is still told."""
    if not enabled():
        return False
    k = key(number)
    if not k:
        return False
    try:
        return conn.execute("SELECT 1 FROM spam_numbers WHERE client_id = ? AND number = ?", (client_id, k)).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def is_spam(client_id: str, number: str | None) -> bool:
    if not enabled() or not key(number):
        return False
    with storage._conn() as conn:
        return marked_in(conn, client_id, number)


def call_is_spam(client_id: str, call_sid: str | None) -> bool:
    """Is the caller ID of this call (as recorded, not as spoken) marked by this business? Never raises."""
    if not enabled() or not call_sid:
        return False
    try:
        with storage._conn() as conn:
            row = conn.execute("SELECT from_number FROM calls WHERE call_sid = ? AND client_id = ?", (call_sid, client_id)).fetchone()
            return bool(row) and marked_in(conn, client_id, row[0])
    except Exception:
        return False


def mark(client_id: str, call_sid: str) -> bool:
    """Tag this call (must belong to client_id) as spam and remember its caller ID. False if the call is not this client's."""
    if not enabled():
        return False
    with storage._conn() as conn:
        ensure_table(conn)
        row = conn.execute("SELECT from_number, outcome_class, needs_attention FROM calls WHERE call_sid = ? AND client_id = ?", (call_sid, client_id)).fetchone()
        if row is None:
            return False
        if row[1] == "SPAM":
            return True  # duplicate submit: retain the original outcome used by Undo
        now = storage._now()
        # attention_resolved_at is already authoritative for the workspace queue.
        # Keep the owner's state/due date untouched so Undo does not discard their work.
        conn.execute("UPDATE calls SET outcome_class = 'SPAM', needs_attention = 0, attention_resolved_at = ? WHERE call_sid = ? AND client_id = ?",
                     (now, call_sid, client_id))
        k = key(row[0])
        if k:
            conn.execute("INSERT OR REPLACE INTO spam_numbers (client_id, number, marked_at, call_sid, prev_class, prev_attention) VALUES (?,?,?,?,?,?)",
                         (client_id, k, now, call_sid, row[1], int(row[2] or 0)))
    return True


def undo(client_id: str, call_sid: str) -> bool:
    """Un-mark: forget the caller ID and restore the call(s) that were tagged because of it."""
    if not enabled():
        return False
    with storage._conn() as conn:
        ensure_table(conn)
        marked = conn.execute("SELECT number, prev_class, prev_attention FROM spam_numbers WHERE client_id = ? AND call_sid = ?", (client_id, call_sid)).fetchone()
        row = conn.execute("SELECT from_number, outcome_class FROM calls WHERE call_sid = ? AND client_id = ?", (call_sid, client_id)).fetchone()
        if row is None or row[1] != "SPAM":
            return False
        k = marked[0] if marked else key(row[0])
        if k:
            conn.execute("DELETE FROM spam_numbers WHERE client_id = ? AND number = ?", (client_id, k))
        prev_class, prev_attention = (marked[1], marked[2]) if marked else (None, 0)
        conn.execute("UPDATE calls SET outcome_class = ?, needs_attention = ?, attention_resolved_at = NULL WHERE call_sid = ? AND client_id = ?",
                     (prev_class, prev_attention, call_sid, client_id))
    # An anonymous call has no caller-ID record in which to preserve its old class.
    # Recompute from recorded facts rather than losing a pending callback on Undo.
    if marked is None:
        storage.classify_and_store(call_sid)
    # Other calls auto-tagged because of this number are recomputed from the facts the system recorded.
    if k:
        with storage._conn() as conn:
            same = [r[0] for r in conn.execute("SELECT call_sid, from_number FROM calls WHERE client_id = ? AND outcome_class = 'SPAM' AND call_sid != ?", (client_id, call_sid)).fetchall()
                    if key(r[1]) == k]
        for sid in same:
            storage.classify_and_store(sid)
    return True


def marked_list(client_id: str, limit: int = 20) -> list[tuple[str, str, str]]:
    """(number digits, marked_at iso, call_sid) newest first, for this client only."""
    if not enabled():
        return []
    with storage._conn() as conn:
        ensure_table(conn)
        return conn.execute("SELECT number, marked_at, call_sid FROM spam_numbers WHERE client_id = ? AND call_sid IS NOT NULL ORDER BY marked_at DESC LIMIT ?", (client_id, limit)).fetchall()
