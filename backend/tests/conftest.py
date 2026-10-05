import os
from datetime import datetime

import pytest

# The suite exercises alerts constantly. Without this, every run pushed real
# notifications to the operator's phone.
os.environ["CALLKETTLE_DISABLE_PUSH"] = "1"


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Pin 'now' to Monday 2026-01-05 09:00 (client-local) so slot logic that
    rejects past times is deterministic and the fixtures' 2026-01-12 dates
    stay in the future forever."""
    from app import tools

    monkeypatch.setattr(tools, "_local_now", lambda config: datetime(2026, 1, 5, 9, 0))
    monkeypatch.delenv("SMS_ENABLED", raising=False)
    yield
    # Notification workers read the process-wide DB path/provider at runtime.
    # Finish them while this test's monkeypatches are still in place, before
    # the next fixture can switch databases or restore real mail settings.
    import threading
    for worker in threading.enumerate():
        target = getattr(worker, "_target", None)
        if getattr(target, "__module__", "") == "app.notify":
            worker.join(timeout=15)
            assert not worker.is_alive(), "Notification worker outlived its isolated test"


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    """A TestClient against a fresh temp database, with signature checks off
    and known report keys."""
    import importlib

    monkeypatch.setenv("CALLKETTLE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("CALLKETTLE_SKIP_SIGNATURE_CHECK", "1")
    monkeypatch.setenv("REPORT_KEY", "master_key_for_tests")

    from app import main, storage, twilio_utils

    importlib.reload(twilio_utils)
    importlib.reload(storage)
    importlib.reload(main)
    storage.init_db()

    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c, main
