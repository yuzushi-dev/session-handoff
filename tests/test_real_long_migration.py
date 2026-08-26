import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from benchmark.real_session import count_thread_items, find_codex_rollout
from server.migration import migrate_session


def _thread_hash(database: Path, session_id: str) -> str:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT item_id, rollout_ordinal, created_at_ms, item_json,
                   item_type, updated_at_ordinal
            FROM thread_items
            WHERE thread_id = ?
            ORDER BY rollout_ordinal, item_id
            """,
            (session_id,),
        ).fetchall()
    finally:
        connection.close()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def test_real_long_codex_session_migrates_without_mutating_source(tmp_path, monkeypatch):
    session_id = os.environ.get("SESSION_HANDOFF_REAL_CODEX_SESSION_ID")
    if not session_id:
        pytest.skip("set SESSION_HANDOFF_REAL_CODEX_SESSION_ID for a local real-session run")

    source_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    rollout = find_codex_rollout(source_home, session_id)
    source_hash = hashlib.sha256(rollout.read_bytes()).hexdigest()
    database = source_home / "thread_history_1.sqlite"
    thread_hash = _thread_hash(database, session_id)
    source_items = count_thread_items(database, session_id)
    minimum_items = int(os.environ.get("SESSION_HANDOFF_REAL_MIN_ITEMS", "1000"))
    assert source_items >= minimum_items

    target_id = "40000000-0000-4000-8000-000000000001"
    target_home = tmp_path / "claude"
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    result = migrate_session(
        "codex",
        "claude",
        session_id,
        str(tmp_path),
        target_session_id=target_id,
        target_home=str(target_home),
    )

    assert result["session_id"] == target_id
    assert result["source_format"] == "codex"
    assert result["target_format"] == "claude"
    assert isinstance(result["warnings"], list)
    assert isinstance(result["dropped_events"], dict)
    assert set(result["dropped_events"]) <= {"reasoning"}
    assert isinstance(result["context_loss"], dict)
    assert Path(result["manifest"]).is_file()
    output = Path(result["output"])
    assert output.is_file() and output.stat().st_size > 0
    assert any(json.loads(line) for line in output.read_text().splitlines())
    assert hashlib.sha256(rollout.read_bytes()).hexdigest() == source_hash
    assert _thread_hash(database, session_id) == thread_hash
