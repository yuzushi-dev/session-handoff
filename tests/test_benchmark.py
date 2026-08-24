import importlib.util
import json
from pathlib import Path


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
            {"id": "F1", "weight": 3, "status": "preserved"},
            {"id": "F2", "weight": 1, "status": "missing"},
            {"id": "F3", "weight": 2, "status": "incorrect"},
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
    assert result["incorrect_fact_rate"] == 1 / 3
    assert result["stale_context_intrusion"] == 3 / 4
    assert result["dod_pass_rate"] == 1 / 2
    assert result["task_success"] is False


def test_aggregate_keeps_conditions_separate():
    base = {
        "case": "fixture",
        "band": "long",
        "facts": [{"id": "F1", "weight": 1, "status": "preserved"}],
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
    scored = [scoring.score_run(run) for run in payload["runs"]]
    assert scored[0]["condition"] == "handoff"
    assert 0 <= scored[0]["weighted_rcr"] <= 1
