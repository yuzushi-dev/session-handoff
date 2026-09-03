import json
from io import StringIO
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from server import checkpoint
from server.checkpoint import (
    CheckpointError,
    _atomic_write,
    capture_checkpoint,
    compact_context,
    main,
)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("checkpoint test\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "initial",
        ],
        check=True,
    )
    (repo / "README.md").write_text("api_key=sk-1234567890\n", encoding="utf-8")
    return repo


def event(repo: Path, *, trigger: str = "auto") -> dict[str, object]:
    return {
        "cwd": str(repo),
        "session_id": "session-123",
        "hook_event_name": "PreCompact",
        "trigger": trigger,
        "transcript_path": str(repo / "transcript.jsonl"),
        "model": "test-model",
    }


def test_git_collection_uses_one_budget_for_all_commands(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    time_elapsed = [0.0]
    timeouts = []

    def monotonic():
        return time_elapsed[0]

    def run(command, *, cwd, text, capture_output, check, timeout):
        timeouts.append(timeout)
        time_elapsed[0] += timeout
        stdout = f"{repo}\n" if command[1:] == ["rev-parse", "--show-toplevel"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(time, "monotonic", monotonic)
    monkeypatch.setattr(checkpoint.subprocess, "run", run)

    checkpoint._git_state(repo)

    assert timeouts
    assert sum(timeouts) < 5
    assert max(timeouts) <= checkpoint.GIT_TIMEOUT_SECONDS


def test_precompact_shares_git_budget_with_lifecycle_record(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    home = tmp_path / "home"
    time_elapsed = [0.0]
    timeouts = []

    def monotonic():
        return time_elapsed[0]

    def run(command, *, cwd, text, capture_output, check, timeout):
        timeouts.append(timeout)
        time_elapsed[0] += timeout
        stdout = f"{repo}\n" if command[1:] == ["rev-parse", "--show-toplevel"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(time, "monotonic", monotonic)
    monkeypatch.setattr(checkpoint.subprocess, "run", run)

    assert main(
        stdin_text=json.dumps(event(repo)),
        home=home,
        stdout=StringIO(),
        stderr=StringIO(),
    ) == 0

    assert sum(timeouts) < 5


def test_atomic_write_syncs_parent_directory_after_replace(tmp_path, monkeypatch):
    path = tmp_path / "checkpoint.md"
    events = []
    real_fsync = os.fsync
    real_replace = os.replace

    def fsync(descriptor):
        events.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        return real_fsync(descriptor)

    def replace(source, destination):
        events.append("replace")
        return real_replace(source, destination)

    monkeypatch.setattr(checkpoint.os, "fsync", fsync)
    monkeypatch.setattr(checkpoint.os, "replace", replace)

    _atomic_write(path, "checkpoint\n")

    assert events == ["file", "replace", "directory"]


def test_capture_writes_canonical_secret_safe_checkpoint_and_latest_pointer(tmp_path):
    repo = make_repo(tmp_path)
    home = tmp_path / "home"

    result = capture_checkpoint({**event(repo), "model": "api_key=sk-1234567890"}, home=home)

    checkpoint = Path(result["path"])
    content = checkpoint.read_text(encoding="utf-8")
    assert checkpoint.is_file()
    assert result["trigger"] == "auto"
    assert str(repo) in content
    assert "## Goal" in content
    assert "## Critical Context" in content
    assert "## Files Observed" in content
    assert "git status --short --branch" in content
    assert "git log -1 --format=%s" in content
    assert "initial" in content
    assert " M README.md" in content
    assert "Tool summary: unavailable from lifecycle hook." in content
    assert "Plan/TODO cursor: unavailable from lifecycle hook." in content
    assert "sk-1234567890" not in content
    assert "api_key=[REDACTED]" in content
    pointer = json.loads(Path(result["latest_path"]).read_text(encoding="utf-8"))
    assert pointer["path"] == str(checkpoint)
    assert not list(checkpoint.parent.glob("*.tmp"))


def test_checkpoint_preserves_only_bounded_explicit_goal(tmp_path):
    repo = make_repo(tmp_path)
    home = tmp_path / "home"
    goal = "Fix the API without changing its signature. " + "x" * 5000

    result = capture_checkpoint({**event(repo), "goal": goal}, home=home)

    content = Path(result["path"]).read_text(encoding="utf-8")
    assert "Fix the API without changing its signature." in content
    assert len(content) < 20_000
    assert "x" * 5000 not in content


def test_precompact_records_secret_free_lifecycle_event(tmp_path):
    repo = make_repo(tmp_path)
    home = tmp_path / "home"
    stdout = StringIO()
    stderr = StringIO()

    assert main(
        stdin_text=json.dumps({**event(repo), "goal": "api_key=sk-1234567890"}),
        home=home, stdout=stdout, stderr=stderr,
    ) == 0

    checkpoint_dir = next((home / ".local/state/session-handoff/checkpoints").iterdir())
    record = json.loads((checkpoint_dir / "events.jsonl").read_text(encoding="utf-8"))
    assert record["event"] == "precompact"
    assert record["session_id"] == "session-123"
    assert record["trigger"] == "auto"
    assert record["checkpoint_path"].endswith("session-123.md")
    assert record["checkpoint_bytes"] > 0
    assert record["injected"] is False
    assert record["injected_bytes"] == 0
    assert "sk-1234567890" not in (checkpoint_dir / "events.jsonl").read_text()


def test_compact_context_returns_short_pointer_for_matching_workspace(tmp_path):
    repo = make_repo(tmp_path)
    home = tmp_path / "home"
    result = capture_checkpoint(event(repo), home=home)

    context = compact_context({"cwd": str(repo), "source": "compact", "session_id": "next"}, home=home)

    assert context is not None
    assert str(result["path"]) in context
    assert "non-semantic" in context
    assert len(context) < 1000


def test_invalid_event_is_rejected_without_guessing(tmp_path):
    with pytest.raises(CheckpointError, match="cwd"):
        capture_checkpoint({"session_id": "session-123"}, home=tmp_path)


def test_hook_main_fails_open_and_emits_empty_json_on_invalid_input(tmp_path, capsys):
    result = main(
        stdin_text=json.dumps({"hook_event_name": "PreCompact", "cwd": ""}),
        home=tmp_path,
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {}
