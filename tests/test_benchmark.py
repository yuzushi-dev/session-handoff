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
    assert len(evaluation["runs"]) == 6 * 3 * 5
    assert all("critical" in fact for run in evaluation["runs"] for fact in run["facts"])
    assert evaluation["study"]["handoff_formats"] == ["markdown-v1", "state-v1"]
    assert {
        run["handoff_format"]
        for run in evaluation["runs"]
        if run["condition"] == "handoff"
    } == {"markdown-v1", "state-v1"}
    assert all(
        run["handoff_format"] == "markdown-v1"
        for run in evaluation["runs"]
        if run["condition"] != "handoff"
    )
    arm_orders = {}
    for run in evaluation["runs"]:
        if run["condition"] != "handoff":
            continue
        key = (run["case"], run["band"], run["replicate"])
        arm_orders.setdefault(key, set()).add(
            (tuple(run["arm_order"]), run["arm_position"])
        )
    assert all(len(entries) == 2 for entries in arm_orders.values())
    assert all(
        {position for _, position in entries} == {1, 2}
        for entries in arm_orders.values()
    )


def test_prepare_study_can_emit_only_the_paired_handoff_candidate(tmp_path):
    output = tmp_path / "study"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/prepare_study.py"),
            str(ROOT / "benchmark/fixtures/context_rot_cases.json"),
            "--output",
            str(output),
            "--runs-per-condition",
            "2",
            "--handoff-only",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    evaluation = json.loads((output / "evaluation.json").read_text(encoding="utf-8"))
    scoring.validate_study_manifest(evaluation)
    assert evaluation["study"]["conditions"] == ["handoff"]
    assert len(evaluation["runs"]) == 6 * 3 * 2 * 2


def test_manifest_rejects_unknown_handoff_format():
    payload = {
        "schema_version": 1,
        "study": {
            "cases": ["fixture"],
            "bands": ["long"],
            "conditions": ["handoff"],
            "handoff_formats": ["markdown-v1", "state-v1"],
            "runs_per_condition": 1,
        },
        "runs": [
            {
                "case": "fixture",
                "band": "long",
                "condition": "handoff",
                "handoff_format": "future-v1",
                "replicate": 1,
            },
            {
                "case": "fixture",
                "band": "long",
                "condition": "handoff",
                "handoff_format": "state-v1",
                "replicate": 1,
            },
        ],
    }

    with pytest.raises(ValueError, match="handoff_format"):
        scoring.validate_study_manifest(payload)


def valid_run(**overrides):
    run = {
        "case": "fixture",
        "band": "long",
        "condition": "handoff",
        "handoff_format": "markdown-v1",
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
        "supplied_context_bytes": None,
        "client": "codex",
        "model": "synthetic-model",
        "revision": "revision-1",
        "source_sha256": "source-1",
        "pair_fingerprint": "pair-1",
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


def candidate_rows():
    rows = []
    for case, band, replicate in itertools.product(
        scoring.RELEASE_CASES,
        scoring.RELEASE_BANDS,
        (1, 2),
    ):
        pair_fingerprint = f"{case}:{band}:{replicate}"
        for handoff_format, arm_position in (
            ("markdown-v1", 1),
            ("state-v1", 2),
        ):
            rows.append(
                scoring.score_run(
                    valid_run(
                        case=case,
                        band=band,
                        replicate=replicate,
                        handoff_format=handoff_format,
                        pair_fingerprint=pair_fingerprint,
                        arm_order=["markdown-v1", "state-v1"],
                        arm_position=arm_position,
                    )
                )
            )
    return rows


def test_structured_state_gate_requires_semantics_task_success_and_complete_dod():
    good_rows = candidate_rows()
    assert scoring.structured_state_gate(good_rows) == {"passed": True, "failures": []}

    bad = next(
        row
        for row in good_rows
        if row["handoff_format"] == "state-v1" and row["replicate"] == 2
    )
    bad["task_success"] = False
    bad["dod_pass_rate"] = 0
    bad["critical_rcr"] = 0
    failed = scoring.structured_state_gate(good_rows)
    assert failed["passed"] is False
    assert {failure["metric"] for failure in failed["failures"]} == {
        "critical_rcr",
        "task_success",
        "dod_pass_rate",
    }

    incomplete = scoring.structured_state_gate(candidate_rows()[:-1])
    assert incomplete["passed"] is False
    assert {failure["metric"] for failure in incomplete["failures"]} == {
        "complete_pairs"
    }


def test_paired_handoff_summary_reports_raw_delta_and_medians():
    markdown = scoring.score_run(
        valid_run(
            supplied_context_bytes=200,
            input_tokens=100,
            recovery_reads=2,
            wall_seconds=4.0,
            arm_order=["markdown-v1", "state-v1"],
            arm_position=1,
        )
    )
    state = scoring.score_run(
        valid_run(
            handoff_format="state-v1",
            supplied_context_bytes=150,
            input_tokens=90,
            recovery_reads=1,
            wall_seconds=3.0,
            arm_order=["markdown-v1", "state-v1"],
            arm_position=2,
        )
    )

    summary = scoring.paired_handoff_summary([markdown, state])

    assert len(summary["pairs"]) == 1
    pair = summary["pairs"][0]
    assert pair["case"] == "fixture"
    assert pair["band"] == "long"
    assert pair["replicate"] == 1
    assert pair["markdown-v1"] == markdown
    assert pair["state-v1"] == state
    assert pair["delta_state_minus_markdown"] == {
        "supplied_context_bytes": -50,
        "input_tokens": -10,
        "recovery_reads": -1,
        "wall_seconds": -1.0,
        "critical_rcr": 0,
        "incorrect_fact_rate": 0,
        "stale_context_intrusion": 0,
        "dod_pass_rate": 0,
        "task_success": 0,
    }
    assert summary["by_format"]["state-v1"]["median_supplied_context_bytes"] == 150
    assert summary["by_format"]["markdown-v1"]["median_input_tokens"] == 100
    assert summary["complete_pairs"] == 1


def test_paired_handoff_summary_rejects_mismatched_pair_identity():
    markdown = scoring.score_run(
        valid_run(
            arm_order=["markdown-v1", "state-v1"],
            arm_position=1,
        )
    )
    state = scoring.score_run(
        valid_run(
            handoff_format="state-v1",
            pair_fingerprint="different-pair",
            arm_order=["markdown-v1", "state-v1"],
            arm_position=2,
        )
    )

    summary = scoring.paired_handoff_summary([markdown, state])

    assert summary["complete_pairs"] == 0
    assert summary["pairs"][0]["delta_state_minus_markdown"] == {}
    assert summary["pairing_errors"][0]["metric"] == "pair_identity"


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
