"""Native Claude Code and Codex session migration."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

try:
    from .migration_engine import EngineError, convert_native_session
    from .paginated_migration import (
        PaginatedMigrationError,
        PaginatedProjection,
        codex_history_mode,
        project_paginated_codex,
    )
except ImportError:
    from migration_engine import EngineError, convert_native_session  # type: ignore[no-redef]
    from paginated_migration import (  # type: ignore[no-redef]
        PaginatedMigrationError,
        PaginatedProjection,
        codex_history_mode,
        project_paginated_codex,
    )

SUPPORTED_CLIENTS = {"claude", "codex"}


class MigrationError(RuntimeError):
    """A migration failed before a resumable target session was established."""


def _numeric_loss_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, min(value, 10000))
    if isinstance(value, dict):
        return min(10000, sum(_numeric_loss_count(item) for item in value.values()))
    if isinstance(value, (list, tuple)):
        return min(10000, len(value))
    return 0


def migration_telemetry_summary(result: dict[str, Any]) -> dict[str, int]:
    """Return only bounded numeric loss counts from a migration result."""
    if not isinstance(result, dict):
        return {"dropped_events": 0, "normalized_fields": 0}
    context_loss = result.get("context_loss")
    if not isinstance(context_loss, dict):
        context_loss = {}
    return {
        "dropped_events": _numeric_loss_count(
            result.get("dropped_events", context_loss.get("dropped_events"))
        ),
        "normalized_fields": _numeric_loss_count(
            context_loss.get("normalized_fields")
        ),
    }


def _user_event_text(record: dict[str, Any]) -> str | None:
    payload = record.get("payload")
    if (
        record.get("type") == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") == "user_message"
        and isinstance(payload.get("message"), str)
    ):
        return payload["message"]
    return None


def _user_response_text(record: dict[str, Any]) -> str | None:
    payload = record.get("payload")
    if not (
        record.get("type") == "response_item"
        and isinstance(payload, dict)
        and payload.get("type") == "message"
        and payload.get("role") == "user"
        and isinstance(payload.get("content"), list)
    ):
        return None
    parts: list[str] = []
    for block in payload["content"]:
        if not (
            isinstance(block, dict)
            and block.get("type") in {"text", "input_text"}
            and isinstance(block.get("text"), str)
        ):
            return None
        parts.append(block["text"])
    return "".join(parts)


def _target_file(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise MigrationError(f"migration returned no {label} path")
    path = Path(value).expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise MigrationError(f"migration returned {label} outside the target home") from exc
    if not path.is_file():
        raise MigrationError(f"migration returned a missing {label}")
    return path


def _replace_file(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, path.stat().st_mode & 0o777)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _normalize_codex_target(
    payload: dict[str, Any],
    *,
    target_home: Path,
    target_id: str,
) -> int:
    """Keep the legacy cleanup helper for callers handling old artifacts."""
    output = _target_file(target_home, payload.get("output"), "target output")
    manifest_path = _target_file(target_home, payload.get("manifest"), "manifest")
    output_before = output.read_bytes()
    manifest_before = manifest_path.read_bytes()
    try:
        records = [json.loads(line) for line in output_before.decode("utf-8").splitlines()]
        manifest = json.loads(manifest_before)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError("migration Codex target is invalid JSON") from exc
    if not records or not isinstance(manifest, dict):
        raise MigrationError("migration Codex target is empty")
    if any(not isinstance(record, dict) for record in records):
        raise MigrationError("migration Codex target contains a non-object record")
    metadata = records[0].get("payload")
    if not isinstance(metadata, dict) or target_id not in {
        metadata.get("id"),
        metadata.get("session_id"),
    }:
        raise MigrationError("migration target metadata does not match")
    target = manifest.get("target")
    if not isinstance(target, dict) or target.get("session_id") != target_id:
        raise MigrationError("migration manifest target does not match")
    if Path(str(target.get("path", ""))).expanduser().resolve() != output:
        raise MigrationError("migration manifest output path does not match")
    if target.get("records") != len(records):
        raise MigrationError("migration manifest record count does not match")
    if target.get("sha256") != hashlib.sha256(output_before).hexdigest():
        raise MigrationError("migration manifest checksum does not match")

    normalized: list[dict[str, Any]] = []
    removed = 0
    index = 0
    while index < len(records):
        if index + 1 < len(records):
            event_text = _user_event_text(records[index])
            response_text = _user_response_text(records[index + 1])
            if event_text is not None and event_text == response_text:
                normalized.append(records[index + 1])
                removed += 1
                index += 2
                continue
        normalized.append(records[index])
        index += 1
    if not removed:
        return 0

    output_after = (
        "\n".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            for record in normalized
        )
        + "\n"
    ).encode("utf-8")
    target["records"] = len(normalized)
    target["sha256"] = hashlib.sha256(output_after).hexdigest()
    manifest_after = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    try:
        _replace_file(output, output_after)
        _replace_file(manifest_path, manifest_after)
    except OSError as exc:
        try:
            _replace_file(output, output_before)
            _replace_file(manifest_path, manifest_before)
        except OSError:
            pass
        raise MigrationError("failed to normalize Codex migration target") from exc
    return removed


def _default_home(client: str) -> Path:
    variable = "CLAUDE_CONFIG_DIR" if client == "claude" else "CODEX_HOME"
    configured = os.environ.get(variable)
    return Path(configured).expanduser().resolve() if configured else Path.home() / f".{client}"


def _find_source(home: Path, client: str, session_id: str) -> Path:
    if client == "claude":
        matches = sorted(home.glob(f"projects/*/{session_id}.jsonl"))
    else:
        matches = sorted(
            [
                *home.glob(f"sessions/*/*/*/rollout-*-{session_id}.jsonl"),
                *home.glob(f"archived_sessions/rollout-*-{session_id}.jsonl"),
            ]
        )
    if not matches:
        raise MigrationError(f"no {client} session found for UUID in the selected source home")
    if len(matches) != 1:
        raise MigrationError(f"multiple {client} sessions matched the UUID")
    return matches[0]


def _migrate_path(
    source_path: Path,
    source_client: str,
    target_client: str,
    source_session_id: str,
    root: Path,
    target_id: str,
    target_root: Path,
    projection: PaginatedProjection | None = None,
) -> dict[str, Any]:
    try:
        result = convert_native_session(
            source_path,
            source_client,
            target_client,
            source_session_id,
            target_id,
            root,
            target_root,
        )
    except MigrationError:
        raise
    except (OSError, ValueError, RuntimeError, EngineError) as exc:
        raise MigrationError(str(exc)) from exc

    warnings = list(projection.warnings) if projection else []
    warnings.extend(result["warnings"])
    dropped = dict(projection.dropped) if projection else {}
    for key, value in result["dropped_events"].items():
        dropped[key] = dropped.get(key, 0) + value
    normalized_fields = dict(projection.normalized_fields) if projection else {}
    if target_client == "codex":
        warnings.append(
            {
                "code": "codex_duplicate_user_event_removed",
                "message": "Codex target stores each user message once",
                "count": 0,
            }
        )
        normalized_fields["codexTarget"] = ["deduplicated_user_events"]
    result["warnings"] = warnings
    result["dropped_events"] = dropped
    result["context_loss"] = {
        "dropped_events": dropped,
        "normalized_fields": normalized_fields,
    }
    return result


def migrate_session(
    source_client: str,
    target_client: str,
    source_session_id: str,
    workspace: str,
    *,
    target_session_id: str | None = None,
    source_home: str | None = None,
    target_home: str | None = None,
) -> dict[str, Any]:
    """Migrate a native Claude or Codex session with the internal engine."""
    if source_client not in SUPPORTED_CLIENTS or target_client not in SUPPORTED_CLIENTS:
        raise MigrationError("migrate mode currently supports only Claude and Codex")
    if source_client == target_client:
        raise MigrationError("migrate mode requires a different target client")
    if not isinstance(source_session_id, str) or not source_session_id.strip():
        raise MigrationError("source session id must be a non-empty string")
    try:
        uuid.UUID(source_session_id)
    except ValueError as exc:
        raise MigrationError("source session id must be a valid UUID") from exc

    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise MigrationError(f"workspace is not a directory: {workspace}")
    target_id = target_session_id or str(uuid.uuid4())
    try:
        uuid.UUID(target_id)
    except ValueError as exc:
        raise MigrationError("target session id must be a valid UUID") from exc

    source_root = (
        Path(source_home).expanduser().resolve()
        if source_home
        else _default_home(source_client)
    )
    target_root = (
        Path(target_home).expanduser().resolve()
        if target_home
        else _default_home(target_client)
    )
    if not source_root.is_dir():
        raise MigrationError(f"source home is not a directory: {source_root}")

    try:
        projection = None
        source_path = _find_source(source_root, source_client, source_session_id)
        if source_client == "codex" and codex_history_mode(source_root, source_session_id) == "paginated":
            with tempfile.TemporaryDirectory(prefix="session-handoff-paginated-") as directory:
                projection = project_paginated_codex(
                    source_root,
                    source_session_id,
                    output_root=directory,
                )
                return _migrate_path(
                    projection.rollout_path,
                    source_client,
                    target_client,
                    source_session_id,
                    root,
                    target_id,
                    target_root,
                    projection,
                )
        return _migrate_path(
            source_path,
            source_client,
            target_client,
            source_session_id,
            root,
            target_id,
            target_root,
        )
    except MigrationError:
        raise
    except PaginatedMigrationError as exc:
        raise MigrationError(str(exc)) from exc
