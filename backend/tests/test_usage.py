"""Every call records its real model usage, so pricing rests on measurements."""
import os
import tempfile
from types import SimpleNamespace

import pytest


@pytest.fixture
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("CALLKETTLE_DB_PATH", path)
    import importlib

    from app import storage

    importlib.reload(storage)
    storage.init_db()
    yield storage
    os.remove(path)


def test_usage_accumulates_across_model_calls(db):
    from app import agent

    db.log_call_start("CA_U1", "acme", "+15555550100")
    resp = SimpleNamespace(usage=SimpleNamespace(input_tokens=1200, output_tokens=40))
    import time

    agent.record_usage("CA_U1", resp, time.perf_counter() - 0.5)
    agent.record_usage("CA_U1", resp, time.perf_counter() - 0.25)
    conn = __import__("sqlite3").connect(db.DB_PATH)
    row = conn.execute("SELECT input_tokens, output_tokens, model_calls, model_ms FROM calls WHERE call_sid='CA_U1'").fetchone()
    conn.close()
    assert row[:3] == (2400, 80, 2) and 600 <= row[3] <= 1200


def test_a_response_without_usage_is_ignored_not_fatal(db):
    from app import agent

    db.log_call_start("CA_U2", "acme", "+15555550100")
    agent.record_usage("CA_U2", SimpleNamespace(), 0.0)  # no .usage attribute
    agent.record_usage("CA_NOPE", SimpleNamespace(usage=SimpleNamespace(input_tokens=1, output_tokens=1)), 0.0)  # unknown call


def test_spoken_characters_are_counted_even_when_words_are_not_stored(db):
    db.log_call_start("CA_U3", "acme", "+15555550100")
    db.log_turn("CA_U3", "caller", "hello there", store_text=False)
    db.log_turn("CA_U3", "ai", "Thanks for calling, how can I help?", store_text=False)
    conn = __import__("sqlite3").connect(db.DB_PATH)
    spoken = conn.execute("SELECT tts_chars FROM calls WHERE call_sid='CA_U3'").fetchone()[0]
    conn.close()
    assert spoken == len("Thanks for calling, how can I help?")
    assert "not recorded" in db.get_call("CA_U3")["transcript_json"]


def test_old_databases_are_migrated_in_place(tmp_path, monkeypatch):
    import importlib
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE calls (call_sid TEXT PRIMARY KEY, client_id TEXT NOT NULL, from_number TEXT, started_at TEXT NOT NULL, ended_at TEXT, turn_count INTEGER NOT NULL DEFAULT 0, outcome TEXT, transcript_json TEXT NOT NULL DEFAULT '[]')")
    conn.execute("INSERT INTO calls (call_sid, client_id, started_at) VALUES ('OLD', 'acme', '2026-01-01')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("CALLKETTLE_DB_PATH", str(path))
    from app import storage

    importlib.reload(storage)
    storage.init_db()
    storage.init_db()  # idempotent
    row = sqlite3.connect(path).execute("SELECT input_tokens, tts_chars FROM calls WHERE call_sid='OLD'").fetchone()
    assert row == (0, 0)
