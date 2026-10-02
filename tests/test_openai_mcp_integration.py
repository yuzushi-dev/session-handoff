from __future__ import annotations

from typing import Any

from server import handoff_mcp
from server.codex_context import CodexContext


def _call_status(request_id: int, meta: Any = None, arguments: Any = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "name": "codex_thread_status",
        "arguments": {} if arguments is None else arguments,
    }
    if meta is not None:
        params["_meta"] = meta
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": params,
    }


def test_codex_status_tool_is_read_only_and_has_app_resource() -> None:
    tool = next(tool for tool in handoff_mcp.TOOLS if tool["name"] == "codex_thread_status")

    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["inputSchema"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert tool["outputSchema"]["required"] == [
        "schema", "thread", "context", "checkpoint", "suggestion"
    ]
    assert tool["outputSchema"]["properties"]["context"]["properties"]["usedTokens"] == {
        "type": ["number", "null"]
    }
    assert tool["_meta"]["ui"]["resourceUri"].startswith("ui://")


def test_codex_status_uses_each_call_meta_without_sticky_binding(monkeypatch) -> None:
    seen: list[CodexContext] = []

    def status(context: CodexContext) -> dict[str, Any]:
        seen.append(context)
        return {
            "schema": "session-handoff.codex-thread-status/v1",
            "thread": {"id": context.thread_id, "sessionId": context.session_id},
            "context": {
                "usedTokens": 120,
                "windowTokens": 1000,
                "usedPercent": 12,
                "source": "app-server",
            },
            "checkpoint": {"summary": None, "updatedAt": None},
            "suggestion": None,
        }

    monkeypatch.setattr(handoff_mcp, "_codex_thread_status", status)
    first = handoff_mcp.handle_request(
        _call_status(1, {"threadId": "thread-a", "sessionId": "family-a"})
    )
    second = handoff_mcp.handle_request(
        _call_status(2, {"threadId": "thread-b", "sessionId": "family-b"})
    )

    assert [item.thread_id for item in seen] == ["thread-a", "thread-b"]
    assert [item.session_id for item in seen] == ["family-a", "family-b"]
    assert first["result"]["structuredContent"]["thread"]["id"] == "thread-a"
    assert second["result"]["structuredContent"]["thread"]["id"] == "thread-b"
    assert first["result"]["content"][0]["type"] == "text"


def test_codex_status_does_not_accept_frontend_thread_argument() -> None:
    response = handoff_mcp.handle_request(
        _call_status(3, {"threadId": "trusted-thread"}, {"thread_id": "other-thread"})
    )

    assert response["result"]["isError"] is True
    assert "unknown tool argument: thread_id" in response["result"]["content"][0]["text"]


def test_status_without_thread_binding_does_not_open_app_server(monkeypatch) -> None:
    def unexpected_adapter():
        raise AssertionError("adapter must not be opened without a threadId")

    monkeypatch.setattr(handoff_mcp, "CodexAppServerAdapter", unexpected_adapter)
    result = handoff_mcp._codex_thread_status(CodexContext(session_id="family-only"))

    assert result["thread"] == {
        "id": None,
        "sessionId": "family-only",
        "status": "unavailable",
    }
    assert result["context"] == {
        "usedTokens": None,
        "windowTokens": None,
        "usedPercent": None,
        "source": "unavailable",
    }
    assert result["checkpoint"] == {"summary": None, "updatedAt": None}
    assert result["suggestion"] is None


def test_app_resource_methods_are_advertised_and_allowlisted() -> None:
    initialized = handoff_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "codex"}},
        }
    )
    resources = handoff_mcp.handle_request(
        {"jsonrpc": "2.0", "id": 5, "method": "resources/list", "params": {}}
    )

    assert initialized["result"]["capabilities"]["resources"]["listChanged"] is False
    resource = resources["result"]["resources"][0]
    assert resource["mimeType"] == "text/html;profile=mcp-app"

    read = handoff_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "resources/read",
            "params": {"uri": resource["uri"]},
        }
    )
    assert read["result"]["contents"][0]["uri"] == resource["uri"]
    assert read["result"]["contents"][0]["mimeType"] == "text/html;profile=mcp-app"
    assert "<html" in read["result"]["contents"][0]["text"].lower()

    rejected = handoff_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "resources/read",
            "params": {"uri": "file:///etc/passwd"},
        }
    )
    assert rejected["error"]["code"] == -32602


def test_resources_read_rejects_missing_or_malformed_params() -> None:
    for params in (None, [], {}, {"uri": 12}):
        response = handoff_mcp.handle_request(
            {"jsonrpc": "2.0", "id": 8, "method": "resources/read", "params": params}
        )
        assert response["error"]["code"] == -32602
