import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from server.command_matrix import probe_command_matrix
from server.setup import install_setup


ROOT = Path(__file__).parents[1]


def install_fixture(tmp_path):
    home = tmp_path / "home"
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    executables = {}
    for client in ("codex", "claude"):
        executable = bin_dir / client
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        executables[client] = executable
    install_setup(
        ROOT,
        home,
        ["codex", "claude"],
        executable_paths=executables,
        runner=lambda _argv: None,
    )
    return home


def successful_runner(argv, **_kwargs):
    if "--version" in argv:
        return SimpleNamespace(returncode=0, stdout="test-client 1.0\n", stderr="")
    return SimpleNamespace(returncode=0, stdout="configured\n", stderr="")


def test_command_matrix_proves_all_four_provider_free_flows(tmp_path):
    home = install_fixture(tmp_path)

    result = probe_command_matrix(
        home,
        runner=successful_runner,
        migration_executable="session-migrate",
    )

    assert result["ready"] is True
    assert result["flows"] == {
        "claude_handoff": {"command": "/session-handoff", "ready": True},
        "codex_handoff": {"command": "$session-handoff", "ready": True},
        "claude_to_codex": {
            "command": "/session-handoff migrate codex",
            "ready": True,
        },
        "codex_to_claude": {
            "command": "$session-handoff migrate claude",
            "ready": True,
        },
    }
    assert str(tmp_path) not in json.dumps(result)


def test_command_matrix_fails_closed_when_one_mcp_registration_is_missing(tmp_path):
    home = install_fixture(tmp_path)

    def runner(argv, **kwargs):
        if Path(argv[0]).name.startswith("claude") and "mcp" in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="missing")
        return successful_runner(argv, **kwargs)

    result = probe_command_matrix(
        home,
        runner=runner,
        migration_executable="session-migrate",
    )

    assert result["ready"] is False
    assert result["clients"]["claude"]["mcp"] is False
    assert result["flows"]["claude_handoff"]["ready"] is False
    assert result["flows"]["codex_handoff"]["ready"] is True
    assert result["flows"]["claude_to_codex"]["ready"] is False
    assert result["flows"]["codex_to_claude"]["ready"] is False


def test_doctor_cli_emits_the_same_content_free_matrix(tmp_path):
    home = install_fixture(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin/session-handoff"),
            "doctor",
            "--home",
            str(home),
            "--migration-executable",
            "session-migrate",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ready"] is True
    assert payload["provider_calls"] == 0
    assert str(tmp_path) not in result.stdout
