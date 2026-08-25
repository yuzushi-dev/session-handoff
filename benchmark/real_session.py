"""Metadata-only helpers for opt-in tests against local native sessions."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def find_codex_rollout(source_home: str | Path, session_id: str) -> Path:
    """Find one native Codex rollout without opening its transcript."""

    if not session_id or any(char in session_id for char in "/*?[]"):
        raise ValueError("invalid Codex session id")
    home = Path(source_home).expanduser().resolve()
    matches = sorted(
        [
            *home.glob(f"sessions/*/*/*/rollout-*-{session_id}.jsonl"),
            *home.glob(f"archived_sessions/rollout-*-{session_id}.jsonl"),
        ]
    )
    if len(matches) != 1:
        raise ValueError(f"expected one Codex rollout, found {len(matches)}")
    return matches[0]


def count_thread_items(history_db: str | Path, session_id: str) -> int:
    """Count canonical items through SQLite read-only mode."""

    path = Path(history_db).expanduser().resolve()
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM thread_items WHERE thread_id = ?",
                (session_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise ValueError("Codex thread history is unreadable") from exc
    return int(row[0]) if row else 0
