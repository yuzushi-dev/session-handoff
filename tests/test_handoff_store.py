from __future__ import annotations

import json
import multiprocessing
import os
import uuid
from datetime import datetime, timezone

import pytest

from server import handoff_store as store


def _concurrent_create(args):
    workspace, data, state = args
    os.environ["XDG_DATA_HOME"] = data; os.environ["XDG_STATE_HOME"] = state
    return store.create_record(workspace, "same.md", "## Goal\ncentral\n")["ref"]


def test_store_root_uses_absolute_xdg_and_home_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert store.data_root() == tmp_path / "home/.local/share/session-handoff"
    assert store.state_root() == tmp_path / "home/.local/state/session-handoff"
    monkeypatch.setenv("XDG_DATA_HOME", "relative")
    monkeypatch.setenv("XDG_STATE_HOME", "")
    assert store.data_root() == tmp_path / "home/.local/share/session-handoff"
    assert store.state_root() == tmp_path / "home/.local/state/session-handoff"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert store.data_root() == tmp_path / "data/session-handoff"
    assert store.state_root() == tmp_path / "state/session-handoff"


def test_reference_parser_is_strict():
    project, handoff = str(uuid.uuid4()), str(uuid.uuid4())
    assert store.parse_ref(f"handoff://{project}/{handoff}") == (project, handoff)
    for value in ("/tmp/x", f"handoff://{project}/{handoff}.md", f"handoff://{project}/{handoff}?x=1", f"handoff://{project}/nope"):
        with pytest.raises(store.HandoffStoreError): store.parse_ref(value)


def test_create_and_read_record_is_immutable(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data")); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    document = "## Goal\ncentral\n"
    result = store.create_record(str(workspace), "next.md", document)
    read = store.read_record(result["ref"], str(workspace))
    assert read["content"] == document
    assert not (workspace / "handoffs").exists()
    with pytest.raises(store.HandoffStoreError): store.publish_record(result["project_id"], result["handoff_id"], "next.md", "changed", result["manifest"]["origin"])


def test_read_only_lookup_does_not_create_store(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data")); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert store.lookup_project(str(workspace)) is None
    assert not (tmp_path / "data").exists(); assert not (tmp_path / "state").exists()


def test_export_rejects_absolute_and_traversal(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data")); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    record = store.create_record(str(workspace), "next.md", "## Goal\ncentral\n")
    with pytest.raises(store.HandoffStoreError): store.export_record(record["ref"], str(workspace), str(tmp_path / "outside"))
    with pytest.raises(store.HandoffStoreError): store.export_record(record["ref"], str(workspace), "../outside")


def test_bundle_import_preserves_manifest(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir(); bundle = tmp_path / "bundle"; bundle.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data")); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    doc = "## Goal\ncentral\n"; hid = str(uuid.uuid4()); pid = str(uuid.uuid4())
    manifest = {"schema_version": 1, "handoff_id": hid, "name": "next.md", "created_at": "2020-01-01T00:00:00Z", "sha256": __import__("hashlib").sha256(doc.encode()).hexdigest(), "origin": {"project_id": pid, "handoff_id": hid, "kind": "create"}}
    (bundle / "document.md").write_text(doc); (bundle / "manifest.json").write_text(json.dumps(manifest))
    result = store.import_bundle(str(workspace), str(bundle))
    assert result["manifest"] == manifest


def test_concurrent_registration_and_names_share_project(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    data, state = str(tmp_path / "data"), str(tmp_path / "state")
    monkeypatch.setenv("XDG_DATA_HOME", data); monkeypatch.setenv("XDG_STATE_HOME", state)
    with multiprocessing.get_context("fork").Pool(4) as pool:
        refs = pool.map(_concurrent_create, [(str(workspace), data, state)] * 4)
    assert len(set(refs)) == 4
    assert len({ref.split("/")[2] for ref in refs}) == 1


def test_publication_failure_does_not_expose_partial_record(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data")); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    project = store.register_project(str(workspace)); real_rename = store.os.rename
    def fail_rename(src, dst, **kwargs):
        if ".staging" in str(src): raise OSError("injected publication failure")
        return real_rename(src, dst, **kwargs)
    monkeypatch.setattr(store.os, "rename", fail_rename)
    with pytest.raises(OSError): store.create_record(str(workspace), "next.md", "## Goal\ncentral\n")
    assert not list((tmp_path / "data" / "session-handoff" / "projects" / project / "handoffs").iterdir())


@pytest.mark.parametrize(
    "change",
    [
        {"extra": True},
        {"created_at": "not-a-timestamp"},
        {"origin": "not-an-object"},
    ],
)
def test_bundle_manifest_is_strictly_validated_before_registration(
    tmp_path, monkeypatch, change
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bundle = workspace / "bundle"
    bundle.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    document = "## Goal\ncentral\n"
    handoff_id = str(uuid.uuid4())
    manifest = {
        "schema_version": 1,
        "handoff_id": handoff_id,
        "name": "next.md",
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sha256": __import__("hashlib").sha256(document.encode()).hexdigest(),
        "origin": {
            "project_id": str(uuid.uuid4()),
            "handoff_id": handoff_id,
            "kind": "create",
        },
    }
    manifest.update(change)
    (bundle / "document.md").write_text(document, encoding="utf-8")
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(store.HandoffStoreError, match="manifest"):
        store.import_bundle(str(workspace), str(bundle))

    assert not (tmp_path / "state").exists()


def test_redacted_marker_is_accepted_but_raw_secret_rejected(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir(); bundle = workspace / "bundle"; bundle.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data")); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    hid = str(uuid.uuid4()); doc = "## Goal\nAPI_KEY=[REDACTED]\n"; manifest = {"schema_version":1,"handoff_id":hid,"name":"x.md","created_at":"2026-01-01T00:00:00Z","sha256":__import__("hashlib").sha256(doc.encode()).hexdigest(),"origin":{"project_id":str(uuid.uuid4()),"handoff_id":hid,"kind":"create"}}
    (bundle / "document.md").write_text(doc); (bundle / "manifest.json").write_text(json.dumps(manifest))
    assert store.import_bundle(str(workspace), str(bundle))["handoff_id"] == hid
    (bundle / "document.md").write_text(doc.replace("[REDACTED]", "rawsecret")); manifest["sha256"] = __import__("hashlib").sha256((bundle / "document.md").read_bytes()).hexdigest(); (bundle / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(store.HandoffStoreError, match="secret"): store.import_bundle(str(workspace), str(bundle))


def test_application_root_world_writable_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data")); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store.data_root().mkdir(parents=True, mode=0o777)
    with pytest.raises(store.HandoffStoreError, match="permissions"): store._mkdir(store.data_root())


def test_bounded_global_discovery_reports_truncation(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir(); monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data")); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state")); monkeypatch.setattr(store, "MAX_RECORDS_SCAN", 1)
    project = store.register_project(str(workspace))
    for _ in range(3): store.publish_record(project, str(uuid.uuid4()), "x.md", "doc", {"project_id": project, "handoff_id": str(uuid.uuid4()), "kind": "create"})
    result = store.list_records(str(workspace)); assert result["scan_truncated"] is True and result["total_count"] is None and len(result["items"]) <= 1


def test_missing_record_does_not_leak_unbound_local_error(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    missing = tmp_path / "data/session-handoff/missing.json"

    with pytest.raises(FileNotFoundError):
        store._read(missing, 10)


def test_global_list_reports_truncated_totals(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(store, "MAX_PROJECTS", 1, raising=False)
    projects = tmp_path / "data/session-handoff/projects"
    for _ in range(2):
        project = projects / str(uuid.uuid4())
        (project / "handoffs").mkdir(parents=True)

    result = store.list_records(str(workspace), scope="all")

    assert result["scan_truncated"] is True
    assert result["total_count"] is None
