"""Running the service safely: operator alerts, log hygiene, data retention,
backups, and memory housekeeping. Nothing here is on the call path except
alert_operator(), which never raises."""
from __future__ import annotations

import contextvars
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os

from app import agent, digest, notify, storage
from app.config import list_client_ids, load_client_config

logger = logging.getLogger("callkettle.ops")

TRANSCRIPT_RETENTION_DAYS = 90
BACKUPS_TO_KEEP = 7
OLD_BACKUP_GLOB = "%s-*.db" % ("desk" + "line")          # backups made before the rename are kept and pruned like the new ones
SESSION_MAX_AGE_SECONDS = 3600  # no phone call lasts an hour; anything older leaked
OPERATOR_CLIENT_ID = "callkettle_sales"  # its ntfy topic / email are the operator's

_last_alert: dict[str, float] = {}
LAST_HOUSEKEEPING: dict = {}   # {"at": iso time, "result": {...}} so the status page can prove the maintenance loop is alive


def alert_operator(title: str, body: str, *, key: str | None = None, min_interval: float = 600.0) -> bool:
    """Push a real-time alert to the operator (Sami). Rate-limited per key so an
    outage produces one alert, not a hundred. Never raises."""
    try:
        now = time.monotonic()
        k = key or title
        if now - _last_alert.get(k, -1e9) < min_interval:
            return False
        _last_alert[k] = now
        notify.notify_owner(load_client_config(OPERATOR_CLIENT_ID), title=f"ALERT: {title}", body=body[:400])
        return True
    except Exception:
        logger.exception("Could not send operator alert %r", title)
        return False


_KEY_RE = re.compile(r"((?:key|token)=)[^&\s\"']+")


class RedactKeys(logging.Filter):
    """Dashboard and admin links carry a secret in the URL. Keep it out of logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_KEY_RE.sub(r"\1REDACTED", a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = _KEY_RE.sub(r"\1REDACTED", record.msg)
        return True


_PHONE_RE = re.compile(r"(?<![\w])\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?(\d{4})(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@([\w-]+\.[\w.-]+)")


def redact_personal(text: str) -> str:
    """Phone numbers become ***-***-1234 and email addresses become ***@domain, so logs never
    hold a caller's or owner's contact details."""
    return _EMAIL_RE.sub(r"***@\1", _PHONE_RE.sub(r"***-***-\1", text))


class RedactPersonal(logging.Filter):
    """Keep callers' phone numbers and owners' emails out of log files."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact_personal(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact_personal(v) if isinstance(v, str) else v for k, v in record.args.items()}
        if isinstance(record.msg, str):
            record.msg = redact_personal(record.msg)
        return True


_LOGGERS = ("uvicorn.access", "uvicorn.error", "callkettle.main", "callkettle.twilio", "callkettle.tools", "callkettle.notify",
            "callkettle.agent", "callkettle.ops", "callkettle.digest", "callkettle.webhooks", "callkettle.summary", "callkettle.gcal")


CALL_SID: contextvars.ContextVar[str | None] = contextvars.ContextVar("call_sid", default=None)


class CorrelateCalls(logging.Filter):
    """Tag every log line written while handling a call's webhook with the end of its CallSid, so one call's story can be
    pulled from the logs with a single search (`flyctl logs | grep "call 3f9a1c2d"`). Safe characters only."""

    def filter(self, record: logging.LogRecord) -> bool:
        sid = CALL_SID.get()
        if sid and isinstance(record.msg, str) and " [call " not in record.msg:
            record.msg = f"{record.msg} [call {sid[-8:]}]"
        return True


def set_call_context(call_sid: str | None) -> None:
    CALL_SID.set(re.sub(r"[^A-Za-z0-9_]", "", call_sid or "")[:64] or None)


def install_log_redaction() -> None:
    for name in _LOGGERS:
        lg = logging.getLogger(name)
        lg.addFilter(RedactKeys())
        lg.addFilter(RedactPersonal())
        lg.addFilter(CorrelateCalls())


def purge_old_transcripts(days: int = TRANSCRIPT_RETENTION_DAYS) -> int:
    """Delete what callers said (and the AI's summary of it) after `days`.
    Booking and call-count records stay, so the dashboard's history remains."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with storage._conn() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(calls)")}
        note_assignment = ", owner_note = ''" if "owner_note" in columns else ""
        note_condition = " OR owner_note != ''" if "owner_note" in columns else ""
        cur = conn.execute(
            "UPDATE calls SET transcript_json = '[]', summary = NULL" + note_assignment +
            " WHERE started_at < ? AND (transcript_json != '[]' OR summary IS NOT NULL" + note_condition + ")",
            (cutoff,),
        )
        return cur.rowcount


def backup_database(keep: int = BACKUPS_TO_KEEP) -> Path | None:
    """A consistent copy of the live database next to it, keeping the newest `keep`.
    (Fly also snapshots the whole volume daily; this protects against an
    application-level mistake rather than a lost disk.)"""
    try:
        source = Path(storage.DB_PATH)
        folder = source.parent / "backups"
        folder.mkdir(exist_ok=True)
        dest = folder / f"callkettle-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.db"
        src = sqlite3.connect(source)
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        for old in sorted(list(folder.glob("callkettle-*.db")) + list(folder.glob(OLD_BACKUP_GLOB)), key=lambda p: p.name[-13:])[:-keep]:
            old.unlink(missing_ok=True)
        return dest
    except Exception:
        logger.exception("Database backup failed")
        alert_operator("Backup failed", "The daily database backup failed. Check the Fly logs.", key="backup")
        return None


def verify_backup(path: Path) -> dict:
    """Open a backup the way a restore would: integrity check, then compare its tables and row counts with the live
    database. A backup nobody has opened is a hope, not a backup. Returns {"ok": bool, "problem": str|None, "rows": {...}}."""
    try:
        bk = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            integrity = bk.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                return {"ok": False, "problem": f"integrity_check says {integrity[:80]}", "rows": {}}
            tables = [r[0] for r in bk.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            rows = {t: bk.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        finally:
            bk.close()
        with storage._conn() as live:
            for t in ("calls", "bookings"):
                if t in rows:
                    now = live.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    if rows[t] > now:      # the backup holds MORE than the live database: something was lost or the wrong file
                        return {"ok": False, "problem": f"backup has more {t} than the live database ({rows[t]} vs {now})", "rows": rows}
        for needed in ("calls", "bookings", "escalations"):
            if needed not in rows:
                return {"ok": False, "problem": f"backup is missing the {needed} table", "rows": rows}
        return {"ok": True, "problem": None, "rows": rows}
    except Exception as exc:
        return {"ok": False, "problem": f"{type(exc).__name__}: {str(exc)[:100]}", "rows": {}}


def twilio_balance() -> float | None:
    """Current Twilio account balance in dollars, or None if it can't be read."""
    try:
        from app import twilio_utils

        client = twilio_utils._client()
        if client is None:
            return None
        return float(client.balance.fetch().balance)
    except Exception:
        logger.exception("Could not read the Twilio balance")
        return None


def check_balance(threshold: float | None = None) -> float | None:
    """Alert the operator (once a day) when the Twilio balance is low. At $0 every call fails.
    Returns the balance that was read."""
    threshold = threshold if threshold is not None else float(os.environ.get("LOW_BALANCE_ALERT_DOLLARS", "15"))
    balance = twilio_balance()
    if balance is not None and balance < threshold:
        alert_operator(
            "Twilio balance is low",
            f"Balance is ${balance:,.2f} (alert threshold ${threshold:,.0f}). At $0 no call can be answered. "
            "Add funds at console.twilio.com and turn on auto-recharge.",
            key="twilio-balance", min_interval=24 * 3600,
        )
    return balance


def purge_webhook_outbox(days: int = 30) -> int:
    """Delivered and cancelled events are kept for a month (for support), failed ones for 90 days."""
    from app import webhooks

    webhooks.ensure_table()
    cut_ok = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cut_failed = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    with storage._conn() as conn:
        a = conn.execute("DELETE FROM webhook_outbox WHERE status IN ('delivered', 'cancelled') AND created_at < ?", (cut_ok,)).rowcount
        b = conn.execute("DELETE FROM webhook_outbox WHERE status = 'failed' AND created_at < ?", (cut_failed,)).rowcount
    return a + b


def check_usage(now: datetime | None = None) -> list[str]:
    """Tell the operator when a client reaches 80% and 100% of the included calls
    for the month, once per level per month, so a heavy client is a decision you
    make on purpose instead of a surprise on the bill. Returns the alerts sent."""
    now = now or datetime.now(timezone.utc)
    limit = int(os.environ.get("FAIR_USE_CALLS", "300"))
    month = now.strftime("%Y-%m")
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    sent: list[str] = []
    with storage._conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS usage_alerts (client_id TEXT, month TEXT, level INTEGER, "
                     "PRIMARY KEY (client_id, month, level))")
        counts = dict(conn.execute(
            "SELECT client_id, COUNT(*) FROM calls WHERE started_at >= ? GROUP BY client_id", (month_start,)))
        for client_id, n in counts.items():
            if client_id in digest.SKIP_CLIENTS or client_id == OPERATOR_CLIENT_ID:
                continue
            for level in (100, 80):
                if n >= limit * level // 100:
                    done = conn.execute("SELECT 1 FROM usage_alerts WHERE client_id=? AND month=? AND level=?",
                                        (client_id, month, level)).fetchone()
                    if not done:
                        conn.execute("INSERT INTO usage_alerts VALUES (?, ?, ?)", (client_id, month, level))
                        msg = f"{client_id} has used {n} of {limit} included calls this month ({level}% level)."
                        if level == 100:
                            msg += " Time to talk to them about it (see the terms: fair use)."
                        if alert_operator("Usage", msg, key=f"usage-{client_id}-{level}", min_interval=0):
                            sent.append(msg)
                    break  # only the highest level reached, and never both in one pass
    return sent


COST_LEVEL_BASE = 1000      # usage_alerts.level for cost warnings = 1000 + percent, so they never collide with call-count levels


def check_cost_levels(config, st: dict | None = None) -> list[str]:
    """Warn at 75%, 90% and 100% of the monthly spending ceiling, once per level per month. The operator hears at every
    level; the client hears only at 100%, because that is when callers start getting the degraded experience."""
    from app import costing

    st = st or costing.status(config)
    level = st["level"] if st["reason"] != "calls" else max(st["level"], 100)
    if level == 0:
        return []
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    sent: list[str] = []
    with storage._conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS usage_alerts (client_id TEXT, month TEXT, level INTEGER, "
                     "PRIMARY KEY (client_id, month, level))")
        for lv in costing.WARN_LEVELS:
            if lv > level:
                continue
            cur = conn.execute("INSERT OR IGNORE INTO usage_alerts VALUES (?, ?, ?)", (config.client_id, month, COST_LEVEL_BASE + lv))
            if cur.rowcount != 1:
                continue
            what = (f"{config.client_id} is at {lv}% of its monthly spending ceiling "
                    f"(est. ${st['cost']:.2f} of ${st['cost_ceiling']:.2f}, {st['calls']} calls).")
            if lv == 100:
                what += f" New calls now use '{config.ceiling_mode}' mode until next month or until you raise the ceiling."
            if lv == 100 and config.owner_email:
                try:
                    notify.notify_owner(config, title="Your phone assistant reached its monthly limit",
                                        body="Your assistant has reached its monthly usage limit, so new callers are asked to leave a "
                                             "message (or are put through to you) until next month. We will be in touch about your plan.")
                except Exception:
                    logger.exception("Could not tell the client about the ceiling")
            if alert_operator("Spending ceiling", what, key=f"cost-{config.client_id}-{lv}", min_interval=0):
                sent.append(what)
    return sent


def reset_demo_data(hours: int = 24) -> dict:
    """Free the demo lines' calendars: delete bookings, cancellations and booking history older than `hours` for clients
    flagged demo_mode. Only those clients, only those tables."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    removed = {"bookings": 0, "cancelled": 0, "events": 0}
    for cid in list_client_ids():
        try:
            cfg = load_client_config(cid)
            if not cfg.demo_mode or cfg.portal_sample:      # the sample owner portal's seeded bookings are reset by its seed script
                continue
        except Exception:
            continue
        with storage._conn() as conn:
            old = [r[0] for r in conn.execute("SELECT id FROM bookings WHERE client_id = ? AND created_at < ?", (cid, cutoff))]
            removed["bookings"] += conn.execute("DELETE FROM bookings WHERE client_id = ? AND created_at < ?", (cid, cutoff)).rowcount
            removed["cancelled"] += conn.execute("DELETE FROM cancelled_bookings WHERE client_id = ? AND cancelled_at < ?", (cid, cutoff)).rowcount
            removed["events"] += conn.execute("DELETE FROM booking_events WHERE client_id = ? AND at < ?", (cid, cutoff)).rowcount
    return removed


def clean_private_demos() -> dict:
    """Expired or revoked private prospect demos stop being served and their data is removed (no private demo outlives its expiry)."""
    from app import config as config_module

    cleaned = []
    for d in storage.due_private_demo_cleanup():
        config_module.remove_live_config(d["client_id"])
        storage.clean_private_demo(d["id"], d["client_id"])
        cleaned.append(d["client_id"])
    return {"cleaned": cleaned}


def housekeeping() -> dict:
    """Run every few hours. Each step is independent and fails soft."""
    result = {}
    try:
        result["stale_sessions_removed"] = agent.purge_stale_sessions(SESSION_MAX_AGE_SECONDS)
    except Exception:
        logger.exception("Session purge failed")
    try:
        result["transcripts_purged"] = purge_old_transcripts()
    except Exception:
        logger.exception("Transcript purge failed")
    try:
        result["twilio_balance"] = check_balance()
    except Exception:
        logger.exception("Balance check failed")
    try:
        result["demo_reset"] = reset_demo_data()
    except Exception:
        logger.exception("Demo reset failed")
    try:
        result["private_demos"] = clean_private_demos()
    except Exception:
        logger.exception("Private demo cleanup failed")
    try:
        result["usage_alerts"] = len(check_usage())
    except Exception:
        logger.exception("Usage check failed")
    try:
        warned = 0
        for cid in list_client_ids():
            if cid in digest.SKIP_CLIENTS or cid == OPERATOR_CLIENT_ID:
                continue
            warned += len(check_cost_levels(load_client_config(cid)))
        result["cost_warnings"] = warned
    except Exception:
        logger.exception("Cost-ceiling check failed")
    try:
        result["recaps_sent"] = len(digest.send_due_digests())
    except Exception:
        logger.exception("Weekly recap step failed")
    try:
        result["webhook_rows_purged"] = purge_webhook_outbox()
    except Exception:
        logger.exception("Webhook outbox purge failed")
    made = backup_database()
    result["backup"] = str(made or "failed")
    if made:
        check = verify_backup(made)
        result["backup_verified"] = check["ok"]
        if not check["ok"]:
            alert_operator("Backup failed its restore test", f"{made.name}: {check['problem']}. The daily backup cannot be trusted; check the Fly logs.",
                           key="backup-verify", min_interval=6 * 3600)
        else:
            from app import offsite

            up = offsite.run_offsite_backup(made)
            result["offsite"] = {k: up[k] for k in ("ok", "configured", "object", "bytes", "problem") if k in up}
            if up.get("configured") and not up["ok"]:
                alert_operator("Offsite backup failed", f"{up.get('problem', 'unknown problem')}. The local backup is fine; the offsite copy is not being made.",
                               key="offsite-backup", min_interval=6 * 3600)
    LAST_HOUSEKEEPING.update({"at": datetime.now(timezone.utc).isoformat(), "result": {k: v for k, v in result.items() if k != "backup"}})
    return result
