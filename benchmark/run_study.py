#!/usr/bin/env python3
"""Plan or execute one isolated context-fidelity study run."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark.native_seed import seed_native_session
from benchmark.score import validate_study_manifest
from server.handoff_state import redact_state, render_state, validate_state
from server.handoff_mcp import redact_secrets, validate_handoff
from server.migration import MigrationError, migrate_session


CLIENTS = ("claude", "codex")
CONDITIONS = ("full", "handoff", "migrate", "oracle")
HANDOFF_FORMATS = ("markdown-v1", "state-v1")
DEFAULT_HANDOFF_FORMAT = "markdown-v1"
PROMPT_VERSION = 1
BASE_ENV = {
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
}
AUTH_ENV = {
    "claude": {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"},
    "codex": {"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"},
}
CONTINUATION_PROMPT = (
    "Continue the task from the supplied authoritative state. Work in the "
    "repository, make the required change, and run the verification tests. "
    "Do not merely describe the change."
)
HANDOFF_PROMPT = """Create a semantic implementation-state handoff from the synthetic transcript below.
Return Markdown only. Preserve current constraints, authoritative decisions, rejected attempts,
completed and pending work, exact paths/tests, and the next safe action. Exclude stale noise.
Use exactly these headings:

## Goal
## Constraints & Preferences
## Progress
### Done
### In Progress
### Pending
## Key Decisions
## Critical Context
## Next Steps

Synthetic transcript:

"""
STATE_HANDOFF_PROMPT = """Create a semantic implementation-state handoff in state-v1 format from the synthetic transcript below.
Return exactly one JSON object and no Markdown, prose, code fence, or other text.
Use exactly these keys and no others:
{
  "schema_version": 1,
  "goal": "...",
  "constraints_preferences": ["..."],
  "progress": {"done": ["..."], "in_progress": ["..."], "pending": ["..."]},
  "key_decisions": ["..."],
  "rejected_attempts": ["..."],
  "verification": ["..."],
  "critical_context": ["..."],
  "uncertainties": ["..."],
  "next_steps": ["..."]
}
All array entries must be strings. Preserve current constraints, authoritative decisions,
rejected attempts, completed and pending work, exact paths/tests, and the next safe action.
Exclude stale noise. Do not include secrets.

Synthetic transcript:

"""


class StudyRunError(RuntimeError):
    """A study run failed without an automatic retry."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _write_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _write_private_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)
    path.chmod(0o600)


def _inside(root: Path, relative: str, *, directory: bool = False) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise StudyRunError(f"study path escapes its root: {relative}") from exc
    expected = candidate.is_dir() if directory else candidate.is_file()
    if not expected:
        kind = "directory" if directory else "file"
        raise StudyRunError(f"study {kind} not found: {relative}")
    return candidate


def _handoff_format(args: argparse.Namespace) -> str:
    value = getattr(args, "handoff_format", DEFAULT_HANDOFF_FORMAT)
    if value not in HANDOFF_FORMATS:
        raise StudyRunError(f"invalid handoff format: {value}")
    if getattr(args, "condition", None) != "handoff" and value != DEFAULT_HANDOFF_FORMAT:
        raise StudyRunError("state-v1 is only valid for the handoff condition")
    return value


def _select_run(payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    handoff_format = _handoff_format(args)
    matches = [
        run
        for run in payload["runs"]
        if (
            run.get("case"),
            run.get("band"),
            run.get("condition"),
            run.get("handoff_format", DEFAULT_HANDOFF_FORMAT),
            run.get("replicate"),
        ) == (args.case, args.band, args.condition, handoff_format, args.replicate)
    ]
    if len(matches) != 1:
        raise StudyRunError("selected run is absent or ambiguous in the study manifest")
    return matches[0]


def _run_id(args: argparse.Namespace) -> str:
    model_key = hashlib.sha256(f"{args.client}:{args.model}".encode()).hexdigest()[:10]
    handoff_format = getattr(args, "handoff_format", DEFAULT_HANDOFF_FORMAT)
    return (
        f"{args.case}--{args.band}--{args.condition}--{handoff_format}--{args.client}--"
        f"{model_key}--r{args.replicate:02d}"
    )


def _fixture_seed(args: argparse.Namespace) -> str:
    return f"context-rot-v1:{args.case}:{args.band}:replicate-{args.replicate}"


def _handoff_prompt(handoff_format: str) -> str:
    if handoff_format == "markdown-v1":
        return HANDOFF_PROMPT
    if handoff_format == "state-v1":
        return STATE_HANDOFF_PROMPT
    raise StudyRunError(f"unsupported handoff format: {handoff_format}")


def _sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _git_revision() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _client_executable(client: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    state_path = Path.home() / ".config/session-handoff/state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    targets = state.get("targets") if isinstance(state, dict) else None
    target = targets.get(client) if isinstance(targets, dict) else None
    if isinstance(target, str) and (Path(target).is_file() or Path(target).is_symlink()):
        return target
    return shutil.which(client) or client


def _auth_source(client: str) -> Path:
    if client == "codex":
        return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / ".credentials.json"


def _credential_mount(
    args: argparse.Namespace, client: str, native_home: Path
) -> tuple[str, Path | None]:
    if args.credential_mode == "environment":
        return "environment", None
    source = _auth_source(client).expanduser().resolve()
    if not source.is_file():
        return "environment", None
    target = native_home / source.name
    if not target.exists():
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    if target.is_symlink() or target.stat().st_size:
        raise StudyRunError(f"isolated {client} credential mount point is not empty")
    return "read_only_mount", source


def _agent_env(args: argparse.Namespace, client: str, native_home: Path) -> dict[str, str]:
    native_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    names = BASE_ENV | AUTH_ENV[client] | set(args.pass_env)
    env = {name: os.environ[name] for name in names if name in os.environ}
    env["HOME"] = str(native_home)
    if client == "claude":
        env["CLAUDE_CONFIG_DIR"] = str(native_home)
    else:
        env["CODEX_HOME"] = str(native_home)
    return env


def _sandbox_agent(
    command: list[str],
    *,
    executable: str,
    workspace: Path,
    native_home: Path,
    hidden_paths: list[Path],
    credential_source: Path | None,
    network: bool = True,
    workspace_writable: bool = True,
) -> list[str]:
    sandbox = shutil.which(executable)
    if not sandbox:
        raise StudyRunError(
            "handoff generation requires bubblewrap so the model cannot read fixture files"
        )
    agent_executable = shutil.which(command[0]) or command[0]
    agent_path = Path(agent_executable).resolve()
    if not agent_path.is_file():
        raise StudyRunError("agent executable is not a readable file")
    agent_paths = [agent_path]
    companion = agent_path.parent / "codex-code-mode-host"
    if companion.is_file():
        agent_paths.append(companion.resolve())
    command = [str(agent_path), *command[1:]]

    masked: list[Path] = []
    candidates = {
        Path.home().resolve(),
        Path("/tmp"),
        *(path.resolve() for path in hidden_paths),
    }
    for candidate in sorted(candidates, key=lambda path: len(path.parts)):
        if any(candidate == parent or parent in candidate.parents for parent in masked):
            continue
        if candidate.is_dir():
            masked.append(candidate)
    result = [
        sandbox,
        "--die-with-parent",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
    ]
    if not network:
        result.append("--unshare-net")
    for path in masked:
        result.extend(("--tmpfs", str(path)))
    for root in (Path.home().resolve(), Path("/tmp")):
        try:
            relative_parent = agent_path.parent.relative_to(root)
        except ValueError:
            continue
        current = root
        for part in relative_parent.parts:
            current /= part
            result.extend(("--dir", str(current)))
        break
    for path in agent_paths:
        result.extend(("--ro-bind", str(path), str(path)))
    result.extend(
        (
            "--tmpfs",
            "/mnt",
            "--dir",
            "/mnt/work",
            "--bind" if workspace_writable else "--ro-bind",
            str(workspace),
            "/mnt/work",
            "--dir",
            "/mnt/native",
            "--bind",
            str(native_home),
            "/mnt/native",
            "--dir",
            "/mnt/tmp",
        )
    )
    if credential_source is not None:
        result.extend(
            (
                "--ro-bind",
                str(credential_source),
                f"/mnt/native/{credential_source.name}",
            )
        )
    result.extend(("--chdir", "/mnt/work", "--", *command))
    return result


def _agent_command(
    client: str,
    executable: str,
    model: str,
    workspace: Path,
    *,
    mode: str,
    session_id: str | None = None,
) -> list[str]:
    if client == "claude":
        command = [
            executable,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--safe-mode",
            "--model",
            model,
        ]
        if mode == "generate":
            return [
                *command,
                "--permission-mode",
                "dontAsk",
                "--tools",
                "",
                "--no-session-persistence",
            ]
        command.extend(
            (
                "--permission-mode",
                "bypassPermissions",
                "--tools",
                "Read,Edit,Write,Bash,Glob,Grep",
            )
        )
        if mode == "resume":
            if not session_id:
                raise StudyRunError("resume mode requires a native session id")
            command.extend(("--resume", session_id))
        elif session_id:
            command.extend(("--session-id", session_id))
        return command

    command = [
        executable,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        "read-only" if mode == "generate" else "workspace-write",
        "--model",
        model,
        "--cd",
        str(workspace),
    ]
    if mode == "generate":
        return [*command, "--ephemeral", "-"]
    if mode == "resume":
        if not session_id:
            raise StudyRunError("resume mode requires a native session id")
        return [*command, "resume", session_id, "-"]
    return [*command, "--ephemeral", "-"]


def _parse_agent_output(client: str, stdout: str) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)

    if client == "claude":
        message = None
        usage: dict[str, Any] = {}
        trace: list[dict[str, Any]] = []
        for event in events:
            if event.get("type") == "result" and isinstance(event.get("result"), str):
                message = event["result"]
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
            content = event.get("message", {}).get("content") if isinstance(event.get("message"), dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    trace.append(
                        {
                            "kind": "tool",
                            "phase": "call",
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "input": block.get("input"),
                        }
                    )
                elif block.get("type") == "tool_result":
                    trace.append(
                        {
                            "kind": "tool",
                            "phase": "result",
                            "id": block.get("tool_use_id"),
                            "is_error": bool(block.get("is_error")),
                            "content": block.get("content"),
                        }
                    )
        if message is None:
            raise StudyRunError("Claude returned no final result; inspect the run artifact")
        input_values = [
            value
            for name in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
            if isinstance((value := usage.get(name)), int) and not isinstance(value, bool)
        ]
        return {
            "text": message,
            "input_tokens": sum(input_values) if input_values else None,
            "output_tokens": usage.get("output_tokens"),
            "trace": trace,
        }

    message = None
    usage: dict[str, Any] = {}
    trace = []
    for event in events:
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            message = item["text"]
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") not in {"agent_message", "reasoning"}
        ):
            trace.append({"kind": "tool", "item": item})
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    if message is None:
        raise StudyRunError("Codex returned no final result; inspect the run artifact")
    return {
        "text": message,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "trace": trace,
    }


def _invoke_agent(
    *,
    client: str,
    command: list[str],
    prompt: str,
    cwd: Path,
    env: dict[str, str],
    artifact_prefix: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise StudyRunError("agent executable could not be started") from exc
    artifact_prefix.with_suffix(".stdout").write_text(result.stdout or "", encoding="utf-8")
    artifact_prefix.with_suffix(".stderr").write_text(result.stderr or "", encoding="utf-8")
    if result.returncode != 0:
        raise StudyRunError("agent command failed; inspect the run artifacts")
    parsed = _parse_agent_output(client, result.stdout or "")
    parsed["wall_seconds"] = time.monotonic() - started
    return parsed


def _version(executable: str, cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            [executable, "--version"],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip()[:200] if result.returncode == 0 else None


def _migration_provenance() -> dict[str, str]:
    return {
        "migration_engine": "session-handoff",
        "migration_version": "0.5.4",
    }


def _snapshot_lines(path: Path) -> list[str]:
    if path.is_symlink():
        return [f"symlink -> {json.dumps(os.readlink(path), ensure_ascii=False)}\n"]
    data = path.read_bytes()
    try:
        return data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return [f"binary sha256: {_sha256(data)}\n"]


def _snapshot_diff(template: Path, workspace: Path) -> str:
    ignored = {"__pycache__", ".pytest_cache"}
    relatives = {
        path.relative_to(root)
        for root in (template, workspace)
        for path in root.rglob("*")
        if (path.is_symlink() or path.is_file())
        and not any(part in ignored for part in path.relative_to(root).parts)
        and path.suffix != ".pyc"
    }
    pieces: list[str] = []
    for relative in sorted(relatives):
        before_path = template / relative
        after_path = workspace / relative
        before = (
            _snapshot_lines(before_path)
            if before_path.exists() or before_path.is_symlink()
            else []
        )
        after = (
            _snapshot_lines(after_path)
            if after_path.exists() or after_path.is_symlink()
            else []
        )
        pieces.extend(
            difflib.unified_diff(
                before,
                after,
                fromfile=f"a/{relative.as_posix()}",
                tofile=f"b/{relative.as_posix()}",
            )
        )
    return "".join(pieces)


def _sum_metric(calls: list[dict[str, Any]], name: str) -> int | float | None:
    values = [call[name] for call in calls if isinstance(call.get(name), (int, float))]
    return sum(values) if values else None


def _manifest_command(run: dict[str, Any], field: str) -> list[str]:
    command = run.get(field)
    if not (
        isinstance(command, list)
        and command
        and all(isinstance(item, str) and item for item in command)
    ):
        raise StudyRunError(f"run {field} must be a non-empty argv array")
    return command


def _run_workspace_check(
    args: argparse.Namespace,
    command: list[str],
    *,
    workspace: Path,
    run_dir: Path,
    study_root: Path,
    name: str,
    writable: bool,
) -> subprocess.CompletedProcess[str]:
    native_home = run_dir / f"{name}-home"
    native_home.mkdir(mode=0o700, exist_ok=True)
    sandboxed = _sandbox_agent(
        command,
        executable=args.sandbox_executable,
        workspace=workspace,
        native_home=native_home,
        hidden_paths=[ROOT, study_root],
        credential_source=None,
        network=False,
        workspace_writable=writable,
    )
    env = {key: os.environ[key] for key in BASE_ENV if key in os.environ}
    env.update(HOME="/mnt/native", TMPDIR="/mnt/tmp")
    return subprocess.run(
        sandboxed,
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _judge_payload(
    run: dict[str, Any], *, blind_id: str, task_success: bool
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "blind_id": blind_id,
        "artifacts": {
            "supplied_context": "supplied-context.md",
            "continuation": "continuation.txt",
            "repository_diff": "workspace.diff",
            "verification": "verify.stdout",
            "acceptance": "acceptance.stdout",
            "acceptance_errors": "acceptance.stderr",
            "trace": "trace.json",
        },
        "facts": [
            {
                "id": item["id"],
                "statement": item.get("statement"),
                "status": None,
                "evidence": None,
            }
            for item in run["facts"]
        ],
        "stale_traps": [
            {
                "id": item["id"],
                "statement": item.get("statement"),
                "activated": None,
                "evidence": None,
            }
            for item in run["stale_traps"]
        ],
        "dod": [
            {
                "id": item["id"],
                "statement": item.get("statement"),
                "automated_pass": task_success,
                "passed": None,
                "evidence": None,
            }
            for item in run["dod"]
        ],
        "counters": {
            "repeated_failed_attempts": None,
            "stale_decisions_acted_on": None,
            "recovery_reads": None,
            "evidence": None,
        },
        "calibration": {
            "rubric_version": 1,
            "judge_id": None,
            "judge_model": None,
            "calibration_set": None,
            "human_reviewed": False,
        },
    }


def _render_blind_handoff(text: str) -> str:
    """Normalize presentation for judges while retaining every non-blank line."""
    headings = {
        "goal": "## Goal",
        "constraints & preferences": "## Constraints & Preferences",
        "constraints and preferences": "## Constraints & Preferences",
        "progress": "## Progress",
        "done": "### Done",
        "in progress": "### In Progress",
        "pending": "### Pending",
        "key decisions": "## Key Decisions",
        "critical context": "## Critical Context",
        "next steps": "## Next Steps",
    }
    normalized_lines: list[str] = []
    for raw_line in text.splitlines():
        line = " ".join(raw_line.strip().split())
        if not line:
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
        if heading:
            canonical_heading = headings.get(heading.group(1).strip().lower())
            if canonical_heading:
                line = canonical_heading
        else:
            item = re.match(r"^(?:[-+*]|\d+[.)])\s+(.*)$", line)
            if item:
                body = item.group(1).strip()
                if body.rstrip(".").lower() in {"none", "none identified"}:
                    body = "None identified."
                line = f"- {body}"
        normalized_lines.append(line)
    return "\n".join(normalized_lines) + "\n"


def _render_generated_handoff(text: str, handoff_format: str) -> tuple[str, int]:
    if handoff_format == "markdown-v1":
        redacted, redactions = redact_secrets(text)
    elif handoff_format == "state-v1":
        try:
            state = validate_state(
                json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
            )
            redacted_state, redactions = redact_state(state)
            redacted = render_state(redacted_state)
        except (TypeError, ValueError) as exc:
            raise StudyRunError("generated state-v1 handoff is invalid") from exc
    else:
        raise StudyRunError(f"unsupported handoff format: {handoff_format}")
    missing = validate_handoff(redacted)
    if missing:
        raise StudyRunError("generated handoff is missing canonical sections")
    return redacted, redactions


def _export_blinded_bundle(
    output: Path,
    run_dir: Path,
    state: dict[str, Any],
    run: dict[str, Any],
    *,
    task_success: bool,
) -> None:
    blind_id = state["blind_id"]
    blind_dir = output / "blinded" / blind_id
    blind_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    context = (run_dir / "supplied-context.md").read_text(encoding="utf-8")
    if run["condition"] == "handoff":
        context = _render_blind_handoff(context)
    elif run["condition"] == "oracle":
        context = re.sub(r"^# Oracle continuation state:", "# Continuation state:", context)
    (blind_dir / "supplied-context.md").write_text(context, encoding="utf-8")
    for name in (
        "continuation.txt",
        "workspace.diff",
        "verify.stdout",
        "verify.stderr",
        "acceptance.stdout",
        "acceptance.stderr",
        "trace.json",
    ):
        shutil.copy2(run_dir / name, blind_dir / name)
    _write_json(
        blind_dir / "judge.json",
        _judge_payload(run, blind_id=blind_id, task_success=task_success),
    )

    private_dir = output / "private"
    private_dir.mkdir(mode=0o700, exist_ok=True)
    private_dir.chmod(0o700)
    mapping_path = private_dir / "blind-map.json"
    if mapping_path.exists():
        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StudyRunError("blind-map.json is invalid") from exc
    else:
        mapping = {}
    existing = mapping.get(blind_id)
    entry = {
        "run_id": state["run_id"],
        "case": run["case"],
        "band": run["band"],
        "condition": run["condition"],
        "handoff_format": run.get("handoff_format", DEFAULT_HANDOFF_FORMAT),
        "client": state["client"],
        "model": state["model"],
        "revision": state["provenance"]["runner_git_revision"],
        "replicate": run["replicate"],
    }
    if existing not in (None, entry):
        raise StudyRunError("blind id already maps to a different run")
    mapping[blind_id] = entry
    _write_private_json(mapping_path, mapping)


def _prepare_context(
    args: argparse.Namespace,
    transcript: str,
    oracle: str,
    study_root: Path,
    workspace: Path,
    run_dir: Path,
    state: dict[str, Any],
) -> tuple[str, str | None, Path, list[dict[str, Any]]]:
    target_id = state["target_session_id"]
    target_home = run_dir / "native-target"
    calls: list[dict[str, Any]] = []
    if args.condition == "full":
        seed_native_session(args.client, target_home, target_id, workspace, transcript)
        (run_dir / "supplied-context.md").write_text(transcript, encoding="utf-8")
        return CONTINUATION_PROMPT, target_id, target_home, calls
    if args.condition == "migrate":
        source_client = "codex" if args.client == "claude" else "claude"
        source_home = run_dir / "native-source"
        seed_native_session(
            source_client,
            source_home,
            state["source_session_id"],
            workspace,
            transcript,
        )
        try:
            migration = migrate_session(
                source_client,
                args.client,
                state["source_session_id"],
                str(workspace),
                target_session_id=target_id,
                source_home=str(source_home),
                target_home=str(target_home),
            )
        except MigrationError as exc:
            raise StudyRunError("native migration failed; inspect local migration setup") from exc
        target_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        (run_dir / "supplied-context.md").write_text(transcript, encoding="utf-8")
        _write_json(
            run_dir / "migration.json",
            {
                "session_id": migration["session_id"],
                "source_format": migration["source_format"],
                "target_format": migration["target_format"],
                "warnings": migration["warnings"],
                "dropped_events": migration["dropped_events"],
                "context_loss": migration["context_loss"],
            },
        )
        return CONTINUATION_PROMPT, target_id, target_home, calls
    if args.condition == "oracle":
        target_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        (run_dir / "supplied-context.md").write_text(oracle, encoding="utf-8")
        return f"{oracle}\n\n{CONTINUATION_PROMPT}\n", None, target_home, calls

    analysis_home = run_dir / "native-analysis"
    generation_workspace = run_dir / "handoff-input"
    generation_workspace.mkdir(mode=0o700, exist_ok=True)
    analysis_home.mkdir(mode=0o700, exist_ok=True)
    handoff_format = _handoff_format(args)
    handoff_prompt = _handoff_prompt(handoff_format)
    generation_access, generation_credential = _credential_mount(
        args, args.client, analysis_home
    )
    state.setdefault("credential_access", {})["generation"] = generation_access
    generation_command = _agent_command(
        args.client,
        args.claude_executable if args.client == "claude" else args.codex_executable,
        args.model,
        Path("/mnt/work"),
        mode="generate",
    )
    generation_command = _sandbox_agent(
        generation_command,
        executable=args.sandbox_executable,
        workspace=generation_workspace,
        native_home=analysis_home,
        hidden_paths=[ROOT, study_root, workspace],
        credential_source=generation_credential,
    )
    generation_env = _agent_env(args, args.client, analysis_home)
    generation_env["HOME"] = "/mnt/native"
    generation_env["TMPDIR"] = "/mnt/tmp"
    if args.client == "claude":
        generation_env["CLAUDE_CONFIG_DIR"] = "/mnt/native"
    else:
        generation_env["CODEX_HOME"] = "/mnt/native"
    state["status"] = "provider_call_started"
    state["stage"] = "handoff_generation"
    state["provider_calls_started"] += 1
    _write_json(run_dir / "state.json", state)
    generated = _invoke_agent(
        client=args.client,
        command=generation_command,
        prompt=handoff_prompt + transcript,
        cwd=generation_workspace,
        env=generation_env,
        artifact_prefix=run_dir / "handoff-generation",
    )
    generation_trace = generated.pop("trace")
    if generation_trace:
        raise StudyRunError("handoff generator used a tool despite the isolated no-tool contract")
    redacted, redactions = _render_generated_handoff(generated["text"], handoff_format)
    (run_dir / "handoff.md").write_text(redacted, encoding="utf-8")
    (run_dir / "supplied-context.md").write_text(redacted, encoding="utf-8")
    generated.pop("text")
    generated["redactions"] = redactions
    calls.append(generated)
    target_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    return f"{redacted}\n\n{CONTINUATION_PROMPT}\n", None, target_home, calls


def execute(args: argparse.Namespace, run: dict[str, Any], study_root: Path, transcript: str) -> dict[str, Any]:
    template = _inside(study_root, run["workspace_template"], directory=True)
    oracle_path = _inside(study_root, f"{args.case}/oracle.md")
    verify_command = _manifest_command(run, "verify_command")
    acceptance_command = _manifest_command(run, "acceptance_command")
    evaluation_path = Path(getattr(args, "evaluation", study_root / "evaluation.json"))
    handoff_format = _handoff_format(args)
    if run.get("handoff_format", DEFAULT_HANDOFF_FORMAT) != handoff_format:
        raise StudyRunError("selected run handoff_format does not match the requested format")
    run_id = _run_id(args)
    run_dir = args.output.resolve() / run_id
    state_path = run_dir / "state.json"

    if run_dir.exists():
        if not args.resume:
            raise StudyRunError(f"run already exists: {run_id}; use --resume only for a safe checkpoint")
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StudyRunError("existing run has no valid resumable state") from exc
        resumable = state.get("status") in {"context_ready", "continuation_complete"}
        resumable = resumable or (
            state.get("status") == "prepared" and state.get("retry_safe") is True
        )
        if not resumable:
            raise StudyRunError("run is not at a retry-free resumable checkpoint")
        provenance = state.get("provenance")
        if not isinstance(provenance, dict):
            raise StudyRunError("existing run has no valid provenance")
        runner_path = Path(__file__).resolve()
        provenance["resume_runner_sha256"] = _sha256(runner_path.read_bytes())
        provenance["resume_runner_git_revision"] = _git_revision()
        _write_json(state_path, state)
        workspace = run_dir / "workspace"
    else:
        if args.resume:
            raise StudyRunError("cannot resume a run that does not exist")
        args.output.resolve().mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(mode=0o700)
        workspace = run_dir / "workspace"
        shutil.copytree(template, workspace)
        migration_provenance = _migration_provenance() if args.condition == "migrate" else {}
        fixture_seed = _fixture_seed(args)
        runner_path = Path(__file__).resolve()
        state = {
            "schema_version": 1,
            "run_id": run_id,
            "status": "prepared",
            "stage": "context",
            "provider_calls_started": 0,
            "blind_id": str(uuid.uuid4()),
            "fixture_seed": fixture_seed,
            "source_session_id": str(uuid.uuid5(uuid.NAMESPACE_URL, fixture_seed + ":source")),
            "target_session_id": str(uuid.uuid5(uuid.NAMESPACE_URL, fixture_seed + ":target")),
            "client": args.client,
            "model": args.model,
            "case": args.case,
            "band": args.band,
            "condition": args.condition,
            "handoff_format": handoff_format,
            "replicate": args.replicate,
            "provenance": {
                "prompt_version": PROMPT_VERSION,
                "handoff_format": handoff_format,
                "fixture_seed": fixture_seed,
                "handoff_prompt_sha256": _sha256(_handoff_prompt(handoff_format)),
                "continuation_prompt_sha256": _sha256(CONTINUATION_PROMPT),
                "source_sha256": _sha256(transcript),
                "oracle_sha256": _sha256(oracle_path.read_bytes()),
                "evaluation_sha256": _sha256(evaluation_path.read_bytes()),
                "verify_command_sha256": _sha256(
                    json.dumps(verify_command, ensure_ascii=False, separators=(",", ":"))
                ),
                "acceptance_command_sha256": _sha256(
                    json.dumps(acceptance_command, ensure_ascii=False, separators=(",", ":"))
                ),
                "workspace_template_sha256": _tree_sha256(template),
                "runner_sha256": _sha256(runner_path.read_bytes()),
                "runner_git_revision": _git_revision(),
                "sandbox_executable": args.sandbox_executable,
                "credential_mode": args.credential_mode,
                "client_executable": (
                    args.claude_executable
                    if args.client == "claude"
                    else args.codex_executable
                ),
                "client_profile": {
                    "customizations": "disabled",
                    "generation_tools": "disabled",
                    "generation_sandbox": "read_only",
                    "continuation_sandbox": "workspace_write",
                    "claude_permission_mode": "bypassPermissions",
                },
                **migration_provenance,
            },
        }
        _write_json(state_path, state)

    started = time.monotonic()
    calls: list[dict[str, Any]] = state.get("calls", [])
    try:
        if state["status"] == "prepared":
            prompt, session_id, target_home, context_calls = _prepare_context(
                args,
                transcript,
                oracle_path.read_text(encoding="utf-8"),
                study_root,
                workspace,
                run_dir,
                state,
            )
            calls.extend(context_calls)
            state.update(
                status="context_ready",
                stage="continuation",
                calls=calls,
                continuation_session_id=session_id,
                target_home=str(target_home),
            )
            state.pop("retry_safe", None)
            state.pop("last_error_stage", None)
            _write_json(state_path, state)
        else:
            supplied = (run_dir / "supplied-context.md").read_text(encoding="utf-8")
            prompt = (
                CONTINUATION_PROMPT
                if args.condition in {"full", "migrate"}
                else f"{supplied}\n\n{CONTINUATION_PROMPT}\n"
            )
            session_id = state.get("continuation_session_id")
            target_home = Path(state["target_home"])

        if state["status"] == "context_ready":
            executable = args.claude_executable if args.client == "claude" else args.codex_executable
            mode = "resume" if args.condition in {"full", "migrate"} else "fresh"
            credential_access, credential_source = _credential_mount(
                args, args.client, target_home
            )
            state.setdefault("credential_access", {})["continuation"] = credential_access
            command = _agent_command(
                args.client,
                executable,
                args.model,
                Path("/mnt/work"),
                mode=mode,
                session_id=session_id,
            )
            command = _sandbox_agent(
                command,
                executable=args.sandbox_executable,
                workspace=workspace,
                native_home=target_home,
                hidden_paths=[ROOT, study_root],
                credential_source=credential_source,
            )
            agent_env = _agent_env(args, args.client, target_home)
            agent_env["HOME"] = "/mnt/native"
            agent_env["TMPDIR"] = "/mnt/tmp"
            if args.client == "claude":
                agent_env["CLAUDE_CONFIG_DIR"] = "/mnt/native"
            else:
                agent_env["CODEX_HOME"] = "/mnt/native"
            state.update(status="provider_call_started", stage="continuation")
            state.pop("retry_safe", None)
            state.pop("last_error_stage", None)
            state["provider_calls_started"] += 1
            _write_json(state_path, state)
            continuation = _invoke_agent(
                client=args.client,
                command=command,
                prompt=prompt,
                cwd=workspace,
                env=agent_env,
                artifact_prefix=run_dir / "continuation",
            )
            trace = continuation.pop("trace")
            _write_json(run_dir / "trace.json", trace)
            (run_dir / "continuation.txt").write_text(continuation.pop("text"), encoding="utf-8")
            calls.append(continuation)
            state.update(status="continuation_complete", stage="verification", calls=calls)
            _write_json(state_path, state)

        verification = _run_workspace_check(
            args,
            verify_command,
            workspace=workspace,
            run_dir=run_dir,
            study_root=study_root,
            name="verification",
            writable=True,
        )
        (run_dir / "verify.stdout").write_text(verification.stdout or "", encoding="utf-8")
        (run_dir / "verify.stderr").write_text(verification.stderr or "", encoding="utf-8")
        acceptance = _run_workspace_check(
            args,
            acceptance_command,
            workspace=workspace,
            run_dir=run_dir,
            study_root=study_root,
            name="acceptance",
            writable=False,
        )
        (run_dir / "acceptance.stdout").write_text(
            acceptance.stdout or "", encoding="utf-8"
        )
        (run_dir / "acceptance.stderr").write_text(
            acceptance.stderr or "", encoding="utf-8"
        )
        task_success = verification.returncode == 0 and acceptance.returncode == 0
        (run_dir / "workspace.diff").write_text(_snapshot_diff(template, workspace), encoding="utf-8")
        evaluation_run = dict(run)
        evaluation_run["dod"] = [dict(item, passed=task_success) for item in run["dod"]]
        evaluation_run.update(
            run_id=state["run_id"],
            client=state["client"],
            model=state["model"],
            revision=state["provenance"]["runner_git_revision"],
            task_success=task_success,
            repeated_failed_attempts=None,
            stale_decisions_acted_on=None,
            recovery_reads=None,
            input_tokens=_sum_metric(calls, "input_tokens"),
            output_tokens=_sum_metric(calls, "output_tokens"),
            wall_seconds=time.monotonic() - started,
            supplied_context_bytes=(run_dir / "supplied-context.md").stat().st_size,
        )
        _write_json(run_dir / "evaluation-run.json", evaluation_run)
        _export_blinded_bundle(
            args.output.resolve(),
            run_dir,
            state,
            run,
            task_success=task_success,
        )
        state.update(
            status="completed",
            stage="done",
            task_success=task_success,
            client_version=_version(
                args.claude_executable if args.client == "claude" else args.codex_executable,
                workspace,
            ),
        )
        state.pop("retry_safe", None)
        state.pop("last_error_stage", None)
        _write_json(state_path, state)
        return {
            "mode": "execute",
            "run_id": run_id,
            "status": "completed",
            "provider_calls": state["provider_calls_started"],
            "task_success": task_success,
        }
    except Exception:
        if state.get("status") == "continuation_complete":
            state.update(retry_safe=True, last_error_stage="verification")
        elif state.get("status") == "context_ready" or (
            state.get("provider_calls_started") == 0
            and state.get("status") == "prepared"
            and args.condition == "handoff"
        ):
            state.update(retry_safe=True, last_error_stage=state.get("stage", "context"))
        else:
            state.update(status="failed", retry_safe=False)
        _write_json(state_path, state)
        raise


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("evaluation", type=Path)
    result.add_argument("--client", choices=CLIENTS, required=True)
    result.add_argument("--model", required=True)
    result.add_argument("--case", required=True)
    result.add_argument("--band", required=True)
    result.add_argument("--condition", choices=CONDITIONS, required=True)
    result.add_argument(
        "--handoff-format",
        choices=HANDOFF_FORMATS,
        default=DEFAULT_HANDOFF_FORMAT,
    )
    result.add_argument("--replicate", type=int, required=True)
    result.add_argument("--output", type=Path, default=Path("benchmark/results"))
    result.add_argument("--source", type=Path)
    result.add_argument("--allow-non-fixture-source", action="store_true")
    result.add_argument("--execute", action="store_true")
    result.add_argument("--acknowledge-provider-cost", action="store_true")
    result.add_argument("--resume", action="store_true")
    result.add_argument("--credential-mode", choices=("auto", "environment"), default="auto")
    result.add_argument("--pass-env", action="append", default=[], metavar="NAME")
    result.add_argument("--sandbox-executable", default="bwrap")
    result.add_argument("--claude-executable")
    result.add_argument("--codex-executable")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        args.claude_executable = _client_executable("claude", args.claude_executable)
        args.codex_executable = _client_executable("codex", args.codex_executable)
        evaluation = args.evaluation.resolve()
        payload = json.loads(evaluation.read_text(encoding="utf-8"))
        validate_study_manifest(payload)
        run = _select_run(payload, args)
        fixture_source = _inside(evaluation.parent, f"{args.case}/session-{args.band}.md")
        if args.source and not args.allow_non_fixture_source:
            raise StudyRunError("--source requires --allow-non-fixture-source")
        source = args.source.resolve() if args.source else fixture_source
        if not source.is_file():
            raise StudyRunError("source transcript is not a readable file")
        plan = {
            "mode": "plan",
            "run_id": _run_id(args),
            "client": args.client,
            "model": args.model,
            "case": args.case,
            "band": args.band,
            "condition": args.condition,
            "handoff_format": args.handoff_format,
            "replicate": args.replicate,
            "provider_calls": 2 if args.condition == "handoff" else 1,
            "synthetic_fixture": not bool(args.source),
        }
        if args.resume:
            state_path = args.output.resolve() / _run_id(args) / "state.json"
            try:
                resumed_state = json.loads(state_path.read_text(encoding="utf-8"))
                started = resumed_state.get("provider_calls_started", 0)
                if isinstance(started, int) and not isinstance(started, bool):
                    plan["provider_calls"] = max(0, plan["provider_calls"] - started)
            except (OSError, json.JSONDecodeError):
                pass
        if not args.execute:
            print(json.dumps(plan, sort_keys=True))
            return 0
        if not args.acknowledge_provider_cost:
            raise StudyRunError("--execute requires --acknowledge-provider-cost")
        summary = execute(
            args,
            run,
            evaluation.parent,
            source.read_text(encoding="utf-8"),
        )
        print(json.dumps(summary, sort_keys=True))
        return 0
    except (OSError, ValueError, StudyRunError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
