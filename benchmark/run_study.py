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
from server.handoff_mcp import redact_secrets, validate_handoff
from server.migration import MigrationError, migrate_session


CLIENTS = ("claude", "codex")
CONDITIONS = ("full", "handoff", "migrate", "oracle")
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


class StudyRunError(RuntimeError):
    """A study run failed without an automatic retry."""


def _write_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


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


def _select_run(payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    matches = [
        run
        for run in payload["runs"]
        if (
            run.get("case"),
            run.get("band"),
            run.get("condition"),
            run.get("replicate"),
        )
        == (args.case, args.band, args.condition, args.replicate)
    ]
    if len(matches) != 1:
        raise StudyRunError("selected run is absent or ambiguous in the study manifest")
    return matches[0]


def _run_id(args: argparse.Namespace) -> str:
    model_key = hashlib.sha256(f"{args.client}:{args.model}".encode()).hexdigest()[:10]
    return (
        f"{args.case}--{args.band}--{args.condition}--{args.client}--"
        f"{model_key}--r{args.replicate:02d}"
    )


def _provider_calls(condition: str) -> int:
    return 2 if condition == "handoff" else 1


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
            "json",
            "--model",
            model,
        ]
        if mode == "generate":
            return [
                *command,
                "--permission-mode",
                "plan",
                "--tools",
                "",
                "--no-session-persistence",
            ]
        command.extend(("--permission-mode", "acceptEdits"))
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


def _agent_env(client: str, native_home: Path) -> dict[str, str]:
    native_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = os.environ.copy()
    if client == "claude":
        env["CLAUDE_CONFIG_DIR"] = str(native_home)
    else:
        env["CODEX_HOME"] = str(native_home)
    return env


def _parse_agent_output(client: str, stdout: str) -> dict[str, Any]:
    if client == "claude":
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise StudyRunError("Claude returned invalid JSON; inspect the run artifact") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), str):
            raise StudyRunError("Claude returned no final result; inspect the run artifact")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return {
            "text": payload["result"],
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        }

    message = None
    usage: dict[str, Any] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            message = item["text"]
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    if message is None:
        raise StudyRunError("Codex returned no final result; inspect the run artifact")
    return {
        "text": message,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
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


def _snapshot_diff(template: Path, workspace: Path) -> str:
    ignored = {"__pycache__", ".pytest_cache"}
    relatives = {
        path.relative_to(root)
        for root in (template, workspace)
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in ignored for part in path.relative_to(root).parts)
        and path.suffix != ".pyc"
    }
    pieces: list[str] = []
    for relative in sorted(relatives):
        before_path = template / relative
        after_path = workspace / relative
        before = before_path.read_text(encoding="utf-8").splitlines(keepends=True) if before_path.exists() else []
        after = after_path.read_text(encoding="utf-8").splitlines(keepends=True) if after_path.exists() else []
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


def _judge_payload(run: dict[str, Any], *, task_success: bool) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "blind_id": str(uuid.uuid4()),
        "artifacts": {
            "supplied_context": "supplied-context.md",
            "continuation": "continuation.txt",
            "repository_diff": "workspace.diff",
            "verification": "verify.stdout",
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


def _prepare_context(
    args: argparse.Namespace,
    transcript: str,
    oracle: str,
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
                executable=args.migration_executable,
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
    generation_command = _agent_command(
        args.client,
        args.claude_executable if args.client == "claude" else args.codex_executable,
        args.model,
        workspace,
        mode="generate",
    )
    state["status"] = "provider_call_started"
    state["stage"] = "handoff_generation"
    state["provider_calls_started"] += 1
    _write_json(run_dir / "state.json", state)
    generated = _invoke_agent(
        client=args.client,
        command=generation_command,
        prompt=HANDOFF_PROMPT + transcript,
        cwd=workspace,
        env=_agent_env(args.client, analysis_home),
        artifact_prefix=run_dir / "handoff-generation",
    )
    redacted, redactions = redact_secrets(generated["text"])
    missing = validate_handoff(redacted)
    if missing:
        raise StudyRunError("generated handoff is missing canonical sections")
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
        if state.get("status") not in {"context_ready", "continuation_complete"}:
            raise StudyRunError("run is not at a retry-free resumable checkpoint")
        workspace = run_dir / "workspace"
    else:
        if args.resume:
            raise StudyRunError("cannot resume a run that does not exist")
        args.output.resolve().mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(mode=0o700)
        workspace = run_dir / "workspace"
        shutil.copytree(template, workspace)
        seed = f"{run_id}:native"
        state = {
            "schema_version": 1,
            "run_id": run_id,
            "status": "prepared",
            "stage": "context",
            "provider_calls_started": 0,
            "source_session_id": str(uuid.uuid5(uuid.NAMESPACE_URL, seed + ":source")),
            "target_session_id": str(uuid.uuid5(uuid.NAMESPACE_URL, seed + ":target")),
            "client": args.client,
            "model": args.model,
            "case": args.case,
            "band": args.band,
            "condition": args.condition,
            "replicate": args.replicate,
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
            command = _agent_command(
                args.client,
                executable,
                args.model,
                workspace,
                mode=mode,
                session_id=session_id,
            )
            state.update(status="provider_call_started", stage="continuation")
            state["provider_calls_started"] += 1
            _write_json(state_path, state)
            continuation = _invoke_agent(
                client=args.client,
                command=command,
                prompt=prompt,
                cwd=workspace,
                env=_agent_env(args.client, target_home),
                artifact_prefix=run_dir / "continuation",
            )
            (run_dir / "continuation.txt").write_text(continuation.pop("text"), encoding="utf-8")
            calls.append(continuation)
            state.update(status="continuation_complete", stage="verification", calls=calls)
            _write_json(state_path, state)

        verify_command = run.get("verify_command")
        if not (
            isinstance(verify_command, list)
            and verify_command
            and all(isinstance(item, str) and item for item in verify_command)
        ):
            raise StudyRunError("run verify_command must be a non-empty argv array")
        verification = subprocess.run(
            verify_command,
            cwd=workspace,
            text=True,
            capture_output=True,
            check=False,
        )
        (run_dir / "verify.stdout").write_text(verification.stdout or "", encoding="utf-8")
        (run_dir / "verify.stderr").write_text(verification.stderr or "", encoding="utf-8")
        task_success = verification.returncode == 0
        (run_dir / "workspace.diff").write_text(_snapshot_diff(template, workspace), encoding="utf-8")
        evaluation_run = dict(run)
        evaluation_run["dod"] = [dict(item, passed=task_success) for item in run["dod"]]
        evaluation_run.update(
            task_success=task_success,
            repeated_failed_attempts=None,
            stale_decisions_acted_on=None,
            recovery_reads=None,
            input_tokens=_sum_metric(calls, "input_tokens"),
            output_tokens=_sum_metric(calls, "output_tokens"),
            wall_seconds=time.monotonic() - started,
        )
        _write_json(run_dir / "evaluation-run.json", evaluation_run)
        _write_json(run_dir / "judge.json", _judge_payload(run, task_success=task_success))
        state.update(
            status="completed",
            stage="done",
            task_success=task_success,
            client_version=_version(
                args.claude_executable if args.client == "claude" else args.codex_executable,
                workspace,
            ),
        )
        _write_json(state_path, state)
        return {
            "mode": "execute",
            "run_id": run_id,
            "status": "completed",
            "provider_calls": state["provider_calls_started"],
            "task_success": task_success,
        }
    except Exception:
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
    result.add_argument("--replicate", type=int, required=True)
    result.add_argument("--output", type=Path, default=Path("benchmark/results"))
    result.add_argument("--source", type=Path)
    result.add_argument("--allow-non-fixture-source", action="store_true")
    result.add_argument("--execute", action="store_true")
    result.add_argument("--acknowledge-provider-cost", action="store_true")
    result.add_argument("--resume", action="store_true")
    result.add_argument("--claude-executable", default="claude")
    result.add_argument("--codex-executable", default="codex")
    result.add_argument("--migration-executable", default="session-migrate")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
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
            "replicate": args.replicate,
            "provider_calls": _provider_calls(args.condition),
            "synthetic_fixture": not bool(args.source),
        }
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
