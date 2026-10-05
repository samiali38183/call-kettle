"""Outbound webhooks: a durable outbox, signed with a timestamp, retried for about 31 hours, SSRF-safe, replay-safe."""
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

SECRET = "a-long-enough-secret-123"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("CALLKETTLE_DB_PATH", path)
    monkeypatch.setenv("CALLKETTLE_ALLOW_PRIVATE_WEBHOOKS", "1")
    monkeypatch.setenv("CALLKETTLE_DISABLE_PUSH", "1")
    import importlib

    from app import storage

    importlib.reload(storage)
    storage.init_db()
    yield storage
    try:
        os.remove(path)
    except PermissionError:
        pass


class _Receiver:
    def __init__(self, behaviour=None):
        self.behaviour = list(behaviour or [])   # per request: an int status, or ("redirect", url), or ("big", n)
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append((dict(self.headers), body))
                step = outer.behaviour.pop(0) if outer.behaviour else 200
                if isinstance(step, tuple) and step[0] == "redirect":
                    self.send_response(302)
                    self.send_header("Location", step[1])
                    self.end_headers()
                elif isinstance(step, tuple) and step[0] == "big":
                    self.send_response(200)
                    self.send_header("Content-Length", str(step[1]))
                    self.end_headers()
                    try:
                        self.wfile.write(b"x" * step[1])
                    except Exception:
                        pass
                else:
                    self.send_response(step)
                    self.end_headers()

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def receiver():
    made = []

    def make(behaviour=None):
        r = _Receiver(behaviour)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


def _cfg(url, cid="demo_hvac"):
    from app.config import load_client_config

    return load_client_config(cid).model_copy(update={"webhook_url": url, "webhook_secret": SECRET})


@pytest.fixture
def serve(monkeypatch):
    """Make process_outbox see a config with a webhook (configs come from YAML in production)."""
    def _serve(cfg):
        from app import webhooks

        monkeypatch.setattr(webhooks, "load_client_config", lambda cid: cfg)
    return _serve


def _rows(storage):
    import sqlite3

    from app import webhooks

    webhooks.ensure_table()
    conn = sqlite3.connect(storage.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows =[dict(r) for r in conn.execute("SELECT * FROM webhook_outbox ORDER BY id")]
    conn.close()
    return rows


def _at(seconds):
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# ---------------------------------------------------------------- the outbox

def test_emit_only_writes_a_row_and_never_touches_the_network(env, receiver):
    from app import webhooks

    r = receiver()
    assert webhooks.emit(_cfg(r.url), "booking.created", {"x": 1}, event_id="booking.created:demo_hvac:1") is True
    assert r.requests == []
    row = _rows(env)[0]
    assert row["status"] == "pending" and row["event"] == "booking.created" and json.loads(row["payload_json"])["data"] == {"x": 1}


def test_an_event_survives_a_restart_and_is_delivered_afterwards(env, receiver, serve):
    """The event is queued, the 'process' dies, a fresh process delivers it."""
    import importlib

    from app import webhooks

    r = receiver()
    cfg = _cfg(r.url)
    webhooks.emit(cfg, "booking.created", {"booking_id": 7}, event_id="e1")
    importlib.reload(webhooks)                       # a brand new process: no memory of the queued event
    serve(cfg)
    stats = webhooks.process_outbox()
    assert stats["delivered"] == 1 and json.loads(r.requests[0][1])["data"]["booking_id"] == 7
    assert _rows(env)[0]["status"] == "delivered"


def test_the_same_event_id_is_queued_once(env, receiver):
    from app import webhooks

    cfg = _cfg(receiver().url)
    assert webhooks.emit(cfg, "booking.created", {}, event_id="dup") is True
    assert webhooks.emit(cfg, "booking.created", {}, event_id="dup") is False
    assert len(_rows(env)) == 1


def test_no_webhook_configured_means_nothing_is_queued(env):
    from app import webhooks
    from app.config import load_client_config

    assert webhooks.emit(load_client_config("demo_hvac"), "booking.created", {}) is False and _rows(env) == []


def test_failures_back_off_on_the_published_schedule_and_keep_the_same_delivery_id(env, receiver, serve):
    from app import webhooks

    r = receiver([500] * 8)
    cfg = _cfg(r.url)
    serve(cfg)
    webhooks.emit(cfg, "booking.created", {}, event_id="retry-me")
    delays = []
    t = datetime.now(timezone.utc)
    for attempt in range(1, 8):
        stats = webhooks.process_outbox(now=t)
        assert stats["retrying"] == 1, attempt
        row = _rows(env)[0]
        delays.append(round((datetime.fromisoformat(row["next_attempt_at"]) - t).total_seconds()))
        t = datetime.fromisoformat(row["next_attempt_at"]) + timedelta(seconds=1)
    assert delays == [10, 30, 120, 600, 3600, 21600, 86400]
    assert len({h["X-Delivery"] for h, _ in r.requests}) == 1                      # receivers can de-duplicate


def test_not_yet_due_events_are_left_alone(env, receiver, serve):
    from app import webhooks

    r = receiver([500])
    cfg = _cfg(r.url)
    serve(cfg)
    webhooks.emit(cfg, "booking.created", {}, event_id="later")
    webhooks.process_outbox()                                                       # attempt 1 fails, next in 10 s
    assert webhooks.process_outbox()["retrying"] == 0 and len(r.requests) == 1


def test_a_recovering_receiver_gets_the_event_on_a_later_attempt(env, receiver, serve):
    from app import webhooks

    r = receiver([503, 503, 200])
    cfg = _cfg(r.url)
    serve(cfg)
    webhooks.emit(cfg, "booking.created", {}, event_id="recover")
    now = datetime.now(timezone.utc)
    webhooks.process_outbox(now=now)
    webhooks.process_outbox(now=now + timedelta(seconds=15))
    assert webhooks.process_outbox(now=now + timedelta(minutes=5))["delivered"] == 1
    assert _rows(env)[0]["status"] == "delivered" and _rows(env)[0]["attempts"] == 3


def test_after_the_last_attempt_it_is_marked_failed_and_the_operator_is_alerted(env, receiver, serve, monkeypatch):
    from app import ops, webhooks

    alerts = []
    monkeypatch.setattr(ops, "alert_operator", lambda title, body, **kw: alerts.append((title, body)) or True)
    r = receiver([500] * 10)
    cfg = _cfg(r.url)
    serve(cfg)
    webhooks.emit(cfg, "booking.created", {}, event_id="doomed")
    t = datetime.now(timezone.utc)
    for _ in range(webhooks.MAX_ATTEMPTS):
        webhooks.process_outbox(now=t)
        t += timedelta(days=2)
    row = _rows(env)[0]
    assert row["status"] == "failed" and row["attempts"] == webhooks.MAX_ATTEMPTS and "HTTP 500" in row["last_error"]
    assert alerts and "webhook is failing" in alerts[0][0]
    assert webhooks.outbox_health()["failed"] == 1


def test_removing_a_clients_webhook_cancels_its_queued_events(env, receiver, serve):
    from app import webhooks
    from app.config import load_client_config

    cfg = _cfg(receiver().url)
    webhooks.emit(cfg, "booking.created", {}, event_id="orphan")
    serve(load_client_config("demo_hvac"))            # the webhook was removed from the config
    assert webhooks.process_outbox()["cancelled"] == 1 and _rows(env)[0]["status"] == "cancelled"


def test_old_rows_are_purged_but_recent_ones_stay(env):
    from app import ops, webhooks

    webhooks.ensure_table()
    import sqlite3

    conn = sqlite3.connect(env.DB_PATH)
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    for i, status in enumerate(["delivered", "cancelled", "failed", "pending"]):
        conn.execute("INSERT INTO webhook_outbox (event_id, client_id, event, payload_json, status, next_attempt_at, created_at) VALUES (?,?,?,?,?,?,?)",
                     (f"o{i}", "demo_hvac", "x", "{}", status, old, old))
    conn.commit()
    conn.close()
    assert ops.purge_webhook_outbox() == 2             # delivered + cancelled at 45 days; failed is kept 90 days; pending never purged
    assert sorted(r["status"] for r in _rows(env)) == ["failed", "pending"]


# ---------------------------------------------------------------- signatures and replay protection

def test_the_signature_covers_the_timestamp_and_the_body():
    from app import webhooks

    body = b'{"a":1}'
    header = webhooks.sign(SECRET, body, 1_700_000_000)
    assert header.startswith("t=+15555550100,v1=") and webhooks.verify(SECRET, body, header, now=1_700_000_100)
    assert not webhooks.verify(SECRET, body + b" ", header, now=1_700_000_100)           # body changed
    assert not webhooks.verify("another-secret-another-secret", body, header, now=1_700_000_100)
    forged = header.replace("t=+15555550100", "t=+15555550100")
    assert not webhooks.verify(SECRET, body, forged, now=1_700_000_100)                   # timestamp changed


def test_an_old_signature_is_rejected_to_stop_replays():
    from app import webhooks

    body = b"{}"
    header = webhooks.sign(SECRET, body, 1_700_000_000)
    assert webhooks.verify(SECRET, body, header, now=1_700_000_000 + 299)
    assert not webhooks.verify(SECRET, body, header, now=1_700_000_000 + 301)
    assert not webhooks.verify(SECRET, body, header, now=1_700_000_000 - 301)             # from the future


@pytest.mark.parametrize("header", ["", "garbage", "t=abc,v1=00", "v1=00", "t=1", "t=+15555550100"])
def test_malformed_signature_headers_never_verify_or_crash(header):
    from app import webhooks

    assert webhooks.verify(SECRET, b"{}", header, now=1_700_000_000) is False


def test_a_delivery_carries_a_verifiable_signature_and_headers(env, receiver, serve):
    from app import webhooks

    r = receiver()
    cfg = _cfg(r.url)
    serve(cfg)
    webhooks.emit(cfg, "booking.created", {"k": "v"}, event_id="signed")
    webhooks.process_outbox()
    headers, body = r.requests[0]
    assert webhooks.verify(SECRET, body, headers["X-Signature"])
    sent = json.loads(body)
    assert sent["version"] == 1 and sent["id"] == "signed" == headers["X-Delivery"] and headers["X-Event"] == "booking.created"
    assert sent["client_id"] == "demo_hvac" and sent["data"] == {"k": "v"} and sent["created_at"]


# ---------------------------------------------------------------- SSRF and hostile receivers

@pytest.mark.parametrize("url", [
    "https://127.0.0.1/x", "https://localhost/x", "https://169.254.169.254/latest/meta-data", "https://10.0.0.5/x", "https://192.168.1.1/x",
    "https://172.16.0.9/x", "https://100.64.0.1/x",                    # CGNAT
    "https://[::1]/x", "https://[::ffff:127.0.0.1]/x", "https://[fd00::1]/x", "https://[fe80::1]/x",
    "https://0.0.0.0/x", "https://224.0.0.1/x", "https://user:pass@example.com/x",
    "http://example.com/x", "ftp://example.com/x", "https:///nohost", "file:///etc/passwd",
])
def test_non_public_or_non_https_destinations_are_refused(monkeypatch, url):
    from app import webhooks

    monkeypatch.delenv("CALLKETTLE_ALLOW_PRIVATE_WEBHOOKS")
    ok, why = webhooks.url_is_safe(url)
    assert ok is False, (url, why)


def test_a_hostname_that_resolves_to_a_private_address_is_refused(monkeypatch):
    import socket

    from app import webhooks

    monkeypatch.delenv("CALLKETTLE_ALLOW_PRIVATE_WEBHOOKS")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 443))])
    assert webhooks.url_is_safe("https://innocent.example.com/hook")[0] is False
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("127.0.0.1", 443))])
    assert webhooks.url_is_safe("https://mixed.example.com/hook")[0] is False        # ONE bad address is enough to refuse
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    assert webhooks.url_is_safe("https://fine.example.com/hook")[0] is True


def test_redirects_are_never_followed(env, receiver, serve):
    from app import webhooks

    target = receiver()                                       # stands in for an internal service
    r = receiver([("redirect", target.url)])
    cfg = _cfg(r.url)
    serve(cfg)
    webhooks.emit(cfg, "booking.created", {}, event_id="redir")
    webhooks.process_outbox()
    assert target.requests == [] and _rows(env)[0]["status"] == "pending" and "HTTP 302" in _rows(env)[0]["last_error"]


def test_a_huge_response_body_cannot_exhaust_memory_or_block_delivery(env, receiver, serve):
    from app import webhooks

    r = receiver([("big", 5_000_000)])
    cfg = _cfg(r.url)
    serve(cfg)
    webhooks.emit(cfg, "booking.created", {}, event_id="big")
    started = time.time()
    assert webhooks.process_outbox()["delivered"] == 1 and time.time() - started < 7


def test_an_unreachable_receiver_is_a_retry_not_a_crash(env, serve):
    from app import webhooks

    cfg = _cfg("http://127.0.0.1:9/hook")
    serve(cfg)
    webhooks.emit(cfg, "booking.created", {}, event_id="down")
    assert webhooks.process_outbox()["retrying"] == 1 and "Error" in _rows(env)[0]["last_error"]


def test_config_requires_https_and_a_secret():
    from pydantic import ValidationError

    from app.config import load_client_config

    base = load_client_config("demo_hvac")
    with pytest.raises(ValidationError):
        type(base).model_validate({**base.model_dump(), "webhook_url": "http://example.com/x", "webhook_secret": SECRET})
    with pytest.raises(ValidationError):
        type(base).model_validate({**base.model_dump(), "webhook_url": "https://example.com/x", "webhook_secret": "short"})
    assert type(base).model_validate({**base.model_dump(), "webhook_url": "https://example.com/x", "webhook_secret": SECRET}).webhook_url


# ---------------------------------------------------------------- the events themselves

def test_a_booking_queues_one_event_and_a_cancel_queues_another(env, receiver, monkeypatch):
    from app import storage, tools, webhooks

    r = receiver()
    cfg = _cfg(r.url)
    res = tools.book_appointment(call_sid="CA_W1", config=cfg, caller_name="Pat Lee", caller_phone="+15555550100",
                                 service="Emergency repair", date="2026-01-12", time="10:00")
    assert res["success"]
    tools.cancel_appointment(config=cfg, call_sid="CA_W2", booking_id=res["booking_id"], caller_id="+15555550100")
    events = [(x["event"], x["event_id"]) for x in _rows(env)]
    assert events == [("booking.created", f"booking.created:demo_hvac:{res['booking_id']}"),
                      ("booking.cancelled", f"booking.cancelled:demo_hvac:{res['booking_id']}")]


def test_call_completed_is_queued_once_per_call(env, receiver, monkeypatch):
    from app import storage, summary

    r = receiver()
    cfg = _cfg(r.url, "callkettle_sales")
    monkeypatch.setattr(summary, "load_client_config", lambda cid: cfg)
    monkeypatch.setattr(summary.agent, "_anthropic_client", lambda: (_ for _ in ()).throw(RuntimeError("no model in this test")))
    storage.log_call_start("CA_DONE", "callkettle_sales", "+15555550100")
    storage.log_call_end("CA_DONE", "completed")
    for _ in range(3):                                   # the several end-of-call code paths
        summary.summarize_call("CA_DONE")
    done = [x for x in _rows(env) if x["event"] == "call.completed"]
    assert len(done) == 1 and json.loads(done[0]["payload_json"])["data"]["outcome"] == "completed"


def test_a_failing_queue_cannot_break_a_booking(env, monkeypatch):
    from app import storage, tools

    def boom(*a, **k):
        raise RuntimeError("disk full")

    cfg = _cfg("https://example.com/hook")
    monkeypatch.setattr(storage, "_conn", boom)
    # emit() swallows the error; the booking itself needs the DB, so exercise emit directly:
    from app import webhooks

    assert webhooks.emit(cfg, "booking.created", {}, event_id="x") is False
