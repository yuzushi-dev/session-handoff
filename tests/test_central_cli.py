import json
import os
import subprocess
import sys
from pathlib import Path

from server import handoff_store


ROOT = Path(__file__).parents[1]
CLI = ROOT / "bin/session-handoff"


def test_central_cli_exposes_bounded_list_and_read_help(tmp_path):
    listed = subprocess.run(
        [sys.executable, str(CLI), "list", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    read = subprocess.run(
        [sys.executable, str(CLI), "read", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert listed.returncode == 0
    assert read.returncode == 0
    assert "--workspace" in listed.stdout
    assert "--limit" in listed.stdout
    assert "--cursor" in listed.stdout
    assert "--scope" in listed.stdout
    assert "--json" in listed.stdout
    assert "--ref" in read.stdout
    assert "search" not in listed.stdout.lower()


def test_central_cli_limit_help_is_compact_and_rejects_out_of_range(tmp_path):
    listed = subprocess.run(
        [sys.executable, str(CLI), "list", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    too_small = subprocess.run(
        [sys.executable, str(CLI), "list", "--workspace", str(tmp_path), "--limit", "0"],
        text=True,
        capture_output=True,
        check=False,
    )
    too_large = subprocess.run(
        [sys.executable, str(CLI), "list", "--workspace", str(tmp_path), "--limit", "101"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert listed.returncode == 0
    assert "--limit LIMIT" in listed.stdout
    assert len(listed.stdout) < 3000
    for result in (too_small, too_large):
        assert result.returncode == 2
        assert "limit must be an integer between 1 and 100" in result.stderr


def test_central_cli_top_level_help_lists_management_commands():
    result = subprocess.run(
        [sys.executable, str(CLI), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    for command in ("setup", "doctor", "list", "read", "telemetry", "uninstall"):
        assert command in result.stdout


def test_central_cli_lists_and_reads_with_cursor_and_scope(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; foreign_workspace = tmp_path / "foreign"
    workspace.mkdir(); foreign_workspace.mkdir()
    data = tmp_path / "data"; state = tmp_path / "state"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    first = handoff_store.create_record(str(workspace), "first.md", "first content")
    second = handoff_store.create_record(str(foreign_workspace), "second.md", "second content")
    environment = {**os.environ, "XDG_DATA_HOME": str(data), "XDG_STATE_HOME": str(state)}

    page = subprocess.run(
        [sys.executable, str(CLI), "list", "--workspace", str(workspace), "--scope", "all", "--limit", "1", "--json"],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    first_page = json.loads(page.stdout)
    continuation = subprocess.run(
        [
            sys.executable, str(CLI), "list", "--workspace", str(workspace),
            "--scope", "all", "--limit", "1", "--cursor", first_page["next_cursor"], "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    read = subprocess.run(
        [sys.executable, str(CLI), "read", "--workspace", str(workspace), "--ref", first["ref"], "--json"],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert page.returncode == 0 and first_page["count"] == 1
    assert first_page["has_more"] is True
    assert continuation.returncode == 0
    assert json.loads(continuation.stdout)["items"][0]["ref"] == second["ref"]
    assert read.returncode == 0 and json.loads(read.stdout)["content"] == "first content"


def test_central_cli_read_list_do_not_create_unregistered_store(tmp_path):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    data = tmp_path / "data"; state = tmp_path / "state"
    environment = {**os.environ, "XDG_DATA_HOME": str(data), "XDG_STATE_HOME": str(state)}

    result = subprocess.run(
        [sys.executable, str(CLI), "list", "--workspace", str(workspace), "--json"],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["items"] == []
    assert not data.exists() and not state.exists()


def test_central_cli_foreign_read_explains_scope_and_rebind(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; foreign_workspace = tmp_path / "foreign"
    workspace.mkdir(); foreign_workspace.mkdir()
    data = tmp_path / "data"; state = tmp_path / "state"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    handoff_store.create_record(str(workspace), "local.md", "local")
    foreign = handoff_store.create_record(str(foreign_workspace), "foreign.md", "private-content")

    result = subprocess.run(
        [sys.executable, str(CLI), "read", "--workspace", str(workspace), "--ref", foreign["ref"]],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "XDG_DATA_HOME": str(data), "XDG_STATE_HOME": str(state)},
    )

    assert result.returncode == 1
    assert "scope='all'" in result.stderr
    assert "handoff_project" in result.stderr
    assert "private-content" not in result.stderr
