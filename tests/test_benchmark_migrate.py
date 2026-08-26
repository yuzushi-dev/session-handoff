import hashlib
import json
from pathlib import Path

from benchmark.native_fixture import SESSION_ID, build_paginated_codex_home
from server.migration import migrate_session


def test_migrate_condition_executes_native_codex_to_claude_transfer(tmp_path, monkeypatch):
    source_home = tmp_path / "codex"
    source_rollout = build_paginated_codex_home(source_home)
    source_hash = hashlib.sha256(source_rollout.read_bytes()).hexdigest()
    source_database = source_home / "thread_history_1.sqlite"
    database_hash = hashlib.sha256(source_database.read_bytes()).hexdigest()
    target_home = tmp_path / "claude"
    target_id = "20000000-0000-4000-8000-000000000002"
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    result = migrate_session(
        "codex",
        "claude",
        SESSION_ID,
        str(tmp_path),
        target_session_id=target_id,
        target_home=str(target_home),
    )

    assert result["session_id"] == target_id
    assert result["warnings"]
    assert result["dropped_events"] == {"reasoning": 1}
    assert result["context_loss"]["dropped_events"] == {"reasoning": 1}
    assert result["context_loss"]["normalized_fields"]["commandExecution"] == [
        "exitCode",
        "status",
    ]
    assert result["context_loss"]["normalized_fields"]["mcpToolCall"]
    assert Path(result["manifest"]).is_file()
    output = Path(result["output"])
    assert output.is_file()
    messages = [json.loads(line)["message"] for line in output.read_text().splitlines()]
    assert [message["role"] for message in messages[:2]] == ["user", "assistant"]
    assert any(
        block.get("type") == "tool_use"
        for message in messages
        for block in (
            message.get("content", [])
            if isinstance(message.get("content"), list)
            else []
        )
    )
    assert isinstance(messages[0]["content"], list)
    assert messages[0]["content"][0]["text"] == "benchmark user request"
    assert {
        "type": "image",
        "source": {
            "type": "url",
            "url": "https://example.test/portable-remote-image-sentinel.png",
        },
    } in messages[0]["content"]
    assert messages[1]["content"][0]["text"] == "benchmark assistant response"
    target_text = output.read_text(encoding="utf-8")
    for expected in (
        "portable-file-change-sentinel",
        "portable-mcp-result-sentinel",
        "portable-mcp-resource-sentinel",
        "portable-collab-result-sentinel",
        "portable-agent-path-sentinel",
        "portable-collab-v2-result-sentinel",
        "portable-image-output-sentinel.png",
        "portable-web-result-sentinel",
        "portable-hook-prompt-sentinel",
        "portable-plan-sentinel",
        "portable-dynamic-result-sentinel",
        "portable-review-result-sentinel",
        "portable-remote-image-sentinel.png",
        "portable-local-image-sentinel.png",
        "portable-audio-sentinel.wav",
        "portable-local-audio-sentinel.wav",
        "portable-skill-sentinel",
        "portable-mention-sentinel",
        "exitCode",
    ):
        assert expected in target_text
    assert hashlib.sha256(source_rollout.read_bytes()).hexdigest() == source_hash
    assert hashlib.sha256(source_database.read_bytes()).hexdigest() == database_hash


def test_migrate_condition_executes_real_writer_in_both_directions(tmp_path, monkeypatch):
    source_home = tmp_path / "source-codex"
    source_rollout = build_paginated_codex_home(source_home)
    source_database = source_home / "thread_history_1.sqlite"
    source_hashes = {
        source_rollout: hashlib.sha256(source_rollout.read_bytes()).hexdigest(),
        source_database: hashlib.sha256(source_database.read_bytes()).hexdigest(),
    }
    claude_home = tmp_path / "target-claude"
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    forward = migrate_session(
        "codex",
        "claude",
        SESSION_ID,
        str(tmp_path),
        target_session_id="20000000-0000-4000-8000-000000000003",
        target_home=str(claude_home),
    )
    claude_output = Path(forward["output"])
    claude_hash = hashlib.sha256(claude_output.read_bytes()).hexdigest()

    reverse = migrate_session(
        "claude",
        "codex",
        forward["session_id"],
        str(tmp_path),
        source_home=str(claude_home),
        target_session_id="30000000-0000-4000-8000-000000000003",
        target_home=str(tmp_path / "roundtrip-codex"),
    )

    target = Path(reverse["output"])
    assert target.is_file()
    target_text = target.read_text(encoding="utf-8")
    for expected in (
        "benchmark user request",
        "benchmark assistant response",
        "pytest -q",
        "2 passed",
        "portable-file-change-sentinel",
        "portable-mcp-result-sentinel",
        "portable-mcp-resource-sentinel",
        "portable-collab-result-sentinel",
        "portable-agent-path-sentinel",
        "portable-collab-v2-result-sentinel",
        "portable-image-output-sentinel.png",
        "portable-web-result-sentinel",
        "portable-hook-prompt-sentinel",
        "portable-plan-sentinel",
        "portable-dynamic-result-sentinel",
        "portable-review-result-sentinel",
        "portable-remote-image-sentinel.png",
        "portable-local-image-sentinel.png",
        "portable-audio-sentinel.wav",
        "portable-local-audio-sentinel.wav",
        "portable-skill-sentinel",
        "portable-mention-sentinel",
        "exitCode",
    ):
        assert expected in target_text
    assert hashlib.sha256(claude_output.read_bytes()).hexdigest() == claude_hash
    for path, expected_hash in source_hashes.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash
