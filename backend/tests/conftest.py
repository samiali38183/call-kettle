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

# --- Public snapshot housekeeping -------------------------------------------------------------------------------------
# These two test files import tooling that is not part of this export, so they cannot even be collected here.
collect_ignore = ["test_practice.py", "test_sales_recheck.py"]


def _known_snapshot_failures() -> set[str]:
    """Node ids listed in snapshot_known_failures.txt (see the comments at the top of that file for why they fail)."""
    from pathlib import Path

    path = Path(__file__).with_name("snapshot_known_failures.txt")
    if not path.exists():
        return set()
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return {line for line in lines if line and not line.startswith("#")}


def pytest_collection_modifyitems(config, items):
    known = _known_snapshot_failures()
    if not known:
        return
    marker = pytest.mark.xfail(
        reason="fails in the public snapshot: needs something the export left out or its fixtures were rewritten (tests/snapshot_known_failures.txt)",
        strict=False,
    )
    for item in items:
        if item.nodeid.replace("\\", "/") in known:
            item.add_marker(marker)
