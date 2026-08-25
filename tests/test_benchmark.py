import importlib.util
import itertools
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def load_module(name, relative):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


renderer = load_module("handoff_benchmark_renderer", "benchmark/render_fixture.py")
scoring = load_module("handoff_benchmark_scoring", "benchmark/score.py")


def fixture_data():
    return renderer.load_spec(ROOT / "benchmark/fixtures/context_rot_cases.json")


def test_fixture_suite_targets_context_rot_failure_modes():
    data = fixture_data()
    ids = {case["id"] for case in data["cases"]}
    assert ids == {
        "buried-constraint",
        "superseded-decision",
        "failed-attempt-trap",
        "partial-state",
        "late-correction",
        "compound-rot",
    }
    compound = renderer.select_case(data, "compound-rot")
    categories = {fact["category"] for fact in compound["gold_facts"]}
    assert {"constraint", "decision", "failed_attempt", "pending", "test"} <= categories
    assert len(compound["stale_traps"]) >= 3
    assert all(isinstance(fact.get("critical"), bool) for case in data["cases"] for fact in case["gold_facts"])


def test_renderer_scales_noise_without_changing_semantic_anchors():
    data = fixture_data()
    case = renderer.select_case(data, "superseded-decision")
    short = renderer.render(case, data["bands"]["short"])
    very_long = renderer.render(case, data["bands"]["very_long"])

    assert len(short) >= data["bands"]["short"]
    assert len(very_long) >= data["bands"]["very_long"]
    assert len(very_long) > len(short) * 5
    for anchor in case["anchors"]:
        assert anchor["content"] in short
        assert anchor["content"] in very_long
    assert short.index("Initial decision: use a 60 second") < short.index("Final decision: replace")
    assert very_long.index("Initial decision: use a 60 second") < very_long.index("Final decision: replace")


def test_rendered_transcript_never_leaks_gold_annotations():
    data = fixture_data()
    case = renderer.select_case(data, "compound-rot")
    transcript = renderer.render(case, data["bands"]["short"])

    assert "gold_facts" not in transcript
    assert "stale_traps" not in transcript
    assert "F1" not in transcript
    assert "T1" not in transcript
    for trap in case["stale_traps"]:
        assert trap["statement"] not in transcript


def test_oracle_contains_current_state_but_not_stale_traps():
    data = fixture_data()
    case = renderer.select_case(data, "failed-attempt-trap")
    oracle = renderer.render_oracle(case)

    for fact in case["gold_facts"]:
        assert fact["statement"] in oracle
    assert "NFC normalization" in oracle
    assert "Retry or expand the regex-only sanitizer" not in oracle


def test_score_run_reports_weighted_context_loss_and_stale_intrusion():
    run = {
        "case": "fixture",
        "band": "long",
        "condition": "handoff",
        "facts": [
            {"id": "F1", "weight": 3, "critical": True, "status": "preserved"},
            {"id": "F2", "weight": 1, "critical": False, "status": "missing"},
            {"id": "F3", "weight": 2, "critical": True, "status": "incorrect"},
        ],
        "stale_traps": [
            {"id": "T1", "weight": 3, "activated": True},
            {"id": "T2", "weight": 1, "activated": False},
        ],
        "dod": [
            {"id": "D1", "weight": 2, "passed": True},
            {"id": "D2", "weight": 2, "passed": False},
        ],
        "task_success": False,
        "repeated_failed_attempts": 1,
        "stale_decisions_acted_on": 1,
        "recovery_reads": 2,
    }

    result = scoring.score_run(run)
    assert result["rcr"] == 1 / 3
    assert result["weighted_rcr"] == 3 / 6
    assert result["critical_rcr"] == 1 / 2
    assert result["incorrect_fact_rate"] == 1 / 3
    assert result["stale_context_intrusion"] == 3 / 4
    assert result["dod_pass_rate"] == 1 / 2
    assert result["task_success"] is False


def test_aggregate_keeps_conditions_separate():
    base = {
        "case": "fixture",
        "band": "long",
        "facts": [{"id": "F1", "weight": 1, "critical": True, "status": "preserved"}],
        "stale_traps": [{"id": "T1", "weight": 1, "activated": False}],
        "dod": [{"id": "D1", "weight": 1, "passed": True}],
        "repeated_failed_attempts": 0,
        "stale_decisions_acted_on": 0,
        "recovery_reads": 0,
    }
    handoff = scoring.score_run({**base, "condition": "handoff", "task_success": True})
    full = scoring.score_run({**base, "condition": "full", "task_success": False})
    summary = scoring.aggregate([handoff, full])

    assert summary["handoff"]["task_success_rate"] == 1.0
    assert summary["full"]["task_success_rate"] == 0.0


def test_example_evaluation_is_scoreable():
    payload = json.loads((ROOT / "benchmark/evaluation.example.json").read_text(encoding="utf-8"))
    result = scoring.score_evaluation(payload)
    assert result["runs"][0]["condition"] == "handoff"
    assert 0 <= result["runs"][0]["weighted_rcr"] <= 1


def test_prepare_study_emits_a_complete_versioned_manifest(tmp_path):
    output = tmp_path / "study"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/prepare_study.py"),
            str(ROOT / "benchmark/fixtures/context_rot_cases.json"),
            "--output",
            str(output),
            "--runs-per-condition",
            "1",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    evaluation = json.loads((output / "evaluation.json").read_text(encoding="utf-8"))
    scoring.validate_study_manifest(evaluation)
    assert evaluation["schema_version"] == 1
    assert len(evaluation["runs"]) == 6 * 3 * 4
    assert all("critical" in fact for run in evaluation["runs"] for fact in run["facts"])


def valid_run(**overrides):
    run = {
        "case": "fixture",
        "band": "long",
        "condition": "handoff",
        "replicate": 1,
        "facts": [
            {"id": "F1", "weight": 3, "critical": True, "status": "preserved"}
        ],
        "stale_traps": [{"id": "T1", "weight": 1, "activated": False}],
        "dod": [{"id": "D1", "weight": 1, "passed": True}],
        "task_success": True,
        "repeated_failed_attempts": 0,
        "stale_decisions_acted_on": 0,
        "recovery_reads": 0,
        "input_tokens": None,
        "output_tokens": None,
        "wall_seconds": None,
    }
    run.update(overrides)
    return run


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_success", None),
        ("repeated_failed_attempts", -1),
        ("stale_decisions_acted_on", 1.5),
        ("recovery_reads", True),
        ("input_tokens", -1),
    ],
)
def test_score_run_rejects_invalid_scalar_fields(field, value):
    with pytest.raises(ValueError):
        scoring.score_run(valid_run(**{field: value}))


def test_score_run_rejects_duplicate_item_ids():
    fact = {"id": "F1", "weight": 1, "critical": True, "status": "preserved"}
    with pytest.raises(ValueError, match="duplicate fact id"):
        scoring.score_run(valid_run(facts=[fact, fact]))


def test_validate_evaluation_requires_exact_cartesian_runs():
    payload = {
        "schema_version": 1,
        "study": {
            "cases": ["fixture"],
            "bands": ["long"],
            "conditions": ["full", "handoff"],
            "runs_per_condition": 1,
        },
        "runs": [valid_run()],
    }

    with pytest.raises(ValueError, match="missing runs"):
        scoring.validate_evaluation(payload)

    payload["runs"].append(valid_run(condition="full"))
    scoring.validate_evaluation(payload)
    payload["runs"].append(valid_run(condition="full"))
    with pytest.raises(ValueError, match="duplicate run"):
        scoring.validate_evaluation(payload)


def test_handoff_fidelity_gate_requires_all_critical_context_and_no_stale_intrusion():
    good = scoring.score_run(valid_run())
    bad = scoring.score_run(
        valid_run(
            replicate=2,
            facts=[
                {"id": "F1", "weight": 3, "critical": True, "status": "missing"}
            ],
            stale_traps=[{"id": "T1", "weight": 1, "activated": True}],
        )
    )

    assert scoring.handoff_fidelity_gate([good])["passed"] is True
    failed = scoring.handoff_fidelity_gate([good, bad])
    assert failed["passed"] is False
    assert {failure["metric"] for failure in failed["failures"]} == {
        "critical_rcr",
        "stale_context_intrusion",
    }


def test_release_gate_rejects_pilot_scope_and_missing_calibration():
    payload = {
        "schema_version": 1,
        "study": {
            "cases": ["superseded-decision"],
            "bands": ["long"],
            "conditions": ["full", "handoff", "migrate", "oracle"],
            "runs_per_condition": 1,
        },
        "runs": [
            valid_run(case="superseded-decision", condition=condition)
            for condition in ("full", "handoff", "migrate", "oracle")
        ],
    }

    result = scoring.score_evaluation(payload)

    assert result["handoff_fidelity_gate"]["passed"] is True
    assert result["release_gate"]["passed"] is False
    assert {failure["metric"] for failure in result["release_gate"]["failures"]} >= {
        "study_cases",
        "study_bands",
        "runs_per_condition",
        "condition_blind",
        "human_reviewed",
    }


def release_payload():
    cases = list(scoring.RELEASE_CASES)
    bands = list(scoring.RELEASE_BANDS)
    conditions = list(scoring.RELEASE_CONDITIONS)
    runs = [
        valid_run(case=case, band=band, condition=condition, replicate=replicate)
        for case, band, condition, replicate in itertools.product(
            cases, bands, conditions, range(1, 3)
        )
    ]
    return {
        "schema_version": 1,
        "study": {
            "cases": cases,
            "bands": bands,
            "conditions": conditions,
            "runs_per_condition": 2,
        },
        "judging": {
            "condition_blind": True,
            "judge_id": "calibrated-judge",
            "judge_model": "fixture-model",
            "calibration_set": "fixture-calibration-v1",
            "calibration_sample_size": 18,
            "agreement": 0.9,
            "human_reviewed": True,
            "critical_disagreements_adjudicated": True,
            "covered_cases": cases,
            "covered_bands": bands,
            "covered_conditions": conditions,
        },
        "runs": runs,
    }


def test_release_gate_accepts_complete_calibrated_study():
    payload = release_payload()

    result = scoring.score_evaluation(payload)

    assert result["handoff_fidelity_gate"]["passed"] is True
    assert result["release_gate"] == {"passed": True, "failures": []}


def test_release_gate_requires_product_and_oracle_continuations_to_succeed():
    payload = release_payload()
    failed_run = next(run for run in payload["runs"] if run["condition"] == "migrate")
    failed_run["task_success"] = False
    failed_run["dod"][0]["passed"] = False

    result = scoring.score_evaluation(payload)

    assert result["handoff_fidelity_gate"]["passed"] is True
    assert result["release_gate"]["passed"] is False
    assert {failure["metric"] for failure in result["release_gate"]["failures"]} == {
        "task_success",
        "dod_pass_rate",
    }


def test_release_gate_rejects_malformed_calibration_without_crashing():
    payload = release_payload()
    payload["judging"]["agreement"] = float("nan")
    payload["judging"]["calibration_sample_size"] = 17
    payload["judging"]["covered_cases"] = [{}]

    result = scoring.score_evaluation(payload)

    assert result["release_gate"]["passed"] is False
    assert {failure["metric"] for failure in result["release_gate"]["failures"]} == {
        "calibration_agreement",
        "calibration_sample_size",
        "calibration_covered_cases",
    }
