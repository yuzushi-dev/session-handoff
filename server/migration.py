"""Optional session-migrate backend used by the supervised launcher."""

from __future__ import annotations

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
            "normalized_fields": projection.normalized_fields if projection else {},
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
