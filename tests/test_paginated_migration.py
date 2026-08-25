import hashlib
import json
import sqlite3
from pathlib import Path

from benchmark.native_fixture import build_paginated_codex_home
from server.migration import migrate_session
from server.paginated_migration import project_paginated_codex


SESSION_ID = "10000000-0000-4000-8000-000000000001"


def _write_fixture(home: Path) -> Path:
    return build_paginated_codex_home(home, SESSION_ID)


def test_project_paginated_codex_reads_canonical_items_without_mutating_source(tmp_path):
    source_home = tmp_path / "codex"
    rollout = _write_fixture(source_home)
    before = hashlib.sha256(rollout.read_bytes()).hexdigest()
    database = source_home / "thread_history_1.sqlite"
    database_before = hashlib.sha256(database.read_bytes()).hexdigest()

    projection = project_paginated_codex(
        source_home,
        SESSION_ID,
        output_root=tmp_path / "projection",
    )

    records = [json.loads(line) for line in projection.rollout_path.read_text().splitlines()]
    assert records[0]["payload"]["history_mode"] == "legacy"
    assert records[0]["payload"]["id"] == SESSION_ID
    tool_calls = {
        record["payload"]["name"]: record["payload"]
        for record in records
        if record["payload"].get("type") == "function_call"
    }
    assert {
        "command_execution",
        "web_search",
        "codex_file_change",
        "codex_mcp_tool_call",
        "codex_collab_tool_call",
        "codex_subagent_activity",
        "codex_image_view",
        "codex_image_generation",
        "codex_context_compaction",
        "codex_dynamic_tool_call",
        "codex_entered_review",
        "codex_exited_review",
        "codex_hook_prompt",
        "codex_plan",
        "codex_sleep",
    } <= tool_calls.keys()
    projected_text = projection.rollout_path.read_text(encoding="utf-8")
    for sentinel in (
        "portable-file-change-sentinel",
        "portable-mcp-query-sentinel",
        "portable-mcp-result-sentinel",
        "portable-mcp-resource-sentinel",
        "portable-collab-prompt-sentinel",
        "portable-collab-result-sentinel",
        "portable-agent-path-sentinel",
        "portable-collab-v2-prompt-sentinel",
        "portable-collab-v2-result-sentinel",
        "portable-image-view-sentinel.png",
        "portable-image-prompt-sentinel",
        "portable-image-result-sentinel",
        "portable-image-output-sentinel.png",
        "portable-web-result-sentinel",
        "portable-hook-prompt-sentinel",
        "portable-plan-sentinel",
        "portable-dynamic-query-sentinel",
        "portable-dynamic-result-sentinel",
        "portable-review-target-sentinel",
        "portable-review-result-sentinel",
        "portable-remote-image-sentinel.png",
        "portable-local-image-sentinel.png",
        "portable-audio-sentinel.wav",
        "portable-local-audio-sentinel.wav",
        "portable-skill-sentinel",
        "portable-mention-sentinel",
    ):
        assert sentinel in projected_text
    assert projection.dropped == {"reasoning": 1}
    assert projection.normalized_fields["commandExecution"] == ["exitCode", "status"]
    assert {"arguments", "mcpAppResourceUri", "result", "status"} <= set(
        projection.normalized_fields["mcpToolCall"]
    )
    assert {"agentsStates", "prompt", "status"} <= set(
        projection.normalized_fields["collabAgentToolCall"]
    )
    assert {"agentStatus", "prompt", "status"} <= set(
        projection.normalized_fields["collabToolCall"]
    )
    assert hashlib.sha256(rollout.read_bytes()).hexdigest() == before
    assert hashlib.sha256(database.read_bytes()).hexdigest() == database_before


def test_project_paginated_codex_reports_unknown_items(tmp_path):
    source_home = tmp_path / "codex"
    _write_fixture(source_home)
    database = source_home / "thread_history_1.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            SESSION_ID,
            "turn-2",
            "future-1",
            999,
            1_756_112_500_000,
            json.dumps(
                {
                    "type": "futureThreadItem",
                    "id": "future-1",
                    "content": "must-not-be-copied-blindly",
                }
            ),
            "futureThreadItem",
            999,
        ),
    )
    connection.commit()
    connection.close()

    projection = project_paginated_codex(
        source_home,
        SESSION_ID,
        output_root=tmp_path / "projection",
    )

    assert projection.dropped["futureThreadItem"] == 1
    assert "must-not-be-copied-blindly" not in projection.rollout_path.read_text(
        encoding="utf-8"
    )


def test_migrate_session_uses_paginated_projection_and_real_claude_writer(tmp_path, monkeypatch):
    source_home = tmp_path / "codex"
    rollout = _write_fixture(source_home)
    before = hashlib.sha256(rollout.read_bytes()).hexdigest()
    target_home = tmp_path / "claude"
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    result = migrate_session(
        "codex",
        "claude",
        SESSION_ID,
        str(tmp_path),
        executable="session-migrate",
        target_session_id="20000000-0000-4000-8000-000000000001",
        target_home=str(target_home),
    )

    assert result["session_id"] == "20000000-0000-4000-8000-000000000001"
    assert result["dropped_events"] == {"reasoning": 1}
    assert result["context_loss"]["dropped_events"] == {"reasoning": 1}
    assert result["context_loss"]["normalized_fields"]["mcpToolCall"]
    assert Path(result["output"]).is_file()
    assert Path(result["manifest"]).is_file()
    messages = [json.loads(line)["message"] for line in Path(result["output"]).read_text().splitlines()]
    assert messages[0]["role"] == "user"
    assert isinstance(messages[0]["content"], list)
    assert messages[0]["content"][0]["text"] == "benchmark user request"
    assert {
        "type": "image",
        "source": {
            "type": "url",
            "url": "https://example.test/portable-remote-image-sentinel.png",
        },
    } in messages[0]["content"]
    assert messages[1]["role"] == "assistant"
    output_text = Path(result["output"]).read_text(encoding="utf-8")
    for sentinel in (
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
        assert sentinel in output_text
    assert hashlib.sha256(rollout.read_bytes()).hexdigest() == before
