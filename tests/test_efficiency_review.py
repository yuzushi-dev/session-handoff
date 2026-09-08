"""Independent checks of benchmark semantics, not product performance."""

from pathlib import Path

import pytest

from benchmark import efficiency as bench


def test_read_sample_is_one_known_document_not_a_full_archive(tmp_path, monkeypatch):
    fixture = bench._prepare_legacy(tmp_path, 3, 8192)
    calls = []

    class Client:
        capabilities = {"handoff_read"}
        startup_metrics = {}

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def call(self, name, arguments):
            calls.append((name, arguments))
            identity = arguments["path"]
            return {
                "path": identity,
                "content": fixture.documents[identity],
                "valid": True,
                "missing_sections": [],
            }, {"request_latency_ns": 1}

    monkeypatch.setattr(bench, "PersistentMCP", Client)
    result = bench._run_operation(
        fixture, Path(tmp_path),
        {"storage": "legacy", "operation": "read", "size": 3},
        timeout=1, deadline=1,
    )
    assert len(calls) == 1
    assert result["metrics"]["request_count"] == 1


def test_create_sample_adds_one_record_to_an_existing_archive(tmp_path, monkeypatch):
    fixture = bench._prepare_legacy(tmp_path, 3, 8192)
    calls = []

    class Client:
        capabilities = {"handoff_create", "handoff_read"}
        startup_metrics = {}

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def call(self, name, arguments):
            calls.append((name, arguments))
            if name == "handoff_create":
                assert arguments["path"] not in fixture.documents
                return {"path": arguments["path"]}, {"request_latency_ns": 1}
            return {
                "path": arguments["path"],
                "content": bench.make_markdown(3, 8192),
                "valid": True,
                "missing_sections": [],
            }, {"request_latency_ns": 1}

    monkeypatch.setattr(bench, "PersistentMCP", Client)
    result = bench._run_operation(
        fixture, Path(tmp_path),
        {"storage": "legacy", "operation": "create", "size": 3},
        timeout=1, deadline=1,
    )
    assert sum(name == "handoff_create" for name, _ in calls) == 1
    assert result["metrics"]["request_count"] == 1


def test_pilot_list_page_size_matches_the_protocol_plan(tmp_path):
    fixture = bench._prepare_legacy(tmp_path, 3, 8192)
    args = bench._list_arguments(
        fixture, "legacy", None, 0, supports_storage_argument=False,
    )
    assert args["limit"] == 20


def test_search_rejects_a_match_for_the_absent_query():
    result = {
        "query": "absent-token", "count": 1,
        "items": [{"path": "handoffs/record-00000.md", "matches": [{"line": 1, "snippet": "absent-token"}]}],
        "scanned_files": 3, "scanned_bytes": 3 * 8192,
        "scan_truncated": False, "has_more": False, "skipped_count": 0,
    }
    with pytest.raises(bench.MCPProtocolError):
        bench._validate_search(result, "absent", 3)


def test_list_rejects_empty_page_when_fixture_is_nonempty():
    with pytest.raises(bench.MCPProtocolError):
        bench._validate_list(
            {"items": [], "count": 0, "has_more": False, "next_offset": None},
            "legacy", set(), ["handoffs/record-00000.md"],
        )


def test_search_rejects_missing_common_matches():
    result = {
        "query": "common-token", "count": 0, "items": [],
        "scanned_files": 3, "scanned_bytes": 3 * 8192,
        "scan_truncated": False, "has_more": False, "skipped_count": 0,
    }
    with pytest.raises(bench.MCPProtocolError):
        bench._validate_search(result, "common", 3)


def test_fixture_cache_reuses_reads_but_restores_create_baseline(tmp_path):
    fixture = bench._prepare_legacy(tmp_path / "live", 3, 8192)
    cached = bench._snapshot_fixture(fixture, tmp_path / "snapshot")
    first = fixture.workspace / fixture.identities[0]
    before = first.stat().st_ino
    assert bench._restore_fixture(cached) is fixture
    assert first.stat().st_ino == before
    extra = fixture.workspace / "handoffs/record-00003.md"
    extra.write_text(bench.make_markdown(3, 8192))
    cached.dirty = True
    bench._restore_fixture(cached)
    assert not extra.exists()
    assert len(list((fixture.workspace / "handoffs").glob("*.md"))) == 3
    assert first.stat().st_ino == before
