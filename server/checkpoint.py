"""Write a small, secret-safe checkpoint before context compaction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

try:
    from .handoff_mcp import redact_secrets
except ImportError:  # direct `python server/checkpoint.py` execution
    from handoff_mcp import redact_secrets


STATE_PATH = Path(".local/state/session-handoff/checkpoints")
EVENTS_FILENAME = "events.jsonl"
MAX_COMMAND_OUTPUT = 12_000
MAX_GOAL_CHARS = 2_000
GIT_TIMEOUT_SECONDS = 1.5
GIT_COLLECTION_BUDGET_SECONDS = 4.5
_SAFE_SESSION = re.compile(r"[^A-Za-z0-9._-]+")


class CheckpointError(ValueError):
    """An invalid lifecycle event that cannot safely produce a checkpoint."""


def _required_string(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CheckpointError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_string(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CheckpointError(f"{name} must be a string or null")
    return value.strip() or None


def _optional_goal(payload: dict[str, Any]) -> str | None:
    value = _optional_string(payload, "goal")
    if value is None or len(value) <= MAX_GOAL_CHARS:
        return value
    return value[:MAX_GOAL_CHARS] + "\n<goal truncated>"


def _event(payload: dict[str, Any]) -> dict[str, str | None]:
    if not isinstance(payload, dict):
        raise CheckpointError("hook input must be an object")
    cwd = _required_string(payload, "cwd")
    session_id = _required_string(payload, "session_id")
    trigger = _required_string(payload, "trigger")
    if trigger not in {"auto", "manual"}:
        raise CheckpointError("trigger must be auto or manual")
    path = Path(cwd).expanduser().resolve()
    if not path.is_dir():
        raise CheckpointError(f"cwd is not a directory: {cwd}")
    return {
        "cwd": str(path),
        "session_id": session_id,
        "trigger": trigger,
        "transcript_path": _optional_string(payload, "transcript_path"),
        "model": _optional_string(payload, "model"),
        "goal": _optional_goal(payload),
    }


def _session_name(session_id: str) -> str:
    safe = _SAFE_SESSION.sub("-", session_id).strip(".-")
    return (safe or "session")[:160]


def _clip(value: str) -> str:
    value = value.strip()
    if len(value) <= MAX_COMMAND_OUTPUT:
        return value or "<none>"
    return value[:MAX_COMMAND_OUTPUT] + "\n<output truncated>"


def _run_git(root: Path, args: list[str], *, deadline: float) -> str:
    timeout = min(GIT_TIMEOUT_SECONDS, deadline - time.monotonic())
    if timeout <= 0:
        return "<unavailable: timeout>"
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"<unavailable: {type(exc).__name__}>"
    if result.returncode:
        return _clip(result.stderr or f"<exit {result.returncode}>")
    return _clip(result.stdout)


def _repository_root(cwd: Path, *, deadline: float) -> tuple[Path, bool]:
    timeout = min(GIT_TIMEOUT_SECONDS, deadline - time.monotonic())
    if timeout <= 0:
        return cwd, False
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return cwd, False
    if result.returncode or not result.stdout.strip():
        return cwd, False
    root = Path(result.stdout.strip()).resolve()
    return (root, root.is_dir())


def _git_state(cwd: Path, *, deadline: float | None = None) -> tuple[Path, dict[str, str]]:
    deadline = deadline or time.monotonic() + GIT_COLLECTION_BUDGET_SECONDS
    root, is_git = _repository_root(cwd, deadline=deadline)
    if not is_git:
        return root, {"repository": "<not a Git repository>"}
    return root, {
        "repository": str(root),
        "branch": _run_git(root, ["branch", "--show-current"], deadline=deadline),
        "head": _run_git(root, ["rev-parse", "HEAD"], deadline=deadline),
        "head_subject": _run_git(root, ["log", "-1", "--format=%s"], deadline=deadline),
        "status": _run_git(root, ["status", "--short", "--branch"], deadline=deadline),
        "diff_stat": _run_git(root, ["diff", "--stat"], deadline=deadline),
        "untracked": _run_git(root, ["ls-files", "--others", "--exclude-standard"], deadline=deadline),
        "changed_paths": _run_git(root, ["status", "--short", "--untracked-files=all"], deadline=deadline),
    }


def _workspace_key(root: Path) -> str:
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
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
        os.chmod(path, 0o600)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def _render(event: dict[str, str | None], root: Path, git: dict[str, str], created_at: str) -> str:
    lines = [
        "## Goal", "", f"Automatic recovery checkpoint for session `{event['session_id']}`.",
        "This is deterministic evidence, not a semantic handoff.",
        f"Explicit user goal: {event['goal'] or '<unavailable from lifecycle hook>'}.", "",
        "## Constraints & Preferences", "", "- Generated by the PreCompact lifecycle hook.",
        "- No tests, model calls, MCP calls, or arbitrary project commands were run.",
        "- Missing information must not be interpreted as completed work.", "", "## Progress", "",
        "### Done", "", f"- Captured before `{event['trigger']}` compaction at `{created_at}`.", "",
        "### In Progress", "", "- Resume from the client transcript and verify the live repository state.", "",
        "### Pending", "", "- None identified.", "", "## Key Decisions", "",
        "- Decision history: unavailable from lifecycle hook.", "", "## Critical Context", "",
        f"- Workspace: `{root}`", f"- Session: `{event['session_id']}`",
        f"- Model: `{event['model'] or '<not provided>'}`",
        f"- Transcript: `{event['transcript_path'] or '<not provided>'}`",
        f"- Captured at: `{created_at}`", "- Tool summary: unavailable from lifecycle hook.",
        "- Plan/TODO cursor: unavailable from lifecycle hook.", "", "## Files Observed", "",
        "- These paths are the current Git worktree state, not a historical edit ledger.",
    ]
    commands = {
        "branch": "git branch --show-current", "head": "git rev-parse HEAD",
        "head_subject": "git log -1 --format=%s", "status": "git status --short --branch",
        "diff_stat": "git diff --stat", "untracked": "git ls-files --others --exclude-standard",
        "changed_paths": "git status --short --untracked-files=all",
    }
    for name, value in git.items():
        command = commands.get(name)
        if command:
            lines.append(f"- Git {name} command: `{command}`")
        lines.extend([f"- Git {name}:", "", "```text", value, "```"])
    lines.extend(["", "## Next Steps", "", "1. Read this checkpoint only as recovery evidence.",
                  "2. Re-check the repository and the client transcript before acting."])
    redacted, _ = redact_secrets("\n".join(lines) + "\n")
    return redacted


def capture_checkpoint(payload: dict[str, Any], *, home: Path | None = None,
                       now: datetime | None = None,
                       deadline: float | None = None) -> dict[str, Any]:
    event = _event(payload)
    home = (home or Path.home()).expanduser().resolve()
    created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    root, git = _git_state(Path(event["cwd"] or "."), deadline=deadline)
    directory = home / STATE_PATH / _workspace_key(root)
    path = directory / f"{_session_name(event['session_id'])}.md"
    content = _render(event, root, git, created_at)
    _atomic_write(path, content)
    checkpoint_bytes = len(content.encode("utf-8"))
    latest = directory / "latest.json"
    _atomic_write(latest, json.dumps({"version": 1, "workspace": str(root),
        "session_id": event["session_id"], "path": str(path), "checkpoint_bytes": checkpoint_bytes,
        "created_at": created_at, "trigger": event["trigger"]}, ensure_ascii=False, indent=2) + "\n")
    return {"path": str(path), "latest_path": str(latest), "workspace": str(root),
            "session_id": event["session_id"] or "", "trigger": event["trigger"] or "",
            "checkpoint_bytes": checkpoint_bytes}


def _workspace_directory(payload: dict[str, Any], home: Path, *,
                         deadline: float | None = None) -> Path | None:
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    deadline = deadline or time.monotonic() + GIT_COLLECTION_BUDGET_SECONDS
    workspace, _ = _repository_root(Path(cwd).expanduser().resolve(), deadline=deadline)
    return home / STATE_PATH / _workspace_key(workspace)


def _latest_pointer(payload: dict[str, Any], home: Path) -> dict[str, Any] | None:
    directory = _workspace_directory(payload, home)
    if directory is None:
        return None
    latest = directory / "latest.json"
    try:
        pointer = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(pointer, dict):
        return None
    path_value = pointer.get("path")
    created_at = pointer.get("created_at")
    if not isinstance(path_value, str) or not isinstance(created_at, str):
        return None
    path = Path(path_value).expanduser().resolve()
    try:
        path.relative_to(latest.parent)
    except ValueError:
        return None
    if not path.is_file():
        return None
    checkpoint_bytes = pointer.get("checkpoint_bytes")
    if isinstance(checkpoint_bytes, bool) or not isinstance(checkpoint_bytes, int):
        try:
            checkpoint_bytes = path.stat().st_size
        except OSError:
            return None
    return {"path": str(path), "created_at": created_at, "checkpoint_bytes": checkpoint_bytes}


def lifecycle_event_path(payload: dict[str, Any], *, home: Path | None = None,
                         deadline: float | None = None) -> Path | None:
    directory = _workspace_directory(
        payload, (home or Path.home()).expanduser().resolve(), deadline=deadline
    )
    return None if directory is None else directory / EVENTS_FILENAME


def _append_lifecycle_event(payload: dict[str, Any], record: dict[str, Any], *, home: Path,
                            deadline: float | None = None) -> Path | None:
    path = lifecycle_event_path(payload, home=home, deadline=deadline)
    if path is None:
        return None
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
    return path


def record_precompact(payload: dict[str, Any], checkpoint: dict[str, Any], *,
                      home: Path | None = None, deadline: float | None = None) -> Path | None:
    event = _event(payload)
    checkpoint_path = checkpoint.get("path")
    checkpoint_bytes = checkpoint.get("checkpoint_bytes")
    if not isinstance(checkpoint_path, str) or not isinstance(checkpoint_bytes, int):
        return None
    record = {"schema_version": 1, "event": "precompact", "session_id": event["session_id"],
              "trigger": event["trigger"], "checkpoint_path": checkpoint_path,
              "checkpoint_bytes": checkpoint_bytes, "injected": False, "injected_bytes": 0,
              "created_at": datetime.now(timezone.utc).isoformat()}
    try:
        return _append_lifecycle_event(
            payload, record, home=(home or Path.home()).expanduser().resolve(), deadline=deadline
        )
    except OSError:
        return None


def record_session_start(payload: dict[str, Any], context: str | None, *, home: Path | None = None) -> Path | None:
    home = (home or Path.home()).expanduser().resolve()
    pointer = _latest_pointer(payload, home)
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        session_id = None
    trigger = payload.get("source")
    if not isinstance(trigger, str) or not trigger.strip():
        trigger = "unknown"
    injected_bytes = len(context.encode("utf-8")) if context else 0
    record = {"schema_version": 1, "event": "session_start", "session_id": session_id,
              "trigger": trigger, "checkpoint_path": pointer["path"] if pointer else None,
              "checkpoint_bytes": pointer["checkpoint_bytes"] if pointer else 0,
              "injected": bool(context), "injected_bytes": injected_bytes,
              "created_at": datetime.now(timezone.utc).isoformat()}
    try:
        return _append_lifecycle_event(payload, record, home=home)
    except OSError:
        return None


def compact_context(payload: dict[str, Any], *, home: Path | None = None) -> str | None:
    if payload.get("source") != "compact":
        return None
    pointer = _latest_pointer(payload, (home or Path.home()).expanduser().resolve())
    if pointer is None:
        return None
    return ("A session-handoff recovery checkpoint was captured before this compaction. "
            "It is non-semantic evidence; verify the live state before relying on it. "
            f"Checkpoint: {pointer['path']} (captured {pointer['created_at']}).")


def main(*, stdin_text: str | None = None, home: Path | None = None,
         stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    try:
        payload = json.loads(sys.stdin.read() if stdin_text is None else stdin_text)
        if not isinstance(payload, dict):
            raise CheckpointError("hook input must be an object")
        if payload.get("hook_event_name") == "PreCompact":
            deadline = time.monotonic() + GIT_COLLECTION_BUDGET_SECONDS
            result = capture_checkpoint(payload, home=home, deadline=deadline)
            record_precompact(payload, result, home=home, deadline=deadline)
            print("{}", file=stdout)
        elif payload.get("hook_event_name") == "SessionStart":
            context = compact_context(payload, home=home)
            record_session_start(payload, context, home=home)
            if context is None:
                print("{}", file=stdout)
            else:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                    "additionalContext": context}}), file=stdout)
        else:
            print("{}", file=stdout)
    except Exception as exc:  # lifecycle hooks must not break compaction
        print(f"session-handoff checkpoint skipped: {exc}", file=stderr)
        print("{}", file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
