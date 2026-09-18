import json
from pathlib import Path

import pytest

from server import compact_advisor
from server.compact_advisor import (
    advise,
    estimate_usage,
    floor_for,
    main,
    qualifies,
    score,
)


# --- score / floor_for / qualifies: pure math, spot-checked against
# kunchenguid/compact-adviser's own published values ---


def test_floor_for_is_strictest_at_or_below_strict_until():
    assert floor_for(0.0) == 0.9
    assert floor_for(0.1) == 0.9


def test_floor_for_is_loosest_at_or_above_loose_at():
    assert floor_for(0.9) == 0.5
    assert floor_for(1.0) == 0.5


def test_floor_for_interpolates_linearly_at_midpoint():
    # Halfway between 0.1 and 0.9 usage -> halfway between 0.9 and 0.5 floor.
    assert floor_for(0.5) == pytest.approx(0.7)


def test_score_finished_hands_on_scores_near_one():
    assert score(1.0, 1.0) == pytest.approx(1.0)


def test_score_finished_coordinating_scores_near_half():
    assert score(1.0, 0.0) == pytest.approx(0.5)


def test_score_unfinished_scores_near_zero_regardless_of_shape():
    assert score(0.0, 1.0) == pytest.approx(0.0)


def test_qualifies_combines_score_and_floor():
    # score(1.0, 1.0) == 1.0, clears any floor.
    assert qualifies(1.0, 1.0, usage=0.0) is True
    # score(0.6, 0.0) == 0.3, below even the loosest floor (0.5).
    assert qualifies(0.6, 0.0, usage=1.0) is False


# --- estimate_usage ---


def test_estimate_usage_scales_with_transcript_size(tmp_path):
    small = tmp_path / "small.jsonl"
    small.write_text("x" * 400, encoding="utf-8")  # ~100 tokens / 200k window
    assert estimate_usage(str(small)) == pytest.approx(100 / 200_000)


def test_estimate_usage_caps_at_one(tmp_path):
    huge = tmp_path / "huge.jsonl"
    huge.write_text("x" * (compact_advisor.ASSUMED_CONTEXT_WINDOW_TOKENS * compact_advisor.CHARS_PER_TOKEN_ESTIMATE * 2), encoding="utf-8")
    assert estimate_usage(str(huge)) == 1.0


def test_estimate_usage_returns_strictest_signal_on_missing_file(tmp_path):
    missing = tmp_path / "does-not-exist.jsonl"
    assert estimate_usage(str(missing)) == 0.0
    assert floor_for(estimate_usage(str(missing))) == 0.9


# --- advise(): fake client, no network ---


class _FakeAnswer:
    def __init__(self, probabilities):
        self.probabilities = probabilities


class _FakeEvalResult:
    def __init__(self, *, ok=True, answers=None):
        self.ok = ok
        self.answers = answers or {}


class _FakeTypeSafeClient:
    def __init__(self, *, configured=True, ok=True, finished=1.0, hands_on=1.0):
        self._configured = configured
        self._ok = ok
        self._finished = finished
        self._hands_on = hands_on
        self.seen_state = None
        self.seen_questions = None

    def is_configured(self):
        return self._configured

    def evaluate(self, state, questions):
        self.seen_state = state
        self.seen_questions = questions
        if not self._ok:
            return _FakeEvalResult(ok=False)
        return _FakeEvalResult(answers={
            "done": _FakeAnswer({"finished": self._finished, "not_finished": 1 - self._finished, "unclear": 0.0}),
            "shape": _FakeAnswer({"hands_on": self._hands_on, "coordinating": 1 - self._hands_on, "unclear": 0.0}),
        })


def _write_transcript(path: Path, session_id="sess-1"):
    records = [
        {"type": "user", "sessionId": session_id, "uuid": "u1", "message": {"role": "user", "content": "go"}},
        {
            "type": "assistant", "sessionId": session_id, "uuid": "a1", "parentUuid": "u1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_advise_returns_none_when_client_not_configured(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript)
    client = _FakeTypeSafeClient(configured=False)
    assert advise(str(transcript), "sess-1", client=client) is None


def test_advise_returns_none_on_eval_failure(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript)
    client = _FakeTypeSafeClient(ok=False)
    assert advise(str(transcript), "sess-1", client=client) is None


def test_advise_itself_fails_open_on_a_transcript_parsing_exception(tmp_path):
    # advise() must catch its own errors, not rely on main() to do it - a
    # malformed/missing transcript path is enough to exercise this without
    # needing a real paginated-projection failure.
    missing = tmp_path / "does-not-exist.jsonl"
    client = _FakeTypeSafeClient(finished=1.0, hands_on=1.0)
    assert advise(str(missing), "sess-1", client=client) is None


def test_advise_returns_hint_when_finished_and_hands_on(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript)
    client = _FakeTypeSafeClient(finished=1.0, hands_on=1.0)
    hint = advise(str(transcript), "sess-1", client=client)
    assert hint == compact_advisor.HINT_MESSAGE
    # The real transcript content reached the client, not a placeholder.
    assert "done" in client.seen_state["recent_conversation"]


def test_advise_returns_none_when_not_finished(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript)
    client = _FakeTypeSafeClient(finished=0.0, hands_on=1.0)
    assert advise(str(transcript), "sess-1", client=client) is None


def _write_codex_transcript(path: Path, session_id="sess-1"):
    records = [
        {"type": "session_meta", "payload": {"id": session_id}},
        {
            "type": "response_item", "timestamp": None,
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_advise_reads_real_codex_transcript_file(tmp_path):
    # Regression: _recent_transcript_text used to hardcode the Claude
    # parser, so under Codex (session_meta-first transcripts) it would
    # silently fail to extract any conversation content.
    transcript = tmp_path / "t.jsonl"
    _write_codex_transcript(transcript)
    client = _FakeTypeSafeClient(finished=1.0, hands_on=1.0)
    hint = advise(str(transcript), "sess-1", client=client)
    assert hint == compact_advisor.HINT_MESSAGE
    assert "done" in client.seen_state["recent_conversation"]


# --- main(): hook contract, opt-in gating ---


def test_main_is_inert_when_env_var_unset(monkeypatch, tmp_path):
    monkeypatch.delenv(compact_advisor.COMPACT_HINT_ENV, raising=False)
    payload = json.dumps({"transcript_path": str(tmp_path / "missing.jsonl"), "session_id": "s"})
    out = _run_main(payload)
    assert out == "{}"


def test_main_is_inert_for_unrecognized_env_value(monkeypatch, tmp_path):
    monkeypatch.setenv(compact_advisor.COMPACT_HINT_ENV, "ollama")
    payload = json.dumps({"transcript_path": str(tmp_path / "missing.jsonl"), "session_id": "s"})
    out = _run_main(payload)
    assert out == "{}"


def test_main_never_raises_on_malformed_payload(monkeypatch):
    monkeypatch.setenv(compact_advisor.COMPACT_HINT_ENV, "typesafe")
    code = main(stdin_text="not json")
    assert code == 0


def test_main_emits_system_message_when_advise_returns_a_hint(monkeypatch, tmp_path):
    monkeypatch.setenv(compact_advisor.COMPACT_HINT_ENV, "typesafe")
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript)
    monkeypatch.setattr(compact_advisor, "advise", lambda *a, **k: "a hint")
    payload = json.dumps({"transcript_path": str(transcript), "session_id": "sess-1"})
    out = _run_main(payload)
    assert json.loads(out) == {"systemMessage": "a hint"}


def _run_main(stdin_text: str) -> str:
    import io
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(stdin_text=stdin_text, stdout=stdout, stderr=stderr)
    assert code == 0
    return stdout.getvalue().strip()
