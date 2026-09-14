import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import server.command_matrix as command_matrix
from server.command_matrix import probe_command_matrix
from server import handoff_store
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


def test_command_matrix_supports_direct_module_help():
    result = subprocess.run(
        [sys.executable, str(ROOT / "server/command_matrix.py"), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Check handoff and migrate readiness" in result.stdout


def test_doctor_reports_absent_central_store_without_creating_it(tmp_path, monkeypatch):
    data_home = tmp_path / "xdg-data"; state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    result = probe_command_matrix(tmp_path / "home", runner=successful_runner)

    health = result["central_store"]
    assert health["status"] == "absent"
    assert health["data_root"]["status"] == "absent"
    assert health["state_root"]["status"] == "absent"
    assert health["catalog"]["status"] == "absent"
    assert health["data_root"]["writable"] is True
    assert health["state_root"]["writable"] is True
    assert not data_home.exists() and not state_home.exists()


def test_doctor_reports_healthy_central_store_and_catalog(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    data_home = tmp_path / "xdg-data"; state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    record = handoff_store.create_record(str(workspace), "one.md", "one")
    handoff_store.list_records(str(workspace))

    health = command_matrix.probe_central_store()

    assert health["status"] == "healthy"
    assert health["data_root"]["status"] == "healthy"
    assert health["state_root"]["status"] == "healthy"
    assert health["catalog"]["status"] == "healthy"
    assert health["catalog"]["path"].endswith("catalog.sqlite3")
    assert record["project_id"]


def test_doctor_distinguishes_unsafe_root_and_corrupt_catalog(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    data_home = tmp_path / "xdg-data"; state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    handoff_store.create_record(str(workspace), "one.md", "one")
    handoff_store.list_records(str(workspace))

    data_root = handoff_store.data_root()
    data_root.chmod(0o777)
    unsafe = command_matrix.probe_central_store()
    data_root.chmod(0o700)
    handoff_store.catalog_path().write_bytes(b"not sqlite")

    corrupt = command_matrix.probe_central_store()

    assert unsafe["data_root"]["status"] == "unsafe"
    assert unsafe["status"] == "unsafe"
    assert corrupt["catalog"]["status"] == "corrupt"
    assert corrupt["status"] == "corrupt"


@pytest.mark.parametrize(
    ("status", "writable", "ready"),
    [
        ("absent", True, True),
        ("healthy", True, True),
        ("unsafe", False, False),
        ("corrupt", False, False),
        ("unwritable", False, False),
        ("healthy", False, False),
    ],
)
def test_doctor_readiness_includes_central_health(tmp_path, monkeypatch, status, writable, ready):
    home = install_fixture(tmp_path)
    monkeypatch.setattr(
        command_matrix,
        "probe_central_store",
        lambda: {"status": status, "writable": writable},
    )

    result = probe_command_matrix(home, runner=successful_runner)

    assert result["ready"] is ready


def test_doctor_reports_read_only_catalog_as_unwritable(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    data_home = tmp_path / "xdg-data"; state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    handoff_store.create_record(str(workspace), "one.md", "one")
    handoff_store.list_records(str(workspace))
    catalog = handoff_store.catalog_path()
    catalog.chmod(0o400)

    health = command_matrix.probe_central_store()

    assert health["catalog"]["status"] == "unwritable"
    assert health["catalog"]["writable"] is False
    assert health["status"] == "unwritable"
    assert health["writable"] is False


def test_doctor_human_mode_is_opt_in_and_keeps_json_default(tmp_path):
    human = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin/session-handoff"),
            "doctor",
            "--home",
            str(tmp_path / "home"),
            "--human",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert human.stdout.startswith("Session-handoff doctor:")
    assert "central store:" in human.stdout
