"""Privacy-safe, closed telemetry event schema."""

from datetime import date, datetime, timedelta, timezone
from contextlib import contextmanager
import errno
import gzip
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
from numbers import Real
from pathlib import Path
import urllib.request

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


EVENTS = frozenset({"operation_summary", "context_feedback"})
OPERATIONS = frozenset({"handoff", "migrate"})
CLIENTS = frozenset({"claude", "codex"})
RESULTS = frozenset({"success", "failure", "fallback"})
FAILURE_STAGES = frozenset(
    {"none", "validation", "control", "source_stop", "conversion", "target_resume", "source_resume", "unknown"}
)
DURATION_BUCKETS = frozenset({"lt_1s", "1_to_5s", "5_to_30s", "30_to_120s", "gte_120s"})
COUNT_BUCKETS = frozenset({"zero", "one", "2_to_5", "6_to_20", "gt_20"})
HANDOFF_BYTES_BUCKETS = frozenset({"lt_4k", "4_to_16k", "16_to_64k", "gte_64k"})
_LEASE_NAME = "telemetry-batch-lease.json"
_GENERATION_NAME = "telemetry-generation"
_LAST_SUMMARY_NAME = "last-operation-summary.json"
_STATE_TARGETS = ("telemetry-counters.json", "telemetry-queue.jsonl", _LAST_SUMMARY_NAME, _LEASE_NAME)
FEEDBACK_CATEGORIES = frozenset({"constraint", "decision", "path", "progress", "rejected_attempt", "other"})
FEEDBACK_SEVERITIES = frozenset({"recoverable", "blocked"})

MAX_QUEUE_ROWS = 256
MAX_QUEUE_BYTES = 256 * 1024
MAX_UPLOAD_ROWS = 32
MAX_UPLOAD_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_QUEUE_AGE_DAYS = 7
MAX_COUNTER_BYTES = 256 * 1024
MAX_CONFIG_BYTES = 16 * 1024
_COUNTERS_NAME = "telemetry-counters.json"
_QUEUE_NAME = "telemetry-queue.jsonl"
_TEMP_BASE_NAMES = (*_STATE_TARGETS, "telemetry.json")

DENYLIST = frozenset(
    {
        "transcript", "prompt", "handoff_text", "tool_trace", "command", "diff", "file_path", "path",
        "source_session_id", "target_session_id", "session_id", "installation_id", "device_id", "account_id",
        "hostname", "username", "ip_address", "user_agent", "locale", "repository_name", "model_name",
        "metadata", "exception", "exception_text", "stack_trace", "free_text", "credential", "credentials",
        "token", "tokens", "cookie", "cookies", "authorization", "authorization_header", "uuid",
    }
)

_COMMON_FIELDS = frozenset({"schema_version", "event", "day_utc", "plugin_version", "operation", "source_client", "target_client"})
_OPERATION_FIELDS = _COMMON_FIELDS | frozenset(
    {"result", "failure_stage", "duration_bucket", "handoff_bytes_bucket", "redaction_bucket", "dropped_events_bucket", "normalized_fields_bucket"}
)
_FEEDBACK_FIELDS = _COMMON_FIELDS | frozenset({"feedback_category", "feedback_severity"})
_VERSION = re.compile(r"[0-9]+\.[0-9]+\Z")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_CONFIG_THREAD_LOCK = threading.RLock()
_LOCK_TIMEOUT = 1.0
_LEASE_SECONDS = 30.0
_MARKER_HIGH_WATER = {}
_IN_FLIGHT_BATCHES = set()

CONFIG_PATH = Path(".config/session-handoff/telemetry.json")
STATE_PATH = Path(".local/state/session-handoff")
ENDPOINT = "https://telemetry.session-handoff.example/v1/logs"
DISCLOSURE = """Anonymous aggregate telemetry is opt-in.
Collected fields: schema_version, event, day_utc, plugin_version, operation, source_client, target_client, result, failure_stage, duration_bucket, handoff_bytes_bucket, redaction_bucket, dropped_events_bucket, normalized_fields_bucket, feedback_category, feedback_severity.
No transcript, prompt, handoff text, tool trace, command, diff, path, session ID, installation ID, device ID, account ID, hostname, username, IP address, model name, metadata, exception, credentials, token, cookie, or authorization data is collected.
Local aggregate data is retained for up to 7 days. The endpoint is https://telemetry.session-handoff.example/v1/logs.
Telemetry is an opt-in sample and may not represent all users. Aggregate rows cannot be attributed to or deleted for one contributor.
"""


class TelemetryConfigError(ValueError):
    """The local telemetry consent config is missing or invalid."""


def _home(home=None):
    return Path(os.path.realpath(Path.home() if home is None else home))


def config_path(home=None):
    return _home(home) / CONFIG_PATH


def disabled_config():
    return {"schema_version": 1, "enabled": False, "prompted_consent_version": 1}


def enabled_config(consented_at=None):
    if consented_at is None:
        consented_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": 1,
        "enabled": True,
        "prompted_consent_version": 1,
        "consent_version": 1,
        "consented_at": consented_at,
        "endpoint": ENDPOINT,
    }


def _validate_config(config):
    if not isinstance(config, dict):
        raise TelemetryConfigError("invalid telemetry config")
    if type(config.get("schema_version")) is not int or config["schema_version"] != 1:
        raise TelemetryConfigError("invalid telemetry config schema version")
    if type(config.get("enabled")) is not bool:
        raise TelemetryConfigError("invalid telemetry config enabled flag")
    if config["enabled"] is False:
        if type(config.get("prompted_consent_version")) is not int or config["prompted_consent_version"] != 1:
            raise TelemetryConfigError("invalid telemetry consent version")
        if config != disabled_config():
            raise TelemetryConfigError("invalid disabled telemetry config")
        return config
    if set(config) != {
        "schema_version", "enabled", "prompted_consent_version", "consent_version", "consented_at", "endpoint"
    }:
        raise TelemetryConfigError("invalid enabled telemetry config fields")
    if any(type(config[field]) is not int or config[field] != 1 for field in ("prompted_consent_version", "consent_version")):
        raise TelemetryConfigError("invalid telemetry consent version")
    if not isinstance(config["consented_at"], str):
        raise TelemetryConfigError("invalid telemetry consent timestamp")
    try:
        parsed = datetime.fromisoformat(config["consented_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise TelemetryConfigError("invalid telemetry consent timestamp") from exc
    if parsed.tzinfo != timezone.utc or parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != config["consented_at"]:
        raise TelemetryConfigError("invalid telemetry consent timestamp")
    if config["endpoint"] != ENDPOINT:
        raise TelemetryConfigError("invalid telemetry endpoint")
    return config


def _require_secure_filesystem():
    supported = getattr(os, "supports_dir_fd", ())
    missing = [
        name
        for name, function in (
            ("open(dir_fd)", getattr(os, "open", None)),
            ("mkdir(dir_fd)", getattr(os, "mkdir", None)),
            ("stat(dir_fd)", getattr(os, "stat", None)),
            ("unlink(dir_fd)", getattr(os, "unlink", None)),
            ("rename(dir_fd)", getattr(os, "rename", None)),
        )
        if not callable(function) or function not in supported
    ]
    if not hasattr(os, "O_NOFOLLOW"):
        missing.append("O_NOFOLLOW")
    if not hasattr(os, "O_DIRECTORY"):
        missing.append("O_DIRECTORY")
    if not callable(getattr(os, "fchmod", None)):
        missing.append("fchmod")
    if not callable(getattr(os, "fsync", None)):
        missing.append("fsync")
    if fcntl is None or not callable(getattr(fcntl, "flock", None)):
        missing.append("fcntl.flock")
    if missing:
        raise TelemetryConfigError(
            "secure telemetry filesystem primitives unavailable: " + ", ".join(missing)
        )


def _ensure_directory(descriptor, path):
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise TelemetryConfigError(f"telemetry path is not a directory: {path}")
    os.fchmod(descriptor, 0o700)
    if os.fstat(descriptor).st_mode & 0o777 != 0o700:
        raise TelemetryConfigError(f"telemetry directory is not private: {path}")


def _open_secure_directory(root, relative, *, create):
    _require_secure_filesystem()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root = Path(root)
    root_parts = root.parts[1:]
    current_fd = os.open(os.sep, flags)
    current_path = Path(os.sep)
    try:
        parts = (*root_parts, *relative.parts)
        for index, part in enumerate(parts):
            child_path = current_path / part
            child_fd = None
            try:
                child_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    os.close(current_fd)
                    return None
                try:
                    os.mkdir(part, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(part, flags, dir_fd=current_fd)
            try:
                if index >= len(root_parts):
                    _ensure_directory(child_fd, child_path)
                elif not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                    raise TelemetryConfigError(f"telemetry path is not a directory: {child_path}")
                os.close(current_fd)
                current_fd = child_fd
                child_fd = None
                current_path = child_path
            finally:
                if child_fd is not None:
                    os.close(child_fd)
        return current_path, current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_relative(directory_fd, directory, name, flags, mode=0o600):
    return os.open(name, flags, mode, dir_fd=directory_fd)


def _stat_relative(directory_fd, directory, name):
    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _unlink_relative(directory_fd, directory, name):
    return os.unlink(name, dir_fd=directory_fd)


@contextmanager
def _config_directory_lock(directory, directory_fd):
    if not _CONFIG_THREAD_LOCK.acquire(timeout=_LOCK_TIMEOUT):
        raise TelemetryConfigError("telemetry lock is unavailable")
    lock_fd = None
    locked = False
    try:
        try:
            lock_fd = _open_relative(
                directory_fd,
                directory,
                ".telemetry.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
            lock_info = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_info.st_mode):
                raise TelemetryConfigError("telemetry lock must be a regular file")
            if lock_info.st_nlink > 1:
                raise TelemetryConfigError("telemetry lock must not be hardlinked")
            os.fchmod(lock_fd, 0o600)
            deadline = time.monotonic() + _LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TelemetryConfigError("telemetry lock is unavailable") from exc
                    time.sleep(min(0.01, remaining))
        except TelemetryConfigError:
            raise
        except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
            raise TelemetryConfigError("telemetry lock is unavailable") from exc
        yield lock_fd
    finally:
        cleanup_error = None
        if locked:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
                cleanup_error = exc
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
                if cleanup_error is None:
                    cleanup_error = exc
                try:
                    os.close(lock_fd)
                except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError):
                    pass
        try:
            _CONFIG_THREAD_LOCK.release()
        except RuntimeError as exc:
            if cleanup_error is None:
                cleanup_error = exc
        if cleanup_error is not None:
            raise TelemetryConfigError("telemetry lock cleanup failed") from cleanup_error


def _lock_marker_key(info, scope=None):
    identity = (info.st_dev, info.st_ino)
    return (str(Path(scope).resolve()), *identity) if scope is not None else identity


def _lock_marker_version(lock_fd, scope=None):
    info = os.fstat(lock_fd)
    if not stat.S_ISREG(info.st_mode):
        raise TelemetryConfigError("telemetry lock must be a regular file")
    if info.st_nlink > 1:
        raise TelemetryConfigError("telemetry lock must not be hardlinked")
    os.lseek(lock_fd, 0, os.SEEK_SET)
    raw = os.read(lock_fd, 64)
    if info.st_size > 64:
        raise TelemetryConfigError("invalid telemetry lock marker")
    key = _lock_marker_key(info, scope)
    if not raw:
        if key in _MARKER_HIGH_WATER:
            raise TelemetryConfigError("invalid telemetry lock marker")
        return 0
    if not re.fullmatch(rb"[1-9][0-9]*", raw):
        raise TelemetryConfigError("invalid telemetry lock marker")
    version = int(raw)
    previous = _MARKER_HIGH_WATER.get(key)
    if previous is not None and version < previous:
        raise TelemetryConfigError("regressive telemetry lock marker")
    _MARKER_HIGH_WATER[key] = max(version, previous or 0)
    return version


def _bump_lock_marker(lock_fd, scope=None):
    version = _lock_marker_version(lock_fd, scope)
    version = version + 1 if version else 1
    os.lseek(lock_fd, 0, os.SEEK_SET)
    os.ftruncate(lock_fd, 0)
    data = str(version).encode("ascii")
    if os.write(lock_fd, data) != len(data):
        raise OSError("could not write telemetry lock marker")
    os.fsync(lock_fd)
    info = os.fstat(lock_fd)
    _MARKER_HIGH_WATER[_lock_marker_key(info, scope)] = version
    return version


def _create_temp(directory_fd, directory):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(10):
        name = f".{CONFIG_PATH.name}.{secrets.token_hex(12)}.tmp"
        try:
            return _open_relative(directory_fd, directory, name, flags), name
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, "could not create unique telemetry temp file")


def _replace_relative(directory_fd, directory, temporary, target):
    if os.rename not in os.supports_dir_fd:
        raise TelemetryConfigError("secure telemetry rename primitives unavailable")
    return os.rename(temporary, target, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)


def _read_limited_descriptor(descriptor, limit, label):
    data = bytearray()
    while len(data) <= limit:
        chunk = os.read(descriptor, min(8192, limit + 1 - len(data)))
        if not chunk:
            return bytes(data)
        if len(chunk) > limit - len(data):
            raise TelemetryConfigError(f"telemetry file exceeds limit: {label}")
        data.extend(chunk)
    raise TelemetryConfigError(f"telemetry file exceeds limit: {label}")


def _read_config(directory, directory_fd, path):
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    descriptor = _open_relative(directory_fd, directory, path.name, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise TelemetryConfigError("telemetry config must be a regular file")
        if info.st_nlink > 1:
            raise TelemetryConfigError("telemetry config must not be hardlinked")
        os.fchmod(descriptor, 0o600)
        data = _read_limited_descriptor(descriptor, MAX_CONFIG_BYTES, path)
        return json.loads(data.decode("utf-8"))
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_generation_locked(directory, directory_fd):
    descriptor = None
    try:
        descriptor = _open_relative(
            directory_fd,
            directory,
            _GENERATION_NAME,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise TelemetryConfigError("telemetry generation must be a private regular file")
        os.fchmod(descriptor, 0o600)
        raw = _read_limited_descriptor(descriptor, 64, directory / _GENERATION_NAME)
    except FileNotFoundError:
        return 0
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not re.fullmatch(rb"[0-9]+", raw):
        raise TelemetryConfigError("invalid telemetry generation")
    return int(raw)


def _write_generation_locked(directory, directory_fd, generation):
    descriptor = None
    temporary = None
    try:
        descriptor, temporary = _create_temp(directory_fd, directory)
        os.fchmod(descriptor, 0o600)
        data = str(generation).encode("ascii")
        if os.write(descriptor, data) != len(data):
            raise OSError("could not write telemetry generation")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        _replace_relative(directory_fd, directory, temporary, _GENERATION_NAME)
        temporary = None
    except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
        raise TelemetryConfigError("could not write telemetry generation") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                _unlink_relative(directory_fd, directory, temporary)
            except FileNotFoundError:
                pass


def _ensure_generation_locked(directory, directory_fd):
    try:
        _stat_relative(directory_fd, directory, _GENERATION_NAME)
    except FileNotFoundError:
        _write_generation_locked(directory, directory_fd, 0)
    return _read_generation_locked(directory, directory_fd)


def _config_signature(directory, directory_fd, lock_fd):
    try:
        info = _stat_relative(directory_fd, directory, CONFIG_PATH.name)
    except FileNotFoundError:
        info = None
    if info is not None:
        if not stat.S_ISREG(info.st_mode):
            raise TelemetryConfigError("telemetry config must be a regular file")
        config_identity = (info.st_ino, info.st_size, info.st_mtime_ns)
    else:
        config_identity = None
    return (config_identity, _read_generation_locked(directory, directory_fd))


def _load_config_snapshot(home=None):
    root = _home(home)
    path = root / CONFIG_PATH
    try:
        opened = _open_secure_directory(root, CONFIG_PATH.parent, create=True)
    except TelemetryConfigError:
        raise
    except OSError as exc:
        raise TelemetryConfigError(f"invalid telemetry config: {path}") from exc
    if opened is None:
        return None, None
    directory, directory_fd = opened
    try:
        with _config_directory_lock(directory, directory_fd) as lock_fd:
            _ensure_generation_locked(directory, directory_fd)
            try:
                config = _read_config(directory, directory_fd, directory / CONFIG_PATH.name)
                config = _validate_config(config)
            except FileNotFoundError:
                config = None
            return config, _config_signature(directory, directory_fd, lock_fd)
    except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TelemetryConfigError(f"invalid telemetry config: {path}") from exc
    finally:
        os.close(directory_fd)


def load_config(home=None):
    config, _ = _load_config_snapshot(home)
    return config


def _write_config_locked(directory, directory_fd, config):
    target = CONFIG_PATH.name
    try:
        info = _stat_relative(directory_fd, directory, target)
    except FileNotFoundError:
        info = None
    if info is not None:
        if stat.S_ISLNK(info.st_mode):
            raise TelemetryConfigError("telemetry config must not be a symlink")
        if not stat.S_ISREG(info.st_mode):
            raise TelemetryConfigError("telemetry config must be a regular file")
    descriptor, temporary = _create_temp(directory_fd, directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(config, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            info = _stat_relative(directory_fd, directory, target)
        except FileNotFoundError:
            info = None
        if info is not None:
            if stat.S_ISLNK(info.st_mode):
                raise TelemetryConfigError("telemetry config must not be a symlink")
            if not stat.S_ISREG(info.st_mode):
                raise TelemetryConfigError("telemetry config must be a regular file")
        _replace_relative(directory_fd, directory, temporary, target)
        temporary = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                _unlink_relative(directory_fd, directory, temporary)
            except FileNotFoundError:
                pass


def write_config(home, config):
    config = _validate_config(config)
    root = _home(home)
    path = root / CONFIG_PATH
    try:
        opened = _open_secure_directory(root, CONFIG_PATH.parent, create=True)
        directory, directory_fd = opened
        try:
            with _config_directory_lock(directory, directory_fd) as lock_fd:
                generation = _read_generation_locked(directory, directory_fd)
                _bump_lock_marker(lock_fd, directory)
                _write_config_locked(directory, directory_fd, config)
                _write_generation_locked(directory, directory_fd, generation + 1)
        finally:
            os.close(directory_fd)
    except TelemetryConfigError:
        raise
    except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
        raise TelemetryConfigError(f"telemetry operation failed: {path}") from exc
    return path


def request_consent(home, *, interactive, input_fn=None):
    current, signature = _load_config_snapshot(home)
    if current is not None and current["enabled"]:
        return current
    if not interactive:
        raise TelemetryConfigError("telemetry consent requires an interactive terminal")
    print(DISCLOSURE)
    try:
        answer = (input if input_fn is None else input_fn)("Enable anonymous aggregate telemetry? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
        print()
    config = enabled_config() if answer.strip().lower() == "yes" else disabled_config()
    root = _home(home)
    path = root / CONFIG_PATH
    try:
        opened = _open_secure_directory(root, CONFIG_PATH.parent, create=True)
        directory, directory_fd = opened
        try:
            with _config_directory_lock(directory, directory_fd) as lock_fd:
                try:
                    current = _validate_config(
                        _read_config(directory, directory_fd, directory / CONFIG_PATH.name)
                    )
                    current_signature = _config_signature(directory, directory_fd, lock_fd)
                except FileNotFoundError:
                    current = None
                    current_signature = _config_signature(directory, directory_fd, lock_fd)
                if current is not None and current["enabled"]:
                    return current
                if current_signature != signature:
                    if current is None:
                        raise TelemetryConfigError("telemetry config changed during consent")
                    return current
                _bump_lock_marker(lock_fd, directory)
                _write_config_locked(directory, directory_fd, config)
                return config
        finally:
            os.close(directory_fd)
    except TelemetryConfigError:
        raise
    except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TelemetryConfigError(f"telemetry operation failed: {path}: {exc}") from exc


def _purge_regular_files(directory, directory_fd, names, failures):
    for name in names:
        try:
            info = _stat_relative(directory_fd, directory, name)
        except FileNotFoundError:
            continue
        except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
            failures.append(f"{directory / name}: {exc}")
            continue
        if not stat.S_ISREG(info.st_mode):
            failures.append(f"{directory / name}: not a regular file")
            continue
        if info.st_nlink > 1:
            failures.append(f"{directory / name}: hardlinked telemetry state is not removable")
            continue
        try:
            _unlink_relative(directory_fd, directory, name)
        except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
            failures.append(f"{directory / name}: {exc}")


def _purge_state(state, failures):
    if state is None:
        return
    state_path, state_fd = state
    try:
        _purge_regular_files(
            state_path,
            state_fd,
            _STATE_TARGETS,
            failures,
        )
        names = os.listdir(state_fd)
        temporary_names = [
            name for name in names
            if name.endswith(".tmp") and any(name.startswith(f".{base}.") for base in _TEMP_BASE_NAMES)
        ]
        _purge_regular_files(state_path, state_fd, temporary_names, failures)
    except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
        failures.append(f"{state_path}: {exc}")


def _purge_config_temps(directory, directory_fd, failures):
    try:
        names = os.listdir(directory_fd)
    except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
        failures.append(f"{directory}: {exc}")
        return
    temporary_names = [
        name for name in names
        if name.startswith(f".{CONFIG_PATH.name}.") and name.endswith(".tmp")
    ]
    _purge_regular_files(directory, directory_fd, temporary_names, failures)


def _assert_no_telemetry_files(state, config_directory, config_directory_fd, failures):
    if state is not None:
        state_path, state_fd = state
        for name in _STATE_TARGETS:
            try:
                _stat_relative(state_fd, state_path, name)
            except FileNotFoundError:
                continue
            except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
                failures.append(f"{state_path / name}: {exc}")
            else:
                failures.append(f"{state_path / name}: telemetry data remains")
        try:
            names = os.listdir(state_fd)
        except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
            failures.append(f"{state_path}: {exc}")
        else:
            for name in names:
                if name.endswith(".tmp") and any(name.startswith(f".{base}.") for base in _TEMP_BASE_NAMES):
                    failures.append(f"{state_path / name}: telemetry temp remains")
    try:
        names = os.listdir(config_directory_fd)
    except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
        failures.append(f"{config_directory}: {exc}")
        return
    for name in names:
        if name.startswith(f".{CONFIG_PATH.name}.") and name.endswith(".tmp"):
            failures.append(f"{config_directory / name}: telemetry temp remains")


def disable(home, *, purge=False):
    if purge:
        root = _home(home)
        failures = []
        state = None
        state_fd = None
        config_parent_fd = None
        try:
            try:
                state = _open_secure_directory(root, STATE_PATH, create=False)
            except (AttributeError, NotImplementedError, RuntimeError, TypeError, TelemetryConfigError, OSError) as exc:
                failures.append(str(exc))
            if state is not None:
                _, state_fd = state
            try:
                config_parent = _open_secure_directory(root, CONFIG_PATH.parent, create=True)
            except (AttributeError, NotImplementedError, RuntimeError, TypeError, TelemetryConfigError, OSError) as exc:
                raise TelemetryConfigError(
                    "telemetry purge incomplete: " + "; ".join((*failures, str(exc)))
                ) from exc
            config_path_value, config_parent_fd = config_parent
            try:
                with _config_directory_lock(config_path_value, config_parent_fd) as lock_fd:
                    generation = _read_generation_locked(config_path_value, config_parent_fd)
                    _bump_lock_marker(lock_fd, config_path_value)
                    _purge_state(state, failures)
                    _purge_config_temps(config_path_value, config_parent_fd, failures)
                    try:
                        info = _stat_relative(config_parent_fd, config_parent, CONFIG_PATH.name)
                    except FileNotFoundError:
                        info = None
                    if info is not None and stat.S_ISLNK(info.st_mode):
                        try:
                            _unlink_relative(config_parent_fd, config_parent, CONFIG_PATH.name)
                        except OSError as exc:
                            failures.append(f"{config_parent / CONFIG_PATH.name}: {exc}")
                    _write_config_locked(config_path_value, config_parent_fd, disabled_config())
                    _write_generation_locked(config_path_value, config_parent_fd, generation + 1)
                    _purge_state(state, failures)
                    _purge_config_temps(config_path_value, config_parent_fd, failures)
                    _assert_no_telemetry_files(state, config_path_value, config_parent_fd, failures)
            except (AttributeError, NotImplementedError, RuntimeError, TypeError, TelemetryConfigError, OSError) as exc:
                failures.append(str(exc))
            marker = config_path_value / CONFIG_PATH.name
            if failures:
                raise TelemetryConfigError("telemetry purge incomplete: " + "; ".join(failures))
            return marker
        finally:
            try:
                if config_parent_fd is not None:
                    try:
                        os.close(config_parent_fd)
                    except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
                        failures.append(str(exc))
            finally:
                if state_fd is not None:
                    try:
                        os.close(state_fd)
                    except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
                        failures.append(str(exc))
            if failures:
                raise TelemetryConfigError("telemetry purge incomplete: " + "; ".join(failures))
    try:
        config = load_config(home)
        if config is not None and not config["enabled"]:
            root = _home(home)
            opened = _open_secure_directory(root, CONFIG_PATH.parent, create=True)
            directory, directory_fd = opened
            try:
                with _config_directory_lock(directory, directory_fd) as lock_fd:
                    generation = _read_generation_locked(directory, directory_fd)
                    _bump_lock_marker(lock_fd, directory)
                    _write_generation_locked(directory, directory_fd, generation + 1)
            finally:
                os.close(directory_fd)
            return config
        return write_config(home, disabled_config())
    except TelemetryConfigError:
        raise
    except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
        raise TelemetryConfigError("telemetry operation failed") from exc


def bucket_duration(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, Real) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("duration must be a finite non-negative number")
    if seconds < 1:
        return "lt_1s"
    if seconds < 5:
        return "1_to_5s"
    if seconds < 30:
        return "5_to_30s"
    if seconds < 120:
        return "30_to_120s"
    return "gte_120s"


def bucket_count(count):
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("count must be a non-negative integer")
    if count == 0:
        return "zero"
    if count == 1:
        return "one"
    if count <= 5:
        return "2_to_5"
    if count <= 20:
        return "6_to_20"
    return "gt_20"


def bucket_handoff_bytes(size):
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("handoff bytes must be a non-negative integer")
    if size < 4096:
        return "lt_4k"
    if size < 16384:
        return "4_to_16k"
    if size < 65536:
        return "16_to_64k"
    return "gte_64k"


def validate_event(payload):
    if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
        raise ValueError("event must be a JSON object")
    if set(payload) & DENYLIST:
        raise ValueError("privacy-sensitive field")
    if not set(payload) <= (_OPERATION_FIELDS | _FEEDBACK_FIELDS):
        raise ValueError("unknown field")
    if not isinstance(payload.get("event"), str) or payload["event"] not in EVENTS:
        raise ValueError("invalid event")

    fields = _OPERATION_FIELDS if payload["event"] == "operation_summary" else _FEEDBACK_FIELDS
    if set(payload) != fields:
        raise ValueError("wrong event shape")

    if payload["schema_version"] != 1 or isinstance(payload["schema_version"], bool) or not isinstance(payload["schema_version"], int):
        raise ValueError("invalid schema version")
    for field in fields - {"schema_version"}:
        if not isinstance(payload[field], str) or len(payload[field]) > 32:
            raise ValueError("invalid field type or length")

    if not _DAY.fullmatch(payload["day_utc"]):
        raise ValueError("invalid UTC day")
    try:
        date.fromisoformat(payload["day_utc"])
    except ValueError as exc:
        raise ValueError("invalid UTC day") from exc
    if not _VERSION.fullmatch(payload["plugin_version"]):
        raise ValueError("invalid plugin version")
    if payload["operation"] not in OPERATIONS or payload["source_client"] not in CLIENTS or payload["target_client"] not in CLIENTS:
        raise ValueError("invalid shared enum")

    if payload["event"] == "operation_summary":
        enums = {
            "result": RESULTS,
            "failure_stage": FAILURE_STAGES,
            "duration_bucket": DURATION_BUCKETS,
            "handoff_bytes_bucket": HANDOFF_BYTES_BUCKETS,
            "redaction_bucket": COUNT_BUCKETS,
            "dropped_events_bucket": COUNT_BUCKETS,
            "normalized_fields_bucket": COUNT_BUCKETS,
        }
    else:
        enums = {"feedback_category": FEEDBACK_CATEGORIES, "feedback_severity": FEEDBACK_SEVERITIES}
    if any(payload[field] not in values for field, values in enums.items()):
        raise ValueError("invalid enum")
    return payload


def serialize_event(payload):
    validated = validate_event(payload)
    serialized = json.dumps(validated, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > 2048:
        raise ValueError("serialized event exceeds 2 KiB")
    return serialized


def _as_day(value=None):
    if value is None:
        return datetime.now(timezone.utc).date()
    if isinstance(value, str):
        if value.endswith("Z"):
            return datetime.fromisoformat(value[:-1] + "+00:00").date()
        return date.fromisoformat(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    raise ValueError("invalid UTC timestamp")


def _as_datetime(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError("invalid UTC timestamp")
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _empty_counters():
    return {"schema_version": 1, "dropped_events": 0, "days": {}}


def _rows_content_digest(rows):
    return hashlib.sha256(
        json.dumps(list(rows), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class _TelemetryBatch(list):
    def __init__(self, rows):
        super().__init__(rows)
        self._queue_token = secrets.token_hex(16)
        self._snapshot_digest = _rows_content_digest(self)

    @property
    def queue_token(self):
        return self._queue_token

    def __copy__(self):
        replay = type(self)(self)
        replay._replay = True
        return replay

    def __deepcopy__(self, memo):
        replay = type(self)(self)
        replay._replay = True
        return replay


@contextmanager
def _state_lock(home):
    root = _home(home)
    config_opened = _open_secure_directory(root, CONFIG_PATH.parent, create=True)
    config_directory, config_fd = config_opened
    state_opened = None
    state_fd = None
    try:
        state_opened = _open_secure_directory(root, STATE_PATH, create=True)
        state_directory, state_fd = state_opened
        with _config_directory_lock(config_directory, config_fd):
            yield config_directory, config_fd, state_directory, state_fd
    finally:
        if state_fd is not None:
            os.close(state_fd)
        os.close(config_fd)


def _locked_config(config_directory, config_fd):
    try:
        return _validate_config(_read_config(config_directory, config_fd, config_directory / CONFIG_PATH.name))
    except FileNotFoundError:
        return None
    except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TelemetryConfigError("invalid telemetry config") from exc


def _read_state_bytes(state_directory, state_fd, name, limit):
    descriptor = None
    try:
        descriptor = _open_relative(
            state_fd,
            state_directory,
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise TelemetryConfigError(f"telemetry state file is not regular: {state_directory / name}")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise TelemetryConfigError(f"telemetry state file is not private: {state_directory / name}")
        return _read_limited_descriptor(descriptor, limit, state_directory / name)
    except FileNotFoundError:
        return None
    except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
        raise TelemetryConfigError(f"invalid telemetry state: {state_directory / name}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_json_state(state_directory, state_fd, name, default, limit):
    raw = _read_state_bytes(state_directory, state_fd, name, limit)
    if raw is None:
        return default
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise TelemetryConfigError(f"invalid telemetry state: {state_directory / name}") from exc


def _write_state(state_directory, state_fd, name, text):
    payload = text.encode("utf-8")
    descriptor = None
    temporary = None
    try:
        descriptor, temporary = _create_temp(state_fd, state_directory)
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        _replace_relative(state_fd, state_directory, temporary, name)
        temporary = None
    except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
        raise TelemetryConfigError(f"could not write telemetry state: {state_directory / name}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                _unlink_relative(state_fd, state_directory, temporary)
            except FileNotFoundError:
                pass


def _remove_state(state_directory, state_fd, name):
    try:
        _unlink_relative(state_fd, state_directory, name)
    except FileNotFoundError:
        pass


def _load_counters_locked(state_directory, state_fd):
    counters = _read_json_state(
        state_directory, state_fd, _COUNTERS_NAME, _empty_counters(), MAX_COUNTER_BYTES
    )
    if not isinstance(counters, dict) or set(counters) != {"schema_version", "dropped_events", "days"}:
        raise TelemetryConfigError("invalid telemetry counters")
    if type(counters["schema_version"]) is not int or counters["schema_version"] != 1:
        raise TelemetryConfigError("invalid telemetry counters")
    if type(counters["dropped_events"]) is not int or not 0 <= counters["dropped_events"] <= 10000:
        raise TelemetryConfigError("invalid telemetry counters")
    if not isinstance(counters.get("days"), dict):
        raise TelemetryConfigError("invalid telemetry counters")
    try:
        for day, entries in counters["days"].items():
            if not isinstance(day, str) or not _DAY.fullmatch(day):
                raise ValueError("invalid counter day")
            date.fromisoformat(day)
            if not isinstance(entries, list):
                raise ValueError("invalid counter entries")
            keys = set()
            for entry in entries:
                if set(entry) != {"key", "event", "count"}:
                    raise ValueError("invalid counter entry")
                if type(entry["count"]) is not int or not 1 <= entry["count"] <= 10000:
                    raise ValueError("invalid counter count")
                event = entry["event"]
                validate_event(event)
                if event["day_utc"] != day or entry["key"] != _event_key(event) or entry["key"] in keys:
                    raise ValueError("invalid counter identity")
                keys.add(entry["key"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TelemetryConfigError("invalid telemetry counters") from exc
    return counters


def _load_counters(home):
    with _state_lock(home) as (_config_directory, _config_fd, state_directory, state_fd):
        return _load_counters_locked(state_directory, state_fd)


def _event_key(event):
    return json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def increment_counter(event, home=None, now=None):
    validate_event(event)
    with _state_lock(home) as (config_directory, config_fd, state_directory, state_fd):
        config = _locked_config(config_directory, config_fd)
        if config is None or not config["enabled"]:
            return 0
        day = event["day_utc"]
        counters = _load_counters_locked(state_directory, state_fd)
        entries = counters["days"].setdefault(day, [])
        key = _event_key(event)
        for entry in entries:
            if entry.get("key") == key:
                if event["event"] == "context_feedback":
                    return 0
                entry["count"] = min(10000, entry["count"] + 1)
                break
        else:
            entries.append({"key": key, "event": dict(event), "count": 1})
        _write_state(state_directory, state_fd, _COUNTERS_NAME, json.dumps(counters, sort_keys=True, separators=(",", ":")) + "\n")
        if event["event"] == "operation_summary":
            _write_state(
                state_directory,
                state_fd,
                _LAST_SUMMARY_NAME,
                json.dumps(
                    {"recorded_at": _as_datetime(now).isoformat().replace("+00:00", "Z"), "event": event},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
            )
        return next(entry["count"] for entry in entries if entry["key"] == key)


def load_last_operation_summary(home=None, now=None):
    missing = object()
    with _state_lock(home) as (_config_directory, _config_fd, state_directory, state_fd):
        saved = _read_json_state(state_directory, state_fd, _LAST_SUMMARY_NAME, missing, 16 * 1024)
        if saved is missing:
            return None
        if not isinstance(saved, dict) or set(saved) != {"recorded_at", "event"}:
            raise TelemetryConfigError("invalid last operation summary")
        try:
            recorded_at = _as_datetime(saved["recorded_at"])
            event = dict(saved["event"])
            validate_event(event)
        except (KeyError, TypeError, ValueError) as exc:
            raise TelemetryConfigError("invalid last operation summary") from exc
        if event["event"] != "operation_summary":
            raise TelemetryConfigError("invalid last operation summary")
        age = _as_datetime(now) - recorded_at
        if age < timedelta(0) or age > timedelta(hours=24):
            _remove_state(state_directory, state_fd, _LAST_SUMMARY_NAME)
            return None
        return event


def record_context_feedback(category, severity, home=None, now=None):
    config = load_config(home)
    if config is None or not config["enabled"]:
        raise TelemetryConfigError("telemetry is disabled")
    operation = load_last_operation_summary(home, now=now)
    if operation is None:
        raise TelemetryConfigError("no recent operation summary")
    event = {field: operation[field] for field in _COMMON_FIELDS}
    event.update({"event": "context_feedback", "feedback_category": category, "feedback_severity": severity})
    validate_event(event)
    if increment_counter(event, home=home, now=now) == 0:
        raise TelemetryConfigError("feedback was already recorded or telemetry was disabled")
    return event


def _aggregate_row(event, count):
    row = {
        "schema_version": 1,
        "event": "daily_aggregate",
        "aggregate": "operation" if event["event"] == "operation_summary" else "context_feedback",
        "day_utc": event["day_utc"],
        "plugin_version": event["plugin_version"],
        "operation": event["operation"],
        "source_client": event["source_client"],
        "target_client": event["target_client"],
        "count": count,
    }
    fields = (
        ("result", "failure_stage", "duration_bucket", "handoff_bytes_bucket", "redaction_bucket", "dropped_events_bucket", "normalized_fields_bucket")
        if row["aggregate"] == "operation"
        else ("feedback_category", "feedback_severity")
    )
    row.update({field: event[field] for field in fields})
    return row


def _validate_aggregate(row):
    if not isinstance(row, dict) or type(row.get("schema_version")) is not int or row["schema_version"] != 1:
        raise ValueError("invalid aggregate row")
    if row.get("event") != "daily_aggregate" or type(row.get("count")) is not int or not 1 <= row["count"] <= 10000:
        raise ValueError("invalid aggregate row")
    common = {"schema_version", "event", "aggregate", "day_utc", "plugin_version", "operation", "source_client", "target_client", "count"}
    if row.get("aggregate") == "operation":
        expected = common | {"result", "failure_stage", "duration_bucket", "handoff_bytes_bucket", "redaction_bucket", "dropped_events_bucket", "normalized_fields_bucket"}
        if set(row) != expected:
            raise ValueError("invalid operation aggregate row")
    elif row.get("aggregate") == "context_feedback":
        expected = common | {"feedback_category", "feedback_severity"}
        if set(row) != expected:
            raise ValueError("invalid feedback aggregate row")
    else:
        raise ValueError("invalid aggregate row")
    if not isinstance(row.get("day_utc"), str) or not _DAY.fullmatch(row["day_utc"]):
        raise ValueError("invalid aggregate day")
    event = dict(row)
    event.pop("event")
    event.pop("aggregate")
    event.pop("count")
    event["event"] = "operation_summary" if row["aggregate"] == "operation" else "context_feedback"
    validate_event(event)
    return row


def _read_queue_snapshot_locked(state_directory, state_fd, limit):
    descriptor = None
    try:
        descriptor = _open_relative(
            state_fd,
            state_directory,
            _QUEUE_NAME,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise TelemetryConfigError(f"telemetry state file is not regular: {state_directory / _QUEUE_NAME}")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise TelemetryConfigError(f"telemetry state file is not private: {state_directory / _QUEUE_NAME}")
        lines = []
        total = 0
        physical_lines = 0
        while physical_lines < limit:
            line = bytearray()
            while True:
                chunk = os.read(descriptor, 1)
                if not chunk:
                    if not line:
                        break
                    break
                total += 1
                if total > MAX_UPLOAD_BYTES:
                    raise TelemetryConfigError(
                        f"telemetry queue exceeds upload byte limit: {state_directory / _QUEUE_NAME}"
                    )
                line.extend(chunk)
                if chunk == b"\n":
                    break
            if not line:
                break
            physical_lines += 1
            if line.strip():
                lines.append(bytes(line))
        try:
            return [_validate_aggregate(json.loads(line)) for line in lines]
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise TelemetryConfigError(f"invalid telemetry queue: {state_directory / _QUEUE_NAME}") from exc
    except FileNotFoundError:
        return []
    except (AttributeError, NotImplementedError, RuntimeError, TypeError, OSError) as exc:
        raise TelemetryConfigError(f"invalid telemetry queue: {state_directory / _QUEUE_NAME}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_queue_locked(state_directory, state_fd, limit=None):
    if limit is not None:
        return _read_queue_snapshot_locked(state_directory, state_fd, limit)
    raw = _read_state_bytes(state_directory, state_fd, _QUEUE_NAME, MAX_QUEUE_BYTES)
    if raw is None:
        return []
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) > MAX_QUEUE_ROWS:
        raise TelemetryConfigError(f"telemetry queue exceeds row limit: {state_directory / _QUEUE_NAME}")
    try:
        return [_validate_aggregate(json.loads(line)) for line in lines]
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise TelemetryConfigError(f"invalid telemetry queue: {state_directory / _QUEUE_NAME}") from exc


def _read_queue(home):
    with _state_lock(home) as (_config_directory, _config_fd, state_directory, state_fd):
        return _read_queue_locked(state_directory, state_fd)


def _row_bytes(rows):
    return sum(len(json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")) + 1 for row in rows)


def _store_queue_locked(state_directory, state_fd, rows):
    rows = sorted(rows, key=lambda row: (row["day_utc"], json.dumps(row, sort_keys=True)))
    rows = rows[-MAX_QUEUE_ROWS:]
    while rows and _row_bytes(rows) > MAX_QUEUE_BYTES:
        rows.pop(0)
    if not rows:
        _remove_state(state_directory, state_fd, _QUEUE_NAME)
        return 0
    text = "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)
    _write_state(state_directory, state_fd, _QUEUE_NAME, text)
    return len(rows)


def _store_queue(home, rows):
    with _state_lock(home) as (_config_directory, _config_fd, state_directory, state_fd):
        return _store_queue_locked(state_directory, state_fd, rows)


def close_day(home=None, now=None):
    current = _as_day(now)
    with _state_lock(home) as (config_directory, config_fd, state_directory, state_fd):
        config = _locked_config(config_directory, config_fd)
        if config is None or not config["enabled"]:
            return []
        counters = _load_counters_locked(state_directory, state_fd)
        closed = []
        remaining = {}
        cutoff = current - timedelta(days=MAX_QUEUE_AGE_DAYS)
        for day, entries in counters["days"].items():
            parsed = date.fromisoformat(day)
            if parsed < current:
                if parsed >= cutoff:
                    closed.extend(_aggregate_row(entry["event"], entry["count"]) for entry in entries)
            else:
                remaining[day] = entries
        rows = _read_queue_locked(state_directory, state_fd) + closed if closed else []
        if rows:
            unique = {}
            for row in rows:
                unique.setdefault(json.dumps(row, sort_keys=True, separators=(",", ":")), row)
            rows = list(unique.values())
        rows = [row for row in rows if date.fromisoformat(row["day_utc"]) >= cutoff]
        rows.sort(key=lambda row: (row["day_utc"], json.dumps(row, sort_keys=True)))
        before = len(rows)
        counters["days"] = remaining
        stored = _store_queue_locked(state_directory, state_fd, rows) if rows else 0
        counters["dropped_events"] = min(10000, counters["dropped_events"] + before - stored)
        if remaining or counters["dropped_events"]:
            _write_state(state_directory, state_fd, _COUNTERS_NAME, json.dumps(counters, sort_keys=True, separators=(",", ":")) + "\n")
        else:
            _remove_state(state_directory, state_fd, _COUNTERS_NAME)
        return closed


def load_batch(home=None, limit=MAX_UPLOAD_ROWS, now=None):
    limit = min(MAX_UPLOAD_ROWS, max(0, int(limit)))
    with _state_lock(home) as (_config_directory, _config_fd, state_directory, state_fd):
        return _load_batch_locked(state_directory, state_fd, limit, now)


def _load_batch_locked(state_directory, state_fd, limit, now):
    batch_rows = _read_queue_locked(state_directory, state_fd, limit)
    rows = _read_queue_locked(state_directory, state_fd)
    cutoff = _as_day(now) - timedelta(days=MAX_QUEUE_AGE_DAYS)
    kept = [row for row in rows if date.fromisoformat(row["day_utc"]) >= cutoff]
    if len(kept) != len(rows):
        _store_queue_locked(state_directory, state_fd, kept)
        batch_rows = kept
    else:
        batch_rows = [row for row in batch_rows if date.fromisoformat(row["day_utc"]) >= cutoff]
    batch_rows.sort(key=lambda row: (row["day_utc"], json.dumps(row, sort_keys=True)))
    batch = _TelemetryBatch(batch_rows[:limit])
    batch._queue_snapshot_token = _queue_token_locked(state_directory, state_fd)
    return batch


def _read_lease_locked(state_directory, state_fd):
    lease = _read_json_state(state_directory, state_fd, _LEASE_NAME, None, 4096)
    if lease is None:
        return None
    if (
        not isinstance(lease, dict)
        or set(lease) != {"schema_version", "token", "digest", "leased_until"}
        or type(lease["schema_version"]) is not int
        or lease["schema_version"] != 1
        or not isinstance(lease["token"], str)
        or not isinstance(lease["digest"], str)
        or isinstance(lease["leased_until"], bool)
        or not isinstance(lease["leased_until"], Real)
    ):
        raise TelemetryConfigError("invalid telemetry batch lease")
    return lease


def _claim_batch_lease_locked(state_directory, state_fd, batch):
    if not batch:
        return None
    lease = _read_lease_locked(state_directory, state_fd)
    if lease is not None and lease["leased_until"] > time.time():
        return None
    batch._queue_token = secrets.token_hex(16)
    batch._lease_claimed = True
    _write_state(
        state_directory,
        state_fd,
        _LEASE_NAME,
        json.dumps(
            {
                "schema_version": 1,
                "token": batch.queue_token,
                "digest": _rows_content_digest(batch),
                "leased_until": time.time() + _LEASE_SECONDS,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )
    return batch


def _release_batch_lease_locked(state_directory, state_fd, batch):
    lease = _read_lease_locked(state_directory, state_fd)
    if lease is not None and lease["token"] == batch.queue_token:
        _remove_state(state_directory, state_fd, _LEASE_NAME)


def _release_batch_lease(home, batch):
    with _state_lock(home) as (_config_directory, _config_fd, state_directory, state_fd):
        _release_batch_lease_locked(state_directory, state_fd, batch)


def _queue_token_locked(state_directory, state_fd):
    try:
        info = _stat_relative(state_fd, state_directory, _QUEUE_NAME)
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _batch_digest(rows, queue_token=None):
    identities = [hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest() for row in rows]
    if queue_token is None:
        queue_token = getattr(rows, "queue_token", None)
    payload = json.dumps(
        {
            "queue_token": queue_token,
            "row_identities": identities,
            "rows": list(rows),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _batch_content_digest(rows):
    return _rows_content_digest(rows)


def _ack_batch_locked(state_directory, state_fd, batch, accepted, digest):
    if not isinstance(batch.queue_token, str) or getattr(batch, "consumed", False) or getattr(batch, "_replay", False):
        raise TelemetryConfigError("telemetry queue batch token is stale")
    if getattr(batch, "_snapshot_digest", None) != _rows_content_digest(batch):
        raise TelemetryConfigError("telemetry queue batch was mutated")
    if digest != _batch_digest(batch):
        raise TelemetryConfigError("telemetry queue batch is stale")
    if getattr(batch, "_queue_snapshot_token", None) != _queue_token_locked(state_directory, state_fd):
        raise TelemetryConfigError("telemetry queue batch is stale")
    if getattr(batch, "_lease_claimed", False):
        lease = _read_lease_locked(state_directory, state_fd)
        if lease is None or lease["token"] != batch.queue_token or lease["digest"] != _rows_content_digest(batch):
            raise TelemetryConfigError("telemetry queue batch lease is stale")
    rows = _read_queue_locked(state_directory, state_fd)
    if _batch_digest(rows[:len(batch)], batch.queue_token) != digest:
        raise TelemetryConfigError("telemetry queue batch is stale")
    _store_queue_locked(state_directory, state_fd, rows[accepted:])
    if getattr(batch, "_lease_claimed", False):
        _remove_state(state_directory, state_fd, _LEASE_NAME)
    batch.consumed = True


def ack_batch(home, batch, *, accepted=None, digest=None):
    if not isinstance(batch, _TelemetryBatch) or not isinstance(batch.queue_token, str) or digest is None:
        raise TelemetryConfigError("telemetry queue batch token is required")
    if not batch:
        raise TelemetryConfigError("empty telemetry queue batch")
    if getattr(batch, "_snapshot_digest", None) != _rows_content_digest(batch):
        raise TelemetryConfigError("telemetry queue batch was mutated")
    if accepted is None:
        accepted = len(batch)
    if type(accepted) is not int or not 0 <= accepted <= len(batch):
        raise TelemetryConfigError("invalid telemetry queue batch acceptance")
    if digest is None:
        digest = _batch_digest(batch)
    if not isinstance(digest, str):
        raise TelemetryConfigError("invalid telemetry queue batch digest")
    with _state_lock(home) as (_config_directory, _config_fd, state_directory, state_fd):
        _ack_batch_locked(state_directory, state_fd, batch, accepted, digest)


def _otlp_value(value):
    if type(value) is int:
        return {"intValue": str(value)}
    return {"stringValue": value}


def to_otlp_logs(rows):
    records = []
    for row in rows:
        _validate_aggregate(row)
        attributes = [
            {"key": key, "value": _otlp_value(row[key])}
            for key in sorted(row)
        ]
        records.append({"body": {"stringValue": "session_handoff.daily_aggregate"}, "attributes": attributes})
    return {
        "resourceLogs": [{
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "session-handoff"}}]},
            "scopeLogs": [{"logRecords": records}],
        }]
    }


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def idempotency_key(body):
    return hashlib.sha256(body).hexdigest()


def request_headers(body, encoding=None):
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Idempotency-Key": idempotency_key(body),
    }
    if encoding:
        headers["Content-Encoding"] = encoding
    return headers


def build_request(endpoint, rows):
    if len(rows) > MAX_UPLOAD_ROWS:
        raise ValueError("telemetry request exceeds row limit")
    raw = json.dumps(to_otlp_logs(rows), sort_keys=True, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw, mtime=0)
    if len(compressed) <= MAX_UPLOAD_BYTES and len(compressed) < len(raw):
        body, encoding = compressed, "gzip"
    elif len(raw) <= MAX_UPLOAD_BYTES:
        body, encoding = raw, None
    else:
        raise ValueError("telemetry request body exceeds limit")
    return urllib.request.Request(
        endpoint,
        data=body,
        headers=request_headers(body, encoding),
        method="POST",
    )


def _set_response_timeout(response, seconds):
    socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    setter = getattr(socket, "settimeout", None)
    if callable(setter):
        setter(max(0.01, seconds))


def _accepted_count(response, total, deadline):
    data = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("telemetry response timed out")
        _set_response_timeout(response, remaining)
        chunk = response.read(min(4096, MAX_RESPONSE_BYTES + 1 - len(data)))
        if not chunk:
            break
        if len(chunk) > MAX_RESPONSE_BYTES - len(data):
            raise ValueError("telemetry acceptance response is too large")
        data.extend(chunk)
    if not data:
        raise ValueError("telemetry acceptance response is empty")
    def reject_duplicate_keys(pairs):
        payload = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError("duplicate telemetry acceptance response field")
            payload[key] = value
        return payload

    payload = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    if not isinstance(payload, dict):
        raise ValueError("invalid telemetry acceptance response")
    if set(payload) != {"accepted"}:
        raise ValueError("invalid telemetry acceptance response")
    accepted = payload.get("accepted")
    if type(accepted) is not int or not 0 <= accepted <= total:
        raise ValueError("invalid telemetry acceptance response")
    return accepted


def flush_queue(home=None, opener=None, now=None):
    deadline = time.monotonic() + 3.0
    response = None
    batch = None
    batch_key = None
    try:
        with _state_lock(home) as (config_directory, config_fd, state_directory, state_fd):
            config = _locked_config(config_directory, config_fd)
            if config is None or not config["enabled"]:
                return 0
            batch = _load_batch_locked(state_directory, state_fd, MAX_UPLOAD_ROWS, now)
            if not _claim_batch_lease_locked(state_directory, state_fd, batch):
                return 0
            batch_key = batch.queue_token
            if batch_key in _IN_FLIGHT_BATCHES:
                return 0
            _IN_FLIGHT_BATCHES.add(batch_key)
            endpoint = config["endpoint"]
        request = build_request(endpoint, batch)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0
        if opener is None:
            response = _NO_REDIRECT_OPENER.open(request, timeout=remaining)
        elif callable(opener):
            response = opener(request, timeout=remaining)
        else:
            response = opener.open(request, timeout=remaining)
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        if not 200 <= status < 300:
            return 0
        geturl = getattr(response, "geturl", None)
        if callable(geturl) and geturl() != endpoint:
            return 0
        accepted = _accepted_count(response, len(batch), deadline)
        ack_batch(home, batch, accepted=accepted, digest=_batch_digest(batch))
        return accepted
    except Exception:
        return 0
    finally:
        if batch is not None and getattr(batch, "_lease_claimed", False) and not getattr(batch, "consumed", False):
            try:
                _release_batch_lease(home, batch)
            except Exception:
                pass
        if batch_key is not None:
            _IN_FLIGHT_BATCHES.discard(batch_key)
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def detached_flush(queue_path, config_path):
    queue_path = Path(queue_path)
    config_path = Path(config_path)
    try:
        home = config_path.resolve().parents[2]
        if queue_path.resolve() != home / STATE_PATH / _QUEUE_NAME:
            return 0
        close_day(home)
        return flush_queue(home)
    except Exception:
        return 0


def _sanitized_environment():
    return {}


def spawn_detached_flush(queue_path, config_path):
    command = [
        sys.executable,
        str(Path(__file__).parents[1] / "bin/session-handoff"),
        "telemetry",
        "_detached-flush",
        "--queue-path",
        str(queue_path),
        "--config-path",
        str(config_path),
    ]
    subprocess.Popen(
        command,
        env=_sanitized_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
