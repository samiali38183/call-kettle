"""Additive owner workspace storage. Initialize after storage.init_db(), never on import.

Client IDs passed here must come from the authenticated server-side session.
Columns on calls deliberately follow call retention/deletion instead of leaving orphan notes.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timezone

from app import storage

# Legacy handled actions remain authoritative even when explicit workspace state exists.
_STATE_SQL = "CASE WHEN attention_resolved_at IS NOT NULL THEN 'handled' WHEN followup_state IS NOT NULL THEN followup_state WHEN needs_attention = 1 THEN 'open' ELSE 'none' END"


def init_db() -> None:
    """Idempotent, serialized migration against the currently configured database."""
    with storage._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(calls)")}
        for name, declaration in (("owner_note", "TEXT NOT NULL DEFAULT ''"), ("followup_state", "TEXT"),
                                  ("followup_due_date", "TEXT"), ("followup_updated_at", "TEXT")):
            if name not in columns:
                conn.execute(f"ALTER TABLE calls ADD COLUMN {name} {declaration}")


def get_call(client_id: str, call_sid: str) -> dict | None:
    with storage._conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT *, {_STATE_SQL} AS state FROM calls WHERE client_id = ? AND call_sid = ?", (client_id, call_sid)).fetchone()
    return dict(row) if row else None


MAX_NOTE_LENGTH = 4000
STATES = ("open", "waiting", "handled", "none")


def _validate_note(call_sid: str, note: str) -> None:
    if not 1 <= len(call_sid) <= 128:
        raise ValueError("Invalid call identifier.")
    if len(note) > MAX_NOTE_LENGTH:
        raise ValueError("Owner notes must be 4000 characters or fewer.")


def save_note(client_id: str, call_sid: str, note: str) -> None:
    note = note.replace("\r\n", "\n").replace("\r", "\n")
    _validate_note(call_sid, note)
    with storage._conn() as conn:
        updated = conn.execute("UPDATE calls SET owner_note = ? WHERE client_id = ? AND call_sid = ?", (note, client_id, call_sid))
        if updated.rowcount != 1:
            raise LookupError("Call not found.")


def list_calls(client_id: str, *, view: str = "queue", page: int = 1, per_page: int = 50) -> tuple[list[dict], int]:
    """Open/waiting calls sorted by due date then oldest call; count is untruncated."""
    if view not in ("queue", "handled", "all") or not 1 <= page <= 10000 or not 1 <= per_page <= 100:
        raise ValueError("Invalid workspace filter or page.")
    where = "client_id = ?"
    if view == "queue":
        where += f" AND ({_STATE_SQL}) IN ('open', 'waiting')"
    elif view == "handled":
        where += f" AND ({_STATE_SQL}) = 'handled'"
    with storage._conn() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        total = conn.execute(f"SELECT COUNT(*) FROM calls WHERE {where}", (client_id,)).fetchone()[0]
        rows = conn.execute(
            f"SELECT *, {_STATE_SQL} AS state FROM calls WHERE {where} "
            "ORDER BY followup_due_date IS NULL, followup_due_date, started_at, call_sid LIMIT ? OFFSET ?",
            (client_id, per_page, (page - 1) * per_page)).fetchall()
    return [dict(row) for row in rows], total


class FollowupConflict(Exception):
    """Another tab or action changed the form's record."""


def revision(row: dict) -> str:
    fields = ("owner_note", "followup_state", "followup_due_date", "followup_updated_at", "needs_attention", "attention_resolved_at")
    return hashlib.sha256(json.dumps([row.get(field) for field in fields], ensure_ascii=True).encode()).hexdigest()


def save_followup(client_id: str, call_sid: str, *, note: str, state: str, due_date: str, expected_revision: str | None = None) -> None:
    """Atomically save owner annotations and synchronize the existing attention queue."""
    note = note.replace("\r\n", "\n").replace("\r", "\n")
    _validate_note(call_sid, note)
    if state not in STATES:
        raise ValueError("Choose a valid follow-up state.")
    if due_date:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", due_date):
            raise ValueError("Use a valid due date in YYYY-MM-DD format.")
        try:
            date.fromisoformat(due_date)
        except ValueError as exc:
            raise ValueError("Use a valid calendar date.") from exc
        if state in ("handled", "none"):
            raise ValueError("Clear the due date when marking handled or no follow-up.")
    now = datetime.now(timezone.utc).isoformat()
    with storage._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if expected_revision is not None:
            conn.row_factory = sqlite3.Row
            current = conn.execute("SELECT * FROM calls WHERE client_id = ? AND call_sid = ?", (client_id, call_sid)).fetchone()
            if current is None:
                raise LookupError("Call not found.")
            if not expected_revision or revision(dict(current)) != expected_revision:
                raise FollowupConflict("This call changed in another tab. Reload its follow-up before saving; your changes were not saved.")
        updated = conn.execute(
            "UPDATE calls SET owner_note = ?, followup_state = ?, followup_due_date = ?, followup_updated_at = ?, "
            "needs_attention = ?, attention_resolved_at = ? WHERE client_id = ? AND call_sid = ?",
            (note, state, due_date or None, now, int(state != "none"), now if state == "handled" else None, client_id, call_sid))
        if updated.rowcount != 1:
            raise LookupError("Call not found.")
