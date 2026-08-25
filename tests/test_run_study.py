import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "benchmark/run_study.py"
SPEC = ROOT / "benchmark/fixtures/context_rot_cases.json"


def prepare_study(tmp_path: Path) -> Path:
    study = tmp_path / "study"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/prepare_study.py"),
            str(SPEC),
            "--output",
            str(study),
            "--runs-per-condition",
            "1",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return study / "evaluation.json"


def write_fake_agent(path: Path) -> None:
    path.write_text(
        '''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if "--version" in args:
    print("fake-agent 1.0")
    raise SystemExit(0)

prompt = sys.stdin.read()
with open(os.environ["FAKE_AGENT_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "argv": args,
        "stdin_chars": len(prompt),
        "codex_home": bool(os.environ.get("CODEX_HOME")),
        "claude_home": bool(os.environ.get("CLAUDE_CONFIG_DIR")),
    }) + "\\n")
if os.environ.get("FAKE_AGENT_FAIL"):
    print("PRIVATE_PROVIDER_OUTPUT", file=sys.stderr)
    raise SystemExit(9)

canonical = """## Goal
Finish the fixture task.

## Constraints & Preferences

- Preserve the final authoritative decision.

## Progress

### Done

- Current runtime value is already correct.

### In Progress

- Focused test update.

### Pending

- Run verification.

## Key Decisions

- Use 15 seconds; 60 seconds is obsolete.

## Critical Context

- tests/cache/test_negative_ttl.py still expects 60.

## Next Steps

1. Update the focused test to 15.
2. Run pytest.
"""
generation = "--tools" in args or (
    "--sandbox" in args and args[args.index("--sandbox") + 1] == "read-only"
)
if not generation:
    target = Path.cwd() / "tests/cache/test_negative_ttl.py"
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("== 60", "== 15"), encoding="utf-8")

message = canonical if generation else "Implemented and verified the focused change."
if Path(sys.argv[0]).name.startswith("claude"):
    print(json.dumps({
        "result": message,
        "usage": {"input_tokens": 101, "output_tokens": 23},
    }))
else:
    print(json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": message},
    }))
    print(json.dumps({
        "type": "turn.completed",
        "usage": {"input_tokens": 101, "output_tokens": 23},
    }))
''',
        encoding="utf-8",
    )
    path.chmod(0o755)


def write_fake_migration(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
source = args[args.index("--from") + 1]
target = args[args.index("--to") + 1]
session_id = args[args.index("--session-id") + 1]
print(json.dumps({
    "source_format": source,
    "target_format": target,
    "session_id": session_id,
    "dry_run": "--dry-run" in args,
    "warnings": [],
    "dropped_events": {},
    "output": "synthetic-target",
    "manifest": "synthetic-manifest",
}))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def command(
    evaluation: Path,
    output: Path,
    condition: str,
    claude: Path,
    codex: Path,
    migration: Path,
) -> list[str]:
    return [
        sys.executable,
        str(RUNNER),
        str(evaluation),
        "--client",
        "claude" if condition == "oracle" else "codex",
        "--model",
        "synthetic-model",
        "--case",
        "superseded-decision",
        "--band",
        "short",
        "--condition",
        condition,
        "--replicate",
        "1",
        "--output",
        str(output),
        "--claude-executable",
        str(claude),
        "--codex-executable",
        str(codex),
        "--migration-executable",
        str(migration),
    ]


def test_default_is_a_content_free_plan_with_no_provider_call(tmp_path):
    evaluation = prepare_study(tmp_path)
    claude = tmp_path / "claude-fake"
    codex = tmp_path / "codex-fake"
    migration = tmp_path / "migration-fake"
    write_fake_agent(claude)
    write_fake_agent(codex)
    write_fake_migration(migration)
    log = tmp_path / "calls.jsonl"
    env = {**os.environ, "FAKE_AGENT_LOG": str(log)}

    result = subprocess.run(
        command(evaluation, tmp_path / "results", "handoff", claude, codex, migration),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["mode"] == "plan"
    assert payload["provider_calls"] == 2
    assert "Initial decision" not in result.stdout
    assert not log.exists()
    assert not (tmp_path / "results").exists()


def test_execute_requires_separate_provider_cost_acknowledgment(tmp_path):
    evaluation = prepare_study(tmp_path)
    claude = tmp_path / "claude-fake"
    codex = tmp_path / "codex-fake"
    migration = tmp_path / "migration-fake"
    write_fake_agent(claude)
    write_fake_agent(codex)
    write_fake_migration(migration)
    log = tmp_path / "calls.jsonl"

    result = subprocess.run(
        [
            *command(evaluation, tmp_path / "results", "full", claude, codex, migration),
            "--execute",
        ],
        env={**os.environ, "FAKE_AGENT_LOG": str(log)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "--acknowledge-provider-cost" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize(
    ("condition", "expected_calls"),
    [("full", 1), ("handoff", 2), ("migrate", 1), ("oracle", 1)],
)
def test_fake_pilot_executes_isolated_condition_and_writes_blinded_artifacts(
    tmp_path,
    condition,
    expected_calls,
):
    evaluation = prepare_study(tmp_path)
    claude = tmp_path / "claude-fake"
    codex = tmp_path / "codex-fake"
    migration = tmp_path / "migration-fake"
    write_fake_agent(claude)
    write_fake_agent(codex)
    write_fake_migration(migration)
    output = tmp_path / "results"
    log = tmp_path / "calls.jsonl"

    result = subprocess.run(
        [
            *command(evaluation, output, condition, claude, codex, migration),
            "--execute",
            "--acknowledge-provider-cost",
        ],
        env={**os.environ, "FAKE_AGENT_LOG": str(log)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary == {
        "mode": "execute",
        "run_id": summary["run_id"],
        "status": "completed",
        "provider_calls": expected_calls,
        "task_success": True,
    }
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == expected_calls
    assert all(call["stdin_chars"] > 0 for call in calls)
    assert all("Initial decision" not in json.dumps(call["argv"]) for call in calls)
    assert all(call["codex_home"] or call["claude_home"] for call in calls)

    run_dir = output / summary["run_id"]
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    judge = json.loads((run_dir / "judge.json").read_text(encoding="utf-8"))
    evaluation_run = json.loads(
        (run_dir / "evaluation-run.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "completed"
    assert "continuation_prompt" not in state
    assert (run_dir / "supplied-context.md").is_file()
    assert "condition" not in judge
    assert judge["calibration"]["human_reviewed"] is False
    assert all("evidence" in item for item in judge["facts"])
    assert all(item["statement"] for item in judge["facts"])
    assert evaluation_run["task_success"] is True
    assert all(item["passed"] is True for item in evaluation_run["dod"])


def test_non_fixture_source_needs_its_own_explicit_flag(tmp_path):
    evaluation = prepare_study(tmp_path)
    claude = tmp_path / "claude-fake"
    codex = tmp_path / "codex-fake"
    migration = tmp_path / "migration-fake"
    write_fake_agent(claude)
    write_fake_agent(codex)
    write_fake_migration(migration)
    source = tmp_path / "external.md"
    source.write_text("non-fixture source", encoding="utf-8")

    result = subprocess.run(
        [
            *command(evaluation, tmp_path / "results", "full", claude, codex, migration),
            "--source",
            str(source),
            "--execute",
            "--acknowledge-provider-cost",
        ],
        env={**os.environ, "FAKE_AGENT_LOG": str(tmp_path / "calls.jsonl")},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "--allow-non-fixture-source" in result.stderr
    assert not (tmp_path / "calls.jsonl").exists()


def test_provider_failure_is_content_free_and_never_retried(tmp_path):
    evaluation = prepare_study(tmp_path)
    claude = tmp_path / "claude-fake"
    codex = tmp_path / "codex-fake"
    migration = tmp_path / "migration-fake"
    write_fake_agent(claude)
    write_fake_agent(codex)
    write_fake_migration(migration)
    output = tmp_path / "results"
    log = tmp_path / "calls.jsonl"
    argv = [
        *command(evaluation, output, "full", claude, codex, migration),
        "--execute",
        "--acknowledge-provider-cost",
    ]
    env = {
        **os.environ,
        "FAKE_AGENT_LOG": str(log),
        "FAKE_AGENT_FAIL": "1",
    }

    failed = subprocess.run(argv, env=env, text=True, capture_output=True, check=False)

    assert failed.returncode != 0
    assert "PRIVATE_PROVIDER_OUTPUT" not in failed.stderr
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1
    run_dir = next(output.iterdir())
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["retry_safe"] is False

    resumed = subprocess.run(
        [*argv, "--resume"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert resumed.returncode != 0
    assert "not at a retry-free resumable checkpoint" in resumed.stderr
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1
