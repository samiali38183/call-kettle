"""Typed per-client policy, the call outcome taxonomy, the owner follow-up queue, and per-call version fingerprints."""
import pytest

from app import outcomes
from tests.test_agent import FakeAnthropicClient, FakeResponse, FakeTextBlock, temp_db  # noqa: F401


def _cfg(**policy):
    from app.config import load_client_config

    base = load_client_config("demo_hvac")
    return base.model_copy(update={"policy": base.policy.model_copy(update=policy)})


# ---------------------------------------------------------------- the outcome taxonomy
def _k(outcome, turns, booked=0, rescheduled=0, cancelled=0, reasons=()):
    return dict(outcome=outcome, caller_turns=turns, booked=booked, rescheduled=rescheduled, cancelled=cancelled, reasons=list(reasons))


@pytest.mark.parametrize(
    "kw,expected,attention",
    [
        (_k("completed", 3, booked=1), outcomes.BOOKED, False),
        (_k("completed", 3, rescheduled=1), outcomes.RESCHEDULED, False),
        (_k("completed", 3, cancelled=1), outcomes.CANCELLED, False),
        (_k("completed", 2), outcomes.FAQ_RESOLVED, False),
        (_k("transferred", 2), outcomes.TRANSFERRED, False),
        (_k("transfer_unanswered", 2), outcomes.TRANSFER_FAILED, True),
        (_k("transfer_unanswered", 3, reasons=["callback_requested"]), outcomes.CALLBACK_REQUESTED, True),
        (_k("completed", 2, reasons=["possible_emergency"]), outcomes.EMERGENCY_ESCALATED, True),
        (_k("completed", 2, reasons=["agent_error"]), outcomes.AI_FAILURE, True),
        (_k("completed", 2, reasons=["max_turns_reached"]), outcomes.AI_FAILURE, True),
        (_k("completed", 2, reasons=["after_hours_message"]), outcomes.AFTER_HOURS_MESSAGE, True),
        (_k("completed", 2, reasons=["outside_service_area"]), outcomes.OUTSIDE_SERVICE_AREA, False),
        (_k("completed", 2, reasons=["service_not_offered"]), outcomes.SERVICE_NOT_OFFERED, False),
        (_k("completed", 2, reasons=["callback_requested"]), outcomes.CALLBACK_REQUESTED, True),
        (_k("completed", 2, reasons=["demo_lead"]), outcomes.LEAD_CAPTURED, True),
        (_k("over_ceiling", 1, reasons=["over_limit_message"]), outcomes.CALLBACK_REQUESTED, True),
        (_k("no_input", 0), outcomes.ABANDONED, False),
        (_k("caller_hung_up", 1), outcomes.ABANDONED, False),
        (_k("caller_hung_up", 4), outcomes.UNKNOWN, True),
        (_k(None, 2), outcomes.UNKNOWN, False),
    ],
)
def test_classify(kw, expected, attention):
    assert outcomes.classify(**kw) == (expected, attention)


def test_every_class_has_a_label_and_the_taxonomy_is_complete():
    assert set(outcomes.LABELS) == set(outcomes.OUTCOMES)
    for must in ("FAQ_RESOLVED", "LEAD_CAPTURED", "BOOKED", "RESCHEDULED", "CANCELLED", "CALLBACK_REQUESTED", "TRANSFERRED", "TRANSFER_FAILED",
                 "OUTSIDE_SERVICE_AREA", "AFTER_HOURS_MESSAGE", "ABANDONED", "SPAM", "AI_FAILURE", "UNKNOWN"):
        assert must in outcomes.OUTCOMES


def test_classification_comes_from_what_the_system_recorded(temp_db):
    from app import storage

    storage.log_call_start("CA_C1", "demo_hvac", "+15555550100")
    storage.log_turn("CA_C1", "caller", "hi")
    storage.log_call_end("CA_C1", "completed")
    assert storage.classify_and_store("CA_C1") == ("FAQ_RESOLVED", False)      # the assistant ended it and nothing needs a human

    storage.log_call_start("CA_C2", "demo_hvac", "+15555550100")
    for t in ("a", "b"):
        storage.log_turn("CA_C2", "caller", t)
    storage.log_escalation(call_sid="CA_C2", client_id="demo_hvac", reason="callback_requested", caller_phone="+15555550100", summary="x")
    storage.log_call_end("CA_C2", "completed")
    assert storage.classify_and_store("CA_C2") == ("CALLBACK_REQUESTED", True)
    assert storage.get_call_class("CA_C2") == "CALLBACK_REQUESTED"
    assert storage.classify_and_store("CA_NOPE") is None


# ---------------------------------------------------------------- versions per call
def test_each_call_records_the_config_prompt_tool_and_model_it_ran_with(temp_db):
    import sqlite3

    from app import agent, storage

    cfg = _cfg()
    storage.log_call_start("CA_V1", cfg.client_id, "+15555550100")
    agent.start_session("CA_V1", cfg, caller_number="+15555550100")
    conn = sqlite3.connect(storage.DB_PATH)
    row = conn.execute("SELECT config_version, prompt_version, tool_version, model_id FROM calls WHERE call_sid='CA_V1'").fetchone()
    conn.close()
    assert all(row) and len(row[0]) == 12 and row[3] == cfg.model
    assert row[1] == agent.prompt_version() and row[2] == agent.tool_version()
    assert agent.config_version(cfg) == agent.config_version(_cfg())
    assert agent.config_version(cfg) != agent.config_version(_cfg(can_quote=True))


# ---------------------------------------------------------------- policy validation
def test_policy_is_strictly_typed():
    from app.config import Policy

    with pytest.raises(Exception):
        Policy(can_bok=False)                                    # a typo is an error, not a silent default
    with pytest.raises(Exception):
        Policy(can_state_dispatch_fee=True)                      # a fee must come with its exact words
    assert Policy(can_state_dispatch_fee=True, dispatch_fee_text="The trip fee is $89.").can_state_dispatch_fee


def test_default_policy_keeps_todays_behavior():
    from app.config import Policy

    p = Policy()
    assert p.can_book and p.can_reschedule and p.can_cancel and p.can_transfer
    assert not p.can_quote and not p.can_state_dispatch_fee and not p.can_collect_address
    assert p.after_hours_action == "book" and p.emergency_action == "transfer"


# ---------------------------------------------------------------- the prompt carries the policy
def test_every_prompt_forbids_repair_and_diy_instructions():
    from app import agent

    prompt = agent.build_system_prompt(_cfg())
    assert "NEVER give repair, troubleshooting, do-it-yourself or safety instructions" in prompt
    assert "Never quote, estimate or guess any price" in prompt


def test_quote_and_fee_rules_follow_the_policy():
    from app import agent

    assert "ONLY if it is written word for word in the FAQs" in agent.build_system_prompt(_cfg(can_quote=True))
    prompt = agent.build_system_prompt(_cfg(can_state_dispatch_fee=True, dispatch_fee_text="The trip fee is $89, credited to the repair."))
    assert "The trip fee is $89, credited to the repair." in prompt and "nothing more about money" in prompt


def test_disabled_abilities_are_stated_in_the_prompt():
    from app import agent

    prompt = agent.build_system_prompt(_cfg(can_book=False, can_transfer=False, can_cancel=False))
    assert "You cannot book appointments" in prompt and "cannot connect the caller to a person" in prompt and "cannot change or cancel" in prompt


# ---------------------------------------------------------------- enforcement in code
def test_a_disabled_tool_is_not_offered_and_is_refused_if_called(temp_db, monkeypatch):
    from app import agent

    cfg = _cfg(can_book=False, can_reschedule=False, can_transfer=False)
    names = {t["name"] for t in agent.tools_for(cfg)}
    assert not names & {"book_appointment", "reschedule_appointment", "transfer_call", "check_availability"}
    assert {"escalate_to_human", "end_call", "cancel_appointment", "find_my_appointments"} <= names
    session = agent.start_session("CA_P1", cfg, caller_number="+15555550100")
    out = agent._dispatch_tool(session, "book_appointment", {"caller_name": "A", "caller_phone": "+15555550100", "service": "x", "date": "2026-01-12", "time": "10:00"})
    assert "not available" in out["error"]

    fake = FakeAnthropicClient([FakeResponse(content=[FakeTextBlock(text="Okay.")], stop_reason="end_turn")])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)
    agent.run_turn(session, "how are you")
    assert "book_appointment" not in {t["name"] for t in fake.messages.calls[0]["tools"]}


def test_default_client_is_offered_every_tool():
    from app import agent

    assert {t["name"] for t in agent.tools_for(_cfg())} == {t["name"] for t in agent.TOOLS}


def test_without_live_transfer_a_request_for_a_person_takes_a_message(temp_db, monkeypatch):
    from app import agent

    fake = FakeAnthropicClient([FakeResponse(content=[FakeTextBlock(text="I cannot connect you, but I will take your number.")], stop_reason="end_turn")])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)
    session = agent.start_session("CA_P2", _cfg(can_transfer=False))
    reply, end, transfer = agent.run_turn(session, "let me talk to a real person")
    assert transfer is None and not end and "number" in reply
    assert "does not connect calls live" in fake.messages.calls[0]["system"]

    live = agent.start_session("CA_P3", _cfg())
    _, end, transfer = agent.run_turn(live, "let me talk to a real person")
    assert end and transfer == _cfg().escalation_phone


def test_emergency_action_message_gives_911_advice_and_does_not_dial(temp_db):
    from app import agent

    on = agent.run_turn(agent.start_session("CA_E1", _cfg()), "I smell gas in the house")
    off = agent.run_turn(agent.start_session("CA_E2", _cfg(emergency_action="message")), "I smell gas in the house")
    assert "911" in on[0] and on[2] == _cfg().escalation_phone
    assert "911" in off[0] and off[1] is True and off[2] is None


def test_after_hours_message_policy_blocks_booking_only_while_closed(temp_db, monkeypatch):
    from app import agent

    cfg = _cfg(after_hours_action="message")
    session = agent.start_session("CA_A1", cfg, caller_number="+15555550100")
    args = {"caller_name": "A", "caller_phone": "+15555550100", "service": cfg.services[0].name, "date": "2026-01-12", "time": "10:00"}
    monkeypatch.setattr(agent, "is_open_now", lambda config, now=None: False)
    assert "after hours" in agent._dispatch_tool(session, "book_appointment", args)["error"]
    assert "does not book appointments by phone after hours" in agent.build_system_prompt(cfg)
    monkeypatch.setattr(agent, "is_open_now", lambda config, now=None: True)
    assert "does not book appointments by phone after hours" not in agent.build_system_prompt(cfg)
    assert "error" not in agent._dispatch_tool(session, "book_appointment", args)


# ---------------------------------------------------------------- the owner's follow-up queue
def _seed_attention(storage, sid="CA_Q1", client="demo_hvac"):
    storage.log_call_start(sid, client, "+15555550100")
    storage.log_turn(sid, "caller", "call me back")
    storage.log_turn(sid, "caller", "about my furnace")
    storage.log_escalation(call_sid=sid, client_id=client, reason="callback_requested", caller_phone="+15555550100", summary="Wants a furnace callback")
    storage.log_call_end(sid, "completed")
    storage.classify_and_store(sid)


def test_dashboard_lists_what_needs_attention_and_the_owner_can_clear_it(app_client):
    client, main = app_client
    from app import storage

    _seed_attention(storage)
    key = main.report_key_for("demo_hvac")
    page = client.get(f"/report/demo_hvac?key={key}").text
    assert "Needs your attention (1)" in page and "Callback requested" in page and "Mark handled" in page
    r = client.post("/report/demo_hvac/handled", data={"key": key, "call": "CA_Q1"}, follow_redirects=False)
    assert r.status_code == 303
    after = client.get(f"/report/demo_hvac?key={key}").text
    assert "Needs your attention (0)" in after and "Nothing waiting on you." in after


def test_one_clients_key_cannot_clear_another_clients_follow_up(app_client):
    client, main = app_client
    from app import storage

    _seed_attention(storage, "CA_Q2", "demo_hvac")
    other = main.report_key_for("demo_dental")
    assert client.post("/report/demo_hvac/handled", data={"key": other, "call": "CA_Q2"}).status_code == 403
    assert client.post("/report/demo_dental/handled", data={"key": other, "call": "CA_Q2"}, follow_redirects=False).status_code == 303
    assert "Needs your attention (1)" in client.get(f"/report/demo_hvac?key={main.report_key_for('demo_hvac')}").text   # unchanged
    assert client.post("/report/demo_hvac/handled", data={"key": "wrong", "call": "CA_Q2"}).status_code == 403


def test_the_call_completed_event_carries_the_outcome_class(app_client, monkeypatch):
    client, main = app_client
    from app import storage, summary, webhooks

    _seed_attention(storage, "CA_Q3")
    seen = {}
    monkeypatch.setattr(webhooks, "emit", lambda config, event, payload, **kw: seen.update(payload=payload))
    monkeypatch.setattr(summary.agent, "_anthropic_client", lambda: FakeAnthropicClient([]))
    summary.summarize_call("CA_Q3")
    assert seen["payload"]["outcome_class"] == "CALLBACK_REQUESTED"


# ---------------------------------------------------------------- production database inspection
def test_the_admin_status_reports_how_sqlite_is_really_configured(app_client):
    client, main = app_client
    body = client.get("/admin/status", params={"key": "master_key_for_tests", "deep": 1}).json()
    sq = body["database"]["sqlite"]
    assert sq["journal_mode"] == "wal" and sq["foreign_keys"] == 1 and sq["busy_timeout"] >= 10000
    assert sq["quick_check"] == "ok" and sq["sqlite_version"] and "db" in sq["file_bytes"]


# ---------------------------------------------------------------- demo-line alerts (a prospect trying the product)
def _demo_call(storage, sid, frm, turns=2, client="demo_nova_hvac"):
    storage.log_call_start(sid, client, frm)
    for i in range(turns):
        storage.log_turn(sid, "caller", f"hello {i}")
    storage.log_call_end(sid, "completed")
    storage.classify_and_store(sid)


def test_a_prospect_trying_a_demo_alerts_the_operator(app_client, monkeypatch):
    client, main = app_client
    from app import notify, storage, summary

    sent = []
    monkeypatch.setattr(notify, "notify_owner", lambda config, title, body: sent.append((title, body)))
    monkeypatch.setattr(summary.agent, "_anthropic_client", lambda: FakeAnthropicClient([FakeResponse(content=[FakeTextBlock(text="Recap.")], stop_reason="end_turn")]))
    _demo_call(storage, "CA_D1", "+15555550100")
    summary.summarize_call("CA_D1")
    assert len(sent) == 1 and "+15555550100" in sent[0][1] and "Sample Heating & Air" in sent[0][1]


def test_no_alert_for_the_owners_own_test_call_silence_the_menu_or_a_real_customer(app_client, monkeypatch):
    client, main = app_client
    from app import notify, storage, summary

    sent = []
    monkeypatch.setattr(notify, "notify_owner", lambda config, title, body: sent.append(title))
    monkeypatch.setattr(summary.agent, "_anthropic_client", lambda: FakeAnthropicClient([FakeResponse(content=[FakeTextBlock(text="Recap.")], stop_reason="end_turn")] * 5))
    _demo_call(storage, "CA_D2", "+15555550100")                       # the owner's own phone
    _demo_call(storage, "CA_D3", "+15555550100", turns=0)              # hung up in silence
    _demo_call(storage, "CA_D4", "+15555550100", client="callkettle_demo")  # the menu root itself
    _demo_call(storage, "CA_D5", "+15555550100", client="demo_hvac")    # (not a demo_mode client)
    for sid in ("CA_D2", "CA_D3", "CA_D4", "CA_D5"):
        summary.summarize_call(sid)
    assert sent == []


def test_demo_alerts_are_capped_per_day(app_client, monkeypatch):
    client, main = app_client
    from app import notify, storage, summary

    sent = []
    monkeypatch.setattr(notify, "notify_owner", lambda config, title, body: sent.append(title))
    monkeypatch.setattr(summary.agent, "_anthropic_client", lambda: FakeAnthropicClient([FakeResponse(content=[FakeTextBlock(text="Recap.")], stop_reason="end_turn")] * 40))
    for i in range(summary.DEMO_ALERTS_PER_DAY + 5):
        _demo_call(storage, f"CA_DX{i}", f"+1703521{1000 + i}")
        summary.summarize_call(f"CA_DX{i}")
    assert len(sent) == summary.DEMO_ALERTS_PER_DAY


def test_our_own_synthetic_callers_never_trigger_a_prospect_alert(app_client, monkeypatch):
    client, main = app_client
    from app import notify, storage, summary

    sent = []
    monkeypatch.setattr(notify, "notify_owner", lambda config, title, body: sent.append(title))
    monkeypatch.setattr(summary.agent, "_anthropic_client", lambda: FakeAnthropicClient([FakeResponse(content=[FakeTextBlock(text="Recap.")], stop_reason="end_turn")] * 5))
    _demo_call(storage, "CA_S1", "+15557770004")                         # selfcheck's numbers
    _demo_call(storage, "CA_S2", "+15555550100")                         # live-check numbers (555 exchange)
    _demo_call(storage, "CA_S3", "+15555550100", client="demo_nova_hvac")
    storage.log_call_end("CA_S3", "completed")
    for sid in ("CA_S1", "CA_S2"):
        summary.summarize_call(sid)
    assert sent == []


# ---------------------------------------------------------------- the multi-week view
def test_weekly_summary_counts_recorded_outcomes_only_and_shows_quiet_weeks():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    now = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)                        # a Wednesday
    rows = [
        ("2026-10-13T14:00:00+00:00", "BOOKED", 0),                                 # Tuesday 10am local, this week
        ("2026-10-12T02:00:00+00:00", "CALLBACK_REQUESTED", 1),                     # Sunday night local: previous week, after hours
        ("2026-10-12T23:30:00+00:00", "FAQ_RESOLVED", 0),                           # Monday 7:30pm local: this week, after hours
        ("2026-09-23T15:00:00+00:00", None, 0),                                     # an old unclassified call (week of 2026-09-21)
        ("not a date", "BOOKED", 0),
    ]
    weeks = outcomes.weekly_summary(rows, tz, weeks=4, now=now)
    this, last = weeks[0], weeks[1]
    assert this["week_start"] == "2026-10-12" and this["calls"] == 2 and this["booked"] == 1 and this["answered_questions"] == 1 and this["after_hours"] == 1
    assert last["calls"] == 1 and last["callbacks_and_leads"] == 1 and last["needs_attention"] == 1 and last["after_hours"] == 1
    assert [w["calls"] for w in weeks[2:]] == [0, 1] and weeks[3]["unclassified"] == 1      # a quiet week is still listed
    assert all("revenue" not in k for w in weeks for k in w)


def test_the_dashboard_shows_week_by_week_without_any_revenue(app_client):
    client, main = app_client
    from app import storage

    _seed_attention(storage, "CA_W1")
    page = client.get(f"/report/demo_hvac?key={main.report_key_for('demo_hvac')}").text
    assert "Week by week" in page and "nothing here estimates income" in page and "$" not in page.split("Week by week")[1].split("Upcoming bookings")[0]


# ---------------------------------------------------------------- audit fixes: unsettled calls and readable phone numbers
def test_a_booked_call_that_ended_while_the_assistant_was_asking_a_question_needs_a_look(temp_db):
    from app import storage

    storage.log_call_start("CA_MQ", "demo_hvac", "+15555550100")
    storage.log_turn("CA_MQ", "caller", "book me tomorrow")
    storage.log_turn("CA_MQ", "caller", "actually move it")
    storage.log_turn("CA_MQ", "ai", "Do you want to keep tomorrow at eight or move it to Thursday?")
    storage.log_call_end("CA_MQ", "caller_hung_up")
    import sqlite3

    c = sqlite3.connect(storage.DB_PATH)
    c.execute("INSERT INTO bookings (call_sid, client_id, caller_name, caller_phone, service, slot_start, slot_end, created_at) VALUES ('CA_MQ','demo_hvac','A','1','s','2026-10-09T10:00','2026-10-09T11:00','now')")
    c.commit()
    c.close()
    assert storage.classify_and_store("CA_MQ") == ("BOOKED", True)
    assert outcomes.classify(outcome="caller_hung_up", caller_turns=3, booked=1, rescheduled=0, cancelled=0, reasons=[], ended_mid_question=False) == ("BOOKED", False)


def test_dashboard_shows_phone_numbers_the_way_people_read_them(app_client):
    client, main = app_client
    assert main._fmt_phone("+15555550100") == "+15555550100" and main._fmt_phone("+15555550100") == "+15555550100" and main._fmt_phone("anonymous") == "anonymous" and main._fmt_phone(None) == ""
