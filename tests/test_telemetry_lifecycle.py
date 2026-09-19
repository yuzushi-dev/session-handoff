import copy
import pytest
from server import telemetry as t

ROW = dict(schema_version=3, event="installation_lifecycle", day_utc="2026-09-19",
           plugin_version=t.PACKAGE_VERSION, origin="real",
           installation_id="abcd0123" * 4, lifecycle_action="registered")

@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": "3"}, {"schema_version": 2}, {"schema_version": 3.0},
    {"day_utc": None}, {"plugin_version": None}, {"lifecycle_action": {}}, {"origin": []},
    {"plugin_version": "1.0.0-" + "x" * 27},
    {"installation_id": "abcd0123" * 4 + "\n"},
    {"installation_id": "ABCD0123" * 4}, {"installation_id": "x" * 32},
    {"installation_id": "a" * 31}, {"installation_id": "a" * 33},
    {"installation_id": None}, {"installation_id": 123},
    {"lifecycle_action": "inactive"}, {"lifecycle_action": "uninstalled\n"},
    {"origin": "synthetic"}, {"count": 1}, {"hostname": "test"},
    {"metadata": {}}, {"plugin_version": "1.0.0\n"},
])
def test_lifecycle_rejects_non_contract_values(changes):
    with pytest.raises(ValueError):
        t.to_otlp_logs([ROW | changes])

@pytest.mark.parametrize("missing", list(ROW))
def test_lifecycle_rejects_missing_keys(missing):
    row = ROW.copy()
    del row[missing]
    with pytest.raises(ValueError):
        t.to_otlp_logs([row])

@pytest.mark.parametrize("action", ["registered", "uninstalled"])
def test_exact_serializer_contract(action):
    row = ROW | {"lifecycle_action": action}
    packet = t.to_otlp_logs([row])
    resource = packet["resourceLogs"][0]
    assert resource["resource"]["attributes"] == [
        {"key": "service.name", "value": {"stringValue": "session-handoff"}}]
    record = resource["scopeLogs"][0]["logRecords"][0]
    assert record["body"] == {"stringValue": "session_handoff.installation_lifecycle"}
    assert {item["key"] for item in record["attributes"]} == set(row)
    assert len(record["attributes"]) == 7

@pytest.mark.parametrize("legacy", [False, True])
def test_old_consent_stays_v1(legacy):
    config = dict(schema_version=1, enabled=True, consent_version=1,
                  consented_at="2026-09-01T12:00:00Z", endpoint=t.ENDPOINT)
    config["prompted_consent_version" if legacy else "consent_state"] = 1 if legacy else "enabled"
    assert t._validate_config(copy.deepcopy(config))["consent_version"] == 1

def test_activity_rows_never_get_identifier():
    row = {key: ROW[key] for key in ("day_utc", "plugin_version", "origin")}
    row.update(schema_version=2, event="active_day", installation_id=ROW["installation_id"])
    with pytest.raises(ValueError):
        t.to_otlp_logs([row])


@pytest.fixture
def lifecycle_cli(tmp_path, monkeypatch):
    import os
    import runpy
    from pathlib import Path
    root = Path(t.__file__).resolve().parents[1]
    home = tmp_path / "home"
    bins = home / ".local/bin"
    bins.mkdir(parents=True)
    for client in ("codex", "claude"):
        launcher = bins / client
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(home))
    monkeypatch.setenv("PATH", str(bins) + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.setattr(t, "flush_queue", lambda *a, **kw: {"sent": 0, "status": "offline"})
    # No test may access the public endpoint, even if a new path bypasses flush_queue.
    def no_network(*a, **kw):
        raise AssertionError("unexpected network attempt")
    monkeypatch.setattr(t._NO_REDIRECT_OPENER, "open", no_network)
    cli = runpy.run_path(str(root / "bin/session-handoff"))
    return home, cli


def lifecycle_state(home):
    import json
    return json.loads((home / ".config/session-handoff/state.json").read_text())


def lifecycle_rows(home):
    return [row for row in t.load_batch(home) if row["event"] == "installation_lifecycle"]


def test_real_setup_preserves_identity_until_successful_uninstall(lifecycle_cli):
    home, cli = lifecycle_cli
    t.write_config(home, t.enabled_config())
    assert cli["_setup"](["--client", "codex", "--yes"]) == 0
    installation = lifecycle_state(home)["installation_id"]
    assert len(installation) == 32
    assert len(lifecycle_rows(home)) == 1
    assert cli["_setup"](["--client", "codex", "--yes"]) == 0
    assert cli["_setup"](["--client", "claude", "--yes"]) == 0
    assert lifecycle_state(home)["installation_id"] == installation
    assert len(lifecycle_rows(home)) == 1
    assert cli["_uninstall"](["--yes"]) == 0
    rows = lifecycle_rows(home)
    assert [(r["installation_id"], r["lifecycle_action"]) for r in rows].count((installation, "uninstalled")) == 1
    assert cli["_uninstall"](["--yes"]) == 0
    assert len(lifecycle_rows(home)) == len(rows)
    assert cli["_setup"](["--client", "codex", "--yes"]) == 0
    assert lifecycle_state(home)["installation_id"] != installation


def test_old_consent_setup_does_not_enroll_until_explicit_yes(lifecycle_cli):
    home, cli = lifecycle_cli
    t.write_config(home, t.enabled_config(consent_version=1))
    assert cli["_setup"](["--client", "codex", "--yes"]) == 0
    assert "installation_id" not in lifecycle_state(home)
    assert not lifecycle_rows(home)
    assert t.load_config(home)["consent_version"] == 1
    assert cli["_telemetry"](["yes"]) == 0
    assert t.load_config(home)["consent_version"] == 2
    assert len(lifecycle_rows(home)) == 1


def test_dnt_prevents_identity_creation_even_with_saved_v2_consent(lifecycle_cli, monkeypatch):
    home, cli = lifecycle_cli
    t.write_config(home, t.enabled_config())
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    assert cli["_setup"](["--client", "codex", "--yes"]) == 0
    assert "installation_id" not in lifecycle_state(home)
    assert not lifecycle_rows(home)


def test_externally_changed_launcher_uninstall_does_not_report_removal(lifecycle_cli):
    home, cli = lifecycle_cli
    t.write_config(home, t.enabled_config())
    assert cli["_setup"](["--client", "codex", "--yes"]) == 0
    (home / ".local/bin/codex").write_text("#!/bin/sh\n# externally replaced\nexit 0\n")
    assert cli["_uninstall"](["--yes"]) != 0
    assert all(row["lifecycle_action"] != "uninstalled" for row in lifecycle_rows(home))


def test_purge_then_explicit_yes_creates_new_identity(lifecycle_cli):
    home, cli = lifecycle_cli
    t.write_config(home, t.enabled_config())
    assert cli["_setup"](["--client", "codex", "--yes"]) == 0
    old = lifecycle_state(home)["installation_id"]
    assert cli["_telemetry"](["disable", "--purge"]) == 0
    state = lifecycle_state(home)
    assert "installation_id" not in state and not state.get("lifecycle_registered")
    assert not lifecycle_rows(home)
    assert cli["_telemetry"](["yes"]) == 0
    assert lifecycle_state(home)["installation_id"] != old
    assert len(lifecycle_rows(home)) == 1


def test_corrupt_telemetry_config_does_not_fail_successful_uninstall(lifecycle_cli):
    home, cli = lifecycle_cli
    t.write_config(home, t.enabled_config())
    assert cli["_setup"](["--client", "codex", "--yes"]) == 0
    (home / t.CONFIG_PATH).write_text("invalid config")
    assert cli["_uninstall"](["--yes"]) == 0
    assert not (home / ".config/session-handoff/state.json").exists()
    assert (home / ".local/bin/codex").read_text() == "#!/bin/sh\nexit 0\n"


def test_cancelled_uninstall_does_not_report_removal(lifecycle_cli, monkeypatch):
    home, cli = lifecycle_cli
    t.write_config(home, t.enabled_config())
    assert cli["_setup"](["--client", "codex", "--yes"]) == 0
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert cli["_uninstall"]([]) == 0
    assert (home / ".config/session-handoff/state.json").exists()
    assert all(row["lifecycle_action"] != "uninstalled" for row in lifecycle_rows(home))
