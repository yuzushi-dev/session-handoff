import json
import shutil
from pathlib import Path

import pytest

from benchmark.information_preservation import (
    ARMS,
    CELL_COUNT,
    CASES,
    BANDS,
    BenchmarkGateError,
    CompactionError,
    CompactionTimeout,
    COMMON_DOWNSTREAM_CONTRACT,
    NativeAppServer,
    ProtocolError,
    STANDARD_NATIVE_COMPACT_PARAMS,
    StructuredConversationError,
    build_cells,
    build_structured_conversation,
    checkpoint_native,
    create_read_handoff,
    compact_completion,
    conversation_to_response_items,
    discover_native_interface,
    execute_benchmark,
    fork_recovery_probes,
    gate_state,
    main,
    plan_benchmark,
    run_metadata,
    _load_build_instructions,
    _tool_item_evidence,
    validate_structured_conversation,
)


def _install_measured_skill(root: Path) -> None:
    destination = root / "skills/session-handoff/SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(
        (Path(__file__).parents[1] / "skills/session-handoff/SKILL.md").read_bytes()
    )


def test_structured_fixture_keeps_user_assistant_tool_order_without_gold():
    conversation = build_structured_conversation("superseded-decision", "short", 1)

    validate_structured_conversation(conversation)
    roles = [message["role"] for message in conversation["messages"]]
    assert roles[0] == "user"
    assert "assistant" in roles
    assert "tool" in roles
    assert roles[-1] == "user"
    assert roles == conversation["role_order"]
    encoded = json.dumps(conversation, sort_keys=True)
    assert "gold_facts" not in encoded
    assert "stale_traps" not in encoded


def test_information_fixture_manifest_is_gold_free_and_counts_the_matrix():
    manifest = json.loads(
        (Path(__file__).parents[1] / "benchmark/fixtures/information_preservation.json").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["gold_transport"] is False
    assert manifest["continuations"] == 108
    assert manifest["roles"] == ["user", "assistant", "tool"]


def test_measured_build_contract_loads_create_mode_and_structure(tmp_path):
    _install_measured_skill(tmp_path)

    contract = _load_build_instructions(tmp_path)

    assert contract["path"] == "skills/session-handoff/SKILL.md"
    assert contract["sha256"]
    assert contract["create_mode_sha256"]
    assert "Use exactly this document structure:" in contract["create_mode"]


def test_tool_evidence_preserves_call_data_but_strips_private_reasoning():
    evidence = _tool_item_evidence(
        {
            "type": "function_call",
            "name": "shell",
            "arguments": '{"command":"pytest"}',
            "output": "ok",
            "reasoning": "private",
        },
        source="item/completed",
    )

    assert evidence["name"] == "shell"
    assert evidence["arguments"] == '{"command":"pytest"}'
    assert evidence["output"] == "ok"
    assert "reasoning" not in evidence


def test_structured_fixture_rejects_duplicate_ids_and_invalid_roles():
    conversation = {
        "schema_version": 1,
        "case": "fixture",
        "band": "short",
        "checkpoint": 2,
        "messages": [
            {"id": "m1", "role": "user", "content": "one"},
            {"id": "m1", "role": "assistant", "content": "two"},
        ],
        "role_order": ["user", "assistant"],
    }

    with pytest.raises(StructuredConversationError, match="duplicate"):
        validate_structured_conversation(conversation)
    conversation["messages"][1]["id"] = "m2"
    conversation["messages"][1]["role"] = "gold"
    with pytest.raises(StructuredConversationError, match="role"):
        validate_structured_conversation(conversation)


def test_response_items_preserve_roles_and_do_not_flatten_history():
    conversation = build_structured_conversation("late-correction", "short", 1)
    items = conversation_to_response_items(conversation)

    assert len(items) == len(conversation["messages"])
    assert [item["type"] for item in items].count("message") >= 2
    assert any(item["role"] == "assistant" for item in items if item["type"] == "message")
    assert any(item["type"] == "function_call_output" for item in items)
    assert all("gold_facts" not in item for item in items)
    assert all("stale_traps" not in item for item in items)


def test_structured_tool_outputs_have_matching_function_calls():
    conversation = build_structured_conversation("compound-rot", "short", 1)
    items = conversation_to_response_items(conversation)

    calls = {item["call_id"] for item in items if item["type"] == "function_call"}
    outputs = {item["call_id"] for item in items if item["type"] == "function_call_output"}
    assert calls
    assert calls == outputs
    assert [item["type"] for item in items].count("function_call") == len(outputs)


def test_structured_anchor_positions_are_distributed_across_history():
    conversation = build_structured_conversation("late-correction", "short", 1)
    content = [message["content"] for message in conversation["messages"]]
    correction = content.index("Correction from the current API contract: /api/v1/export is obsolete. The supported endpoint is /api/v2/exports, plural. Use /api/v2/exports from this point onward.")
    assert correction > len(content) // 2
    assert any(message.get("role") == "tool" for message in conversation["messages"][:correction])


def test_plan_has_108_downstream_continuations_and_counts_extra_operations():
    plan = plan_benchmark()

    assert plan["arms"] == list(ARMS)
    assert plan["cases"] == list(CASES)
    assert plan["bands"] == list(BANDS)
    assert plan["replicates"] == 3
    assert plan["continuations"] == CELL_COUNT == 108
    assert plan["preparation_operations"] > 0
    assert plan["compaction_operations"] > 0
    assert plan["retrieval_probe_operations"] > 0
    assert plan["provider_calls"] is None
    assert plan["execution"] == "blocked"


def test_near_threshold_requires_verified_context_budget():
    with pytest.raises(BenchmarkGateError, match="context budget"):
        plan_benchmark(bands=("near_threshold",))

    plan = plan_benchmark(
        bands=("near_threshold",), verified_context_budget_tokens=100_000
    )
    assert plan["bands"] == ["near_threshold"]


def test_plan_rejects_duplicate_cells():
    with pytest.raises(BenchmarkGateError, match="duplicate"):
        plan_benchmark(cases=("buried-constraint", "buried-constraint"))


def test_dry_run_never_executes_provider_work():
    plan = plan_benchmark()
    result = NativeAppServer.dry_run(plan)

    assert result["execution"] == "blocked"
    assert result["provider_calls"] == 0
    assert result["gate"] == "harness-ready"


def test_dry_run_excludes_one_cell_but_keeps_full_plan_counts(capsys):
    failed_cell = next(
        cell
        for cell in build_cells(
            cases=("superseded-decision",), bands=("short",), replicates=1
        )
        if cell["arm"] == "main"
    )

    assert main(
        [
            "--dry-run",
            "--case",
            "superseded-decision",
            "--band",
            "short",
            "--replicate",
            "1",
            "--exclude-cell",
            failed_cell["cell_id"],
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["planned_continuations"] == 3
    assert result["excluded_continuations"] == 1
    assert result["remaining_continuations"] == 2
    assert result["excluded_cell_ids"] == [failed_cell["cell_id"]]


def test_dry_run_rejects_unknown_duplicate_and_all_excluded_cells(capsys):
    cells = build_cells(cases=("superseded-decision",), bands=("short",), replicates=1)
    unknown = "not-a-real-cell"
    assert main(["--dry-run", "--exclude-cell", unknown]) == 2
    assert "unknown excluded cell" in capsys.readouterr().err

    cell_id = cells[0]["cell_id"]
    assert main(["--dry-run", "--exclude-cell", cell_id, "--exclude-cell", cell_id]) == 2
    assert "duplicate excluded cell" in capsys.readouterr().err

    assert main(
        [
            "--dry-run",
            "--case",
            "superseded-decision",
            "--band",
            "short",
            "--replicate",
            "1",
            *sum((["--exclude-cell", cell["cell_id"]] for cell in cells), []),
        ]
    ) == 2
    assert "all selected cells are excluded" in capsys.readouterr().err


def test_execution_requires_explicit_cost_acknowledgement():
    plan = plan_benchmark()
    with pytest.raises(BenchmarkGateError, match="acknowledgement"):
        NativeAppServer.require_execution(plan, execute=True, acknowledge_provider_cost=False)


def test_execution_requires_a_separate_runtime_gate():
    plan = plan_benchmark()
    with pytest.raises(BenchmarkGateError, match="runtime gate"):
        NativeAppServer.require_execution(
            plan,
            execute=True,
            acknowledge_provider_cost=True,
            allow_runtime=False,
        )


def test_schema_probe_does_not_claim_real_compaction(capsys, tmp_path):
    binary = tmp_path / "fake-codex"
    binary.write_text(
        """#!/usr/bin/env python3
import json, pathlib, sys
if sys.argv[1:] == ['--version']:
    print('codex-cli 0.153.4')
    raise SystemExit(0)
if sys.argv[1:4] == ['app-server', 'generate-json-schema', '--experimental']:
    out = pathlib.Path(sys.argv[sys.argv.index('--out') + 1])
    out.mkdir(parents=True, exist_ok=True)
    method_names = ['initialize','thread/start','thread/fork','thread/compact/start','thread/inject_items','thread/read','thread/items/list','turn/start']
    notification_names = ['item/completed','error']
    def schema(names):
        return {'oneOf': [{'properties': {'method': {'enum': [name]}}} for name in names]}
    (out / 'ClientRequest.json').write_text(json.dumps(schema(method_names)))
    (out / 'ServerNotification.json').write_text(json.dumps(schema(notification_names)))
    raise SystemExit(0)
raise SystemExit(1)
""",
        encoding="utf-8",
    )
    binary.chmod(0o700)

    assert main(["--verify-native", "--codex-binary", str(binary)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["gate"] == "native-schema-verified"


def test_offline_execution_runs_three_arms_and_writes_artifacts(tmp_path):
    product_roots = []
    for label in ("main", "candidate"):
        root = tmp_path / label
        (root / "server").mkdir(parents=True)
        _install_measured_skill(root)
        (root / "server/handoff_mcp.py").write_text(
            """import json, os, pathlib, sys
request = json.loads(sys.stdin.readline())
args = request['params']['arguments']
state = pathlib.Path(os.environ['XDG_STATE_HOME']) / 'handoff'
if request['params']['name'] == 'handoff_create':
    state.write_text(args['content'])
    data = {'path': 'handoffs/pilot.md', 'ref': 'pilot', 'storage': 'workspace'}
else:
    data = {'content': state.read_text(), 'valid': True, 'missing_sections': []}
print(json.dumps({'jsonrpc':'2.0','id':1,'result':{'content':[{'type':'text','text':json.dumps(data)}]}}))
""",
            encoding="utf-8",
        )
        product_roots.append(root)

    native = tmp_path / "fake-codex"
    _fake_app_server(
        native,
        [
            {
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"type": "contextCompaction", "id": "compact-1"},
                },
            }
        ],
    )
    result = execute_benchmark(
        output=tmp_path / "out",
        native_binary=native,
        native_interface=_verified_interface(),
        main_root=product_roots[0],
        candidate_root=product_roots[1],
        cases=("superseded-decision",),
        bands=("short",),
        replicates=1,
        deadline=2,
        runtime_authorized=True,
        sandbox_executable=None,
    )
    assert result["gate"] == "benchmark-results"
    assert result["planned_continuations"] == 3
    assert result["executed_continuations"] == 3
    assert result["native_compactions"] == 1
    assert len(result["results"]) == 3
    candidate_row = next(row for row in result["results"] if row["arm"] == "candidate")
    assert candidate_row["handoff"]["storage"] == "central"
    assert "workspace_diff" in candidate_row
    assert (tmp_path / "out/plan.json").is_file()
    assert all(row["score"]["recoverability_probe_separate"] for row in result["results"])
    blind = result["results"][0]["blind_id"]
    judge = json.loads((tmp_path / "out/blinded" / blind / "judge.json").read_text())
    private = json.loads((tmp_path / "out/private/gold" / f"{blind}.json").read_text())
    assert "arm" not in judge
    assert all(fact["status"] is None for fact in judge["facts"])
    assert private["arm"] in ARMS
    assert (tmp_path / "out/private/blind-map.json").stat().st_mode & 0o077 == 0
    requests = [json.loads(line) for line in (native.parent / "protocol.log").read_text().splitlines()]
    assert sum(request["method"] == "thread/start" for request in requests) == 5
    assert sum(request["method"] == "thread/compact/start" for request in requests) == 1
    assert sum(request["method"] == "thread/fork" for request in requests) == 6
    history_injections = [
        request["params"]["items"]
        for request in requests
        if request["method"] == "thread/inject_items" and len(request["params"]["items"]) > 1
    ]
    assert len(history_injections) == 3
    assert len({json.dumps(items, sort_keys=True) for items in history_injections}) == 1
    assert all(row["hidden_evaluation"]["passed"] for row in result["results"])
    assert all(not row["verification"]["passed"] for row in result["results"])
    assert all(not row["score"]["task_success"] for row in result["results"])


def test_offline_execution_excludes_attempted_cell_and_runs_remaining_arms(tmp_path):
    product_roots = []
    for label in ("main", "candidate"):
        root = tmp_path / label
        (root / "server").mkdir(parents=True)
        _install_measured_skill(root)
        (root / "server/handoff_mcp.py").write_text(
            """import json, os, pathlib, sys
request = json.loads(sys.stdin.readline())
args = request['params']['arguments']
state = pathlib.Path(os.environ['XDG_STATE_HOME']) / 'handoff'
if request['params']['name'] == 'handoff_create':
    state.write_text(args['content'])
    data = {'path': 'handoffs/pilot.md', 'ref': 'pilot', 'storage': 'workspace'}
else:
    data = {'content': state.read_text(), 'valid': True, 'missing_sections': []}
print(json.dumps({'jsonrpc':'2.0','id':1,'result':{'content':[{'type':'text','text':json.dumps(data)}]}}))
""",
            encoding="utf-8",
        )
        product_roots.append(root)

    failed_cell = next(
        cell
        for cell in build_cells(
            cases=("superseded-decision",), bands=("short",), replicates=1
        )
        if cell["arm"] == "main"
    )
    native = tmp_path / "fake-codex"
    _fake_app_server(
        native,
        [
            {
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"type": "contextCompaction", "id": "compact-1"},
                },
            }
        ],
    )

    result = execute_benchmark(
        output=tmp_path / "out",
        native_binary=native,
        native_interface=_verified_interface(),
        main_root=product_roots[0],
        candidate_root=product_roots[1],
        cases=("superseded-decision",),
        bands=("short",),
        replicates=1,
        deadline=2,
        runtime_authorized=True,
        sandbox_executable=None,
        exclude_cell_ids=(failed_cell["cell_id"],),
    )

    assert result["planned_continuations"] == 3
    assert result["excluded_continuations"] == 1
    assert result["remaining_continuations"] == 2
    assert result["executed_continuations"] == 2
    assert result["executed_arm_counts"] == {"main": 0, "candidate": 1, "nativecompact": 1}
    assert result["gate"] == "benchmark-results"


def test_preparation_failure_does_not_count_as_downstream_attempt(tmp_path):
    product_roots = []
    for label in ("main", "candidate"):
        root = tmp_path / label
        (root / "server").mkdir(parents=True)
        _install_measured_skill(root)
        (root / "server/handoff_mcp.py").write_text("# preparation fails before MCP\n", encoding="utf-8")
        product_roots.append(root)
    native = tmp_path / "fake-codex"
    _fake_app_server(native, [], fail_method="thread/start")

    result = execute_benchmark(
        output=tmp_path / "out",
        native_binary=native,
        native_interface=_verified_interface(),
        main_root=product_roots[0],
        candidate_root=product_roots[1],
        cases=("superseded-decision",),
        bands=("short",),
        replicates=1,
        deadline=1,
        runtime_authorized=True,
        sandbox_executable=None,
    )

    assert result["rows_written"] == 1
    assert result["preparation_failures"] == 1
    assert result["attempted_continuations"] == 0
    assert result["executed_continuations"] == 0
    assert result["completed_continuations"] == 0


def test_cli_authorized_execution_dispatches_fake_protocol(tmp_path, capsys):
    products = []
    for label in ("main", "candidate"):
        root = tmp_path / label
        (root / "server").mkdir(parents=True)
        _install_measured_skill(root)
        (root / "server/handoff_mcp.py").write_text(
            """import json, os, pathlib, sys
request = json.loads(sys.stdin.readline())
args = request['params']['arguments']
state = pathlib.Path(os.environ['XDG_STATE_HOME']) / 'handoff'
if request['params']['name'] == 'handoff_create':
    state.write_text(args['content'])
    data = {'path': 'handoffs/pilot.md', 'ref': 'pilot', 'storage': 'workspace'}
else:
    data = {'content': state.read_text(), 'valid': True, 'missing_sections': []}
print(json.dumps({'jsonrpc':'2.0','id':1,'result':{'content':[{'type':'text','text':json.dumps(data)}]}}))
""",
            encoding="utf-8",
        )
        products.append(root)
    native = tmp_path / "fake-cli-codex"
    _fake_app_server(
        native,
        [{
            "jsonrpc": "2.0",
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"type": "contextCompaction", "id": "compact-1"},
            },
        }],
    )
    assert main([
        "--execute",
        "--acknowledge-provider-cost",
        "--allow-runtime",
        "--sandbox-executable",
        "",
        "--native-binary",
        str(native),
        "--main-root",
        str(products[0]),
        "--candidate-root",
        str(products[1]),
        "--case",
        "superseded-decision",
        "--band",
        "short",
        "--replicate",
        "1",
        "--output",
        str(tmp_path / "cli-out"),
        "--timeout",
        "2",
    ]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["gate"] == "benchmark-results"
    assert summary["executed_continuations"] == 3


def test_compaction_does_not_accept_ack_without_completion():
    events = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
    ]

    with pytest.raises(CompactionError, match="completion"):
        compact_completion(events, request_id=1, thread_id="thread-1")


def test_compaction_correlates_completion_thread_and_item_type():
    events = [
        {"jsonrpc": "2.0", "id": 9, "result": {}},
        {
            "jsonrpc": "2.0",
            "method": "item/completed",
            "params": {
                "threadId": "other-thread",
                "turnId": "turn-1",
                "item": {"type": "contextCompaction", "id": "item-1"},
            },
        },
    ]

    with pytest.raises(ProtocolError, match="thread"):
        compact_completion(events, request_id=9, thread_id="thread-1")


def test_compaction_failed_event_is_not_reported_as_success():
    events = [
        {"jsonrpc": "2.0", "id": 2, "result": {}},
        {
            "jsonrpc": "2.0",
            "method": "error",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "willRetry": False},
        },
    ]

    with pytest.raises(CompactionError, match="failed"):
        compact_completion(events, request_id=2, thread_id="thread-1")


def test_compaction_timeout_is_distinct_from_protocol_failure():
    with pytest.raises(CompactionTimeout):
        compact_completion([], request_id=3, thread_id="thread-1")


def test_interface_discovery_requires_all_native_methods(tmp_path):
    binary = tmp_path / "fake-codex"
    binary.write_text(
        """#!/usr/bin/env python3
import sys
if sys.argv[1:] == ['--version']:
    print('codex-cli 0.153.4')
    raise SystemExit(0)
raise SystemExit(1)
""",
        encoding="utf-8",
    )
    binary.chmod(0o700)

    with pytest.raises(BenchmarkGateError, match="schema"):
        discover_native_interface(binary)


def test_metadata_records_unknown_cost_and_pinned_hashes():
    metadata = NativeAppServer.metadata(
        client="codex",
        model="gpt-5.6-luna",
        reasoning_effort="max",
        prompt="continue",
        build="codex-cli 0.153.4",
    )

    assert metadata["client"] == "codex"
    assert metadata["model"] == "gpt-5.6-luna"
    assert metadata["reasoning_effort"] == "max"
    assert metadata["build"] == "codex-cli 0.153.4"
    assert metadata["prompt_sha256"]
    assert metadata["cost"] is None
    assert metadata["usage"] is None


def test_native_compact_requires_verified_interface(tmp_path):
    with pytest.raises(BenchmarkGateError, match="verified"):
        NativeAppServer(binary=tmp_path / "missing", interface=None)


def _fake_app_server(path: Path, event_lines: list[dict], *, fail_method: str | None = None) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
import pathlib
"""
        + f"EVENTS = {event_lines!r}\nFAIL_METHOD = {fail_method!r}\nTHREADS = 0\nFORKS = 0\n"
        + """
if sys.argv[1:] == ['--version']:
    print('codex-cli 0.153.4')
    raise SystemExit(0)
if sys.argv[1:4] == ['app-server', 'generate-json-schema', '--experimental']:
    output = pathlib.Path(sys.argv[sys.argv.index('--out') + 1])
    output.mkdir(parents=True, exist_ok=True)
    def schema(names):
        return {'oneOf': [{'properties': {'method': {'enum': [name]}}} for name in names]}
    (output / 'ClientRequest.json').write_text(json.dumps(schema(['initialize','thread/start','thread/fork','thread/compact/start','thread/inject_items','thread/read','thread/items/list','turn/start'])))
    (output / 'ServerNotification.json').write_text(json.dumps(schema(['item/completed','error'])))
    raise SystemExit(0)
for line in sys.stdin:
    request = json.loads(line)
    with open(os.path.join(os.path.dirname(sys.argv[0]), 'protocol.log'), 'a') as log:
        log.write(json.dumps(request, sort_keys=True) + '\\n')
    method = request.get('method')
    if method == FAIL_METHOD:
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'error':{'code':-32000,'message':'forced preparation failure'}}), flush=True)
        continue
    if method == 'initialize':
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{}}), flush=True)
    elif method == 'thread/start':
        THREADS += 1
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'thread':{'id':f'thread-{THREADS}'}}}), flush=True)
    elif method == 'thread/inject_items':
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{}}), flush=True)
    elif method == 'thread/compact/start':
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{}}), flush=True)
        for event in EVENTS:
            print(json.dumps(event), flush=True)
    elif method == 'turn/start':
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'turn':{'id':'turn-continue','status':'inProgress','items':[]}}}), flush=True)
        prompt = request['params'].get('input', [{}])[0].get('text', '')
        text = 'HANDOFF' if 'handoff' in prompt.lower() else 'completed'
        print(json.dumps({'jsonrpc':'2.0','method':'item/completed','params':{'threadId':request['params']['threadId'],'turnId':'turn-continue','item':{'type':'agentMessage','id':'message-continue','text':text}}}), flush=True)
        print(json.dumps({'jsonrpc':'2.0','method':'turn/completed','params':{'threadId':request['params']['threadId'],'turn':{'id':'turn-continue','status':'completed','items':[]}}}), flush=True)
    elif method == 'thread/fork':
        FORKS += 1
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'thread':{'id':f'fork-{FORKS}'}}}), flush=True)
    elif method == 'thread/read':
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'thread':{'id':request['params']['threadId']}}}), flush=True)
    elif method == 'thread/items/list':
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'data':[]}}), flush=True)

""",
        encoding="utf-8",
    )
    path.chmod(0o700)


def test_thread_start_uses_default_environment_access(tmp_path):
    binary = tmp_path / "fake-thread-start"
    _fake_app_server(binary, [])

    with NativeAppServer(binary=binary, interface=_verified_interface(), deadline=1) as server:
        assert server.start_thread() == "thread-1"

    requests = [json.loads(line) for line in (tmp_path / "protocol.log").read_text().splitlines()]
    start = next(request for request in requests if request["method"] == "thread/start")
    assert "environments" not in start["params"]


def _verified_interface() -> object:
    return type(
        "Interface",
        (),
        {
            "verified": True,
            "version": "codex-cli 0.153.4",
            "client_methods": frozenset(),
            "server_notifications": frozenset(),
            "schema_sha256": "schema",
        },
    )()


def _fake_turn_server(path: Path, *, completion_turn: dict, response_turn: dict | None = None) -> None:
    response_turn = response_turn or {"id": "turn-1", "status": "inProgress", "items": []}
    path.write_text(
        """#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize':
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{}}), flush=True)
    elif method == 'turn/start':
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'turn':RESPONSE}}), flush=True)
        print(json.dumps({'jsonrpc':'2.0','method':'turn/completed','params':{'threadId':request['params']['threadId'],'turn':COMPLETION}}), flush=True)
""".replace("RESPONSE", repr(response_turn)).replace("COMPLETION", repr(completion_turn)),
        encoding="utf-8",
    )
    path.chmod(0o700)


def test_turn_completion_must_match_ack_and_completed_status(tmp_path):
    binary = tmp_path / "fake-turn"
    _fake_turn_server(binary, completion_turn={"id": "turn-2", "status": "completed", "items": []})
    with NativeAppServer(binary=binary, interface=_verified_interface(), deadline=1) as server:
        with pytest.raises(CompactionTimeout):
            server.continue_thread("thread-1", "continue")

    binary = tmp_path / "fake-failed-turn"
    _fake_turn_server(binary, completion_turn={"id": "turn-1", "status": "failed", "items": [], "error": {}})
    with NativeAppServer(binary=binary, interface=_verified_interface(), deadline=1) as server:
        with pytest.raises(ProtocolError, match="status"):
            server.continue_thread("thread-1", "continue")


def _fake_interleaved_turn_server(path: Path, *, complete_target: bool = True) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize':
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{}}), flush=True)
    elif method == 'turn/start':
        thread_id = request['params']['threadId']
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'turn':{'id':'turn-target','status':'inProgress','items':[]}}}), flush=True)
        print(json.dumps({'jsonrpc':'2.0','method':'item/completed','params':{'threadId':'foreign-thread','turnId':'turn-old','item':{'type':'agentMessage','id':'foreign-item','text':'foreign-secret'}}}), flush=True)
        print(json.dumps({'jsonrpc':'2.0','method':'item/completed','params':{'threadId':thread_id,'turnId':'turn-old','item':{'type':'agentMessage','id':'same-thread-old-item','text':'wrong-turn-secret'}}}), flush=True)
        print(json.dumps({'jsonrpc':'2.0','method':'turn/completed','params':{'threadId':thread_id,'turn':{'id':'turn-old','status':'completed','items':[]}}}), flush=True)
        print(json.dumps({'jsonrpc':'2.0','method':'item/completed','params':{'threadId':thread_id,'turnId':'turn-target','item':{'type':'agentMessage','id':'target-item','text':'target-answer'}}}), flush=True)
        if COMPLETE_TARGET:
            print(json.dumps({'jsonrpc':'2.0','method':'turn/completed','params':{'threadId':thread_id,'turn':{'id':'turn-target','status':'completed','items':[]}}}), flush=True)
""".replace("COMPLETE_TARGET", repr(complete_target)),
        encoding="utf-8",
    )
    path.chmod(0o700)


def test_turn_wait_routes_interleaved_notifications_by_thread_and_turn(tmp_path):
    binary = tmp_path / "fake-interleaved-turn"
    _fake_interleaved_turn_server(binary)

    with NativeAppServer(binary=binary, interface=_verified_interface(), deadline=1) as server:
        result = server.continue_thread("thread-target", "continue")

    assert result["completed"] is True
    assert result["output"] == "target-answer"
    evidence = result["protocol_events"]
    assert [event["classification"] for event in evidence[:3]] == [
        "foreign",
        "foreign",
        "foreign",
    ]
    assert all("secret" not in json.dumps(event) for event in evidence)


def test_turn_wait_ignores_foreign_notifications_and_times_out_without_target(tmp_path):
    binary = tmp_path / "fake-foreign-only-turn"
    _fake_interleaved_turn_server(binary, complete_target=False)

    with NativeAppServer(binary=binary, interface=_verified_interface(), deadline=1) as server:
        with pytest.raises(CompactionTimeout) as raised:
            server.continue_thread("thread-target", "continue")

    evidence = getattr(raised.value, "protocol_events")
    assert len(evidence) == 4
    assert [event["classification"] for event in evidence] == [
        "foreign",
        "foreign",
        "foreign",
        "target",
    ]
    assert all("secret" not in json.dumps(event) for event in evidence)


def test_turn_wait_keeps_target_error_fatal_after_foreign_error(tmp_path):
    binary = tmp_path / "fake-error-turn"
    binary.write_text(
        """#!/usr/bin/env python3
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if request.get('method') == 'initialize':
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{}}), flush=True)
    elif request.get('method') == 'turn/start':
        thread_id = request['params']['threadId']
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'turn':{'id':'turn-target','status':'inProgress','items':[]}}}), flush=True)
        for event_thread in ('foreign-thread', thread_id):
            print(json.dumps({'jsonrpc':'2.0','method':'error','params':{'threadId':event_thread,'turnId':'turn-target','willRetry':False,'error':{'message':'sensitive-error'}}}), flush=True)
""",
        encoding="utf-8",
    )
    binary.chmod(0o700)

    with NativeAppServer(binary=binary, interface=_verified_interface(), deadline=1) as server:
        with pytest.raises(ProtocolError, match="failed") as raised:
            server.continue_thread("thread-target", "continue")

    evidence = getattr(raised.value, "protocol_events")
    assert [event["classification"] for event in evidence] == ["foreign", "target"]
    assert all("sensitive-error" not in json.dumps(event) for event in evidence)


def test_app_server_waits_for_correlated_compaction_completion(tmp_path):
    binary = tmp_path / "fake-codex"
    _fake_app_server(
        binary,
        [
            {
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"type": "contextCompaction", "id": "compact-1"},
                },
            }
        ],
    )
    with NativeAppServer(binary=binary, interface=_verified_interface(), deadline=1) as server:
        thread_id = server.start_thread()
        assert thread_id == "thread-1"
        result = server.compact_thread(thread_id)
    assert result["completion_event"] == "item/completed"
    assert result["item_id"] == "compact-1"


def test_app_server_ack_without_completion_times_out_and_closes(tmp_path):
    binary = tmp_path / "fake-codex"
    _fake_app_server(binary, [])
    server = NativeAppServer(binary=binary, interface=_verified_interface(), deadline=1)
    with server:
        thread_id = server.start_thread()
        with pytest.raises(CompactionTimeout):
            server.compact_thread(thread_id)
    assert server._process is None


def test_app_server_uses_isolated_home_for_lifecycle(tmp_path):
    marker = tmp_path / "home-marker"
    binary = tmp_path / "fake-codex"
    binary.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
path = os.path.join(os.path.dirname(sys.argv[0]), 'home-marker')
for line in sys.stdin:
    request = json.loads(line)
    if request.get('method') == 'initialize':
        open(path, 'w').write(os.environ.get('HOME', ''))
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{}}), flush=True)
""",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    server = NativeAppServer(binary=binary, interface=_verified_interface(), deadline=1)
    server.start()
    server.close()
    isolated_home = marker.read_text(encoding="utf-8")
    assert isolated_home
    assert isolated_home != str(Path.home())


def test_real_pinned_app_server_forks_injected_history_without_provider(tmp_path):
    binary = Path(
        "/home/cristina/.codex/packages/standalone/releases/0.153.4-x86_64-unknown-linux-musl/bin/codex"
    )
    if not binary.is_file() or shutil.which("bwrap") is None:
        pytest.skip("pinned Codex ELF and bubblewrap are required")
    interface = discover_native_interface(binary)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    conversation = build_structured_conversation("superseded-decision", "short", 1)

    with NativeAppServer(
        binary=binary,
        interface=interface,
        cwd=workspace,
        hidden_paths=(Path(__file__).parents[1],),
        sandbox_executable="bwrap",
        deadline=5,
    ) as server:
        source_thread = server.start_thread()
        server.inject_items(source_thread, conversation)
        fork_thread = server.fork_thread(source_thread)
        assert server.read_thread(fork_thread)["id"] == fork_thread
        fork_items = server.list_items(fork_thread)
        assert isinstance(fork_items, list)
        state_root = Path(server._state_temp.name)

        def rollout_records(thread_id: str) -> list[dict]:
            path = Path(server.read_thread(thread_id)["path"])
            host_path = state_root / path.relative_to("/mnt/native")
            return [json.loads(line) for line in host_path.read_text().splitlines()]

        source_records = rollout_records(source_thread)
        fork_records = rollout_records(fork_thread)
        assert any(
            record.get("type") == "response_item"
            and "cache/config.py" in json.dumps(record, sort_keys=True)
            for record in source_records
        )
        fork_meta = next(record for record in fork_records if record.get("type") == "session_meta")
        history_base = fork_meta["payload"]["history_base"]
        assert history_base["thread_id"] == source_thread
        assert history_base["end_ordinal_exclusive"] == len(source_records)


def test_cli_dry_run_is_reproducible_and_provider_free(capsys):
    assert main(["--dry-run"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["continuations"] == 108
    assert output["provider_calls"] == 0
    assert output["gate"] == "harness-ready"


def test_cli_dry_run_honors_explicit_matrix_selection(capsys):
    assert main(
        [
            "--dry-run",
            "--case",
            "superseded-decision",
            "--band",
            "short",
            "--replicate",
            "1",
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["continuations"] == 3
    assert output["gate"] == "harness-ready"


def test_cli_execute_without_cost_ack_is_rejected(capsys):
    assert main(["--execute"]) == 2
    assert "acknowledgement" in capsys.readouterr().err


def test_cli_native_schema_probe_stays_provider_free_when_unavailable(tmp_path, capsys):
    assert main(["--verify-native", "--codex-binary", str(tmp_path / "missing")]) == 2
    assert "unavailable" in capsys.readouterr().err


def test_plan_records_common_downstream_and_native_compact_contracts():
    plan = plan_benchmark()

    assert plan["native_compact_params"] == STANDARD_NATIVE_COMPACT_PARAMS
    assert plan["downstream_contract"] == COMMON_DOWNSTREAM_CONTRACT
    assert plan["model"] == "gpt-5.6-luna"
    assert plan["reasoning_effort"] == "xhigh"


def test_cells_are_unique_and_cover_all_three_arms():
    cells = build_cells()

    assert len(cells) == 108
    assert len({cell["cell_id"] for cell in cells}) == 108
    assert {cell["arm"] for cell in cells} == set(ARMS)
    assert all(cell["continuation_budget"] == COMMON_DOWNSTREAM_CONTRACT for cell in cells)


def test_cells_balance_arm_positions_across_logical_replicates():
    cells = build_cells()
    positions = {arm: [] for arm in ARMS}
    for cell in cells:
        positions[cell["arm"]].append(cell["arm_position"])
    assert all(len(values) == 36 for values in positions.values())
    assert all(set(values) == {1, 2, 3} for values in positions.values())


def test_real_handoff_create_read_uses_public_product_roundtrip(tmp_path):
    root = tmp_path / "product"
    server = root / "server"
    server.mkdir(parents=True)
    (server / "handoff_mcp.py").write_text(
        """import json, os, sys
request = json.loads(sys.stdin.readline())
args = request['params']['arguments']
cache = os.path.join(os.environ['XDG_STATE_HOME'], 'handoff')
if request['params']['name'] == 'handoff_create':
    open(cache, 'w').write(args['content'])
    data = {'path': 'handoffs/pilot.md', 'storage': 'workspace'}
else:
    data = {'content': open(cache).read(), 'valid': True, 'missing_sections': []}
print(json.dumps({'jsonrpc':'2.0','id':1,'result':{'content':[{'type':'text','text':json.dumps(data)}]}}))
""",
        encoding="utf-8",
    )
    content = "## Goal\n\nContinue.\n## Constraints & Preferences\n\n- Keep it small.\n## Progress\n\n### Done\n\n- None.\n### In Progress\n\n- Continue.\n### Pending\n\n- Verify.\n## Key Decisions\n\n- Use MCP.\n## Critical Context\n\n- Fixture.\n## Next Steps\n\n1. Verify.\n"

    result = create_read_handoff(
        root,
        storage="legacy",
        isolated_root=tmp_path / "isolated",
        workspace=tmp_path / "workspace",
        content=content,
    )
    assert result["content"] == content
    assert result["read_verified"] is True


def test_forked_recovery_probes_are_opaque_and_independent():
    class FakeAdapter:
        def __init__(self):
            self.forks = []

        def fork_thread(self, thread_id):
            fork = f"{thread_id}-fork-{len(self.forks)}"
            self.forks.append(fork)
            return fork

        def read_thread(self, thread_id):
            return {"id": thread_id}

        def list_items(self, thread_id):
            return [{"id": thread_id}]

    adapter = FakeAdapter()
    probes = fork_recovery_probes(adapter, "thread-1", ("active", "decision"))

    assert [probe["probe"] for probe in probes] == ["active", "decision"]
    assert len(adapter.forks) == 2
    assert len(set(probe["fork_thread_id"] for probe in probes)) == 2
    assert all(probe["opaque_state"] for probe in probes)
    assert all(probe["text_recall"] is None for probe in probes)


def test_recovery_probe_does_not_treat_completed_wrong_answer_as_retention():
    class FakeAdapter:
        def fork_thread(self, thread_id):
            return f"{thread_id}-fork"

        def read_thread(self, thread_id):
            return {"id": thread_id}

        def list_items(self, thread_id):
            return [{"id": thread_id}]

        def continue_thread(self, thread_id, prompt):
            return {"completed": True, "output": "I do not know."}

    probes = fork_recovery_probes(
        FakeAdapter(),
        "thread-1",
        ("facts",),
        gold_facts=[{"id": "F1", "critical": True, "statement": "TTL is 15 seconds."}],
    )
    assert probes[0]["model_queried"] is True
    assert probes[0]["recoverable"] is False
    assert probes[0]["semantic_score_status"] == "exact_match_proxy"


def test_recovery_probe_without_answer_is_unscored_not_false():
    class FakeAdapter:
        def fork_thread(self, thread_id):
            return f"{thread_id}-fork"

        def read_thread(self, thread_id):
            return {"id": thread_id}

        def list_items(self, thread_id):
            return []

        def continue_thread(self, thread_id, prompt):
            return {"completed": True, "output": ""}

    probes = fork_recovery_probes(
        FakeAdapter(),
        "thread-1",
        gold_facts=[{"id": "F1", "critical": True, "statement": "TTL is 15 seconds."}],
    )
    assert probes[0]["recoverable"] is None
    assert probes[0]["semantic_score_status"] == "unscored"


def test_native_checkpoint_records_actual_compaction_and_no_fallback():
    class FakeAdapter:
        def compact_thread(self, thread_id):
            return {
                "thread_id": thread_id,
                "turn_id": "turn-1",
                "item_id": "compact-1",
                "completion_event": "item/completed",
                "acknowledged": True,
                "opaque": True,
            }

    checkpoint = checkpoint_native(FakeAdapter(), "thread-1")

    assert checkpoint["actual_compaction"] is True
    assert checkpoint["truncated"] is None
    assert checkpoint["fallback"] is None
    assert checkpoint["checkpoint_id"]
    assert checkpoint["gate"] == "realcompact-verified"


def test_gate_states_do_not_confuse_harness_readiness_with_results():
    assert gate_state() == "harness-ready"
    assert gate_state(native_compact_verified=True) == "realcompact-verified"
    assert gate_state(native_compact_verified=True, benchmark_completed=True) == "benchmark-results"


def test_run_metadata_distinguishes_unknown_cost_from_recorded_usage():
    unknown = run_metadata(
        build="codex-cli 0.153.4",
        client="codex",
        model="gpt-5.6-luna",
        effort="max",
        prompt="continue",
    )
    measured = run_metadata(
        build="codex-cli 0.153.4",
        client="codex",
        model="gpt-5.6-luna",
        effort="max",
        prompt="continue",
        usage={"input_tokens": 10, "output_tokens": 3},
        cost=None,
    )

    assert unknown["cost"] is None
    assert unknown["usage"] is None
    assert measured["usage"]["input_tokens"] == 10
    assert measured["cost"] is None
    assert measured["build_sha256"] is None
    assert measured["build_hash_kind"] == "unavailable"
