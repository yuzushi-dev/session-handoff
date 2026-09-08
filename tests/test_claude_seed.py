import copy
import json

import pytest

from benchmark.claude_seed import native_messages
from benchmark.information_preservation import build_structured_conversation


def test_actual_pilot_history_preserves_order_text_and_tool_pairs():
    source = build_structured_conversation("compound-rot", "long", 1)
    before = copy.deepcopy(source)
    converted = native_messages(source)
    assert source == before
    assert all(a["role"] != b["role"] for a, b in zip(converted, converted[1:]))
    assert converted[0]["role"] == "user"
    texts, calls, results = [], [], []
    for message in converted:
        for block in message["content"]:
            if block["type"] == "text":
                texts.append(block["text"])
            elif block["type"] == "tool_use":
                assert message["role"] == "assistant"
                calls.append((block["id"], block["name"], block["input"]))
            else:
                assert message["role"] == "user"
                assert block["type"] == "tool_result"
                texts.append(block["content"])
                results.append(block["tool_use_id"])
    assert texts == [message["content"] for message in source["messages"]]
    assert calls == [
        (
            m["tool_call"]["call_id"],
            m["tool_call"]["name"],
            json.loads(m["tool_call"]["arguments"]),
        )
        for m in source["messages"]
        if "tool_call" in m
    ]
    assert results == [m["call_id"] for m in source["messages"] if m["role"] == "tool"]


def test_fresh_handoff_has_only_its_user_content():
    source = {
        "schema_version": 1,
        "case": "handoff",
        "band": "short",
        "replicate": 1,
        "checkpoint": 1,
        "role_order": ["user"],
        "messages": [{"id": "handoff", "role": "user", "content": "HANDOFF_ONLY"}],
    }
    assert native_messages(source) == [
        {"role": "user", "content": [{"type": "text", "text": "HANDOFF_ONLY"}]}
    ]


def test_invalid_tool_pair_is_rejected():
    source = build_structured_conversation("compound-rot", "long", 1)
    source["messages"][2]["call_id"] = "unmatched"
    with pytest.raises(ValueError):
        native_messages(source)


def test_nonobject_tool_arguments_are_rejected():
    source = build_structured_conversation("compound-rot", "long", 1)
    source["messages"][1]["tool_call"]["arguments"] = "[]"
    with pytest.raises(ValueError, match="must be an object"):
        native_messages(source)
