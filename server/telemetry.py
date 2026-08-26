"""Privacy-safe, closed telemetry event schema."""

from datetime import date, datetime, timezone
from contextlib import contextmanager
import errno
import json
import math
import os
import re
import secrets
import stat
import threading
import time
from numbers import Real
from pathlib import Path

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
_STATE_TARGETS = ("telemetry-counters.json", "telemetry-queue.jsonl", "last-operation-summary.json")
FEEDBACK_CATEGORIES = frozenset({"constraint", "decision", "path", "progress", "rejected_attempt", "other"})
FEEDBACK_SEVERITIES = frozenset({"recoverable", "blocked"})

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
_MARKER_HIGH_WATER = {}

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


def _lock_marker_version(lock_fd):
    info = os.fstat(lock_fd)
    if not stat.S_ISREG(info.st_mode):
        raise TelemetryConfigError("telemetry lock must be a regular file")
    if info.st_nlink > 1:
        raise TelemetryConfigError("telemetry lock must not be hardlinked")
    os.lseek(lock_fd, 0, os.SEEK_SET)
    raw = os.read(lock_fd, 64)
    if info.st_size > 64:
        raise TelemetryConfigError("invalid telemetry lock marker")
    key = (info.st_dev, info.st_ino)
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


def _bump_lock_marker(lock_fd):
    version = _lock_marker_version(lock_fd)
    version = version + 1 if version else 1
    os.lseek(lock_fd, 0, os.SEEK_SET)
    os.ftruncate(lock_fd, 0)
    data = str(version).encode("ascii")
    if os.write(lock_fd, data) != len(data):
        raise OSError("could not write telemetry lock marker")
    os.fsync(lock_fd)
    info = os.fstat(lock_fd)
    _MARKER_HIGH_WATER[(info.st_dev, info.st_ino)] = version
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


def _read_config(directory, directory_fd, path):
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    descriptor = _open_relative(directory_fd, directory, path.name, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TelemetryConfigError("telemetry config must be a regular file")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            return json.load(stream)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _config_signature(directory, directory_fd, lock_fd):
    info = _stat_relative(directory_fd, directory, CONFIG_PATH.name)
    if not stat.S_ISREG(info.st_mode):
        raise TelemetryConfigError("telemetry config must be a regular file")
    return (info.st_ino, info.st_size, info.st_mtime_ns, _lock_marker_version(lock_fd))


def _load_config_snapshot(home=None):
    root = _home(home)
    path = root / CONFIG_PATH
    try:
        opened = _open_secure_directory(root, CONFIG_PATH.parent, create=False)
    except TelemetryConfigError:
        raise
    except OSError as exc:
        raise TelemetryConfigError(f"invalid telemetry config: {path}") from exc
    if opened is None:
        return None, None
    directory, directory_fd = opened
    try:
        with _config_directory_lock(directory, directory_fd) as lock_fd:
            config = _read_config(directory, directory_fd, directory / CONFIG_PATH.name)
            config = _validate_config(config)
            return config, _config_signature(directory, directory_fd, lock_fd)
    except FileNotFoundError:
        return None, None
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
                _bump_lock_marker(lock_fd)
                _write_config_locked(directory, directory_fd, config)
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
                    current_signature = None
                if current is not None and current["enabled"]:
                    return current
                if current_signature != signature:
                    if current is None:
                        raise TelemetryConfigError("telemetry config changed during consent")
                    return current
                _bump_lock_marker(lock_fd)
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
                    _bump_lock_marker(lock_fd)
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
                    _bump_lock_marker(lock_fd)
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
