import json
import sys

from server.session_switch import (
    CONTROL_PATH_ENV,
    CONTROL_TOKEN_ENV,
    SessionSupervisor,
    write_switch_request,
)


def test_write_switch_request_is_consumed_by_supervisor(tmp_path):
    handoff = tmp_path / "handoffs" / "feature.md"
    handoff.parent.mkdir()
    handoff.write_text("## Goal\nContinue\n", encoding="utf-8")

    calls = []

    class FakeProcess:
        def __init__(self, argv):
            self.argv = argv
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 143

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = 137

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        process = FakeProcess(argv)
        if len(calls) == 1:
            write_switch_request(
                kwargs["env"][CONTROL_PATH_ENV],
                kwargs["env"][CONTROL_TOKEN_ENV],
                str(tmp_path),
                "handoffs/feature.md",
            )
        else:
            process.returncode = 0
        return process

    supervisor = SessionSupervisor(
        "claude",
        ["--plugin-dir", "/plugin"],
        popen=fake_popen,
        sleep=lambda _: None,
        temp_dir=tmp_path / "control",
        executable="claude",
    )

    assert supervisor.run() == 0
    assert calls[0][0] == ["claude", "--plugin-dir", "/plugin"]
    assert calls[1][0][:3] == ["claude", "--plugin-dir", "/plugin"]
    assert calls[1][0][-1] == "Resume from handoffs/feature.md in /" + str(tmp_path).lstrip("/") + "."


def test_write_switch_request_rejects_invalid_handoff(tmp_path):
    control = tmp_path / "request.json"
    token = "token"

    try:
        write_switch_request(str(control), token, str(tmp_path), "missing.md")
    except ValueError as exc:
        assert "handoff file" in str(exc)
    else:
        raise AssertionError("missing handoff must not trigger a session switch")


def test_supervisor_without_request_returns_child_status(tmp_path):
    calls = []

    class Process:
        def poll(self):
            return 7

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        return Process()

    supervisor = SessionSupervisor(
        "codex",
        [],
        popen=fake_popen,
        sleep=lambda _: None,
        temp_dir=tmp_path / "control",
        executable="codex",
    )

    assert supervisor.run() == 7
    assert calls == [["codex"]]


def test_supervisor_relaunches_a_real_fake_host(tmp_path):
    handoff = tmp_path / "handoffs" / "feature.md"
    handoff.parent.mkdir()
    handoff.write_text("## Goal\nContinue\n", encoding="utf-8")
    marker = tmp_path / "requested"
    runs = tmp_path / "runs.log"
    fake_host = tmp_path / "fake_host.py"
    fake_host.write_text(
        """import json
import os
import pathlib
import sys
import time

root = pathlib.Path(sys.argv[1])
marker = pathlib.Path(sys.argv[2])
runs = pathlib.Path(sys.argv[3])
runs.open("a", encoding="utf-8").write("run\\n")
if not marker.exists():
    pathlib.Path(os.environ["SESSION_HANDOFF_CONTROL"]).write_text(
        json.dumps({
            "token": os.environ["SESSION_HANDOFF_CONTROL_TOKEN"],
            "workspace": str(root),
            "path": "handoffs/feature.md",
        }),
        encoding="utf-8",
    )
    marker.write_text("requested", encoding="utf-8")
    while True:
        time.sleep(0.01)
""",
        encoding="utf-8",
    )

    supervisor = SessionSupervisor(
        "codex",
        [str(fake_host), str(tmp_path), str(marker), str(runs)],
        executable=sys.executable,
        temp_dir=tmp_path / "control",
        poll_interval=0.01,
    )

    assert supervisor.run() == 0
    assert runs.read_text(encoding="utf-8").splitlines() == ["run", "run"]
