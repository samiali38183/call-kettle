"""Account-wide Twilio spend guard: script logic (fake Twilio client only) and the
trigger callback endpoint (real Twilio signature algorithm, no network)."""
from __future__ import annotations

import importlib.util
import importlib
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "twilio_usage_guard.py"
SECRET_TOKEN = "tok_SECRET_+15555550100abcdef"
SECRET_SID = "AC" + "1" * 32


def load_guard():
    spec = importlib.util.spec_from_file_location("twilio_usage_guard", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeRecords:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def list(self, **kw):
        self.calls += 1
        return list(self.rows)


class FakeTriggers:
    def __init__(self, existing=()):
        self.existing = list(existing)
        self.created = []
        self.deleted = []

    def list(self, **kw):
        return list(self.existing)

    def create(self, **kw):
        self.created.append(kw)
        sid = "UT" + str(len(self.created)).zfill(32)
        self.existing.append(SimpleNamespace(sid=sid, friendly_name=kw.get("friendly_name"), **{k: v for k, v in kw.items() if k != "friendly_name"}))
        return SimpleNamespace(sid=sid, **kw)


class FakeClient:
    def __init__(self, rows=(), existing=()):
        self.records = FakeRecords(rows)
        self.triggers = FakeTriggers(existing)
        self.usage = SimpleNamespace(records=SimpleNamespace(this_month=self.records), triggers=self.triggers)


def rec(category, count, usage, price, unit="minutes"):
    return SimpleNamespace(category=category, count=count, usage=usage, price=price, usage_unit=unit,
                           description=category, start_date="2026-10-01", end_date="2026-10-04")


ROWS = [
    rec("totalprice", None, "12.34", "12.34", "usd"),
    rec("calls-inbound", "40", "88", "0.75", "minutes"),
    rec("speech-recognition", "60", "60", "1.20", "uses"),
    rec("sms-outbound", "5", "5", "0.04", "messages"),
    rec("phonenumbers", "2", "2", "2.00", "numbers"),
    rec("lookups", "0", "0", "0", "lookups"),
]


# ---------------------------------------------------------------- plan
def test_plan_defaults_are_derived_and_dry():
    g = load_guard()
    plan = g.build_plan()
    price = [t for t in plan if t["usage_category"] == "totalprice" and t["recurring"] == "monthly"]
    assert sorted(Decimal(t["trigger_value"]) for t in price) == [Decimal(60), Decimal(120), Decimal(200)]
    assert all(t["trigger_by"] == "price" for t in price)
    assert any(t["trigger_by"] == "count" and t["recurring"] == "daily" and t["usage_category"] == "calls-inbound" for t in plan)
    assert all(t["callback_url"] == "https://app.callkettle.com/ops/twilio-usage-trigger" for t in plan)
    assert all(len(t["friendly_name"]) <= 64 and t["friendly_name"].startswith(g.NAME_PREFIX) for t in plan)
    assert all(t["callback_method"] == "POST" for t in plan)


def test_plan_thresholds_overridable_and_validated():
    g = load_guard()
    plan = g.build_plan(monthly_price=["10", "20"], daily_price="5", daily_calls="50", daily_sms="7", daily_speech="90")
    values = {(t["usage_category"], t["recurring"], t["trigger_by"]): [] for t in plan}
    for t in plan:
        values[(t["usage_category"], t["recurring"], t["trigger_by"])].append(t["trigger_value"])
    assert values[("totalprice", "monthly", "price")] == ["10", "20"]
    assert values[("calls-inbound", "daily", "count")] == ["50"]
    with pytest.raises(ValueError):
        g.build_plan(monthly_price=["-1"])
    with pytest.raises(ValueError):
        g.build_plan(monthly_price=["abc"])
    with pytest.raises(ValueError):
        g.build_plan(callback_url="http://app.callkettle.com/x")


def test_plan_command_never_touches_twilio(capsys):
    g = load_guard()
    client = FakeClient()
    rc = g.main(["plan"], client_factory=lambda: (_ for _ in ()).throw(AssertionError("plan must not build a client")))
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "totalprice" in out and "60" in out


# ---------------------------------------------------------------- status
def test_status_reads_this_month_and_prints_no_secrets(capsys, monkeypatch):
    g = load_guard()
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", SECRET_TOKEN)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", SECRET_SID)
    client = FakeClient(ROWS)
    rc = g.main(["status"], client_factory=lambda: client)
    out = capsys.readouterr().out
    assert rc == 0 and client.records.calls == 1
    assert "totalprice" in out and "12.34" in out and "calls-inbound" in out and "speech-recognition" in out
    assert SECRET_TOKEN not in out and SECRET_SID not in out
    assert client.triggers.created == []


def test_status_summary_structure():
    g = load_guard()
    s = g.collect_status(FakeClient(ROWS))
    assert s["total_price"] == Decimal("12.34")
    assert s["categories"]["calls-inbound"]["price"] == Decimal("0.75")
    assert "lookups" not in s["categories"] or s["categories"]["lookups"]["price"] == 0
    assert s["month"]


# ---------------------------------------------------------------- apply
def test_apply_requires_flag(capsys):
    g = load_guard()
    client = FakeClient()
    with pytest.raises(SystemExit):
        g.main(["apply"], client_factory=lambda: client)
    assert client.triggers.created == []


def test_apply_creates_all_and_is_idempotent(capsys):
    g = load_guard()
    client = FakeClient()
    flag = "--i-understand-this-modifies-my-twilio-account"
    ok = lambda url: True
    assert g.main(["apply", flag], client_factory=lambda: client, endpoint_check=ok) == 0
    n = len(client.triggers.created)
    assert n == len(g.build_plan()) and n >= 5
    assert g.main(["apply", flag], client_factory=lambda: client, endpoint_check=ok) == 0
    assert len(client.triggers.created) == n  # second run creates nothing
    out = capsys.readouterr().out
    assert "already exists" in out


def test_apply_refuses_when_endpoint_not_live(capsys):
    g = load_guard()
    client = FakeClient()
    rc = g.main(["apply", "--i-understand-this-modifies-my-twilio-account"], client_factory=lambda: client,
                endpoint_check=lambda url: False)
    assert rc != 0 and client.triggers.created == []
    assert "not live" in capsys.readouterr().out.lower()


def test_guard_source_has_no_secret_printing():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "print(token" not in src.lower() and "auth_token}" not in src.lower()


# ---------------------------------------------------------------- endpoint
@pytest.fixture
def ep(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLKETTLE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("CALLKETTLE_SKIP_SIGNATURE_CHECK", raising=False)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", SECRET_TOKEN)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", SECRET_SID)
    from app import main, storage, twilio_utils

    monkeypatch.setattr(twilio_utils, "_AUTH_TOKEN", SECRET_TOKEN)
    monkeypatch.setattr(twilio_utils, "_SKIP_SIGNATURE_CHECK", False)
    importlib.reload(storage)
    importlib.reload(main)
    storage.init_db()
    alerts = []
    from app import ops
    monkeypatch.setattr(ops, "alert_operator", lambda title, body, **kw: alerts.append((title, body, kw)) or True)
    from fastapi.testclient import TestClient
    with TestClient(main.app, base_url="https://app.callkettle.com") as c:
        yield c, alerts


def form(**over):
    base = {"AccountSid": SECRET_SID, "UsageTriggerSid": "UT" + "a" * 32, "DateFired": "Sun, 04 Oct 2026 12:00:00 +0000",
            "Recurring": "monthly", "UsageCategory": "totalprice", "TriggerBy": "price", "TriggerValue": "60",
            "CurrentValue": "60.52", "UsageRecordUri": "/2010-04-01/Accounts/x/Usage/Records.json?Category=totalprice",
            "IdempotencyToken": "idem-1"}
    base.update(over)
    return base


def signed(data, url="https://app.callkettle.com/ops/twilio-usage-trigger", token=SECRET_TOKEN):
    from twilio.request_validator import RequestValidator
    return {"X-Twilio-Signature": RequestValidator(token).compute_signature(url, data)}


URL = "/ops/twilio-usage-trigger"


def test_endpoint_rejects_missing_and_bad_signature(ep):
    c, alerts = ep
    assert c.post(URL, data=form()).status_code == 403
    assert c.post(URL, data=form(), headers={"X-Twilio-Signature": "bogus"}).status_code == 403
    assert c.post(URL, data=form(), headers=signed(form(), token="wrong")).status_code == 403
    assert alerts == []


def test_endpoint_rejects_other_account(ep):
    c, alerts = ep
    d = form(AccountSid="AC" + "9" * 32)
    assert c.post(URL, data=d, headers=signed(d)).status_code == 403
    assert alerts == []


def test_endpoint_accepts_valid_trigger_and_alerts_operator(ep):
    c, alerts = ep
    d = form()
    r = c.post(URL, data=d, headers=signed(d))
    assert r.status_code == 200
    assert len(alerts) == 1
    title, body, kw = alerts[0]
    assert "Twilio" in title
    assert "totalprice" in body and "60.52" in body and "monthly" in body
    assert kw.get("min_interval") == 0


def test_endpoint_is_idempotent_on_token(ep):
    c, alerts = ep
    d = form()
    assert c.post(URL, data=d, headers=signed(d)).status_code == 200
    assert c.post(URL, data=d, headers=signed(d)).status_code == 200
    assert len(alerts) == 1
    d2 = form(IdempotencyToken="idem-2", TriggerValue="120")
    assert c.post(URL, data=d2, headers=signed(d2)).status_code == 200
    assert len(alerts) == 2


def test_endpoint_does_not_leak_secrets_or_echo_input(ep):
    c, alerts = ep
    d = form(CurrentValue="61<script>")
    r = c.post(URL, data=d, headers=signed(d))
    blob = r.text + repr(alerts)
    assert SECRET_TOKEN not in blob and SECRET_SID not in blob
    assert "<script>" not in blob


def test_endpoint_malformed_payload_is_400_not_alert(ep):
    c, alerts = ep
    d = {"AccountSid": SECRET_SID}
    r = c.post(URL, data=d, headers=signed(d))
    assert r.status_code == 400
    assert alerts == []
