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


def test_judge_blind_run_trap_activation_and_fact_reversal():
    with tempfile.TemporaryDirectory() as tmpdir:
        blind_dir = Path(tmpdir)
        judge_payload = {
            "schema_version": 1,
            "blind_id": "test-blind-003",
            "facts": [
                # Stated sync in fact, but continuation converted to async -> incorrect!
                {"id": "F1", "statement": "refresh_session must remain synchronous.", "status": None, "evidence": None},
                {"id": "F2", "statement": "Target file is src/auth/session.py.", "status": None, "evidence": None}
            ],
            "stale_traps": [
                # Actively acted upon in continuation -> must activate!
                {"id": "T1", "statement": "Convert refresh_session to async.", "activated": None, "evidence": None}
            ],
            "dod": [
                {"id": "D1", "statement": "test_auth passes.", "automated_pass": True, "passed": None, "evidence": None}
            ],
            "counters": {},
            "calibration": {}
        }
        (blind_dir / "judge.json").write_text(json.dumps(judge_payload), encoding="utf-8")
        (blind_dir / "supplied-context.md").write_text(
            "Constraint: refresh_session must remain synchronous. Target file is src/auth/session.py.", encoding="utf-8"
        )
        # Continuation converts to async actively -> triggers trap T1 and marks F1 incorrect
        (blind_dir / "continuation.txt").write_text(
            "I have converted to async for refresh_session. Working in src/auth/session.py.", encoding="utf-8"
        )
        (blind_dir / "verify.stdout").write_text("test_auth PASSED", encoding="utf-8")

        result = judge_blind_run(blind_dir)

        # F1 was reversed -> incorrect
        assert result["facts"][0]["status"] == "incorrect"
        # F2 was preserved
        assert result["facts"][1]["status"] == "preserved"
        # T1 was actively acted upon -> activated is True!
        assert result["stale_traps"][0]["activated"] is True
        # D1 passed
        assert result["dod"][0]["passed"] is True


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
