import hashlib
import json
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


def test_migrate_session_is_self_contained_and_does_not_invoke_external_backend(tmp_path):
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

    def external_backend_must_not_run(*_args, **_kwargs):
        raise AssertionError("migrate must not invoke an external backend")

    result = migrate_session(
        "claude",
        "codex",
        "50000000-0000-4000-8000-000000000010",
        str(workspace),
        executable="does-not-exist",
        source_home=str(source_home),
        target_session_id="60000000-0000-4000-8000-000000000010",
        target_home=str(tmp_path / "codex"),
        runner=external_backend_must_not_run,
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
            executable="smigrate",
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
