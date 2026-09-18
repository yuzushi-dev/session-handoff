from server.compaction_scoring import heuristic_score, pair_tool_events


def test_pair_tool_events_matches_call_to_result_by_id():
    events = [
        {"kind": "text", "role": "user", "text": "hi"},
        {"kind": "tool_call", "id": "t1", "name": "Read", "input": {"file": "a.py"}},
        {"kind": "tool_result", "id": "t1", "output": "contents", "is_error": False},
        {"kind": "tool_call", "id": "t2", "name": "Bash", "input": {"command": "ls"}},
    ]
    pairs = pair_tool_events(events)
    assert len(pairs) == 2
    assert pairs[0][0]["id"] == "t1"
    assert pairs[0][1]["output"] == "contents"
    assert pairs[1][0]["id"] == "t2"
    assert pairs[1][1] is None  # no result yet — still in flight


def _pair(call_id, name="Bash", output="ok", is_error=False):
    call = {"kind": "tool_call", "id": call_id, "name": name, "input": {}}
    result = {"kind": "tool_result", "id": call_id, "output": output, "is_error": is_error}
    return call, result


def test_pinned_recent_pairs_are_always_kept():
    pairs = [_pair(str(i)) for i in range(8)]
    scored = heuristic_score(pairs, preserve_recent=6)
    recent = scored[-6:]
    assert all(item.decision == "keep" and item.reason == "pinned_recent" for item in recent)


def test_error_outside_pinned_window_is_kept_not_dropped():
    old_error = _pair("old", output="Traceback: boom", is_error=True)
    pairs = [old_error] + [_pair(str(i)) for i in range(6)]
    scored = heuristic_score(pairs, preserve_recent=6)
    assert scored[0].decision == "keep"
    assert scored[0].reason == "error"


def test_duplicate_output_outside_pinned_window_is_dropped():
    dup_a = _pair("a", name="Bash", output="same output")
    dup_b = _pair("b", name="Bash", output="same output")
    pairs = [dup_a, dup_b] + [_pair(str(i)) for i in range(6)]
    scored = heuristic_score(pairs, preserve_recent=6)
    assert scored[1].decision == "drop"
    assert scored[1].reason == "duplicate_output"
    assert scored[0].decision == "keep"  # first occurrence stays


def test_oversized_output_outside_pinned_window_is_truncated():
    big = _pair("big", output="x" * 5000)
    pairs = [big] + [_pair(str(i)) for i in range(6)]
    scored = heuristic_score(pairs, preserve_recent=6, truncate_chars=4000)
    assert scored[0].decision == "truncate"
    assert scored[0].reason == "oversized_output"


def test_unremarkable_pair_outside_pinned_window_is_ambiguous():
    plain = _pair("plain", output="fine, nothing special")
    pairs = [plain] + [_pair(str(i)) for i in range(6)]
    scored = heuristic_score(pairs, preserve_recent=6)
    assert scored[0].decision == "ambiguous"
