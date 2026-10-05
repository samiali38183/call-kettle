import os

import pytest

os.environ["CALLKETTLE_SKIP_SIGNATURE_CHECK"] = "1"

from app import twilio_utils  # noqa: E402


def test_gather_twiml_contains_expected_tags():
    xml = twilio_utils.gather_twiml(say_text="Hello there", action_url="https://example.com/voice/gather")
    assert "<Gather" in xml
    assert "Hello there" in xml
    assert 'action="https://example.com/voice/gather"' in xml
    assert "<Redirect" in xml


def test_gather_twiml_escapes_special_characters():
    xml = twilio_utils.gather_twiml(say_text="Tom & Jerry's <shop>", action_url="https://example.com/x")
    assert "&amp;" in xml
    assert "&lt;shop&gt;" in xml
    assert "<shop>" not in xml


def test_transfer_twiml_contains_dial():
    xml = twilio_utils.transfer_twiml(say_text="Connecting you now.", phone_number="+15555550111")
    assert ">+15555550111</Dial>" in xml
    assert 'timeout="25"' in xml  # never ring forever
    assert "Connecting you now." in xml
    assert "<Hangup" not in xml  # Dial handles the call end, not an explicit hangup


def test_transfer_twiml_reports_result_back_when_action_url_given():
    xml = twilio_utils.transfer_twiml(
        say_text="Connecting you now.", phone_number="+15555550111", action_url="https://x.test/voice/transfer-result?client_id=a&b=1"
    )
    assert 'action="https://x.test/voice/transfer-result?client_id=a&amp;b=1"' in xml


def test_every_say_uses_the_neural_voice():
    for xml in (
        twilio_utils.gather_twiml(say_text="Hi", action_url="https://x"),
        twilio_utils.say_and_hangup_twiml("Bye"),
        twilio_utils.transfer_twiml(say_text="Hold on", phone_number="+1555"),
    ):
        assert "<Say>" not in xml  # bare <Say> = the robotic default voice
        assert 'voice="Polly.Joanna-Neural"' in xml


@pytest.mark.parametrize(
    "written",
    ["+1-555-777-0101", "555-777-0101", "(555) 777 0101", "5557770101", "+15557770101", "1 555 777 0101"],
)
def test_phone_numbers_are_respelled_for_speech(written):
    spoken = twilio_utils.speakable(f"Is {written} the best number?")
    assert spoken == "Is five five five, seven seven seven, zero one zero one the best number?"


def test_speakable_leaves_prices_dates_and_times_alone():
    text = "That's $1,500 on 2026-01-12 at 10:00, order 12345."
    assert twilio_utils.speakable(text) == text


def test_spoken_reply_in_twiml_has_no_digit_soup():
    xml = twilio_utils.gather_twiml(say_text="Call +1-555-777-0101", action_url="https://x")
    assert "+1-555" not in xml and "five five five" in xml


def test_gather_hints_and_dictation_timeout_are_applied_and_escaped():
    xml = twilio_utils.gather_twiml(
        say_text="What's your phone number?",
        action_url="https://x",
        speech_timeout="3",
        hints='Bright "Smile" Dental,Cleaning',
    )
    assert 'speechTimeout="3"' in xml
    assert "&quot;Smile&quot;" in xml
    assert 'hints="Bright &quot;Smile&quot; Dental,Cleaning"' in xml


def test_say_and_hangup_twiml():
    xml = twilio_utils.say_and_hangup_twiml("Goodbye")
    assert "<Hangup" in xml
    assert "Goodbye" in xml


def test_signature_check_skipped_in_dev_mode():
    assert twilio_utils.validate_signature(url="https://x", form={}, signature="") is True


def test_send_sms_failure_does_not_raise(monkeypatch):
    class FakeMessagesThatFail:
        def create(self, **kwargs):
            raise RuntimeError("simulated: unverified number / no A2P 10DLC registration")

    class FakeTwilioClient:
        def __init__(self):
            self.messages = FakeMessagesThatFail()

    monkeypatch.setattr(twilio_utils, "_client", lambda: FakeTwilioClient())
    monkeypatch.setattr(twilio_utils, "_FROM_NUMBER", "+15555550100")

    result = twilio_utils.send_sms(to="+15551234567", body="You're booked!")
    assert result is False  # must fail soft, never raise
