import json
import os
import shlex
import sys
import time
from pathlib import Path

from server.session_switch import (
    CONTROL_PATH_ENV,
    CONTROL_TOKEN_ENV,
    SessionSupervisor,
    _fresh_session_args,
    handoff_prompt,
    write_switch_request,
)


def test_handoff_prompt_is_a_manual_reference():
    assert handoff_prompt("/workspace", "handoffs/feature.md") == (
        "reference [handoffs/feature.md] riparti da qui"
    )


def test_draft_relaunch_removes_non_interactive_client_modes():
    assert _fresh_session_args(
        "codex",
        ["exec", "--ephemeral", "--json", "original prompt"],
        interactive=True,
    ) == []
    assert _fresh_session_args(
        "codex",
        ["exec", "review", "--uncommitted"],
        interactive=True,
    ) == []
    assert _fresh_session_args(
        "codex",
        ["review", "--uncommitted"],
        interactive=True,
    ) == []
    assert _fresh_session_args(
        "codex",
        ["fork", "session-id"],
        interactive=True,
    ) == []
    assert _fresh_session_args(
        "codex",
        ["--profile", "exec", "--cd", "e"],
        interactive=True,
    ) == ["--profile", "exec", "--cd", "e"]
    assert _fresh_session_args(
        "claude",
        ["-p", "--output-format", "json", "--json-schema", "schema.json", "original prompt"],
        interactive=True,
    ) == []


def test_supervisor_prefills_relaunch_without_submitting_prompt(tmp_path):
    handoff = tmp_path / "handoffs" / "feature.md"
    handoff.parent.mkdir()
    handoff.write_text("handoff", encoding="utf-8")
    result = tmp_path / "result.json"
    host = tmp_path / "fake_host.py"
    host.write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "import tty\n"
        "from pathlib import Path\n"
        "tty.setraw(sys.stdin.fileno())\n"
        "draft = os.read(sys.stdin.fileno(), 4096).decode()\n"
        "Path(sys.argv[1]).write_text(json.dumps({\n"
        "    'argv': sys.argv[1:],\n"
        "    'draft': draft,\n"
        "}), encoding='utf-8')\n",
        encoding="utf-8",
    )

    class InitialProcess:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 143

        def kill(self):
            pass

    def fake_popen(argv, **kwargs):
        control = Path(kwargs["env"][CONTROL_PATH_ENV])
        write_switch_request(
            str(control),
            control.with_name("token").read_text(encoding="utf-8"),
            str(tmp_path),
            "handoffs/feature.md",
        )
        return InitialProcess()

    supervisor = SessionSupervisor(
        "claude",
        [str(host), str(result)],
        popen=fake_popen,
        sleep=lambda seconds: time.sleep(seconds),
        temp_dir=tmp_path / "control",
        executable=sys.executable,
        draft=True,
    )

    assert supervisor.run() == 0
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["argv"] == [str(result)]
    assert payload["draft"] == "reference [handoffs/feature.md] riparti da qui"


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
                Path(kwargs["env"][CONTROL_PATH_ENV]).with_name("token").read_text(encoding="utf-8"),
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
        draft=False,
    )

    assert supervisor.run() == 0
    assert calls[0][0] == ["claude", "--plugin-dir", "/plugin"]
    assert calls[1][0][:3] == ["claude", "--plugin-dir", "/plugin"]
    assert calls[1][0][-1] == "reference [handoffs/feature.md] riparti da qui"


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
    assert calls[0][0] == "codex"
    assert calls[0][1] == "-c"
    assert "mcp_servers.session-handoff.env.SESSION_HANDOFF_CONTROL=" in calls[0][2]


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
            "token": pathlib.Path(os.environ["SESSION_HANDOFF_CONTROL"]).with_name("token").read_text(encoding="utf-8"),
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
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then shift 2; fi\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)

    supervisor = SessionSupervisor(
        "codex",
        [str(fake_host), str(tmp_path), str(marker), str(runs)],
        executable=str(fake_codex),
        temp_dir=tmp_path / "control",
        poll_interval=0.01,
    )

    assert supervisor.run() == 0
    assert runs.read_text(encoding="utf-8").splitlines() == ["run", "run"]


def test_supervisor_uses_a_token_file_instead_of_exporting_the_token(tmp_path):
    seen = []
    token_modes = []

    class Process:
        def poll(self):
            return 0

    def fake_popen(argv, **kwargs):
        seen.append(kwargs["env"])
        token_file = Path(kwargs["env"][CONTROL_PATH_ENV]).with_name("token")
        token_modes.append((token_file.read_text(encoding="utf-8"), os.stat(token_file).st_mode & 0o777))
        return Process()

    supervisor = SessionSupervisor(
        "codex",
        [],
        popen=fake_popen,
        temp_dir=tmp_path / "control",
        executable="codex",
    )

    assert supervisor.run() == 0
    env = seen[0]
    assert CONTROL_TOKEN_ENV not in env
    assert "SESSION_HANDOFF_CONTROL_TOKEN_FILE" not in env
    assert token_modes[0][0]
    assert token_modes[0][1] == 0o600


def test_codex_receives_control_path_as_mcp_config_override(tmp_path):
    seen = []

    class Process:
        def poll(self):
            return 0

    def fake_popen(argv, **kwargs):
        seen.append(argv)
        return Process()

    supervisor = SessionSupervisor(
        "codex",
        ["exec", "--ephemeral"],
        popen=fake_popen,
        temp_dir=tmp_path / "control",
        executable="codex",
    )

    assert supervisor.run() == 0
    assert seen[0][:2] == ["codex", "-c"]
    assert seen[0][2].startswith("mcp_servers.session-handoff.env.SESSION_HANDOFF_CONTROL=")
    assert str(tmp_path / "control" / "switch.json") in seen[0][2]
    assert seen[0][-2:] == ["exec", "--ephemeral"]


def test_supervisor_strips_resume_selectors_for_the_fresh_session(tmp_path):
    handoff = tmp_path / "handoff.md"
    handoff.write_text("handoff", encoding="utf-8")
    calls = []

    class Process:
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
        calls.append(argv)
        process = Process(argv)
        if len(calls) == 1:
            control = Path(kwargs["env"][CONTROL_PATH_ENV])
            token_file = Path(kwargs["env"][CONTROL_PATH_ENV]).with_name("token")
            control.write_text(
                json.dumps({
                    "token": token_file.read_text(encoding="utf-8"),
                    "workspace": str(tmp_path),
                    "path": handoff.name,
                }),
                encoding="utf-8",
            )
        else:
            process.returncode = 0
        return process

    supervisor = SessionSupervisor(
        "claude",
        ["--continue", "--session-id", "old", "--plugin-dir", "/plugin"],
        popen=fake_popen,
        sleep=lambda _: None,
        temp_dir=tmp_path / "control",
        executable="claude",
        draft=False,
    )

    assert supervisor.run() == 0
    assert calls[1] == [
        "claude",
        "--plugin-dir",
        "/plugin",
        "reference [handoff.md] riparti da qui",
    ]


def test_supervisor_replaces_codex_exec_prompt_on_relaunch(tmp_path):
    handoff = tmp_path / "handoff.md"
    handoff.write_text("handoff", encoding="utf-8")
    calls = []

    class Process:
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
        calls.append(argv)
        process = Process(argv)
        if len(calls) == 1:
            control = Path(kwargs["env"][CONTROL_PATH_ENV])
            write_switch_request(
                str(control),
                control.with_name("token").read_text(encoding="utf-8"),
                str(tmp_path),
                handoff.name,
            )
        else:
            process.returncode = 0
        return process

    supervisor = SessionSupervisor(
        "codex",
        ["exec", "--ephemeral", "original prompt"],
        popen=fake_popen,
        sleep=lambda _: None,
        temp_dir=tmp_path / "control",
        executable="codex",
        draft=False,
    )

    assert supervisor.run() == 0
    assert "original prompt" not in calls[1]
    assert calls[1][-1] == "reference [handoff.md] riparti da qui"


def test_supervisor_replaces_claude_print_prompt_on_relaunch(tmp_path):
    handoff = tmp_path / "handoff.md"
    handoff.write_text("handoff", encoding="utf-8")
    calls = []

    class Process:
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
        calls.append(argv)
        process = Process(argv)
        if len(calls) == 1:
            control = Path(kwargs["env"][CONTROL_PATH_ENV])
            write_switch_request(
                str(control),
                control.with_name("token").read_text(encoding="utf-8"),
                str(tmp_path),
                handoff.name,
            )
        else:
            process.returncode = 0
        return process

    supervisor = SessionSupervisor(
        "claude",
        ["-p", "--verbose", "original prompt"],
        popen=fake_popen,
        sleep=lambda _: None,
        temp_dir=tmp_path / "control",
        executable="claude",
        draft=False,
    )

    assert supervisor.run() == 0
    assert "original prompt" not in calls[1]
    assert calls[1][-1] == "reference [handoff.md] riparti da qui"
