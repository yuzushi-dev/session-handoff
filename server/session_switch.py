"""Supervised client relaunch for automatic handoffs."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

CONTROL_PATH_ENV = "SESSION_HANDOFF_CONTROL"
CONTROL_TOKEN_ENV = "SESSION_HANDOFF_CONTROL_TOKEN"
REQUEST_LIMIT = 64 * 1024


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


def write_switch_request(
    control_path: str,
    token: str,
    workspace: str,
    handoff_path: str,
) -> None:
    """Publish an authenticated request for the supervising launcher."""

    try:
        from .handoff_mcp import HandoffError, _safe_path
    except ImportError:
        from handoff_mcp import HandoffError, _safe_path

    if not isinstance(control_path, str) or not control_path.strip():
        raise HandoffError("automatic session switching is unavailable")
    if not isinstance(token, str) or not token:
        raise HandoffError("automatic session switching is unavailable")
    control = Path(control_path).expanduser()
    if not control.is_absolute() or not control.parent.is_dir():
        raise HandoffError("automatic session switching is unavailable")
    root, path = _safe_path(workspace, handoff_path, must_exist=True)
    _atomic_json_write(
        control,
        {
            "token": token,
            "workspace": str(root),
            "path": path.relative_to(root).as_posix(),
        },
    )


def _read_switch_request(control: Path, token: str) -> dict[str, str] | None:
    try:
        from .handoff_mcp import HandoffError, _safe_path
    except ImportError:
        from handoff_mcp import HandoffError, _safe_path

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
        workspace = payload.get("workspace")
        path = payload.get("path")
        if not isinstance(workspace, str) or not isinstance(path, str):
            raise HandoffError("session switch request is incomplete")
        root, handoff = _safe_path(workspace, path, must_exist=True)
        return {"workspace": str(root), "path": handoff.relative_to(root).as_posix()}
    finally:
        control.unlink(missing_ok=True)


def handoff_prompt(workspace: str, path: str) -> str:
    return f"Resume from {path} in {workspace}."


def _fresh_session_args(client: str, args: list[str]) -> list[str]:
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
    if client == "codex" and "exec" in fresh:
        exec_index = fresh.index("exec")
        if len(fresh) > exec_index + 1 and not fresh[-1].startswith("-"):
            fresh.pop()
    elif client == "claude" and ("-p" in fresh or "--print" in fresh):
        if fresh and not fresh[-1].startswith("-"):
            fresh.pop()
    return fresh


class SessionSupervisor:
    """Run a client and replace it when the MCP server requests a handoff."""

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
    ) -> None:
        if client not in {"codex", "claude"}:
            raise ValueError("client must be codex or claude")
        self.client = client
        self.host_args = list(host_args)
        self.popen = popen
        self.sleep = sleep
        self.temp_dir = temp_dir
        self.poll_interval = poll_interval
        self.executable = executable

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
        executable = self.executable or shutil.which(self.client) or self.client
        host_args = list(self.host_args)
        if self.client == "codex":
            host_args = [
                "-c",
                f"mcp_servers.session-handoff.env.{CONTROL_PATH_ENV}={json.dumps(str(control))}",
                *host_args,
            ]
        base_argv = [executable, *host_args]
        process = self.popen(base_argv, env=env)

        while True:
            try:
                request = _read_switch_request(control, token)
            except (ValueError, OSError):
                request = None
            if request:
                self._terminate(process)
                process = self.popen(
                    [
                        executable,
                        *_fresh_session_args(self.client, host_args),
                        handoff_prompt(request["workspace"], request["path"]),
                    ],
                    env=env,
                    cwd=request["workspace"],
                )
                continue
            status = process.poll()
            if status is not None:
                return status
            self.sleep(self.poll_interval)

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
