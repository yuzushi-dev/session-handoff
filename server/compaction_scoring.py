"""Score tool calls in a transcript for selective retention in a recovery checkpoint."""

from __future__ import annotations

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
