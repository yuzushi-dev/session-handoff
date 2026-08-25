import hashlib
import json
from pathlib import Path

from benchmark.native_fixture import SESSION_ID, build_paginated_codex_home
from server.migration import migrate_session


def test_migrate_condition_executes_native_codex_to_claude_transfer(tmp_path, monkeypatch):
    source_home = tmp_path / "codex"
    source_rollout = build_paginated_codex_home(source_home)
    source_hash = hashlib.sha256(source_rollout.read_bytes()).hexdigest()
    target_home = tmp_path / "claude"
    target_id = "20000000-0000-4000-8000-000000000002"
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    result = migrate_session(
        "codex",
        "claude",
        SESSION_ID,
        str(tmp_path),
        executable="session-migrate",
        target_session_id=target_id,
        target_home=str(target_home),
    )

    assert result["session_id"] == target_id
    assert result["warnings"]
    assert result["dropped_events"] == {"reasoning": 1}
    assert result["context_loss"]["dropped_events"] == {"reasoning": 1}
    assert result["context_loss"]["normalized_fields"] == {
        "commandExecution": ["exitCode", "status"]
    }
    assert Path(result["manifest"]).is_file()
    output = Path(result["output"])
    assert output.is_file()
    messages = [json.loads(line)["message"] for line in output.read_text().splitlines()]
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert messages[0]["content"] == "benchmark user request"
    assert messages[1]["content"][0]["text"] == "benchmark assistant response"
    assert hashlib.sha256(source_rollout.read_bytes()).hexdigest() == source_hash
