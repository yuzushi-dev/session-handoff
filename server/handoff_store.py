"""Private, immutable central storage for semantic handoffs."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    from .redaction import redact_secrets
except ImportError:
    from redaction import redact_secrets  # type: ignore[no-redef]

MAX_MANIFEST_BYTES = 16 * 1024
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_RECORDS_SCAN = 256
CATALOG_SCHEMA_VERSION = 1
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class HandoffStoreError(ValueError):
    pass


class HandoffBudgetExceeded(HandoffStoreError):
    pass


class HandoffRecordError(HandoffStoreError):
    def __init__(self, message: str, scanned_bytes: int):
        super().__init__(message)
        self.scanned_bytes = scanned_bytes


def _parent_fd(path: Path, create: bool = False) -> tuple[int, str]:
    parts = path.absolute().parts
    fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[1:-1]:
            try: child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create: raise
                try: os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError: pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        return fd, parts[-1]
    except Exception:
        os.close(fd); raise


def _base(env: str, fallback: Path) -> Path:
    raw = os.environ.get(env, "")
    value = Path(raw) if raw and Path(raw).is_absolute() else fallback
    return value / "session-handoff"


def data_root() -> Path:
    return _base("XDG_DATA_HOME", Path.home() / ".local" / "share")


def state_root() -> Path:
    return _base("XDG_STATE_HOME", Path.home() / ".local" / "state")


def catalog_path() -> Path:
    return state_root() / "catalog.sqlite3"


def _catalog_dirty_path() -> Path:
    return state_root() / "catalog.dirty"


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
    if redact_secrets(name)[0] != name: raise HandoffStoreError("name contains secrets")
    return name


def _validate_manifest(manifest: dict[str, Any], document: str) -> None:
    if set(manifest) != {"schema_version", "handoff_id", "name", "created_at", "sha256", "origin"} or manifest.get("schema_version") != 1: raise HandoffStoreError("invalid manifest schema")
    if not isinstance(manifest.get("handoff_id"), str) or not _UUID.fullmatch(manifest["handoff_id"]): raise HandoffStoreError("invalid manifest handoff_id")
    validate_name(manifest.get("name"))
    if redact_secrets(manifest["name"])[0] != manifest["name"]: raise HandoffStoreError("portable manifest contains secrets")
    if not isinstance(manifest.get("created_at"), str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", manifest["created_at"]): raise HandoffStoreError("invalid manifest created_at")
    try: datetime.strptime(manifest["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc: raise HandoffStoreError("invalid manifest created_at") from exc
    if not isinstance(manifest.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]) or manifest["sha256"] != hashlib.sha256(document.encode()).hexdigest(): raise HandoffStoreError("invalid manifest sha256")
    origin = manifest.get("origin")
    if not isinstance(origin, dict) or set(origin) - {"project_id", "handoff_id", "kind", "source_path"} or not _UUID.fullmatch(str(origin.get("project_id"))) or not _UUID.fullmatch(str(origin.get("handoff_id"))) or origin.get("kind") not in {"create", "legacy"}: raise HandoffStoreError("invalid manifest origin")
    if "source_path" in origin and (not isinstance(origin["source_path"], str) or not origin["source_path"] or len(origin["source_path"].encode()) > 512 or Path(origin["source_path"]).is_absolute() or ".." in Path(origin["source_path"]).parts): raise HandoffStoreError("invalid manifest source_path")
    if redact_secrets(document)[0] != document: raise HandoffStoreError("portable document contains secrets")
    origin_json = json.dumps(origin, ensure_ascii=False, sort_keys=True)
    if redact_secrets(origin_json)[0] != origin_json: raise HandoffStoreError("portable manifest contains secrets")
    if manifest["origin"].get("handoff_id") != manifest["handoff_id"]: raise HandoffStoreError("invalid manifest origin")


def _mkdir(path: Path) -> None:
    parent, name = _parent_fd(path, create=True)
    try:
        try: os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError: pass
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            st = os.fstat(fd)
            if st.st_uid != os.geteuid() or st.st_mode & 0o077: raise HandoffStoreError("unsafe central directory permissions")
        finally: os.close(fd)
    finally: os.close(parent)


def _check_private_dir(path: Path) -> None:
    parent, name = _parent_fd(path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            st = os.fstat(fd)
            if st.st_uid != os.geteuid() or st.st_mode & 0o077: raise HandoffStoreError("unsafe central directory permissions")
        finally: os.close(fd)
    finally: os.close(parent)


def _check_private_file(path: Path) -> None:
    parent, name = _parent_fd(path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent); st = os.fstat(fd); os.close(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077: raise HandoffStoreError("unsafe central file permissions")
    finally: os.close(parent)


def _private_file_size(path: Path) -> int:
    parent, name = _parent_fd(path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            value = os.fstat(fd)
            if not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid() or value.st_mode & 0o077: raise HandoffStoreError("unsafe central file permissions")
            return value.st_size
        finally: os.close(fd)
    finally: os.close(parent)


def _read(path: Path, limit: int) -> bytes:
    parent, name = _parent_fd(path)
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise HandoffStoreError("central record must be a regular file")
        chunks = []; remaining = limit + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk: break
            chunks.append(chunk); remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        if fd is not None:
            try: os.close(fd)
            except OSError: pass
        os.close(parent)
    if len(data) > limit:
        raise HandoffStoreError("central record exceeds size limit")
    return data


def _json(path: Path, limit: int = MAX_MANIFEST_BYTES) -> dict[str, Any]:
    try: value = json.loads(_read(path, limit).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise HandoffStoreError("invalid central metadata") from exc
    if not isinstance(value, dict): raise HandoffStoreError("invalid central metadata")
    return value


def _write(path: Path, payload: bytes) -> None:
    parent, name = _parent_fd(path, create=True)
    temporary = f".{name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600, dir_fd=parent)
    try:
        view = memoryview(payload)
        while view: view = view[os.write(fd, view):]
        os.fsync(fd); os.close(fd)
        os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    except Exception:
        try: os.close(fd)
        except OSError: pass
        try: os.unlink(temporary, dir_fd=parent)
        except OSError: pass
        raise
    finally: os.close(parent)


def _bindings() -> dict[str, str]:
    path = state_root() / "bindings.json"
    if not path.exists(): return {}
    _check_private_dir(state_root()); _check_private_file(path)
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
    parent, name = _parent_fd(lock, create=True)
    fd = os.open(name, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
    os.close(parent)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        os.close(fd); raise HandoffStoreError("unsafe bindings lock")
    try: fcntl.flock(fd, fcntl.LOCK_EX); yield
    finally: fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


def _create_catalog(path: Path) -> sqlite3.Connection:
    parent, name = _parent_fd(path, create=True)
    try:
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
        os.close(fd)
    finally:
        os.close(parent)
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {CATALOG_SCHEMA_VERSION}")
    connection.execute("CREATE TABLE metadata (generation TEXT NOT NULL)")
    connection.execute("INSERT INTO metadata VALUES (?)", (secrets.token_hex(16),))
    connection.execute("CREATE TABLE records (sequence INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, handoff_id TEXT NOT NULL, UNIQUE(project_id, handoff_id))")
    connection.execute("CREATE INDEX records_project_sequence ON records(project_id, sequence)")
    connection.commit()
    return connection


def _open_catalog() -> sqlite3.Connection:
    root = state_root(); _mkdir(root); path = catalog_path()
    if not path.exists() and not path.is_symlink(): return _rebuild_catalog()
    try: _check_private_dir(root); _check_private_file(path)
    except OSError as exc: raise HandoffStoreError("unsafe catalog file") from exc
    connection = sqlite3.connect(path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()
        generation = connection.execute("SELECT generation FROM metadata").fetchone()
        connection.execute("SELECT sequence, project_id, handoff_id FROM records LIMIT 1").fetchone()
        if version != (CATALOG_SCHEMA_VERSION,) or not generation or not isinstance(generation[0], str) or not re.fullmatch(r"[0-9a-f]{32}", generation[0]):
            raise sqlite3.DatabaseError("unsupported catalog schema")
        connection.execute("CREATE INDEX IF NOT EXISTS records_project_sequence ON records(project_id, sequence)")
        connection.commit()
    except Exception:
        connection.close()
        raise
    return connection


def _catalog_generation(connection: sqlite3.Connection) -> str:
    return str(connection.execute("SELECT generation FROM metadata").fetchone()[0])


def _mark_catalog_dirty(project_id: str) -> None:
    _write(_catalog_dirty_path(), project_id.encode("ascii"))


def _dirty_catalog_project() -> str | None:
    path = _catalog_dirty_path()
    if not path.exists() and not path.is_symlink(): return None
    try: _check_private_file(path); project_id = _read(path, 36).decode("ascii")
    except (OSError, UnicodeDecodeError) as exc: raise HandoffStoreError("unsafe catalog recovery marker") from exc
    if not _UUID.fullmatch(project_id): raise HandoffStoreError("invalid catalog recovery marker")
    return project_id


def _clear_catalog_dirty() -> None:
    path = _catalog_dirty_path(); parent, name = _parent_fd(path)
    try:
        try: os.unlink(name, dir_fd=parent)
        except FileNotFoundError: return
        os.fsync(parent)
    finally: os.close(parent)


def _encode_cursor(operation: str, scope: str, project_id: str | None, generation: str, sequence: int, query_digest: str | None = None) -> str:
    value: dict[str, Any] = {"v": 1, "op": operation, "scope": scope, "project": project_id, "generation": generation, "sequence": sequence}
    if query_digest is not None: value["query"] = query_digest
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str, operation: str, scope: str, project_id: str | None, generation: str, query_digest: str | None = None) -> int:
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor): raise ValueError
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandoffStoreError("invalid cursor") from exc
    expected = {"v", "op", "scope", "project", "generation", "sequence"}
    if query_digest is not None: expected.add("query")
    if not isinstance(value, dict) or set(value) != expected or type(value.get("v")) is not int or value["v"] != 1 or value.get("op") != operation or value.get("scope") != scope or value.get("project") != project_id or value.get("generation") != generation or value.get("query") != query_digest or type(value.get("sequence")) is not int or not 0 <= value["sequence"] <= 2**63 - 1:
        raise HandoffStoreError("cursor does not match this request")
    return value["sequence"]


def _remove_staging(path: Path) -> None:
    parent, name = _parent_fd(path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            for child in ("document.md", "manifest.json"):
                try: os.unlink(child, dir_fd=fd)
                except FileNotFoundError: pass
        finally: os.close(fd)
        os.rmdir(name, dir_fd=parent)
    finally: os.close(parent)


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
    _check_private_dir(data_root()); _check_private_dir(data_root() / "projects")
    path = data_root() / "projects" / project_id / "project.json"
    _check_private_dir(path.parent)
    _check_private_file(path)
    value = _json(path)
    if value.get("schema_version") != 1 or value.get("project_id") != project_id or not isinstance(value.get("label"), str):
        raise HandoffStoreError("corrupt project metadata")
    if redact_secrets(value["label"])[0] != value["label"]: raise HandoffStoreError("project metadata contains secrets")
    return value


def register_project(workspace: str) -> str:
    anchor = anchor_for(workspace); existing = _bindings().get(anchor)
    if existing: _metadata(existing); return existing
    with _lock():
        bindings = _bindings(); existing = bindings.get(anchor)
        if existing: _metadata(existing); return existing
        _mkdir(data_root()); _mkdir(data_root() / "projects")
        project_id = str(uuid.uuid4()); project = data_root() / "projects" / project_id; _mkdir(project)
        _mkdir(project / "handoffs")
        label = redact_secrets(Path(workspace).resolve().name)[0][:128] or "project"
        _write(project / "project.json", json.dumps({"schema_version":1,"project_id":project_id,"label":label}, separators=(",", ":")).encode())
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
    if preserved_manifest is not None: _validate_manifest(manifest, document)
    project = data_root() / "projects" / project_id; _metadata(project_id); target = project / "handoffs" / handoff_id
    with _lock():
        dirty_project = _dirty_catalog_project()
        if dirty_project:
            connection = _catalog()
            try: _sync_catalog(connection, [dirty_project])
            finally: connection.close()
            _clear_catalog_dirty()
        if target.exists(): raise HandoffStoreError("central handoff already exists")
        _mark_catalog_dirty(project_id)
        _mkdir(target.parent); staging = target.parent / ("." + handoff_id + "." + secrets.token_hex(8) + ".staging"); _mkdir(staging)
        try:
            _write(staging / "document.md", raw); _write(staging / "manifest.json", json.dumps(manifest, separators=(",", ":")).encode())
            parent, target_name = _parent_fd(target); os.rename(staging.name, target_name, src_dir_fd=parent, dst_dir_fd=parent); os.fsync(parent); os.close(parent)
            connection = None
            try:
                connection = _catalog()
                connection.execute("INSERT OR IGNORE INTO records(project_id, handoff_id) VALUES (?, ?)", (project_id, handoff_id))
                connection.commit()
                _clear_catalog_dirty()
            except (HandoffStoreError, OSError, sqlite3.Error):
                pass
            finally:
                if connection is not None: connection.close()
        finally:
            try: _remove_staging(staging)
            except FileNotFoundError: pass
    return {"ref": make_ref(project_id, handoff_id), "project_id": project_id, "handoff_id": handoff_id, "name": name, "document": document, "manifest": manifest}


def read_record(ref: str, workspace: str, scope: str = "project", max_document_bytes: int | None = None) -> dict[str, Any]:
    project_id, handoff_id = parse_ref(ref); bound = lookup_project(workspace)
    if scope != "all" and bound != project_id: raise HandoffStoreError("handoff reference is outside workspace project")
    _metadata(project_id)
    record = data_root() / "projects" / project_id / "handoffs" / handoff_id
    if not record.is_dir(): raise HandoffStoreError("central handoff not found")
    _check_private_dir(record.parent); _check_private_dir(record); _check_private_file(record / "manifest.json"); _check_private_file(record / "document.md")
    manifest = _json(record / "manifest.json")
    if manifest.get("handoff_id") != handoff_id: raise HandoffStoreError("handoff manifest identity mismatch")
    document_size = _private_file_size(record / "document.md")
    if document_size > MAX_DOCUMENT_BYTES: raise HandoffStoreError("central record exceeds size limit")
    if max_document_bytes is not None and document_size > max_document_bytes: raise HandoffBudgetExceeded("central search byte budget reached")
    try:
        document = _read(record / "document.md", MAX_DOCUMENT_BYTES).decode("utf-8")
        _validate_manifest(manifest, document)
    except (HandoffStoreError, OSError, UnicodeDecodeError) as exc:
        raise HandoffRecordError(str(exc), document_size) from exc
    return {"ref": ref, "project_id": project_id, "handoff_id": handoff_id, "name": manifest.get("name"), "content": document, "manifest": manifest}


def _record_summary(project_id: str, handoff_id: str) -> dict[str, Any]:
    record = data_root() / "projects" / project_id / "handoffs" / handoff_id
    _check_private_dir(record.parent); _check_private_dir(record)
    _check_private_file(record / "manifest.json"); _check_private_file(record / "document.md")
    manifest = _json(record / "manifest.json")
    if manifest.get("handoff_id") != handoff_id: raise HandoffStoreError("handoff manifest identity mismatch")
    document = _read(record / "document.md", MAX_DOCUMENT_BYTES).decode("utf-8")
    _validate_manifest(manifest, document)
    return {"ref": make_ref(project_id, handoff_id), "project_id": project_id, "handoff_id": handoff_id, "name": manifest.get("name"), "created_at": manifest.get("created_at")}


def _project_ids() -> Iterator[str]:
    projects = data_root() / "projects"
    if not projects.exists(): return
    _check_private_dir(data_root()); _check_private_dir(projects)
    with os.scandir(projects) as entries:
        for entry in entries:
            if _UUID.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False): yield entry.name


def _sync_catalog(connection: sqlite3.Connection, projects: Iterable[str]) -> None:
    for project_id in projects:
        try: _metadata(project_id)
        except (HandoffStoreError, FileNotFoundError): continue
        handoffs = data_root() / "projects" / project_id / "handoffs"
        if not handoffs.is_dir(): continue
        _check_private_dir(handoffs)
        with os.scandir(handoffs) as entries:
            for entry in entries:
                handoff_id = entry.name
                if not _UUID.fullmatch(handoff_id) or not entry.is_dir(follow_symlinks=False) or connection.execute("SELECT 1 FROM records WHERE project_id = ? AND handoff_id = ?", (project_id, handoff_id)).fetchone(): continue
                connection.execute("INSERT OR IGNORE INTO records(project_id, handoff_id) VALUES (?, ?)", (project_id, handoff_id))
    connection.commit()


def _rebuild_catalog() -> sqlite3.Connection:
    root = state_root(); _mkdir(root); path = catalog_path()
    if path.exists(): _check_private_file(path)
    temporary = root / (".catalog." + secrets.token_hex(8) + ".tmp")
    connection = _create_catalog(temporary)
    try:
        _sync_catalog(connection, _project_ids())
        connection.close()
        parent, name = _parent_fd(path, create=True)
        try:
            os.rename(temporary.name, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally: os.close(parent)
    except Exception:
        connection.close()
        try: temporary.unlink()
        except FileNotFoundError: pass
        raise
    return _open_catalog()


def _catalog() -> sqlite3.Connection:
    try: return _open_catalog()
    except sqlite3.DatabaseError: return _rebuild_catalog()


def _catalog_page(connection: sqlite3.Connection, bound: str | None, scope: str, limit: int, cursor: str | None, cursor_operation: str, cursor_query: str | None, dirty_project: str | None) -> dict[str, Any]:
    generation = _catalog_generation(connection)
    if dirty_project and (scope == "all" or dirty_project == bound): _sync_catalog(connection, [dirty_project])
    sequence = _decode_cursor(cursor, cursor_operation, scope, bound if scope == "project" else None, generation, cursor_query) if cursor is not None else 0
    where = "project_id = ? AND sequence > ?" if scope == "project" else "sequence > ?"
    params: tuple[Any, ...] = (bound, sequence) if scope == "project" else (sequence,)
    items: list[dict[str, Any]] = []; skipped_count = 0; scanned = 0; last = sequence
    while len(items) < limit and scanned < MAX_RECORDS_SCAN:
        rows = connection.execute(f"SELECT sequence, project_id, handoff_id FROM records WHERE {where} ORDER BY sequence LIMIT ?", (*params[:-1], last, min(32, MAX_RECORDS_SCAN - scanned))).fetchall()
        if not rows: break
        for row_sequence, project_id, handoff_id in rows:
            last = row_sequence; scanned += 1
            try:
                item = _record_summary(project_id, handoff_id) if cursor_operation == "list" else {"ref": make_ref(project_id, handoff_id), "project_id": project_id, "handoff_id": handoff_id}
                if cursor_operation != "list": item["_cursor"] = _encode_cursor(cursor_operation, scope, bound if scope == "project" else None, generation, row_sequence, cursor_query)
                items.append(item)
            except (HandoffStoreError, OSError, UnicodeDecodeError): skipped_count += 1
            if len(items) >= limit or scanned >= MAX_RECORDS_SCAN: break
    more_params: tuple[Any, ...] = (bound, last) if scope == "project" else (last,)
    more = connection.execute(f"SELECT 1 FROM records WHERE {where} LIMIT 1", more_params).fetchone() is not None
    next_cursor = _encode_cursor(cursor_operation, scope, bound if scope == "project" else None, generation, last, cursor_query) if more else None
    total_count = 0 if cursor is None and not items and not skipped_count and not more else None
    return {"items": items, "count": len(items), "total_count": total_count, "offset": 0, "has_more": more, "next_offset": None, "next_cursor": next_cursor, "scan_truncated": False, "skipped_count": skipped_count}


def list_records(workspace: str, scope: str = "project", limit: int = 20, offset: int = 0, cursor: str | None = None, *, cursor_operation: str = "list", cursor_query: str | None = None) -> dict[str, Any]:
    if scope not in {"project", "all"}: raise HandoffStoreError("scope must be project or all")
    if offset: raise HandoffStoreError("offset is not supported for central storage; use cursor")
    bound = lookup_project(workspace)
    if not bound and scope != "all":
        if cursor is not None: raise HandoffStoreError("cursor does not match this request")
        return {"items": [], "count": 0, "total_count": 0, "offset": 0, "has_more": False, "next_offset": None, "next_cursor": None, "scan_truncated": False, "skipped_count": 0}
    if not (data_root() / "projects").exists():
        if cursor is not None: raise HandoffStoreError("cursor does not match this request")
        return {"items": [], "count": 0, "total_count": 0, "offset": 0, "has_more": False, "next_offset": None, "next_cursor": None, "scan_truncated": False, "skipped_count": 0}
    _check_private_dir(data_root()); _check_private_dir(data_root() / "projects")
    with _lock():
        dirty_project = _dirty_catalog_project()
        for attempt in range(2):
            connection = None
            try:
                connection = _catalog() if attempt == 0 else _rebuild_catalog()
                result = _catalog_page(connection, bound, scope, limit, cursor, cursor_operation, cursor_query, dirty_project)
                if dirty_project and (scope == "all" or dirty_project == bound): _clear_catalog_dirty()
                return result
            except sqlite3.DatabaseError as exc:
                if cursor is not None:
                    if connection is not None: connection.close(); connection = None
                    rebuilt = _rebuild_catalog(); rebuilt.close()
                    raise HandoffStoreError("cursor invalidated by catalog rebuild") from exc
                if attempt: raise HandoffStoreError("corrupt central catalog") from exc
            finally:
                if connection is not None: connection.close()
    raise HandoffStoreError("corrupt central catalog")


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
        parent, name = _parent_fd(destination); os.rename(staging.name, name, src_dir_fd=parent, dst_dir_fd=parent); os.fsync(parent); os.close(parent)
    finally:
        try: _remove_staging(staging)
        except FileNotFoundError: pass
    return {"directory": str(destination), "ref": ref, "idempotent": False}


def import_bundle(workspace: str, source: str, document: str | None = None, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    if manifest is None:
        manifest = _json(Path(source) / "manifest.json")
        document = _read(Path(source) / "document.md", MAX_DOCUMENT_BYTES).decode("utf-8")
    if not isinstance(document, str) or not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise HandoffStoreError("invalid handoff bundle")
    handoff_id = manifest.get("handoff_id"); name = manifest.get("name")
    if not isinstance(handoff_id, str) or not _UUID.fullmatch(handoff_id): raise HandoffStoreError("invalid handoff bundle identity")
    _validate_manifest(manifest, document)
    project_id = register_project(workspace)
    target = data_root() / "projects" / project_id / "handoffs" / handoff_id
    if target.exists():
        existing = read_record(make_ref(project_id, handoff_id), workspace)
        if existing["content"] == document and existing["manifest"] == manifest:
            return {"ref": existing["ref"], "project_id": project_id, "handoff_id": handoff_id, "name": name, "idempotent": True}
        raise HandoffStoreError("central handoff identity conflict")
    try:
        result = publish_record(project_id, handoff_id, name, document, manifest.get("origin", {}), manifest)
    except HandoffStoreError as exc:
        if "already exists" not in str(exc): raise
        existing = read_record(make_ref(project_id, handoff_id), workspace)
        if existing["content"] != document or existing["manifest"] != manifest: raise HandoffStoreError("central handoff identity conflict")
        return {"ref": existing["ref"], "project_id": project_id, "handoff_id": handoff_id, "name": name, "idempotent": True}
    result["idempotent"] = False
    return result
