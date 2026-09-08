import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import server.handoff_mcp as handoff_mcp
from server import session_switch, telemetry


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
        "handoff_search",
        "handoff_project",
        "handoff_import",
        "handoff_export",
        "handoff_setup",
    }

    export = next(
        tool for tool in tool_result(responses[1])["tools"]
        if tool["name"] == "handoff_export"
    )
    assert export["annotations"]["readOnlyHint"] is False


def test_setup_commands_come_from_server_installation_without_workspace(tmp_path, monkeypatch):
    import shlex

    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    response = handoff_mcp._call_tool({"name": "handoff_setup", "arguments": {}})
    assert not response.get("isError"), response
    result = json.loads(response["content"][0]["text"])
    cli = str(SERVER.parent.parent / "bin/session-handoff")
    for client in ("claude", "codex"):
        assert shlex.split(result["setup_commands"][client]) == [
            "python3", cli, "setup", "--client", client,
        ]
    assert shlex.split(result["telemetry_commands"]["enable"]) == [
        "python3", cli, "telemetry", "enable",
    ]
    assert not list(tmp_path.iterdir())
    again = handoff_mcp._call_tool({"name": "handoff_setup"})
    assert again == response
    assert handoff_mcp._call_tool({"name": "handoff_setup", "arguments": {
        "plugin_root": "/untrusted",
    }})["isError"] is True


def test_handoff_create_schema_explains_central_and_legacy_modes():
    tool = next(tool for tool in handoff_mcp.TOOLS if tool["name"] == "handoff_create")
    properties = tool["inputSchema"]["properties"]

    assert "immutable central record outside the workspace" in tool["description"]
    assert "explicit legacy workspace file" in tool["description"]
    assert "overwrite=true" in tool["description"]
    assert "managed launcher" in tool["description"]
    assert "immutable central record outside the workspace" in properties["name"]["description"]
    assert "legacy workspace file" in properties["path"]["description"]
    assert "legacy" in properties["overwrite"]["description"]
    assert "managed launcher" in properties["auto_switch"]["description"]


@pytest.mark.parametrize("bound", [True, False])
def test_cross_project_read_error_explains_scope_or_rebind(tmp_path, monkeypatch, bound):
    workspace = tmp_path / "workspace"; foreign_workspace = tmp_path / "foreign"
    workspace.mkdir(); foreign_workspace.mkdir()
    if not bound:
        workspace = tmp_path / "unbound"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    handoff_mcp.handoff_store.create_record(str(workspace if bound else foreign_workspace), "local.md", "local")
    foreign = handoff_mcp.handoff_store.create_record(str(foreign_workspace), "foreign.md", "foreign-secret")

    with pytest.raises(handoff_mcp.HandoffError) as error:
        handoff_mcp._read({"workspace": str(workspace), "ref": foreign["ref"]})

    message = str(error.value)
    assert "scope='all'" in message
    assert "handoff_project" in message
    assert str(workspace) not in message
    assert "foreign-secret" not in message


def test_project_status_lists_existing_project_ids_and_labels(tmp_path, monkeypatch):
    workspace = tmp_path / "work"; other = tmp_path / "other"
    workspace.mkdir(); other.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    first = handoff_mcp.handoff_store.create_record(str(workspace), "one.md", "one")
    second = handoff_mcp.handoff_store.create_record(str(other), "two.md", "two")

    result = handoff_mcp._project({"workspace": str(workspace)})

    assert result["project_id"] == first["project_id"]
    assert {item["project_id"] for item in result["projects"]} == {first["project_id"], second["project_id"]}
    assert all(isinstance(item["label"], str) for item in result["projects"])


def test_project_status_paginates_existing_projects(tmp_path, monkeypatch):
    with tempfile.TemporaryDirectory(dir="/var/tmp") as root:
        workspace = Path(root) / "work"; other = Path(root) / "other"
        workspace.mkdir(); other.mkdir()
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        first = handoff_mcp.handoff_store.create_record(str(workspace), "one.md", "one")
        second = handoff_mcp.handoff_store.create_record(str(other), "two.md", "two")

        page = handoff_mcp._project({"workspace": str(workspace), "limit": 1})

        assert page["count"] == 1
        assert page["has_more"] is True
        assert isinstance(page["next_cursor"], str)
        assert page["projects"][0]["project_id"] in {first["project_id"], second["project_id"]}

        continuation = handoff_mcp._project(
            {"workspace": str(workspace), "limit": 1, "cursor": page["next_cursor"]}
        )

        assert continuation["count"] == 1
        assert continuation["has_more"] is False
        assert continuation["next_cursor"] is None
        assert continuation["projects"][0]["project_id"] != page["projects"][0]["project_id"]


def test_list_rejects_unknown_storage(tmp_path):
    with pytest.raises(handoff_mcp.HandoffError, match="storage"):
        handoff_mcp._list({"workspace": str(tmp_path), "storage": "archive"})


def test_central_search_rejects_raw_query_over_byte_limit(tmp_path, monkeypatch):
    workspace = tmp_path / "work"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    query = "PASSWORD=" + "secret" * 200

    with pytest.raises(handoff_mcp.HandoffError, match="query exceeds"):
        handoff_mcp._search({"workspace": str(workspace), "storage": "central", "query": query})


def test_stdio_central_create_returns_ref(tmp_path):
    workspace = tmp_path / "work"; workspace.mkdir()
    env = {**os.environ, "HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "data"), "XDG_STATE_HOME": str(tmp_path / "state")}
    content = "## Goal\nneedle\n## Constraints & Preferences\nnone\n## Progress\ndone\n## Key Decisions\nnone\n## Critical Context\nnone\n## Next Steps\nnext\n"
    calls = [{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"handoff_create","arguments":{"workspace":str(workspace),"name":"next.md","content":content}}}]
    proc = subprocess.run(["python3","-m","server.handoff_mcp"], input="\n".join(json.dumps(x) for x in calls)+"\n", text=True, capture_output=True, env=env, check=True)
    payload = json.loads(proc.stdout)["result"]["content"][0]["text"]
    assert json.loads(payload)["storage"] == "central"


def test_stdio_legacy_import_export_reimport_idempotent(tmp_path):
    workspace = tmp_path / "work"; workspace.mkdir(); source = workspace / "legacy.md"
    source.write_text("## Goal\nlegacy\n## Constraints & Preferences\nnone\n## Progress\ndone\n## Key Decisions\nnone\n## Critical Context\nnone\n## Next Steps\nnext\n")
    before = source.read_bytes(); env = {**os.environ, "HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "data"), "XDG_STATE_HOME": str(tmp_path / "state")}
    def call(i, name, args):
        request = json.dumps({"jsonrpc":"2.0","id":i,"method":"tools/call","params":{"name":name,"arguments":args}})+"\n"
        response = json.loads(subprocess.run(["python3", "-m", "server.handoff_mcp"], input=request, capture_output=True, text=True, env=env, check=True).stdout)
        if "result" not in response: raise AssertionError(response)
        value = response["result"]["content"][0]["text"]
        try: return json.loads(value)
        except json.JSONDecodeError as exc: raise AssertionError(value) from exc
    imported = call(1, "handoff_import", {"workspace":str(workspace),"path":"legacy.md"})
    call(2, "handoff_export", {"workspace":str(workspace),"ref":imported["ref"],"directory":"bundle"})
    again = call(3, "handoff_import", {"workspace":str(workspace),"path":"bundle"})
    assert again["ref"] == imported["ref"] and again["idempotent"] is True
    assert source.read_bytes() == before and (workspace / "bundle/manifest.json").is_file()


def test_legacy_import_uses_source_mtime_and_rejects_name_conflicts(tmp_path, monkeypatch):
    workspace = tmp_path / "work"; workspace.mkdir()
    source = workspace / "legacy.md"
    content = "## Goal\nlegacy\n## Constraints & Preferences\nnone\n## Progress\ndone\n## Key Decisions\nnone\n## Critical Context\nnone\n## Next Steps\nnext\n"
    source.write_text(content, encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    first_mtime = 1_700_000_000
    os.utime(source, (first_mtime, first_mtime))

    imported = handoff_mcp._import({"workspace": str(workspace), "path": "legacy.md", "name": "first.md"})
    expected_created = "2023-11-14T22:13:20Z"
    record = handoff_mcp.handoff_store.read_record(imported["ref"], str(workspace))
    assert record["manifest"]["created_at"] == expected_created

    later_mtime = first_mtime + 86_400
    os.utime(source, (later_mtime, later_mtime))
    again = handoff_mcp._import({"workspace": str(workspace), "path": "legacy.md", "name": "first.md"})
    assert again["ref"] == imported["ref"] and again["idempotent"] is True
    assert handoff_mcp.handoff_store.read_record(imported["ref"], str(workspace))["manifest"]["created_at"] == expected_created

    with pytest.raises(handoff_mcp.HandoffError, match="conflict"):
        handoff_mcp._import({"workspace": str(workspace), "path": "legacy.md", "name": "renamed.md"})

    changed = content.replace("legacy", "changed")
    source.write_text(changed, encoding="utf-8")
    changed_import = handoff_mcp._import({"workspace": str(workspace), "path": "legacy.md", "name": "first.md"})
    assert changed_import["ref"] != imported["ref"]
    assert handoff_mcp.handoff_store.read_record(imported["ref"], str(workspace))["content"] == content


def test_legacy_import_redacts_secret_source_path_metadata(tmp_path, monkeypatch):
    workspace = tmp_path / "work"; workspace.mkdir()
    source_dir = workspace / "handoffs"; source_dir.mkdir()
    source = source_dir / "API_TOKEN=secret.md"
    source.write_text("## Goal\nlegacy\n## Constraints & Preferences\nnone\n## Progress\ndone\n## Key Decisions\nnone\n## Critical Context\nnone\n## Next Steps\nnext\n", encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    imported = handoff_mcp._import({"workspace": str(workspace), "path": "handoffs/API_TOKEN=secret.md", "name": "source.md"})
    origin = handoff_mcp.handoff_store.read_record(imported["ref"], str(workspace))["manifest"]["origin"]

    assert "secret" not in json.dumps(origin)
    assert source.is_file()


def test_legacy_import_race_returns_idempotent_existing_record(tmp_path, monkeypatch):
    workspace = tmp_path / "work"; workspace.mkdir()
    source = workspace / "legacy.md"
    source.write_text("## Goal\nlegacy\n## Constraints & Preferences\nnone\n## Progress\ndone\n## Key Decisions\nnone\n## Critical Context\nnone\n## Next Steps\nnext\n", encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    real_publish = handoff_mcp.handoff_store.publish_record

    def publish_then_report_race(*args, **kwargs):
        real_publish(*args, **kwargs)
        raise handoff_mcp.handoff_store.HandoffStoreError("central handoff already exists")

    monkeypatch.setattr(handoff_mcp.handoff_store, "publish_record", publish_then_report_race)
    result = handoff_mcp._import({"workspace": str(workspace), "path": "legacy.md"})

    assert result["idempotent"] is True


@pytest.mark.parametrize("params", [None, [], "invalid"])
def test_initialize_non_object_params_use_default_protocol_version(params):
    response = handoff_mcp.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
    )

    assert response["result"]["protocolVersion"] == handoff_mcp.DEFAULT_PROTOCOL_VERSION


def test_handoff_create_schema_accepts_exact_state_v1_contract():
    tool = next(tool for tool in handoff_mcp.TOOLS if tool["name"] == "handoff_create")
    schema = tool["inputSchema"]

    assert schema["required"] == ["workspace"]
    assert not {"oneOf", "anyOf", "allOf"}.intersection(schema)
    description = tool["description"]
    assert "exactly one" in description
    assert "path" in description and "name" in description
    assert "content" in description and "state" in description
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


@pytest.mark.parametrize("tool_name", ["handoff_read", "handoff_validate"])
def test_read_tool_schemas_are_root_object_compatible_and_describe_exclusivity(tool_name):
    tool = next(tool for tool in handoff_mcp.TOOLS if tool["name"] == tool_name)
    schema = tool["inputSchema"]

    assert schema["type"] == "object"
    assert not {"oneOf", "anyOf", "allOf"}.intersection(schema)
    description = tool["description"]
    assert "exactly one" in description
    assert "path" in description and "ref" in description


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "handoffs/bad.md", "name": "bad.md", "content": "content"},
        {"path": "handoffs/bad.md", "content": "content", "state": structured_state()},
        {"path": "handoffs/bad.md"},
    ],
)
def test_create_rejects_exclusive_fields_without_write_or_success_telemetry(
    monkeypatch, tmp_path, arguments
):
    summaries = []
    monkeypatch.setenv("SESSION_HANDOFF_CLIENT", "codex")
    monkeypatch.setattr(handoff_mcp, "record_terminal_outcome", summaries.append)

    with pytest.raises(handoff_mcp.HandoffError, match="exactly one"):
        handoff_mcp._create({"workspace": str(tmp_path), **arguments})

    assert not list(tmp_path.iterdir())
    assert summaries == []


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


def test_call_tool_accepts_standard_mcp_meta(tmp_path):
    path = tmp_path / "handoff.md"
    path.write_text("## Goal\nmeta\n")
    result = handoff_mcp._call_tool({"name": "handoff_read", "arguments": {"workspace": str(tmp_path), "path": "handoff.md"}, "_meta": {"progressToken": "real-client"}})
    assert result.get("isError") is not True


def test_call_tool_rejects_unknown_tool_argument():
    result = handoff_mcp._call_tool(
        {
            "name": "handoff_read",
            "arguments": {"workspace": ".", "path": "handoff.md", "unexpected": True},
        }
    )

    assert result["isError"] is True
    assert result["structuredContent"]["message"] == "unknown tool argument: unexpected"


def test_central_create_store_errors_remain_correlated_tool_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    result = handoff_mcp._call_tool({"name": "handoff_create", "arguments": {"workspace": str(tmp_path), "name": "../bad", "content": "doc"}})
    assert result["isError"] is True
    assert "name must contain" in result["structuredContent"]["message"]


def test_invalid_utf8_bundle_remains_a_correlated_tool_error(tmp_path):
    bundle = tmp_path / "bundle"; bundle.mkdir()
    (bundle / "document.md").write_bytes(b"\xff")
    (bundle / "manifest.json").write_text("{}")
    result = handoff_mcp._call_tool({"name": "handoff_import", "arguments": {"workspace": str(tmp_path), "path": "bundle"}})
    assert result["isError"] is True
    assert "decode" in result["structuredContent"]["message"]


def test_import_rejects_non_regular_bundle_entries(tmp_path):
    bundle = tmp_path / "bundle"; bundle.mkdir()
    os.mkfifo(bundle / "document.md")
    (bundle / "manifest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(handoff_mcp.HandoffError, match="bundle"):
        handoff_mcp._import({"workspace": str(tmp_path), "path": "bundle"})


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


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("API_TOKEN=[REDACTED]secret-value", "API_TOKEN=[REDACTED]"),
        ("API_TOKEN='[REDACTED]'secret-value", "API_TOKEN='[REDACTED]'"),
    ],
)
def test_redact_secrets_removes_text_attached_to_redacted_marker(source, expected):
    redacted, count = handoff_mcp.redact_secrets(source)

    assert redacted == expected
    assert count == 1


def test_search_returns_redacted_matches_from_handoffs(tmp_path):
    handoffs = tmp_path / "handoffs"
    nested = handoffs / "archive"
    nested.mkdir(parents=True)
    (handoffs / "a.md").write_text(
        "## Goal\nNeedle API_TOKEN=top-secret\n", encoding="utf-8"
    )
    (nested / "b.md").write_text("## Goal\nAnother needle\n", encoding="utf-8")

    responses = exchange(
        [
            initialized(),
            call(2, "handoff_search", {"workspace": str(tmp_path), "query": "needle"}),
        ]
    )

    result = tool_result(responses[1])
    assert [item["path"] for item in result["items"]] == [
        "handoffs/a.md",
        "handoffs/archive/b.md",
    ]
    assert result["items"][0]["matches"] == [
        {"line": 2, "snippet": "Needle API_TOKEN=[REDACTED]"}
    ]
    assert result["scan_truncated"] is False


def test_search_stops_at_global_file_budget(monkeypatch, tmp_path):
    handoffs = tmp_path / "handoffs"
    handoffs.mkdir()
    (handoffs / "a.md").write_text("needle\n", encoding="utf-8")
    (handoffs / "b.md").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(handoff_mcp, "MAX_SEARCH_FILES", 1, raising=False)

    result = handoff_mcp._search({"workspace": str(tmp_path), "query": "needle"})

    assert result["scanned_files"] == 1
    assert result["total_count"] == 1
    assert result["scan_truncated"] is True


def test_search_stops_before_global_byte_budget(monkeypatch, tmp_path):
    handoffs = tmp_path / "handoffs"
    handoffs.mkdir()
    (handoffs / "a.md").write_text("needle\n", encoding="utf-8")
    (handoffs / "b.md").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(handoff_mcp, "MAX_SEARCH_BYTES", 7, raising=False)

    result = handoff_mcp._search({"workspace": str(tmp_path), "query": "needle"})

    assert result["scanned_bytes"] == 7
    assert result["total_count"] == 1
    assert result["scan_truncated"] is True


def test_search_trims_page_to_global_output_budget(monkeypatch, tmp_path):
    handoffs = tmp_path / "handoffs"
    handoffs.mkdir()
    for name in ("a.md", "b.md", "c.md"):
        (handoffs / name).write_text(f"needle {'x' * 400}\n", encoding="utf-8")
    monkeypatch.setattr(handoff_mcp, "MAX_SEARCH_OUTPUT_BYTES", 1_000, raising=False)

    result = handoff_mcp._search({"workspace": str(tmp_path), "query": "needle"})

    assert len(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")) <= 1_000
    assert result["output_truncated"] is True
    assert result["count"] < result["total_count"]
    assert result["next_offset"] == result["count"]


def test_search_paginates_and_counts_unreadable_handoffs(tmp_path):
    handoffs = tmp_path / "handoffs"
    handoffs.mkdir()
    (handoffs / "a.md").write_text("needle\n", encoding="utf-8")
    (handoffs / "b.md").write_text("needle\n", encoding="utf-8")
    (handoffs / "bad.md").write_bytes(b"needle \xff\n")

    first = handoff_mcp._search(
        {"workspace": str(tmp_path), "query": "needle", "limit": 1}
    )
    second = handoff_mcp._search(
        {"workspace": str(tmp_path), "query": "needle", "limit": 1, "offset": 1}
    )

    assert first["count"] == 1
    assert first["total_count"] == 2
    assert first["next_offset"] == 1
    assert first["skipped_count"] == 1
    assert second["items"][0]["path"] == "handoffs/b.md"
    assert second["has_more"] is False


def test_central_search_cursor_reaches_match_beyond_file_budget(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    handoff_mcp.handoff_store.create_record(str(workspace), "first.md", "nothing")
    expected = handoff_mcp.handoff_store.create_record(str(workspace), "second.md", "needle")
    monkeypatch.setattr(handoff_mcp, "MAX_SEARCH_FILES", 1)

    first = handoff_mcp._search({"workspace": str(workspace), "storage": "central", "query": "needle"})
    second = handoff_mcp._search({"workspace": str(workspace), "storage": "central", "query": "needle", "cursor": first["next_cursor"]})

    assert first["items"] == [] and first["has_more"] is True
    assert second["items"][0]["ref"] == expected["ref"]
    assert second["has_more"] is False


def test_failed_rebind_preserves_previous_project_and_catalog_recovery(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; other = tmp_path / "other"
    workspace.mkdir(); other.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    project_a = handoff_mcp.handoff_store.register_project(str(workspace))
    project_b = handoff_mcp.handoff_store.register_project(str(other))
    real_catalog = handoff_mcp.handoff_store._catalog

    def fail_catalog():
        raise handoff_mcp.handoff_store.HandoffStoreError("injected catalog failure")

    monkeypatch.setattr(handoff_mcp.handoff_store, "_catalog", fail_catalog)
    record = handoff_mcp.handoff_store.create_record(str(workspace), "a.md", "needle")
    assert handoff_mcp.handoff_store._dirty_catalog_project() == project_a

    with pytest.raises(handoff_mcp.handoff_store.HandoffStoreError, match="catalog"):
        handoff_mcp.handoff_store.associate_project(str(workspace), project_b, replace=True)

    monkeypatch.setattr(handoff_mcp.handoff_store, "_catalog", real_catalog)
    assert handoff_mcp.handoff_store.lookup_project(str(workspace)) == project_a
    assert handoff_mcp.handoff_store._dirty_catalog_project() == project_a
    assert handoff_mcp._read({"workspace": str(workspace), "ref": record["ref"]})["content"] == "needle"
    assert handoff_mcp._list({"workspace": str(workspace), "storage": "central"})["items"][0]["ref"] == record["ref"]
    assert handoff_mcp._search({"workspace": str(workspace), "storage": "central", "query": "needle"})["items"][0]["ref"] == record["ref"]


def test_central_search_cursor_is_bound_to_query(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for index in range(2): handoff_mcp.handoff_store.create_record(str(workspace), f"{index}.md", "needle")
    first = handoff_mcp._search({"workspace": str(workspace), "storage": "central", "query": "needle", "limit": 1})

    with pytest.raises(handoff_mcp.HandoffError, match="cursor"):
        handoff_mcp._search({"workspace": str(workspace), "storage": "central", "query": "different", "cursor": first["next_cursor"]})


def test_central_search_does_not_preload_catalog_documents(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    expected = handoff_mcp.handoff_store.create_record(str(workspace), "one.md", "needle")
    monkeypatch.setattr(handoff_mcp.handoff_store, "_record_summary", lambda *_: pytest.fail("catalog page preloaded document"))

    result = handoff_mcp._search({"workspace": str(workspace), "storage": "central", "query": "needle"})

    assert result["items"][0]["ref"] == expected["ref"]


def test_central_search_skips_record_corrupted_after_indexing(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    record = handoff_mcp.handoff_store.create_record(str(workspace), "one.md", "needle")
    document = handoff_mcp.handoff_store.data_root() / "projects" / record["project_id"] / "handoffs" / record["handoff_id"] / "document.md"
    document.write_bytes(b"needle \xff"); document.chmod(0o600)

    result = handoff_mcp._search({"workspace": str(workspace), "storage": "central", "query": "needle"})

    assert result["items"] == []
    assert result["skipped_count"] == 1


def test_central_search_charges_corrupt_documents_to_byte_budget(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    records = [handoff_mcp.handoff_store.create_record(str(workspace), f"{index}.md", "needle!") for index in range(2)]
    for record in records:
        document = handoff_mcp.handoff_store.data_root() / "projects" / record["project_id"] / "handoffs" / record["handoff_id"] / "document.md"
        document.write_bytes(b"needle\xff"); document.chmod(0o600)
    monkeypatch.setattr(handoff_mcp, "MAX_SEARCH_BYTES", len(b"needle\xff"))
    real_read = handoff_mcp.handoff_store._read
    document_reads = 0

    def counted_read(path, limit, expected_identity=None):
        nonlocal document_reads
        if path.name == "document.md": document_reads += 1
        return real_read(path, limit) if expected_identity is None else real_read(path, limit, expected_identity)

    monkeypatch.setattr(handoff_mcp.handoff_store, "_read", counted_read)
    result = handoff_mcp._search({"workspace": str(workspace), "storage": "central", "query": "needle"})

    assert document_reads == 1
    assert result["scanned_bytes"] == len(b"needle\xff")
    assert result["skipped_count"] == 1
    assert result["has_more"] is True


def test_central_search_checks_byte_budget_before_reading_next_record(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for index in range(2): handoff_mcp.handoff_store.create_record(str(workspace), f"{index}.md", "needle")
    monkeypatch.setattr(handoff_mcp, "MAX_SEARCH_BYTES", len("needle".encode()))
    real_read = handoff_mcp.handoff_store._read
    document_reads = 0

    def counted_read(path, limit, expected_identity=None):
        nonlocal document_reads
        if path.name == "document.md": document_reads += 1
        return real_read(path, limit) if expected_identity is None else real_read(path, limit, expected_identity)

    monkeypatch.setattr(handoff_mcp.handoff_store, "_read", counted_read)
    result = handoff_mcp._search({"workspace": str(workspace), "storage": "central", "query": "needle"})

    assert document_reads == 1
    assert result["has_more"] is True
    assert result["next_cursor"] is not None


def test_central_search_output_budget_measures_rendered_payload(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for index in range(5): handoff_mcp.handoff_store.create_record(str(workspace), f"{index}.md", "needle " + "x" * 80)
    baseline = handoff_mcp._search({"workspace": str(workspace), "storage": "central", "query": "needle", "limit": 100})
    compact = len(json.dumps(baseline, ensure_ascii=False).encode())
    rendered = len(json.dumps(baseline, ensure_ascii=False, indent=2).encode())
    budget = (compact + rendered) // 2
    monkeypatch.setattr(handoff_mcp, "MAX_SEARCH_OUTPUT_BYTES", budget)

    result = handoff_mcp._search({"workspace": str(workspace), "storage": "central", "query": "needle", "limit": 100})

    assert len(json.dumps(result, ensure_ascii=False, indent=2).encode()) <= budget
    assert result["output_truncated"] is True
    assert result["next_cursor"] is not None


def test_central_list_accepts_cursor_but_not_nonzero_offset(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    expected = [handoff_mcp.handoff_store.create_record(str(workspace), f"{index}.md", "doc")["ref"] for index in range(2)]

    first = handoff_mcp._list({"workspace": str(workspace), "storage": "central", "limit": 1})
    second = handoff_mcp._list({"workspace": str(workspace), "storage": "central", "limit": 1, "cursor": first["next_cursor"]})

    assert [first["items"][0]["ref"], second["items"][0]["ref"]] == expected
    with pytest.raises(handoff_mcp.HandoffError, match="offset"):
        handoff_mcp._list({"workspace": str(workspace), "storage": "central", "offset": 1})


def test_list_and_search_schemas_expose_central_cursor():
    tools = {tool["name"]: tool for tool in handoff_mcp.TOOLS}

    assert tools["handoff_list"]["inputSchema"]["properties"]["cursor"]["type"] == "string"
    assert tools["handoff_search"]["inputSchema"]["properties"]["cursor"]["type"] == "string"


def test_workspace_list_and_search_reject_central_cursor(tmp_path):
    with pytest.raises(handoff_mcp.HandoffError, match="central"):
        handoff_mcp._list({"workspace": str(tmp_path), "cursor": "opaque"})
    with pytest.raises(handoff_mcp.HandoffError, match="central"):
        handoff_mcp._search({"workspace": str(tmp_path), "query": "x", "cursor": "opaque"})


def test_search_rejects_custom_directory_scope(tmp_path):
    result = handoff_mcp._call_tool(
        {
            "name": "handoff_search",
            "arguments": {"workspace": str(tmp_path), "query": "x", "directory": "."},
        }
    )

    assert result["isError"] is True
    assert result["structuredContent"]["message"] == "unknown tool argument: directory"


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
    monkeypatch.setenv("SESSION_HANDOFF_CLIENT", "codex")
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
    monkeypatch.setenv("SESSION_HANDOFF_CLIENT", "codex")

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


@pytest.mark.parametrize("client_name,client", [
    ("claude-code", "claude"),
    ("claude-ai", "claude"),
    ("codex-mcp-client", "codex"),
])
def test_manual_create_uses_known_client_from_mcp_initialize(
    monkeypatch, tmp_path, client_name, client,
):
    content = "".join(f"{section}\n" for section in handoff_mcp.REQUIRED_SECTIONS)
    summaries = []
    monkeypatch.delenv("SESSION_HANDOFF_CLIENT", raising=False)
    monkeypatch.setattr(handoff_mcp, "_mcp_client", None, raising=False)
    monkeypatch.setattr(handoff_mcp, "record_terminal_outcome", summaries.append)
    request = initialized()
    request["params"]["clientInfo"]["name"] = client_name

    handoff_mcp.handle_request(request)
    handoff_mcp._create({
        "workspace": str(tmp_path),
        "path": "handoffs/claude.md",
        "content": content,
    })

    assert summaries[0]["source_client"] == client
    assert summaries[0]["target_client"] == client


def test_manual_create_skips_unknown_client_attribution(monkeypatch, tmp_path):
    content = "".join(f"{section}\n" for section in handoff_mcp.REQUIRED_SECTIONS)
    summaries = []
    monkeypatch.delenv("SESSION_HANDOFF_CLIENT", raising=False)
    monkeypatch.setattr(handoff_mcp, "_mcp_client", None, raising=False)
    monkeypatch.setattr(handoff_mcp, "record_terminal_outcome", summaries.append)
    request = initialized()

    handoff_mcp.handle_request(request)
    handoff_mcp._create({
        "workspace": str(tmp_path),
        "path": "handoffs/unknown.md",
        "content": content,
    })

    assert summaries == []


def test_invalid_initialize_resets_previous_client_attribution(monkeypatch, tmp_path):
    content = "".join(f"{section}\n" for section in handoff_mcp.REQUIRED_SECTIONS)
    summaries = []
    monkeypatch.delenv("SESSION_HANDOFF_CLIENT", raising=False)
    monkeypatch.setattr(handoff_mcp, "_mcp_client", None, raising=False)
    monkeypatch.setattr(handoff_mcp, "record_terminal_outcome", summaries.append)
    known = initialized()
    known["params"]["clientInfo"]["name"] = "claude-code"
    unknown = initialized(2)
    unknown["params"]["clientInfo"]["name"] = "pytest"

    handoff_mcp.handle_request(known)
    handoff_mcp.handle_request(unknown)
    handoff_mcp._create({
        "workspace": str(tmp_path),
        "path": "handoffs/reset.md",
        "content": content,
    })

    assert summaries == []


def test_explicit_client_environment_overrides_mcp_initialize(
    monkeypatch, tmp_path,
):
    content = "".join(f"{section}\n" for section in handoff_mcp.REQUIRED_SECTIONS)
    summaries = []
    monkeypatch.setenv("SESSION_HANDOFF_CLIENT", "codex")
    monkeypatch.setattr(handoff_mcp, "_mcp_client", None, raising=False)
    monkeypatch.setattr(handoff_mcp, "record_terminal_outcome", summaries.append)
    request = initialized()
    request["params"]["clientInfo"]["name"] = "claude-code"

    handoff_mcp.handle_request(request)
    handoff_mcp._create({
        "workspace": str(tmp_path),
        "path": "handoffs/env.md",
        "content": content,
    })

    assert summaries[0]["source_client"] == "codex"


def test_supervised_create_does_not_emit_mcp_terminal_outcome(
    monkeypatch, tmp_path,
):
    content = "".join(f"{section}\n" for section in handoff_mcp.REQUIRED_SECTIONS)
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    summaries = []
    monkeypatch.setenv("SESSION_HANDOFF_CLIENT", "claude")
    monkeypatch.setenv("SESSION_HANDOFF_CONTROL", str(control_dir / "switch.json"))
    monkeypatch.setenv("SESSION_HANDOFF_CONTROL_TOKEN", "token")
    monkeypatch.setattr(handoff_mcp, "record_terminal_outcome", summaries.append)

    result = handoff_mcp._create({
        "workspace": str(tmp_path),
        "path": "handoffs/supervised.md",
        "content": content,
        "auto_switch": True,
    })

    assert result["auto_switch_requested"] is True
    assert summaries == []


def test_manual_claude_create_reaches_closed_telemetry_counter(
    monkeypatch, tmp_path,
):
    content = "".join(f"{section}\n" for section in handoff_mcp.REQUIRED_SECTIONS)
    home = tmp_path / "telemetry-home"
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(home))
    monkeypatch.delenv("SESSION_HANDOFF_CLIENT", raising=False)
    monkeypatch.setattr(handoff_mcp, "_mcp_client", None, raising=False)
    monkeypatch.setattr(session_switch.telemetry, "spawn_detached_flush", lambda *_args: None)
    telemetry.write_config(home, telemetry.enabled_config("2026-09-08T00:00:00Z"))
    request = initialized()
    request["params"]["clientInfo"]["name"] = "claude-code"

    handoff_mcp.handle_request(request)
    result = handoff_mcp._create({
        "workspace": str(tmp_path),
        "path": "handoffs/claude.md",
        "content": content,
    })

    counters = telemetry._load_counters(home)
    operation = next(
        entry["event"]
        for entries in counters["days"].values()
        for entry in entries
        if entry["event"]["event"] == "operation_summary"
    )
    assert result["valid"] is True
    assert operation["operation"] == "handoff"
    assert operation["source_client"] == operation["target_client"] == "claude"
    assert operation["result"] == "success"
    assert operation["failure_stage"] == "none"


def test_central_create_records_manual_fallback_without_supervisor(monkeypatch, tmp_path):
    content = "".join(f"{section}\n" for section in handoff_mcp.REQUIRED_SECTIONS)
    summaries = []
    monkeypatch.setenv("SESSION_HANDOFF_CLIENT", "codex")
    monkeypatch.delenv("SESSION_HANDOFF_CONTROL", raising=False)
    monkeypatch.delenv("SESSION_HANDOFF_CONTROL_TOKEN", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(handoff_mcp, "record_terminal_outcome", summaries.append)

    result = handoff_mcp._create({
        "workspace": str(tmp_path),
        "name": "central.md",
        "content": content,
        "auto_switch": True,
    })

    assert result["auto_switch_requested"] is False
    assert summaries[0]["result"] == "fallback"
    assert summaries[0]["failure_stage"] == "control"


def test_unsupervised_migration_records_control_failure(monkeypatch, tmp_path):
    summaries = []
    monkeypatch.delenv("SESSION_HANDOFF_CONTROL", raising=False)
    monkeypatch.delenv("SESSION_HANDOFF_CONTROL_TOKEN", raising=False)
    monkeypatch.setattr(handoff_mcp, "record_terminal_outcome", summaries.append)

    result = handoff_mcp._migrate({
        "workspace": str(tmp_path),
        "source_client": "claude",
        "target_client": "codex",
        "source_session_id": "source-id",
    })

    assert result["auto_switch_requested"] is False
    assert summaries == [{
        "operation": "migrate",
        "source_client": "claude",
        "target_client": "codex",
        "result": "failure",
        "failure_stage": "control",
    }]


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
