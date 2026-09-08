import json
import os
from pathlib import Path
import sys
import time

import pytest

from benchmark.efficiency import (
    EfficiencyError,
    MCPProtocolError,
    DEFAULT_SCENARIOS,
    SCENARIO_NAMES,
    aggregate_samples,
    classify_search_coverage,
    make_markdown,
    main,
    parse_sizes,
    parse_scenarios,
    plan_cells,
    _parser,
    _fresh_output,
    _prepare_legacy,
    PersistentMCP,
    _run,
    _run_operation,
    _ensure_advancing,
    _expected_search_matches,
    _validate_list,
    _validate_search,
    Fixture,
)
from benchmark.version_aware import Build


def test_make_markdown_is_valid_and_deterministic_at_requested_size():
    first = make_markdown(7, 8192)
    second = make_markdown(7, 8192)

    assert first == second
    assert len(first.encode("utf-8")) == 8192
    for heading in (
        "## Goal",
        "## Constraints & Preferences",
        "## Progress",
        "## Key Decisions",
        "## Critical Context",
        "## Next Steps",
    ):
        assert heading in first


def test_parse_sizes_rejects_invalid_and_duplicate_values():
    assert parse_sizes("10,1000,10000") == (10, 1000, 10000)

    for raw in ("", "0", "10,10", "ten", "-1,10"):
        with pytest.raises(EfficiencyError):
            parse_sizes(raw)


def test_search_walk_is_opt_in_and_cli_dry_run_accepts_it(tmp_path, capsys):
    assert "candidate_search_walk" in SCENARIO_NAMES
    assert "candidate_search_walk" not in DEFAULT_SCENARIOS
    assert "candidate_central_list" in DEFAULT_SCENARIOS
    assert parse_scenarios("candidate_search_walk") == ("candidate_search_walk",)
    assert _parser().parse_args(["--output", str(tmp_path / "default-output")]).scenarios == ",".join(DEFAULT_SCENARIOS)

    for root in (tmp_path / "main", tmp_path / "candidate"):
        for relative in (
            "package.json",
            "plugin.json",
            ".claude-plugin/plugin.json",
            ".codex-plugin/plugin.json",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"name":"session-handoff","version":"0.7.0"}', encoding="utf-8")

    output = tmp_path / "dry-run-output"
    assert main(
        [
            "--main-root",
            str(tmp_path / "main"),
            "--candidate-root",
            str(tmp_path / "candidate"),
            "--output",
            str(output),
            "--sizes",
            "1",
            "--samples",
            "1",
            "--scenarios",
            "candidate_search_walk",
        ]
    ) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["scenarios"] == ["candidate_search_walk"]
    assert plan["cells"][0]["operation"] == "search_full_walk"


def test_plan_cells_is_explicit_and_does_not_cartesian_explode():
    cells = plan_cells((10,), ("main", "candidate"), ("shared_legacy",))

    assert {cell["build"] for cell in cells} == {"main", "candidate"}
    assert all(cell["size"] == 10 for cell in cells)
    assert {cell["operation"] for cell in cells} == {
        "create",
        "read",
        "list_first_page",
    }
    assert all("provider" not in cell for cell in cells)


def test_search_coverage_does_not_call_skipped_beyond_budget_complete():
    coverage = classify_search_coverage(
        {
            "scanned_files": 256,
            "scanned_bytes": 256 * 8192,
            "scan_truncated": True,
            "has_more": True,
            "skipped_count": 0,
            "items": [],
        },
        query_kind="only_beyond_256",
        target_index=256,
    )

    assert coverage["status"] == "truncated"
    assert coverage["target_covered"] is False
    assert coverage["complete"] is False

    late_page = classify_search_coverage(
        {
            "scanned_files": 256,
            "scanned_bytes": 256 * 8192,
            "scan_truncated": False,
            "has_more": False,
            "skipped_count": 0,
            "items": [],
        },
        query_kind="only_beyond_256",
        target_index=256,
        page_start=512,
    )

    assert late_page["target_covered"] is False


def test_aggregate_samples_reports_median_range_p95_and_failures():
    samples = [
        {"cell_key": "x", "status": "ok", "metrics": {"latency_ns": value}}
        for value in (1, 2, 3, 4)
    ]
    samples.append({"cell_key": "x", "status": "failed", "error": "timeout"})

    result = aggregate_samples(samples)

    stats = result["x"]["metrics"]["latency_ns"]
    assert stats == {
        "median": 2.5,
        "range": {"min": 1, "max": 4},
        "p95": 3.85,
        "samples": 4,
    }
    assert result["x"]["attempted"] == 5
    assert result["x"]["failures"] == 1


def test_each_fixture_sample_starts_with_the_requested_record_count(tmp_path):
    first = _prepare_legacy(tmp_path / "first", 3, 8192)
    second = _prepare_legacy(tmp_path / "second", 3, 8192)

    assert len(first.documents) == len(second.documents) == 3
    assert sorted(first.documents) == sorted(second.documents)


def test_existing_output_is_rejected_without_overwrite(tmp_path):
    output = tmp_path / "results"
    output.mkdir()
    with pytest.raises(EfficiencyError, match="already exists"):
        _fresh_output(output)


def test_list_validation_rejects_duplicates_and_nonadvancing_cursor():
    with pytest.raises(MCPProtocolError, match="duplicate"):
        _validate_list(
            {"items": [{"ref": "r"}, {"ref": "r"}], "count": 2, "has_more": False, "next_cursor": None},
            "central",
            set(),
        )
    with pytest.raises(MCPProtocolError, match="cursor"):
        _ensure_advancing("c", "c", "central")


def test_search_validation_checks_exact_match_lines_and_snippets():
    content = "prefix\nCommon-token in the line\nsuffix\n"
    data = {
        "query": "common-token",
        "items": [{"path": "handoffs/a.md", "matches": []}],
        "count": 1,
        "has_more": False,
        "next_offset": None,
        "next_cursor": None,
        "skipped_count": 0,
        "scanned_files": 1,
        "scanned_bytes": len(content.encode()),
        "scan_truncated": False,
        "output_truncated": False,
    }

    with pytest.raises(MCPProtocolError, match="matches"):
        _validate_search(
            data,
            "common",
            1,
            ["handoffs/a.md"],
            "legacy",
            len(content.encode()),
            expected_documents={"handoffs/a.md": content},
        )


def test_search_validation_requires_advancing_cursor_and_output_budget():
    data = {
        "query": "only-beyond-256",
        "items": [],
        "count": 0,
        "has_more": True,
        "next_offset": None,
        "next_cursor": None,
        "skipped_count": 0,
        "scanned_files": 256,
        "scanned_bytes": 256 * 8192,
        "scan_truncated": True,
        "output_truncated": False,
    }

    with pytest.raises(MCPProtocolError, match="cursor"):
        _validate_search(data, "only_beyond_256", 257, storage="central")

    data.update(
        {
            "items": [{"ref": "r", "matches": [{"line": 1, "snippet": "x" * 70_000}]}],
            "count": 1,
            "has_more": False,
            "scan_truncated": False,
            "scanned_files": 1,
            "scanned_bytes": 8192,
        }
    )
    with pytest.raises(MCPProtocolError, match="output budget"):
        _validate_search(data, "only_beyond_256", 1, storage="central")


def test_search_validation_binds_target_to_scanned_page_range():
    target = "handoff://project/256"
    document = make_markdown(256, 8192)
    first_page = {
        "query": "only-beyond-256",
        "items": [{"ref": target, "matches": _expected_search_matches(document, "only-beyond-256")}],
        "count": 1,
        "has_more": True,
        "next_offset": None,
        "next_cursor": "cursor-1",
        "skipped_count": 0,
        "scanned_files": 256,
        "scanned_bytes": 256 * 8192,
        "scan_truncated": True,
        "output_truncated": False,
    }

    with pytest.raises(MCPProtocolError, match="page range"):
        _validate_search(
            first_page,
            "only_beyond_256",
            300,
            [target],
            "central",
            8192,
            expected_documents={target: document},
            page_start=0,
        )

    second_page = {
        **first_page,
        "items": [{"ref": target, "matches": _expected_search_matches(document, "only-beyond-256")}],
        "has_more": False,
        "next_cursor": None,
        "scanned_files": 44,
        "scanned_bytes": 44 * 8192,
        "scan_truncated": False,
    }
    coverage = _validate_search(
        second_page,
        "only_beyond_256",
        300,
        [target],
        "central",
        8192,
        expected_documents={target: document},
        page_start=256,
    )

    assert coverage["target_covered"] is True
    assert coverage["status"] == "complete"


def test_search_full_walk_finds_beyond_256_target_without_duplicates(tmp_path, monkeypatch):
    identities = [f"handoff://project/{index}" for index in range(257)]
    target = identities[-1]
    fixture = Fixture(
        workspace=tmp_path / "workspace",
        home=tmp_path / "home",
        data=tmp_path / "data",
        state=tmp_path / "state",
        config=tmp_path / "config",
        storage="central",
        documents={target: "only-beyond-256\n"},
        identities=identities,
        document_bytes=8192,
    )
    for path in (fixture.workspace, fixture.home, fixture.data, fixture.state, fixture.config):
        path.mkdir()
    calls = []

    class FakeClient:
        capabilities = {"handoff_search"}
        tool_arguments = {}
        startup_metrics = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def call(self, name, arguments):
            assert name == "handoff_search"
            calls.append(arguments)
            page = len(calls)
            common = {
                "query": "only-beyond-256",
                "count": 0,
                "next_offset": None,
                "skipped_count": 0,
                "output_truncated": False,
            }
            metrics = {
                "request_latency_ns": 1,
                "cpu_user_ns": 1,
                "cpu_system_ns": 1,
                "request_rss_delta_bytes": 0,
                "peak_process_rss_bytes": 1,
                "proc_io_rchar_bytes": 1,
                "proc_io_wchar_bytes": 1,
                "proc_io_read_bytes": 1,
                "proc_io_write_bytes": 1,
            }
            if page == 1:
                return {
                    **common,
                    "items": [],
                    "has_more": True,
                    "next_cursor": "cursor-1",
                    "scanned_files": 256,
                    "scanned_bytes": 256 * 8192,
                    "scan_truncated": True,
                }, metrics
            assert arguments["cursor"] == "cursor-1"
            return {
                **common,
                "items": [{"ref": target, "matches": [{"line": 1, "snippet": "only-beyond-256"}]}],
                "count": 1,
                "has_more": False,
                "next_cursor": None,
                "scanned_files": 1,
                "scanned_bytes": 8192,
                "scan_truncated": False,
            }, metrics

    monkeypatch.setattr("benchmark.efficiency.PersistentMCP", lambda **_: FakeClient())
    result = _run_operation(
        fixture,
        tmp_path,
        {"operation": "search_full_walk", "storage": "central", "size": 257, "query_kind": "only_beyond_256"},
        timeout=1,
        deadline=10,
    )

    assert len(calls) == 2
    assert result["details"]["search_pages"] == 2
    assert result["details"]["search_evidence"][-1]["evidence"] == [
        {"identity": target, "matches": [{"line": 1, "snippet": "only-beyond-256"}]}
    ]


def test_run_retains_tool_failure_and_reports_unsuccessful_execution(tmp_path):
    root = tmp_path / "product"
    (root / "server").mkdir(parents=True)
    _write_server(
        root / "server" / "handoff_mcp.py",
        """
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize':
        result = {'protocolVersion': '2024-11-05'}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'handoff_read', 'inputSchema': {'properties': {'workspace': {}, 'path': {}}}}]}
    elif method == 'tools/call':
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': {'isError': True, 'content': [{'type': 'text', 'text': 'synthetic failure'}]}}), flush=True)
        continue
    else:
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
""",
    )
    build = Build("candidate", root, {})
    output = tmp_path / "results"
    success = _run(
        {"candidate": build},
        [{"scenario": "test", "build": "candidate", "size": 1, "storage": "legacy", "operation": "read"}],
        output=output,
        samples=1,
        document_bytes=8192,
        timeout=1,
        deadline=10,
        disk_budget=16 * 1024 * 1024,
        plan={"notes": []},
    )

    row = json.loads((output / "raw.jsonl").read_text().splitlines()[0])
    summary = json.loads((output / "summary.json").read_text())
    assert success is False
    assert row["status"] == "failed"
    assert "synthetic failure" in row["error"]
    assert summary["failed"] == 1


def _write_server(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)


def test_persistent_client_validates_response_id_and_cleans_process_group(tmp_path):
    server = tmp_path / "server.py"
    _write_server(
        server,
        """
import json, os, sys, time
for line in sys.stdin:
    request = json.loads(line)
    if request.get('method') == 'initialize':
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': {}}), flush=True)
    if request.get('method') == 'ping':
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'] + 1, 'result': {}}), flush=True)
""",
    )
    from benchmark.efficiency import PersistentMCP

    client = PersistentMCP(
        root=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        server=server,
        timeout=1,
    )
    with pytest.raises(MCPProtocolError, match="response id"):
        client.start()
    client.close()
    assert client.process is None or client.process.poll() is not None


def test_persistent_client_timeout_kills_process_group(tmp_path):
    server = tmp_path / "server.py"
    _write_server(
        server,
        """
import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    time.sleep(2)
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': {}}), flush=True)
""",
    )
    from benchmark.efficiency import PersistentMCP

    client = PersistentMCP(
        root=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        server=server,
        timeout=0.05,
    )
    with pytest.raises(TimeoutError):
        client.start()
    client.close()
    assert client.process is None or client.process.poll() is not None


def test_persistent_client_closes_child_after_leader_exits(tmp_path):
    server = tmp_path / "server.py"
    child_pid = tmp_path / "child.pid"
    _write_server(
        server,
        """
import json, os, subprocess, sys
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
Path(os.environ['CHILD_PID']).write_text(str(child.pid))
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize':
        result = {'protocolVersion': '2024-11-05'}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': []}
    else:
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
    if method == 'tools/list':
        os._exit(0)
""",
    )
    env = {"PATH": os.environ.get("PATH", ""), "CHILD_PID": str(child_pid)}
    client = PersistentMCP(root=tmp_path, env=env, server=server, timeout=1)
    client.start()
    leader = client.process
    assert leader is not None
    if sys.platform != "linux":
        client.close()
        pytest.skip("requires Linux process-state inspection")

    def process_state(pid):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        except (FileNotFoundError, OSError):
            return None
        return fields[0]

    for _ in range(100):
        if process_state(leader.pid) == "Z":
            break
        time.sleep(0.01)
    assert process_state(leader.pid) == "Z"
    pid = int(child_pid.read_text())

    started = time.monotonic()
    client.close()
    elapsed = time.monotonic() - started
    assert client.process is None
    assert elapsed < 1.25
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("child process survived PersistentMCP.close")
