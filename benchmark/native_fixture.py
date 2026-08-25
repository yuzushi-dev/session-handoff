"""Build a minimal native Codex paginated home for migration tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SESSION_ID = "10000000-0000-4000-8000-000000000002"


def build_paginated_codex_home(
    home: str | Path,
    session_id: str = SESSION_ID,
) -> Path:
    root = Path(home)
    rollout = root / "sessions" / "2026" / "08" / "25" / f"rollout-benchmark-{session_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-25T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "session_id": session_id,
                    "timestamp": "2026-08-25T10:00:00Z",
                    "cwd": "/work/benchmark",
                    "cli_version": "0.149.1",
                    "history_mode": "paginated",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    database = root / "thread_history_1.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
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
            {"type": "userMessage", "id": "user-1", "content": [{"type": "text", "text": "benchmark user request"}]},
        ),
        (
            "agent-1",
            "agentMessage",
            {"type": "agentMessage", "id": "agent-1", "text": "benchmark assistant response"},
        ),
        (
            "command-1",
            "commandExecution",
            {
                "type": "commandExecution",
                "id": "command-1",
                "command": "pytest -q",
                "cwd": "/work/benchmark",
                "aggregatedOutput": "2 passed",
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
            {"type": "webSearch", "id": "search-1", "query": "context rot", "results": []},
        ),
    ]
    for ordinal, (item_id, item_type, item) in enumerate(items):
        connection.execute(
            "INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                "turn-1",
                item_id,
                ordinal,
                1_756_112_400_000 + ordinal,
                json.dumps(item),
                item_type,
                ordinal,
            ),
        )
    connection.commit()
    connection.close()
    return rollout
