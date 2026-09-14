#!/usr/bin/env python3
"""Exercise a frozen session-handoff build through its stdio MCP surface."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from benchmark.version_aware import _git_revision, _tree_sha256


class ProductRoundtripError(RuntimeError):
    pass


def _sha256(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def product_identity(root: Path) -> dict[str, str | None]:
    root = root.resolve()
    server = root / "server/handoff_mcp.py"
    if not server.is_file():
        raise ProductRoundtripError(f"product MCP server not found: {server}")
    return {
        "root": str(root),
        "tree_sha256": _tree_sha256(root),
        "git_commit": _git_revision(root),
        "adapter_sha256": _sha256(Path(__file__).read_bytes()),
    }


def _call(root: Path, env: dict[str, str], name: str, arguments: dict[str, Any], timeout: int) -> tuple[dict[str, Any], dict[str, Any]]:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    process = subprocess.Popen(
        [sys.executable, str(root / "server/handoff_mcp.py")],
        cwd=root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, _ = process.communicate(json.dumps(request) + "\n", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise ProductRoundtripError(f"{name} timed out") from exc
    if process.returncode != 0:
        raise ProductRoundtripError(f"{name} MCP process failed")
    try:
        response = json.loads(stdout)
        if not isinstance(response, dict) or response.get("jsonrpc") != "2.0" or response.get("id") != 1:
            raise ProductRoundtripError(f"{name} returned an invalid JSON-RPC response")
        result = response["result"]
        if not isinstance(result, dict):
            raise ProductRoundtripError(f"{name} returned an invalid MCP result")
        if result.get("isError"):
            raise ProductRoundtripError(f"{name} failed: {result['content'][0]['text']}")
        data = json.loads(result["content"][0]["text"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ProductRoundtripError(f"{name} returned an invalid MCP response") from exc
    if not isinstance(data, dict):
        raise ProductRoundtripError(f"{name} returned non-object tool data")
    return response, data


def roundtrip(
    root: Path,
    storage: str,
    isolated_root: Path,
    workspace: Path,
    content: str,
    *,
    timeout: int = 30,
) -> dict[str, Any]:
    if storage not in {"legacy", "central"}:
        raise ProductRoundtripError(f"invalid storage mode: {storage}")
    root = root.resolve()
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    home = isolated_root / "home"
    data = isolated_root / "data"
    state = isolated_root / "state"
    config = isolated_root / "config"
    for directory in (home, data, state, config):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE")
        if key in os.environ
    }
    env.update(
        HOME=str(home),
        XDG_DATA_HOME=str(data),
        XDG_STATE_HOME=str(state),
        XDG_CONFIG_HOME=str(config),
        DO_NOT_TRACK="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    create_arguments: dict[str, Any] = {
        "workspace": str(workspace.resolve()),
        "content": content,
        "auto_switch": False,
    }
    create_arguments["path" if storage == "legacy" else "name"] = "handoffs/pilot.md" if storage == "legacy" else "pilot.md"
    create_response, created = _call(root, env, "handoff_create", create_arguments, timeout)
    identity = created.get("path" if storage == "legacy" else "ref")
    if not isinstance(identity, str) or not identity:
        raise ProductRoundtripError("handoff_create omitted its storage identity")
    read_arguments = {"workspace": str(workspace.resolve()), "path" if storage == "legacy" else "ref": identity}
    read_response, read = _call(root, env, "handoff_read", read_arguments, timeout)
    read_content = read.get("content")
    if not isinstance(read_content, str) or read.get("valid") is not True or read.get("missing_sections"):
        raise ProductRoundtripError("handoff_read returned an invalid canonical handoff")
    create_encoded = json.dumps(create_response, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    read_encoded = json.dumps(read_response, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "storage": storage,
        "create": created,
        "read": read,
        "content": read_content,
        "content_sha256": _sha256(read_content),
        "generated_content_sha256": _sha256(content),
        "create_response": create_response,
        "create_response_sha256": _sha256(create_encoded),
        "read_response": read_response,
        "read_response_sha256": _sha256(read_encoded),
    }
