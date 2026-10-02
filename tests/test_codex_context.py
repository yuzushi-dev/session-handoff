import dataclasses

import pytest

from server.codex_context import MAX_ID_LENGTH, CodexContext, parse_request_context


def test_parses_distinct_request_metadata_ids():
    params = {"_meta": {"threadId": "thread-123", "sessionId": "session-456"}}

    assert parse_request_context(params) == CodexContext(
        thread_id="thread-123", session_id="session-456"
    )


@pytest.mark.parametrize("field", ["threadId", "sessionId"])
def test_does_not_use_one_id_as_a_fallback_for_the_other(field):
    context = parse_request_context({"_meta": {field: "identifier"}})

    assert context.thread_id == ("identifier" if field == "threadId" else None)
    assert context.session_id == ("identifier" if field == "sessionId" else None)


@pytest.mark.parametrize(
    "params",
    [None, [], "params", {}, {"_meta": None}, {"_meta": []}, {"_meta": "legacy"}],
)
def test_missing_or_malformed_metadata_has_no_context(params):
    assert parse_request_context(params) == CodexContext()


def test_legacy_and_unrelated_fields_do_not_supply_ids():
    params = {
        "threadId": "top-level-thread",
        "sessionId": "top-level-session",
        "arguments": {"threadId": "argument-thread", "sessionId": "argument-session"},
        "_meta": {
            "thread_id": "legacy-thread",
            "session_id": "legacy-session",
            "com.openai.codex": {"threadId": "nested-thread"},
            "workspace": "/untrusted/workspace",
        },
    }

    assert parse_request_context(params) == CodexContext()


@pytest.mark.parametrize("field", ["threadId", "sessionId"])
@pytest.mark.parametrize(
    "invalid",
    [None, True, 123, [], {}, "", "  ", "\t", "bad\nidentifier", "bad\x00id", "bad\x7fid", "bad\x85id", "x" * (MAX_ID_LENGTH + 1)],
)
def test_invalid_id_is_ignored_without_discarding_the_other_id(field, invalid):
    other = "sessionId" if field == "threadId" else "threadId"
    context = parse_request_context({"_meta": {field: invalid, other: "valid-id"}})

    assert getattr(context, "thread_id" if field == "threadId" else "session_id") is None
    assert getattr(context, "session_id" if field == "threadId" else "thread_id") == "valid-id"


def test_accepts_bounded_printable_strings_without_changing_them():
    params = {"_meta": {"threadId": "x" * MAX_ID_LENGTH, "sessionId": "session-α"}}

    context = parse_request_context(params)

    assert context.thread_id == params["_meta"]["threadId"]
    assert context.session_id == "session-α"


def test_context_is_immutable_and_detached_from_input_metadata():
    params = {"_meta": {"threadId": "original-thread"}}
    context = parse_request_context(params)
    params["_meta"]["threadId"] = "replacement-thread"

    assert context.thread_id == "original-thread"
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.thread_id = "replacement-thread"


def test_each_request_has_its_own_context_without_global_binding():
    first = parse_request_context({"_meta": {"threadId": "first", "sessionId": "first-session"}})
    second = parse_request_context({"_meta": {"threadId": "second"}})
    missing = parse_request_context({})

    assert first == CodexContext(thread_id="first", session_id="first-session")
    assert second == CodexContext(thread_id="second")
    assert missing == CodexContext()
