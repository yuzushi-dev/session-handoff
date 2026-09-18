"""Score tool calls in a transcript for selective retention in a recovery checkpoint."""

from __future__ import annotations

import json
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

try:
    from .migration_engine import _parse_claude, _read_jsonl
except ImportError:  # direct `python server/compaction_scoring.py` execution
    from migration_engine import _parse_claude, _read_jsonl


def pair_tool_events(events: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Pair each tool_call with its tool_result by id, in call order."""
    results_by_id: dict[str, dict[str, Any]] = {
        event["id"]: event
        for event in events
        if event.get("kind") == "tool_result" and isinstance(event.get("id"), str)
    }
    return [
        (event, results_by_id.get(event.get("id")))
        for event in events
        if event.get("kind") == "tool_call"
    ]


@dataclass(frozen=True)
class ScoredItem:
    call: dict[str, Any]
    result: dict[str, Any] | None
    decision: str  # "keep" | "truncate" | "drop" | "ambiguous"
    reason: str


def heuristic_score(
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]],
    *,
    preserve_recent: int = 6,
    truncate_chars: int = 4000,
) -> list[ScoredItem]:
    pinned_from = max(0, len(pairs) - preserve_recent)

    output_counts: dict[tuple[str | None, str], int] = {}
    for call, result in pairs[:pinned_from]:
        if result is None or result.get("is_error"):
            continue
        output = result.get("output")
        output_text = output if isinstance(output, str) else str(output)
        key = (call.get("name"), output_text)
        output_counts[key] = output_counts.get(key, 0) + 1

    seen_outputs: set[tuple[str | None, str]] = set()
    scored: list[ScoredItem] = []
    for index, (call, result) in enumerate(pairs):
        if index >= pinned_from:
            scored.append(ScoredItem(call, result, "keep", "pinned_recent"))
            continue
        if result is None:
            scored.append(ScoredItem(call, result, "keep", "no_result_yet"))
            continue
        if result.get("is_error"):
            scored.append(ScoredItem(call, result, "keep", "error"))
            continue
        output = result.get("output")
        output_text = output if isinstance(output, str) else str(output)
        key = (call.get("name"), output_text)
        is_duplicate = output_counts.get(key, 0) > 1
        if is_duplicate and key in seen_outputs:
            scored.append(ScoredItem(call, result, "drop", "duplicate_output"))
            continue
        seen_outputs.add(key)
        if len(output_text) > truncate_chars:
            scored.append(ScoredItem(call, result, "truncate", "oversized_output"))
            continue
        if is_duplicate:
            scored.append(ScoredItem(call, result, "keep", "duplicate_output_first"))
            continue
        scored.append(ScoredItem(call, result, "ambiguous", "no_heuristic_signal"))
    return scored


CONFIDENCE_FLOOR = 0.6


def resolve_ambiguous(
    items: list[ScoredItem],
    *,
    asker: Any | None,
    deadline: float,
) -> list[ScoredItem]:
    resolved: list[ScoredItem] = []
    for item in items:
        if item.decision != "ambiguous":
            resolved.append(item)
            continue
        if asker is None:
            resolved.append(ScoredItem(item.call, item.result, "keep", "no_asker"))
            continue
        if _time.monotonic() >= deadline:
            resolved.append(ScoredItem(item.call, item.result, "keep", "deadline_reached"))
            continue
        try:
            decision, confidence = asker.ask(item.call, item.result)
        except Exception:
            resolved.append(ScoredItem(item.call, item.result, "keep", "asker_error"))
            continue
        if confidence < CONFIDENCE_FLOOR or decision not in {"keep", "truncate", "drop"}:
            resolved.append(ScoredItem(item.call, item.result, "keep", "low_confidence_default"))
            continue
        source = getattr(asker, "source_name", "local_model")
        resolved.append(ScoredItem(item.call, item.result, decision, source))
    return resolved


def score_pairs(
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]],
    *,
    preserve_recent: int = 6,
    truncate_chars: int = 4000,
    asker: Any | None = None,
    deadline: float | None = None,
) -> list[ScoredItem]:
    heuristic = heuristic_score(pairs, preserve_recent=preserve_recent, truncate_chars=truncate_chars)
    return resolve_ambiguous(heuristic, asker=asker, deadline=deadline if deadline is not None else _time.monotonic())


def score_transcript(
    transcript_path: str,
    *,
    session_id: str,
    preserve_recent: int = 6,
    truncate_chars: int = 4000,
    asker: Any | None = None,
    deadline: float | None = None,
) -> list[ScoredItem]:
    """Parse a real Claude transcript JSONL file and score its tool calls."""
    data = Path(transcript_path).read_bytes()
    records = _read_jsonl(data)
    _metadata, events, _dropped = _parse_claude(records, session_id)
    pairs = pair_tool_events(events)
    return score_pairs(
        pairs,
        preserve_recent=preserve_recent,
        truncate_chars=truncate_chars,
        asker=asker,
        deadline=deadline,
    )


def render_tool_summary(items: list[ScoredItem]) -> list[str]:
    """Render scored tool calls into markdown lines for the checkpoint's tool-summary section."""
    if not items:
        return ["- No tool calls recorded before this compaction."]
    lines: list[str] = []
    for item in items:
        name = item.call.get("name", "<unknown>")
        if item.decision == "drop":
            lines.append(f"- `{name}`: dropped ({item.reason}).")
            continue
        output = "" if item.result is None else item.result.get("output", "")
        output_text = output if isinstance(output, str) else str(output)
        if item.decision == "truncate":
            output_text = output_text[:1000] + f"\n<truncated, reason: {item.reason}>"
        lines.append(f"- `{name}` ({item.decision}, {item.reason}):")
        lines.extend(["", "  ```text", f"  {output_text}", "  ```", ""])
    return lines


OLLAMA_PROMPT = (
    "A recovery checkpoint is being written before a Claude Code session compacts. "
    "Decide whether this tool call and its result still matter enough to keep verbatim. "
    "Tool: {name}\nInput: {input}\nResult: {output}\n"
    'Reply with only a JSON object: {{"decision": "keep"|"truncate"|"drop", "confidence": 0.0-1.0}}'
)


class OllamaAsker:
    source_name = "local_model"

    def __init__(
        self,
        *,
        model: str = "smollm2:1.7b",
        base_url: str = "http://localhost:11434",
        timeout: float = 2.0,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.timeout = timeout

    def ask(self, call: dict[str, Any], result: dict[str, Any] | None) -> tuple[str, float]:
        prompt = OLLAMA_PROMPT.format(
            name=call.get("name", "<unknown>"),
            input=call.get("input", {}),
            output="" if result is None else result.get("output", ""),
        )
        body = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
        }).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/generate",
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            envelope = json.loads(response.read())
        verdict = json.loads(envelope["response"])
        decision = verdict["decision"]
        confidence = float(verdict["confidence"])
        if decision not in {"keep", "truncate", "drop"}:
            raise ValueError(f"unexpected decision: {decision!r}")
        return decision, confidence


TYPESAFE_KEEP_INSTRUCTIONS = (
    "A recovery checkpoint is being written before a Claude Code session compacts. "
    "Does this tool call and its result still matter enough to keep verbatim?"
)
TYPESAFE_HIGH = 0.7
TYPESAFE_LOW = 0.3


class TypeSafeAsker:
    """Alternative to OllamaAsker: same .ask(call, result) contract, backed by
    the real TypeSafe/Jev `noul` primitive instead of a local model. Opt-in
    only: requires an explicitly configured TypeSafeClient (TYPESAFE_API_KEY
    or ~/.config/typesafe/auth.json) and is never constructed by default.
    """

    source_name = "typesafe_remote"

    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            from server.typesafe_client import TypeSafeClient

            client = TypeSafeClient()
        self.client = client

    def ask(self, call: dict[str, Any], result: dict[str, Any] | None) -> tuple[str, float]:
        from server.typesafe_client import NoulQuestion

        state = {
            "tool": call.get("name", "<unknown>"),
            "input": call.get("input", {}),
            "output": "" if result is None else result.get("output", ""),
        }
        questions = {"should_keep": NoulQuestion(instructions=TYPESAFE_KEEP_INSTRUCTIONS)}
        eval_result = self.client.evaluate(state, questions)
        if not eval_result.ok:
            raise RuntimeError(eval_result.error or "TypeSafe evaluation failed")
        answer = eval_result.answers.get("should_keep")
        if answer is None or not isinstance(answer.value, (int, float)):
            raise ValueError("TypeSafe returned no usable noul answer")
        noul = float(answer.value)
        if noul >= TYPESAFE_HIGH:
            return "keep", noul
        if noul <= TYPESAFE_LOW:
            return "drop", 1.0 - noul
        # TypeSafe's own documented uncertainty zone (0.30-0.70, see the
        # consistency/noul cookbook): always report confidence 0.0 here,
        # unconditionally below resolve_ambiguous's CONFIDENCE_FLOOR, so the
        # existing low-confidence-defaults-to-keep safety net decides this
        # case instead of us guessing a direction from a noul value TypeSafe
        # itself flags as too uncertain to act on.
        return "keep", 0.0
