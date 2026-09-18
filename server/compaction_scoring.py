"""Score tool calls in a transcript for selective retention in a recovery checkpoint."""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from typing import Any


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
        resolved.append(ScoredItem(item.call, item.result, decision, "local_model"))
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
