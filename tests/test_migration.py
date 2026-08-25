import json
from types import SimpleNamespace

import pytest

from server.migration import MigrationError, migrate_session


def test_migrate_session_uses_one_target_id_for_dry_run_and_apply(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        dry_run = "--dry-run" in argv
        payload = {
            "source_format": "claude",
            "target_format": "codex",
            "session_id": "target-id",
            "dry_run": dry_run,
            "warnings": [{"kind": "fixture"}],
            "dropped_events": {"thinking:unsupported": 1},
            "manifest": "/tmp/manifest.json" if not dry_run else None,
            "output": "/tmp/target.jsonl" if not dry_run else None,
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload) + "\n", stderr="")

    result = migrate_session(
        "claude",
        "codex",
        "source-id",
        str(tmp_path),
        executable="smigrate",
        target_session_id="target-id",
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
