import json
import os
import subprocess
import sys
from pathlib import Path


SERVER = Path(__file__).parents[1] / "server" / "handoff_mcp.py"


def exchange(requests):
    payload = "\n".join(json.dumps(request) for request in requests) + "\n"
    result = subprocess.run(
        [sys.executable, str(SERVER)],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
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
