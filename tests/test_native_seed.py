import hashlib
import json
from pathlib import Path

import pytest

from benchmark.native_seed import seed_native_session
from server.migration import migrate_session


def test_seeded_codex_rollout_contains_resumable_native_history(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = "resumable-codex-context"

    rollout, _ = seed_native_session(
        "codex",
        tmp_path / "codex",
        "50000000-0000-4000-8000-000000000003",
        workspace,
        marker,
    )

    records = [json.loads(line) for line in rollout.read_text().splitlines()]
    assert records[0]["payload"]["history_mode"] == "legacy"
    assert records[0]["payload"]["originator"] == "session-handoff-benchmark"
    assert records[0]["payload"]["source"] == "cli"
    assert records[1] == {
        "timestamp": "2026-08-25T10:00:00Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": marker}],
        },
    }


@pytest.mark.parametrize(
    ("source_client", "target_client", "source_id", "target_id"),
    [
        (
            "codex",
            "claude",
            "50000000-0000-4000-8000-000000000001",
            "60000000-0000-4000-8000-000000000001",
        ),
        (
            "claude",
            "codex",
            "50000000-0000-4000-8000-000000000002",
            "60000000-0000-4000-8000-000000000002",
        ),
    ],
)
def test_seeded_native_session_migrates_with_content_and_source_immutability(
    tmp_path,
    source_client,
    target_client,
    source_id,
    target_id,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_home = tmp_path / f"source-{source_client}"
    marker = f"synthetic-context-{source_client}-to-{target_client}"
    source_files = seed_native_session(
        source_client,
        source_home,
        source_id,
        workspace,
        f"Long synthetic transcript containing {marker}.",
    )
    source_hashes = {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files
    }

    result = migrate_session(
        source_client,
        target_client,
        source_id,
        str(workspace),
        source_home=str(source_home),
        target_session_id=target_id,
        target_home=str(tmp_path / f"target-{target_client}"),
    )

    assert marker in Path(result["output"]).read_text(encoding="utf-8")
    for path, expected_hash in source_hashes.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash


def test_claude_to_codex_migration_removes_duplicate_user_event(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_home = tmp_path / "claude"
    target_home = tmp_path / "codex"
    marker = "one-logical-user-message"
    source_id = "50000000-0000-4000-8000-000000000007"
    target_id = "60000000-0000-4000-8000-000000000007"
    seed_native_session("claude", source_home, source_id, workspace, marker)

    result = migrate_session(
        "claude",
        "codex",
        source_id,
        str(workspace),
        source_home=str(source_home),
        target_session_id=target_id,
        target_home=str(target_home),
    )

    output = Path(result["output"])
    records = [json.loads(line) for line in output.read_text().splitlines()]
    event_messages = [
        record["payload"]["message"]
        for record in records
        if record["type"] == "event_msg"
        and record["payload"].get("type") == "user_message"
    ]
    response_messages = [
        "".join(block.get("text", "") for block in record["payload"]["content"])
        for record in records
        if record["type"] == "response_item"
        and record["payload"].get("type") == "message"
        and record["payload"].get("role") == "user"
    ]
    assert marker not in event_messages
    assert response_messages.count(marker) == 1
    assert any(
        warning.get("code") == "codex_duplicate_user_event_removed"
        for warning in result["warnings"]
    )
    manifest = json.loads(Path(result["manifest"]).read_text())
    assert manifest["target"]["records"] == len(records)
    assert manifest["target"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
