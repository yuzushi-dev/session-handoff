import hashlib
import json
from pathlib import Path

from benchmark.native_fixture import build_paginated_codex_home
from server import session_switch
from server.migration import migrate_session


class Process:
    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 143

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = 137


def test_paginated_manifest_keeps_native_source_provenance(tmp_path):
    session_id = "10000000-0000-4000-8000-000000000001"
    source_home = tmp_path / "codex"
    rollout = build_paginated_codex_home(source_home, session_id)

    result = migrate_session(
        "codex",
        "claude",
        session_id,
        str(tmp_path),
        source_home=str(source_home),
        target_home=str(tmp_path / "claude"),
        target_session_id="20000000-0000-4000-8000-000000000001",
    )

    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert Path(manifest["source"]["path"]) == rollout.resolve()
    assert manifest["source"]["sha256"] == hashlib.sha256(rollout.read_bytes()).hexdigest()


def _supervisor(tmp_path, popen, outcomes, monkeypatch):
    monkeypatch.setattr(session_switch.telemetry, "session_start_flush", lambda: None)
    monkeypatch.setattr(session_switch, "record_terminal_outcome", outcomes.append)

    return session_switch.SessionSupervisor(
        "codex",
        [],
        popen=popen,
        sleep=lambda _: None,
        temp_dir=tmp_path / "control",
        client_executables={"codex": "codex", "claude": "claude"},
        migrate=lambda *args, **kwargs: {
            "session_id": "20000000-0000-4000-8000-000000000001",
            "warnings": [],
            "dropped_events": {},
            "context_loss": {},
        },
    )


def _request_source(control, token, workspace):
    session_switch.write_migration_request(
        str(control),
        token,
        str(workspace),
        "codex",
        "claude",
        "10000000-0000-4000-8000-000000000001",
    )


def test_target_spawn_failure_resumes_exact_source_once(tmp_path, monkeypatch):
    calls = []
    outcomes = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        if len(calls) == 1:
            env = kwargs["env"]
            control = Path(env[session_switch.CONTROL_PATH_ENV])
            _request_source(control, control.with_name("token").read_text(), tmp_path)
            return Process()
        if len(calls) == 2:
            raise OSError("synthetic target launch failure")
        return Process(0)

    assert _supervisor(tmp_path, popen, outcomes, monkeypatch).run() == 0
    assert len(calls) == 3
    assert calls[-1][0][0] == "codex"
    assert "resume" in calls[-1][0] and "10000000-0000-4000-8000-000000000001" in calls[-1][0]
    assert outcomes[-1]["result"] == "fallback"


def test_target_early_exit_resumes_exact_source_once(tmp_path, monkeypatch):
    calls = []
    outcomes = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        if len(calls) == 1:
            env = kwargs["env"]
            control = Path(env[session_switch.CONTROL_PATH_ENV])
            _request_source(control, control.with_name("token").read_text(), tmp_path)
            return Process()
        return Process(1 if len(calls) == 2 else 0)

    assert _supervisor(tmp_path, popen, outcomes, monkeypatch).run() == 0
    assert len(calls) == 3
    assert calls[-1][0][0] == "codex"
    assert "resume" in calls[-1][0] and "10000000-0000-4000-8000-000000000001" in calls[-1][0]
    assert outcomes[-1]["result"] == "fallback"
