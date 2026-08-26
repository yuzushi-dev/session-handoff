import hashlib
import json
from types import SimpleNamespace

import pytest

from server.migration import (
    MigrationError,
    _normalize_codex_target,
    migrate_session,
    migration_telemetry_summary,
)


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


def test_migrate_session_uses_one_target_id_for_dry_run_and_apply(tmp_path):
    calls = []
    target_home = tmp_path / "target"
    output = target_home / "sessions/2026/08/25/rollout-target-id.jsonl"
    manifest = target_home / "session-migrate/manifests/target-id.json"

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        dry_run = "--dry-run" in argv
        if not dry_run:
            output.parent.mkdir(parents=True)
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
            manifest.parent.mkdir(parents=True)
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
        payload = {
            "source_format": "claude",
            "target_format": "codex",
            "session_id": "target-id",
            "dry_run": dry_run,
            "warnings": [{"kind": "fixture"}],
            "dropped_events": {"thinking:unsupported": 1},
            "manifest": str(manifest) if not dry_run else None,
            "output": str(output) if not dry_run else None,
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload) + "\n", stderr="")

    result = migrate_session(
        "claude",
        "codex",
        "source-id",
        str(tmp_path),
        executable="smigrate",
        target_session_id="target-id",
        target_home=str(target_home),
        runner=runner,
    )

    assert len(calls) == 2
    assert calls[0][0][0:3] == ["smigrate", "transfer", "source-id"]
    assert calls[0][0][-1] == "--dry-run"
    assert "--dry-run" not in calls[1][0]
    for argv, kwargs in calls:
        session_index = argv.index("--session-id")
        assert argv[session_index + 1] == "target-id"
        assert kwargs["cwd"] == str(tmp_path)
    assert result["session_id"] == "target-id"
    assert result["warnings"] == [{"kind": "fixture"}]
    assert result["dropped_events"] == {"thinking:unsupported": 1}


def test_migrate_session_stops_before_apply_when_dry_run_fails(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=2, stdout="", stderr="dry-run failed")

    with pytest.raises(MigrationError, match="dry-run failed"):
        migrate_session(
            "codex",
            "claude",
            "source-id",
            str(tmp_path),
            executable="smigrate",
            target_session_id="target-id",
            runner=runner,
        )

    assert len(calls) == 1
    assert "--dry-run" in calls[0]


def test_migrate_session_rejects_same_client(tmp_path):
    with pytest.raises(MigrationError, match="different target"):
        migrate_session(
            "codex",
            "codex",
            "source-id",
            str(tmp_path),
            executable="smigrate",
        )


def test_migrate_session_rejects_loss_report_changed_after_dry_run(tmp_path):
    def runner(argv, **_kwargs):
        dry_run = "--dry-run" in argv
        payload = {
            "source_format": "claude",
            "target_format": "codex",
            "session_id": "target-id",
            "dry_run": dry_run,
            "warnings": [],
            "dropped_events": {"tool_result:unsupported": 1 if dry_run else 2},
            "manifest": "/tmp/manifest.json",
            "output": "/tmp/target.jsonl",
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    with pytest.raises(MigrationError, match="loss report changed"):
        migrate_session(
            "claude",
            "codex",
            "source-id",
            str(tmp_path),
            executable="smigrate",
            target_session_id="target-id",
            runner=runner,
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
