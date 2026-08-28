"""Supervised client relaunch for automatic handoffs and session migration."""

from __future__ import annotations

import errno
import fcntl
import hmac
import json
import math
import os
import pty
import secrets
import select
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
REQUEST_LIMIT = 64 * 1024
SUPPORTED_CLIENTS = {"codex", "claude"}
TELEMETRY_PLUGIN_VERSION = PACKAGE_VERSION
TELEMETRY_SUMMARY_FIELDS = frozenset(
    {"handoff_bytes", "redacted_count", "dropped_events", "normalized_fields", "duration_seconds"}
)


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
    }
    if not isinstance(summary, dict) or not set(summary) <= allowed:
        raise ValueError("telemetry summary contains an unauthorized field")
    safe = _safe_numeric_summary(
        {field: summary[field] for field in summary if field in TELEMETRY_SUMMARY_FIELDS}
    )
    event = {
        "schema_version": 1,
        "event": "operation_summary",
        "day_utc": datetime.now(timezone.utc).date().isoformat(),
        "plugin_version": TELEMETRY_PLUGIN_VERSION,
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


def record_terminal_outcome(summary: dict[str, Any]) -> None:
    """Record one validated, aggregate-only outcome before detached upload."""
    try:
        if telemetry.do_not_track_enabled():
            return
        home = Path(os.environ.get("SESSION_HANDOFF_HOME", str(Path.home()))).expanduser()
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
    handoff_path: str,
    *,
    telemetry_summary: dict[str, Any] | None = None,
) -> None:
    """Publish an authenticated request for a fresh-session handoff."""

    try:
        from .handoff_mcp import _safe_path
    except ImportError:
        from handoff_mcp import _safe_path

    control, token = _control_credentials(control_path, token)
    root, path = _safe_path(workspace, handoff_path, must_exist=True)
    payload = {
        "token": token,
        "workspace": str(root),
        "path": path.relative_to(root).as_posix(),
    }
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
    except ImportError:
        from handoff_mcp import HandoffError, _safe_path, _workspace_root

    if not control.exists():
        return None
    try:
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
            if not isinstance(path, str):
                raise HandoffError("session switch request is incomplete")
            root, handoff = _safe_path(workspace, path, must_exist=True)
            request: dict[str, Any] = {
                "mode": "handoff",
                "workspace": str(root),
                "path": handoff.relative_to(root).as_posix(),
            }
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
                "mode": "migrate",
                "workspace": str(root),
                "source_client": source_client,
                "target_client": target_client,
                "source_session_id": source_session_id.strip(),
            }

        raise HandoffError(f"unknown session switch mode: {mode}")
    finally:
        control.unlink(missing_ok=True)


def handoff_prompt(workspace: str, path: str) -> str:
    return f"reference [{path}] riparti da qui"


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
    setting = f"mcp_servers.session-handoff.env.{CONTROL_PATH_ENV}={json.dumps(str(control))}"
    if setting in args:
        return list(args)
    return ["-c", setting, *args]


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
        self._draft_at = time.monotonic() + 0.25
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
        if not self._draft_sent and time.monotonic() >= self._draft_at:
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

    def _launch(
        self,
        client: str,
        executable: str,
        args: list[str],
        env: dict[str, str],
        *,
        cwd: str | None = None,
    ) -> Any:
        return self.popen([executable, *args], env=env, cwd=cwd) if cwd else self.popen(
            [executable, *args], env=env
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
        env.pop(CONTROL_TOKEN_ENV, None)

        current_client = self.client
        current_executable = self._client_executable(current_client) or current_client
        current_args = _with_control_path(current_client, self.host_args, control)
        process = self._launch(current_client, current_executable, current_args, env)
        pending_summary: dict[str, Any] | None = None
        pending_started: float | None = None

        try:
            while True:
                if hasattr(process, "pump"):
                    process.pump()
                try:
                    request = _read_switch_request(control, token)
                except (ValueError, OSError) as exc:
                    print(f"session-handoff: ignored invalid switch request: {exc}", file=sys.stderr)
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
                    self._terminate(process)
                    fresh_args = _fresh_session_args(
                        current_client,
                        current_args,
                        interactive=self.draft,
                    )
                    if self.draft:
                        try:
                            process = _DraftProcess(
                                [current_executable, *fresh_args],
                                env,
                                request["workspace"],
                                handoff_prompt(request["workspace"], request["path"]),
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
                                    handoff_prompt(request["workspace"], request["path"]),
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
                        continue
                    self._terminate(process)
                    started = time.monotonic()
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
                    try:
                        process = self._launch(
                            current_client,
                            current_executable,
                            current_args,
                            env,
                            cwd=workspace,
                        )
                    except (OSError, subprocess.SubprocessError):
                        record_terminal_outcome({
                            **_terminal_summary(pending_summary, pending_started or time.monotonic()),
                            "result": "failure",
                            "failure_stage": "target_resume",
                        })
                        return 1
                    continue

                status = process.poll()
                if status is not None:
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
        with tempfile.TemporaryDirectory(prefix="session-handoff-") as directory:
            return self._run(Path(directory))
