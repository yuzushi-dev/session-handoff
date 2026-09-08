import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest


PROXY = Path("/home/cristina/.local/bin/claude-code-proxy")
PROXY_VERSION = "claude-code-proxy 0.1.22"
COMPACT_MARKER = "You are a helpful AI assistant tasked with summarizing conversations"


def _unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _proxy_binary():
    if not PROXY.is_file() or not os.access(PROXY, os.X_OK):
        pytest.skip("claude-code-proxy is unavailable")
    version = subprocess.run(
        [str(PROXY), "--version"], capture_output=True, text=True, check=True
    )
    assert version.stdout.strip() == PROXY_VERSION
    return PROXY


def _backend():
    captured = {}
    received = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers["Content-Length"])
            request = json.loads(self.rfile.read(size))
            captured.update(
                model=request.get("model"),
                effort=request.get("reasoning", {}).get("effort"),
            )
            received.set()
            self.send_response(400)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, captured, received


def _wait_for_proxy(port):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.2):
                return
        except URLError:
            time.sleep(0.1)
    raise AssertionError("disposable claude-code-proxy did not become healthy")


def _stop(process):
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize(
    ("compact_effort", "system", "expected_effort"),
    [
        ("off", None, "xhigh"),
        (None, COMPACT_MARKER, "low"),
        ("off", COMPACT_MARKER, "xhigh"),
    ],
)
def test_installed_proxy_forces_and_preserves_xhigh(
    tmp_path, compact_effort, system, expected_effort
):
    proxy = _proxy_binary()
    config = tmp_path / "config"
    auth = config / "codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps(
            {
                "access": "synthetic-access",
                "refresh": "synthetic-refresh",
                "expires": 4102444800000,
                "accountId": "synthetic-account",
            }
        )
    )
    backend, thread, captured, received = _backend()
    proxy_port = _unused_port()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "CCP_CONFIG_DIR": str(config),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "CCP_BIND_ADDRESS": "127.0.0.1",
        "CCP_CODEX_BASE_URL": f"http://127.0.0.1:{backend.server_port}/responses",
        "CCP_CODEX_TRANSPORT": "http",
        "CCP_CODEX_EFFORT": "xhigh",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    if compact_effort:
        env["CCP_COMPACT_EFFORT"] = compact_effort
    process = subprocess.Popen(
        [str(proxy), "serve", "--port", str(proxy_port), "--no-monitor"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_proxy(proxy_port)
        body = {
            "model": "gpt-5.6-luna",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "synthetic"}],
            "output_config": {"effort": "low"},
        }
        if system:
            body["system"] = system
        request = Request(
            f"http://127.0.0.1:{proxy_port}/v1/messages",
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            urlopen(request, timeout=10)
        except HTTPError:
            pass
        assert received.wait(5)
        assert captured == {"model": "gpt-5.6-luna", "effort": expected_effort}
    finally:
        _stop(process)
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=5)
    stdout, stderr = process.communicate()
    assert "synthetic-access" not in stdout + stderr
    assert "synthetic-refresh" not in stdout + stderr
