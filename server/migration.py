"""Optional session-migrate backend used by the supervised launcher."""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

SUPPORTED_CLIENTS = {"claude", "codex"}


class MigrationError(RuntimeError):
    """A migration failed before a resumable target session was established."""


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

    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise MigrationError("session-migrate returned no JSON result")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
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


def migrate_session(
    source_client: str,
    target_client: str,
    source_session_id: str,
    workspace: str,
    *,
    executable: str | None = None,
    target_session_id: str | None = None,
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

    dry_run_result = _run_json([*base, "--dry-run"], cwd=str(root), runner=runner)
    _validate_result(
        dry_run_result,
        source_client=source_client,
        target_client=target_client,
        target_session_id=target_id,
        dry_run=True,
    )

    applied_result = _run_json(base, cwd=str(root), runner=runner)
    _validate_result(
        applied_result,
        source_client=source_client,
        target_client=target_client,
        target_session_id=target_id,
        dry_run=False,
    )

    return {
        "session_id": target_id,
        "source_format": source_client,
        "target_format": target_client,
        "warnings": dry_run_result.get("warnings", []),
        "dropped_events": dry_run_result.get("dropped_events", {}),
        "manifest": applied_result.get("manifest"),
        "output": applied_result.get("output"),
    }
