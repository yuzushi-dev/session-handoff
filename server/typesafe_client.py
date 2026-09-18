"""TypeSafe AI (Jev System One) Client for Session Handoff.

Provides structured judgment primitives (Choice, Score, Noul) with strict
bidirectional secret redaction, offline heuristic fallback, and sub-second
fail-open execution. Conforms to TypeSafe System One API (POST /v1/systemone).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Callable
import urllib.error
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

try:
    from server.sanitize import redact_secrets
except ImportError:
    _SECRET_PATTERN = re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b|"
        r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}|"
        r"postgres(?:ql)?://[^@\s]+@[^\s/]+|"
        r"(?:key|token|secret|password|passwd)\s*[:=]\s*['\"][^'\"]+['\"]",
        flags=re.IGNORECASE,
    )

    def redact_secrets(text: str) -> tuple[str, list[str]]:
        findings = _SECRET_PATTERN.findall(text)
        redacted = _SECRET_PATTERN.sub("[REDACTED]", text)
        return redacted, findings


@dataclass
class Question:
    type: str
    instructions: Any
    criteria: Any = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.type,
            "instructions": self.instructions,
        }
        if self.criteria is not None:
            data["criteria"] = self.criteria
        return data


@dataclass
class ChoiceQuestion(Question):
    def __init__(self, instructions: Any, criteria: dict[str, Any]) -> None:
        super().__init__(type="choice", instructions=instructions, criteria=criteria)


@dataclass
class ScoreQuestion(Question):
    def __init__(self, instructions: Any, criteria: list[str]) -> None:
        super().__init__(type="score", instructions=instructions, criteria=criteria)


@dataclass
class NoulQuestion(Question):
    def __init__(self, instructions: Any, criteria: dict[str, str] | None = None) -> None:
        super().__init__(type="noul", instructions=instructions, criteria=criteria)


@dataclass
class Answer:
    value: Any
    confidence: float = 1.0
    probabilities: dict[str, float] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    type: str = ""


@dataclass
class EvaluationResult:
    ok: bool
    answers: dict[str, Answer]
    model: str = "jev-latest"
    elapsed_ms: float = 0.0
    error: str | None = None
    offline: bool = False
    usage: dict[str, int] = field(default_factory=dict)


class TypeSafeClient:
    """Fail-open client for TypeSafe System One judgments with bidirectional redaction."""

    _auth_warned = False

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.typesafe.ai/v1",
        timeout: float = 1.5,
        model: str = "jev-latest",
        offline_fallback: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.default_model = model
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

    def _redact_questions(self, questions: dict[str, Question]) -> dict[str, Question]:
        redacted: dict[str, Question] = {}
        for q_id, q in questions.items():
            if isinstance(q, ChoiceQuestion):
                redacted[q_id] = ChoiceQuestion(
                    instructions=self._redact_value(q.instructions),
                    criteria=self._redact_value(q.criteria),
                )
            elif isinstance(q, ScoreQuestion):
                redacted[q_id] = ScoreQuestion(
                    instructions=self._redact_value(q.instructions),
                    criteria=self._redact_value(q.criteria),
                )
            elif isinstance(q, NoulQuestion):
                redacted[q_id] = NoulQuestion(
                    instructions=self._redact_value(q.instructions),
                )
            else:
                redacted[q_id] = q
        return redacted

    def evaluate(
        self,
        state: dict[str, Any],
        questions: dict[str, Question],
        *,
        timeout: float | None = None,
        model: str | None = None,
        offline_handler: Callable[[dict[str, Any], dict[str, Question]], dict[str, Answer]] | None = None,
    ) -> EvaluationResult:
        """Evaluate typed questions over state with secret redaction and fail-open handling."""
        start_time = time.perf_counter()
        call_timeout = timeout or self.timeout
        eval_model = model or self.default_model

        # Step 1: Redact both state AND questions for comprehensive privacy
        safe_state = self._redact_value(state)
        safe_questions = self._redact_questions(questions)

        # Step 2: Offline evaluation when unconfigured
        if not self.api_key:
            if offline_handler:
                try:
                    answers = offline_handler(safe_state, safe_questions)
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
                        error=f"Offline handler error: {exc}",
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

        # Step 3: Build HTTP request conforming to /v1/systemone
        payload = {
            "state": safe_state,
            "model": eval_model,
            "questions": {k: q.to_dict() for k, q in safe_questions.items()},
        }
        body = json.dumps(payload).encode("utf-8")
        url = f"{self.base_url}/systemone"
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
                if not isinstance(q_ans, dict):
                    continue
                val = q_ans.get("value")
                if val is None:
                    if "noul" in q_ans:
                        val = q_ans["noul"]
                    elif "choice" in q_ans:
                        val = q_ans["choice"]
                    elif "score" in q_ans:
                        val = q_ans["score"]

                answers[q_id] = Answer(
                    value=val,
                    confidence=float(q_ans.get("confidence", 1.0 if "noul" in q_ans else 0.0)),
                    probabilities=q_ans.get("probabilities", {}),
                    raw=q_ans,
                    type=q_ans.get("type", ""),
                )
            return EvaluationResult(
                ok=True,
                answers=answers,
                model=data.get("model", eval_model),
                elapsed_ms=elapsed,
                offline=False,
                usage=data.get("usage", {}),
            )
        except urllib.error.HTTPError as http_err:
            elapsed = (time.perf_counter() - start_time) * 1000
            if http_err.code in (401, 403) and not TypeSafeClient._auth_warned:
                TypeSafeClient._auth_warned = True
                logger.warning("TypeSafe API key rejected (HTTP %d). Check TYPESAFE_API_KEY.", http_err.code)

            if self.offline_fallback and offline_handler:
                try:
                    answers = offline_handler(safe_state, safe_questions)
                    return EvaluationResult(
                        ok=True,
                        answers=answers,
                        model="offline-fallback-heuristic",
                        elapsed_ms=elapsed,
                        error=f"Live request returned HTTP {http_err.code}; fell back to offline handler",
                        offline=True,
                    )
                except Exception:
                    pass
            return EvaluationResult(
                ok=False,
                answers={},
                model=eval_model,
                elapsed_ms=elapsed,
                error=f"TypeSafe HTTP {http_err.code}: {http_err.reason}",
                offline=False,
            )
        except Exception as exc:
            elapsed = (time.perf_counter() - start_time) * 1000
            if self.offline_fallback and offline_handler:
                try:
                    answers = offline_handler(safe_state, safe_questions)
                    return EvaluationResult(
                        ok=True,
                        answers=answers,
                        model="offline-fallback-heuristic",
                        elapsed_ms=elapsed,
                        error=f"Live request failed ({exc}); fell back to offline handler",
                        offline=True,
                    )
                except Exception:
                    pass
            return EvaluationResult(
                ok=False,
                answers={},
                model=eval_model,
                elapsed_ms=elapsed,
                error=f"TypeSafe evaluation failed: {exc}",
                offline=False,
            )
