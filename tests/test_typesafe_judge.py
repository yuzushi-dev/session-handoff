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


def test_client_redacts_secrets_in_state():
    client = TypeSafeClient(offline_fallback=True)
    captured_state = {}

    def mock_handler(state, questions):
        nonlocal captured_state
        captured_state = state
        return {}

    state = {
        "normal": "hello world",
        "nested": {"token": "ghp_1234567890abcdefghijklmnopqrstuvwxyz"},
    }
    client.evaluate(state, {}, offline_handler=mock_handler)

    assert "ghp_" not in str(captured_state)
    assert "[REDACTED" in str(captured_state) or "token" in captured_state.get("nested", {})


def test_judge_blind_run_end_to_end():
    with tempfile.TemporaryDirectory() as tmpdir:
        blind_dir = Path(tmpdir)
        judge_payload = {
            "schema_version": 1,
            "blind_id": "test-blind-001",
            "facts": [
                {"id": "F1", "statement": "Must preserve function refresh_session synchronously.", "status": None, "evidence": None},
                {"id": "F2", "statement": "Non-existent missing fact.", "status": None, "evidence": None}
            ],
            "stale_traps": [
                {"id": "T1", "statement": "Convert refresh_session to async.", "activated": None, "evidence": None}
            ],
            "dod": [
                {"id": "D1", "statement": "Function signature unchanged.", "automated_pass": True, "passed": None, "evidence": None}
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
            }
        }
        (blind_dir / "judge.json").write_text(json.dumps(judge_payload), encoding="utf-8")
        (blind_dir / "supplied-context.md").write_text(
            "Hard constraint: refresh_session must remain synchronous and preserve signature.", encoding="utf-8"
        )
        (blind_dir / "continuation.txt").write_text("I completed the fix keeping refresh_session synchronous.", encoding="utf-8")
        (blind_dir / "verify.stdout").write_text("tests passed 10/10", encoding="utf-8")

        result = judge_blind_run(blind_dir)

        assert result["facts"][0]["status"] == "preserved"
        assert result["facts"][1]["status"] == "missing"
        assert result["stale_traps"][0]["activated"] is False
        assert result["dod"][0]["passed"] is True
        assert result["calibration"]["judge_id"] == "typesafe-jev"
