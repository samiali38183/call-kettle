"""A wrong weekday next to a date is corrected before the caller hears it."""
import os
import tempfile
from datetime import date

import pytest

from app.datefix import fix_weekdays

TODAY = date(2026, 9, 30)      # a Wednesday; 2026-10-02 is a Friday, 2026-10-05 a Monday


@pytest.mark.parametrize("said,expected", [
    ("Your appointment is confirmed for Thursday, October 2nd at 10 a.m.", "Your appointment is confirmed for Friday, October 2nd at 10 a.m."),
    ("how about Monday October 2", "how about Friday October 2"),
    ("thursday, october 2nd works", "friday, october 2nd works"),
    ("Friday, the October 2nd", "Friday, the October 2nd"),
    ("Tuesday, October 6th at nine, or Thursday, October 8th at ten", "Tuesday, October 6th at nine, or Thursday, October 8th at ten"),
    ("Sunday, October 5th", "Monday, October 5th"),
])
def test_english_weekdays_follow_the_calendar(said, expected):
    assert fix_weekdays(said, TODAY)[0] == expected


def test_correct_text_is_returned_untouched_and_counted_as_zero():
    text = "We have Friday, October 2nd at nine, or Saturday, October 3rd at ten."
    assert fix_weekdays(text, TODAY) == (text, 0)


def test_the_count_says_how_many_were_wrong():
    assert fix_weekdays("Monday, October 2nd or Tuesday, October 3rd", TODAY)[1] == 2


@pytest.mark.parametrize("text", [
    "We are open Monday through Friday.", "Call us on Monday.", "See you on October 2nd.", "Friday at ten.", "", "Thursday, Octember 2",
    "Friday, February 30th",
])
def test_anything_that_is_not_weekday_month_day_is_left_alone(text):
    assert fix_weekdays(text, TODAY)[0] == text


def test_a_date_early_next_year_resolves_to_next_year():
    # Dec 30 2026 (Wed): "Thursday, January 7th" is 2027-01-07, which really is a Thursday
    assert fix_weekdays("Thursday, January 7th", date(2026, 12, 30)) == ("Thursday, January 7th", 0)
    assert fix_weekdays("Friday, January 7th", date(2026, 12, 30))[0] == "Thursday, January 7th"


def test_spanish_weekdays_are_corrected_too():
    assert fix_weekdays("Su cita es el jueves, 2 de octubre a las diez.", TODAY)[0] == "Su cita es el viernes, 2 de octubre a las diez."
    assert fix_weekdays("el viernes 2 de octubre", TODAY) == ("el viernes 2 de octubre", 0)
    assert fix_weekdays("el miércoles, 7 de octubre", TODAY)[0] == "el miércoles, 7 de octubre"


def test_the_agent_corrects_a_wrong_weekday_in_the_final_reply(monkeypatch):
    from tests.test_agent import FakeAnthropicClient, FakeResponse, FakeTextBlock

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("CALLKETTLE_DB_PATH", path)
    import importlib

    from app import agent, storage

    importlib.reload(storage)
    storage.init_db()
    from app.config import load_client_config

    real = agent.datefix.fix_weekdays
    monkeypatch.setattr(agent.datefix, "fix_weekdays", lambda text, today: real(text, TODAY))
    fake = FakeAnthropicClient([FakeResponse([FakeTextBlock("We could do Thursday, October 2nd at ten. Does that work?")], "end_turn")])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)
    storage.log_call_start("CA_DF", "demo_hvac", "+15555550100")
    s = agent.start_session("CA_DF", load_client_config("demo_hvac"), "+15555550100")
    reply, ended, _ = agent.run_turn(s, "what do you have")
    assert "Friday, October 2nd" in reply and "Thursday" not in reply and ended is False
    import sqlite3

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM metrics WHERE name='weekday_corrected'").fetchone()[0] == 1
    conn.close()
    os.remove(path)
