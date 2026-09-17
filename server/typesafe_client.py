"""TypeSafe AI (Jev System One) Client for structured semantic evaluations.

Provides typed questions (choice, score, noul), automatic secret redaction,
fail-open error handling, and support for offline/calibrated fallback.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

try:
    from .redaction import redact_secrets
except ImportError:
    try:
        from server.redaction import redact_secrets
    except ImportError:
        def redact_secrets(text: str) -> tuple[str, int]:
            return text, 0


QuestionType = Literal["choice", "score", "noul"]


@dataclass(frozen=True)
class ChoiceQuestion:
    instructions: str
    criteria: dict[str, str]
    type: QuestionType = "choice"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "instructions": self.instructions,
            "criteria": self.criteria,
        }


@dataclass(frozen=True)
class ScoreQuestion:
    instructions: str
    criteria: list[str]
    type: QuestionType = "score"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "instructions": self.instructions,
            "criteria": self.criteria,
        }


@dataclass(frozen=True)
class NoulQuestion:
    instructions: str
    type: QuestionType = "noul"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "instructions": self.instructions,
        }


Question = ChoiceQuestion | ScoreQuestion | NoulQuestion


@dataclass
class Answer:
    value: Any
    confidence: float = 1.0
    probabilities: dict[str, float] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationResult:
    ok: bool
    answers: dict[str, Answer]
    model: str
    elapsed_ms: float
    error: str | None = None
    offline: bool = False


class TypeSafeClient:
    """System One client for TypeSafe AI (Jev)."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.typesafe.ai/v1",
        timeout: float = 1.5,
        offline_fallback: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.offline_fallback = offline_fallback
        self.api_key = api_key or self._resolve_api_key()

    @staticmethod
    def _resolve_api_key() -> str | None:
        env_key = os.getenv("TYPESAFE_API_KEY")
        if env_key and env_key.strip():
            return env_key.strip()
        auth_path = Path.home() / ".config" / "typesafe" / "auth.json"
        if auth_path.is_file():
            try:
                data = json.loads(auth_path.read_text(encoding="utf-8"))
                key = data.get("api_key") or data.get("key") or data.get("token")
                if isinstance(key, str) and key.strip():
                    return key.strip()
            except Exception:
                pass
        return None

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            redacted, _ = redact_secrets(value)
            return redacted
        if isinstance(value, dict):
            return {k: self._redact_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact_value(v) for v in value]
        return value

    def evaluate(
        self,
        state: dict[str, Any],
        questions: dict[str, Question],
        *,
        timeout: float | None = None,
        offline_handler: Callable[[dict[str, Any], dict[str, Question]], dict[str, Answer]] | None = None,
    ) -> EvaluationResult:
        """Evaluate typed questions over state with secret redaction and fail-open handling."""
        start_time = time.perf_counter()
        call_timeout = timeout or self.timeout

        # Step 1: Redact state for security and privacy
        safe_state = self._redact_value(state)

        # Step 2: If no key or offline handler provided, use offline evaluation
        if not self.api_key:
            if offline_handler:
                try:
                    answers = offline_handler(safe_state, questions)
                    elapsed = (time.perf_counter() - start_time) * 1000
                    return EvaluationResult(
                        ok=True,
                        answers=answers,
                        model="offline-heuristic",
                        elapsed_ms=elapsed,
                        offline=True,
                    )
                except Exception as exc:
                    elapsed = (time.perf_counter() - start_time) * 1000
                    return EvaluationResult(
                        ok=False,
                        answers={},
                        model="offline-heuristic",
                        elapsed_ms=elapsed,
                        error=str(exc),
                        offline=True,
                    )
            elapsed = (time.perf_counter() - start_time) * 1000
            return EvaluationResult(
                ok=False,
                answers={},
                model="none",
                elapsed_ms=elapsed,
                error="No TYPESAFE_API_KEY configured and no offline handler provided",
                offline=True,
            )

        # Step 3: Build HTTP request
        payload = {
            "state": safe_state,
            "questions": {k: q.to_dict() for k, q in questions.items()},
        }
        body = json.dumps(payload).encode("utf-8")
        url = f"{self.base_url}/evaluate"
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "session-handoff-typesafe/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=call_timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            elapsed = (time.perf_counter() - start_time) * 1000
            answers = {}
            for q_id, q_ans in data.get("answers", {}).items():
                answers[q_id] = Answer(
                    value=q_ans.get("value"),
                    confidence=float(q_ans.get("confidence", 1.0)),
                    probabilities=q_ans.get("probabilities", {}),
                    raw=q_ans,
                )
            return EvaluationResult(
                ok=True,
                answers=answers,
                model=data.get("model", "jev-1"),
                elapsed_ms=elapsed,
                offline=False,
            )
        except Exception as exc:
            elapsed = (time.perf_counter() - start_time) * 1000
            if self.offline_fallback and offline_handler:
                try:
                    answers = offline_handler(safe_state, questions)
                    return EvaluationResult(
                        ok=True,
                        answers=answers,
                        model="offline-fallback-heuristic",
                        elapsed_ms=elapsed,
                        error=f"Live request failed ({exc}), fell back to offline handler",
                        offline=True,
                    )
                except Exception:
                    pass
            return EvaluationResult(
                ok=False,
                answers={},
                model="jev-1",
                elapsed_ms=elapsed,
                error=f"TypeSafe evaluation failed: {exc}",
                offline=False,
            )
