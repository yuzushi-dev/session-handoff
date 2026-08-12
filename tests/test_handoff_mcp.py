import json
import os
import subprocess
import sys
from pathlib import Path


SERVER = Path(__file__).parents[1] / "server" / "handoff_mcp.py"


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
    return result.get("structuredContent", result)


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
        "handoff_read",
        "handoff_validate",
        "handoff_list",
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
    assert (tmp_path / path).read_text() .find("super-secret-value") == -1
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
    }


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
