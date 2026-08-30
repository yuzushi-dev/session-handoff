import json
import stat
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
IMPORTER = ROOT / "benchmark/import_judgments.py"


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def fixture(tmp_path: Path):
    run = {
        "case": "fixture",
        "band": "long",
        "condition": "handoff",
        "handoff_format": "markdown-v1",
        "replicate": 1,
        "arm_order": ["markdown-v1"],
        "arm_position": 1,
        "execution_started_at_ns": 1,
        "source_sha256": "source-1",
        "pair_fingerprint": "pair-1",
        "workspace_template": "fixture/workspace",
        "verify_command": ["python3", "-c", "pass"],
        "acceptance_command": ["python3", "-c", "pass"],
        "facts": [
            {
                "id": "F1",
                "weight": 3,
                "critical": True,
                "statement": "Keep the active decision.",
                "status": None,
            }
        ],
        "stale_traps": [
            {
                "id": "T1",
                "weight": 2,
                "statement": "Use the obsolete decision.",
                "activated": None,
            }
        ],
        "dod": [
            {
                "id": "D1",
                "weight": 3,
                "statement": "Focused behavior passes.",
                "passed": None,
            }
        ],
        "task_success": None,
        "repeated_failed_attempts": None,
        "stale_decisions_acted_on": None,
        "recovery_reads": None,
        "input_tokens": None,
        "output_tokens": None,
        "wall_seconds": None,
        "supplied_context_bytes": None,
    }
    evaluation = {
        "schema_version": 1,
        "study": {
            "cases": ["fixture"],
            "bands": ["long"],
            "conditions": ["handoff"],
            "runs_per_condition": 1,
        },
        "runs": [run],
    }
    evaluation_path = tmp_path / "evaluation.json"
    write_json(evaluation_path, evaluation)

    results = tmp_path / "results"
    mapping = {
        "blind-1": {
            "run_id": "run-1",
            "case": "fixture",
            "band": "long",
            "condition": "handoff",
            "client": "codex",
            "model": "synthetic-model",
            "revision": "revision-1",
            "source_sha256": "source-1",
            "pair_fingerprint": "pair-1",
            "arm_order": ["markdown-v1"],
            "arm_position": 1,
            "execution_started_at_ns": 1,
            "replicate": 1,
        }
    }
    write_json(results / "private/blind-map.json", mapping)
    completed = dict(run)
    completed.update(
        task_success=True,
        repeated_failed_attempts=None,
        stale_decisions_acted_on=None,
        recovery_reads=None,
        input_tokens=101,
        output_tokens=23,
        wall_seconds=1.5,
        supplied_context_bytes=123,
        run_id="run-1",
        client="codex",
        model="synthetic-model",
        revision="revision-1",
        source_sha256="source-1",
        pair_fingerprint="pair-1",
        arm_order=["markdown-v1"],
        arm_position=1,
        execution_started_at_ns=1,
    )
    completed["dod"] = [dict(run["dod"][0], passed=True)]
    write_json(results / "run-1/evaluation-run.json", completed)
    judge = {
        "schema_version": 1,
        "blind_id": "blind-1",
        "artifacts": {},
        "facts": [
            {
                "id": "F1",
                "statement": "Keep the active decision.",
                "status": "preserved",
                "evidence": "supplied-context.md: active decision",
            }
        ],
        "stale_traps": [
            {
                "id": "T1",
                "statement": "Use the obsolete decision.",
                "activated": False,
                "evidence": "continuation.txt: obsolete value ignored",
            }
        ],
        "dod": [
            {
                "id": "D1",
                "statement": "Focused behavior passes.",
                "automated_pass": True,
                "passed": True,
                "evidence": "acceptance.stdout: pass",
            }
        ],
        "counters": {
            "repeated_failed_attempts": 0,
            "stale_decisions_acted_on": 0,
            "recovery_reads": 0,
            "evidence": "trace.json: no repeated or stale action",
        },
        "calibration": {
            "rubric_version": 1,
            "judge_id": "judge-1",
            "judge_model": "judge-model",
            "calibration_set": None,
            "human_reviewed": False,
        },
    }
    write_json(results / "blinded/blind-1/judge.json", judge)
    judging = {
        "condition_blind": True,
        "judge_id": "judge-1",
        "judge_model": "judge-model",
        "calibration_set": None,
        "calibration_sample_size": 0,
        "agreement": None,
        "human_reviewed": False,
        "critical_disagreements_adjudicated": False,
        "covered_cases": ["fixture"],
        "covered_bands": ["long"],
        "covered_conditions": ["handoff"],
    }
    judging_path = tmp_path / "judging.json"
    write_json(judging_path, judging)
    return evaluation_path, results, judging_path, evaluation


def run_import(evaluation: Path, results: Path, judging: Path, output: Path):
    return subprocess.run(
        [
            sys.executable,
            str(IMPORTER),
            str(evaluation),
            str(results),
            "--judging",
            str(judging),
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_imports_complete_blinded_judgment_without_mutating_source(tmp_path):
    evaluation, results, judging, original = fixture(tmp_path)
    output = tmp_path / "judged.json"

    result = run_import(evaluation, results, judging, output)

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary == {"output": str(output.resolve()), "runs": 1}
    merged = json.loads(output.read_text())
    assert merged["runs"][0]["facts"][0]["status"] == "preserved"
    assert merged["runs"][0]["facts"][0]["evidence"]
    assert merged["runs"][0]["repeated_failed_attempts"] == 0
    assert merged["runs"][0]["task_success"] is True
    assert merged["runs"][0]["supplied_context_bytes"] == 123
    assert merged["runs"][0]["pair_fingerprint"] == "pair-1"
    assert merged["judging"]["judge_id"] == "judge-1"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(evaluation.read_text()) == original


def test_rejects_blind_mapping_path_outside_results(tmp_path):
    evaluation, results, judging, _ = fixture(tmp_path)
    original_judge = results / "blinded/blind-1/judge.json"
    escaped_judge = results / "outside/judge.json"
    judge = json.loads(original_judge.read_text())
    judge["blind_id"] = "../outside"
    write_json(escaped_judge, judge)
    mapping = json.loads((results / "private/blind-map.json").read_text())
    mapping["../outside"] = mapping.pop("blind-1")
    write_json(results / "private/blind-map.json", mapping)

    result = run_import(
        evaluation, results, judging, tmp_path / "judged.json"
    )

    assert result.returncode != 0
    assert "outside" in result.stderr


def test_rejects_pair_identity_mismatch(tmp_path):
    evaluation, results, judging, _ = fixture(tmp_path)
    mapping = json.loads((results / "private/blind-map.json").read_text())
    mapping["blind-1"]["pair_fingerprint"] = "different-pair"
    write_json(results / "private/blind-map.json", mapping)

    result = run_import(evaluation, results, judging, tmp_path / "judged.json")

    assert result.returncode != 0
    assert "pair_fingerprint" in result.stderr


def test_rejects_completed_run_identity_mismatch(tmp_path):
    evaluation, results, judging, _ = fixture(tmp_path)
    completed_path = results / "run-1/evaluation-run.json"
    completed = json.loads(completed_path.read_text())
    completed["model"] = "different-model"
    write_json(completed_path, completed)

    result = run_import(
        evaluation, results, judging, tmp_path / "judged.json"
    )

    assert result.returncode != 0
    assert "identity" in result.stderr


def test_rejects_judgment_that_changes_automated_result(tmp_path):
    evaluation, results, judging, _ = fixture(tmp_path)
    judge_path = results / "blinded/blind-1/judge.json"
    judge = json.loads(judge_path.read_text())
    judge["dod"][0]["automated_pass"] = False
    write_json(judge_path, judge)

    result = run_import(
        evaluation, results, judging, tmp_path / "judged.json"
    )

    assert result.returncode != 0
    assert "automated_pass" in result.stderr


def test_rejects_judge_identity_mismatch(tmp_path):
    evaluation, results, judging_path, _ = fixture(tmp_path)
    judging = json.loads(judging_path.read_text())
    judging["judge_id"] = "different-judge"
    write_json(judging_path, judging)

    result = run_import(
        evaluation, results, judging_path, tmp_path / "judged.json"
    )

    assert result.returncode != 0
    assert "judge_id" in result.stderr


def test_rejects_incomplete_judgment_set(tmp_path):
    evaluation, results, judging, _ = fixture(tmp_path)
    write_json(results / "private/blind-map.json", {})

    result = run_import(
        evaluation, results, judging, tmp_path / "judged.json"
    )

    assert result.returncode != 0
    assert "incomplete" in result.stderr


def test_refuses_to_overwrite_existing_output(tmp_path):
    evaluation, results, judging, _ = fixture(tmp_path)
    output = tmp_path / "judged.json"
    output.write_text("keep\n", encoding="utf-8")

    result = run_import(evaluation, results, judging, output)

    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert output.read_text() == "keep\n"


def test_refuses_to_replace_dangling_output_symlink(tmp_path):
    evaluation, results, judging, _ = fixture(tmp_path)
    output = tmp_path / "judged.json"
    output.symlink_to(tmp_path / "missing-target")

    result = run_import(evaluation, results, judging, output)

    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert output.is_symlink()


def test_rejects_judgment_label_without_evidence(tmp_path):
    evaluation, results, judging, _ = fixture(tmp_path)
    judge_path = results / "blinded/blind-1/judge.json"
    judge = json.loads(judge_path.read_text())
    judge["facts"][0]["evidence"] = ""
    write_json(judge_path, judge)

    result = run_import(
        evaluation, results, judging, tmp_path / "judged.json"
    )

    assert result.returncode != 0
    assert "evidence" in result.stderr


def test_judged_failed_dod_downgrades_automated_task_success(tmp_path):
    evaluation, results, judging, _ = fixture(tmp_path)
    judge_path = results / "blinded/blind-1/judge.json"
    judge = json.loads(judge_path.read_text())
    judge["dod"][0]["passed"] = False
    write_json(judge_path, judge)
    output = tmp_path / "judged.json"

    result = run_import(evaluation, results, judging, output)

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["runs"][0]["task_success"] is False
