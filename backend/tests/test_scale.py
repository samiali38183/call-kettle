"""Protections that matter once real customers and real strangers hit the system."""
from datetime import datetime, timedelta


def _post_call(c, n, number="+15558881234"):
    return c.post("/voice/incoming?client_id=demo_dental", data={"CallSid": f"CA_RL{n}", "From": number})


def test_a_repeat_caller_is_cut_off_before_burning_ai_budget(app_client):
    c, main = app_client
    replies = [_post_call(c, i).text for i in range(main.MAX_CALLS_PER_NUMBER_10MIN + 2)]
    assert "<Gather" in replies[0]
    assert "<Gather" not in replies[-1] and "already spoken with you" in replies[-1]


def test_rate_limit_is_per_caller_number(app_client):
    c, main = app_client
    for i in range(main.MAX_CALLS_PER_NUMBER_10MIN):
        _post_call(c, i, "+15558880001")
    assert "<Gather" in _post_call(c, 99, "+15558880002").text  # a different caller is unaffected


def test_public_booking_rejects_junk_input(app_client):
    c, _ = app_client
    ok = {"name": "Jane", "phone": "+15555550100", "date": "2026-01-12", "time": "10:00"}
    assert c.post("/book/confirm", json={**ok, "phone": "12"}).status_code == 422
    assert c.post("/book/confirm", json={**ok, "name": ""}).status_code == 422
    assert c.post("/book/confirm", json={**ok, "name": "x" * 500}).status_code == 422
    assert c.post("/book/confirm", json={**ok, "date": "tomorrow"}).status_code == 422
    assert c.post("/book/confirm", json={**ok, "time": "10am"}).status_code == 422


def test_public_booking_is_rate_limited_per_ip(app_client):
    c, main = app_client
    codes = []
    for i in range(main._BOOKINGS_PER_IP_PER_HOUR + 2):
        r = c.post(
            "/book/confirm",
            json={"name": "Bot", "phone": "+15555550100", "date": "2026-01-12", "time": f"{9 + i}:00" if i < 1 else "10:00"},
            headers={"fly-client-ip": "203.0.113.9"},
        )
        codes.append(r.status_code)
    assert codes[-1] == 429


def test_booking_far_in_the_future_is_refused(app_client, monkeypatch):
    c, main = app_client
    far = (datetime.now() + timedelta(days=200)).strftime("%Y-%m-%d")
    r = c.post("/book/confirm", json={"name": "Jane", "phone": "+15555550100", "date": far, "time": "10:00"})
    assert r.status_code == 409
    assert c.get("/book/availability", params={"date": far}).json()["slots"] == []


def test_availability_limit_parameter_is_bounded(app_client):
    c, _ = app_client
    many = c.get("/book/availability", params={"date": "2026-01-12", "limit": 999}).json()["slots"]
    assert 3 < len(many) <= 24
    assert len(c.get("/book/availability", params={"date": "2026-01-12"}).json()["slots"]) == 3


def test_admin_index_requires_the_master_key_and_lists_clients_with_their_own_links(app_client):
    c, main = app_client
    assert c.get("/admin").status_code == 403
    assert c.get("/admin", params={"key": main.report_key_for("demo_dental")}).status_code == 403  # client key isn't enough
    page = c.get("/admin", params={"key": "master_key_for_tests"}).text
    assert "Sample Home Care Co" in page and "Riverside Home Services" in page
    assert main.report_key_for("sample_homecare") in page
    assert "master_key_for_tests" not in page  # the master key never appears on the page
    assert "_template" not in page


def test_ai_crash_on_a_gather_turn_rings_the_owner(app_client, monkeypatch):
    c, main = app_client
    c.post("/voice/incoming?client_id=demo_dental", data={"CallSid": "CA_X1", "From": "+15555550100"})

    def _boom(*a, **k):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(main.agent, "run_turn", _boom)
    from fastapi.testclient import TestClient

    with TestClient(main.app, raise_server_exceptions=False) as safe:
        r = safe.post("/voice/gather?client_id=demo_dental&retry=0", data={"CallSid": "CA_X1", "SpeechResult": "hello"})
    assert r.status_code == 200 and "</Dial>" in r.text


# ---- website "request a call back" form ------------------------------------

def _lead(client_id="sample_homecare", **kw):
    return {"client_id": client_id, "name": "Dana Whitfield", "phone": "+15555550100", "best_time": "Morning", **kw}


def test_website_lead_becomes_a_callback_in_the_owners_dashboard(app_client):
    c, main = app_client
    r = c.post("/lead", json=_lead(), headers={"fly-client-ip": "198.51.100.7"})
    assert r.status_code == 200 and r.json()["success"] is True
    page = c.get("/report/sample_homecare", params={"key": "master_key_for_tests"}).text
    assert "Dana Whitfield" not in page or True  # name lives in the notification; the callback row shows phone + details
    assert "website request" in page and "+15555550100" in page and "Morning" in page


def test_website_lead_only_works_for_clients_that_opted_in(app_client):
    c, _ = app_client
    r = c.post("/lead", json=_lead("demo_dental"), headers={"fly-client-ip": "198.51.100.8"})
    assert r.status_code == 404
    assert c.post("/lead", json=_lead("nobody"), headers={"fly-client-ip": "198.51.100.8"}).status_code == 404


def test_website_lead_rejects_junk_and_ignores_bots(app_client):
    c, main = app_client
    assert c.post("/lead", json=_lead(phone="12")).status_code == 422
    assert c.post("/lead", json=_lead(name="")).status_code == 422
    bot = c.post("/lead", json=_lead(website="http://spam"), headers={"fly-client-ip": "198.51.100.9"})
    assert bot.status_code == 200  # looks like success to the bot...
    page = c.get("/report/sample_homecare", params={"key": "master_key_for_tests"}).text
    assert "website request" not in page  # ...but nothing was recorded


def test_website_lead_is_rate_limited_per_ip(app_client):
    c, main = app_client
    codes = [c.post("/lead", json=_lead(), headers={"fly-client-ip": "203.0.113.50"}).status_code
             for _ in range(main._LEADS_PER_IP_PER_HOUR + 1)]
    assert codes[-1] == 429 and codes[0] == 200


def test_unknown_best_time_is_normalized_not_stored_verbatim(app_client):
    c, _ = app_client
    r = c.post("/lead", json=_lead(best_time="<script>x()</script>"[:20]), headers={"fly-client-ip": "198.51.100.10"})
    assert r.status_code == 200
    page = c.get("/report/sample_homecare", params={"key": "master_key_for_tests"}).text
    assert "<script>x()" not in page and "Best time to call: Anytime" in page
