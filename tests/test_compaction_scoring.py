from server.compaction_scoring import pair_tool_events


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
