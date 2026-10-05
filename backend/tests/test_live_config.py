"""Clients uploaded to the volume: served instantly, validated, private, removable."""
import pytest

KEY = "master_key_for_tests"


@pytest.fixture
def live(app_client, tmp_path, monkeypatch):
    client, main = app_client
    from app import config as config_module

    monkeypatch.setattr(config_module, "LIVE_CLIENTS_DIR", tmp_path / "live")
    config_module.load_client_config.cache_clear()
    yield client, config_module
    config_module.load_client_config.cache_clear()


def _yaml(client_id="acme_plumbing", name="Acme Plumbing"):
    base = (__import__("pathlib").Path(__file__).resolve().parent.parent / "clients" / "demo_hvac.yaml").read_text(encoding="utf-8")
    lines = []
    for line in base.splitlines():
        if line.startswith("client_id:"):
            line = f"client_id: {client_id}"
        elif line.startswith("business_name:"):
            line = f'business_name: "{name}"'
        lines.append(line)
    return "\n".join(lines) + "\n"


def _upload(client, text, key=KEY):
    return client.post(f"/admin/client/upload?key={key}", content=text.encode("utf-8"))


def test_upload_needs_the_master_key(live):
    client, _ = live
    assert _upload(client, _yaml(), key="").status_code == 403
    assert _upload(client, _yaml(), key="wrong").status_code == 403


def test_uploaded_client_answers_calls_immediately_without_a_restart(live):
    client, cfg = live
    form = {"CallSid": "CA_X1", "From": "+15555550100"}
    before = client.post("/voice/incoming?client_id=acme_plumbing", data=form)
    assert before.status_code == 200 and "AI" not in before.text.split("<Say")[-1][:40]  # unknown client: safe spoken error
    assert _upload(client, _yaml()).status_code == 200
    opening = cfg.load_client_config("acme_plumbing").opening_line
    after = client.post("/voice/incoming?client_id=acme_plumbing", data={"CallSid": "CA_X2", "From": "+15555550100"})
    assert after.status_code == 200 and opening.split(".")[0][:30] in after.text


def test_upload_reply_reports_what_happened_and_the_dashboard_link(live):
    client, _ = live
    r = _upload(client, _yaml())
    assert r.json()["created"] is True and r.json()["client_id"] == "acme_plumbing"
    assert "/report/acme_plumbing?key=" in r.json()["dashboard"]


def test_changing_a_live_client_takes_effect_at_once(live):
    client, cfg = live
    _upload(client, _yaml(name="Acme Plumbing"))
    assert cfg.load_client_config("acme_plumbing").business_name == "Acme Plumbing"
    r = _upload(client, _yaml(name="Acme Plumbing and Heating"))
    assert r.json()["created"] is False
    assert cfg.load_client_config("acme_plumbing").business_name == "Acme Plumbing and Heating"


@pytest.mark.parametrize("bad", [
    "not: [valid",                                   # broken YAML
    "- just\n- a list\n",                            # not a mapping
    "client_id: ../evil\nbusiness_name: x\n",        # path traversal
    "client_id: _hidden\nbusiness_name: x\n",        # reserved prefix
    "client_id: no_fields\nbusiness_name: x\n",      # missing required fields
])
def test_bad_configs_are_rejected_with_a_reason_and_change_nothing(live, bad):
    client, cfg = live
    r = _upload(client, bad)
    assert r.status_code == 422 and r.json()["error"]
    assert "acme_plumbing" not in cfg.list_client_ids()


def test_a_config_that_does_not_admit_being_ai_is_rejected(live):
    client, _ = live
    text = _yaml().replace("AI", "friendly").replace("ai ", "friendly ")
    lines = [l for l in text.splitlines() if not l.startswith("opening_line:")]
    lines.append('opening_line: "Hello, thanks for calling!"')
    r = _upload(client, "\n".join(lines) + "\n")
    assert r.status_code == 422


def test_oversized_upload_is_refused(live):
    client, _ = live
    assert _upload(client, "x: " + "a" * 200_000).status_code == 413


def test_uploaded_clients_appear_in_admin_export_and_client_list(live):
    client, cfg = live
    _upload(client, _yaml())
    assert "acme_plumbing" in cfg.list_client_ids()
    exported = client.get(f"/admin/export?key={KEY}").json()
    assert "acme_plumbing" in exported["client_configs"]
    assert "Acme Plumbing" in client.get(f"/admin?key={KEY}").text


def test_a_live_config_overrides_a_baked_one_and_removal_reverts(live):
    client, cfg = live
    baked_name = cfg.load_client_config("demo_hvac").business_name
    _upload(client, _yaml(client_id="demo_hvac", name="Overridden Name"))
    assert cfg.load_client_config("demo_hvac").business_name == "Overridden Name"
    r = client.post(f"/admin/client/demo_hvac/config-remove?key={KEY}&confirm=demo_hvac")
    assert r.json() == {"removed": True, "still_served_from_image": True}
    assert cfg.load_client_config("demo_hvac").business_name == baked_name


def test_removing_a_client_needs_confirmation_and_the_key(live):
    client, cfg = live
    _upload(client, _yaml())
    assert client.post("/admin/client/acme_plumbing/config-remove?key=bad&confirm=acme_plumbing").status_code == 403
    assert client.post(f"/admin/client/acme_plumbing/config-remove?key={KEY}").status_code == 400
    assert "acme_plumbing" in cfg.list_client_ids()
    r = client.post(f"/admin/client/acme_plumbing/config-remove?key={KEY}&confirm=acme_plumbing")
    assert r.json()["removed"] is True and "acme_plumbing" not in cfg.list_client_ids()


def test_without_a_live_folder_uploads_are_refused_cleanly(app_client, monkeypatch):
    client, _ = app_client
    from app import config as config_module

    monkeypatch.setattr(config_module, "LIVE_CLIENTS_DIR", None)
    assert _upload(client, _yaml()).status_code == 422
