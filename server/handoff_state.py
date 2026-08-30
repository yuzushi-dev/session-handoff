"""Validation and Markdown rendering for structured session handoffs."""

from __future__ import annotations

import json
from typing import Any

MAX_STRING_BYTES = 8 * 1024
MAX_ARRAY_ITEMS = 256
MAX_CONTENT_BYTES = 2_000_000

_TOP_LEVEL_KEYS = (
    "schema_version",
    "goal",
    "constraints_preferences",
    "progress",
    "key_decisions",
    "rejected_attempts",
    "verification",
    "critical_context",
    "uncertainties",
    "next_steps",
)
_PROGRESS_KEYS = ("done", "in_progress", "pending")
_ARRAY_KEYS = (
    "constraints_preferences",
    "key_decisions",
    "rejected_attempts",
    "verification",
    "critical_context",
    "uncertainties",
    "next_steps",
)


class HandoffStateError(ValueError):
    """Raised when structured handoff state violates the v1 contract."""


def _validate_keys(value: dict[Any, Any], expected: tuple[str, ...], path: str) -> None:
    unknown = [key for key in value if not isinstance(key, str) or key not in expected]
    if unknown:
        raise HandoffStateError(f"{path} contains unknown key: {unknown[0]!r}")
    missing = [key for key in expected if key not in value]
    if missing:
        raise HandoffStateError(f"{path} missing required key: {missing[0]}")


def _validate_string(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise HandoffStateError(f"{path} must be a string")
    if not value.strip():
        raise HandoffStateError(f"{path} must be non-blank")
    if "\x00" in value:
        raise HandoffStateError(f"{path} must not contain NUL bytes")
    if len(value.encode("utf-8")) > MAX_STRING_BYTES:
        raise HandoffStateError(f"{path} exceeds 8 KiB")
    return value


def _validate_string_array(value: object, path: str) -> list[str]:
    if not isinstance(value, list):
        raise HandoffStateError(f"{path} must be an array")
    if len(value) > MAX_ARRAY_ITEMS:
        raise HandoffStateError(f"{path} exceeds 256 entries")
    return [_validate_string(item, f"{path}[{index}]") for index, item in enumerate(value)]


def _serialize_size(value: dict[str, object]) -> int:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return len(serialized.encode("utf-8"))


def validate_state(value: object) -> dict[str, object]:
    """Validate and return a canonical copy of a v1 structured handoff."""
    if not isinstance(value, dict):
        raise HandoffStateError("state must be an object")
    _validate_keys(value, _TOP_LEVEL_KEYS, "state")

    schema_version = value["schema_version"]
    if type(schema_version) is not int:
        raise HandoffStateError("schema_version must be an integer")
    if schema_version != 1:
        raise HandoffStateError("schema_version must be 1")

    goal = _validate_string(value["goal"], "goal")
    arrays: dict[str, list[str]] = {
        key: _validate_string_array(value[key], key) for key in _ARRAY_KEYS
    }

    progress = value["progress"]
    if not isinstance(progress, dict):
        raise HandoffStateError("progress must be an object")
    _validate_keys(progress, _PROGRESS_KEYS, "progress")
    normalized_progress = {
        key: _validate_string_array(progress[key], f"progress.{key}")
        for key in _PROGRESS_KEYS
    }

    normalized: dict[str, object] = {
        "schema_version": schema_version,
        "goal": goal,
        "constraints_preferences": arrays["constraints_preferences"],
        "progress": normalized_progress,
        "key_decisions": arrays["key_decisions"],
        "rejected_attempts": arrays["rejected_attempts"],
        "verification": arrays["verification"],
        "critical_context": arrays["critical_context"],
        "uncertainties": arrays["uncertainties"],
        "next_steps": arrays["next_steps"],
    }
    if _serialize_size(normalized) > MAX_CONTENT_BYTES:
        raise HandoffStateError("serialized state exceeds MAX_CONTENT_BYTES")
    return normalized


def redact_state(state: dict[str, object]) -> tuple[dict[str, object], int]:
    """Redact credential-like values in every state string."""
    normalized = validate_state(state)
    try:
        from .handoff_mcp import redact_secrets
    except ImportError:  # direct module execution compatibility
        from handoff_mcp import redact_secrets

    redacted_count = 0

    def redact(value: object) -> object:
        nonlocal redacted_count
        if isinstance(value, str):
            result, count = redact_secrets(value)
            redacted_count += count
            return result
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        return value

    redacted = redact(normalized)
    assert isinstance(redacted, dict)
    return redacted, redacted_count


def _bullets(items: list[str], prefix: str = "") -> list[str]:
    if not items:
        return ["- None identified."]
    return [f"- {prefix}{item}" for item in items]


def _numbered(items: list[str]) -> list[str]:
    if not items:
        return ["- None identified."]
    return [f"{index}. {item}" for index, item in enumerate(items, start=1)]


def render_state(state: dict[str, object]) -> str:
    """Render validated structured state in the canonical handoff format."""
    normalized = validate_state(state)
    progress = normalized["progress"]
    assert isinstance(progress, dict)

    critical_context: list[str] = []
    critical_context.extend(_bullets(normalized["critical_context"]))
    critical_context.extend(_bullets(normalized["rejected_attempts"], "Rejected attempt: "))
    critical_context.extend(_bullets(normalized["verification"], "Verification: "))
    critical_context.extend(_bullets(normalized["uncertainties"], "Uncertainty: "))

    lines = [
        "## Goal",
        "",
        normalized["goal"],
        "",
        "## Constraints & Preferences",
        "",
        *_bullets(normalized["constraints_preferences"]),
        "",
        "## Progress",
        "",
        "### Done",
        "",
        *_bullets(progress["done"]),
        "",
        "### In Progress",
        "",
        *_bullets(progress["in_progress"]),
        "",
        "### Pending",
        "",
        *_bullets(progress["pending"]),
        "",
        "## Key Decisions",
        "",
        *_bullets(normalized["key_decisions"]),
        "",
        "## Critical Context",
        "",
        *critical_context,
        "",
        "## Next Steps",
        "",
        *_numbered(normalized["next_steps"]),
        "",
    ]
    return "\n".join(str(line) for line in lines)
