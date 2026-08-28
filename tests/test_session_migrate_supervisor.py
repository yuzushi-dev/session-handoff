from pathlib import Path

from server.migration import MigrationError
from server.session_switch import CONTROL_PATH_ENV, SessionSupervisor, write_migration_request
from server import telemetry


class FakeProcess:
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


def test_supervisor_migrates_after_stopping_source_and_resumes_target(tmp_path):
    calls = []
    migrated = []

    def fake_migrate(source, target, source_session_id, workspace, **kwargs):
        migrated.append((source, target, source_session_id, workspace, kwargs))
        return {
            "session_id": "target-id",
            "warnings": [],
            "dropped_events": {},
        }

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        process = FakeProcess()
        if len(calls) == 1:
            control = Path(kwargs["env"][CONTROL_PATH_ENV])
            write_migration_request(
                str(control),
                control.with_name("token").read_text(encoding="utf-8"),
                str(tmp_path),
                "claude",
                "codex",
                "source-id",
            )
        else:
            process.returncode = 0
        return process

    supervisor = SessionSupervisor(
        "claude",
        [],
        popen=fake_popen,
        sleep=lambda _: None,
        temp_dir=tmp_path / "control",
        client_executables={"claude": "claude", "codex": "codex"},
        migrate=fake_migrate,
    )

    assert supervisor.run() == 0
    assert migrated[0][0:4] == ("claude", "codex", "source-id", str(tmp_path))
    assert calls[0][0] == ["claude"]
    assert calls[1][0][0] == "codex"
    assert calls[1][0][-2:] == ["resume", "target-id"]
    assert calls[1][1]["cwd"] == str(tmp_path)


def test_supervisor_attempts_startup_flush(monkeypatch):
    calls = []
    monkeypatch.setattr(telemetry, "session_start_flush", lambda: calls.append(True))
    SessionSupervisor("codex", [], popen=lambda *_args, **_kwargs: None)
    assert calls == [True]


def test_supervisor_resumes_source_when_migration_fails(tmp_path):
    calls = []

    def fake_migrate(*args, **kwargs):
        raise MigrationError("fixture failure")

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        process = FakeProcess()
        if len(calls) == 1:
            control = Path(kwargs["env"][CONTROL_PATH_ENV])
            write_migration_request(
                str(control),
                control.with_name("token").read_text(encoding="utf-8"),
                str(tmp_path),
                "codex",
                "claude",
                "source-id",
            )
        else:
            process.returncode = 0
        return process

    supervisor = SessionSupervisor(
        "codex",
        [],
        popen=fake_popen,
        sleep=lambda _: None,
        temp_dir=tmp_path / "control",
        client_executables={"codex": "codex", "claude": "claude"},
        migrate=fake_migrate,
    )

    assert supervisor.run() == 0
    assert calls[0][0][0] == "codex"
    assert calls[1][0][0] == "codex"
    assert calls[1][0][-2:] == ["resume", "source-id"]
    assert calls[1][1]["cwd"] == str(tmp_path)
