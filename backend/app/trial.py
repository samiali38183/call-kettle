"""Operator-approved trials. No billing integration or activation on import."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app import storage

DAYS = 7
CALLS = 30


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Use an explicit timezone for go-live.")
    return value.astimezone(timezone.utc)


def _init(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS customer_trials (client_id TEXT PRIMARY KEY, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, approved_by TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active', changed_by TEXT, changed_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS trial_admissions (call_sid TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES customer_trials(client_id), admitted_at TEXT NOT NULL)")


def validate_trial_config(config) -> None:
    # Normal/error transfers still use Twilio's four-hour default. Until those
    # paths are separately bounded, a trial must never have a dialable target.
    if (not getattr(config, "trial_enabled", False) or config.demo_mode or config.demo_menu
            or config.demo_private_codes or config.portal_sample
            or config.client_id.startswith(("demo_", "prep_", "sample_"))):
        raise ValueError("Enable trials explicitly on a real, reviewed tenant only.")
    if (config.escalation_phone.strip() or config.routing_mode != "ai_first" or config.always_ring_owner
            or config.policy.can_transfer or config.policy.emergency_action != "message"
            or config.ceiling_mode != "message" or config.stt_mode != "gather"):
        raise ValueError("Trial requires message-only escalation, blank escalation_phone, ai_first, no VIP routing and Gather.")
    if not (1 <= config.max_turns <= 12 and 1 <= config.max_call_seconds <= 360):
        raise ValueError("Trial limits must be positive and no more than 12 turns / 360 seconds.")


def activate(config, starts_at: datetime, *, approved_by: str, owner_notified: bool, apply: bool = False) -> dict:
    validate_trial_config(config)
    if not approved_by.strip() or len(approved_by) > 100 or owner_notified is not True:
        raise ValueError("Operator identity and confirmed owner notice are required.")
    start = _utc(starts_at)
    plan = {"client_id": config.client_id, "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(days=DAYS)).isoformat(), "max_calls": CALLS, "applied": apply}
    if apply:
        with storage._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _init(conn)
            if conn.execute("SELECT 1 FROM customer_trials WHERE client_id=?", (config.client_id,)).fetchone():
                raise ValueError("A trial already exists; it cannot be restarted or extended.")
            conn.execute("INSERT INTO customer_trials(client_id, starts_at, ends_at, approved_by) VALUES (?, ?, ?, ?)",
                         (config.client_id, plan["starts_at"], plan["ends_at"], approved_by))
    return plan


def admit(config, call_sid: str, *, now: datetime | None = None) -> Decision:
    if not getattr(config, "trial_enabled", False):
        return Decision(True, "not_trial")
    if not isinstance(call_sid, str) or not 1 <= len(call_sid) <= 128 or not call_sid.isalnum():
        return Decision(False, "invalid_call_sid")
    try:
        return _admit(config, call_sid, _utc(now or datetime.now(timezone.utc)))
    except Exception:
        # Missing schema, locks, I/O, malformed evidence: never enter a paid pipeline.
        return Decision(False, "evidence_unavailable")


def _admit(config, call_sid: str, now: datetime) -> Decision:
    with storage._conn(timeout=1.5) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT starts_at, ends_at, state FROM customer_trials WHERE client_id=?", (config.client_id,)).fetchone()
        if not row:
            return Decision(False, "missing_evidence")
        if row[2] == "converted":
            return Decision(True, "converted")
        if row[2] != "active":
            return Decision(False, "stopped")
        validate_trial_config(config)
        start = _utc(datetime.fromisoformat(row[0]))
        stored_end = _utc(datetime.fromisoformat(row[1]))
        end = min(stored_end, start + timedelta(days=DAYS))
        if not start <= now < end:
            return Decision(False, "outside_window")
        prior = conn.execute("SELECT client_id FROM trial_admissions WHERE call_sid=?", (call_sid,)).fetchone()
        if prior:
            return Decision(prior[0] == config.client_id, "duplicate" if prior[0] == config.client_id else "tenant_mismatch")
        used = conn.execute("SELECT COUNT(*) FROM trial_admissions WHERE client_id=?", (config.client_id,)).fetchone()[0]
        if used >= CALLS:
            return Decision(False, "quota_reached")
        conn.execute("INSERT INTO trial_admissions VALUES (?, ?, ?)", (call_sid, config.client_id, now.isoformat()))
        return Decision(True, "admitted")


def status(client_id: str, *, now: datetime | None = None) -> dict:
    """Read-only, including on a database without a trial schema."""
    import sqlite3
    from pathlib import Path
    if not Path(storage.DB_PATH).is_file():
        return {"client_id": client_id, "state": "not_configured"}
    uri = Path(storage.DB_PATH).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1.5)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='customer_trials'").fetchone():
            return {"client_id": client_id, "state": "not_configured"}
        row = conn.execute("SELECT starts_at, ends_at, state FROM customer_trials WHERE client_id=?", (client_id,)).fetchone()
        if not row:
            return {"client_id": client_id, "state": "not_configured"}
        used = conn.execute("SELECT COUNT(*) FROM trial_admissions WHERE client_id=?", (client_id,)).fetchone()[0]
        instant = _utc(now or datetime.now(timezone.utc))
        state = row[2]
        start = _utc(datetime.fromisoformat(row[0]))
        end = min(_utc(datetime.fromisoformat(row[1])), start + timedelta(days=DAYS))
        if state == "active":
            state = ("scheduled" if instant < start else
                     "expired" if instant >= end else
                     "exhausted" if used >= CALLS else "active")
        return {"client_id": client_id, "starts_at": row[0], "ends_at": row[1], "state": state,
                "calls_reserved": used, "remaining_calls": max(0, CALLS - used)}
    finally:
        conn.close()


def transition(client_id: str, state: str, *, approved_by: str, agreement_confirmed: bool, apply: bool = False) -> dict:
    if state not in ("stopped", "converted") or not approved_by.strip() or len(approved_by) > 100:
        raise ValueError("Explicit operator and stop/convert action required.")
    if state == "converted" and agreement_confirmed is not True:
        raise ValueError("Conversion requires a separately confirmed written paid-service agreement; no billing is performed.")
    existing = status(client_id)
    if existing["state"] == "not_configured":
        raise ValueError("No trial exists for this tenant.")
    if apply:
        with storage._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute("UPDATE customer_trials SET state=?, changed_by=?, changed_at=? WHERE client_id=?",
                                   (state, approved_by, datetime.now(timezone.utc).isoformat(), client_id))
            if updated.rowcount != 1:
                raise ValueError("Trial disappeared; transition refused.")
        return {**status(client_id), "applied": True}
    return {**existing, "planned_state": state, "applied": False}
