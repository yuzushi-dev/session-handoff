import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import server.handoff_mcp as handoff_mcp


SERVER = Path(__file__).parents[1] / "server" / "handoff_mcp.py"


@pytest.fixture(autouse=True)
def isolate_telemetry_home(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path / "telemetry-home"))


def exchange(requests, env_overrides=None):
    payload = "\n".join(json.dumps(request) for request in requests) + "\n"
    result = subprocess.run(
        [sys.executable, str(SERVER)],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONUNBUFFERED": "1", **(env_overrides or {})},
    )
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def initialized(request_id=1):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    }


def call(request_id, name, arguments):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def tool_result(response):
    result = response["result"]
    if result.get("isError"):
        return result["structuredContent"]
    if "content" not in result:
        return result
    return json.loads(result["content"][0]["text"])


def structured_state():
    return {
        "schema_version": 1,
        "goal": "Ship the focused change",
        "constraints_preferences": ["Keep the public API synchronous"],
        "progress": {
            "done": ["Implementation is in place"],
            "in_progress": [],
            "pending": ["Run the focused test"],
        },
        "key_decisions": ["Use the final value"],
        "rejected_attempts": ["The regex-only fix failed"],
        "verification": ["The focused test is still red"],
        "critical_context": ["Target: src/example.py"],
        "uncertainties": [],
        "next_steps": ["Update the assertion"],
    }


def test_server_initializes_and_lists_handoff_tools():
    responses = exchange(
        [
            initialized(),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
    )

    assert tool_result(responses[0])["serverInfo"]["name"] == "session-handoff"
    names = {tool["name"] for tool in tool_result(responses[1])["tools"]}
    assert names == {
        "handoff_create",
        "handoff_migrate",
        "handoff_read",
        "handoff_validate",
        "handoff_list",
    }


@pytest.mark.parametrize("params", [None, [], "invalid"])
def test_initialize_non_object_params_use_default_protocol_version(params):
    response = handoff_mcp.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
    )

    assert response["result"]["protocolVersion"] == handoff_mcp.DEFAULT_PROTOCOL_VERSION


def test_handoff_create_schema_accepts_exact_state_v1_contract():
    tool = next(tool for tool in handoff_mcp.TOOLS if tool["name"] == "handoff_create")
    schema = tool["inputSchema"]

    assert schema["required"] == ["workspace", "path"]
    assert schema["additionalProperties"] is False
    assert "content" not in schema["required"]
    state = schema["properties"]["state"]
    assert state["type"] == "object"
    assert state["additionalProperties"] is False
    assert state["required"] == [
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
    ]
    assert state["properties"]["progress"]["additionalProperties"] is False
    assert state["properties"]["progress"]["required"] == [
        "done",
        "in_progress",
        "pending",
    ]
    assert "maxLength" not in state["properties"]["goal"]
    assert "maxLength" not in state["properties"]["constraints_preferences"]["items"]


def test_call_tool_rejects_unknown_top_level_parameter():
    result = handoff_mcp._call_tool(
        {
            "name": "handoff_read",
            "arguments": {},
            "unexpected": True,
        }
    )

    assert result["isError"] is True
    assert result["structuredContent"]["message"] == "unknown tool call parameter: unexpected"


def test_call_tool_rejects_unknown_tool_argument():
    result = handoff_mcp._call_tool(
        {
            "name": "handoff_read",
            "arguments": {"workspace": ".", "path": "handoff.md", "unexpected": True},
        }
    )

    assert result["isError"] is True
    assert result["structuredContent"]["message"] == "unknown tool argument: unexpected"


@pytest.mark.parametrize("tool_name", ["handoff_read", "handoff_validate"])
def test_read_tools_reject_oversized_handoff(tmp_path, tool_name):
    path = tmp_path / "oversized.md"
    path.write_bytes(b"x" * (handoff_mcp.MAX_CONTENT_BYTES + 1))

    with pytest.raises(
        handoff_mcp.HandoffError,
        match=f"content exceeds {handoff_mcp.MAX_CONTENT_BYTES} bytes",
    ):
        getattr(handoff_mcp, "_read" if tool_name == "handoff_read" else "_validate")(
            {"workspace": str(tmp_path), "path": path.name}
        )


def test_read_fails_closed_without_secure_relative_io(monkeypatch, tmp_path):
    path = tmp_path / "handoff.md"
    path.write_text("## Goal\nOnly\n", encoding="utf-8")
    monkeypatch.setattr(handoff_mcp, "_secure_relative_io_supported", lambda: False)

    with pytest.raises(handoff_mcp.HandoffError, match="secure filesystem"):
        handoff_mcp._read({"workspace": str(tmp_path), "path": path.name})


@pytest.mark.skipif(
    not (
        hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
    ),
    reason="secure relative directory primitives are unavailable",
)
def test_create_rejects_parent_directory_swap_before_write(monkeypatch, tmp_path):
    content = "".join(f"{section}\nx\n\n" for section in handoff_mcp.REQUIRED_SECTIONS)
    handoffs = tmp_path / "handoffs"
    handoffs.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    original = handoff_mcp._atomic_write

    def swap_parent_then_write(*args, **kwargs):
        handoffs.rename(tmp_path / "handoffs-real")
        handoffs.symlink_to(outside, target_is_directory=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(handoff_mcp, "_atomic_write", swap_parent_then_write)

    with pytest.raises(OSError):
        handoff_mcp._create(
            {"workspace": str(tmp_path), "path": "handoffs/race.md", "content": content}
        )

    assert not (outside / "race.md").exists()


def test_create_accepts_state_and_writes_canonical_headings(tmp_path):
    path = tmp_path / "handoffs" / "state.md"

    result = handoff_mcp._create(
        {"workspace": str(tmp_path), "path": "handoffs/state.md", "state": structured_state()}
    )

    content = path.read_text(encoding="utf-8")
    assert result["valid"] is True
    assert all(section in content for section in handoff_mcp.REQUIRED_SECTIONS)
    assert "- Rejected attempt: The regex-only fix failed" in content
    assert "- Verification: The focused test is still red" in content


def test_create_rejects_both_content_and_state(tmp_path):
    with pytest.raises(handoff_mcp.HandoffError, match="exactly one"):
        handoff_mcp._create(
            {
                "workspace": str(tmp_path),
                "path": "handoffs/both.md",
                "content": "content",
                "state": structured_state(),
            }
        )


def test_create_rejects_neither_content_nor_state(tmp_path):
    with pytest.raises(handoff_mcp.HandoffError, match="exactly one"):
        handoff_mcp._create(
            {"workspace": str(tmp_path), "path": "handoffs/neither.md"}
        )


def test_invalid_state_writes_no_file_or_switch_request(monkeypatch, tmp_path):
    state = structured_state()
    state["progress"]["done"] = ["invalid\x00state"]
    control_path = tmp_path / "control" / "switch.json"
    monkeypatch.setenv("SESSION_HANDOFF_CONTROL", str(control_path))
    monkeypatch.setenv("SESSION_HANDOFF_CONTROL_TOKEN", "test-token")

    with pytest.raises(handoff_mcp.HandoffError, match="state"):
        handoff_mcp._create(
            {
                "workspace": str(tmp_path),
                "path": "handoffs/invalid-state.md",
                "state": state,
                "auto_switch": True,
            }
        )

    assert not (tmp_path / "handoffs" / "invalid-state.md").exists()
    assert not control_path.exists()


def test_state_redaction_count_covers_all_state_strings(monkeypatch, tmp_path):
    state = structured_state()
    state["goal"] = "Ship API_TOKEN=goal-secret"
    state["progress"]["done"] = ["Used Bearer abcdefghijkl"]
    state["next_steps"] = ["Remove sk-1234567890"]
    monkeypatch.setattr(handoff_mcp, "record_terminal_outcome", lambda summary: None)

    result = handoff_mcp._create(
        {"workspace": str(tmp_path), "path": "handoffs/redacted.md", "state": state}
    )

    content = (tmp_path / "handoffs" / "redacted.md").read_text(encoding="utf-8")
    assert result["redacted_count"] == 3
    assert "goal-secret" not in content
    assert "abcdefghijkl" not in content
    assert "sk-1234567890" not in content


def test_state_create_preserves_auto_switch_numeric_telemetry(tmp_path):
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    control_path = control_dir / "switch.json"
    token = "test-control-token"
    state = structured_state()
    state["goal"] = "Ship API_TOKEN=state-secret"

    responses = exchange(
        [
            initialized(),
            call(
                2,
                "handoff_create",
                {
                    "workspace": str(tmp_path),
                    "path": "handoffs/state-switch.md",
                    "state": state,
                    "auto_switch": True,
                },
            ),
        ],
        {
            "SESSION_HANDOFF_CONTROL": str(control_path),
            "SESSION_HANDOFF_CONTROL_TOKEN": token,
        },
    )

    result = tool_result(responses[1])
    content = (tmp_path / "handoffs" / "state-switch.md").read_text(encoding="utf-8")
    request = json.loads(control_path.read_text(encoding="utf-8"))
    assert result["auto_switch_requested"] is True
    assert request == {
        "token": token,
        "workspace": str(tmp_path),
        "path": "handoffs/state-switch.md",
        "telemetry": {"handoff_bytes": len(content.encode("utf-8")), "redacted_count": 1},
    }


def test_create_redacts_secrets_and_refuses_overwrite(tmp_path):
    secret = "fixture" + "-secret-value"
    content = f"""## Goal
Ship the feature.

## Constraints & Preferences
Keep the API stable.

## Progress
### Done
- Added implementation.
### In Progress
- None.
### Pending
- Run the release check.

## Key Decisions
- Use a local file.

## Critical Context
`API_TOKEN={secret}`

## Next Steps
1. Run the release check.
"""
    path = "handoffs/test.md"
    responses = exchange(
        [
            initialized(),
            call(2, "handoff_create", {"workspace": str(tmp_path), "path": path, "content": content}),
            call(3, "handoff_create", {"workspace": str(tmp_path), "path": path, "content": content}),
            call(4, "handoff_read", {"workspace": str(tmp_path), "path": path}),
        ]
    )

    created = tool_result(responses[1])
    assert created["redacted_count"] == 1
    assert (tmp_path / path).read_text().find("super-secret-value") == -1
    assert tool_result(responses[2])["isError"] is True
    read = tool_result(responses[3])
    assert "API_TOKEN=[REDACTED]" in read["content"]


def test_create_requests_automatic_switch_when_supervised(tmp_path):
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    control_path = control_dir / "switch.json"
    token = "test-control-token"
    content = """## Goal
Continue the feature.

## Constraints & Preferences
- Keep the API stable.

## Progress
### Done
- Wrote the handoff.
### In Progress
- None.
### Pending
- Continue implementation.

## Key Decisions
- Use the handoff file.

## Critical Context
- The supervisor owns the next session.

## Next Steps
1. Resume from this file.
"""

    responses = exchange(
        [
            initialized(),
            call(
                2,
                "handoff_create",
                {
                    "workspace": str(tmp_path),
                    "path": "handoffs/feature.md",
                    "content": content,
                    "auto_switch": True,
                },
            ),
        ],
        {
            "SESSION_HANDOFF_CONTROL": str(control_path),
            "SESSION_HANDOFF_CONTROL_TOKEN": token,
        },
    )

    result = tool_result(responses[1])
    assert result["auto_switch_requested"] is True
    request = json.loads(control_path.read_text(encoding="utf-8"))
    assert request == {
        "token": token,
        "workspace": str(tmp_path),
        "path": "handoffs/feature.md",
        "telemetry": {"handoff_bytes": len(content.encode("utf-8")), "redacted_count": 0},
    }


def test_create_validation_failure_records_only_safe_summary(monkeypatch, tmp_path):
    summaries = []
    monkeypatch.setattr(handoff_mcp, "record_terminal_outcome", summaries.append)

    with pytest.raises(handoff_mcp.HandoffError, match="missing canonical sections"):
        handoff_mcp._create(
            {
                "workspace": str(tmp_path),
                "path": "handoffs/invalid.md",
                "content": "## Goal\ncontains /sensitive/path and session-id\n",
            }
        )

    assert len(summaries) == 1
    assert summaries[0] == {
        "operation": "handoff",
        "source_client": "codex",
        "target_client": "codex",
        "result": "failure",
        "failure_stage": "missing_sections",
        "handoff_bytes": len("## Goal\ncontains /sensitive/path and session-id\n".encode()),
        "redacted_count": 0,
        "dropped_events": 0,
        "normalized_fields": 0,
    }


def test_create_validation_failures_report_distinct_stages(monkeypatch, tmp_path):
    """Each validation cause reports its own stage, so the dashboard can tell them apart."""
    complete = "".join(f"{section}\nx\n\n" for section in handoff_mcp.REQUIRED_SECTIONS)

    def stage_for(arguments):
        summaries = []
        monkeypatch.setattr(handoff_mcp, "record_terminal_outcome", summaries.append)
        with pytest.raises(handoff_mcp.HandoffError):
            handoff_mcp._create(arguments)
        assert len(summaries) == 1
        return summaries[0]["failure_stage"]

    assert stage_for(
        {
            "workspace": str(tmp_path),
            "path": "handoffs/missing.md",
            "content": "## Goal\nonly a goal\n",
        }
    ) == "missing_sections"

    assert stage_for(
        {
            "workspace": str(tmp_path),
            "path": "handoffs/big.md",
            "content": "x" * (handoff_mcp.MAX_CONTENT_BYTES + 1),
        }
    ) == "size_limit"

    existing = tmp_path / "handoffs"
    existing.mkdir()
    (existing / "taken.md").write_text(complete, encoding="utf-8")
    assert stage_for(
        {
            "workspace": str(tmp_path),
            "path": "handoffs/taken.md",
            "content": complete,
        }
    ) == "path_exists"

    assert stage_for(
        {
            "workspace": str(tmp_path),
            "path": "handoffs/state.md",
            "state": {"goal": ""},
        }
    ) == "state_schema"


def test_migrate_requests_supervised_native_switch(tmp_path):
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    control_path = control_dir / "switch.json"
    token = "test-control-token"

    responses = exchange(
        [
            initialized(),
            call(
                2,
                "handoff_migrate",
                {
                    "workspace": str(tmp_path),
                    "source_client": "claude",
                    "target_client": "codex",
                    "source_session_id": "source-session-id",
                },
            ),
        ],
        {
            "SESSION_HANDOFF_CONTROL": str(control_path),
            "SESSION_HANDOFF_CONTROL_TOKEN": token,
        },
    )

    result = tool_result(responses[1])
    assert result["auto_switch_requested"] is True
    assert json.loads(control_path.read_text(encoding="utf-8")) == {
        "token": token,
        "mode": "migrate",
        "workspace": str(tmp_path),
        "source_client": "claude",
        "target_client": "codex",
        "source_session_id": "source-session-id",
    }


def test_migrate_requires_managed_launcher(tmp_path):
    responses = exchange(
        [
            initialized(),
            call(
                2,
                "handoff_migrate",
                {
                    "workspace": str(tmp_path),
                    "source_client": "codex",
                    "target_client": "claude",
                    "source_session_id": "thread-id",
                },
            ),
        ],
        {
            "SESSION_HANDOFF_CONTROL": "",
            "SESSION_HANDOFF_CONTROL_TOKEN": "",
        },
    )

    result = tool_result(responses[1])
    assert result["auto_switch_requested"] is False
    assert "unavailable" in result["auto_switch_error"]


def test_create_reports_manual_fallback_without_supervisor(tmp_path):
    content = """## Goal
Continue the feature.

## Constraints & Preferences
- Keep the API stable.

## Progress
### Done
- Wrote the handoff.
### In Progress
- None.
### Pending
- Continue implementation.

## Key Decisions
- Use the handoff file.

## Critical Context
- No launcher is active.

## Next Steps
1. Resume from this file.
"""

    responses = exchange(
        [
            initialized(),
            call(
                2,
                "handoff_create",
                {
                    "workspace": str(tmp_path),
                    "path": "handoffs/manual.md",
                    "content": content,
                    "auto_switch": True,
                },
            ),
        ],
        {
            "SESSION_HANDOFF_CONTROL": "",
            "SESSION_HANDOFF_CONTROL_TOKEN": "",
        },
    )

    result = tool_result(responses[1])
    assert result["valid"] is True
    assert result["auto_switch_requested"] is False
    assert "unavailable" in result["auto_switch_error"]


def test_create_rejects_path_escape_and_missing_sections(tmp_path):
    responses = exchange(
        [
            initialized(),
            call(
                2,
                "handoff_create",
                {"workspace": str(tmp_path), "path": "../outside.md", "content": "## Goal\nOnly"},
            ),
            call(
                3,
                "handoff_create",
                {"workspace": str(tmp_path), "path": "bad.md", "content": "## Goal\nOnly"},
            ),
        ]
    )

    assert tool_result(responses[1])["isError"] is True
    assert "workspace" in tool_result(responses[1])["message"]
    assert tool_result(responses[2])["isError"] is True
    assert "missing" in tool_result(responses[2])["message"].lower()


def test_validate_and_list_are_read_only(tmp_path):
    handoffs = tmp_path / "handoffs"
    handoffs.mkdir()
    (handoffs / "one.md").write_text("## Goal\nOne\n")
    (handoffs / "two.md").write_text("## Goal\nTwo\n")

    responses = exchange(
        [
            initialized(),
            call(2, "handoff_validate", {"workspace": str(tmp_path), "path": "handoffs/one.md"}),
            call(3, "handoff_list", {"workspace": str(tmp_path), "limit": 1, "offset": 0}),
        ]
    )

    validation = tool_result(responses[1])
    assert validation["valid"] is False
    assert "## Next Steps" in validation["missing_sections"]
    listing = tool_result(responses[2])
    assert listing["count"] == 1
    assert listing["has_more"] is True
    assert listing["next_offset"] == 1
