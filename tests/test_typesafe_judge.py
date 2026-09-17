"""Unit tests for TypeSafe client and benchmark judge."""

import json
import tempfile
from pathlib import Path
import pytest

from server.typesafe_client import (
    ChoiceQuestion,
    NoulQuestion,
    ScoreQuestion,
    TypeSafeClient,
)
from benchmark.typesafe_judge import judge_blind_run


def test_question_serialization():
    q_choice = ChoiceQuestion(
        instructions="Which category fits?",
        criteria={"a": "Option A", "b": "Option B"},
    )
    assert q_choice.to_dict()["type"] == "choice"
    assert "criteria" in q_choice.to_dict()

    q_noul = NoulQuestion(instructions="Is this true?")
    assert q_noul.to_dict()["type"] == "noul"

    q_score = ScoreQuestion(instructions="Score?", criteria=["low", "high"])
    assert q_score.to_dict()["type"] == "score"


def test_client_redacts_both_state_and_questions():
    client = TypeSafeClient(offline_fallback=True)
    captured_state = {}
    captured_questions = {}

    def mock_handler(state, questions):
        nonlocal captured_state, captured_questions
        captured_state = state
        captured_questions = questions
        return {}

    state = {
        "normal": "hello world",
        "nested": {"token": "ghp_1234567890abcdefghijklmnopqrstuvwxyz"},
    }
    questions = {
        "q1": NoulQuestion(instructions="Is password='SuperSecretPassword123' present?")
    }
    client.evaluate(state, questions, offline_handler=mock_handler)

    assert "ghp_" not in str(captured_state)
    assert "[REDACTED" in str(captured_state)
    assert "SuperSecretPassword123" not in str(captured_questions)


def test_judge_blind_run_word_boundary_and_rejection():
    with tempfile.TemporaryDirectory() as tmpdir:
        blind_dir = Path(tmpdir)
        judge_payload = {
            "schema_version": 1,
            "blind_id": "test-blind-002",
            "facts": [
                {"id": "F1", "statement": "Must preserve api endpoint synchronously.", "status": None, "evidence": None},
            ],
            "stale_traps": [
                # Has rejection term 'superseded' in continuation -> should NOT activate
                {"id": "T1", "statement": "Use the obsolete 60 second negative cache ttl.", "activated": None, "evidence": None}
            ],
            "dod": [
                {"id": "D1", "statement": "test_cache_ttl passes.", "automated_pass": True, "passed": None, "evidence": None},
                {"id": "D2", "statement": "test_failing_suite passes.", "automated_pass": True, "passed": None, "evidence": None}
            ],
            "counters": {},
            "calibration": {}
        }
        (blind_dir / "judge.json").write_text(json.dumps(judge_payload), encoding="utf-8")
        # Context has rapid, which must not match 'api'
        (blind_dir / "supplied-context.md").write_text(
            "We want rapid development. Also keep api endpoint synchronously.", encoding="utf-8"
        )
        (blind_dir / "continuation.txt").write_text(
            "The 60 second negative cache ttl was superseded. I updated the cache logic.", encoding="utf-8"
        )
        (blind_dir / "verify.stdout").write_text(
            "test_cache_ttl PASSED\nFAILED test_failing_suite\n1 passed, 1 failed in 0.1s", encoding="utf-8"
        )
        (blind_dir / "trace.json").write_text(json.dumps([
            {"tool": "read", "status": "success"},
            {"tool": "bash", "input": "pytest", "status": 1},
            {"tool": "bash", "input": "pytest", "status": 1}
        ]), encoding="utf-8")

        result = judge_blind_run(blind_dir)

        # Fact matched
        assert result["facts"][0]["status"] == "preserved"
        # Stale trap was rejected -> activated is False
        assert result["stale_traps"][0]["activated"] is False
        # D1 passed (in verify stdout), D2 failed (marked FAILED)
        assert result["dod"][0]["passed"] is True
        assert result["dod"][1]["passed"] is False
        # Trace counters extracted
        assert result["counters"]["recovery_reads"] == 1
        assert result["counters"]["repeated_failed_attempts"] == 1


def test_judge_blind_run_fails_loudly_on_eval_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        blind_dir = Path(tmpdir)
        judge_payload = {
            "schema_version": 1,
            "blind_id": "test-err",
            "facts": [],
            "stale_traps": [],
            "dod": [],
        }
        (blind_dir / "judge.json").write_text(json.dumps(judge_payload), encoding="utf-8")

        class FailingClient(TypeSafeClient):
            def evaluate(self, state, questions, **kwargs):
                from server.typesafe_client import EvaluationResult
                return EvaluationResult(ok=False, answers={}, model="err", elapsed_ms=10, error="Simulated API Error")

        with pytest.raises(RuntimeError, match="TypeSafe benchmark evaluation failed"):
            judge_blind_run(blind_dir, client=FailingClient())
