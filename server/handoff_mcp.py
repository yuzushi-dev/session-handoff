#!/usr/bin/env python3
"""Local MCP server for safe, workspace-scoped session handoffs.

The implementation intentionally uses only the Python standard library so the
plugin works immediately in Codex and Claude without a package installation.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    from .session_switch import (
        CONTROL_PATH_ENV,
        CONTROL_TOKEN_ENV,
        write_switch_request,
    )
except ImportError:  # direct `python server/handoff_mcp.py` execution
    from session_switch import CONTROL_PATH_ENV, CONTROL_TOKEN_ENV, write_switch_request


SERVER_NAME = "session-handoff"
SERVER_VERSION = "0.5.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
MAX_CONTENT_BYTES = 2_000_000
MAX_LIST_LIMIT = 100

REQUIRED_SECTIONS = (
    "## Goal",
    "## Constraints & Preferences",
    "## Progress",
    "## Key Decisions",
    "## Critical Context",
    "## Next Steps",
)

_ASSIGNMENT = re.compile(
    r"(?P<key>\b[A-Za-z][A-Za-z0-9_-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|AUTHORIZATION)\b)"
    r"(?P<spacing>\s*)(?P<separator>[:=])(?P<after>\s*)"
    r"(?P<quote>['\"]?)(?P<value>[^\s'\"`;,\)\]]+)(?P=quote)",
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


def _replace_assignment(match: re.Match[str]) -> str:
    value = match.group("value")
    if value.startswith("[REDACTED]"):
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

    redacted = _PRIVATE_KEY.sub(replace_private, text)
    redacted = _BEARER.sub(replace_bearer, redacted)
    redacted = _KNOWN_TOKEN.sub(replace_token, redacted)
    redacted = _ASSIGNMENT.sub(replace_assignment, redacted)
    return redacted, count


def validate_handoff(text: str) -> list[str]:
    """Return canonical sections absent from a handoff document."""

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


def _read_file(root: Path, path: Path) -> tuple[str, int]:
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HandoffError("handoff file must be UTF-8 text") from exc
    redacted, redacted_count = redact_secrets(raw)
    return redacted, redacted_count


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _require_string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise HandoffError(f"{name} must be a non-empty string")
    return value


def _create(arguments: dict[str, Any]) -> dict[str, Any]:
    workspace = _require_string(arguments, "workspace")
    requested_path = _require_string(arguments, "path")
    content = _require_string(arguments, "content")
    root, path = _safe_path(workspace, requested_path)
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise HandoffError(f"content exceeds {MAX_CONTENT_BYTES} bytes")
    overwrite = arguments.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise HandoffError("overwrite must be a boolean")
    auto_switch = arguments.get("auto_switch", False)
    if not isinstance(auto_switch, bool):
        raise HandoffError("auto_switch must be a boolean")

    redacted, redacted_count = redact_secrets(content)
    missing_sections = validate_handoff(redacted)
    if missing_sections:
        raise HandoffError("missing canonical sections: " + ", ".join(missing_sections))

    if path.exists() and not overwrite:
        raise HandoffError(
            f"handoff already exists: {_relative(root, path)}; choose a new path or explicitly set overwrite=true"
        )
    _atomic_write(path, redacted)
    result = {
        "path": _relative(root, path),
        "valid": True,
        "redacted_count": redacted_count,
        "bytes": len(redacted.encode("utf-8")),
    }
    if auto_switch:
        try:
            control_path = os.environ.get(CONTROL_PATH_ENV)
            token = os.environ.get(CONTROL_TOKEN_ENV)
            if control_path:
                token_path = Path(control_path).with_name("token")
                if token_path.is_file():
                    token = token_path.read_text(encoding="utf-8").strip()
            write_switch_request(
                control_path,
                token,
                str(root),
                result["path"],
            )
            result["auto_switch_requested"] = True
        except (ValueError, OSError) as exc:
            result["auto_switch_requested"] = False
            result["auto_switch_error"] = str(exc)
    return result


def _read(arguments: dict[str, Any]) -> dict[str, Any]:
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


TOOLS = [
    {
        "name": "handoff_create",
        "description": "Create a validated handoff document inside a workspace. Secrets are redacted before writing; existing files are never overwritten unless overwrite=true is explicit. Set auto_switch=true when running under the session-handoff launcher to replace the current client session automatically.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["workspace", "path", "content"],
            "properties": {
                "workspace": {"type": "string", "description": "Absolute workspace directory."},
                "path": {"type": "string", "description": "File path relative to workspace, for example handoffs/2026-08-12-feature.md."},
                "content": {"type": "string", "description": "Complete handoff with all canonical sections."},
                "overwrite": {"type": "boolean", "default": False, "description": "Explicitly allow replacing an existing handoff."},
                "auto_switch": {"type": "boolean", "default": False, "description": "Ask the session-handoff launcher to terminate this client and start a fresh session with the handoff."},
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "handoff_read",
        "description": "Read a handoff from inside a workspace. Credential-like values are redacted in the returned content.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["workspace", "path"],
            "properties": {
                "workspace": {"type": "string", "description": "Absolute workspace directory."},
                "path": {"type": "string", "description": "File path relative to workspace."},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "handoff_validate",
        "description": "Validate a handoff's canonical sections without changing the file.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["workspace", "path"],
            "properties": {
                "workspace": {"type": "string", "description": "Absolute workspace directory."},
                "path": {"type": "string", "description": "File path relative to workspace."},
            },
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
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_LIMIT, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]


def _success(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
        "structuredContent": data,
    }


def _error(message: str) -> dict[str, Any]:
    data = {"isError": True, "message": message}
    return {
        "isError": True,
        "content": [{"type": "text", "text": message}],
        "structuredContent": data,
    }


def _call_tool(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(name, str) or name not in {tool["name"] for tool in TOOLS}:
        return _error(f"unknown tool: {name}")
    if not isinstance(arguments, dict):
        return _error("tool arguments must be a JSON object")
    try:
        handlers = {
            "handoff_create": _create,
            "handoff_read": _read,
            "handoff_validate": _validate,
            "handoff_list": _list,
        }
        return _success(handlers[name](arguments))
    except (HandoffError, OSError) as exc:
        return _error(str(exc))


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC request; return None for notifications."""

    method = request.get("method")
    request_id = request.get("id")
    if method == "notifications/initialized" or (isinstance(method, str) and method.startswith("notifications/")):
        return None
    if method == "initialize":
        requested = request.get("params", {}).get("protocolVersion", DEFAULT_PROTOCOL_VERSION)
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
