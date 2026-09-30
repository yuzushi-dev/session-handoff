"""Supervised client relaunch for automatic handoffs and session migration."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
import pty
import re
import secrets
import select
import shlex
import signal
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tty
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from .migration import MigrationError, migrate_session, migration_telemetry_summary
    from . import telemetry
    from .version import PACKAGE_VERSION
except ImportError:
    from migration import MigrationError, migrate_session, migration_telemetry_summary
    import telemetry  # type: ignore[no-redef]
    from version import PACKAGE_VERSION

CONTROL_PATH_ENV = "SESSION_HANDOFF_CONTROL"
CONTROL_TOKEN_ENV = "SESSION_HANDOFF_CONTROL_TOKEN"
CONTROL_PROTOCOL_ENV = "SESSION_HANDOFF_CONTROL_PROTOCOL"
CONTROL_PROTOCOL_VERSION = "2"
CLIENT_ENV = "SESSION_HANDOFF_CLIENT"
REQUEST_LIMIT = 64 * 1024
SUPPORTED_CLIENTS = {"codex", "claude"}
INCOMPLETE_SWITCH_PHASES = {
    "claimed",
    "source_stopped",
    "target_launching",
    "target_started",
    "converting",
    "source_fallback_launching",
    "source_fallback_started",
}
TELEMETRY_PLUGIN_VERSION = PACKAGE_VERSION
TELEMETRY_SUMMARY_FIELDS = frozenset(
    {"handoff_bytes", "redacted_count", "dropped_events", "normalized_fields", "duration_seconds"}
)


def _python_cli_command(*args: str) -> str:
    cli = Path(__file__).resolve().parents[1] / "bin/session-handoff"
    return shlex.join([sys.executable, str(cli), *args])


def _safe_numeric_summary(summary: dict[str, Any] | None) -> dict[str, int | float]:
    if summary is None:
        return {}
    if not isinstance(summary, dict) or not set(summary) <= TELEMETRY_SUMMARY_FIELDS:
        raise ValueError("telemetry summary contains an unauthorized numeric field")
    result: dict[str, int | float] = {}
    for field, value in summary.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < 0
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            raise ValueError("telemetry summary fields must be non-negative numeric values")
        if field != "duration_seconds" and not isinstance(value, int):
            raise ValueError("telemetry counts and bytes must be integers")
        result[field] = value
    return result


def _operation_event(summary: dict[str, Any]) -> dict[str, Any]:
    allowed = TELEMETRY_SUMMARY_FIELDS | {
        "operation",
        "source_client",
        "target_client",
        "result",
        "failure_stage",
        "origin",
    }
    if not isinstance(summary, dict) or not set(summary) <= allowed:
        raise ValueError("telemetry summary contains an unauthorized field")
    origin = summary.get("origin", "real")
    if not isinstance(origin, str) or origin not in telemetry.ORIGINS:
        raise ValueError("telemetry summary contains an invalid origin")
    safe = _safe_numeric_summary(
        {field: summary[field] for field in summary if field in TELEMETRY_SUMMARY_FIELDS}
    )
    event = {
        "schema_version": telemetry.EVENT_SCHEMA_VERSION,
        "event": "operation_summary",
        "day_utc": datetime.now(timezone.utc).date().isoformat(),
        "plugin_version": PACKAGE_VERSION,
        "origin": origin,
        "operation": summary["operation"],
        "source_client": summary["source_client"],
        "target_client": summary["target_client"],
        "result": summary["result"],
        "failure_stage": summary["failure_stage"],
        "duration_bucket": telemetry.bucket_duration(safe.get("duration_seconds")),
        "handoff_bytes_bucket": telemetry.bucket_handoff_bytes(int(safe.get("handoff_bytes", 0))),
        "redaction_bucket": telemetry.bucket_count(int(safe.get("redacted_count", 0))),
        "dropped_events_bucket": telemetry.bucket_count(int(safe.get("dropped_events", 0))),
        "normalized_fields_bucket": telemetry.bucket_count(int(safe.get("normalized_fields", 0))),
    }
    telemetry.validate_event(event)
    return event


def _suite_against_real_home() -> bool:
    """True when a test run would queue its events as real usage.

    Tests isolate collection with SESSION_HANDOFF_HOME. A suite run without it
    writes into the operator's own queue, where a later flush uploads synthetic
    outcomes indistinguishable from real ones.
    """
    return "PYTEST_CURRENT_TEST" in os.environ and "SESSION_HANDOFF_HOME" not in os.environ


def record_terminal_outcome(summary: dict[str, Any]) -> None:
    """Record one validated, aggregate-only outcome before detached upload."""
    try:
        if telemetry.do_not_track_enabled() or _suite_against_real_home():
            return
        home = telemetry._home()
        config = telemetry.load_config(home)
        if config is None or not config["enabled"]:
            return
        event = _operation_event(summary)
        telemetry.increment_counter(event, home=home)
        telemetry.spawn_detached_flush(
            home / telemetry.STATE_PATH / telemetry._QUEUE_NAME,
            telemetry.config_path(home),
        )
    except (KeyError, OSError, TypeError, ValueError, telemetry.TelemetryConfigError):
        return


def _terminal_summary(summary: dict[str, Any], started: float) -> dict[str, Any]:
    result = dict(summary)
    result["duration_seconds"] = max(0.0, time.monotonic() - started)
    return result


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
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
            json.dump(payload, temporary, ensure_ascii=False)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def _control_credentials(control_path: str, token: str) -> tuple[Path, str]:
    try:
        from .handoff_mcp import HandoffError
    except ImportError:
        from handoff_mcp import HandoffError

    if not isinstance(control_path, str) or not control_path.strip():
        raise HandoffError("automatic session switching is unavailable")
    if not isinstance(token, str) or not token:
        raise HandoffError("automatic session switching is unavailable")
    control = Path(control_path).expanduser()
    if not control.is_absolute() or not control.parent.is_dir():
        raise HandoffError("automatic session switching is unavailable")
    return control, token


def write_switch_request(
    control_path: str,
    token: str,
    workspace: str,
    handoff_path: str | None = None,
    *,
    handoff_ref: str | None = None,
    telemetry_summary: dict[str, Any] | None = None,
) -> None:
    """Publish an authenticated request for a fresh-session handoff."""

    try:
        from .handoff_mcp import _safe_path
    except ImportError:
        from handoff_mcp import _safe_path

    control, token = _control_credentials(control_path, token)
    if (handoff_path is None) == (handoff_ref is None): raise ValueError("exactly one of handoff_path or handoff_ref is required")
    if handoff_ref is not None and os.environ.get(CONTROL_PROTOCOL_ENV) != CONTROL_PROTOCOL_VERSION:
        raise ValueError("central handoff switching requires restart of the session-handoff launcher")
    payload = {"token": token, "workspace": str(Path(workspace).expanduser().resolve())}
    payload["request_id"] = secrets.token_hex(16)
    if handoff_ref is not None:
        try:
            from . import handoff_store
        except ImportError:
            import handoff_store  # type: ignore[no-redef]
        try: handoff_store.read_record(handoff_ref, workspace)
        except handoff_store.HandoffStoreError as exc: raise ValueError(str(exc)) from exc
        payload["ref"] = handoff_ref
    else:
        root, path = _safe_path(workspace, handoff_path or "", must_exist=True)
        payload["path"] = path.relative_to(root).as_posix()
    safe_summary = _safe_numeric_summary(telemetry_summary)
    if safe_summary:
        payload["telemetry"] = safe_summary
    _atomic_json_write(control, payload)


def write_migration_request(
    control_path: str,
    token: str,
    workspace: str,
    source_client: str,
    target_client: str,
    source_session_id: str,
) -> None:
    """Publish an authenticated cross-client native-session migration request."""

    try:
        from .handoff_mcp import HandoffError, _workspace_root
    except ImportError:
        from handoff_mcp import HandoffError, _workspace_root

    control, token = _control_credentials(control_path, token)
    root = _workspace_root(workspace)
    if source_client not in SUPPORTED_CLIENTS or target_client not in SUPPORTED_CLIENTS:
        raise HandoffError("migrate mode currently supports only Claude and Codex")
    if source_client == target_client:
        raise HandoffError("migrate mode requires a different target client")
    if not isinstance(source_session_id, str) or not source_session_id.strip():
        raise HandoffError("source_session_id must be a non-empty string")
    _atomic_json_write(
        control,
        {
            "token": token,
            "request_id": secrets.token_hex(16),
            "mode": "migrate",
            "workspace": str(root),
            "source_client": source_client,
            "target_client": target_client,
            "source_session_id": source_session_id.strip(),
        },
    )


def _read_switch_request(control: Path, token: str) -> dict[str, Any] | None:
    try:
        from .handoff_mcp import HandoffError, _safe_path, _workspace_root
        from . import handoff_store
    except ImportError:
        from handoff_mcp import HandoffError, _safe_path, _workspace_root
        import handoff_store  # type: ignore[no-redef]

    if not control.exists():
        return None
    raw = control.read_text(encoding="utf-8")
    if len(raw.encode("utf-8")) > REQUEST_LIMIT:
        raise HandoffError("session switch request is too large")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise HandoffError("session switch request must be an object")
    request_token = payload.get("token")
    if not isinstance(request_token, str) or not hmac.compare_digest(request_token, token):
        raise HandoffError("invalid session switch request")

    mode = payload.get("mode", "handoff")
    workspace = payload.get("workspace")
    if not isinstance(workspace, str):
        raise HandoffError("session switch request is incomplete")

    if mode == "handoff":
        path = payload.get("path")
        ref = payload.get("ref")
        if (path is None) == (ref is None):
            raise HandoffError("session switch request is incomplete")
        root = _workspace_root(workspace)
        request: dict[str, Any] = {
            "request_id": _switch_request_id(payload),
            "mode": "handoff",
            "workspace": str(root),
        }
        if ref is not None:
            if not isinstance(ref, str): raise HandoffError("session switch request is incomplete")
            try: handoff_store.read_record(ref, str(root))
            except handoff_store.HandoffStoreError as exc: raise HandoffError(str(exc)) from exc
            request["ref"] = ref
        else:
            if not isinstance(path, str): raise HandoffError("session switch request is incomplete")
            _, handoff = _safe_path(workspace, path, must_exist=True)
            request["path"] = handoff.relative_to(root).as_posix()
        if "telemetry" in payload:
            request["telemetry"] = _safe_numeric_summary(payload["telemetry"])
        return request

    if mode == "migrate":
        root = _workspace_root(workspace)
        source_client = payload.get("source_client")
        target_client = payload.get("target_client")
        source_session_id = payload.get("source_session_id")
        if source_client not in SUPPORTED_CLIENTS or target_client not in SUPPORTED_CLIENTS:
            raise HandoffError("invalid migration client")
        if source_client == target_client:
            raise HandoffError("migration target must differ from source")
        if not isinstance(source_session_id, str) or not source_session_id.strip():
            raise HandoffError("migration request is missing source session id")
        return {
            "request_id": _switch_request_id(payload),
            "mode": "migrate",
            "workspace": str(root),
            "source_client": source_client,
            "target_client": target_client,
            "source_session_id": source_session_id.strip(),
        }

    raise HandoffError(f"unknown session switch mode: {mode}")


def _switch_request_id(payload: dict[str, Any]) -> str:
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("invalid or missing session switch request id")
    return request_id


def _consume_switch_request(control: Path, request_id: str) -> None:
    try:
        payload = json.loads(control.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and _switch_request_id(payload) == request_id:
            control.unlink(missing_ok=True)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return


def _transition_path(control_dir: Path) -> Path:
    return control_dir / "transition.json"


def _load_transition(control_dir: Path) -> dict[str, Any] | None:
    try:
        state = json.loads(_transition_path(control_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid durable switch state: {exc}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("request_id"), str):
        raise RuntimeError("invalid durable switch state")
    return state


def _write_transition(control_dir: Path, state: dict[str, Any], phase: str) -> dict[str, Any]:
    updated = {**state, "phase": phase, "updated_at": datetime.now(timezone.utc).isoformat()}
    _atomic_json_write(_transition_path(control_dir), updated)
    return updated


def _supervisor_state_root() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".local/state"
    return base.resolve() / "session-handoff" / "supervisors"


def _supervisor_scope(client: str, executable: str | None, host_args: list[str]) -> str:
    material = {
        "client": client,
        "executable": executable,
        "host_args": host_args,
        "cwd": str(Path.cwd().resolve()),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]


def _process_start(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except (OSError, UnicodeError):
        return None
    return fields[21] if len(fields) > 21 else None


def _owner_is_live(metadata: dict[str, Any]) -> bool:
    if metadata.get("active") is not True:
        return False
    pid = metadata.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    expected_start = metadata.get("process_start")
    actual_start = _process_start(pid)
    if isinstance(expected_start, str) and actual_start is not None:
        return hmac.compare_digest(expected_start, actual_start)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _stale_incomplete_transitions(root: Path, scope: str) -> list[Path]:
    try:
        candidates = sorted(root.iterdir())
    except FileNotFoundError:
        return []
    stale: list[Path] = []
    for candidate in candidates:
        metadata = _read_json_object(candidate / "supervisor.json")
        if metadata is None or metadata.get("scope") != scope or _owner_is_live(metadata):
            continue
        transition_path = candidate / "transition.json"
        transition = _read_json_object(transition_path)
        if transition_path.exists() and (
            transition is None or transition.get("phase") in INCOMPLETE_SWITCH_PHASES
        ):
            stale.append(transition_path)
    return stale


def _allocate_supervisor_directory(root: Path, scope: str) -> tuple[Path, dict[str, Any]]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    directory = Path(tempfile.mkdtemp(prefix=f"{scope}-", dir=root))
    os.chmod(directory, 0o700)
    metadata = {
        "schema_version": 1,
        "scope": scope,
        "pid": os.getpid(),
        "process_start": _process_start(os.getpid()),
        "active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_json_write(directory / "supervisor.json", metadata)
    return directory, metadata


def _active_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        **metadata,
        "pid": os.getpid(),
        "process_start": _process_start(os.getpid()),
        "active": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def _finalize_supervisor_directory(directory: Path, metadata: dict[str, Any]) -> None:
    try:
        _atomic_json_write(
            directory / "supervisor.json",
            {
                **metadata,
                "active": False,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except OSError:
        return
    transition_path = directory / "transition.json"
    transition = _read_json_object(transition_path)
    for name in ("token", "switch.json"):
        try:
            (directory / name).unlink(missing_ok=True)
        except OSError:
            pass
    if transition_path.exists() and (
        transition is None or transition.get("phase") in INCOMPLETE_SWITCH_PHASES
    ):
        return
    for name in ("transition.json", "supervisor.json"):
        try:
            (directory / name).unlink(missing_ok=True)
        except OSError:
            return
    try:
        directory.rmdir()
    except OSError:
        pass


def _prompt_quote(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("handoff prompt values must be strings")
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def handoff_prompt(workspace: str, path: str | None = None, ref: str | None = None) -> str:
    quoted_workspace = _prompt_quote(workspace)
    if ref is not None:
        instruction = f"handoff_read(workspace={quoted_workspace}, ref={_prompt_quote(ref)})"
    else:
        if path is None:
            raise ValueError("handoff prompt requires a path or ref")
        quoted_path = _prompt_quote(path)
        instruction = f"handoff_read(workspace={quoted_workspace}, path={quoted_path})"
    return f"Resume task: call {instruction} and proceed with the next steps. Do not create a new handoff."


def _fresh_session_args(
    client: str,
    args: list[str],
    *,
    interactive: bool = False,
) -> list[str]:
    """Remove selectors that would make the relaunch reuse the old session."""

    selectors = {"--resume", "--session-id"}
    if client == "claude":
        selectors.update({"-c", "--continue"})
    else:
        selectors.update({"--last"})
    fresh: list[str] = []
    index = 0
    while index < len(args):
        argument = args[index]
        if client == "codex" and argument == "resume":
            index += 1
            if index < len(args) and not args[index].startswith("-"):
                index += 1
            continue
        if argument in selectors:
            index += 1
            if argument in {"--resume", "--session-id"} and index < len(args):
                index += 1
            continue
        if argument.startswith("--resume=") or argument.startswith("--session-id="):
            index += 1
            continue
        fresh.append(argument)
        index += 1
    codex_command = _codex_command_index(fresh) if client == "codex" else None
    if codex_command is not None:
        exec_index = codex_command
        if len(fresh) > exec_index + 1 and not fresh[-1].startswith("-"):
            fresh.pop()
    elif client == "claude" and ("-p" in fresh or "--print" in fresh):
        if fresh and not fresh[-1].startswith("-"):
            fresh.pop()
    if interactive and client == "codex":
        if codex_command is not None:
            fresh = fresh[:codex_command]
        fresh = _remove_options(
            fresh,
            flags={
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--json",
                "--skip-git-repo-check",
            },
            value_options={"--color", "--output-schema", "-o", "--output-last-message"},
        )
    elif interactive and client == "claude":
        fresh = _remove_options(
            fresh,
            flags={
                "-p",
                "--print",
                "--forward-subagent-text",
                "--include-hook-events",
                "--include-partial-messages",
                "--no-session-persistence",
                "--replay-user-messages",
            },
            value_options={
                "--fallback-model",
                "--input-format",
                "--json-schema",
                "--max-budget-usd",
                "--output-format",
            },
        )
    return fresh


def _codex_command_index(args: list[str]) -> int | None:
    value_options = {
        "-a",
        "-C",
        "-c",
        "-i",
        "-m",
        "-p",
        "-s",
        "--add-dir",
        "--ask-for-approval",
        "--config",
        "--cd",
        "--disable",
        "--enable",
        "--local-provider",
        "--model",
        "--profile",
        "--remote",
        "--remote-auth-token-env",
        "--sandbox",
    }
    index = 0
    while index < len(args):
        argument = args[index]
        if argument in {"exec", "e", "review", "fork"}:
            return index
        option, separator, _ = argument.partition("=")
        if option in value_options and not separator:
            index += 2
        else:
            index += 1
    return None


def _remove_options(
    args: list[str],
    *,
    flags: set[str],
    value_options: set[str],
) -> list[str]:
    filtered: list[str] = []
    skip_next = False
    for argument in args:
        if skip_next:
            skip_next = False
            continue
        option, separator, _ = argument.partition("=")
        if option in flags:
            continue
        if option in value_options:
            if not separator:
                skip_next = True
            continue
        filtered.append(argument)
    return filtered


def _resume_args(client: str, session_id: str) -> list[str]:
    if client == "codex":
        return ["resume", session_id]
    return ["--resume", session_id]


def _with_control_path(client: str, args: list[str], control: Path) -> list[str]:
    if client != "codex":
        return list(args)
    settings = [
        f"mcp_servers.session-handoff.env.{CONTROL_PATH_ENV}={json.dumps(str(control))}",
        f"mcp_servers.session-handoff.env.{CONTROL_PROTOCOL_ENV}={json.dumps(CONTROL_PROTOCOL_VERSION)}",
        f"mcp_servers.session-handoff.env.{CLIENT_ENV}={json.dumps(client)}",
    ]
    prefix = [item for setting in settings if setting not in args for item in ("-c", setting)]
    return [*prefix, *args]


def _managed_launcher(executable: str, client: str) -> Path | None:
    path = Path(executable)
    if client == "claude" and path.name == "claude.session-handoff-original":
        return path.with_name(client)
    if not path.name.startswith(f"{client}.session-handoff-active"):
        return None
    return path.with_name(client)


def _repair_launcher(executable: str, client: str, original: str | None) -> None:
    launcher = _managed_launcher(executable, client)
    if launcher is None or original is None:
        return
    temporary: Path | None = None
    try:
        if launcher.is_symlink():
            pass
        elif launcher.read_text(encoding="utf-8") == original:
            return
        else:
            raise OSError("replacement is not a symlink")
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=launcher.parent,
            prefix=f".{launcher.name}.repair-", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(original)
        temporary.chmod(0o755)
        os.replace(temporary, launcher)
    except (OSError, UnicodeDecodeError) as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        print(
            f"session-handoff: launcher repair failed for {launcher}: {exc}; "
            f"run `{_python_cli_command('setup', '--client', client, '--yes')}`",
            file=sys.stderr,
        )


def _reconcile_claude_target(
    executable: str, launcher: Path | None, original: str | None
) -> None:
    if launcher is None or original is None:
        return
    target = Path(executable)
    try:
        if launcher.read_text(encoding="utf-8") != original:
            return
        resolved = target.resolve(strict=True)
        versions = resolved.parent
        if versions.name != "versions" or versions.parent.name != "claude":
            return
        candidates = []
        for candidate in versions.iterdir():
            if (
                re.fullmatch(r"\d+\.\d+\.\d+", candidate.name)
                and candidate.is_file()
                and os.access(candidate, os.X_OK)
            ):
                candidates.append((tuple(map(int, candidate.name.split("."))), candidate))
        for _, candidate in sorted(candidates, reverse=True):
            try:
                result = subprocess.run(
                    [str(candidate), "--version"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if re.search(rf"\b{re.escape(candidate.name)}\b", result.stdout):
                newest = candidate
                break
        else:
            return
        if newest == resolved:
            return
        descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.repair-", dir=target.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            temporary.unlink()
            temporary.symlink_to(newest)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        print(
            f"session-handoff: Claude target repair failed for {target}: {exc}; "
            f"run `{_python_cli_command('setup', '--client', 'claude', '--yes')}`",
            file=sys.stderr,
        )


class _DraftProcess:
    """Run an interactive client and seed its input without submitting it."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str, draft: str) -> None:
        self.argv = argv
        self.pid, self.master = pty.fork()
        self._status: int | None = None
        self._closed = False
        self._stdin_fd: int | None = None
        self._stdout_fd: int | None = None
        self._stdin_state: list[Any] | None = None
        self._draft = draft.encode("utf-8")
        self._draft_sent = False
        self._resize_requested = False
        self._previous_sigwinch: Any = None
        self._sigwinch_handler: Callable[..., Any] | None = None

        if self.pid == 0:
            try:
                os.chdir(cwd)
                os.execvpe(argv[0], argv, env)
            except BaseException:
                os._exit(127)

        try:
            self._stdout_fd = sys.stdout.fileno()
            if sys.stdin.isatty():
                self._stdin_fd = sys.stdin.fileno()
                self._stdin_state = termios.tcgetattr(self._stdin_fd)
                tty.setraw(self._stdin_fd)
            if self._stdout_fd is not None and os.isatty(self._stdout_fd):
                self._previous_sigwinch = signal.getsignal(signal.SIGWINCH)

                def handle_winch(signum: int, frame: Any) -> None:
                    self._resize_requested = True

                self._sigwinch_handler = handle_winch
                signal.signal(signal.SIGWINCH, handle_winch)
            self._resize()
        except BaseException:
            self.terminate()
            try:
                self.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.kill()
            raise

    def _resize(self) -> None:
        if self._stdout_fd is None or not os.isatty(self._stdout_fd):
            return
        try:
            size = fcntl.ioctl(self._stdout_fd, termios.TIOCGWINSZ, b"\0" * 8)
            rows, columns, _, _ = struct.unpack("HHHH", size)
            if rows and columns:
                fcntl.ioctl(
                    self.master,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, columns, 0, 0),
                )
                os.kill(self.pid, signal.SIGWINCH)
        except (OSError, struct.error):
            pass

    def pump(self) -> None:
        if self._closed:
            return
        if self._resize_requested:
            self._resize_requested = False
            self._resize()
        if not self._draft_sent:
            try:
                terminal_mode = termios.tcgetattr(self.master)
            except OSError:
                terminal_mode = None
            if terminal_mode is not None and not terminal_mode[3] & termios.ICANON:
                os.write(self.master, self._draft)
                self._draft_sent = True
        readers = [self.master]
        if self._stdin_fd is not None:
            readers.append(self._stdin_fd)
        try:
            ready, _, _ = select.select(readers, [], [], 0)
        except OSError:
            return
        if self.master in ready:
            try:
                output = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                output = b""
            if output and self._stdout_fd is not None:
                os.write(self._stdout_fd, output)
        if self._stdin_fd is not None and self._stdin_fd in ready:
            data = os.read(self._stdin_fd, 65536)
            if data:
                os.write(self.master, data)

    def poll(self) -> int | None:
        if self._status is not None:
            return self._status
        child, status = os.waitpid(self.pid, os.WNOHANG)
        if child:
            self._status = os.waitstatus_to_exitcode(status)
            self._close()
        return self._status

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            self.pump()
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.argv, timeout)
            time.sleep(0.01)
        return self._status

    def terminate(self) -> None:
        if self.poll() is None:
            os.kill(self.pid, signal.SIGTERM)

    def kill(self) -> None:
        if self.poll() is None:
            os.kill(self.pid, signal.SIGKILL)

    def close(self) -> None:
        if self.poll() is None:
            self.terminate()
            try:
                self.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.kill()
                self.wait(timeout=3)
        self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._stdin_state is not None and self._stdin_fd is not None:
            termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, self._stdin_state)
        if self._sigwinch_handler is not None:
            signal.signal(signal.SIGWINCH, self._previous_sigwinch)
        if self.master >= 0:
            os.close(self.master)
            self.master = -1


def _telemetry_notice(client: str, stream: Any = None) -> None:
    """Fallback telemetry consent notice, once, on Codex only.

    Client hooks normally ask in chat. The managed Codex launcher retains this
    fallback for installs where the hook is missing or did not run. It prints
    after exit because the full-screen client would otherwise hide the notice.
    """
    stream = sys.stderr if stream is None else stream
    try:
        if client != "codex" or not stream.isatty():
            return
        if telemetry.do_not_track_enabled():
            return
        if telemetry.consent_state(telemetry.load_config()) != "unasked":
            return
        if not telemetry.claim_consent_prompt():
            return
        yes = _python_cli_command("telemetry", "yes")
        no = _python_cli_command("telemetry", "no")
        print(
            "session-handoff telemetry is off by default. Run "
            f"`{yes}` to enable optional telemetry: anonymous aggregate telemetry plus a random per-home installation ID used for registration, daily/version status observations, and managed install/uninstall counts. The registry retains the ID, first/last observation, last version, and uninstall state while the service operates. Reply with exactly one of: `{no}` to decline. "
            f"Details: {telemetry.TELEMETRY_DETAILS_URL}",
            file=stream,
        )
    except Exception:
        return


class SessionSupervisor:
    """Run a client and replace it after handoff or native-session migration requests."""

    def __init__(
        self,
        client: str,
        host_args: list[str],
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        sleep: Callable[[float], None] = time.sleep,
        temp_dir: Path | None = None,
        poll_interval: float = 0.05,
        executable: str | None = None,
        draft: bool = True,
        client_executables: dict[str, str] | None = None,
        migrate: Callable[..., dict[str, Any]] = migrate_session,
    ) -> None:
        if client not in SUPPORTED_CLIENTS:
            raise ValueError("client must be codex or claude")
        self.client = client
        self.host_args = list(host_args)
        self.popen = popen
        self.sleep = sleep
        self.temp_dir = temp_dir
        self.poll_interval = poll_interval
        self.draft = draft
        self.client_executables = dict(client_executables or {})
        if executable:
            self.client_executables[client] = executable
        self.migrate = migrate
        telemetry.session_start_flush()

    def _client_executable(self, client: str) -> str | None:
        return self.client_executables.get(client) or shutil.which(client)

    @staticmethod
    def _launch_environment(client: str, env: dict[str, str]) -> dict[str, str]:
        launch_env = dict(env)
        launch_env[CLIENT_ENV] = client
        return launch_env

    def _launch(
        self,
        client: str,
        executable: str,
        args: list[str],
        env: dict[str, str],
        *,
        cwd: str | None = None,
    ) -> Any:
        launch_env = self._launch_environment(client, env)
        return self.popen([executable, *args], env=launch_env, cwd=cwd) if cwd else self.popen(
            [executable, *args], env=launch_env
        )

    def _run(self, control_dir: Path) -> int:
        control_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(control_dir, 0o700)
        control = control_dir / "switch.json"
        token = secrets.token_urlsafe(32)
        token_file = control_dir / "token"
        token_file.write_text(token, encoding="utf-8")
        os.chmod(token_file, 0o600)
        env = os.environ.copy()
        env[CONTROL_PATH_ENV] = str(control)
        env[CONTROL_PROTOCOL_ENV] = CONTROL_PROTOCOL_VERSION
        env.pop(CONTROL_TOKEN_ENV, None)

        try:
            transition = _load_transition(control_dir)
        except RuntimeError as exc:
            print(f"session-handoff: {exc}", file=sys.stderr)
            return 1
        if transition is not None and transition.get("phase") in (
            INCOMPLETE_SWITCH_PHASES - {"source_stopped"}
        ):
            print(
                "session-handoff: incomplete durable switch state requires recovery; "
                f"destination was {transition.get('phase')}",
                file=sys.stderr,
            )
            return 1

        current_client = self.client
        current_executable = self._client_executable(current_client) or current_client
        managed_launcher = _managed_launcher(current_executable, current_client)
        original_launcher = None
        if managed_launcher is not None:
            try:
                original_launcher = managed_launcher.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                original_launcher = None
        current_args = _with_control_path(current_client, self.host_args, control)
        pending_summary: dict[str, Any] | None = None
        pending_started: float | None = None

        if transition is not None and transition.get("phase") == "source_stopped":
            request = transition.get("request")
            fresh_args = transition.get("fresh_args")
            source_client = transition.get("source_client")
            source_executable = transition.get("source_executable")
            if (
                transition.get("mode") != "handoff"
                or not isinstance(request, dict)
                or request.get("mode") != "handoff"
                or source_client not in SUPPORTED_CLIENTS
                or not isinstance(source_executable, str)
                or not source_executable
                or not isinstance(fresh_args, list)
                or any(not isinstance(item, str) for item in fresh_args)
            ):
                print(
                    "session-handoff: incomplete durable switch state cannot be recovered safely",
                    file=sys.stderr,
                )
                return 1
            current_client = source_client
            current_executable = source_executable
            current_args = list(fresh_args)
            pending_started = time.monotonic()
            transition = _write_transition(control_dir, transition, "target_launching")
            try:
                if self.draft:
                    process = _DraftProcess(
                        [current_executable, *current_args],
                        self._launch_environment(current_client, env),
                        request["workspace"],
                        handoff_prompt(
                            request["workspace"], request.get("path"), request.get("ref")
                        ),
                    )
                else:
                    process = self._launch(
                        current_client,
                        current_executable,
                        [
                            *current_args,
                            handoff_prompt(
                                request["workspace"], request.get("path"), request.get("ref")
                            ),
                        ],
                        env,
                        cwd=request["workspace"],
                    )
            except (KeyError, OSError, subprocess.SubprocessError):
                print(
                    "session-handoff: recovered target launch outcome is unknown; "
                    "refusing a blind retry",
                    file=sys.stderr,
                )
                return 1
            transition = _write_transition(control_dir, transition, "target_started")
            pending_summary = {
                "operation": "handoff",
                "source_client": current_client,
                "target_client": current_client,
                "result": "success",
                "failure_stage": "none",
                "handoff_bytes": 0,
                "redacted_count": 0,
                "dropped_events": 0,
                "normalized_fields": 0,
                **request.get("telemetry", {}),
            }
        else:
            process = self._launch(current_client, current_executable, current_args, env)

        rollback_source: dict[str, str] | None = None

        def _resume_source(
            source_client: str,
            source_executable: str,
            source_session_id: str,
            workspace: str,
            started: float,
        ) -> Any | None:
            nonlocal current_client, current_executable, current_args
            nonlocal pending_summary, pending_started, rollback_source
            rollback_args = _with_control_path(
                source_client,
                _resume_args(source_client, source_session_id),
                control,
            )
            print("session-handoff: target resume failed; resuming source session", file=sys.stderr)
            fallback_summary = {
                "operation": "migrate",
                "source_client": source_client,
                "target_client": current_client,
                "result": "fallback",
                "failure_stage": "target_resume",
                "dropped_events": 0,
                "normalized_fields": 0,
            }
            current_client = source_client
            current_executable = source_executable
            current_args = rollback_args
            rollback_source = None
            try:
                resumed = self._launch(
                    source_client,
                    source_executable,
                    rollback_args,
                    env,
                    cwd=workspace,
                )
            except (OSError, subprocess.SubprocessError):
                print("session-handoff: source session resume failed", file=sys.stderr)
                record_terminal_outcome({
                    **fallback_summary,
                    "result": "failure",
                    "failure_stage": "source_resume",
                    "duration_seconds": max(0.0, time.monotonic() - started),
                })
                return None
            pending_summary = fallback_summary
            pending_started = started
            return resumed


        try:
            while True:
                if hasattr(process, "pump"):
                    process.pump()
                try:
                    request = _read_switch_request(control, token)
                except (ValueError, OSError) as exc:
                    print(f"session-handoff: ignored invalid switch request: {exc}", file=sys.stderr)
                    control.unlink(missing_ok=True)
                    request = None

                if (
                    request is not None
                    and transition is not None
                    and request["request_id"] == transition.get("request_id")
                ):
                    _consume_switch_request(control, request["request_id"])
                    request = None

                if request and request["mode"] == "handoff":
                    started = time.monotonic()
                    if pending_summary is not None:
                        record_terminal_outcome(
                            _terminal_summary(
                                pending_summary, pending_started or time.monotonic()
                            )
                        )
                        pending_summary = None
                        pending_started = None
                    fresh_args = _fresh_session_args(
                        current_client,
                        current_args,
                        interactive=self.draft,
                    )
                    transition = _write_transition(control_dir, {
                        "schema_version": 1,
                        "request_id": request["request_id"],
                        "mode": "handoff",
                        "request": request,
                        "source_client": current_client,
                        "source_executable": current_executable,
                        "fresh_args": fresh_args,
                    }, "claimed")
                    _consume_switch_request(control, request["request_id"])
                    self._terminate(process)
                    transition = _write_transition(control_dir, transition, "source_stopped")
                    rollback_source = None
                    transition = _write_transition(control_dir, transition, "target_launching")
                    if self.draft:
                        try:
                            process = _DraftProcess(
                                [current_executable, *fresh_args],
                                self._launch_environment(current_client, env),
                                request["workspace"],
                                handoff_prompt(request["workspace"], request.get("path"), request.get("ref")),
                            )
                        except (OSError, subprocess.SubprocessError):
                            record_terminal_outcome({
                                "operation": "handoff", "source_client": current_client,
                                "target_client": current_client, "result": "failure",
                                "failure_stage": "target_resume",
                                **request.get("telemetry", {}),
                                "duration_seconds": max(0.0, time.monotonic() - started),
                            })
                            return 1
                    else:
                        try:
                            process = self._launch(
                                current_client,
                                current_executable,
                                [
                                    *fresh_args,
                                    handoff_prompt(request["workspace"], request.get("path"), request.get("ref")),
                                ],
                                env,
                                cwd=request["workspace"],
                            )
                        except (OSError, subprocess.SubprocessError):
                            record_terminal_outcome({
                                "operation": "handoff", "source_client": current_client,
                                "target_client": current_client, "result": "failure",
                                "failure_stage": "target_resume",
                                **request.get("telemetry", {}),
                                "duration_seconds": max(0.0, time.monotonic() - started),
                            })
                            return 1
                    transition = _write_transition(control_dir, transition, "target_started")
                    pending_summary = {
                        "operation": "handoff",
                        "source_client": current_client,
                        "target_client": current_client,
                        "result": "success",
                        "failure_stage": "none",
                        "handoff_bytes": 0,
                        "redacted_count": 0,
                        "dropped_events": 0,
                        "normalized_fields": 0,
                        **request.get("telemetry", {}),
                    }
                    pending_started = started
                    current_args = fresh_args
                    continue

                if request and request["mode"] == "migrate":
                    if pending_summary is not None:
                        record_terminal_outcome(
                            _terminal_summary(
                                pending_summary, pending_started or time.monotonic()
                            )
                        )
                        pending_summary = None
                        pending_started = None
                    rollback_source = None
                    source_client = request["source_client"]
                    target_client = request["target_client"]
                    source_session_id = request["source_session_id"]
                    workspace = request["workspace"]

                    if source_client != current_client:
                        print(
                            "session-handoff: migration request source does not match the active client",
                            file=sys.stderr,
                        )
                        record_terminal_outcome({
                            "operation": "migrate", "source_client": source_client,
                            "target_client": target_client, "result": "failure",
                            "failure_stage": "validation",
                        })
                        _consume_switch_request(control, request["request_id"])
                        continue
                    target_executable = self._client_executable(target_client)
                    if not target_executable:
                        print(
                            f"session-handoff: target client executable not found: {target_client}",
                            file=sys.stderr,
                        )
                        record_terminal_outcome({
                            "operation": "migrate", "source_client": source_client,
                            "target_client": target_client, "result": "failure",
                            "failure_stage": "control",
                        })
                        _consume_switch_request(control, request["request_id"])
                        continue
                    transition = _write_transition(control_dir, {
                        "schema_version": 1,
                        "request_id": request["request_id"],
                        "mode": "migrate",
                        "request": request,
                        "source_client": source_client,
                        "source_executable": current_executable,
                    }, "claimed")
                    _consume_switch_request(control, request["request_id"])
                    self._terminate(process)
                    transition = _write_transition(control_dir, transition, "source_stopped")
                    source_executable = current_executable
                    started = time.monotonic()
                    transition = _write_transition(control_dir, transition, "converting")
                    try:
                        migration = self.migrate(
                            source_client,
                            target_client,
                            source_session_id,
                            workspace,
                        )
                    except (MigrationError, OSError, subprocess.SubprocessError) as exc:
                        print(
                            f"session-handoff: migration failed; resuming source session: {exc}",
                            file=sys.stderr,
                        )
                        rollback_args = _with_control_path(
                            source_client,
                            _resume_args(source_client, source_session_id),
                            control,
                        )
                        fallback_summary = {
                            "operation": "migrate",
                            "source_client": source_client,
                            "target_client": target_client,
                            "result": "fallback",
                            "failure_stage": "conversion",
                            "dropped_events": 0,
                            "normalized_fields": 0,
                            "duration_seconds": 0,
                        }
                        try:
                            transition = _write_transition(
                                control_dir, transition, "source_fallback_launching"
                            )
                            process = self._launch(
                                source_client,
                                current_executable,
                                rollback_args,
                                env,
                                cwd=workspace,
                            )
                        except (OSError, subprocess.SubprocessError):
                            record_terminal_outcome({
                                **fallback_summary,
                                "result": "failure",
                                "failure_stage": "source_resume",
                                "duration_seconds": max(0.0, time.monotonic() - started),
                            })
                            return 1
                        transition = _write_transition(
                            control_dir, transition, "source_fallback_started"
                        )
                        pending_summary = fallback_summary
                        pending_started = started
                        current_args = rollback_args
                        continue

                    summary = {
                        "source": source_client,
                        "target": target_client,
                        "session_id": migration["session_id"],
                        "dropped_events": migration.get("dropped_events", {}),
                        "context_loss": migration.get("context_loss", {}),
                        "warnings": migration.get("warnings", []),
                    }
                    print(
                        "session-handoff: migration complete "
                        + json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                        file=sys.stderr,
                    )
                    current_client = target_client
                    current_executable = target_executable
                    current_args = _with_control_path(
                        target_client,
                        _resume_args(target_client, migration["session_id"]),
                        control,
                    )
                    pending_summary = {
                        "operation": "migrate",
                        "source_client": source_client,
                        "target_client": target_client,
                        "result": "success",
                        "failure_stage": "none",
                        **migration_telemetry_summary(migration),
                    }
                    pending_started = started
                    rollback_source = {
                        "client": source_client,
                        "executable": source_executable,
                        "session_id": source_session_id,
                        "workspace": workspace,
                    }
                    transition = _write_transition(control_dir, transition, "target_launching")
                    try:
                        process = self._launch(
                            current_client,
                            current_executable,
                            current_args,
                            env,
                            cwd=workspace,
                        )
                    except (OSError, subprocess.SubprocessError):
                        source = rollback_source
                        if source is None:
                            return 1
                        process = _resume_source(
                            source["client"], source["executable"], source["session_id"], source["workspace"], started
                        )
                        if process is None:
                            return 1
                    if current_client == target_client:
                        transition = _write_transition(control_dir, transition, "target_started")
                    continue

                status = process.poll()
                if status is not None:
                    if status != 0 and rollback_source is not None:
                        source = rollback_source
                        process = _resume_source(
                            source["client"],
                            source["executable"],
                            source["session_id"],
                            source["workspace"],
                            pending_started or time.monotonic(),
                        )
                        if process is None:
                            return 1
                        continue
                    if pending_summary is not None:
                        terminal = _terminal_summary(
                            pending_summary, pending_started or time.monotonic()
                        )
                        if status != 0 and terminal["result"] == "success":
                            terminal["result"] = "failure"
                            terminal["failure_stage"] = "target_resume"
                        elif status != 0 and terminal["result"] == "fallback":
                            terminal["result"] = "failure"
                            terminal["failure_stage"] = "source_resume"
                        record_terminal_outcome(terminal)
                        if transition is not None:
                            transition = _write_transition(
                                control_dir,
                                transition,
                                "completed" if status == 0 else "failed",
                            )
                    _repair_launcher(current_executable, current_client, original_launcher)
                    if current_client == "claude":
                        _reconcile_claude_target(
                            current_executable,
                            _managed_launcher(current_executable, current_client),
                            original_launcher,
                        )
                    return status
                self.sleep(self.poll_interval)
        finally:
            if isinstance(process, _DraftProcess):
                process.close()

    @staticmethod
    def _terminate(process: Any) -> None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def run(self) -> int:
        if self.temp_dir is not None:
            return self._run(self.temp_dir)
        root = _supervisor_state_root()
        scope = _supervisor_scope(
            self.client,
            self.client_executables.get(self.client),
            self.host_args,
        )
        stale = _stale_incomplete_transitions(root, scope)
        if stale:
            if len(stale) != 1:
                print(
                    "session-handoff: multiple interrupted switches require manual recovery: "
                    + ", ".join(str(path) for path in stale),
                    file=sys.stderr,
                )
                return 1
            transition_path = stale[0]
            transition = _read_json_object(transition_path)
            metadata_path = transition_path.parent / "supervisor.json"
            metadata = _read_json_object(metadata_path)
            if (
                transition is not None
                and transition.get("phase") == "source_stopped"
                and transition.get("mode") == "handoff"
                and metadata is not None
            ):
                lock_path = transition_path.parent / "recovery.lock"
                try:
                    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                except OSError:
                    return 1
                acquired = False
                try:
                    try:
                        fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except BlockingIOError:
                        print(
                            "session-handoff: interrupted switch recovery is already active",
                            file=sys.stderr,
                        )
                        return 1
                    transition = _read_json_object(transition_path)
                    metadata = _read_json_object(metadata_path)
                    if (
                        transition is None
                        or transition.get("phase") != "source_stopped"
                        or transition.get("mode") != "handoff"
                        or metadata is None
                        or metadata.get("scope") != scope
                        or _owner_is_live(metadata)
                    ):
                        return 1
                    metadata = _active_metadata(metadata)
                    _atomic_json_write(metadata_path, metadata)
                    try:
                        return self._run(transition_path.parent)
                    finally:
                        _finalize_supervisor_directory(transition_path.parent, metadata)
                finally:
                    if acquired:
                        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                    os.close(lock_descriptor)
                    if acquired and not transition_path.exists() and not metadata_path.exists():
                        try:
                            lock_path.unlink(missing_ok=True)
                            transition_path.parent.rmdir()
                        except OSError:
                            pass
            print(
                "session-handoff: an interrupted switch has durable state at "
                f"{transition_path}; inspect the recorded phase before manual recovery",
                file=sys.stderr,
            )
            return 1
        directory, metadata = _allocate_supervisor_directory(root, scope)
        try:
            return self._run(directory)
        finally:
            _finalize_supervisor_directory(directory, metadata)
