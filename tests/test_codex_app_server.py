import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import codex_app_server


def test_adapter_can_be_imported_by_direct_server_script():
    server_path = str(Path(codex_app_server.__file__).parent)
    result = subprocess.run(
        [sys.executable, "-c", "import codex_app_server; print(codex_app_server.SDK_VERSION)"],
        cwd=server_path, capture_output=True, text=True, timeout=2,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.160.0"


@pytest.fixture
def local_socket(tmp_path):
    path = tmp_path / "control.sock"
    with socket.socket(socket.AF_UNIX) as endpoint:
        endpoint.bind(str(path))
        yield path


@pytest.fixture
def fake_sdk(monkeypatch):
    calls = []
    clients = []

    class Client:
        def __init__(self, config, approval_handler=None):
            self.config = config
            self.approval_handler = approval_handler
            self.read_response = SimpleNamespace(
                thread=SimpleNamespace(
                    id="thread-123",
                    status=SimpleNamespace(root=SimpleNamespace(type="idle")),
                    preview="private conversation",
                    turns=["private turn"],
                    cwd="private working directory",
                )
            )
            clients.append(self)

        def start(self):
            calls.append(("start",))

        def initialize(self):
            calls.append(("initialize",))

        def thread_read(self, thread_id, include_turns=False):
            calls.append(("thread_read", thread_id, include_turns))
            return self.read_response

        def close(self):
            calls.append(("close",))

    sdk = SimpleNamespace(__version__="0.160.0", CodexConfig=SimpleNamespace)

    def load(name):
        calls.append(("import", name))
        if name == "openai_codex":
            return sdk
        if name == "openai_codex.client":
            return SimpleNamespace(CodexClient=Client)
        raise AssertionError(f"Unexpected module: {name}")

    monkeypatch.setattr(codex_app_server.importlib, "import_module", load)
    return SimpleNamespace(calls=calls, clients=clients, sdk=sdk, Client=Client)


def test_connects_only_existing_socket_and_reads_requested_thread(local_socket, fake_sdk):
    adapter = codex_app_server.CodexAppServerAdapter(local_socket)

    assert adapter.thread_context("thread-123") == {
        "thread_id": "thread-123",
        "status": "idle",
        "source": "codex_app_server",
        "token_usage": None,
    }
    assert fake_sdk.clients[0].config.launch_args_override == (
        "codex", "app-server", "proxy", "--sock", str(local_socket)
    )
    assert fake_sdk.clients[0].config.experimental_api is False
    assert fake_sdk.calls == [
        ("import", "openai_codex"),
        ("import", "openai_codex.client"),
        ("start",), ("initialize",),
        ("thread_read", "thread-123", False), ("close",),
    ]
    assert adapter.unavailable_reason is None


@pytest.mark.parametrize("method", [
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/tool/requestUserInput",
    "unknown/request",
])
def test_read_only_client_never_answers_server_requests(local_socket, fake_sdk, method):
    assert codex_app_server.CodexAppServerAdapter(local_socket).thread_context("thread-123") is not None
    with pytest.raises(PermissionError, match="Read-only adapter"):
        fake_sdk.clients[0].approval_handler(method, {"private": "uninspected"})


@pytest.mark.parametrize("thread_id", [None, "", " ", 42, "bad\nthread", "x" * 257])
def test_invalid_ids_never_load_sdk_or_connect(local_socket, fake_sdk, thread_id):
    adapter = codex_app_server.CodexAppServerAdapter(local_socket)
    assert adapter.thread_context(thread_id) is None
    assert adapter.unavailable_reason == "invalid_thread_id"
    assert fake_sdk.calls == []


@pytest.mark.parametrize("kind", ["missing", "regular_file", "relative", "network"])
def test_invalid_socket_never_loads_sdk(tmp_path, fake_sdk, kind):
    path = tmp_path / "control.sock"
    if kind == "regular_file":
        path.write_text("not a socket")
    elif kind == "relative":
        path = "control.sock"
    elif kind == "network":
        path = "tcp://127.0.0.1:9000"
    adapter = codex_app_server.CodexAppServerAdapter(path)
    assert adapter.thread_context("thread-123") is None
    assert adapter.unavailable_reason == "socket_unavailable"
    assert fake_sdk.calls == []


def test_default_socket_uses_codex_home(tmp_path, monkeypatch, fake_sdk):
    monkeypatch.delenv("SESSION_HANDOFF_CODEX_APP_SERVER_SOCKET", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = tmp_path / "app-server-control" / "app-server-control.sock"
    path.parent.mkdir()
    with socket.socket(socket.AF_UNIX) as endpoint:
        endpoint.bind(str(path))
        adapter = codex_app_server.CodexAppServerAdapter()
        assert adapter.thread_context("thread-123") is not None
    assert fake_sdk.clients[0].config.launch_args_override[-1] == str(path)


def test_env_socket_override(local_socket, monkeypatch, fake_sdk):
    monkeypatch.setenv("CODEX_HOME", "/does/not/exist")
    monkeypatch.setenv("SESSION_HANDOFF_CODEX_APP_SERVER_SOCKET", str(local_socket))
    assert codex_app_server.CodexAppServerAdapter().thread_context("thread-123") is not None
    assert fake_sdk.clients[0].config.launch_args_override[-1] == str(local_socket)


def test_default_socket_uses_home_when_codex_home_is_unset(monkeypatch, fake_sdk):
    monkeypatch.delenv("SESSION_HANDOFF_CODEX_APP_SERVER_SOCKET", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    with tempfile.TemporaryDirectory(prefix="codex-home-") as home:
        monkeypatch.setenv("HOME", home)
        path = Path(home) / ".codex" / "app-server-control" / "app-server-control.sock"
        path.parent.mkdir(parents=True)
        with socket.socket(socket.AF_UNIX) as endpoint:
            endpoint.bind(str(path))
            assert codex_app_server.CodexAppServerAdapter().thread_context("thread-123") is not None
        assert fake_sdk.clients[0].config.launch_args_override[-1] == str(path)


def test_sdk_uninstalled_is_a_clear_optional_failure(local_socket, monkeypatch):
    def unavailable(name):
        raise ImportError("SDK is optional")

    monkeypatch.setattr(codex_app_server.importlib, "import_module", unavailable)
    adapter = codex_app_server.CodexAppServerAdapter(local_socket)
    assert adapter.thread_context("thread-123") is None
    assert adapter.unavailable_reason == "sdk_unavailable"


def test_unsupported_sdk_never_starts_proxy(local_socket, fake_sdk):
    fake_sdk.sdk.__version__ = "0.159.0"
    adapter = codex_app_server.CodexAppServerAdapter(local_socket)
    assert adapter.thread_context("thread-123") is None
    assert adapter.unavailable_reason == "sdk_version_unsupported"
    assert fake_sdk.clients == []


@pytest.mark.parametrize("phase", ["start", "initialize", "thread_read"])
def test_failed_operation_closes_proxy_without_leaking_exception(local_socket, fake_sdk, phase):
    def fail(*args, **kwargs):
        raise RuntimeError("private server error content")

    setattr(fake_sdk.Client, phase, fail)
    adapter = codex_app_server.CodexAppServerAdapter(local_socket)
    assert adapter.thread_context("thread-123") is None
    assert adapter.unavailable_reason == "connection_failed"
    assert fake_sdk.calls[-1] == ("close",)


@pytest.mark.parametrize("field, value", [
    ("id", "another-thread"),
    ("status", None),
    ("status", SimpleNamespace(root=SimpleNamespace(type="unknown"))),
])
def test_rejects_mismatched_or_malformed_response(local_socket, fake_sdk, field, value):
    original = fake_sdk.Client.thread_read

    def invalid(self, *args, **kwargs):
        response = original(self, *args, **kwargs)
        setattr(response.thread, field, value)
        return response

    fake_sdk.Client.thread_read = invalid
    adapter = codex_app_server.CodexAppServerAdapter(local_socket)
    assert adapter.thread_context("thread-123") is None
    assert adapter.unavailable_reason == "invalid_response"
    assert fake_sdk.calls[-1] == ("close",)


@pytest.mark.parametrize("status", ["notLoaded", "idle", "systemError", "active"])
def test_only_known_statuses_are_returned(local_socket, fake_sdk, status):
    original = fake_sdk.Client.thread_read

    def response(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        result.thread.status.root.type = status
        return result

    fake_sdk.Client.thread_read = response
    assert codex_app_server.CodexAppServerAdapter(local_socket).thread_context("thread-123")["status"] == status


@pytest.mark.parametrize("phase", ["start", "initialize", "thread_read"])
def test_timeout_closes_proxy_and_returns_promptly(local_socket, fake_sdk, phase):
    released = threading.Event()

    def blocked(*args, **kwargs):
        assert released.wait(1)

    def close(self):
        fake_sdk.calls.append(("close",))
        released.set()

    setattr(fake_sdk.Client, phase, blocked)
    fake_sdk.Client.close = close
    adapter = codex_app_server.CodexAppServerAdapter(
        local_socket, startup_timeout=0.03, request_timeout=0.03, close_timeout=0.03
    )
    started = time.monotonic()
    assert adapter.thread_context("thread-123") is None
    assert time.monotonic() - started < 0.5
    assert adapter.unavailable_reason == ("request_timeout" if phase == "thread_read" else "startup_timeout")
    assert released.is_set()


def test_close_timeout_discards_the_result(local_socket, fake_sdk):
    release = threading.Event()

    def blocked_close(self):
        assert release.wait(1)

    fake_sdk.Client.close = blocked_close
    adapter = codex_app_server.CodexAppServerAdapter(local_socket, close_timeout=0.03)
    try:
        started = time.monotonic()
        assert adapter.thread_context("thread-123") is None
        assert time.monotonic() - started < 0.5
        assert adapter.unavailable_reason == "close_timeout"
    finally:
        release.set()


def test_startup_finishing_after_timeout_still_closes_proxy(local_socket, fake_sdk):
    release = threading.Event()
    late_closed = threading.Event()

    def late_start(self):
        assert release.wait(1)
        self.started_late = True

    def close(self):
        fake_sdk.calls.append(("close",))
        if getattr(self, "started_late", False):
            late_closed.set()

    fake_sdk.Client.start = late_start
    fake_sdk.Client.close = close
    adapter = codex_app_server.CodexAppServerAdapter(local_socket, startup_timeout=0.03)
    assert adapter.thread_context("thread-123") is None
    assert adapter.unavailable_reason == "startup_timeout"
    release.set()
    assert late_closed.wait(0.5)
    assert ("initialize",) not in fake_sdk.calls
    assert ("thread_read", "thread-123", False) not in fake_sdk.calls


def test_failed_close_discards_the_result(local_socket, fake_sdk):
    def failed_close(self):
        raise RuntimeError("private transport failure")

    fake_sdk.Client.close = failed_close
    adapter = codex_app_server.CodexAppServerAdapter(local_socket)
    assert adapter.thread_context("thread-123") is None
    assert adapter.unavailable_reason == "close_failed"


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True])
def test_timeouts_must_be_finite_positive_numbers(value):
    with pytest.raises(ValueError):
        codex_app_server.CodexAppServerAdapter(startup_timeout=value)


def test_each_call_closes_its_own_proxy(local_socket, fake_sdk):
    adapter = codex_app_server.CodexAppServerAdapter(local_socket)
    assert adapter.thread_context("thread-123") is not None
    assert adapter.thread_context("thread-123") is not None
    assert len(fake_sdk.clients) == 2
    assert fake_sdk.calls.count(("close",)) == 2
