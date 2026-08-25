"""Project Codex paginated history into a temporary legacy rollout view."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_ITEMS = 100_000
MAX_ITEM_BYTES = 16 * 1024 * 1024
NORMALIZED_FIELDS = {
    "userMessage": ("clientId",),
    "agentMessage": ("delivery", "memoryCitation", "phase"),
    "commandExecution": (
        "commandActions",
        "durationMs",
        "exitCode",
        "pluginId",
        "processId",
        "scriptPath",
        "source",
        "status",
    ),
    "webSearch": ("action",),
}


class PaginatedMigrationError(RuntimeError):
    """A paginated Codex source cannot be projected safely."""


@dataclass(frozen=True)
class PaginatedProjection:
    source_rollout: Path
    rollout_path: Path
    dropped: dict[str, int]
    normalized_fields: dict[str, list[str]]
    warnings: tuple[dict[str, str], ...]


def project_paginated_codex(
    source_home: str | Path,
    session_id: str,
    *,
    output_root: str | Path,
) -> PaginatedProjection:
    """Create a private legacy Codex view from canonical paginated SQLite items."""

    home = Path(source_home).expanduser().resolve()
    source_rollout = _find_rollout(home, session_id)
    before_hash = _sha256(source_rollout)
    metadata = _read_metadata(source_rollout, session_id)
    if metadata.get("history_mode") != "paginated":
        raise PaginatedMigrationError("source rollout is not Codex paginated history")

    history_db = home / "thread_history_1.sqlite"
    if not history_db.is_file():
        raise PaginatedMigrationError(f"Codex thread history database not found: {history_db}")
    items = _read_items(history_db, session_id)
    if not items:
        raise PaginatedMigrationError("Codex paginated thread has no canonical history items")
    if _sha256(source_rollout) != before_hash:
        raise PaginatedMigrationError("Codex source rollout changed during projection")

    records, dropped, normalized_fields = _legacy_records(metadata, items)
    destination = Path(output_root).expanduser().resolve()
    rollout_path = (
        destination
        / "sessions"
        / "2000"
        / "01"
        / "01"
        / f"rollout-projected-{session_id}.jsonl"
    )
    rollout_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_private(rollout_path, records)
    return PaginatedProjection(
        source_rollout=source_rollout,
        rollout_path=rollout_path,
        dropped=dict(sorted(dropped.items())),
        normalized_fields={
            key: sorted(values) for key, values in sorted(normalized_fields.items())
        },
        warnings=(
            {
                "code": "codex_paginated_projection",
                "message": "Codex canonical paginated items were projected into a temporary legacy view",
            },
        ),
    )


def _find_rollout(home: Path, session_id: str) -> Path:
    matches = sorted(
        [
            *home.glob(f"sessions/*/*/*/rollout-*-{session_id}.jsonl"),
            *home.glob(f"archived_sessions/rollout-*-{session_id}.jsonl"),
        ]
    )
    if len(matches) != 1:
        raise PaginatedMigrationError(
            f"expected one Codex rollout for session {session_id}, found {len(matches)}"
        )
    return matches[0]


def codex_history_mode(source_home: str | Path, session_id: str) -> str | None:
    """Return a Codex rollout's history mode, or None when no rollout exists."""

    home = Path(source_home).expanduser().resolve()
    matches = sorted(
        [
            *home.glob(f"sessions/*/*/*/rollout-*-{session_id}.jsonl"),
            *home.glob(f"archived_sessions/rollout-*-{session_id}.jsonl"),
        ]
    )
    if not matches:
        return None
    if len(matches) != 1:
        raise PaginatedMigrationError(
            f"expected one Codex rollout for session {session_id}, found {len(matches)}"
        )
    return str(_read_metadata(matches[0], session_id).get("history_mode") or "")


def _read_metadata(path: Path, session_id: str) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            record = json.loads(handle.readline())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaginatedMigrationError("Codex source metadata is unreadable") from exc
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        raise PaginatedMigrationError("Codex source does not begin with session metadata")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise PaginatedMigrationError("Codex session metadata payload is invalid")
    if payload.get("id") != session_id and payload.get("session_id") != session_id:
        raise PaginatedMigrationError("Codex session metadata does not match requested ID")
    return payload


def _read_items(path: Path, session_id: str) -> list[tuple[int, int | None, dict[str, Any]]]:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = connection.execute(
            """
            SELECT item_id, rollout_ordinal, created_at_ms, item_json
            FROM thread_items
            WHERE thread_id = ?
            ORDER BY rollout_ordinal, item_id
            """,
            (session_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise PaginatedMigrationError("Codex thread history projection is unreadable") from exc
    finally:
        if connection is not None:
            connection.close()
    if len(rows) > MAX_ITEMS:
        raise PaginatedMigrationError("Codex paginated thread exceeds the item limit")

    items: list[tuple[int, int | None, dict[str, Any]]] = []
    for item_id, ordinal, created_at_ms, raw in rows:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_ITEM_BYTES:
            raise PaginatedMigrationError("Codex paginated item exceeds the size limit")
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PaginatedMigrationError("Codex paginated item is invalid JSON") from exc
        if not isinstance(item, dict):
            raise PaginatedMigrationError("Codex paginated item is not an object")
        items.append((int(ordinal), created_at_ms, item))
    return items


def _legacy_records(
    metadata: dict[str, Any],
    items: list[tuple[int, int | None, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], Counter[str], dict[str, set[str]]]:
    session_id = str(metadata.get("id") or metadata.get("session_id"))
    fallback_timestamp = _timestamp(metadata.get("timestamp"))
    records: list[dict[str, Any]] = [
        {
            "timestamp": fallback_timestamp,
            "type": "session_meta",
            "payload": {
                "session_id": session_id,
                "id": session_id,
                "timestamp": fallback_timestamp,
                "cwd": metadata.get("cwd") or str(Path.cwd()),
                "originator": "session-handoff-paginated",
                "cli_version": metadata.get("cli_version") or "unknown",
                "source": "cli",
                "model_provider": metadata.get("model_provider") or "openai",
                "history_mode": "legacy",
            },
        }
    ]
    dropped: Counter[str] = Counter()
    normalized_fields: dict[str, set[str]] = {}
    for ordinal, created_at_ms, item in items:
        item_type = item.get("type")
        _collect_normalized_fields(normalized_fields, item_type, item)
        timestamp = _timestamp_from_ms(created_at_ms) or fallback_timestamp
        if item_type == "userMessage":
            text = _text_content(item.get("content"), dropped)
            if text:
                records.append(_message(timestamp, "user", text))
            else:
                dropped["userMessage"] += 1
        elif item_type == "agentMessage":
            text = item.get("text")
            if isinstance(text, str) and text:
                records.append(_message(timestamp, "assistant", text))
            else:
                dropped["agentMessage"] += 1
        elif item_type == "commandExecution":
            call_id = _item_id(item, ordinal)
            arguments = {
                "command": item.get("command", ""),
                "cwd": item.get("cwd"),
            }
            records.extend(
                _tool_records(
                    timestamp,
                    call_id,
                    "command_execution",
                    arguments,
                    item.get("aggregatedOutput") or item.get("status", ""),
                )
            )
        elif item_type == "webSearch":
            call_id = _item_id(item, ordinal)
            records.extend(
                _tool_records(
                    timestamp,
                    call_id,
                    "web_search",
                    {"query": item.get("query", "")},
                    item.get("results", []),
                )
            )
        elif item_type == "reasoning":
            dropped["reasoning"] += 1
        else:
            dropped[str(item_type or "<missing>")] += 1
    return records, dropped, normalized_fields


def _collect_normalized_fields(
    normalized_fields: dict[str, set[str]],
    item_type: Any,
    item: dict[str, Any],
) -> None:
    if not isinstance(item_type, str):
        return
    fields = {field for field in NORMALIZED_FIELDS.get(item_type, ()) if field in item}
    if fields:
        normalized_fields.setdefault(item_type, set()).update(fields)


def _text_content(content: Any, dropped: Counter[str]) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in {"text", "input_text"}:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
            continue
        dropped["userMessage:block"] += 1
    return "".join(parts)


def _message(timestamp: str, role: str, text: str) -> dict[str, Any]:
    content_type = "input_text" if role == "user" else "output_text"
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": content_type, "text": text}],
        },
    }


def _tool_records(
    timestamp: str,
    call_id: str,
    name: str,
    arguments: dict[str, Any],
    output: Any,
) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                "call_id": call_id,
            },
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": output if isinstance(output, (str, list, dict)) else str(output),
            },
        },
    ]


def _item_id(item: dict[str, Any], ordinal: int) -> str:
    value = item.get("id")
    return value if isinstance(value, str) and value else f"paginated-item-{ordinal}"


def _timestamp(value: Any) -> str:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _timestamp_from_ms(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, UTC).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private(path: Path, records: list[dict[str, Any]]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
    finally:
        if descriptor != -1:
            os.close(descriptor)
