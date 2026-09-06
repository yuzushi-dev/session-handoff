#!/usr/bin/env python3
"""Local MCP server for safe, workspace-scoped session handoffs.

The implementation intentionally uses only the Python standard library so the
plugin works immediately in Codex and Claude without a package installation.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Iterator

try:
    from .handoff_state import (
        HandoffStateError,
        MAX_CONTENT_BYTES,
        redact_state,
        render_state,
    )
    from .session_switch import (
        CONTROL_PATH_ENV,
        CONTROL_TOKEN_ENV,
        record_terminal_outcome,
        write_migration_request,
        write_switch_request,
    )
    from .version import PACKAGE_VERSION
    from . import handoff_store
except ImportError:  # direct `python server/handoff_mcp.py` execution
    from handoff_state import (  # type: ignore[no-redef]
        HandoffStateError,
        MAX_CONTENT_BYTES,
        redact_state,
        render_state,
    )
    from session_switch import (
        CONTROL_PATH_ENV,
        CONTROL_TOKEN_ENV,
        record_terminal_outcome,
        write_migration_request,
        write_switch_request,
    )
    from version import PACKAGE_VERSION
    import handoff_store  # type: ignore[no-redef]

SERVER_NAME = "session-handoff"
SERVER_VERSION = PACKAGE_VERSION
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
MAX_LIST_LIMIT = 100
MAX_SEARCH_QUERY_BYTES = 512
MAX_SEARCH_SNIPPET_BYTES = 512
MAX_SEARCH_MATCHES_PER_FILE = 8
MAX_SEARCH_FILES = 256
MAX_SEARCH_BYTES = 4 * 1024 * 1024
MAX_SEARCH_OUTPUT_BYTES = 64 * 1024

REQUIRED_SECTIONS = (
    "## Goal",
    "## Constraints & Preferences",
    "## Progress",
    "## Key Decisions",
    "## Critical Context",
    "## Next Steps",
)

_SECRET_KEY_PATTERN = r"\b[A-Za-z][A-Za-z0-9_-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|AUTHORIZATION)\b"
_ASSIGNMENT = re.compile(
    rf"(?P<key>{_SECRET_KEY_PATTERN})"
    r"(?P<spacing>\s*)(?P<separator>[:=])(?P<after>\s*)"
    r"(?P<quote>['\"]?)(?P<value>[^\s'\"`;,\)\]]+)(?P=quote)",
    re.IGNORECASE,
)
_MALFORMED_REDACTED = re.compile(
    rf"(?P<prefix>{_SECRET_KEY_PATTERN}\s*[:=]\s*(?P<quote>['\"]?)\[REDACTED\](?P=quote))"
    r"(?P<attached>[^\s'\"`;)\],]+)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_KNOWN_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)


class HandoffError(ValueError):
    """An actionable input or workspace error returned by an MCP tool."""


def _record_outcome(summary: dict[str, Any]) -> None:
    try:
        record_terminal_outcome(summary)
    except Exception:
        pass


def _replace_assignment(match: re.Match[str]) -> str:
    value = match.group("value")
    if value == "[REDACTED" and match.end() < len(match.string) and match.string[match.end()] == "]":
        return match.group(0)
    return (
        f"{match.group('key')}{match.group('spacing')}{match.group('separator')}"
        f"{match.group('after')}{match.group('quote')}[REDACTED]"
        f"{match.group('quote')}"
    )


def redact_secrets(text: str) -> tuple[str, int]:
    """Redact common credential forms before handoff text is persisted/displayed."""
    count = 0

    def replace_private(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "[PRIVATE KEY REDACTED]"

    def replace_bearer(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "Bearer [REDACTED]"

    def replace_token(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "[REDACTED]"

    def replace_assignment(match: re.Match[str]) -> str:
        nonlocal count
        replacement = _replace_assignment(match)
        if replacement != match.group(0):
            count += 1
        return replacement

    def replace_malformed_marker(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return match.group("prefix")

    redacted = _PRIVATE_KEY.sub(replace_private, text)
    redacted = _BEARER.sub(replace_bearer, redacted)
    redacted = _KNOWN_TOKEN.sub(replace_token, redacted)
    redacted = _MALFORMED_REDACTED.sub(replace_malformed_marker, redacted)
    redacted = _ASSIGNMENT.sub(replace_assignment, redacted)
    return redacted, count


def validate_handoff(text: str) -> list[str]:
    return [section for section in REQUIRED_SECTIONS if section not in text]


def _workspace_root(workspace: str) -> Path:
    if not isinstance(workspace, str) or not workspace.strip():
        raise HandoffError("workspace must be a non-empty path")
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise HandoffError(f"workspace is not a directory: {workspace}")
    return root


def _safe_path(
    workspace: str,
    path: str,
    *,
    must_exist: bool = False,
    allow_directory: bool = False,
) -> tuple[Path, Path]:
    root = _workspace_root(workspace)
    if not isinstance(path, str) or not path.strip():
        raise HandoffError("path must be a non-empty workspace-relative path")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise HandoffError("path must remain inside workspace") from exc
    if resolved == root:
        raise HandoffError("path must identify a file, not the workspace directory")
    if must_exist and not resolved.is_file():
        raise HandoffError(f"handoff file not found: {relative.as_posix()}")
    if resolved.exists() and resolved.is_dir() and not allow_directory:
        raise HandoffError("path must identify a file, not a directory")
    return root, resolved


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _secure_relative_io_supported() -> bool:
    supported = getattr(os, "supports_dir_fd", ())
    return (
        hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and all(
            function in supported
            for function in (os.open, os.mkdir, os.rename, os.link, os.unlink)
        )
    )


def _open_relative_parent(root: Path, path: Path, *, create: bool) -> tuple[int, str]:
    parts = Path(_relative(root, path)).parts
    parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            try:
                child_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o755, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            os.close(parent_fd)
            parent_fd = child_fd
    except Exception:
        os.close(parent_fd)
        raise
    return parent_fd, parts[-1]


def _read_file(root: Path, path: Path) -> tuple[str, int]:
    if _secure_relative_io_supported():
        parent_fd, name = _open_relative_parent(root, path, create=False)
        try:
            file_fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        finally:
            os.close(parent_fd)
        with os.fdopen(file_fd, "rb") as handoff_file:
            raw = handoff_file.read(MAX_CONTENT_BYTES + 1)
    else:
        raise HandoffError("secure filesystem primitives unavailable")
    if len(raw) > MAX_CONTENT_BYTES:
        raise HandoffError(f"content exceeds {MAX_CONTENT_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HandoffError("handoff file must be UTF-8 text") from exc
    redacted, redacted_count = redact_secrets(text)
    return redacted, redacted_count


def _atomic_write(root: Path, path: Path, content: str, *, overwrite: bool) -> None:
    if not _secure_relative_io_supported():
        raise HandoffError("secure filesystem primitives unavailable")

    parent_fd, name = _open_relative_parent(root, path, create=True)
    temporary_name: str | None = None
    temporary_fd: int | None = None
    try:
        for _ in range(10):
            candidate = f".{name}.{secrets.token_hex(8)}.tmp"
            try:
                temporary_fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_fd is None or temporary_name is None:
            raise FileExistsError("unable to create temporary handoff file")
        temporary = os.fdopen(temporary_fd, "w", encoding="utf-8")
        temporary_fd = None
        with temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        if overwrite:
            os.rename(
                temporary_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        else:
            os.link(
                temporary_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.unlink(temporary_name, dir_fd=parent_fd)
        temporary_name = None
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


def _require_string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise HandoffError(f"{name} must be a non-empty string")
    return value


def _control_credentials() -> tuple[str, str]:
    control_path = os.environ.get(CONTROL_PATH_ENV)
    token = os.environ.get(CONTROL_TOKEN_ENV)
    if control_path:
        token_path = Path(control_path).with_name("token")
        if token_path.is_file():
            token = token_path.read_text(encoding="utf-8").strip()
    return control_path or "", token or ""


def _state_array_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "maxItems": 256,
        "items": {
            "type": "string",
            "minLength": 1,
        },
    }


def _state_schema() -> dict[str, Any]:
    array_schema = _state_array_schema()
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "goal",
            "constraints_preferences",
            "progress",
            "key_decisions",
            "rejected_attempts",
            "verification",
            "critical_context",
            "uncertainties",
            "next_steps",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "goal": {"type": "string", "minLength": 1},
            "constraints_preferences": array_schema,
            "progress": {
                "type": "object",
                "additionalProperties": False,
                "required": ["done", "in_progress", "pending"],
                "properties": {
                    "done": _state_array_schema(),
                    "in_progress": _state_array_schema(),
                    "pending": _state_array_schema(),
                },
            },
            "key_decisions": _state_array_schema(),
            "rejected_attempts": _state_array_schema(),
            "verification": _state_array_schema(),
            "critical_context": _state_array_schema(),
            "uncertainties": _state_array_schema(),
            "next_steps": _state_array_schema(),
        },
    }


def _create(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        workspace = _require_string(arguments, "workspace")
        has_path = "path" in arguments
        has_name = "name" in arguments
        if has_path == has_name:
            raise HandoffError("exactly one of path or name must be provided")
        if has_name:
            handoff_store.validate_name(arguments["name"])
            root = _workspace_root(workspace)
            path = None
        else:
            requested_path = _require_string(arguments, "path")
            root, path = _safe_path(workspace, requested_path)
    except HandoffError:
        raise
    has_content = "content" in arguments
    has_state = "state" in arguments
    if has_content == has_state:
        raise HandoffError("exactly one of content or state must be provided")

    if has_content:
        try:
            content, redacted_count = redact_secrets(_require_string(arguments, "content"))
        except HandoffError:
            raise
    else:
        try:
            redacted_state, redacted_count = redact_state(arguments["state"])
            content = render_state(redacted_state)
        except HandoffStateError as exc:
            _record_outcome({
                "operation": "handoff", "source_client": "codex", "target_client": "codex",
                "result": "failure", "failure_stage": "state_schema",
                "handoff_bytes": 0, "redacted_count": 0, "dropped_events": 0,
                "normalized_fields": 0,
            })
            raise HandoffError(f"invalid state: {exc}") from exc

    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        _record_outcome({
            "operation": "handoff", "source_client": "codex", "target_client": "codex",
            "result": "failure", "failure_stage": "size_limit",
            "handoff_bytes": len(content.encode("utf-8")),
            "redacted_count": redacted_count, "dropped_events": 0, "normalized_fields": 0,
        })
        raise HandoffError(f"content exceeds {MAX_CONTENT_BYTES} bytes")
    try:
        overwrite = arguments.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise HandoffError("overwrite must be a boolean")
        auto_switch = arguments.get("auto_switch", False)
        if not isinstance(auto_switch, bool):
            raise HandoffError("auto_switch must be a boolean")
    except HandoffError:
        raise

    redacted = content
    missing_sections = validate_handoff(redacted)
    if missing_sections:
        _record_outcome({
            "operation": "handoff", "source_client": "codex", "target_client": "codex",
            "result": "failure", "failure_stage": "missing_sections",
            "handoff_bytes": len(redacted.encode("utf-8")),
            "redacted_count": redacted_count, "dropped_events": 0, "normalized_fields": 0,
        })
        raise HandoffError("missing canonical sections: " + ", ".join(missing_sections))
    if path is None:
        if overwrite:
            raise HandoffError("overwrite is not supported for central handoffs")
        try:
            central = handoff_store.create_record(workspace, arguments["name"], redacted)
        except handoff_store.HandoffStoreError as exc:
            raise HandoffError(str(exc)) from exc
        result = {"ref": central["ref"], "project_id": central["project_id"], "handoff_id": central["handoff_id"], "name": central["name"], "storage": "central", "valid": True, "redacted_count": redacted_count, "bytes": len(redacted.encode("utf-8"))}
        if auto_switch:
            try:
                control_path, token = _control_credentials()
                write_switch_request(control_path, token, workspace, handoff_ref=central["ref"], telemetry_summary={"handoff_bytes": result["bytes"], "redacted_count": redacted_count})
                result["auto_switch_requested"] = True
            except (ValueError, OSError) as exc:
                result["auto_switch_requested"] = False; result["auto_switch_error"] = str(exc)
        else:
            _record_outcome({"operation":"handoff","source_client":"codex","target_client":"codex","result":"success","failure_stage":"none","handoff_bytes":result["bytes"],"redacted_count":redacted_count,"dropped_events":0,"normalized_fields":0})
        return result
    if path.exists() and not overwrite:
        _record_outcome({
            "operation": "handoff", "source_client": "codex", "target_client": "codex",
            "result": "failure", "failure_stage": "path_exists",
            "handoff_bytes": len(redacted.encode("utf-8")),
            "redacted_count": redacted_count, "dropped_events": 0, "normalized_fields": 0,
        })
        raise HandoffError(
            f"handoff already exists: {_relative(root, path)}; choose a new path or explicitly set overwrite=true"
        )
    try:
        _atomic_write(root, path, redacted, overwrite=overwrite)
    except FileExistsError:
        if overwrite:
            raise
        _record_outcome({
            "operation": "handoff", "source_client": "codex", "target_client": "codex",
            "result": "failure", "failure_stage": "path_exists",
            "handoff_bytes": len(redacted.encode("utf-8")),
            "redacted_count": redacted_count, "dropped_events": 0, "normalized_fields": 0,
        })
        raise HandoffError(
            f"handoff already exists: {_relative(root, path)}; choose a new path or explicitly set overwrite=true"
        )
    except OSError:
        raise
    result = {
        "path": _relative(root, path),
        "valid": True,
        "redacted_count": redacted_count,
        "bytes": len(redacted.encode("utf-8")),
    }
    if auto_switch:
        try:
            control_path, token = _control_credentials()
            write_switch_request(
                control_path,
                token,
                str(root),
                result["path"],
                telemetry_summary={
                    "handoff_bytes": result["bytes"],
                    "redacted_count": redacted_count,
                },
            )
            result["auto_switch_requested"] = True
        except (ValueError, OSError) as exc:
            result["auto_switch_requested"] = False
            result["auto_switch_error"] = str(exc)
            _record_outcome({
                "operation": "handoff", "source_client": "codex", "target_client": "codex",
                "result": "failure", "failure_stage": "control", "handoff_bytes": result["bytes"],
                "redacted_count": redacted_count, "dropped_events": 0, "normalized_fields": 0,
            })
    else:
        _record_outcome({
            "operation": "handoff", "source_client": "codex", "target_client": "codex",
            "result": "success", "failure_stage": "none", "handoff_bytes": result["bytes"],
            "redacted_count": redacted_count, "dropped_events": 0, "normalized_fields": 0,
        })
    return result


def _migrate(arguments: dict[str, Any]) -> dict[str, Any]:
    root = _workspace_root(_require_string(arguments, "workspace"))
    source_client = _require_string(arguments, "source_client").lower()
    target_client = _require_string(arguments, "target_client").lower()
    source_session_id = _require_string(arguments, "source_session_id")
    if source_client not in {"claude", "codex"} or target_client not in {"claude", "codex"}:
        raise HandoffError("migrate mode currently supports only Claude and Codex")
    if source_client == target_client:
        raise HandoffError("migrate mode requires a different target client")

    result = {
        "source_client": source_client,
        "target_client": target_client,
        "source_session_id": source_session_id,
    }
    try:
        control_path, token = _control_credentials()
        write_migration_request(
            control_path,
            token,
            str(root),
            source_client,
            target_client,
            source_session_id,
        )
        result["auto_switch_requested"] = True
    except (ValueError, OSError) as exc:
        result["auto_switch_requested"] = False
        result["auto_switch_error"] = str(exc)
    return result


def _read(arguments: dict[str, Any]) -> dict[str, Any]:
    if "ref" in arguments:
        if "path" in arguments: raise HandoffError("exactly one of path or ref must be provided")
        try: record = handoff_store.read_record(_require_string(arguments, "ref"), _require_string(arguments, "workspace"), arguments.get("scope", "project"))
        except handoff_store.HandoffStoreError as exc: raise HandoffError(str(exc)) from exc
        missing_sections = validate_handoff(record["content"])
        return {"ref": record["ref"], "project_id": record["project_id"], "handoff_id": record["handoff_id"], "name": record["name"], "content": record["content"], "valid": not missing_sections, "missing_sections": missing_sections, "redacted_count": 0, "storage": "central"}
    if "path" not in arguments: raise HandoffError("exactly one of path or ref must be provided")
    root, path = _safe_path(
        _require_string(arguments, "workspace"),
        _require_string(arguments, "path"),
        must_exist=True,
    )
    content, redacted_count = _read_file(root, path)
    missing_sections = validate_handoff(content)
    return {
        "path": _relative(root, path),
        "content": content,
        "valid": not missing_sections,
        "missing_sections": missing_sections,
        "redacted_count": redacted_count,
    }


def _validate(arguments: dict[str, Any]) -> dict[str, Any]:
    if "ref" in arguments:
        if "path" in arguments: raise HandoffError("exactly one of path or ref must be provided")
        try: record = handoff_store.read_record(_require_string(arguments, "ref"), _require_string(arguments, "workspace"), arguments.get("scope", "project"))
        except handoff_store.HandoffStoreError as exc: raise HandoffError(str(exc)) from exc
        missing_sections = validate_handoff(record["content"])
        return {"ref": record["ref"], "project_id": record["project_id"], "handoff_id": record["handoff_id"], "valid": not missing_sections, "missing_sections": missing_sections, "redacted_count": 0, "storage": "central"}
    if "path" not in arguments: raise HandoffError("exactly one of path or ref must be provided")
    root, path = _safe_path(
        _require_string(arguments, "workspace"),
        _require_string(arguments, "path"),
        must_exist=True,
    )
    content, redacted_count = _read_file(root, path)
    missing_sections = validate_handoff(content)
    return {
        "path": _relative(root, path),
        "valid": not missing_sections,
        "missing_sections": missing_sections,
        "redacted_count": redacted_count,
    }


def _list(arguments: dict[str, Any]) -> dict[str, Any]:
    root = _workspace_root(_require_string(arguments, "workspace"))
    storage = arguments.get("storage", "workspace")
    if storage == "central":
        if "directory" in arguments: raise HandoffError("directory is not supported for central storage")
        scope = arguments.get("scope", "project")
        if scope not in {"project", "all"}: raise HandoffError("scope must be project or all")
        limit = arguments.get("limit", 20); offset = arguments.get("offset", 0)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_LIMIT: raise HandoffError(f"limit must be an integer between 1 and {MAX_LIST_LIMIT}")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0: raise HandoffError("offset must be a non-negative integer")
        try: return handoff_store.list_records(str(root), scope, limit, offset)
        except handoff_store.HandoffStoreError as exc: raise HandoffError(str(exc)) from exc
    directory = arguments.get("directory", "handoffs")
    if not isinstance(directory, str) or not directory.strip():
        raise HandoffError("directory must be a non-empty workspace-relative path")
    _, handoff_dir = _safe_path(str(root), directory, allow_directory=True)
    if not handoff_dir.exists():
        return {
            "items": [],
            "count": 0,
            "total_count": 0,
            "offset": 0,
            "has_more": False,
            "next_offset": None,
        }
    if not handoff_dir.is_dir():
        raise HandoffError("directory must identify a directory")
    limit = arguments.get("limit", 20)
    offset = arguments.get("offset", 0)
    if not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_LIMIT:
        raise HandoffError(f"limit must be an integer between 1 and {MAX_LIST_LIMIT}")
    if not isinstance(offset, int) or offset < 0:
        raise HandoffError("offset must be a non-negative integer")
    files = []
    for candidate in handoff_dir.rglob("*.md"):
        resolved = candidate.resolve(strict=False)
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        if resolved.is_file():
            files.append(relative.as_posix())
    files.sort()
    page = files[offset : offset + limit]
    has_more = offset + len(page) < len(files)
    return {
        "items": page,
        "count": len(page),
        "total_count": len(files),
        "offset": offset,
        "has_more": has_more,
        "next_offset": offset + len(page) if has_more else None,
    }


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    suffix = b"..."
    return encoded[: limit - len(suffix)].decode("utf-8", errors="ignore") + "..."


def _markdown_files(directory: Path) -> Iterator[Path]:
    for current, directories, files in os.walk(directory, followlinks=False):
        directories[:] = sorted(
            name for name in directories if not (Path(current) / name).is_symlink()
        )
        for name in sorted(files):
            if name.endswith(".md"):
                yield Path(current) / name


def _search(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments.get("storage") == "central":
        query = _require_string(arguments, "query"); query, _ = redact_secrets(query); needle = query.casefold()
        if len(query.encode("utf-8")) > MAX_SEARCH_QUERY_BYTES: raise HandoffError(f"query exceeds {MAX_SEARCH_QUERY_BYTES} bytes")
        scope = arguments.get("scope", "project"); limit = arguments.get("limit", 20); offset = arguments.get("offset", 0)
        listing = handoff_store.list_records(_require_string(arguments, "workspace"), scope, MAX_SEARCH_FILES, 0)
        matches = []
        scanned_bytes = 0; skipped_count = 0; scan_truncated = listing.get("has_more", False)
        for item in listing["items"][:MAX_SEARCH_FILES]:
            try: record = handoff_store.read_record(item["ref"], _require_string(arguments, "workspace"), "all" if scope == "all" else "project")
            except (handoff_store.HandoffStoreError, OSError): skipped_count += 1; continue
            size = len(record["content"].encode("utf-8"))
            if scanned_bytes + size > MAX_SEARCH_BYTES: scan_truncated = True; break
            scanned_bytes += size
            lines = [{"line": n, "snippet": _truncate_utf8(line.strip(), MAX_SEARCH_SNIPPET_BYTES)} for n,line in enumerate(record["content"].splitlines(), 1) if needle in line.casefold()][:MAX_SEARCH_MATCHES_PER_FILE]
            if lines: matches.append({"ref": item["ref"], "project_id": item["project_id"], "name": item["name"], "matches": lines})
        page = matches[offset:offset + limit]; more = offset + len(page) < len(matches)
        response = {"query": query, "items": page, "count": len(page), "total_count": len(matches) if not scan_truncated else None, "offset": offset, "has_more": more or scan_truncated, "next_offset": offset + len(page) if more else None, "skipped_count": skipped_count, "scanned_files": min(len(listing["items"]), MAX_SEARCH_FILES), "scanned_bytes": scanned_bytes, "scan_truncated": scan_truncated, "output_truncated": False}
        while len(json.dumps(response, ensure_ascii=False).encode()) > MAX_SEARCH_OUTPUT_BYTES and page:
            page.pop(); response["count"] = len(page); response["output_truncated"] = True
        return response
    root = _workspace_root(_require_string(arguments, "workspace"))
    query = _require_string(arguments, "query")
    if len(query.encode("utf-8")) > MAX_SEARCH_QUERY_BYTES:
        raise HandoffError(f"query exceeds {MAX_SEARCH_QUERY_BYTES} bytes")
    query, _ = redact_secrets(query)
    needle = query.casefold()
    _, handoff_dir = _safe_path(str(root), "handoffs", allow_directory=True)
    limit = arguments.get("limit", 20)
    offset = arguments.get("offset", 0)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_LIMIT:
        raise HandoffError(f"limit must be an integer between 1 and {MAX_LIST_LIMIT}")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise HandoffError("offset must be a non-negative integer")

    matches: list[dict[str, Any]] = []
    skipped_count = 0
    scanned_files = 0
    scanned_bytes = 0
    scan_truncated = False
    if handoff_dir.exists():
        if not handoff_dir.is_dir():
            raise HandoffError("handoffs must identify a directory")
        for candidate in _markdown_files(handoff_dir):
            resolved = candidate.resolve(strict=False)
            try:
                resolved.relative_to(handoff_dir)
            except ValueError:
                continue
            if not resolved.is_file():
                continue
            if scanned_files >= MAX_SEARCH_FILES:
                scan_truncated = True
                break
            try:
                file_bytes = resolved.stat().st_size
            except OSError:
                skipped_count += 1
                continue
            if scanned_bytes + file_bytes > MAX_SEARCH_BYTES:
                scan_truncated = True
                break
            scanned_files += 1
            scanned_bytes += file_bytes
            try:
                content, _ = _read_file(root, resolved)
            except (HandoffError, OSError):
                skipped_count += 1
                continue
            file_matches = []
            for line_number, line in enumerate(content.splitlines(), start=1):
                if needle not in line.casefold():
                    continue
                file_matches.append(
                    {
                        "line": line_number,
                        "snippet": _truncate_utf8(line.strip(), MAX_SEARCH_SNIPPET_BYTES),
                    }
                )
                if len(file_matches) >= MAX_SEARCH_MATCHES_PER_FILE:
                    break
            if file_matches:
                matches.append({"path": _relative(root, resolved), "matches": file_matches})

    page = matches[offset : offset + limit]
    has_more = offset + len(page) < len(matches)
    response = {
        "query": query,
        "items": page,
        "count": len(page),
        "total_count": len(matches),
        "offset": offset,
        "has_more": has_more,
        "next_offset": offset + len(page) if has_more else None,
        "skipped_count": skipped_count,
        "scanned_files": scanned_files,
        "scanned_bytes": scanned_bytes,
        "scan_truncated": scan_truncated,
        "output_truncated": False,
    }
    while (
        len(json.dumps(response, ensure_ascii=False, indent=2).encode("utf-8"))
        > MAX_SEARCH_OUTPUT_BYTES
        and len(page) > 1
    ):
        page.pop()
        response["count"] = len(page)
        response["has_more"] = offset + len(page) < len(matches)
        response["next_offset"] = offset + len(page) if response["has_more"] else None
        response["output_truncated"] = True
    return response


def _project(arguments: dict[str, Any]) -> dict[str, Any]:
    workspace = _require_string(arguments, "workspace")
    project_id = arguments.get("project_id")
    if project_id is None:
        return {"project_id": handoff_store.lookup_project(workspace), "registered": handoff_store.lookup_project(workspace) is not None}
    replace = arguments.get("replace", False)
    if not isinstance(replace, bool): raise HandoffError("replace must be a boolean")
    try: previous = handoff_store.associate_project(workspace, _require_string(arguments, "project_id"), replace)
    except handoff_store.HandoffStoreError as exc: raise HandoffError(str(exc)) from exc
    return {"project_id": project_id, "previous_project_id": previous, "replaced": previous not in (None, project_id)}


def _import(arguments: dict[str, Any]) -> dict[str, Any]:
    workspace = _require_string(arguments, "workspace"); source = _require_string(arguments, "path")
    root, path = _safe_path(workspace, source, must_exist=False, allow_directory=True)
    if not path.exists(): raise HandoffError(f"handoff file not found: {_relative(root, path)}")
    try:
        if path.is_dir(): result = handoff_store.import_bundle(workspace, str(path))
        else:
            content, redacted = _read_file(root, path)
            missing = validate_handoff(content)
            if missing: raise HandoffError("missing canonical sections: " + ", ".join(missing))
            name = arguments.get("name", path.name); handoff_store.validate_name(name)
            project = handoff_store.register_project(workspace)
            hid = str(__import__("uuid").uuid5(__import__("uuid").UUID(project), "legacy:" + _relative(root, path) + ":" + __import__("hashlib").sha256(content.encode()).hexdigest()))
            origin = {"project_id": project, "handoff_id": hid, "kind": "legacy", "source_path": _relative(root, path)}
            ref = handoff_store.make_ref(project, hid)
            try:
                existing = handoff_store.read_record(ref, workspace)
                if existing["content"] != content: raise HandoffError("central handoff identity conflict")
                result = existing
                result["idempotent"] = True
            except handoff_store.HandoffStoreError:
                result = handoff_store.publish_record(project, hid, name, content, origin)
    except handoff_store.HandoffStoreError as exc: raise HandoffError(str(exc)) from exc
    return {"ref": result["ref"], "project_id": result["project_id"], "handoff_id": result["handoff_id"], "name": result["name"], "storage": "central", "idempotent": result.get("idempotent", False)}


def _export(arguments: dict[str, Any]) -> dict[str, Any]:
    try: return handoff_store.export_record(_require_string(arguments, "ref"), _require_string(arguments, "workspace"), _require_string(arguments, "directory"))
    except handoff_store.HandoffStoreError as exc: raise HandoffError(str(exc)) from exc


TOOLS = [
    {
        "name": "handoff_create",
        "description": "Create a validated handoff document inside a workspace. Secrets are redacted before writing; existing files are never overwritten unless overwrite=true is explicit. Set auto_switch=true when running under the session-handoff launcher to replace the current client session automatically.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["workspace"],
            "properties": {
                "workspace": {"type": "string", "description": "Absolute workspace directory."},
                "path": {"type": "string", "description": "File path relative to workspace, for example handoffs/2026-08-12-feature.md."},
                "name": {"type": "string", "description": "Display name for an immutable central handoff."},
                "content": {"type": "string", "description": "Complete handoff with all canonical sections."},
                "state": _state_schema(),
                "overwrite": {"type": "boolean", "default": False, "description": "Explicitly allow replacing an existing handoff."},
                "auto_switch": {"type": "boolean", "default": False, "description": "Ask the session-handoff launcher to terminate this client and start a fresh session with the handoff."},
            },
            "oneOf": [
                {"required": ["content"], "not": {"required": ["state"]}},
                {"required": ["state"], "not": {"required": ["content"]}},
            ],
            "allOf": [{"oneOf": [{"required": ["path"], "not": {"required": ["name"]}}, {"required": ["name"], "not": {"required": ["path"]}}]}],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "handoff_migrate",
        "description": "Request a supervised native-session migration from Claude to Codex or Codex to Claude. The launcher stops the source client before conversion and resumes the source session if migration fails.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["workspace", "source_client", "target_client", "source_session_id"],
            "properties": {
                "workspace": {"type": "string", "description": "Absolute workspace directory."},
                "source_client": {"type": "string", "enum": ["claude", "codex"]},
                "target_client": {"type": "string", "enum": ["claude", "codex"]},
                "source_session_id": {"type": "string", "description": "Exact native session/thread id of the active source session."},
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "handoff_read",
        "description": "Read a handoff from inside a workspace. Credential-like values are redacted in the returned content.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["workspace"],
            "properties": {
                "workspace": {"type": "string", "description": "Absolute workspace directory."},
                "path": {"type": "string", "description": "File path relative to workspace."},
                "ref": {"type": "string", "description": "Canonical central handoff reference."},
                "scope": {"type": "string", "enum": ["project", "all"], "default": "project"},
            },
            "allOf": [{"oneOf": [{"required": ["path"], "not": {"required": ["ref"]}}, {"required": ["ref"], "not": {"required": ["path"]}}]}],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "handoff_validate",
        "description": "Validate a handoff's canonical sections without changing the file.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["workspace"],
            "properties": {
                "workspace": {"type": "string", "description": "Absolute workspace directory."},
                "path": {"type": "string", "description": "File path relative to workspace."},
                "ref": {"type": "string", "description": "Canonical central handoff reference."},
                "scope": {"type": "string", "enum": ["project", "all"], "default": "project"},
            },
            "allOf": [{"oneOf": [{"required": ["path"], "not": {"required": ["ref"]}}, {"required": ["ref"], "not": {"required": ["path"]}}]}],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "handoff_list",
        "description": "List Markdown handoffs in a workspace directory with stable pagination.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["workspace"],
            "properties": {
                "workspace": {"type": "string", "description": "Absolute workspace directory."},
                "directory": {"type": "string", "default": "handoffs", "description": "Directory relative to workspace."},
                "storage": {"type": "string", "enum": ["workspace", "central"], "default": "workspace"},
                "scope": {"type": "string", "enum": ["project", "all"], "default": "project"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_LIMIT, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "handoff_search",
        "description": "Search redacted text in Markdown handoffs with bounded, stable pagination.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["workspace", "query"],
            "properties": {
                "workspace": {"type": "string", "description": "Absolute workspace directory."},
                "query": {"type": "string", "minLength": 1, "description": "Literal, case-insensitive text to find."},
                "storage": {"type": "string", "enum": ["workspace", "central"], "default": "workspace"},
                "scope": {"type": "string", "enum": ["project", "all"], "default": "project"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_LIMIT, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "handoff_project", "description": "Inspect or explicitly associate a workspace with a central handoff project.",
        "inputSchema": {"type":"object", "additionalProperties":False, "required":["workspace"], "properties":{"workspace":{"type":"string"},"project_id":{"type":"string"},"replace":{"type":"boolean","default":False}}},
        "annotations": {"readOnlyHint":False,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False},
    },
    {
        "name": "handoff_import", "description": "Copy a legacy handoff or portable bundle into central storage.",
        "inputSchema": {"type":"object", "additionalProperties":False, "required":["workspace","path"], "properties":{"workspace":{"type":"string"},"path":{"type":"string"},"name":{"type":"string"}}},
        "annotations": {"readOnlyHint":False,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False},
    },
    {
        "name": "handoff_export", "description": "Export a central handoff as a portable bundle.",
        "inputSchema": {"type":"object", "additionalProperties":False, "required":["workspace","ref","directory"], "properties":{"workspace":{"type":"string"},"ref":{"type":"string"},"directory":{"type":"string"}}},
        "annotations": {"readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False},
    },
]


def _success(data: dict[str, Any]) -> dict[str, Any]:
    # No tool declares an outputSchema, so the spec's backwards-compatible
    # duplicate in structuredContent only doubles the payload on the wire.
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
    }


def _error(message: str) -> dict[str, Any]:
    data = {"isError": True, "message": message}
    return {
        "isError": True,
        "content": [{"type": "text", "text": message}],
        "structuredContent": data,
    }


def _call_tool(params: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(params, dict):
        return _error("tool call parameters must be a JSON object")
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(name, str) or name not in {tool["name"] for tool in TOOLS}:
        return _error(f"unknown tool: {name}")
    unknown = next((key for key in params if key not in {"name", "arguments"}), None)
    if unknown is not None:
        return _error(f"unknown tool call parameter: {unknown}")
    if not isinstance(arguments, dict):
        return _error("tool arguments must be a JSON object")
    tool = next(tool for tool in TOOLS if tool["name"] == name)
    properties = tool["inputSchema"].get("properties", {})
    unknown_argument = next((key for key in arguments if key not in properties), None)
    if unknown_argument is not None:
        return _error(f"unknown tool argument: {unknown_argument}")
    try:
        handlers = {
            "handoff_create": _create,
            "handoff_migrate": _migrate,
            "handoff_read": _read,
            "handoff_validate": _validate,
            "handoff_list": _list,
            "handoff_search": _search,
            "handoff_project": _project,
            "handoff_import": _import,
            "handoff_export": _export,
        }
        return _success(handlers[name](arguments))
    except (HandoffError, OSError) as exc:
        return _error(str(exc))


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "notifications/initialized" or (isinstance(method, str) and method.startswith("notifications/")):
        return None
    if method == "initialize":
        params = request.get("params", {})
        if not isinstance(params, dict):
            params = {}
        requested = params.get("protocolVersion", DEFAULT_PROTOCOL_VERSION)
        protocol_version = requested if isinstance(requested, str) else DEFAULT_PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": request_id, "result": _call_tool(request.get("params", {}))}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def serve() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            response = handle_request(request)
        except (json.JSONDecodeError, ValueError) as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"invalid JSON-RPC request: {exc}"},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    serve()
