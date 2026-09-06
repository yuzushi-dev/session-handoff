from __future__ import annotations

import json
import uuid

import pytest

from server import handoff_store as store


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
