import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from benchmark.native_fixture import build_paginated_codex_home
import server.migration_engine as migration_engine
import server.paginated_migration as paginated_migration
from server.migration import MigrationError, migrate_session
from server.paginated_migration import PaginatedMigrationError, project_paginated_codex


SESSION_ID = "10000000-0000-4000-8000-000000000001"


def _write_fixture(home: Path) -> Path:
    return build_paginated_codex_home(home, SESSION_ID)


def test_project_paginated_codex_reads_canonical_items_without_mutating_source(
    tmp_path,
):
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

    records = [
        json.loads(line) for line in projection.rollout_path.read_text().splitlines()
    ]
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
    assert projection.warnings == (
        {
            "code": "codex_paginated_projection",
            "message": "Codex canonical paginated items were projected into a temporary legacy view",
        },
        {
            "code": "client_private_state_not_migrated",
            "message": "Codex client-private state outside supported transcript items is not part of portable migration",
        },
    )
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


def test_paginated_tool_without_native_id_is_rejected(tmp_path):
    source_home = tmp_path / "codex"
    _write_fixture(source_home)
    database = source_home / "thread_history_1.sqlite"
    connection = sqlite3.connect(database)
    row = connection.execute(
        "SELECT item_json FROM thread_items WHERE item_type = 'commandExecution'"
    ).fetchone()
    item = json.loads(row[0])
    item.pop("id")
    connection.execute(
        "UPDATE thread_items SET item_json = ? WHERE item_type = 'commandExecution'",
        (json.dumps(item),),
    )
    connection.commit()
    connection.close()

    with pytest.raises(PaginatedMigrationError, match="tool item ID"):
        project_paginated_codex(
            source_home,
            SESSION_ID,
            output_root=tmp_path / "projection",
        )


def test_paginated_duplicate_tool_id_is_rejected(tmp_path):
    source_home = tmp_path / "codex"
    _write_fixture(source_home)
    database = source_home / "thread_history_1.sqlite"
    connection = sqlite3.connect(database)
    row = connection.execute(
        "SELECT item_json FROM thread_items WHERE item_type = 'webSearch'"
    ).fetchone()
    item = json.loads(row[0])
    item["id"] = "command-1"
    connection.execute(
        "UPDATE thread_items SET item_json = ? WHERE item_type = 'webSearch'",
        (json.dumps(item),),
    )
    connection.commit()
    connection.close()

    with pytest.raises(PaginatedMigrationError, match="duplicated"):
        project_paginated_codex(
            source_home,
            SESSION_ID,
            output_root=tmp_path / "projection",
        )


def test_paginated_projection_rejects_output_symlink_escape(tmp_path):
    source_home = tmp_path / "codex"
    _write_fixture(source_home)
    output_root = tmp_path / "projection"
    output_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (output_root / "sessions").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PaginatedMigrationError, match="outside the output root"):
        project_paginated_codex(source_home, SESSION_ID, output_root=output_root)

    assert not list(outside.rglob("*.jsonl"))


def test_paginated_projection_detects_rollout_change_and_cleans_owned_output(
    tmp_path, monkeypatch
):
    source_home = tmp_path / "codex"
    rollout = _write_fixture(source_home)
    real_write = paginated_migration._write_private
    projected: list[Path] = []

    def mutate_after_write(path, records):
        ownership = real_write(path, records)
        projected.append(path)
        rollout.write_bytes(rollout.read_bytes() + b"\n")
        return ownership

    monkeypatch.setattr(paginated_migration, "_write_private", mutate_after_write)

    with pytest.raises(PaginatedMigrationError, match="changed during projection"):
        project_paginated_codex(
            source_home,
            SESSION_ID,
            output_root=tmp_path / "projection",
        )

    assert projected and not projected[0].exists()


def test_migrate_session_uses_paginated_projection_and_real_claude_writer(
    tmp_path, monkeypatch
):
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
        target_session_id="20000000-0000-4000-8000-000000000001",
        target_home=str(target_home),
    )

    assert result["session_id"] == "20000000-0000-4000-8000-000000000001"
    assert result["dropped_events"] == {"reasoning": 1}
    assert result["context_loss"]["dropped_events"] == {"reasoning": 1}
    assert result["context_loss"]["normalized_fields"]["mcpToolCall"]
    assert Path(result["output"]).is_file()
    assert Path(result["manifest"]).is_file()
    messages = [
        json.loads(line)["message"]
        for line in Path(result["output"]).read_text().splitlines()
    ]
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


def test_paginated_manifest_persists_projection_losses_and_warnings(tmp_path):
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
            json.dumps({"type": "futureThreadItem", "id": "future-1"}),
            "futureThreadItem",
            999,
        ),
    )
    connection.commit()
    connection.close()

    result = migrate_session(
        "codex",
        "claude",
        SESSION_ID,
        str(tmp_path),
        source_home=str(source_home),
        target_home=str(tmp_path / "claude"),
        target_session_id="20000000-0000-4000-8000-000000000009",
    )

    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["dropped_events"] == {"futureThreadItem": 1, "reasoning": 1}
    assert manifest["dropped_events"] == result["dropped_events"]
    assert manifest["warnings"] == result["warnings"]
    assert manifest["context_loss"] == result["context_loss"]


def test_paginated_history_database_change_aborts_and_cleans_outputs(
    tmp_path, monkeypatch
):
    source_home = tmp_path / "codex"
    _write_fixture(source_home)
    database = source_home / "thread_history_1.sqlite"
    target_home = tmp_path / "claude"
    real_write_pair = migration_engine._write_pair

    def mutate_after_write(output, data, manifest, manifest_data):
        ownership = real_write_pair(output, data, manifest, manifest_data)
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE thread_items SET created_at_ms = created_at_ms + 1 WHERE item_id = 'user-1'"
        )
        connection.commit()
        connection.close()
        return ownership

    monkeypatch.setattr(migration_engine, "_write_pair", mutate_after_write)

    with pytest.raises(MigrationError, match="source session changed"):
        migrate_session(
            "codex",
            "claude",
            SESSION_ID,
            str(tmp_path),
            source_home=str(source_home),
            target_home=str(target_home),
            target_session_id="20000000-0000-4000-8000-000000000010",
        )

    assert not list(target_home.rglob("*.jsonl"))
    assert not list(target_home.rglob("*.json"))


def test_paginated_wal_change_is_detected_from_selected_item_digest(
    tmp_path, monkeypatch
):
    source_home = tmp_path / "codex"
    _write_fixture(source_home)
    database = source_home / "thread_history_1.sqlite"
    target_home = tmp_path / "claude"
    keeper = sqlite3.connect(database)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("PRAGMA wal_autocheckpoint=0")
    real_write_pair = migration_engine._write_pair

    def mutate_after_write(output, data, manifest, manifest_data):
        ownership = real_write_pair(output, data, manifest, manifest_data)
        keeper.execute(
            "UPDATE thread_items SET created_at_ms = created_at_ms + 1 WHERE item_id = 'user-1'"
        )
        keeper.commit()
        return ownership

    monkeypatch.setattr(migration_engine, "_write_pair", mutate_after_write)
    try:
        with pytest.raises(MigrationError, match="source session changed"):
            migrate_session(
                "codex",
                "claude",
                SESSION_ID,
                str(tmp_path),
                source_home=str(source_home),
                target_home=str(target_home),
                target_session_id="20000000-0000-4000-8000-000000000011",
            )
    finally:
        keeper.close()

    assert not list(target_home.rglob("*.jsonl"))
    assert not list(target_home.rglob("*.json"))
