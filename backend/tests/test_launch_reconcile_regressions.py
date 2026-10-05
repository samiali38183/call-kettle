"""Evidence mismatches must never produce authoritative-looking cost flags."""
import pytest
from tests.test_reconcile_costs import FakeClient, OCT, full_app, make_db, rc, rec, row, tw


@pytest.mark.parametrize("case,expected", [
    ("tenant", "NON_COMPARABLE"), ("partial", "UNKNOWN"),
    ("coverage_missing", "UNKNOWN"), ("scope_missing", "UNKNOWN"),
    ("total_missing", "UNKNOWN"), ("polly_use", "NON_COMPARABLE"),
    ("polly_unit_missing", "NON_COMPARABLE"), ("twilio_field_missing", "UNKNOWN"),
    ("invalid_number", "UNKNOWN"), ("mixed_say_voices", "NON_COMPARABLE"),
])
def test_incompatible_or_missing_evidence_never_computes_delta(case, expected):
    app = full_app()
    app["scope"] = "account"
    app["polly_only"] = True
    records = tw(**{"amazon-polly": rec("amazon-polly", 5, 150, "characters")})
    name = "gather_count"
    if case == "tenant":
        app["scope"] = "tenant:a"
    elif case == "partial":
        app[name]["calls_measured"] = 1
    elif case == "coverage_missing":
        app[name].pop("calls_measured")
    elif case == "scope_missing":
        app.pop("scope")
    elif case == "total_missing":
        app.pop("calls")
    elif case == "twilio_field_missing":
        records["speech-recognition"].pop("count")
    elif case == "invalid_number":
        app[name]["value"] = "not measured"
    elif case == "mixed_say_voices":
        name = "tts_chars"
        app.pop("polly_only")
    else:
        name = "tts_chars"
        records["amazon-polly"]["unit"] = "use" if case == "polly_use" else ""
    result = row(rc.reconcile(app, records), name)
    assert result["status"] == expected
    assert result["delta"] is None and result["pct"] is None


def test_tenant_collection_is_non_comparable_to_account(tmp_path):
    db = make_db(tmp_path, [("a", "C1", OCT, {"gather_count": 3})])
    result = row(rc.reconcile(rc.collect_app(db, "2026-10", tenant="a"), tw()), "gather_count")
    assert result["status"] == "NON_COMPARABLE" and result["delta"] is None


def test_cli_tenant_option_cannot_claim_account_comparison_from_json(tmp_path, capsys):
    import json
    data = full_app()
    data["scope"] = "account"
    path = tmp_path / "app.json"
    path.write_text(json.dumps(data, default=str))
    client = FakeClient([rec("calls-inbound", 2, 3, "minutes"), rec("speech-recognition", 5, 5, "")])
    assert rc.main(["--month", "2026-10", "--app-json", str(path), "--tenant", "a"], client_factory=lambda: client) == 0
    output = capsys.readouterr().out
    assert "NON_COMPARABLE" in output
    assert "not invoice certification" in output
