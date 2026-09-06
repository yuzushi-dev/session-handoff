"""Private, immutable central storage for semantic handoffs."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import re
import secrets
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MAX_MANIFEST_BYTES = 16 * 1024
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class HandoffStoreError(ValueError):
    pass


def _base(env: str, fallback: Path) -> Path:
    raw = os.environ.get(env, "")
    value = Path(raw) if raw and Path(raw).is_absolute() else fallback
    return value / "session-handoff"


def data_root() -> Path:
    return _base("XDG_DATA_HOME", Path.home() / ".local" / "share")


def state_root() -> Path:
    return _base("XDG_STATE_HOME", Path.home() / ".local" / "state")


def parse_ref(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not re.fullmatch(r"handoff://[^/]+/[^/]+", value):
        raise HandoffStoreError("invalid handoff reference")
    project, handoff = value[len("handoff://"):].split("/")
    if not _UUID.fullmatch(project) or not _UUID.fullmatch(handoff):
        raise HandoffStoreError("invalid handoff reference")
    return project, handoff


def make_ref(project_id: str, handoff_id: str) -> str:
    parse_ref(f"handoff://{project_id}/{handoff_id}")
    return f"handoff://{project_id}/{handoff_id}"


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name.startswith("."):
        raise HandoffStoreError("name must contain only ASCII letters, digits, _, -, . and be at most 128 bytes")
    return name


def _mkdir(path: Path) -> None:
    current = Path(path.anchor or "/")
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        current /= part
        if current.exists():
            st = current.lstat()
            if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode): raise HandoffStoreError("unsafe central directory")
        else:
            current.mkdir(mode=0o700)


def _read(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise HandoffStoreError("central record must be a regular file")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        st = os.fstat(fd)
        if not __import__("stat").S_ISREG(st.st_mode):
            raise HandoffStoreError("central record must be a regular file")
        chunks = []; remaining = limit + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk: break
            chunks.append(chunk); remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        try: os.close(fd)
        except (UnboundLocalError, OSError): pass
    if len(data) > limit:
        raise HandoffStoreError("central record exceeds size limit")
    return data


def _json(path: Path, limit: int = MAX_MANIFEST_BYTES) -> dict[str, Any]:
    try: value = json.loads(_read(path, limit).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise HandoffStoreError("invalid central metadata") from exc
    if not isinstance(value, dict): raise HandoffStoreError("invalid central metadata")
    return value


def _write(path: Path, payload: bytes) -> None:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally: os.close(fd)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    try:
        parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(parent_fd); os.close(parent_fd)
    except OSError:
        pass


def _bindings() -> dict[str, str]:
    path = state_root() / "bindings.json"
    if not path.exists(): return {}
    value = _json(path)
    if value.get("schema_version") != 1 or not isinstance(value.get("bindings"), dict):
        raise HandoffStoreError("unsupported or corrupt bindings schema")
    result = value["bindings"]
    if any(not isinstance(k, str) or not isinstance(v, str) or not _UUID.fullmatch(v) for k,v in result.items()):
        raise HandoffStoreError("corrupt bindings")
    return result


@contextmanager
def _lock() -> Iterator[None]:
    root = state_root(); _mkdir(root)
    lock = root / "bindings.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX); yield
    finally: fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


def _save_bindings(bindings: dict[str, str]) -> None:
    root = state_root(); _mkdir(root)
    _write(root / "bindings.json", json.dumps({"schema_version": 1, "bindings": bindings}, separators=(",", ":")).encode())


def anchor_for(workspace: str) -> str:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir(): raise HandoffStoreError("workspace is not a directory")
    try:
        proc = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True, timeout=3, check=False)
    except (subprocess.TimeoutExpired, PermissionError, OSError) as exc:
        raise HandoffStoreError("unable to determine git workspace anchor") from exc
    if proc.returncode == 0:
        value = proc.stdout.strip()
        if value and "\n" not in value and Path(value).is_absolute(): return "git:" + str(Path(value).resolve())
        raise HandoffStoreError("malformed git workspace anchor")
    if proc.returncode == 128: return "directory:" + str(root)
    raise HandoffStoreError("unable to determine git workspace anchor")


def lookup_project(workspace: str) -> str | None:
    return _bindings().get(anchor_for(workspace))


def _metadata(project_id: str) -> dict[str, Any]:
    path = data_root() / "projects" / project_id / "project.json"
    value = _json(path)
    if value.get("schema_version") != 1 or value.get("project_id") != project_id or not isinstance(value.get("label"), str):
        raise HandoffStoreError("corrupt project metadata")
    return value


def register_project(workspace: str) -> str:
    anchor = anchor_for(workspace); existing = _bindings().get(anchor)
    if existing: _metadata(existing); return existing
    with _lock():
        bindings = _bindings(); existing = bindings.get(anchor)
        if existing: _metadata(existing); return existing
        project_id = str(uuid.uuid4()); project = data_root() / "projects" / project_id; _mkdir(project)
        _mkdir(project / "handoffs")
        _write(project / "project.json", json.dumps({"schema_version":1,"project_id":project_id,"label":Path(workspace).resolve().name}, separators=(",", ":")).encode())
        bindings[anchor] = project_id; _save_bindings(bindings)
        return project_id


def associate_project(workspace: str, project_id: str, replace: bool = False) -> str | None:
    if not _UUID.fullmatch(project_id): raise HandoffStoreError("project_id must be a canonical UUID")
    _metadata(project_id); anchor = anchor_for(workspace)
    with _lock():
        bindings = _bindings(); previous = bindings.get(anchor)
        if previous and previous != project_id and not replace: raise HandoffStoreError("binding exists; replace=true required")
        bindings[anchor] = project_id; _save_bindings(bindings); return previous


def create_record(workspace: str, name: str, document: str, origin: dict[str, Any] | None = None) -> dict[str, Any]:
    validate_name(name); project_id = register_project(workspace); handoff_id = str(uuid.uuid4())
    return publish_record(project_id, handoff_id, name, document, origin or {"project_id": project_id, "handoff_id": handoff_id, "kind": "create"})


def publish_record(project_id: str, handoff_id: str, name: str, document: str, origin: dict[str, Any], preserved_manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    if not _UUID.fullmatch(project_id) or not _UUID.fullmatch(handoff_id): raise HandoffStoreError("invalid UUID")
    validate_name(name); raw = document.encode("utf-8")
    if len(raw) > MAX_DOCUMENT_BYTES: raise HandoffStoreError("central document exceeds size limit")
    created = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    manifest = preserved_manifest if preserved_manifest is not None else {"schema_version":1,"handoff_id":handoff_id,"name":name,"created_at":created,"sha256":hashlib.sha256(raw).hexdigest(),"origin":origin}
    if manifest.get("handoff_id") != handoff_id or manifest.get("sha256") != hashlib.sha256(raw).hexdigest(): raise HandoffStoreError("invalid preserved manifest")
    project = data_root() / "projects" / project_id; _metadata(project_id); target = project / "handoffs" / handoff_id
    with _lock():
        if target.exists(): raise HandoffStoreError("central handoff already exists")
        _mkdir(target.parent); staging = target.parent / ("." + handoff_id + "." + secrets.token_hex(8) + ".staging"); _mkdir(staging)
        try:
            _write(staging / "document.md", raw); _write(staging / "manifest.json", json.dumps(manifest, separators=(",", ":")).encode()); os.replace(staging, target)
        finally:
            if staging.exists():
                for p in staging.iterdir(): p.unlink(missing_ok=True)
                staging.rmdir()
    return {"ref": make_ref(project_id, handoff_id), "project_id": project_id, "handoff_id": handoff_id, "name": name, "document": document, "manifest": manifest}


def read_record(ref: str, workspace: str, scope: str = "project") -> dict[str, Any]:
    project_id, handoff_id = parse_ref(ref); bound = lookup_project(workspace)
    if scope != "all" and bound != project_id: raise HandoffStoreError("handoff reference is outside workspace project")
    record = data_root() / "projects" / project_id / "handoffs" / handoff_id; manifest = _json(record / "manifest.json")
    if manifest.get("handoff_id") != handoff_id: raise HandoffStoreError("handoff manifest identity mismatch")
    document = _read(record / "document.md", MAX_DOCUMENT_BYTES).decode("utf-8")
    if hashlib.sha256(document.encode()).hexdigest() != manifest.get("sha256"): raise HandoffStoreError("handoff document hash mismatch")
    return {"ref": ref, "project_id": project_id, "handoff_id": handoff_id, "name": manifest.get("name"), "content": document, "manifest": manifest}


def list_records(workspace: str, scope: str = "project", limit: int = 20, offset: int = 0) -> dict[str, Any]:
    bound = lookup_project(workspace)
    if not bound and scope != "all":
        return {"items": [], "count": 0, "total_count": 0, "offset": offset, "has_more": False, "next_offset": None}
    projects = [bound] if bound and scope != "all" else []
    if not projects and scope == "all" and (data_root() / "projects").is_dir():
        projects = []
        for entry in sorted((data_root() / "projects").iterdir(), key=lambda p: p.name):
            if len(projects) >= 256: break
            if entry.is_dir() and _UUID.fullmatch(entry.name): projects.append(entry.name)
    items: list[dict[str, Any]] = []
    for project in sorted(projects):
        handoffs = data_root() / "projects" / project / "handoffs"
        if not handoffs.is_dir(): continue
        for entry in sorted(handoffs.iterdir(), key=lambda p: p.name):
            if not _UUID.fullmatch(entry.name) or not entry.is_dir(): continue
            try:
                manifest = _json(entry / "manifest.json")
                if manifest.get("handoff_id") != entry.name: continue
                items.append({"ref": make_ref(project, entry.name), "project_id": project, "handoff_id": entry.name, "name": manifest.get("name"), "created_at": manifest.get("created_at")})
            except HandoffStoreError: continue
    page = items[offset:offset + limit]
    more = offset + len(page) < len(items)
    return {"items": page, "count": len(page), "total_count": len(items), "offset": offset, "has_more": more, "next_offset": offset + len(page) if more else None}


def export_record(ref: str, workspace: str, directory: str) -> dict[str, Any]:
    record = read_record(ref, workspace)
    root = Path(workspace).expanduser().resolve()
    supplied = Path(directory).expanduser()
    if supplied.is_absolute(): raise HandoffStoreError("export directory must be workspace-relative")
    destination = root / supplied
    try: destination.relative_to(root)
    except ValueError as exc: raise HandoffStoreError("export directory must remain inside workspace") from exc
    if any(part == ".." for part in supplied.parts) or destination.is_symlink(): raise HandoffStoreError("export directory is unsafe")
    if not destination.parent.exists() or destination.parent.is_symlink(): raise HandoffStoreError("export directory parent is unsafe")
    if destination.exists():
        if not destination.is_dir() or {p.name for p in destination.iterdir()} != {"document.md", "manifest.json"}: raise HandoffStoreError("export destination conflicts")
        if _read(destination / "document.md", MAX_DOCUMENT_BYTES) != record["content"].encode() or _read(destination / "manifest.json", MAX_MANIFEST_BYTES) != json.dumps(record["manifest"], separators=(",", ":")).encode(): raise HandoffStoreError("export destination conflicts")
        return {"directory": str(destination), "ref": ref, "idempotent": True}
    staging = destination.parent / ("." + destination.name + ".staging." + secrets.token_hex(8)); _mkdir(staging)
    try:
        _write(staging / "document.md", record["content"].encode())
        _write(staging / "manifest.json", json.dumps(record["manifest"], separators=(",", ":")).encode())
        os.replace(staging, destination)
    finally:
        if staging.exists():
            for p in staging.iterdir(): p.unlink(missing_ok=True)
            staging.rmdir()
    return {"directory": str(destination), "ref": ref, "idempotent": False}


def import_bundle(workspace: str, source: str, document: str | None = None, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    if manifest is None:
        manifest = _json(Path(source) / "manifest.json")
        document = _read(Path(source) / "document.md", MAX_DOCUMENT_BYTES).decode("utf-8")
    if not isinstance(document, str) or not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise HandoffStoreError("invalid handoff bundle")
    handoff_id = manifest.get("handoff_id"); name = manifest.get("name"); project_id = register_project(workspace)
    if not isinstance(handoff_id, str) or not _UUID.fullmatch(handoff_id): raise HandoffStoreError("invalid handoff bundle identity")
    validate_name(name)
    if manifest.get("sha256") != hashlib.sha256(document.encode()).hexdigest(): raise HandoffStoreError("handoff document hash mismatch")
    target = data_root() / "projects" / project_id / "handoffs" / handoff_id
    if target.exists():
        existing = read_record(make_ref(project_id, handoff_id), workspace)
        if existing["content"] == document and existing["manifest"] == manifest:
            return {"ref": existing["ref"], "project_id": project_id, "handoff_id": handoff_id, "name": name, "idempotent": True}
        raise HandoffStoreError("central handoff identity conflict")
    result = publish_record(project_id, handoff_id, name, document, manifest.get("origin", {}), manifest)
    result["idempotent"] = False
    return result
