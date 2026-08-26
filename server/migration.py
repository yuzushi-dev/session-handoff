"""Optional session-migrate backend used by the supervised launcher."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

try:
    from .paginated_migration import (
        PaginatedMigrationError,
        PaginatedProjection,
        codex_history_mode,
        project_paginated_codex,
    )
except ImportError:
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


def _resolve_executable(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    for candidate in ("smigrate", "session-migrate"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise MigrationError(
        "session-migrate is not installed or not on PATH; install the optional "
        "session-migrate backend before using migrate mode"
    )


def _run_json(
    argv: list[str],
    *,
    cwd: str,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    result = runner(
        argv,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "migration command failed").strip()
        raise MigrationError(detail[:4000])

    output = (result.stdout or "").strip()
    if not output:
        raise MigrationError("session-migrate returned no JSON result")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        lines = [line for line in output.splitlines() if line.strip()]
        try:
            payload = json.loads(lines[-1])
        except (IndexError, json.JSONDecodeError):
            raise MigrationError("session-migrate returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise MigrationError("session-migrate returned an invalid result object")
    return payload


def _validate_result(
    payload: dict[str, Any],
    *,
    source_client: str,
    target_client: str,
    target_session_id: str,
    dry_run: bool,
) -> None:
    if payload.get("source_format") != source_client:
        raise MigrationError("session-migrate reported an unexpected source format")
    if payload.get("target_format") != target_client:
        raise MigrationError("session-migrate reported an unexpected target format")
    if payload.get("session_id") != target_session_id:
        raise MigrationError("session-migrate reported an unexpected target session id")
    if bool(payload.get("dry_run")) is not dry_run:
        raise MigrationError("session-migrate reported an unexpected dry-run state")


def _transfer_args(
    base: list[str],
    *,
    source_home: Path | None,
    target_home: str | None,
) -> list[str]:
    args = list(base)
    if source_home is not None:
        args.extend(("--source-home", str(source_home)))
    if target_home is not None:
        args.extend(("--home", str(Path(target_home).expanduser().resolve())))
    return args


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
        raise MigrationError(f"session-migrate returned no {label} path")
    path = Path(value).expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise MigrationError(f"session-migrate returned {label} outside the target home") from exc
    if not path.is_file():
        raise MigrationError(f"session-migrate returned a missing {label}")
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
    output = _target_file(target_home, payload.get("output"), "target output")
    manifest_path = _target_file(target_home, payload.get("manifest"), "manifest")
    output_before = output.read_bytes()
    manifest_before = manifest_path.read_bytes()
    try:
        records = [json.loads(line) for line in output_before.decode("utf-8").splitlines()]
        manifest = json.loads(manifest_before)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError("session-migrate Codex target is invalid JSON") from exc
    if not records or not isinstance(manifest, dict):
        raise MigrationError("session-migrate Codex target is empty")
    if any(not isinstance(record, dict) for record in records):
        raise MigrationError("session-migrate Codex target contains a non-object record")
    metadata = records[0].get("payload")
    if not isinstance(metadata, dict) or target_id not in {
        metadata.get("id"),
        metadata.get("session_id"),
    }:
        raise MigrationError("session-migrate Codex target metadata does not match")
    target = manifest.get("target")
    if not isinstance(target, dict) or target.get("session_id") != target_id:
        raise MigrationError("session-migrate manifest target does not match")
    if Path(str(target.get("path", ""))).expanduser().resolve() != output:
        raise MigrationError("session-migrate manifest output path does not match")
    if target.get("records") != len(records):
        raise MigrationError("session-migrate manifest record count does not match")
    if target.get("sha256") != hashlib.sha256(output_before).hexdigest():
        raise MigrationError("session-migrate manifest checksum does not match")

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


def _run_transfer(
    base: list[str],
    *,
    cwd: str,
    source_client: str,
    target_client: str,
    target_id: str,
    runner: Callable[..., Any],
    source_home: Path | None = None,
    target_home: str | None = None,
    projection: PaginatedProjection | None = None,
) -> dict[str, Any]:
    command = _transfer_args(base, source_home=source_home, target_home=target_home)
    dry_run_result = _run_json([*command, "--dry-run"], cwd=cwd, runner=runner)
    _validate_result(
        dry_run_result,
        source_client=source_client,
        target_client=target_client,
        target_session_id=target_id,
        dry_run=True,
    )

    applied_result = _run_json(command, cwd=cwd, runner=runner)
    _validate_result(
        applied_result,
        source_client=source_client,
        target_client=target_client,
        target_session_id=target_id,
        dry_run=False,
    )
    for field in ("warnings", "dropped_events"):
        if (dry_run_result.get(field) or []) != (applied_result.get(field) or []):
            raise MigrationError("session-migrate loss report changed after dry-run")

    warnings = list(projection.warnings) if projection else []
    warnings.extend(dry_run_result.get("warnings", []))
    normalized_fields = dict(projection.normalized_fields) if projection else {}
    if target_client == "codex":
        target_root = Path(
            target_home
            or os.environ.get("CODEX_HOME", Path.home() / ".codex")
        ).expanduser().resolve()
        removed = _normalize_codex_target(
            applied_result,
            target_home=target_root,
            target_id=target_id,
        )
        if removed:
            warnings.append(
                {
                    "code": "codex_duplicate_user_event_removed",
                    "message": "Removed duplicate Codex user event records",
                    "count": removed,
                }
            )
            normalized_fields["codexTarget"] = ["deduplicated_user_events"]
    dropped = dict(projection.dropped) if projection else {}
    for key, value in (dry_run_result.get("dropped_events", {}) or {}).items():
        dropped[key] = dropped.get(key, 0) + value
    return {
        "session_id": target_id,
        "source_format": source_client,
        "target_format": target_client,
        "warnings": warnings,
        "dropped_events": dropped,
        "context_loss": {
            "dropped_events": dropped,
            "normalized_fields": normalized_fields,
        },
        "manifest": applied_result.get("manifest"),
        "output": applied_result.get("output"),
    }


def migrate_session(
    source_client: str,
    target_client: str,
    source_session_id: str,
    workspace: str,
    *,
    executable: str | None = None,
    target_session_id: str | None = None,
    source_home: str | None = None,
    target_home: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Dry-run and apply one native Claude/Codex migration with a fixed target id."""

    if source_client not in SUPPORTED_CLIENTS or target_client not in SUPPORTED_CLIENTS:
        raise MigrationError("migrate mode currently supports only Claude and Codex")
    if source_client == target_client:
        raise MigrationError("migrate mode requires a different target client")
    if not isinstance(source_session_id, str) or not source_session_id.strip():
        raise MigrationError("source session id must be a non-empty string")

    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise MigrationError(f"workspace is not a directory: {workspace}")
    source_root = Path(source_home).expanduser().resolve() if source_home else None
    if source_root is not None and not source_root.is_dir():
        raise MigrationError(f"source home is not a directory: {source_home}")

    migration_executable = _resolve_executable(executable)
    target_id = target_session_id or str(uuid.uuid4())
    base = [
        migration_executable,
        "transfer",
        source_session_id,
        "--from",
        source_client,
        "--to",
        target_client,
        "--cwd",
        str(root),
        "--session-id",
        target_id,
    ]

    if source_client != "codex" or target_client != "claude":
        return _run_transfer(
            base,
            cwd=str(root),
            source_client=source_client,
            target_client=target_client,
            target_id=target_id,
            runner=runner,
            source_home=source_root,
            target_home=target_home,
        )

    codex_home = source_root or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    try:
        paginated = codex_history_mode(codex_home, source_session_id) == "paginated"
        if not paginated:
            return _run_transfer(
                base,
                cwd=str(root),
                source_client=source_client,
                target_client=target_client,
                target_id=target_id,
                runner=runner,
                source_home=source_root,
                target_home=target_home,
            )
        with tempfile.TemporaryDirectory(prefix="session-handoff-paginated-") as projection_root:
            projection = project_paginated_codex(
                codex_home,
                source_session_id,
                output_root=projection_root,
            )
            return _run_transfer(
                base,
                cwd=str(root),
                source_client=source_client,
                target_client=target_client,
                target_id=target_id,
                runner=runner,
                source_home=Path(projection_root),
                target_home=target_home,
                projection=projection,
            )
    except PaginatedMigrationError as exc:
        raise MigrationError(str(exc)) from exc
