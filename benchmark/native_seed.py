"""Create isolated native Claude or Codex sessions from synthetic transcripts."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from pathlib import Path


TIMESTAMP = "2026-08-25T10:00:00Z"


def _private_text(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def _seed_codex(
    home: Path,
    session_id: str,
    workspace: Path,
    transcript: str,
) -> tuple[Path, ...]:
    rollout = (
        home
        / "sessions/2026/08/25"
        / f"rollout-benchmark-{session_id}.jsonl"
    )
    metadata = {
        "timestamp": TIMESTAMP,
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "session_id": session_id,
            "timestamp": TIMESTAMP,
            "cwd": str(workspace),
            "cli_version": "0.149.1",
            "model_provider": "openai",
            "history_mode": "paginated",
        },
    }
    _private_text(rollout, json.dumps(metadata, separators=(",", ":")) + "\n")
    database = home / "thread_history_1.sqlite"
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
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
        item = {
            "type": "userMessage",
            "id": "synthetic-transcript",
            "content": [{"type": "text", "text": transcript}],
        }
        connection.execute(
            "INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                "synthetic-turn",
                "synthetic-transcript",
                0,
                1_756_112_400_000,
                json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                "userMessage",
                0,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    database.chmod(0o600)
    return rollout, database


def _seed_claude(
    home: Path,
    session_id: str,
    workspace: Path,
    transcript: str,
) -> tuple[Path, ...]:
    project = re.sub(r"[^A-Za-z0-9_-]", "-", str(workspace))
    transcript_path = home / "projects" / project / f"{session_id}.jsonl"
    record = {
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": str(workspace),
        "sessionId": session_id,
        "version": "2.1.233",
        "gitBranch": "",
        "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"session-handoff:{session_id}:0")),
        "timestamp": TIMESTAMP,
        "type": "user",
        "message": {"role": "user", "content": transcript},
    }
    _private_text(
        transcript_path,
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n",
    )
    return (transcript_path,)


def seed_native_session(
    client: str,
    home: str | Path,
    session_id: str,
    workspace: str | Path,
    transcript: str,
) -> tuple[Path, ...]:
    """Seed one synthetic native session and return its authoritative files."""

    if client not in {"claude", "codex"}:
        raise ValueError("client must be claude or codex")
    try:
        uuid.UUID(session_id)
    except ValueError as exc:
        raise ValueError("session_id must be a UUID") from exc
    if not isinstance(transcript, str) or not transcript:
        raise ValueError("transcript must be non-empty text")
    root = Path(home).expanduser().resolve()
    worktree = Path(workspace).expanduser().resolve()
    if not worktree.is_dir():
        raise ValueError(f"workspace is not a directory: {workspace}")
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"source home is not empty: {root}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if client == "codex":
        return _seed_codex(root, session_id, worktree, transcript)
    return _seed_claude(root, session_id, worktree, transcript)
