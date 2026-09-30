import hashlib
import json
import stat
from pathlib import Path

import pytest

import server.migration_engine as migration_engine
from server.migration import MigrationError, migrate_session


SOURCE_ID = "71000000-0000-4000-8000-000000000001"
TARGET_ID = "72000000-0000-4000-8000-000000000001"
TIMESTAMP = "2026-09-30T10:00:00Z"


def _write_claude_source(home: Path, records: list[dict]) -> Path:
    source = home / "projects" / "fixture" / f"{SOURCE_ID}.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return source


def _claude_record(
    record_id: str,
    content,
    *,
    role: str = "user",
    parent_id: str | None = None,
) -> dict:
    return {
        "type": role,
        "sessionId": SOURCE_ID,
        "uuid": record_id,
        "parentUuid": parent_id,
        "timestamp": TIMESTAMP,
        "message": {"role": role, "content": content},
    }


def _migrate_claude(tmp_path: Path, source_home: Path, target_home: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return migrate_session(
        "claude",
        "codex",
        SOURCE_ID,
        str(workspace),
        source_home=str(source_home),
        target_home=str(target_home),
        target_session_id=TARGET_ID,
    )


def test_unknown_codex_record_is_omitted_and_declared_in_hashed_manifest(tmp_path):
    source = tmp_path / "source.jsonl"
    source_records = [
        {
            "type": "session_meta",
            "timestamp": TIMESTAMP,
            "payload": {"id": SOURCE_ID, "timestamp": TIMESTAMP},
        },
        {
            "type": "response_item",
            "timestamp": TIMESTAMP,
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "portable"}],
            },
        },
        {
            "type": "future_native_event",
            "timestamp": TIMESTAMP,
            "payload": {"secret_shape": "must-not-be-copied-blindly"},
        },
    ]
    source.write_text(
        "\n".join(json.dumps(record) for record in source_records) + "\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = migration_engine.convert_native_session(
        source,
        "codex",
        "claude",
        SOURCE_ID,
        TARGET_ID,
        workspace,
        tmp_path / "claude",
    )

    output = Path(result["output"])
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert "must-not-be-copied-blindly" not in output.read_text(encoding="utf-8")
    assert manifest["dropped_events"] == {"future_native_event": 1}
    assert (
        manifest["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert (
        manifest["target"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    )


def test_unknown_claude_record_is_omitted_and_declared(tmp_path):
    source_home = tmp_path / "claude"
    _write_claude_source(
        source_home,
        [
            _claude_record("record-1", "portable"),
            {
                "type": "future-native-event",
                "sessionId": SOURCE_ID,
                "payload": "must-not-be-copied-blindly",
            },
        ],
    )

    result = _migrate_claude(tmp_path, source_home, tmp_path / "codex")

    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["dropped_events"] == {"future-native-event": 1}
    assert "must-not-be-copied-blindly" not in Path(result["output"]).read_text(
        encoding="utf-8"
    )


def test_missing_tool_call_id_refuses_migration_instead_of_inventing_one(tmp_path):
    source_home = tmp_path / "claude"
    _write_claude_source(
        source_home,
        [
            _claude_record(
                "record-1",
                [{"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}}],
                role="assistant",
            )
        ],
    )

    with pytest.raises(MigrationError, match="tool call ID"):
        _migrate_claude(tmp_path, source_home, tmp_path / "codex")


def test_duplicate_claude_record_uuid_is_rejected(tmp_path):
    source_home = tmp_path / "claude"
    _write_claude_source(
        source_home,
        [
            _claude_record("duplicate", "first"),
            _claude_record("duplicate", "second"),
        ],
    )

    with pytest.raises(MigrationError, match="duplicate record UUID"):
        _migrate_claude(tmp_path, source_home, tmp_path / "codex")


def test_claude_message_without_uuid_is_rejected(tmp_path):
    source_home = tmp_path / "claude"
    record = _claude_record("record-1", "hello")
    record.pop("uuid")
    _write_claude_source(source_home, [record])

    with pytest.raises(MigrationError, match="message record UUID"):
        _migrate_claude(tmp_path, source_home, tmp_path / "codex")


def test_claude_last_prompt_with_dangling_leaf_is_rejected(tmp_path):
    source_home = tmp_path / "claude"
    _write_claude_source(
        source_home,
        [
            _claude_record("record-1", "hello"),
            {"type": "last-prompt", "leafUuid": "missing-record"},
        ],
    )

    with pytest.raises(MigrationError, match="leaf UUID"):
        _migrate_claude(tmp_path, source_home, tmp_path / "codex")


def test_claude_last_prompt_without_leaf_uuid_is_rejected(tmp_path):
    source_home = tmp_path / "claude"
    _write_claude_source(
        source_home,
        [
            _claude_record("record-1", "hello"),
            {"type": "last-prompt"},
        ],
    )

    with pytest.raises(MigrationError, match="leaf UUID"):
        _migrate_claude(tmp_path, source_home, tmp_path / "codex")


def test_claude_malformed_parent_uuid_is_rejected(tmp_path):
    source_home = tmp_path / "claude"
    record = _claude_record("record-1", "hello")
    record["parentUuid"] = {"invalid": "parent"}
    _write_claude_source(source_home, [record])

    with pytest.raises(MigrationError, match="parent UUID"):
        _migrate_claude(tmp_path, source_home, tmp_path / "codex")


@pytest.mark.parametrize("target_id", ["", 123])
def test_invalid_explicit_target_session_id_is_rejected(tmp_path, target_id):
    source_home = tmp_path / "claude"
    _write_claude_source(source_home, [_claude_record("record-1", "hello")])
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(MigrationError, match="target session id must be a valid UUID"):
        migrate_session(
            "claude",
            "codex",
            SOURCE_ID,
            str(workspace),
            source_home=str(source_home),
            target_home=str(tmp_path / "codex"),
            target_session_id=target_id,
        )


def test_duplicate_tool_call_id_is_rejected(tmp_path):
    source_home = tmp_path / "claude"
    _write_claude_source(
        source_home,
        [
            _claude_record(
                "record-1",
                [
                    {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {}},
                    {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {}},
                ],
                role="assistant",
            )
        ],
    )

    with pytest.raises(MigrationError, match="tool call ID is duplicated"):
        _migrate_claude(tmp_path, source_home, tmp_path / "codex")


def test_unmatched_tool_result_is_rejected(tmp_path):
    source_home = tmp_path / "claude"
    _write_claude_source(
        source_home,
        [
            _claude_record(
                "record-1",
                [{"type": "tool_result", "tool_use_id": "unknown", "content": "no"}],
            )
        ],
    )

    with pytest.raises(MigrationError, match="unknown tool call ID"):
        _migrate_claude(tmp_path, source_home, tmp_path / "codex")


def test_tool_result_before_its_call_is_rejected(tmp_path):
    source_home = tmp_path / "claude"
    _write_claude_source(
        source_home,
        [
            _claude_record(
                "record-1",
                [{"type": "tool_result", "tool_use_id": "tool-1", "content": "early"}],
            ),
            _claude_record(
                "record-2",
                [{"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {}}],
                role="assistant",
                parent_id="record-1",
            ),
        ],
    )

    with pytest.raises(MigrationError, match="unknown tool call ID"):
        _migrate_claude(tmp_path, source_home, tmp_path / "codex")


def test_truncated_jsonl_is_rejected_before_any_target_is_created(tmp_path):
    source_home = tmp_path / "claude"
    source = source_home / "projects" / "fixture" / f"{SOURCE_ID}.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(b'{"type":"user","sessionId":"' + SOURCE_ID.encode() + b'"')
    target_home = tmp_path / "codex"

    with pytest.raises(MigrationError, match="invalid JSONL record 1"):
        _migrate_claude(tmp_path, source_home, target_home)

    assert not target_home.exists()


def test_jsonl_record_and_line_limits_are_enforced(monkeypatch):
    monkeypatch.setattr(migration_engine, "MAX_RECORDS", 1)
    with pytest.raises(migration_engine.EngineError, match="record limit"):
        migration_engine._read_jsonl(b"{}\n{}\n")

    monkeypatch.setattr(migration_engine, "MAX_LINE_BYTES", 2)
    with pytest.raises(migration_engine.EngineError, match="size limit"):
        migration_engine._read_jsonl(b'{"a":1}\n')


def test_existing_manifest_is_preserved_and_output_is_not_created(tmp_path):
    source_home = tmp_path / "claude"
    _write_claude_source(source_home, [_claude_record("record-1", "hello")])
    target_home = tmp_path / "codex"
    manifest = target_home / "session-handoff" / "manifests" / f"{TARGET_ID}.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("preexisting", encoding="utf-8")

    with pytest.raises(MigrationError, match="existing target"):
        _migrate_claude(tmp_path, source_home, target_home)

    assert manifest.read_text(encoding="utf-8") == "preexisting"
    assert not list((target_home / "sessions").rglob("*.jsonl"))


def test_target_symlink_escape_is_rejected(tmp_path):
    source_home = tmp_path / "claude"
    _write_claude_source(source_home, [_claude_record("record-1", "hello")])
    target_home = tmp_path / "codex"
    target_home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (target_home / "sessions").symlink_to(outside, target_is_directory=True)

    with pytest.raises(MigrationError, match="outside the target home"):
        _migrate_claude(tmp_path, source_home, target_home)

    assert not list(outside.rglob("*.jsonl"))


def test_source_symlink_escape_is_rejected(tmp_path):
    source_home = tmp_path / "claude"
    projects = source_home / "projects"
    projects.mkdir(parents=True)
    outside = tmp_path / "outside"
    _write_claude_source(outside, [_claude_record("record-1", "hello")])
    (projects / "fixture").symlink_to(
        outside / "projects" / "fixture", target_is_directory=True
    )

    with pytest.raises(MigrationError, match="outside the source home"):
        _migrate_claude(tmp_path, source_home, tmp_path / "codex")


def test_source_change_cleanup_preserves_replaced_foreign_output(tmp_path, monkeypatch):
    source_home = tmp_path / "claude"
    source = _write_claude_source(source_home, [_claude_record("record-1", "hello")])
    target_home = tmp_path / "codex"
    real_write_pair = migration_engine._write_pair
    captured: dict[str, Path] = {}

    def race(output, data, manifest, manifest_data):
        ownership = real_write_pair(output, data, manifest, manifest_data)
        captured.update(output=output, manifest=manifest)
        source.write_bytes(source.read_bytes() + b"\n")
        output.unlink()
        output.write_bytes(b"foreign replacement")
        return ownership

    monkeypatch.setattr(migration_engine, "_write_pair", race)

    with pytest.raises(MigrationError, match="source session changed"):
        _migrate_claude(tmp_path, source_home, target_home)

    assert captured["output"].read_bytes() == b"foreign replacement"
    assert not captured["manifest"].exists()


def test_write_pair_fsyncs_files_before_deduplicated_parent_directories(
    tmp_path, monkeypatch
):
    output = tmp_path / "target" / "session.jsonl"
    manifest = tmp_path / "manifest" / "result.json"
    calls: list[str] = []
    real_fsync = migration_engine.os.fsync

    def record_fsync(descriptor):
        mode = migration_engine.os.fstat(descriptor).st_mode
        calls.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(migration_engine.os, "fsync", record_fsync)

    migration_engine._write_pair(output, b"target", manifest, b"manifest")

    assert calls == ["file", "file", "directory", "directory"]


def test_write_pair_directory_fsync_failure_cleans_only_owned_files(
    tmp_path, monkeypatch
):
    output = tmp_path / "target" / "session.jsonl"
    manifest = tmp_path / "manifest" / "result.json"
    preserved = tmp_path / "target" / "preserved.txt"
    preserved.parent.mkdir(parents=True)
    preserved.write_text("keep", encoding="utf-8")
    real_fsync = migration_engine.os.fsync

    def fail_on_directory(descriptor):
        mode = migration_engine.os.fstat(descriptor).st_mode
        if stat.S_ISDIR(mode):
            raise OSError("synthetic directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(migration_engine.os, "fsync", fail_on_directory)

    with pytest.raises(OSError, match="directory fsync"):
        migration_engine._write_pair(output, b"target", manifest, b"manifest")

    assert not output.exists()
    assert not manifest.exists()
    assert preserved.read_text(encoding="utf-8") == "keep"
