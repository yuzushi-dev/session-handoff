import hashlib
import json
import os
from pathlib import Path

import pytest

from benchmark.real_session import count_thread_items, find_codex_rollout
from server.migration import migrate_session


def test_real_long_codex_session_migrates_without_mutating_source(tmp_path, monkeypatch):
    session_id = os.environ.get("SESSION_HANDOFF_REAL_CODEX_SESSION_ID")
    if not session_id:
        pytest.skip("set SESSION_HANDOFF_REAL_CODEX_SESSION_ID for a local real-session run")

    source_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    rollout = find_codex_rollout(source_home, session_id)
    source_hash = hashlib.sha256(rollout.read_bytes()).hexdigest()
    database = source_home / "thread_history_1.sqlite"
    database_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    source_items = count_thread_items(database, session_id)
    minimum_items = int(os.environ.get("SESSION_HANDOFF_REAL_MIN_ITEMS", "1000"))
    assert source_items >= minimum_items

    target_id = "40000000-0000-4000-8000-000000000001"
    target_home = tmp_path / "claude"
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    result = migrate_session(
        "codex",
        "claude",
        session_id,
        str(tmp_path),
        executable=os.environ.get("SESSION_HANDOFF_MIGRATOR", "session-migrate"),
        target_session_id=target_id,
        target_home=str(target_home),
    )

    assert result["session_id"] == target_id
    assert result["source_format"] == "codex"
    assert result["target_format"] == "claude"
    assert isinstance(result["warnings"], list)
    assert isinstance(result["dropped_events"], dict)
    assert set(result["dropped_events"]) <= {"reasoning"}
    assert isinstance(result["context_loss"], dict)
    assert Path(result["manifest"]).is_file()
    output = Path(result["output"])
    assert output.is_file() and output.stat().st_size > 0
    assert any(json.loads(line) for line in output.read_text().splitlines())
    assert hashlib.sha256(rollout.read_bytes()).hexdigest() == source_hash
    assert hashlib.sha256(database.read_bytes()).hexdigest() == database_hash
