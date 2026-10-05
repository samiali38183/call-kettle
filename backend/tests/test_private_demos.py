"""Private prospect demos: reached with a one-time code on the public demo menu, isolated, expiring, rate limited, audited, and never able to touch a customer."""
import sqlite3
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

import pytest

KEY = "master_key_for_tests"
CALLER = "+15555550100"


@pytest.fixture
def live(app_client, tmp_path, monkeypatch):
    client, main = app_client
    from app import config as config_module

    monkeypatch.setattr(config_module, "LIVE_CLIENTS_DIR", tmp_path / "live")
    config_module.load_client_config.cache_clear()
    yield client, main, config_module
    config_module.load_client_config.cache_clear()


def _prep_yaml(client_id="prep_example_hvac", **extra):
    import yaml

    from app.config import load_client_config

    cfg = load_client_config("demo_nova_hvac").model_dump(mode="json", exclude_none=True)
    cfg.update(client_id=client_id, business_name="Example Heating & Cooling",
               opening_line="Hi, this is a demonstration of an AI receptionist for Example Heating & Cooling. It is a demo. How can I help?")
    cfg.update(extra)
    return yaml.safe_dump(cfg, sort_keys=False)


def _create(client, text=None, key=KEY, **params):
    qs = "&".join(f"{k}={v}" for k, v in {"key": key, **params}.items())
    return client.post(f"/admin/private-demo?{qs}", content=(text or _prep_yaml()).encode())


def _enter(client, code, sid="CA_PD1", frm=CALLER):
    return client.post("/voice/demo-code?client_id=callkettle_demo", data={"CallSid": sid, "From": frm, "Digits": code})


def test_the_demo_menu_offers_a_private_code_and_pressing_9_asks_for_digits_only(app_client):
    client, main = app_client
    menu = client.post("/voice/incoming?client_id=callkettle_demo", data={"CallSid": "CA_PD0", "From": CALLER})
    assert "press 9" in menu.text
    r = client.post("/voice/demo-select?client_id=callkettle_demo", data={"CallSid": "CA_PD0", "Digits": "9"})
    gather = ElementTree.fromstring(r.text).find(".//Gather")
    assert gather.get("input") == "dtmf" and gather.get("numDigits") == "6" and "/voice/demo-code" in gather.get("action")


def test_creating_a_private_demo_needs_the_master_key_and_a_prep_id(live):
    client, main, _ = live
    assert _create(client, key="").status_code == 403
    assert _create(client, key="wrong").status_code == 403
    assert _create(client, _prep_yaml("acme_plumbing")).status_code == 422          # not a prep_ id: cannot overwrite a real client
    assert _create(client, _prep_yaml("demo_hvac")).status_code == 422


def test_the_server_forces_isolation_whatever_the_submitted_config_says(live):
    client, main, config_module = live
    hostile = _prep_yaml(owner_email="owner@example.com", ntfy_topic="real-topic", webhook_url="https://example.test/hook", webhook_secret="x" * 20,
                         calendar_ical_url="https://example.test/cal.ics", demo_mode=False, monthly_cost_ceiling_usd=5000, ceiling_mode="transfer")
    r = _create(client, hostile)
    assert r.status_code == 200, r.text
    cfg = config_module.load_client_config("prep_example_hvac")
    assert cfg.demo_mode and cfg.owner_email is None and cfg.ntfy_topic is None and cfg.webhook_url is None and cfg.calendar_ical_url is None
    assert cfg.monthly_cost_ceiling_usd <= 10 and cfg.ceiling_mode == "message" and not cfg.demo_menu


def test_a_valid_code_moves_the_call_to_that_prospects_demo_and_counts_the_use(live):
    client, main, _ = live
    from app import storage

    made = _create(client, hours=48, max_calls=3).json()
    assert len(made["code"]) == 6 and made["client_id"] == "prep_example_hvac" and made["max_calls"] == 3
    storage.log_call_start("CA_PD1", "callkettle_demo", CALLER)
    r = _enter(client, made["code"])
    assert "client_id=prep_example_hvac" in r.text and "<Redirect" in r.text
    assert storage.get_call("CA_PD1")["client_id"] == "prep_example_hvac"
    follow = client.post("/voice/incoming?client_id=prep_example_hvac", data={"CallSid": "CA_PD1", "From": CALLER})
    assert "<Gather" in follow.text and "demonstration" in follow.text
    assert [d["calls_used"] for d in storage.list_private_demos()] == [1]


def test_wrong_expired_revoked_and_used_up_codes_are_refused(live):
    client, main, config_module = live
    from app import storage

    made = _create(client, max_calls=1).json()
    assert "not recognized" in _enter(client, "000000").text and "<Hangup" in _enter(client, "000000").text
    assert "not recognized" in _enter(client, "12").text                              # too short
    assert "client_id=prep" in _enter(client, made["code"], sid="CA_U1").text
    assert "maximum number of times" in _enter(client, made["code"], sid="CA_U2").text
    conn = sqlite3.connect(storage.DB_PATH)
    conn.execute("UPDATE private_demos SET expires_at = ?, calls_used = 0", ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),))
    conn.commit()
    conn.close()
    assert "expired" in _enter(client, made["code"], sid="CA_U3").text
    revoked = client.post(f"/admin/private-demo/revoke?key={KEY}&client_id=prep_example_hvac").json()
    assert revoked["revoked"] == 1 and revoked["cleaned"] == ["prep_example_hvac"]
    assert "prep_example_hvac" not in config_module.list_client_ids()               # unserved at once, not at the next maintenance run
    assert "no longer available" in _enter(client, made["code"], sid="CA_U4").text


def test_guessing_is_locked_out_per_caller_and_overall(live):
    client, main, _ = live
    from app import storage

    made = _create(client).json()
    for _ in range(storage.MAX_BAD_CODES_PER_CALLER_HOUR):
        _enter(client, "999999", frm="+15555550100")
    locked = _enter(client, made["code"], frm="+15555550100")                         # even the RIGHT code is refused for a guesser
    assert "Too many attempts" in locked.text
    assert "client_id=prep" in _enter(client, made["code"], frm="+15555550100", sid="CA_G2").text   # another caller is unaffected
    conn = sqlite3.connect(storage.DB_PATH)
    for i in range(storage.MAX_BAD_CODES_GLOBAL_HOUR):
        conn.execute("INSERT INTO private_demo_audit (at, event, caller_hash) VALUES (?, 'code_bad', ?)", (datetime.now(timezone.utc).isoformat(), f"h{i}"))
    conn.commit()
    conn.close()
    assert "Too many attempts" in _enter(client, made["code"], frm="+15555550100", sid="CA_G3").text


def test_the_code_and_the_callers_number_are_never_stored_in_the_clear(live):
    client, main, _ = live
    from app import storage

    made = _create(client).json()
    _enter(client, made["code"], sid="CA_H1", frm="+15555550100")
    _enter(client, "111111", sid="CA_H2", frm="+15555550100")
    conn = sqlite3.connect(storage.DB_PATH)
    dump = " ".join(str(v) for t in ("private_demos", "private_demo_audit") for row in conn.execute(f"SELECT * FROM {t}") for v in row)
    conn.close()
    assert made["code"] not in dump and "+15555550100" not in dump
    assert storage._caller_hash("+15555550100") in dump
    listing = client.get(f"/admin/private-demos?key={KEY}")
    assert listing.status_code == 200 and made["code"] not in listing.text
    assert client.get("/admin/private-demos?key=wrong").status_code == 403


def test_a_private_demo_cannot_point_at_a_real_customers_line(live, monkeypatch):
    client, main, config_module = live
    from app import storage

    made = _create(client).json()
    real = config_module.load_client_config("demo_hvac")                                # demo_hvac is not demo_mode here: stands in for a customer
    assert not real.demo_mode
    conn = sqlite3.connect(storage.DB_PATH)
    conn.execute("UPDATE private_demos SET client_id = 'demo_hvac'")
    conn.commit()
    conn.close()
    r = _enter(client, made["code"])
    assert "not available" in r.text and "demo_hvac" not in r.text.replace("not available", "")


def test_expired_demos_are_unserved_and_their_words_and_bookings_are_wiped(live):
    client, main, config_module = live
    from app import ops, storage

    made = _create(client).json()
    storage.log_call_start("CA_C1", "prep_example_hvac", CALLER)
    storage.log_turn("CA_C1", "caller", "my furnace is out")
    storage.set_call_summary("CA_C1", "Caller needs a furnace visit")
    storage.log_escalation(call_sid="CA_C1", client_id="prep_example_hvac", reason="callback_requested", caller_phone=CALLER, summary="x")
    assert ops.clean_private_demos() == {"cleaned": []}                                 # still valid: nothing happens
    conn = sqlite3.connect(storage.DB_PATH)
    conn.execute("UPDATE private_demos SET expires_at = ?", ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),))
    conn.commit()
    conn.close()
    assert ops.clean_private_demos() == {"cleaned": ["prep_example_hvac"]}
    assert "prep_example_hvac" not in config_module.list_client_ids()
    call = storage.get_call("CA_C1")
    assert call["transcript_json"] == "[]" and call["summary"] is None
    conn = sqlite3.connect(storage.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM escalations WHERE client_id = 'prep_example_hvac'").fetchone()[0] == 0
    events = [r[0] for r in conn.execute("SELECT event FROM private_demo_audit ORDER BY id")]
    conn.close()
    assert events == ["created", "cleaned"]
    assert ops.clean_private_demos() == {"cleaned": []}                                 # once only


def test_a_prospects_demo_call_alerts_the_operator_not_the_prospect(live, monkeypatch):
    client, main, config_module = live
    from app import notify, storage, summary
    from tests.test_agent import FakeAnthropicClient, FakeResponse, FakeTextBlock

    _create(client)
    sent = []
    monkeypatch.setattr(notify, "notify_owner", lambda config, title, body: sent.append((config.client_id, body)))
    monkeypatch.setattr(summary.agent, "_anthropic_client", lambda: FakeAnthropicClient([FakeResponse(content=[FakeTextBlock(text="Recap.")], stop_reason="end_turn")]))
    storage.log_call_start("CA_A1", "prep_example_hvac", "+15555550100")
    for t in ("hi", "my furnace is out"):
        storage.log_turn("CA_A1", "caller", t)
    storage.log_call_end("CA_A1", "completed")
    storage.classify_and_store("CA_A1")
    summary.summarize_call("CA_A1")
    assert len(sent) == 1 and sent[0][0] == "callkettle_sales" and "Example Heating & Cooling" in sent[0][1]
