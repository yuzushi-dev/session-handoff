"""Root acceptance: wire privacy and consent boundaries independent of producer helpers."""
import copy
import gzip
import json
from datetime import datetime, timezone

import pytest
from server import telemetry as t


def status_row():
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return dict(schema_version=4, event="installation_status", day_utc=stamp[:10],
                plugin_version=t.PACKAGE_VERSION, origin="real",
                installation_id="abcd0123" * 4, lifecycle_action="observed",
                observed_at=stamp)


@pytest.mark.parametrize("action", ["registered", "observed", "uninstalled"])
def test_status_exact_wire_contract(action):
    row = status_row() | {"lifecycle_action": action}
    resource = t.to_otlp_logs([row])["resourceLogs"][0]
    assert resource["resource"]["attributes"] == [
        {"key": "service.name", "value": {"stringValue": "session-handoff"}}]
    record = resource["scopeLogs"][0]["logRecords"][0]
    assert record["body"] == {"stringValue": "session_handoff.installation_status"}
    attrs = {a["key"]: next(iter(a["value"].values())) for a in record["attributes"]}
    assert set(attrs) == set(row)
    assert int(attrs.pop("schema_version")) == 4
    assert attrs == {k: v for k, v in row.items() if k != "schema_version"}


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": "4"}, {"schema_version": 4.0},
    {"observed_at": None}, {"observed_at": 0}, {"observed_at": "2026-09-19"},
    {"observed_at": "2026-09-19T00:00:00+00:00"},
    {"observed_at": "2026-09-19T00:00:00.001Z"},
    {"observed_at": "2026-02-30T00:00:00Z"},
    {"observed_at": "2026-09-19T24:00:00Z"},
    {"observed_at": "2026-09-19T00:00:00Z\n"},
    {"day_utc": "2000-01-01"},
    {"installation_id": "A" * 32}, {"installation_id": "a" * 31},
    {"installation_id": "a" * 32 + "\n"}, {"installation_id": {}},
    {"lifecycle_action": "inactive"}, {"lifecycle_action": "observed\n"},
    {"origin": "synthetic"}, {"event": "installation_lifecycle"},
    {"hostname": "private-host"}, {"path": "/private/home"}, {"count": 1},
    {"plugin_version": "1.0.0\n"}, {"plugin_version": "1.0.0-"},
])
def test_status_rejects_noncontract_fields(changes):
    with pytest.raises(ValueError):
        t.to_otlp_logs([status_row() | changes])


@pytest.mark.parametrize("missing", list(status_row()))
def test_status_requires_every_field(missing):
    row = status_row()
    del row[missing]
    with pytest.raises(ValueError):
        t.to_otlp_logs([row])


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("legacy", [False, True])
def test_existing_consent_is_not_silently_upgraded(version, legacy):
    config = t.enabled_config(consent_version=version)
    if legacy:
        config.pop("consent_state")
        config["prompted_consent_version"] = version
    assert t._validate_config(copy.deepcopy(config))["consent_version"] == version


@pytest.mark.parametrize("consent,expected", [(1, {2}), (2, {2, 3}), (3, {2, 3, 4})])
def test_flush_respects_each_consents_scope(tmp_path, monkeypatch, consent, expected):
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    t.write_config(tmp_path, t.enabled_config(consent_version=consent))
    four = status_row()
    three = {k: v for k, v in four.items() if k != "observed_at"}
    three.update(schema_version=3, event="installation_lifecycle", lifecycle_action="registered")
    two = {k: four[k] for k in ("day_utc", "plugin_version", "origin")}
    two.update(schema_version=2, event="active_day")
    with t._state_lock(tmp_path) as (_c, _cf, state, fd):
        t._store_queue_locked(state, fd, [four, three, two])
    sent = []
    class Response:
        status = 200
        done = False
        def read(self, size):
            if self.done:
                return b""
            self.done = True
            return b'{"partialSuccess":{}}'
        def close(self):
            pass
    def opener(request, **kwargs):
        packet = json.loads(gzip.decompress(request.data))
        for resource in packet["resourceLogs"]:
            for scope in resource["scopeLogs"]:
                for record in scope["logRecords"]:
                    attrs = {a["key"]: next(iter(a["value"].values())) for a in record["attributes"]}
                    sent.append(int(attrs["schema_version"]))
        return Response()
    assert t.flush_queue(tmp_path, opener=opener) == len(expected)
    assert set(sent) == expected
    assert {row["schema_version"] for row in t.load_batch(tmp_path)} == {2, 3, 4} - expected

# Reuse the independent real CLI fixture (temporary home and blocked network).
from test_telemetry_lifecycle import lifecycle_cli, lifecycle_state  # noqa: E402, F401


def test_v3_purge_then_yes_registers_new_identity_immediately(lifecycle_cli):  # noqa: F811
    home, cli = lifecycle_cli
    t.write_config(home, t.enabled_config(consent_version=3))
    assert cli["_setup"](["--client", "codex", "--yes"]) == 0
    old = lifecycle_state(home)["installation_id"]
    assert cli["_telemetry"](["disable", "--purge"]) == 0
    assert cli["_telemetry"](["yes"]) == 0
    current = lifecycle_state(home)["installation_id"]
    assert current != old
    statuses = [row for row in t.load_batch(home) if row["event"] == "installation_status"]
    assert len(statuses) == 1
    assert statuses[0]["installation_id"] == current
    assert statuses[0]["lifecycle_action"] == "registered"


def test_v3_daily_observation_version_change_and_dnt(lifecycle_cli, monkeypatch):  # noqa: F811
    home, cli = lifecycle_cli
    t.write_config(home, t.enabled_config(consent_version=3))
    assert cli["_setup"](["--client", "codex", "--yes"]) == 0
    assert t.observe_installation(home) is False
    monkeypatch.setattr(t, "PACKAGE_VERSION", "9.8.7")
    assert t.observe_installation(home) is True
    statuses = [row for row in t.load_batch(home) if row["event"] == "installation_status"]
    assert len(statuses) == 2
    assert any(row["plugin_version"] == "9.8.7" and row["lifecycle_action"] == "observed" for row in statuses)
    assert len({row["installation_id"] for row in statuses}) == 1
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setattr(t, "PACKAGE_VERSION", "9.8.8")
    assert t.observe_installation(home) is False
    monkeypatch.delenv("DO_NOT_TRACK")
    assert len([row for row in t.load_batch(home) if row["event"] == "installation_status"]) == 2
