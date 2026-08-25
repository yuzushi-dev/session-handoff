import hashlib
import json
import sqlite3
from pathlib import Path

from server.migration import migrate_session
from server.paginated_migration import project_paginated_codex


SESSION_ID = "10000000-0000-4000-8000-000000000001"


def _write_fixture(home: Path) -> Path:
    rollout = home / "sessions" / "2026" / "08" / "25" / f"rollout-test-{SESSION_ID}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-25T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": SESSION_ID,
                    "session_id": SESSION_ID,
                    "timestamp": "2026-08-25T10:00:00Z",
                    "cwd": "/work/project",
                    "cli_version": "0.149.1",
                    "history_mode": "paginated",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    db = home / "thread_history_1.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE thread_items (
            thread_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            rollout_ordinal INTEGER NOT NULL,
            created_at_ms INTEGER,
            item_json TEXT NOT NULL,
            item_type TEXT NOT NULL,
            updated_at_ordinal INTEGER
        );
        """
    )
    items = [
        (
            "user-1",
            "userMessage",
            {"type": "userMessage", "id": "user-1", "content": [{"type": "text", "text": "hello"}]},
        ),
        (
            "agent-1",
            "agentMessage",
            {"type": "agentMessage", "id": "agent-1", "text": "hi"},
        ),
        (
            "command-1",
            "commandExecution",
            {
                "type": "commandExecution",
                "id": "command-1",
                "command": "ls",
                "cwd": "/work/project",
                "aggregatedOutput": "file.txt",
                "exitCode": 0,
                "status": "completed",
            },
        ),
        (
            "reasoning-1",
            "reasoning",
            {"type": "reasoning", "id": "reasoning-1", "summary": ["private"]},
        ),
        (
            "search-1",
            "webSearch",
            {"type": "webSearch", "id": "search-1", "query": "session migration", "results": []},
        ),
    ]
    for ordinal, (item_id, item_type, item) in enumerate(items):
        con.execute(
            "INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (SESSION_ID, "turn-1", item_id, ordinal, 1_756_112_400_000 + ordinal, json.dumps(item), item_type, ordinal),
        )
    con.commit()
    con.close()
    return rollout


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
    assert [record["payload"].get("type") for record in records[1:]] == [
        "message",
        "message",
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
    ]
    assert projection.dropped == {"reasoning": 1}
    assert projection.normalized_fields == {"commandExecution": ["exitCode", "status"]}
    assert hashlib.sha256(rollout.read_bytes()).hexdigest() == before
    assert hashlib.sha256(database.read_bytes()).hexdigest() == database_before


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
    assert result["context_loss"] == {
        "dropped_events": {"reasoning": 1},
        "normalized_fields": {"commandExecution": ["exitCode", "status"]},
    }
    assert Path(result["output"]).is_file()
    assert Path(result["manifest"]).is_file()
    messages = [json.loads(line)["message"] for line in Path(result["output"]).read_text().splitlines()]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "hello"
    assert messages[1]["role"] == "assistant"
    assert messages[2]["content"][0]["type"] == "tool_use"
    assert messages[-1]["content"][0]["type"] == "tool_result"
    assert hashlib.sha256(rollout.read_bytes()).hexdigest() == before
