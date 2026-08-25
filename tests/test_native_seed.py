import hashlib
from pathlib import Path

import pytest

from benchmark.native_seed import seed_native_session
from server.migration import migrate_session


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
        executable="session-migrate",
        source_home=str(source_home),
        target_session_id=target_id,
        target_home=str(tmp_path / f"target-{target_client}"),
    )

    assert marker in Path(result["output"]).read_text(encoding="utf-8")
    for path, expected_hash in source_hashes.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash
