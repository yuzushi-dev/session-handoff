"""Provider-free harness for Claude Code's native session operations."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from benchmark.claude_seed import native_messages


PINNED_BINARY = Path("/home/cristina/.local/share/claude/versions/2.1.263")
PINNED_VERSION = "2.1.263 (Claude Code)"
NATIVE_CWD = "/mnt/work"
NATIVE_HOME = "/mnt/native"


class ClaudeNativeError(RuntimeError):
    """Claude's native session operation could not be proved safely."""


class ClaudeNativeAdapter:
    def __init__(
        self,
        binary: str | Path,
        workspace: str | Path,
        state_root: str | Path,
        base_url: str,
        timeout: int = 180,
        socat_binary: str | Path | None = None,
    ) -> None:
        try:
            self.binary = Path(binary).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ClaudeNativeError("pinned Claude binary is unavailable") from exc
        if self.binary != PINNED_BINARY:
            raise ClaudeNativeError("pinned Claude binary does not resolve to 2.1.263")
        version = subprocess.run(
            [str(self.binary), "--version"],
            text=True,
            capture_output=True,
            check=False,
        )
        if version.returncode != 0 or version.stdout.strip() != PINNED_VERSION:
            raise ClaudeNativeError("pinned Claude binary version assertion failed")

        self.workspace = Path(workspace).expanduser().resolve()
        if not self.workspace.is_dir():
            raise ClaudeNativeError("workspace must be an existing directory")
        self.state_root = Path(state_root).expanduser().resolve()
        if (
            self.state_root == self.workspace
            or self.state_root in self.workspace.parents
            or self.workspace in self.state_root.parents
        ):
            raise ClaudeNativeError("native state must be outside the workspace")
        hidden_source_roots = (Path.home().resolve(), Path("/tmp"))
        if not any(root in self.state_root.parents for root in hidden_source_roots):
            raise ClaudeNativeError("native state source must be beneath HOME or /tmp")
        endpoint = urlsplit(base_url)
        if (
            endpoint.scheme != "http"
            or endpoint.hostname not in {"127.0.0.1", "::1", "localhost"}
            or endpoint.username is not None
            or endpoint.password is not None
        ):
            raise ClaudeNativeError("base URL must be a loopback HTTP endpoint")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise ClaudeNativeError("timeout must be a positive integer")
        if shutil.which("bwrap") is None:
            raise ClaudeNativeError("bubblewrap is required")
        self.socat_binary = None
        socat_candidate = socat_binary or shutil.which("socat")
        if socat_candidate is not None:
            try:
                resolved_socat = Path(socat_candidate).expanduser().resolve(strict=True)
            except OSError as exc:
                raise ClaudeNativeError("socat binary is unavailable") from exc
            if not resolved_socat.is_file() or not os.access(resolved_socat, os.X_OK):
                raise ClaudeNativeError("socat binary is not executable")
            self.socat_binary = resolved_socat
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._entered = False
        self._seed_usage: dict[str, str] = {}

    def __enter__(self) -> "ClaudeNativeAdapter":
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_root.chmod(0o700)
        self._entered = True
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self._entered = False

    def seed(self, conversation: object) -> str:
        self._require_entered()
        if isinstance(conversation, dict):
            try:
                conversation = native_messages(conversation)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ClaudeNativeError("structured conversation is invalid") from exc
        messages = _validated_messages(conversation)
        session_id = str(uuid.uuid4())
        project = re.sub(r"[^A-Za-z0-9_-]", "-", NATIVE_CWD)
        transcript = self.state_root / "projects" / project / f"{session_id}.jsonl"
        transcript.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent: str | None = None
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        records = []
        estimated_context_bytes = 0
        for index, item in enumerate(messages):
            record_id = str(uuid.uuid5(uuid.UUID(session_id), str(index)))
            message: dict[str, Any] = {
                "role": item["role"],
                "content": item["content"],
            }
            if item["role"] == "assistant":
                message.update(
                    {
                        "id": f"msg_seed_{index}",
                        "type": "message",
                        "model": "claude-sonnet-4-6",
                        "usage": {
                            "input_tokens": max(1, estimated_context_bytes // 4),
                            "output_tokens": max(
                                1,
                                len(
                                    json.dumps(
                                        item["content"], ensure_ascii=False
                                    ).encode()
                                )
                                // 4,
                            ),
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    }
                )
            estimated_context_bytes += len(
                json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
            )
            records.append(
                {
                    "parentUuid": parent,
                    "isSidechain": False,
                    "userType": "external",
                    "cwd": NATIVE_CWD,
                    "sessionId": session_id,
                    "version": "2.1.263",
                    "gitBranch": "",
                    "uuid": record_id,
                    "timestamp": timestamp,
                    "type": item["role"],
                    "message": message,
                }
            )
            parent = record_id
        descriptor = os.open(transcript, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                )
                handle.write("\n")
        self._seed_usage[session_id] = "estimated_serialized_utf8_bytes_div_4"
        return session_id

    def turn(
        self,
        session_id: str,
        prompt: str,
        write: bool = False,
        fork: bool = False,
    ) -> dict[str, Any]:
        self._require_entered()
        _valid_session_id(session_id)
        if not isinstance(prompt, str) or not prompt:
            raise ClaudeNativeError("prompt must be non-empty text")
        if not isinstance(write, bool) or not isinstance(fork, bool):
            raise ClaudeNativeError("write and fork must be booleans")
        return self._invoke(session_id, prompt, write=write, fork=fork)[0]

    def compact(self, session_id: str) -> dict[str, Any]:
        self._require_entered()
        _valid_session_id(session_id)
        result, events = self._invoke(
            session_id,
            "/compact",
            write=False,
            fork=False,
            followup_prompts=["/context"],
        )
        boundary = next(
            (
                event
                for event in events
                if event.get("type") == "system"
                and event.get("subtype") == "compact_boundary"
            ),
            None,
        )
        if boundary is not None and not self._compact_boundary_persisted(
            session_id, boundary
        ):
            raise ClaudeNativeError("compact boundary was not persisted")
        metadata = boundary.get("compact_metadata") if boundary is not None else None
        dropped = (
            metadata.get("cumulative_dropped_tokens")
            if isinstance(metadata, dict)
            else None
        )
        actual_compaction = isinstance(dropped, int) and dropped > 0
        return {
            **result,
            "actual_compaction": actual_compaction,
            "event": boundary,
        }

    def _invoke(
        self,
        session_id: str,
        prompt: str,
        *,
        write: bool,
        fork: bool,
        followup_prompts: list[str] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if write and self.socat_binary is None:
            raise ClaudeNativeError("socat is required for the write-mode sandbox")
        settings = {
            "permissions": {
                "deny": ["Read(//mnt/native/**)"],
                "blockReadsOutsideWorkingDirectories": True,
            },
        }
        if write:
            settings["sandbox"] = {
                "enabled": True,
                "failIfUnavailable": True,
                "autoAllowBashIfSandboxed": True,
                "allowUnsandboxedCommands": False,
                "filesystem": {
                    "denyRead": [NATIVE_HOME, str(Path.home()), "/tmp"],
                },
                "network": {
                    "allowedDomains": [],
                    "strictAllowlist": True,
                    "allowAllUnixSockets": False,
                    "allowLocalBinding": False,
                },
            }
        tools = "Read,Edit,Write,Bash" if write else ""
        permission_mode = "acceptEdits" if write else "dontAsk"
        command = [
            "/mnt/bin/claude",
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--safe-mode",
            "--bare",
            "--no-chrome",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--settings",
            json.dumps(settings, separators=(",", ":")),
            "--permission-mode",
            permission_mode,
            "--permission-prompts",
            "none",
            "--tools",
            tools,
            "--effort",
            "xhigh",
            "--model",
            "gpt-5.6-luna",
            "--resume",
            session_id,
        ]
        if fork:
            command.append("--fork-session")
        if followup_prompts:
            command.extend(["--input-format", "stream-json"])
        invocation = [
            shutil.which("bwrap") or "bwrap",
            "--die-with-parent",
            "--unshare-pid",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            str(Path.home()),
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/mnt",
            "--dir",
            "/mnt/bin",
            "--ro-bind",
            str(self.binary),
            "/mnt/bin/claude",
            *(
                ["--ro-bind", str(self.socat_binary), "/mnt/bin/socat"]
                if self.socat_binary is not None
                else []
            ),
            "--dir",
            NATIVE_CWD,
            "--bind" if write else "--ro-bind",
            str(self.workspace),
            NATIVE_CWD,
            "--dir",
            NATIVE_HOME,
            "--bind",
            str(self.state_root),
            NATIVE_HOME,
            "--chdir",
            NATIVE_CWD,
            "--",
            *command,
        ]
        env = {
            "HOME": NATIVE_HOME,
            "CLAUDE_CONFIG_DIR": NATIVE_HOME,
            "ANTHROPIC_API_KEY": "provider-free-local-test",
            "ANTHROPIC_BASE_URL": self.base_url,
            "PATH": "/mnt/bin:/usr/local/bin:/usr/bin:/bin",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                invocation,
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            stdin = prompt
            if followup_prompts:
                stdin = "\n".join(
                    json.dumps(
                        {
                            "type": "user",
                            "message": {"role": "user", "content": item},
                        },
                        separators=(",", ":"),
                    )
                    for item in [prompt, *followup_prompts]
                )
                stdin += "\n"
            stdout, _stderr = process.communicate(stdin, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise ClaudeNativeError("Claude native operation timed out") from exc
        except OSError as exc:
            raise ClaudeNativeError("Claude native operation could not start") from exc
        if process.returncode != 0:
            raise ClaudeNativeError(
                f"Claude native operation failed with exit status {process.returncode}"
            )
        events = _json_events(stdout)
        final = next(
            (event for event in reversed(events) if event.get("type") == "result"),
            None,
        )
        if final is None:
            raise ClaudeNativeError("Claude native operation returned no result")
        output = final.get("result")
        returned_id = final.get("session_id")
        if not isinstance(output, str) or not isinstance(returned_id, str):
            raise ClaudeNativeError("Claude native result is malformed")
        completed = (
            final.get("subtype") == "success"
            and final.get("is_error") is not True
            and final.get("terminal_reason") in {None, "completed"}
        )
        tool_items = []
        for event in events:
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            tool_items.extend(
                block
                for block in content
                if isinstance(block, dict)
                and block.get("type") in {"tool_use", "tool_result"}
            )
        return (
            {
                "completed": completed,
                "output": output,
                "session_id": returned_id,
                "tool_items": tool_items,
                "usage": final.get("usage")
                if isinstance(final.get("usage"), dict)
                else {},
                "seed_usage": self._seed_usage.get(session_id),
            },
            events,
        )

    def _require_entered(self) -> None:
        if not self._entered:
            raise ClaudeNativeError("adapter must be used as a context manager")

    def _compact_boundary_persisted(
        self, session_id: str, boundary: dict[str, Any]
    ) -> bool:
        project = re.sub(r"[^A-Za-z0-9_-]", "-", NATIVE_CWD)
        transcript = self.state_root / "projects" / project / f"{session_id}.jsonl"
        boundary_id = boundary.get("uuid")
        if not isinstance(boundary_id, str):
            return False
        try:
            records = [json.loads(line) for line in transcript.read_text().splitlines()]
        except (OSError, json.JSONDecodeError):
            return False
        boundary_indexes = [
            index
            for index, record in enumerate(records)
            if isinstance(record, dict)
            and record.get("uuid") == boundary_id
            and record.get("subtype") == "compact_boundary"
        ]
        return bool(boundary_indexes) and any(
            isinstance(record, dict) and record.get("isCompactSummary") is True
            for record in records[boundary_indexes[-1] + 1 :]
        )


def _valid_session_id(session_id: str) -> None:
    try:
        uuid.UUID(session_id)
    except (ValueError, TypeError) as exc:
        raise ClaudeNativeError("session_id must be a UUID") from exc


def _validated_messages(conversation: object) -> list[dict[str, Any]]:
    if not isinstance(conversation, list) or not conversation:
        raise ClaudeNativeError("conversation must be a non-empty message list")
    result = []
    expected = "user"
    pending_tools: set[str] = set()
    for item in conversation:
        if not isinstance(item, dict) or item.get("role") != expected:
            raise ClaudeNativeError("conversation roles must alternate from user")
        content = item.get("content")
        blocks = (
            [{"type": "text", "text": content}] if isinstance(content, str) else content
        )
        if not isinstance(blocks, list) or not blocks:
            raise ClaudeNativeError("message content must be non-empty")
        for block in blocks:
            if not isinstance(block, dict):
                raise ClaudeNativeError("message content blocks must be objects")
            kind = block.get("type")
            if kind == "text":
                if not isinstance(block.get("text"), str):
                    raise ClaudeNativeError("text blocks require text")
            elif kind == "tool_use" and expected == "assistant":
                tool_id = block.get("id")
                if (
                    not isinstance(tool_id, str)
                    or not tool_id
                    or not isinstance(block.get("name"), str)
                    or not isinstance(block.get("input"), dict)
                ):
                    raise ClaudeNativeError("tool_use block is malformed")
                pending_tools.add(tool_id)
            elif kind == "tool_result" and expected == "user":
                tool_id = block.get("tool_use_id")
                if tool_id not in pending_tools:
                    raise ClaudeNativeError("tool_result has no matching tool_use")
                pending_tools.remove(tool_id)
            else:
                raise ClaudeNativeError("unsupported message content block")
        result.append({"role": expected, "content": blocks})
        expected = "assistant" if expected == "user" else "user"
    if pending_tools:
        raise ClaudeNativeError("conversation has unresolved tool_use blocks")
    return result


def _json_events(stdout: str) -> list[dict[str, Any]]:
    events = []
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ClaudeNativeError("Claude emitted invalid stream JSON") from exc
        if not isinstance(item, dict):
            raise ClaudeNativeError("Claude emitted a non-object stream event")
        events.append(item)
    return events
