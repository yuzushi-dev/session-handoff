import json
from pathlib import Path
from urllib.request import urlopen

import pytest

import benchmark.claude_proxy as claude_proxy
from benchmark.claude_proxy import isolated_proxy


SKIP_PROXY_INTEGRATION = pytest.mark.skipif(
    not claude_proxy.PROXY_BINARY.exists(),
    reason="exact pinned claude-code-proxy 0.1.22 binary is unavailable",
)


def test_proxy_requires_explicit_authorization_before_creating_state(tmp_path):
    state = tmp_path / "state"
    with pytest.raises(ValueError, match="authorization"):
        with isolated_proxy(state, tmp_path / "auth.json"):
            pytest.fail("unauthorized proxy started")
    assert not state.exists()


def test_proxy_rejects_symlink_credentials(tmp_path):
    auth = tmp_path / "auth.json"
    auth.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="regular file"):
        with isolated_proxy(tmp_path / "state", auth, authorized=True):
            pytest.fail("symlink credentials accepted")


@SKIP_PROXY_INTEGRATION
def test_isolated_proxy_health_with_synthetic_credentials(tmp_path):
    auth = tmp_path / "auth.json"
    content = json.dumps(
        {
            "access": "synthetic",
            "refresh": "synthetic",
            "expires": 4102444800000,
            "accountId": "synthetic",
        }
    )
    auth.write_text(content)
    auth.chmod(0o600)
    with isolated_proxy(tmp_path / "state", auth, authorized=True) as endpoint:
        with urlopen(endpoint + "/healthz", timeout=2) as response:
            assert response.status == 200
    assert auth.read_text() == content
    assert Path(tmp_path / "state").is_dir()


@SKIP_PROXY_INTEGRATION
def test_proxy_does_not_accept_readiness_from_unowned_listener(tmp_path, monkeypatch):
    import benchmark.claude_proxy as module

    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    monkeypatch.setattr(module, "_owns_listener", lambda *_: False, raising=False)
    with pytest.raises(RuntimeError, match="listener ownership"):
        with isolated_proxy(tmp_path / "state", auth, authorized=True):
            pytest.fail("unowned listener accepted")


def test_proxy_rejects_changed_binary_before_execution(tmp_path, monkeypatch):
    import benchmark.claude_proxy as module

    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    binary = tmp_path / "proxy"
    binary.write_text('#!/bin/sh\necho "claude-code-proxy 0.1.22"\n')
    binary.chmod(0o700)
    monkeypatch.setattr(module, "PROXY_BINARY", binary, raising=False)
    with pytest.raises(ValueError, match="hash"):
        with isolated_proxy(tmp_path / "state", auth, authorized=True):
            pytest.fail("substituted proxy accepted")


@SKIP_PROXY_INTEGRATION
def test_proxy_state_is_ephemeral_and_auth_mount_read_only(tmp_path, monkeypatch):
    import benchmark.claude_proxy as module

    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    real_popen = module.subprocess.Popen

    def checked_popen(command, **kwargs):
        if command[0] == "/usr/bin/bwrap":
            assert str(tmp_path / "state") not in command
            index = command.index(str(auth))
            assert command[index - 1] == "--ro-bind"
        return real_popen(command, **kwargs)

    monkeypatch.setattr(module.subprocess, "Popen", checked_popen)
    with isolated_proxy(tmp_path / "state", auth, authorized=True):
        pass


def test_listener_check_rejects_nonloopback_bind(monkeypatch):
    import os
    import sys
    from types import SimpleNamespace
    import benchmark.claude_proxy as module

    output = (
        f'LISTEN 0 1024 0.0.0.0:9999 0.0.0.0:* users:(("proxy",pid={os.getpid()},fd=4))'
    )
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=output)
    )
    assert not module._owns_listener(
        SimpleNamespace(pid=os.getpgrp()), 9999, sys.executable
    )
