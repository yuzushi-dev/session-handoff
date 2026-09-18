import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from benchmark.native_seed import seed_native_session
import server.migration_engine as migration_engine
from server.migration import (
    MigrationError,
    _normalize_codex_target,
    migrate_session,
    migration_telemetry_summary,
)


def test_internal_writer_removes_partial_target_after_write_failure(tmp_path, monkeypatch):
    output = tmp_path / "target/session.jsonl"
    manifest = tmp_path / "manifest/result.json"
    real_fdopen = migration_engine.os.fdopen

    class FailingHandle:
        def __init__(self, descriptor, mode):
            self.handle = real_fdopen(descriptor, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def write(self, _content):
            raise OSError("simulated write failure")

    monkeypatch.setattr(migration_engine.os, "fdopen", FailingHandle)

    with pytest.raises(OSError, match="simulated"):
        migration_engine._write_pair(output, b"target", manifest, b"manifest")

    assert not output.exists()
    assert not manifest.exists()


def test_migrate_session_uses_the_internal_engine(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_home = tmp_path / "claude"
    seed_native_session(
        "claude",
        source_home,
        "50000000-0000-4000-8000-000000000010",
        workspace,
        "bundled migration marker",
    )

    result = migrate_session(
        "claude",
        "codex",
        "50000000-0000-4000-8000-000000000010",
        str(workspace),
        source_home=str(source_home),
        target_session_id="60000000-0000-4000-8000-000000000010",
        target_home=str(tmp_path / "codex"),
    )

    assert result["source_format"] == "claude"
    assert result["target_format"] == "codex"
    assert "bundled migration marker" in Path(result["output"]).read_text(encoding="utf-8")


def _write_codex_target(tmp_path, records):
    output = tmp_path / "rollout.jsonl"
    output.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "target": {
                    "session_id": "target-id",
                    "path": str(output),
                    "records": len(records),
                    "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                }
            }
        ),
        encoding="utf-8",
    )
    return output, manifest


def test_codex_target_normalization_removes_adjacent_duplicate_and_rehashes(tmp_path):
    records = [
        {
            "type": "session_meta",
            "payload": {"id": "target-id", "session_id": "target-id"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "fixture"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "fixture"}],
            },
        },
    ]
    output, manifest_path = _write_codex_target(tmp_path, records)

    removed = _normalize_codex_target(
        {"output": str(output), "manifest": str(manifest_path)},
        target_home=tmp_path,
        target_id="target-id",
    )

    normalized = [json.loads(line) for line in output.read_text().splitlines()]
    manifest = json.loads(manifest_path.read_text())
    assert removed == 1
    assert [record["type"] for record in normalized] == ["session_meta", "response_item"]
    assert manifest["target"]["records"] == len(normalized)
    assert manifest["target"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


def test_codex_target_normalization_rejects_non_object_records(tmp_path):
    output, manifest = _write_codex_target(
        tmp_path,
        [
            {
                "type": "session_meta",
                "payload": {"id": "target-id", "session_id": "target-id"},
            },
            "invalid",
        ],
    )

    with pytest.raises(MigrationError, match="record"):
        _normalize_codex_target(
            {"output": str(output), "manifest": str(manifest)},
            target_home=tmp_path,
            target_id="target-id",
        )


def test_codex_target_normalization_rejects_manifest_checksum_mismatch(tmp_path):
    output = tmp_path / "rollout.jsonl"
    records = [
        {
            "type": "session_meta",
            "payload": {"id": "target-id", "session_id": "target-id"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "fixture"}],
            },
        },
    ]
    output.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "target": {
                    "session_id": "target-id",
                    "path": str(output),
                    "records": len(records),
                    "sha256": "incorrect",
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MigrationError, match="checksum"):
        _normalize_codex_target(
            {"output": str(output), "manifest": str(manifest)},
            target_home=tmp_path,
            target_id="target-id",
        )


def test_migrate_session_refuses_existing_target_without_overwrite(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_home = tmp_path / "claude"
    source_id = "50000000-0000-4000-8000-000000000011"
    target_id = "60000000-0000-4000-8000-000000000011"
    seed_native_session("claude", source_home, source_id, workspace, "collision marker")

    migrate_session(
        "claude",
        "codex",
        source_id,
        str(workspace),
        source_home=str(source_home),
        target_session_id=target_id,
        target_home=str(tmp_path / "codex"),
    )

    with pytest.raises(MigrationError, match="existing target"):
        migrate_session(
            "claude",
            "codex",
            source_id,
            str(workspace),
            source_home=str(source_home),
            target_session_id=target_id,
            target_home=str(tmp_path / "codex"),
        )


def test_migrate_session_rejects_same_client(tmp_path):
    with pytest.raises(MigrationError, match="different target"):
        migrate_session(
            "codex",
            "codex",
            "source-id",
            str(tmp_path),
        )


def test_migration_telemetry_summary_normalizes_loss_to_numeric_counts():
    summary = migration_telemetry_summary(
        {
            "dropped_events": {"reasoning": 2, "tool_result": 1, "session-id": "ignored"},
            "context_loss": {
                "normalized_fields": {
                    "codexTarget": ["deduplicated_user_events", "other"],
                    "source": ["safe"],
                }
            },
            "session_id": "must-not-escape",
            "output": "/sensitive/path",
        }
    )

    assert summary == {"dropped_events": 3, "normalized_fields": 3}


def test_parse_transcript_auto_detects_claude_format():
    records = [
        {"type": "user", "sessionId": "sess-1", "uuid": "u1", "message": {"role": "user", "content": "go"}},
    ]
    metadata, events, dropped = migration_engine._parse_transcript_auto(records, "sess-1")
    assert events == [{"kind": "text", "role": "user", "text": "go", "timestamp": None}]


def test_parse_transcript_auto_detects_codex_format():
    records = [
        {"type": "session_meta", "payload": {"id": "sess-1"}},
        {
            "type": "response_item", "timestamp": None,
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
        },
    ]
    metadata, events, dropped = migration_engine._parse_transcript_auto(records, "sess-1")
    assert events == [{"kind": "text", "role": "user", "text": "go", "timestamp": None}]


def test_parse_transcript_auto_empty_records_defaults_to_claude_parser_no_crash():
    # Empty input never reaches here in the real pipeline (_read_jsonl
    # raises "session is empty" first), but the dispatch itself must not
    # IndexError on records[0] when given an empty list directly.
    metadata, events, dropped = migration_engine._parse_transcript_auto([], "sess-1")
    assert events == []


def _write_paginated_codex_fixture(source_home: Path, session_id: str) -> Path:
    rollout_dir = source_home / "sessions" / "2026" / "01" / "01"
    rollout_dir.mkdir(parents=True)
    rollout_path = rollout_dir / f"rollout-test-{session_id}.jsonl"
    rollout_path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": session_id, "history_mode": "paginated"}}) + "\n",
        encoding="utf-8",
    )
    db_path = source_home / "thread_history_1.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE thread_items ("
        "item_id INTEGER, thread_id TEXT, rollout_ordinal INTEGER, created_at_ms INTEGER, item_json TEXT)"
    )
    conn.execute(
        "INSERT INTO thread_items VALUES (?, ?, ?, ?, ?)",
        (1, session_id, 1, None, json.dumps({"type": "userMessage", "content": "go"})),
    )
    conn.commit()
    conn.close()
    return rollout_path


def test_parse_transcript_file_projects_paginated_codex_history(tmp_path, monkeypatch):
    # Regression: real Codex sessions on a checked machine were all
    # history_mode "paginated" (SQLite-backed), not "legacy" - the plain
    # _parse_transcript_auto dispatch (records-only) can't handle this, it
    # needs the file's location to find the sibling thread_history_1.sqlite.
    source_home = tmp_path / "codex-home"
    source_home.mkdir()
    session_id = "11111111-1111-1111-1111-111111111111"
    rollout_path = _write_paginated_codex_fixture(source_home, session_id)
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    metadata, events, dropped = migration_engine._parse_transcript_file(str(rollout_path), session_id)
    assert any(event["kind"] == "text" and event.get("text") == "go" for event in events)


def test_parse_transcript_file_claude_format_unaffected(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    records = [
        {"type": "user", "sessionId": "sess-1", "uuid": "u1", "message": {"role": "user", "content": "go"}},
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    metadata, events, dropped = migration_engine._parse_transcript_file(str(transcript), "sess-1")
    assert events == [{"kind": "text", "role": "user", "text": "go", "timestamp": None}]
