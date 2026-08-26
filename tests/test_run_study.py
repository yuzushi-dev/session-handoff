import hashlib
import json
import os
import stat
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

import benchmark.run_study as study_runner
from benchmark.run_study import _credential_mount, _sandbox_agent


ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "benchmark/run_study.py"
SPEC = ROOT / "benchmark/fixtures/context_rot_cases.json"


def test_claude_input_tokens_include_cache_usage():
    stdout = json.dumps(
        {
            "type": "result",
            "result": "done",
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 8,
                "output_tokens": 3,
            },
        }
    )

    parsed = study_runner._parse_agent_output("claude", stdout)

    assert parsed["input_tokens"] == 15


def test_snapshot_diff_does_not_dereference_symlinks(tmp_path):
    template = tmp_path / "template"
    workspace = tmp_path / "workspace"
    template.mkdir()
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must-not-leak\n", encoding="utf-8")
    (workspace / "link").symlink_to(outside)

    diff = study_runner._snapshot_diff(template, workspace)

    assert "must-not-leak" not in diff
    assert "symlink ->" in diff
    assert str(outside) in diff


def test_snapshot_diff_represents_binary_files_by_hash(tmp_path):
    template = tmp_path / "template"
    workspace = tmp_path / "workspace"
    template.mkdir()
    workspace.mkdir()
    (workspace / "artifact.bin").write_bytes(b"\xff\x00")

    diff = study_runner._snapshot_diff(template, workspace)

    assert "binary sha256:" in diff


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
generation = ("--tools" in args and args[args.index("--tools") + 1] == "") or (
    "--sandbox" in args and args[args.index("--sandbox") + 1] == "read-only"
)
client = "claude" if "--safe-mode" in args else "codex"
print(json.dumps({
    "type": "fake_meta",
    "argv": args,
    "stdin_chars": len(prompt),
    "cwd": os.getcwd(),
    "generation": generation,
    "workspace_entries": sorted(path.name for path in Path.cwd().iterdir()),
    "codex_home": bool(os.environ.get("CODEX_HOME")),
    "claude_home": bool(os.environ.get("CLAUDE_CONFIG_DIR")),
    "unrelated_secret_present": "SHOULD_NOT_REACH_AGENT" in os.environ,
}))
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
if not generation:
    if "resume" in args or "--resume" in args:
        session_id = args[args.index("resume") + 1] if "resume" in args else args[args.index("--resume") + 1]
        native_home = Path(os.environ["CODEX_HOME"] if client == "codex" else os.environ["CLAUDE_CONFIG_DIR"])
        marker = native_home / (".fake-session-" + session_id)
        if not marker.exists():
            if client == "codex":
                import sqlite3
                database = native_home / "thread_history_1.sqlite"
                found = None
                if database.is_file():
                    connection = sqlite3.connect(database)
                    try:
                        found = connection.execute(
                            "SELECT 1 FROM thread_items WHERE thread_id = ?", (session_id,)
                        ).fetchone()
                    finally:
                        connection.close()
                if not found:
                    found = list(native_home.rglob("rollout-*.jsonl"))
                if not found:
                    raise SystemExit(8)
            elif not list(native_home.rglob(session_id + ".jsonl")):
                raise SystemExit(8)
    target = Path.cwd() / "tests/cache/test_negative_ttl.py"
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("== 60", "== 15"), encoding="utf-8")

message = canonical if generation else "Implemented and verified the focused change."
if client == "claude":
    if not generation:
        print(json.dumps({
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use",
                "id": "tool-1",
                "name": "Read",
                "input": {"file_path": "tests/cache/test_negative_ttl.py"},
            }]},
        }))
        print(json.dumps({
            "type": "user",
            "message": {"content": [{
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "content": "focused file read",
                "is_error": False,
            }]},
        }))
    print(json.dumps({
        "type": "result",
        "subtype": "success",
        "result": message,
        "usage": {"input_tokens": 101, "output_tokens": 23},
    }))
else:
    if not generation:
        print(json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "sed -n '1,80p' tests/cache/test_negative_ttl.py",
                "aggregated_output": "focused file read",
                "exit_code": 0,
                "status": "completed",
            },
        }))
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
import hashlib
import json
import sys

args = sys.argv[1:]
if "--version" in args:
    print("migration-fake 1.0")
    raise SystemExit(0)
source = args[args.index("--from") + 1]
target = args[args.index("--to") + 1]
session_id = args[args.index("--session-id") + 1]
home = args[args.index("--home") + 1]
output = None
manifest = None
if "--dry-run" not in args:
    from pathlib import Path
    root = Path(home)
    root.mkdir(parents=True, exist_ok=True)
    (root / (".fake-session-" + session_id)).write_text("ready", encoding="utf-8")
    if target == "codex":
        output_path = root / "sessions/2026/08/25" / ("rollout-fake-" + session_id + ".jsonl")
        output_path.parent.mkdir(parents=True)
        records = [
            {"type": "session_meta", "payload": {"id": session_id, "session_id": session_id}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fixture"}]}},
        ]
        output_path.write_text("\\n".join(json.dumps(record) for record in records) + "\\n", encoding="utf-8")
        manifest_path = root / "session-handoff/manifests" / (session_id + ".json")
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(json.dumps({"target": {"session_id": session_id, "path": str(output_path), "records": len(records), "sha256": hashlib.sha256(output_path.read_bytes()).hexdigest()}}), encoding="utf-8")
        output = str(output_path)
        manifest = str(manifest_path)
    else:
        output = "synthetic-target"
        manifest = "synthetic-manifest"
print(json.dumps({
    "source_format": source,
    "target_format": target,
    "session_id": session_id,
    "dry_run": "--dry-run" in args,
    "warnings": [],
    "dropped_events": {},
    "output": output,
    "manifest": manifest,
}))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def command(
    evaluation: Path,
    output: Path,
    condition: str,
    client: str,
    claude: Path,
    codex: Path,
    migration: Path,
) -> list[str]:
    return [
        sys.executable,
        str(RUNNER),
        str(evaluation),
        "--client",
        client,
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
        "--credential-mode",
        "environment",
        "--pass-env",
        "FAKE_AGENT_FAIL",
    ]


def fake_calls(run_dir: Path) -> list[dict]:
    calls = []
    for name in ("handoff-generation.stdout", "continuation.stdout"):
        path = run_dir / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            payload = json.loads(line)
            if payload.get("type") == "fake_meta":
                calls.append(payload)
    return calls


def test_default_is_a_content_free_plan_with_no_provider_call(tmp_path):
    evaluation = prepare_study(tmp_path)
    claude = tmp_path / "claude-fake"
    codex = tmp_path / "codex-fake"
    migration = tmp_path / "migration-fake"
    write_fake_agent(claude)
    write_fake_agent(codex)
    write_fake_migration(migration)
    env = os.environ.copy()

    result = subprocess.run(
        command(evaluation, tmp_path / "results", "handoff", "codex", claude, codex, migration),
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
    assert not (tmp_path / "results").exists()


def test_execute_requires_separate_provider_cost_acknowledgment(tmp_path):
    evaluation = prepare_study(tmp_path)
    claude = tmp_path / "claude-fake"
    codex = tmp_path / "codex-fake"
    migration = tmp_path / "migration-fake"
    write_fake_agent(claude)
    write_fake_agent(codex)
    write_fake_migration(migration)

    result = subprocess.run(
        [
            *command(evaluation, tmp_path / "results", "full", "codex", claude, codex, migration),
            "--execute",
        ],
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "--acknowledge-provider-cost" in result.stderr


def test_invalid_hidden_acceptance_fails_before_provider_call(tmp_path):
    evaluation = prepare_study(tmp_path)
    payload = json.loads(evaluation.read_text(encoding="utf-8"))
    selected = next(
        run
        for run in payload["runs"]
        if (run["case"], run["band"], run["condition"], run["replicate"])
        == ("superseded-decision", "short", "full", 1)
    )
    selected["acceptance_command"] = []
    evaluation.write_text(json.dumps(payload), encoding="utf-8")
    claude = tmp_path / "claude-fake"
    codex = tmp_path / "codex-fake"
    migration = tmp_path / "migration-fake"
    write_fake_agent(claude)
    write_fake_agent(codex)
    write_fake_migration(migration)
    output = tmp_path / "results"

    result = subprocess.run(
        [
            *command(evaluation, output, "full", "codex", claude, codex, migration),
            "--execute",
            "--acknowledge-provider-cost",
        ],
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "acceptance_command" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    ("client", "condition", "expected_calls"),
    [
        (client, condition, 2 if condition == "handoff" else 1)
        for client in ("claude", "codex")
        for condition in ("full", "handoff", "migrate", "oracle")
    ],
)
def test_fake_pilot_executes_isolated_condition_and_writes_blinded_artifacts(
    tmp_path,
    client,
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

    result = subprocess.run(
        [
            *command(evaluation, output, condition, client, claude, codex, migration),
            "--execute",
            "--acknowledge-provider-cost",
        ],
        env={
            **os.environ,
            "SHOULD_NOT_REACH_AGENT": "secret",
        },
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
    run_dir = output / summary["run_id"]
    calls = fake_calls(run_dir)
    assert len(calls) == expected_calls
    assert all(call["stdin_chars"] > 0 for call in calls)
    assert all("Initial decision" not in json.dumps(call["argv"]) for call in calls)
    assert all(call["codex_home"] or call["claude_home"] for call in calls)
    assert all(call["unrelated_secret_present"] is False for call in calls)
    continuation_call = next(call for call in calls if not call["generation"])
    if client == "claude":
        mode_index = continuation_call["argv"].index("--permission-mode")
        assert continuation_call["argv"][mode_index + 1] == "bypassPermissions"
    generation_calls = [call for call in calls if call["generation"]]
    if condition == "handoff":
        assert len(generation_calls) == 1
        assert generation_calls[0]["workspace_entries"] == []
        if client == "claude":
            mode_index = generation_calls[0]["argv"].index("--permission-mode")
            assert generation_calls[0]["argv"][mode_index + 1] == "dontAsk"

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    blind_dir = output / "blinded" / state["blind_id"]
    judge = json.loads((blind_dir / "judge.json").read_text(encoding="utf-8"))
    evaluation_run = json.loads(
        (run_dir / "evaluation-run.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "completed"
    assert "continuation_prompt" not in state
    assert (blind_dir / "supplied-context.md").is_file()
    assert "condition" not in judge
    assert judge["blind_id"] == state["blind_id"]
    assert judge["calibration"]["human_reviewed"] is False
    assert all("evidence" in item for item in judge["facts"])
    assert all(item["statement"] for item in judge["facts"])
    assert judge["artifacts"]["trace"] == "trace.json"
    trace = json.loads((blind_dir / "trace.json").read_text(encoding="utf-8"))
    assert trace and trace[0]["kind"] == "tool"
    assert evaluation_run["task_success"] is True
    assert all(item["passed"] is True for item in evaluation_run["dod"])
    assert state["provenance"]["source_sha256"]
    assert state["provenance"]["workspace_template_sha256"]
    assert state["provenance"]["runner_sha256"]
    assert state["provenance"]["evaluation_sha256"]
    assert state["provenance"]["acceptance_command_sha256"]
    assert state["provenance"]["verify_command_sha256"]
    assert state["provenance"]["prompt_version"] == 1
    private_dir = output / "private"
    mapping_path = private_dir / "blind-map.json"
    assert stat.S_IMODE(private_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(mapping_path.stat().st_mode) == 0o600
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert mapping[state["blind_id"]]["condition"] == condition
    if condition == "migrate":
        provenance = state["provenance"]
        assert provenance["migration_engine"] == "session-handoff"
        assert provenance["migration_version"] == "0.5.4"


def test_hidden_acceptance_controls_automated_task_success(tmp_path):
    evaluation = prepare_study(tmp_path)
    payload = json.loads(evaluation.read_text(encoding="utf-8"))
    selected = next(
        run
        for run in payload["runs"]
        if (run["case"], run["band"], run["condition"], run["replicate"])
        == ("superseded-decision", "short", "oracle", 1)
    )
    selected["acceptance_command"] = [
        "python3",
        "-c",
        "raise AssertionError('hidden stale-state rejection')",
    ]
    evaluation.write_text(json.dumps(payload), encoding="utf-8")
    claude = tmp_path / "claude-fake"
    codex = tmp_path / "codex-fake"
    migration = tmp_path / "migration-fake"
    write_fake_agent(claude)
    write_fake_agent(codex)
    write_fake_migration(migration)
    output = tmp_path / "results"

    result = subprocess.run(
        [
            *command(evaluation, output, "oracle", "codex", claude, codex, migration),
            "--execute",
            "--acknowledge-provider-cost",
        ],
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["task_success"] is False
    run_dir = output / summary["run_id"]
    assert "hidden stale-state rejection" in (run_dir / "acceptance.stderr").read_text()
    evaluation_run = json.loads((run_dir / "evaluation-run.json").read_text())
    assert evaluation_run["task_success"] is False
    assert all(item["passed"] is False for item in evaluation_run["dod"])
    state = json.loads((run_dir / "state.json").read_text())
    judge = json.loads(
        (output / "blinded" / state["blind_id"] / "judge.json").read_text()
    )
    assert judge["artifacts"]["acceptance"] == "acceptance.stdout"


def test_hidden_acceptance_runs_without_host_environment_or_files(tmp_path):
    evaluation = prepare_study(tmp_path)
    payload = json.loads(evaluation.read_text(encoding="utf-8"))
    outside = tmp_path / "outside-secret"
    outside.write_text("must stay hidden", encoding="utf-8")
    selected = next(
        run
        for run in payload["runs"]
        if (run["case"], run["band"], run["condition"], run["replicate"])
        == ("superseded-decision", "short", "oracle", 1)
    )
    selected["acceptance_command"] = [
        "python3",
        "-c",
        (
            "import os\n"
            "from pathlib import Path\n"
            "from cache.config import NEGATIVE_CACHE_TTL\n"
            "assert NEGATIVE_CACHE_TTL == 15\n"
            "assert 'SHOULD_NOT_REACH_AGENT' not in os.environ\n"
            f"assert not Path({str(outside)!r}).exists()\n"
        ),
    ]
    evaluation.write_text(json.dumps(payload), encoding="utf-8")
    claude = tmp_path / "claude-fake"
    codex = tmp_path / "codex-fake"
    migration = tmp_path / "migration-fake"
    write_fake_agent(claude)
    write_fake_agent(codex)
    write_fake_migration(migration)

    result = subprocess.run(
        [
            *command(
                evaluation,
                tmp_path / "results",
                "oracle",
                "codex",
                claude,
                codex,
                migration,
            ),
            "--execute",
            "--acknowledge-provider-cost",
        ],
        env={**os.environ, "SHOULD_NOT_REACH_AGENT": "secret"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["task_success"] is True


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
            *command(evaluation, tmp_path / "results", "full", "codex", claude, codex, migration),
            "--source",
            str(source),
            "--execute",
            "--acknowledge-provider-cost",
        ],
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "--allow-non-fixture-source" in result.stderr


def test_provider_failure_is_content_free_and_never_retried(tmp_path):
    evaluation = prepare_study(tmp_path)
    claude = tmp_path / "claude-fake"
    codex = tmp_path / "codex-fake"
    migration = tmp_path / "migration-fake"
    write_fake_agent(claude)
    write_fake_agent(codex)
    write_fake_migration(migration)
    output = tmp_path / "results"
    argv = [
        *command(evaluation, output, "full", "codex", claude, codex, migration),
        "--execute",
        "--acknowledge-provider-cost",
    ]
    env = {
        **os.environ,
        "FAKE_AGENT_FAIL": "1",
    }

    failed = subprocess.run(argv, env=env, text=True, capture_output=True, check=False)

    assert failed.returncode != 0
    assert "PRIVATE_PROVIDER_OUTPUT" not in failed.stderr
    run_dir = next(output.iterdir())
    assert len(fake_calls(run_dir)) == 1
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
    assert len(fake_calls(run_dir)) == 1


def test_pre_provider_failure_resumes_from_prepared_checkpoint(tmp_path):
    evaluation = prepare_study(tmp_path)
    claude = tmp_path / "claude-fake"
    codex = tmp_path / "codex-fake"
    migration = tmp_path / "migration-fake"
    write_fake_agent(claude)
    write_fake_agent(codex)
    write_fake_migration(migration)
    output = tmp_path / "results"
    base = command(
        evaluation, output, "handoff", "codex", claude, codex, migration
    )
    cost_flags = ["--execute", "--acknowledge-provider-cost"]

    interrupted = subprocess.run(
        [*base, *cost_flags, "--sandbox-executable", "missing-bwrap"],
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert interrupted.returncode != 0
    run_dir = next(output.iterdir())
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "prepared"
    assert state["provider_calls_started"] == 0
    assert state["retry_safe"] is True
    assert state["last_error_stage"] == "context"
    assert fake_calls(run_dir) == []

    resumed = subprocess.run(
        [*base, *cost_flags, "--sandbox-executable", "bwrap", "--resume"],
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["provider_calls"] == 2
    assert len(fake_calls(run_dir)) == 2


def test_context_ready_remains_resumable_after_handoff_generation(
    tmp_path, monkeypatch
):
    evaluation = prepare_study(tmp_path)
    payload = json.loads(evaluation.read_text(encoding="utf-8"))
    run = next(
        item
        for item in payload["runs"]
        if (item["case"], item["band"], item["condition"], item["replicate"])
        == ("superseded-decision", "short", "handoff", 1)
    )
    source = evaluation.parent / "superseded-decision/session-short.md"
    args = SimpleNamespace(
        output=tmp_path / "results",
        resume=False,
        client="codex",
        model="synthetic-model",
        case="superseded-decision",
        band="short",
        condition="handoff",
        replicate=1,
        sandbox_executable="bwrap",
        credential_mode="environment",
        pass_env=[],
        claude_executable="claude-fake",
        codex_executable="codex-fake",
    )

    def generated_context(*call_args):
        run_dir = call_args[5]
        state = call_args[6]
        target_home = run_dir / "native-target"
        target_home.mkdir()
        (run_dir / "supplied-context.md").write_text("handoff", encoding="utf-8")
        state["provider_calls_started"] = 1
        return "continue", None, target_home, [{"input_tokens": 1}]

    monkeypatch.setattr(study_runner, "_prepare_context", generated_context)
    monkeypatch.setattr(
        study_runner,
        "_credential_mount",
        lambda *unused: (_ for _ in ()).throw(study_runner.StudyRunError("setup")),
    )

    with pytest.raises(study_runner.StudyRunError, match="setup"):
        study_runner.execute(
            args,
            run,
            evaluation.parent,
            source.read_text(encoding="utf-8"),
        )

    run_dir = next(args.output.iterdir())
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "context_ready"
    assert state["provider_calls_started"] == 1
    assert state["retry_safe"] is True


@pytest.mark.parametrize("condition", ["full", "migrate"])
def test_verification_checkpoint_resumes_without_another_provider_call(
    tmp_path, condition
):
    evaluation = prepare_study(tmp_path)
    payload = json.loads(evaluation.read_text(encoding="utf-8"))
    selected = next(
        run
        for run in payload["runs"]
        if (run["case"], run["band"], run["condition"], run["replicate"])
        == ("superseded-decision", "short", condition, 1)
    )
    valid_verify_command = selected["verify_command"]
    selected["verify_command"] = ["missing-verifier-for-resume-test"]
    evaluation.write_text(json.dumps(payload), encoding="utf-8")
    claude = tmp_path / "claude-fake"
    codex = tmp_path / "codex-fake"
    migration = tmp_path / "migration-fake"
    write_fake_agent(claude)
    write_fake_agent(codex)
    write_fake_migration(migration)
    output = tmp_path / "results"
    argv = [
        *command(evaluation, output, condition, "codex", claude, codex, migration),
        "--execute",
        "--acknowledge-provider-cost",
    ]

    interrupted = subprocess.run(
        argv,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert interrupted.returncode != 0
    run_dir = next(output.iterdir())
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "continuation_complete"
    assert state["retry_safe"] is True
    assert len(fake_calls(run_dir)) == 1

    if condition == "migrate":
        migration.unlink()
    selected["verify_command"] = valid_verify_command
    evaluation.write_text(json.dumps(payload), encoding="utf-8")
    resumed = subprocess.run(
        [*argv, "--resume"],
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["task_success"] is True
    assert len(fake_calls(run_dir)) == 1
    resumed_state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert resumed_state["provenance"]["resume_runner_sha256"]
    assert resumed_state["provenance"]["resume_runner_git_revision"]


@pytest.mark.parametrize(
    ("client", "home_env", "credential_name"),
    [
        ("codex", "CODEX_HOME", "auth.json"),
        ("claude", "CLAUDE_CONFIG_DIR", ".credentials.json"),
    ],
)
def test_oauth_credentials_use_an_empty_read_only_mount_point(
    tmp_path,
    monkeypatch,
    client,
    home_env,
    credential_name,
):
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    source = source_home / credential_name
    source.write_text("opaque-synthetic-credential", encoding="utf-8")
    monkeypatch.setenv(home_env, str(source_home))
    isolated = tmp_path / "isolated"
    isolated.mkdir()

    mode, mount_source = _credential_mount(
        SimpleNamespace(credential_mode="auto"),
        client,
        isolated,
    )

    assert mode == "read_only_mount"
    assert mount_source == source
    placeholder = isolated / credential_name
    assert placeholder.is_file()
    assert not placeholder.is_symlink()
    assert placeholder.read_bytes() == b""
    assert source.read_text(encoding="utf-8") == "opaque-synthetic-credential"


def test_bubblewrap_exposes_oauth_credential_read_only(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    native_home = tmp_path / "native"
    native_home.mkdir()
    source = tmp_path / "synthetic-auth.json"
    source.write_text("opaque-synthetic-credential", encoding="utf-8")
    (native_home / source.name).write_text("", encoding="utf-8")
    script = """
import json
from pathlib import Path
p = Path('/mnt/native/synthetic-auth.json')
readable = p.read_text(encoding='utf-8') == 'opaque-synthetic-credential'
try:
    p.write_text('changed', encoding='utf-8')
    writable = True
except OSError:
    writable = False
print(json.dumps({'readable': readable, 'writable': writable}))
"""
    command = _sandbox_agent(
        [sys.executable, "-c", script],
        executable="bwrap",
        workspace=workspace,
        native_home=native_home,
        hidden_paths=[ROOT],
        credential_source=source,
    )

    result = subprocess.run(command, text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"readable": True, "writable": False}
    assert source.read_text(encoding="utf-8") == "opaque-synthetic-credential"


def test_bubblewrap_exposes_codex_code_mode_companion(tmp_path):
    bin_dir = tmp_path / "codex-bin"
    bin_dir.mkdir()
    executable = bin_dir / "codex"
    companion = bin_dir / "codex-code-mode-host"
    for path in (executable, companion):
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)

    command = _sandbox_agent(
        [str(executable), "--version"],
        executable="bwrap",
        workspace=tmp_path / "workspace",
        native_home=tmp_path / "native",
        hidden_paths=[],
        credential_source=None,
    )

    assert command.count(str(companion.resolve())) == 2


def test_handoff_generation_sandbox_cannot_read_fixture_repository(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    hidden = fixture / "gold-bearing-test.py"
    hidden.write_text("authoritative_fact = 15\n", encoding="utf-8")
    workspace = tmp_path / "empty-input"
    workspace.mkdir()
    native_home = tmp_path / "native"
    native_home.mkdir()
    command = _sandbox_agent(
        [
            sys.executable,
            "-c",
            f"from pathlib import Path; print(Path({str(hidden)!r}).exists())",
        ],
        executable="bwrap",
        workspace=workspace,
        native_home=native_home,
        hidden_paths=[fixture, ROOT],
        credential_source=None,
    )

    result = subprocess.run(command, text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
