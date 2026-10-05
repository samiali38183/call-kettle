"""Tenant isolation: one client's config, data, keys and calls can never touch another's."""
import pytest

KEY = "master_key_for_tests"


@pytest.fixture
def two_clients(app_client, tmp_path, monkeypatch):
    client, main = app_client
    from app import config as config_module
    from app import storage

    monkeypatch.setattr(config_module, "LIVE_CLIENTS_DIR", tmp_path / "live")
    config_module.load_client_config.cache_clear()

    def yaml_for(cid, name):
        from pathlib import Path

        base = (Path(__file__).resolve().parent.parent / "clients" / "demo_hvac.yaml").read_text(encoding="utf-8")
        out = []
        for line in base.splitlines():
            if line.startswith("client_id:"):
                line = f"client_id: {cid}"
            elif line.startswith("business_name:"):
                line = f'business_name: "{name}"'
            out.append(line)
        return "\n".join(out) + "\n"

    for cid, name in (("tenant_a", "Alpha Plumbing"), ("tenant_b", "Bravo Electric")):
        assert client.post(f"/admin/client/upload?key={KEY}", content=yaml_for(cid, name).encode()).status_code == 200
    yield client, main, config_module, storage, yaml_for
    config_module.load_client_config.cache_clear()


def test_updating_one_clients_config_leaves_the_other_untouched(two_clients):
    client, main, cfg, storage, yaml_for = two_clients
    before_b = cfg.load_client_config("tenant_b")
    client.post(f"/admin/client/upload?key={KEY}", content=yaml_for("tenant_a", "Alpha Renamed").encode())
    assert cfg.load_client_config("tenant_a").business_name == "Alpha Renamed"
    assert cfg.load_client_config("tenant_b") == before_b


def test_removing_one_client_leaves_the_other_serving(two_clients):
    client, main, cfg, storage, yaml_for = two_clients
    client.post(f"/admin/client/tenant_a/config-remove?key={KEY}&confirm=tenant_a")
    assert "tenant_a" not in cfg.list_client_ids() and "tenant_b" in cfg.list_client_ids()
    r = client.post("/voice/incoming?client_id=tenant_b", data={"CallSid": "CA_ISO1", "From": "+15555550100"})
    assert "<Gather" in r.text and "Bravo Electric" in r.text or "AI" in r.text


def test_a_booking_for_one_client_does_not_block_the_same_slot_for_another(two_clients):
    client, main, cfg, storage, yaml_for = two_clients
    from app import tools

    a, b = cfg.load_client_config("tenant_a"), cfg.load_client_config("tenant_b")
    first = tools.book_appointment(call_sid="CA_A", config=a, caller_name="A", caller_phone="+15555550100",
                                   service="Emergency repair", date="2026-01-12", time="10:00")
    assert first["success"] is True
    assert "10:00" not in tools.check_availability(config=a, date="2026-01-12", limit=50)["slots"]
    assert "10:00" in tools.check_availability(config=b, date="2026-01-12", limit=50)["slots"]
    second = tools.book_appointment(call_sid="CA_B", config=b, caller_name="B", caller_phone="+15555550100",
                                    service="Emergency repair", date="2026-01-12", time="10:00")
    assert second["success"] is True


def test_a_clients_dashboard_key_opens_only_that_clients_dashboard(two_clients):
    client, main, cfg, storage, yaml_for = two_clients
    key_a, key_b = main.report_key_for("tenant_a"), main.report_key_for("tenant_b")
    assert key_a != key_b
    assert client.get(f"/report/tenant_a?key={key_a}").status_code == 200
    assert client.get(f"/report/tenant_b?key={key_a}").status_code == 403
    assert client.get(f"/report/tenant_a?key={key_b}").status_code == 403
    assert client.get("/report/tenant_a").status_code == 403


def test_one_clients_calls_never_appear_on_anothers_dashboard(two_clients):
    client, main, cfg, storage, yaml_for = two_clients
    storage.log_call_start("CA_SECRET_A", "tenant_a", "+15555550100")
    storage.log_turn("CA_SECRET_A", "caller", "my address is 12 Private Lane")
    page_b = client.get(f"/report/tenant_b?key={main.report_key_for('tenant_b')}").text
    assert "Private Lane" not in page_b and "0177" not in page_b
    page_a = client.get(f"/report/tenant_a?key={main.report_key_for('tenant_a')}").text
    assert "Private Lane" in page_a


def test_the_clients_admin_delete_erases_only_that_clients_data(two_clients):
    client, main, cfg, storage, yaml_for = two_clients
    storage.log_call_start("CA_DA", "tenant_a", "+15555550100")
    storage.log_call_start("CA_DB", "tenant_b", "+15555550100")
    client.post(f"/admin/client/tenant_a/delete?key={KEY}&confirm=tenant_a")
    assert storage.get_call("CA_DA") is None and storage.get_call("CA_DB") is not None


@pytest.mark.parametrize("evil", ["../clients/callkettle_sales", "tenant_a/../tenant_b", "tenant_a%00", "TENANT_A; DROP TABLE calls", ""])
def test_hostile_client_ids_are_refused_not_resolved(two_clients, evil):
    client, main, cfg, storage, yaml_for = two_clients
    r = client.post(f"/voice/incoming?client_id={evil}", data={"CallSid": "CA_EVIL", "From": "+15555550100"})
    assert "Gather" not in r.text          # never starts an AI conversation for a client that doesn't exist
    if ".." not in evil:  # an HTTP client rewrites dot-segments itself, so that path is really a different, legitimate URL
        assert client.get(f"/report/{evil}?key={KEY}").status_code in (403, 404, 422)


def test_the_public_booking_page_can_only_ever_touch_the_sales_calendar(two_clients):
    client, main, cfg, storage, yaml_for = two_clients
    r = client.post("/book/confirm", json={"name": "X", "phone": "+15555550100", "date": "2026-01-12", "time": "09:00", "client_id": "tenant_a"})
    assert r.status_code in (200, 409, 422)
    import sqlite3

    conn = sqlite3.connect(storage.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM bookings WHERE client_id = 'tenant_a'").fetchone()[0] == 0
    conn.close()
