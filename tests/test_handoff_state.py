import copy
import json
import math

import pytest

from server.handoff_state import (
    MAX_ARRAY_ITEMS,
    MAX_CONTENT_BYTES,
    MAX_STRING_BYTES,
    HandoffStateError,
    redact_state,
    render_state,
    validate_state,
)


def valid_state() -> dict[str, object]:
    return {
        "schema_version": 1,
        "goal": "Ship the focused change",
        "constraints_preferences": ["Keep the public API synchronous"],
        "progress": {
            "done": ["src/example.py already uses the final value"],
            "in_progress": [],
            "pending": ["Update tests/example_test.py"],
        },
        "key_decisions": ["The final value is 15; 60 is obsolete"],
        "rejected_attempts": ["Do not repeat the regex-only fix; its regression test failed"],
        "verification": ["tests/example_test.py::test_final_value currently fails expecting 60"],
        "critical_context": ["Implementation target: src/example.py::apply_value"],
        "uncertainties": [],
        "next_steps": ["Change the focused assertion", "Run the focused test"],
    }


def test_validate_state_accepts_and_preserves_the_v1_contract():
    state = valid_state()

    assert validate_state(state) == state


def test_render_state_matches_the_canonical_markdown_shape():
    assert render_state(valid_state()) == """## Goal

Ship the focused change

## Constraints & Preferences

- Keep the public API synchronous

## Progress

### Done

- src/example.py already uses the final value

### In Progress

- None identified.

### Pending

- Update tests/example_test.py

## Key Decisions

- The final value is 15; 60 is obsolete

## Critical Context

- Implementation target: src/example.py::apply_value
- Rejected attempt: Do not repeat the regex-only fix; its regression test failed
- Verification: tests/example_test.py::test_final_value currently fails expecting 60
- None identified.

## Next Steps

1. Change the focused assertion
2. Run the focused test
"""


@pytest.mark.parametrize(
    "key",
    [
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
    ],
)
def test_validate_state_requires_every_top_level_key(key):
    state = valid_state()
    del state[key]

    with pytest.raises(HandoffStateError, match="missing required key"):
        validate_state(state)


@pytest.mark.parametrize("key", ["done", "in_progress", "pending"])
def test_validate_state_requires_every_progress_key(key):
    state = valid_state()
    del state["progress"][key]

    with pytest.raises(HandoffStateError, match="missing required key"):
        validate_state(state)


def test_validate_state_rejects_unknown_keys_at_each_object_level():
    state = valid_state()
    state["unexpected"] = []
    with pytest.raises(HandoffStateError, match="unknown key"):
        validate_state(state)

    state = valid_state()
    state["progress"]["unexpected"] = []
    with pytest.raises(HandoffStateError, match="unknown key"):
        validate_state(state)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("schema_version", 2),
        ("goal", 42),
        ("goal", {"text": "value"}),
        ("constraints_preferences", "not an array"),
        ("key_decisions", [42]),
        ("progress", []),
        ("progress.done", ["ok", ["nested"]]),
        ("progress.done", [None]),
        ("verification", [math.inf]),
        ("uncertainties", [math.nan]),
    ],
)
def test_validate_state_rejects_wrong_scalar_and_nested_types(path, value):
    state = valid_state()
    if "." in path:
        parent, child = path.split(".")
        state[parent][child] = value
    else:
        state[path] = value

    with pytest.raises(HandoffStateError):
        validate_state(state)


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_validate_state_rejects_blank_strings(value):
    state = valid_state()
    state["goal"] = value

    with pytest.raises(HandoffStateError, match="non-blank"):
        validate_state(state)


def test_validate_state_rejects_nul_bytes():
    state = valid_state()
    state["critical_context"] = ["path\x00suffix"]

    with pytest.raises(HandoffStateError, match="NUL"):
        validate_state(state)


def test_validate_state_enforces_string_and_array_boundaries():
    state = valid_state()
    state["goal"] = "x" * MAX_STRING_BYTES
    assert validate_state(state)["goal"] == "x" * MAX_STRING_BYTES

    state["goal"] = "x" * (MAX_STRING_BYTES + 1)
    with pytest.raises(HandoffStateError, match="8 KiB"):
        validate_state(state)

    state = valid_state()
    state["verification"] = ["item"] * MAX_ARRAY_ITEMS
    assert len(validate_state(state)["verification"]) == MAX_ARRAY_ITEMS

    state["verification"] = ["item"] * (MAX_ARRAY_ITEMS + 1)
    with pytest.raises(HandoffStateError, match="256"):
        validate_state(state)


def test_validate_state_rejects_oversized_serialized_state():
    state = valid_state()
    state["critical_context"] = ["x" * MAX_STRING_BYTES] * MAX_ARRAY_ITEMS
    state["verification"] = ["y" * MAX_STRING_BYTES] * MAX_ARRAY_ITEMS
    state["uncertainties"] = ["z" * MAX_STRING_BYTES] * MAX_ARRAY_ITEMS

    serialized = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    assert len(serialized.encode("utf-8")) > MAX_CONTENT_BYTES
    with pytest.raises(HandoffStateError, match="serialized state"):
        validate_state(state)


def test_redact_state_redacts_every_secret_string_without_reordering_facts():
    state = valid_state()
    state["goal"] = "Ship API_TOKEN=goal-secret"
    state["constraints_preferences"] = ["Bearer abcdefghijkl"]
    state["progress"]["done"] = ["Used sk-1234567890"]

    redacted, count = redact_state(state)

    assert count == 3
    assert redacted["goal"] == "Ship API_TOKEN=[REDACTED]"
    assert redacted["constraints_preferences"] == ["Bearer [REDACTED]"]
    assert redacted["progress"]["done"] == ["Used [REDACTED]"]
    assert redacted["next_steps"] == state["next_steps"]
    assert state["goal"] == "Ship API_TOKEN=goal-secret"


def test_render_state_preserves_array_order_and_marks_empty_lists():
    state = valid_state()
    state["constraints_preferences"] = []
    state["key_decisions"] = []
    state["critical_context"] = []
    state["rejected_attempts"] = []
    state["verification"] = []
    state["uncertainties"] = []
    state["next_steps"] = []

    rendered = render_state(state)

    assert "## Constraints & Preferences\n\n- None identified." in rendered
    assert "## Key Decisions\n\n- None identified." in rendered
    assert "## Critical Context\n\n- None identified." in rendered
    assert "## Next Steps\n\n- None identified." in rendered

    state["constraints_preferences"] = ["first", "second"]
    rendered = render_state(state)
    assert rendered.index("- first") < rendered.index("- second")


def test_validation_does_not_mutate_input_state():
    state = valid_state()
    before = copy.deepcopy(state)

    validate_state(state)

    assert state == before
