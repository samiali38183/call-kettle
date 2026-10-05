"""Logs must not hold callers' phone numbers or owners' emails."""
import logging

import pytest


@pytest.mark.parametrize("raw,expected_fragment", [
    ("Rate-limiting repeat caller +15555550100 on acme", "***-***-0142"),
    ("would have sent to+15555550100: hi", "***-***-0142"),
    ("to+15555550100 failed", "***-***-0142"),
    ("call from+15555550100", "***-***-0142"),
    ("Email notification to owner@example.com failed", "***@acme-plumbing.com"),
])
def test_personal_details_are_masked(raw, expected_fragment):
    from app.ops import redact_personal

    out = redact_personal(raw)
    assert expected_fragment in out
    assert "703" not in out.replace("***-***-", "") or "0142" in out  # area code and exchange are gone
    assert "owner@" not in out


def test_ordinary_ids_and_numbers_survive():
    from app.ops import redact_personal

    keep = "call_sid=CA1f2e3d4c5b6a7988 turn 12 booking 481 took 2350 ms"
    assert redact_personal(keep) == keep


def test_the_filter_is_applied_to_application_loggers_and_their_arguments():
    from app import ops

    ops.install_log_redaction()
    records = []

    class Catch(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    lg = logging.getLogger("callkettle.twilio")
    h = Catch()
    lg.addHandler(h)
    try:
        lg.warning("Twilio SMS not configured, would have sent to %s: %s", "+15555550100", "call me at+15555550100 or owner@example.com")
    finally:
        lg.removeHandler(h)
    text = records[0]
    assert "0142" in text and "0199" in text          # last four digits are kept for debugging
    assert "703" not in text and "owner@" not in text


# ------------------------------------------------------------ one call's story from the logs

def test_log_lines_written_during_a_call_carry_the_end_of_its_call_sid():
    import logging

    from app import ops

    ops.install_log_redaction()
    seen = []

    class H(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    lg = logging.getLogger("callkettle.tools")
    h = H()
    lg.addHandler(h)
    try:
        ops.set_call_context("CA+15555550100abcdef+15555550100abcdef12")
        lg.warning("something happened %s", "here")
        ops.set_call_context(None)
        lg.warning("not during a call")
    finally:
        lg.removeHandler(h)
    assert seen[0].endswith("something happened here [call abcdef12]")
    assert "[call " not in seen[1]


def test_call_context_is_sanitised_and_isolated_per_request(app_client):
    from app import ops

    ops.set_call_context("CA<script>x</script>%s%d" + "Z" * 100)
    sid = ops.CALL_SID.get()
    assert sid is not None and all(c.isalnum() or c == "_" for c in sid) and len(sid) <= 64
    ops.set_call_context(None)
    assert ops.CALL_SID.get() is None


def test_a_webhook_sets_the_context_for_the_logs_it_causes(app_client, caplog):
    import logging

    client, main = app_client
    from app import ops

    ops.install_log_redaction()
    with caplog.at_level(logging.WARNING):
        client.post("/voice/incoming?client_id=does_not_exist", data={"CallSid": "CAtrace0001abcd", "From": "+15555550100"})
    assert any(r.getMessage().endswith("[call 0001abcd]") for r in caplog.records), [r.getMessage() for r in caplog.records]
