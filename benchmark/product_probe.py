#!/usr/bin/env python3
"""Assert product behavior in an isolated synthetic home."""

from __future__ import annotations

import inspect
import hashlib
import json
import subprocess
import sys
from pathlib import Path


CONTENT = """## Goal
Continue the synthetic task.

## Constraints & Preferences
- Offline only.

## Progress
- Ready.

## Key Decisions
- Use needle[1].* literally.

## Critical Context
- Synthetic fixture.

## Next Steps
1. Verify.
"""


def _emit(**values: object) -> int:
    print(json.dumps(values, sort_keys=True, separators=(",", ":")))
    return 0


def _require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _load(root: Path):
    sys.path.insert(0, str(root))
    from server import handoff_mcp  # type: ignore

    return handoff_mcp


def _legacy_create_read(root: Path, workspace: Path) -> int:
    mcp = _load(root)
    created = mcp._create(
        {"workspace": str(workspace), "path": "handoffs/legacy.md", "content": CONTENT}
    )
    read = mcp._read({"workspace": str(workspace), "path": created["path"]})
    _require(read["content"] == CONTENT, "legacy read changed content")
    return _emit(capability="supported", create=True, read=True)


def _literal_search(root: Path, workspace: Path) -> int:
    mcp = _load(root)
    if not hasattr(mcp, "_search"):
        return _emit(capability="unsupported", reason="missing_literal_search")
    created = mcp._create(
        {"workspace": str(workspace), "path": "handoffs/legacy.md", "content": CONTENT}
    )
    found = mcp._search({"workspace": str(workspace), "query": "needle[1].*"})
    _require(found["count"] == 1, "literal legacy search did not match")
    _require(found["items"][0]["path"] == created["path"], "legacy search path mismatch")
    return _emit(capability="supported", literal_search=True)


def _central_modules(root: Path):
    if not (root / "server/handoff_store.py").is_file():
        return None, None
    mcp = _load(root)
    from server import handoff_store  # type: ignore

    return mcp, handoff_store


def _central_roundtrip(root: Path, workspace: Path) -> int:
    mcp, _ = _central_modules(root)
    if mcp is None:
        return _emit(capability="unsupported", reason="missing_handoff_store")
    first = mcp._create({"workspace": str(workspace), "name": "same.md", "content": CONTENT})
    second_content = CONTENT.replace("Synthetic fixture.", "Second synthetic fixture.")
    second = mcp._create(
        {"workspace": str(workspace), "name": "same.md", "content": second_content}
    )
    _require(first["ref"] != second["ref"], "same-name creates reused identity")
    _require(mcp._read({"workspace": str(workspace), "ref": first["ref"]})["content"] == CONTENT, "first immutable record changed")
    _require(mcp._read({"workspace": str(workspace), "ref": second["ref"]})["content"] == second_content, "second record mismatch")
    _require(not (workspace / "handoffs").exists(), "central create wrote workspace handoffs")
    return _emit(capability="supported", distinct_refs=True, immutable=True, workspace_clean=True)


def _central_pagination_scope(root: Path, workspace: Path) -> int:
    mcp, store = _central_modules(root)
    if mcp is None:
        return _emit(capability="unsupported", reason="missing_handoff_store")
    foreign = workspace.parent / "foreign"
    foreign.mkdir()
    local_refs = [store.create_record(str(workspace), f"local-{index}.md", CONTENT)["ref"] for index in range(2)]
    foreign_ref = store.create_record(str(foreign), "foreign.md", CONTENT)["ref"]
    first = store.list_records(str(workspace), limit=1)
    second = store.list_records(str(workspace), limit=1, cursor=first["next_cursor"])
    _require(first["has_more"] and not second["has_more"], "project pagination flags invalid")
    _require({first["items"][0]["ref"], second["items"][0]["ref"]} == set(local_refs), "project pagination lost records")
    try:
        store.read_record(foreign_ref, str(workspace))
    except store.HandoffStoreError:
        pass
    else:
        raise RuntimeError("project scope exposed foreign record")
    _require(store.read_record(foreign_ref, str(workspace), "all")["ref"] == foreign_ref, "all scope failed")
    return _emit(capability="supported", pagination=True, project_scope=True, all_scope=True)


def _snapshot(home: Path) -> list[tuple[str, str, str]]:
    return sorted(
        (
            str(path.relative_to(home)),
            "file" if path.is_file() else "directory",
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "",
        )
        for path in home.rglob("*")
    )


def _cli_management(root: Path, workspace: Path) -> int:
    _, store = _central_modules(root)
    if store is None:
        return _emit(capability="unsupported", reason="missing_central_cli")
    record = store.create_record(str(workspace), "cli.md", CONTENT)
    before = _snapshot(Path.home())
    command = [sys.executable, str(root / "bin/session-handoff")]
    listed = subprocess.run([*command, "list", "--workspace", str(workspace), "--json"], text=True, capture_output=True, check=False)
    read = subprocess.run([*command, "read", "--workspace", str(workspace), "--ref", record["ref"], "--json"], text=True, capture_output=True, check=False)
    doctor = subprocess.run([*command, "doctor"], text=True, capture_output=True, check=False)
    _require(listed.returncode == 0 and json.loads(listed.stdout)["items"][0]["ref"] == record["ref"], "CLI list failed")
    _require(read.returncode == 0 and json.loads(read.stdout)["content"] == CONTENT, "CLI read failed")
    _require(doctor.returncode in {0, 1} and json.loads(doctor.stdout)["provider_calls"] == 0, "doctor failed")
    _require(_snapshot(Path.home()) == before, "read-only CLI command mutated isolated home")
    return _emit(capability="supported", list=True, read=True, doctor=True, nonmutating=True)


def _resume_draft(root: Path, workspace: Path) -> int:
    if not (root / "server/handoff_store.py").is_file():
        return _emit(capability="unsupported", reason="missing_central_resume")
    sys.path.insert(0, str(root))
    from server import session_switch  # type: ignore

    if "ref" not in inspect.signature(session_switch.handoff_prompt).parameters:
        return _emit(capability="unsupported", reason="missing_central_resume")
    ref = "handoff://11111111-1111-4111-8111-111111111111/22222222-2222-4222-8222-222222222222"
    prompt = session_switch.handoff_prompt(str(workspace), ref=ref)
    expected = f'Resume task: call handoff_read(workspace={json.dumps(str(workspace))}, ref={json.dumps(ref)}) and proceed with the next steps. Do not create a new handoff.'
    _require(prompt == expected and "\n" not in prompt, "central resume draft changed")
    return _emit(capability="supported", exact_draft=True)


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    scenario = sys.argv[2]
    workspace = Path.home() / "workspace"
    workspace.mkdir()
    probes = {
        "legacy_create_read": _legacy_create_read,
        "literal_search": _literal_search,
        "central_roundtrip": _central_roundtrip,
        "central_pagination_scope": _central_pagination_scope,
        "cli_management": _cli_management,
        "supervised_resume_draft": _resume_draft,
    }
    try:
        return probes[scenario](root, workspace)
    except Exception as exc:
        print(f"product probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
