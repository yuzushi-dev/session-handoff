import contextlib
import http.server
import json
import os
import shutil
import threading
from pathlib import Path

import pytest

from benchmark.claude_native import ClaudeNativeAdapter, ClaudeNativeError
from benchmark.information_preservation import build_structured_conversation


CLAUDE = Path("/home/cristina/.local/bin/claude.session-handoff-original")
_SOCAT_PATH = os.environ.get("SESSION_HANDOFF_BENCHMARK_SOCAT") or shutil.which("socat")
SOCAT = Path(_SOCAT_PATH) if _SOCAT_PATH else None


def _sse(blocks, *, stop_reason="end_turn", input_tokens=17):
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_fake",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0},
                },
            },
        )
    ]
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            start = {
                "type": "tool_use",
                "id": block["id"],
                "name": block["name"],
                "input": {},
            }
            delta = {
                "type": "input_json_delta",
                "partial_json": json.dumps(block["input"], separators=(",", ":")),
            }
        events.extend(
            [
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": start,
                    },
                ),
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "index": index, "delta": delta},
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": index}),
            ]
        )
    events.extend(
        [
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": 5},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )
    return "".join(
        f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
        for name, data in events
    ).encode()


class _AnthropicHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.gets.append(self.path)
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size))
        self.server.requests.append({"path": self.path, "body": body})
        if self.path.endswith("/count_tokens"):
            payload = json.dumps({"input_tokens": self.server.count_tokens}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        blocks, stop_reason = self.server.reply(body, len(self.server.requests) - 1)
        payload = _sse(
            blocks,
            stop_reason=stop_reason,
            input_tokens=max(17, len(json.dumps(body)) // 4),
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        pass


@contextlib.contextmanager
def _fake_anthropic(reply, *, count_tokens=100):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _AnthropicHandler)
    server.requests = []
    server.gets = []
    server.reply = reply
    server.count_tokens = count_tokens
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _adapter(tmp_path, base_url):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ClaudeNativeAdapter(
        CLAUDE,
        workspace,
        tmp_path / "native",
        base_url,
        timeout=30,
        socat_binary=SOCAT if SOCAT is not None and SOCAT.is_file() else None,
    )


def _seed():
    return [
        {
            "role": "user",
            "content": [{"type": "text", "text": "ORIGINAL_HISTORY_USER"}],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I will inspect."},
                {
                    "type": "tool_use",
                    "id": "toolu_seed",
                    "name": "Read",
                    "input": {"file_path": "/mnt/work/spec.txt"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_seed",
                    "content": "ORIGINAL_TOOL_RESULT",
                }
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "ORIGINAL_HISTORY_ASSISTANT"}],
        },
    ]


def _message_requests(server):
    return [
        item["body"]
        for item in server.requests
        if "/messages" in item["path"] and "count_tokens" not in item["path"]
    ]


def test_rejects_unpinned_binary_and_nonlocal_endpoint(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ClaudeNativeError, match="pinned Claude binary"):
        ClaudeNativeAdapter(
            "/bin/true", workspace, tmp_path / "native", "http://127.0.0.1:9"
        )
    with pytest.raises(ClaudeNativeError, match="loopback"):
        ClaudeNativeAdapter(
            CLAUDE,
            workspace,
            tmp_path / "native",
            "https://api.anthropic.com",
        )


def test_rejects_native_state_inside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ClaudeNativeError, match="outside the workspace"):
        ClaudeNativeAdapter(
            CLAUDE,
            workspace,
            workspace / "native",
            "http://127.0.0.1:9",
        )
    with pytest.raises(ClaudeNativeError, match="beneath HOME or /tmp"):
        ClaudeNativeAdapter(
            CLAUDE,
            workspace,
            "/var/lib/session-handoff-native-test",
            "http://127.0.0.1:9",
        )


def test_structured_seed_is_resumed_into_real_cli_request(tmp_path):
    def reply(_body, _ordinal):
        return [{"type": "text", "text": "RESUMED_OK"}], "end_turn"

    with (
        _fake_anthropic(reply) as (server, base_url),
        _adapter(tmp_path, base_url) as adapter,
    ):
        session_id = adapter.seed(_seed())
        result = adapter.turn(session_id, "CONTINUE_NOW")

    assert result["completed"] is True
    assert result["output"] == "RESUMED_OK"
    assert result["session_id"] == session_id
    assert result["tool_items"] == []
    assert result["seed_usage"] == "estimated_serialized_utf8_bytes_div_4"
    request = _message_requests(server)[0]
    assert request["model"] == "gpt-5.6-luna"
    assert request["output_config"]["effort"] == "xhigh"
    messages = request["messages"]
    assert [item["role"] for item in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert any(
        block.get("text") == "ORIGINAL_HISTORY_USER" for block in messages[0]["content"]
    )
    assert messages[1]["content"][1]["type"] == "tool_use"
    assert messages[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_seed",
        "content": "ORIGINAL_TOOL_RESULT",
    }
    assert any(block.get("text") == "CONTINUE_NOW" for block in messages[-1]["content"])


def test_fork_probe_does_not_contaminate_original_session(tmp_path):
    def reply(body, _ordinal):
        prompt = json.dumps(body["messages"][-1])
        text = "FORK_OK" if "FORK_PROBE" in prompt else "ORIGINAL_OK"
        return [{"type": "text", "text": text}], "end_turn"

    with (
        _fake_anthropic(reply) as (server, base_url),
        _adapter(tmp_path, base_url) as adapter,
    ):
        original_id = adapter.seed(_seed())
        fork = adapter.turn(original_id, "FORK_PROBE", fork=True)
        continued = adapter.turn(original_id, "ORIGINAL_CONTINUATION")

    assert fork["session_id"] != original_id
    assert continued["session_id"] == original_id
    first, second = _message_requests(server)
    assert "FORK_PROBE" in json.dumps(first)
    assert "FORK_PROBE" not in json.dumps(second)
    assert "ORIGINAL_CONTINUATION" in json.dumps(second)


def test_real_compact_emits_boundary_and_replaces_precompact_history(tmp_path):
    def reply(body, _ordinal):
        if "POSTCOMPACT_PROBE" in json.dumps(body["messages"][-1]):
            return [{"type": "text", "text": "POSTCOMPACT_OK"}], "end_turn"
        return [{"type": "text", "text": "COMPACT_SUMMARY_ONLY"}], "end_turn"

    with (
        _fake_anthropic(reply, count_tokens=50_000) as (server, base_url),
        _adapter(tmp_path, base_url) as adapter,
    ):
        conversation = build_structured_conversation("compound-rot", "long", 1)
        original_id = adapter.seed(conversation)
        compacted = adapter.compact(original_id)
        probe = adapter.turn(original_id, "POSTCOMPACT_PROBE", fork=True)

    assert compacted["actual_compaction"] is True
    assert compacted["event"]["subtype"] == "compact_boundary"
    assert compacted["event"]["compact_metadata"]["cumulative_dropped_tokens"] > 0
    assert probe["session_id"] != original_id
    requests = _message_requests(server)
    assert requests[0]["output_config"]["effort"] == "xhigh"
    postcompact = requests[-1]
    assert postcompact["output_config"]["effort"] == "xhigh"
    assert "COMPACT_SUMMARY_ONLY" in json.dumps(postcompact)
    assert "tool output 00000" not in json.dumps(postcompact)
    assert len(requests) == 2  # summary, then probe; /context is provider-free


def test_short_noop_compact_is_not_reported_as_actual(tmp_path):
    def reply(_body, _ordinal):
        return [{"type": "text", "text": "UNEXPECTED_API_CALL"}], "end_turn"

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with (
        _fake_anthropic(reply) as (_server, base_url),
        ClaudeNativeAdapter(
            CLAUDE,
            workspace,
            tmp_path / "native",
            base_url,
            timeout=30,
        ) as adapter,
    ):
        session_id = adapter.seed(_seed())
        result = adapter.compact(session_id)

    assert result["actual_compaction"] is False


def test_write_mode_fails_closed_when_socat_is_unavailable(tmp_path):
    if shutil.which("socat") is not None:
        pytest.skip("host has the write-mode sandbox prerequisite")

    def reply(_body, _ordinal):
        pytest.fail("write mode reached the API without sandbox prerequisites")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with (
        _fake_anthropic(reply) as (_server, base_url),
        ClaudeNativeAdapter(
            CLAUDE,
            workspace,
            tmp_path / "native",
            base_url,
            timeout=30,
        ) as adapter,
    ):
        session_id = adapter.seed(_seed())
        with pytest.raises(ClaudeNativeError, match="socat is required"):
            adapter.turn(session_id, "MUST_NOT_RUN", write=True)


@pytest.mark.skipif(
    SOCAT is None or not SOCAT.is_file(),
    reason="temporary socat sandbox prerequisite missing",
)
def test_native_state_is_hidden_from_read_and_bash_but_workspace_is_writable(tmp_path):
    calls = [
        {
            "type": "tool_use",
            "id": "toolu_read",
            "name": "Read",
            "input": {"file_path": "/mnt/native/secret.txt"},
        },
        {
            "type": "tool_use",
            "id": "toolu_bash",
            "name": "Bash",
            "input": {"command": "cat /mnt/native/secret.txt"},
        },
        {
            "type": "tool_use",
            "id": "toolu_proc_1",
            "name": "Bash",
            "input": {"command": "cat /proc/1/root/mnt/native/secret.txt"},
        },
        {
            "type": "tool_use",
            "id": "toolu_proc_self",
            "name": "Bash",
            "input": {"command": "cat /proc/self/root/mnt/native/secret.txt"},
        },
        {
            "type": "tool_use",
            "id": "toolu_bash_write",
            "name": "Bash",
            "input": {
                "command": (
                    "printf 'BASH_WORKSPACE_OK\\n' > "
                    "/mnt/work/bash-written.txt && cat /mnt/work/bash-written.txt"
                )
            },
        },
        {
            "type": "tool_use",
            "id": "toolu_write",
            "name": "Edit",
            "input": {
                "file_path": "/mnt/work/written.txt",
                "old_string": "BEFORE\n",
                "new_string": "WORKSPACE_OK\n",
            },
        },
    ]

    def reply(body, _ordinal):
        if any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in body["messages"][-1].get("content", [])
        ):
            return [{"type": "text", "text": "TOOLS_DONE"}], "end_turn"
        return calls, "tool_use"

    with (
        _fake_anthropic(reply) as (server, base_url),
        _adapter(tmp_path, base_url) as adapter,
    ):
        calls.append(
            {
                "type": "tool_use",
                "id": "toolu_network",
                "name": "Bash",
                "input": {
                    "command": f"curl --max-time 1 {base_url}/native-tool-must-not-reach"
                },
            }
        )
        (adapter.state_root / "secret.txt").write_text("NATIVE_SECRET\n")
        (adapter.workspace / "written.txt").write_text("BEFORE\n")
        session_id = adapter.seed(_seed())
        result = adapter.turn(session_id, "RUN_ISOLATION_CHECK", write=True, fork=True)

    assert result["completed"] is True
    assert (tmp_path / "workspace/written.txt").read_text() == "WORKSPACE_OK\n"
    assert (tmp_path / "workspace/bash-written.txt").read_text() == (
        "BASH_WORKSPACE_OK\n"
    )
    followup = _message_requests(server)[-1]
    results = {
        block["tool_use_id"]: block
        for block in followup["messages"][-1]["content"]
        if block.get("type") == "tool_result"
    }
    assert results["toolu_read"].get("is_error") is True
    assert results["toolu_bash"].get("is_error") is True
    assert results["toolu_proc_1"].get("is_error") is True
    assert results["toolu_proc_self"].get("is_error") is True
    assert results["toolu_bash_write"].get("is_error") is not True
    assert "BASH_WORKSPACE_OK" in json.dumps(results["toolu_bash_write"])
    assert results["toolu_write"].get("is_error") is not True
    assert results["toolu_network"].get("is_error") is True
    assert server.gets == []
    assert "NATIVE_SECRET" not in json.dumps(result)
