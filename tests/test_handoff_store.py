from __future__ import annotations

import base64
import json
import multiprocessing
import os
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

from server import handoff_store as store


def _concurrent_create(args):
    workspace, data, state = args
    os.environ["XDG_DATA_HOME"] = data; os.environ["XDG_STATE_HOME"] = state
    return store.create_record(workspace, "same.md", "## Goal\ncentral\n")["ref"]


def _publish_with_failed_index(args):
    workspace, data, state = args
    os.environ["XDG_DATA_HOME"] = data; os.environ["XDG_STATE_HOME"] = state
    store._catalog = lambda: (_ for _ in ()).throw(store.HandoffStoreError("injected catalog failure"))
    return store.create_record(workspace, "child.md", "child")["ref"]


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


@pytest.mark.parametrize("version", [True, 1.0])
def test_cursor_version_requires_an_integer(version):
    generation = "a" * 32
    payload = {"v": version, "op": "list", "scope": "all", "project": None, "generation": generation, "sequence": 0}
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()

    with pytest.raises(store.HandoffStoreError, match="cursor"):
        store._decode_cursor(cursor, "list", "all", None, generation)


def test_cursor_sequence_must_fit_sqlite_integer():
    generation = "a" * 32
    payload = {"v": 1, "op": "list", "scope": "all", "project": None, "generation": generation, "sequence": 2**100}
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()

    with pytest.raises(store.HandoffStoreError, match="cursor"):
        store._decode_cursor(cursor, "list", "all", None, generation)


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


@pytest.mark.parametrize(
    ("document", "source_path"),
    [("## Goal\nBearer abcdefghijklmnop\n", "handoffs/x.md"), ("## Goal\nok\n", "API_TOKEN=supersecretvalue")],
)
def test_bundle_rejects_secrets_in_document_or_origin(tmp_path, monkeypatch, document, source_path):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data")); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    handoff_id = str(uuid.uuid4())
    manifest = {"schema_version":1,"handoff_id":handoff_id,"name":"x.md","created_at":"2026-01-01T00:00:00Z","sha256":__import__("hashlib").sha256(document.encode()).hexdigest(),"origin":{"project_id":str(uuid.uuid4()),"handoff_id":handoff_id,"kind":"legacy","source_path":source_path}}
    with pytest.raises(store.HandoffStoreError, match="secret"): store.import_bundle(str(workspace), "unused", document, manifest)


def test_durable_names_and_project_labels_do_not_persist_secrets(tmp_path, monkeypatch):
    workspace = tmp_path / "API_TOKEN=workspaceSecret"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data")); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    project_id = store.register_project(str(workspace))
    metadata = json.loads((store.data_root() / "projects" / project_id / "project.json").read_text())
    assert "workspaceSecret" not in metadata["label"]
    with pytest.raises(store.HandoffStoreError, match="secret"):
        store.create_record(str(workspace), "sk-ABCDEFGHIJKL", "doc")


def test_application_root_world_writable_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data")); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store.data_root().mkdir(parents=True, mode=0o777)
    with pytest.raises(store.HandoffStoreError, match="permissions"): store._mkdir(store.data_root())


def test_record_scan_budget_returns_a_continuation_cursor(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir(); monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data")); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state")); monkeypatch.setattr(store, "MAX_RECORDS_SCAN", 1)
    for index in range(3): store.create_record(str(workspace), f"{index}.md", "doc")
    first = store.list_records(str(workspace))
    second = store.list_records(str(workspace), cursor=first["next_cursor"])
    assert first["scan_truncated"] is False and first["has_more"] is True
    assert len(first["items"]) == len(second["items"]) == 1
    assert first["items"] != second["items"]


def test_record_scan_cursor_continues_across_projects(tmp_path, monkeypatch):
    first = tmp_path / "first"; second = tmp_path / "second"; first.mkdir(); second.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data")); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state")); monkeypatch.setattr(store, "MAX_RECORDS_SCAN", 1)
    records = [store.create_record(str(first), "x.md", "first"), store.create_record(str(second), "x.md", "second")]
    page = store.list_records(str(first), scope="all")
    following = store.list_records(str(first), scope="all", cursor=page["next_cursor"])
    assert {page["items"][0]["ref"], following["items"][0]["ref"]} == {record["ref"] for record in records}


def test_read_paths_reject_world_writable_data_root(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir(); monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data")); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    record = store.create_record(str(workspace), "x.md", "doc")
    store.data_root().chmod(0o777)
    with pytest.raises(store.HandoffStoreError, match="permissions"): store.read_record(record["ref"], str(workspace))
    with pytest.raises(store.HandoffStoreError, match="permissions"): store.list_records(str(workspace))


def test_read_rejects_public_record_and_bindings(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data")); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    record = store.create_record(str(workspace), "x.md", "doc")
    record_dir = store.data_root() / "projects" / record["project_id"] / "handoffs" / record["handoff_id"]
    record_dir.chmod(0o777)
    with pytest.raises(store.HandoffStoreError, match="permissions"): store.read_record(record["ref"], str(workspace))
    record_dir.chmod(0o700)
    (store.state_root() / "bindings.json").chmod(0o666)
    with pytest.raises(store.HandoffStoreError, match="permissions"): store.lookup_project(str(workspace))


def test_list_is_sorted_and_skips_corrupt_records(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data")); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    records = [store.create_record(str(workspace), f"{index}.md", "doc") for index in range(3)]
    corrupt = store.data_root() / "projects" / records[1]["project_id"] / "handoffs" / records[1]["handoff_id"] / "manifest.json"
    manifest = json.loads(corrupt.read_text()); manifest["schema_version"] = 999; corrupt.write_text(json.dumps(manifest)); corrupt.chmod(0o600)
    result = store.list_records(str(workspace))
    refs = [item["ref"] for item in result["items"]]
    assert refs == [records[0]["ref"], records[2]["ref"]]
    assert records[1]["ref"] not in refs
    assert result["skipped_count"] == 1


def test_missing_record_does_not_leak_unbound_local_error(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    missing = tmp_path / "data/session-handoff/missing.json"

    with pytest.raises(FileNotFoundError):
        store._read(missing, 10)


def test_global_list_catalog_discovers_all_projects(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; other = tmp_path / "other"
    workspace.mkdir(); other.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    expected = {store.create_record(str(workspace), "a.md", "a")["ref"], store.create_record(str(other), "b.md", "b")["ref"]}

    result = store.list_records(str(workspace), scope="all")

    assert {item["ref"] for item in result["items"]} == expected
    assert result["scan_truncated"] is False
    assert result["total_count"] is None


def test_central_cursor_reaches_every_record_beyond_scan_cap(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(store, "MAX_RECORDS_SCAN", 1)
    expected = [
        store.create_record(str(workspace), f"{index}.md", f"doc {index}")["ref"]
        for index in range(5)
    ]

    cursor = None
    actual = []
    while True:
        page = store.list_records(str(workspace), limit=2, cursor=cursor)
        actual.extend(item["ref"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert actual == expected
    assert len(set(actual)) == len(expected)
    assert page["has_more"] is False
    assert page["scan_truncated"] is False


def test_central_cursor_is_bound_to_scope_and_project(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir(); second.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store.create_record(str(first), "a.md", "a")
    store.create_record(str(first), "b.md", "b")
    cursor = store.list_records(str(first), limit=1)["next_cursor"]

    with pytest.raises(store.HandoffStoreError, match="cursor"):
        store.list_records(str(first), scope="all", cursor=cursor)
    with pytest.raises(store.HandoffStoreError, match="cursor"):
        store.list_records(str(second), cursor=cursor)
    with pytest.raises(store.HandoffStoreError, match="cursor"):
        store.list_records(str(first), cursor="not-a-cursor")
    with pytest.raises(store.HandoffStoreError, match="offset"):
        store.list_records(str(first), offset=1)


def test_corrupt_catalog_is_rebuilt_and_invalidates_old_cursor(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    expected = [store.create_record(str(workspace), f"{index}.md", "doc")["ref"] for index in range(2)]
    cursor = store.list_records(str(workspace), limit=1)["next_cursor"]
    store.catalog_path().write_bytes(b"not sqlite")
    store.catalog_path().chmod(0o600)

    with pytest.raises(store.HandoffStoreError, match="cursor"):
        store.list_records(str(workspace), limit=1, cursor=cursor)
    rebuilt = store.list_records(str(workspace))

    assert {item["ref"] for item in rebuilt["items"]} == set(expected)


def test_non_text_catalog_generation_is_rebuilt(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    expected = store.create_record(str(workspace), "one.md", "doc")["ref"]
    connection = sqlite3.connect(store.catalog_path())
    connection.execute("UPDATE metadata SET generation = ?", (sqlite3.Binary(b"broken"),))
    connection.commit(); connection.close()

    result = store.list_records(str(workspace))
    assert [item["ref"] for item in result["items"]] == [expected]


def test_failed_publish_index_is_reconciled_without_restart(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    first = store.create_record(str(workspace), "first.md", "first")
    store.list_records(str(workspace))
    real_catalog = store._catalog
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1: raise store.HandoffStoreError("injected catalog failure")
        return real_catalog()

    monkeypatch.setattr(store, "_catalog", fail_once)
    second = store.create_record(str(workspace), "second.md", "second")

    result = store.list_records(str(workspace))
    assert {item["ref"] for item in result["items"]} == {first["ref"], second["ref"]}


def test_failed_publish_index_is_reconciled_across_processes(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    data, state = str(tmp_path / "data"), str(tmp_path / "state")
    monkeypatch.setenv("XDG_DATA_HOME", data); monkeypatch.setenv("XDG_STATE_HOME", state)
    first = store.create_record(str(workspace), "first.md", "first")
    store.list_records(str(workspace))
    with multiprocessing.get_context("fork").Pool(1) as pool:
        second_ref = pool.apply(_publish_with_failed_index, ((str(workspace), data, state),))
    third = store.create_record(str(workspace), "third.md", "third")

    result = store.list_records(str(workspace))
    assert {item["ref"] for item in result["items"]} == {first["ref"], second_ref, third["ref"]}


def test_catalog_has_project_sequence_index(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store.create_record(str(workspace), "one.md", "doc")

    connection = __import__("sqlite3").connect(store.catalog_path())
    indexes = {row[1] for row in connection.execute("PRAGMA index_list(records)")}
    connection.close()

    assert "records_project_sequence" in indexes


def test_late_catalog_corruption_rebuilds_before_listing(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    expected = store.create_record(str(workspace), "one.md", "doc")["ref"]
    real_catalog = store._catalog
    failed = False

    class LateCorruption:
        def __init__(self, connection): self.connection = connection
        def execute(self, sql, parameters=()):
            nonlocal failed
            if not failed and "ORDER BY sequence" in sql:
                failed = True
                raise sqlite3.DatabaseError("database disk image is malformed")
            return self.connection.execute(sql, parameters)
        def __getattr__(self, name): return getattr(self.connection, name)

    monkeypatch.setattr(store, "_catalog", lambda: LateCorruption(real_catalog()))

    result = store.list_records(str(workspace))
    assert [item["ref"] for item in result["items"]] == [expected]


@pytest.mark.parametrize("target_exists", [True, False])
def test_catalog_symlink_is_rejected(tmp_path, monkeypatch, target_exists):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store.register_project(str(workspace))
    store.state_root().mkdir(parents=True, exist_ok=True)
    target = tmp_path / "outside.sqlite3"
    if target_exists: target.write_bytes(b"")
    store.catalog_path().symlink_to(target)

    with pytest.raises(store.HandoffStoreError, match="catalog"):
        store.list_records(str(workspace))
