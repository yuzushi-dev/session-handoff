import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from server import telemetry
from server import setup as onboarding_setup


ROOT = Path(__file__).resolve().parents[1]


_CLIENT_SESSION_VARS = {"CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"}


def run_start(home, *, extra_env=None):
    # Also strip the client-session vars: this test may itself be running
    # inside a Claude Code or Codex session, and the hook must not mistake
    # that ambient state for the session under test detecting a client.
    env = {key: value for key, value in os.environ.items()
           if key not in {"SESSION_HANDOFF_CONTROL", "DO_NOT_TRACK", *_CLIENT_SESSION_VARS}}
    env.update(HOME=str(home), SESSION_HANDOFF_HOME=str(home),
               **(extra_env or {}))
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks/session-start.py")],
        input='{"hook_event_name":"SessionStart","source":"startup"}',
        text=True, capture_output=True, env=env, timeout=10, check=True,
    )
    return json.loads(result.stdout)


def test_first_start_offers_setup_and_separate_telemetry_without_installing(tmp_path):
    result = run_start(tmp_path)
    message = result["systemMessage"]
    assert "Automatic session switching (optional)" in message
    for client in ("claude", "codex"):
        assert shlex.join(["python3", str(ROOT / "bin/session-handoff"),
                           "setup", "--client", client]) in message
    assert "Restart" in message
    assert "Telemetry (optional)" in message
    assert "telemetry yes" in message and "telemetry no" in message
    assert "npx" not in message
    assert not (tmp_path / ".config/session-handoff/state.json").exists()
    assert telemetry.load_config(tmp_path)["enabled"] is False
    assert run_start(tmp_path) == {}


def _stub_client_bin(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for client in ("claude", "codex"):
        executable = bindir / client
        executable.write_text("#!/usr/bin/env python3\nimport sys\nprint('client ' + ' '.join(sys.argv[1:]))\n")
        executable.chmod(0o755)
    (bindir / "python3").symlink_to(sys.executable)
    return bindir


def test_first_start_auto_installs_the_detected_client(tmp_path):
    bindir = _stub_client_bin(tmp_path)
    result = run_start(tmp_path, extra_env={"PATH": str(bindir), "CLAUDE_CODE_SESSION_ID": "sess-1"})
    message = result["systemMessage"]
    assert "Installing it for claude in the background" in message
    assert shlex.join(["python3", str(ROOT / "bin/session-handoff"),
                       "setup", "--client", "codex"]) in message
    assert shlex.join(["python3", str(ROOT / "bin/session-handoff"), "uninstall"]) in message
    assert "asks for confirmation" not in message

    state_path = tmp_path / ".config/session-handoff/state.json"
    deadline = time.time() + 5
    while time.time() < deadline and not state_path.is_file():
        time.sleep(0.05)
    assert json.loads(state_path.read_text())["clients"] == ["claude"]
    assert telemetry.load_config(tmp_path)["enabled"] is False
    assert run_start(tmp_path, extra_env={"PATH": str(bindir)}) == {}


def test_spawn_failure_releases_notice_for_retry(tmp_path, monkeypatch):
    from server import onboarding

    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("SESSION_HANDOFF_CONTROL", raising=False)
    monkeypatch.setattr(onboarding.shutil, "which", lambda client: "/tmp/fake-client")
    calls = []

    def fail_once(*args):
        calls.append(args)
        raise OSError("spawn failed")

    monkeypatch.setattr(onboarding, "_spawn_detached_setup", fail_once)
    with pytest.raises(OSError, match="spawn failed"):
        onboarding.launcher_notice(ROOT)

    marker = tmp_path / ".config/session-handoff/launcher-notice-v1"
    assert not marker.exists()

    monkeypatch.setattr(onboarding, "_spawn_detached_setup", lambda *args: calls.append(args))
    assert onboarding.launcher_notice(ROOT)
    assert len(calls) == 2


def test_failed_detached_setup_releases_notice_for_retry(tmp_path, monkeypatch):
    from server import onboarding

    plugin_root = tmp_path / "plugin"
    cli = plugin_root / "bin/session-handoff"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/usr/bin/env python3\nraise SystemExit(7)\n", encoding="utf-8")
    cli.chmod(0o700)
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path / "home"))
    marker = Path(os.environ["SESSION_HANDOFF_HOME"]) / ".config/session-handoff/launcher-notice-v1"
    marker.parent.mkdir(parents=True)
    marker.touch()

    onboarding._spawn_detached_setup(plugin_root, "claude")
    deadline = time.time() + 3
    while time.time() < deadline and marker.exists():
        time.sleep(0.05)
    assert not marker.exists()


def test_pre_existing_notice_marker_blocks_auto_install(tmp_path, monkeypatch):
    # Simulates a user who already saw the pre-upgrade manual notice (the
    # marker exists) but never ran setup (no state.json). The upgrade to
    # auto-install must not silently wrap their launcher without ever asking.
    from server.onboarding import launcher_notice

    bindir = _stub_client_bin(tmp_path)
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    monkeypatch.delenv("SESSION_HANDOFF_CONTROL", raising=False)
    marker_dir = tmp_path / ".config/session-handoff"
    marker_dir.mkdir(parents=True)
    (marker_dir / "launcher-notice-v1").touch()

    assert launcher_notice(ROOT) is None
    time.sleep(0.5)
    assert not (tmp_path / ".config/session-handoff/state.json").exists()


def test_state_json_present_suppresses_the_notice_entirely(tmp_path, monkeypatch):
    from server.onboarding import launcher_notice

    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.delenv("SESSION_HANDOFF_CONTROL", raising=False)
    state_path = tmp_path / onboarding_setup.STATE_PATH
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}")

    assert launcher_notice(ROOT) is None
    assert not (tmp_path / ".config/session-handoff/launcher-notice-v1").exists()


def test_detect_client_reads_the_host_session_env(monkeypatch):
    from server.onboarding import _detect_client

    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    assert _detect_client() is None
    monkeypatch.setenv("CODEX_THREAD_ID", "t-1")
    assert _detect_client() == "codex"
    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-1")
    assert _detect_client() == "claude"


def test_detect_client_refuses_to_guess_when_both_env_vars_are_set(monkeypatch):
    from server.onboarding import _detect_client

    monkeypatch.setenv("CODEX_THREAD_ID", "t-1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-1")
    assert _detect_client() is None


def test_auto_install_falls_back_to_manual_notice_when_client_is_not_on_path(tmp_path, monkeypatch):
    # CLAUDE_CODE_SESSION_ID is set (the host says this is a Claude session)
    # but the detected client binary isn't actually reachable from the hook's
    # PATH: spawning `setup --client claude --yes` would be doomed, and the
    # one-shot notice marker would then be burned with nothing to show for
    # it. Must fall back to the manual dual-command notice instead.
    from server.onboarding import launcher_notice

    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # deliberately has no "claude"
    (tmp_path / "empty-bin").mkdir()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    monkeypatch.delenv("SESSION_HANDOFF_CONTROL", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)

    message = launcher_notice(ROOT)

    assert "Automatic session switching (optional)" in message
    assert "Installing it for" not in message
    time.sleep(0.3)
    assert not (tmp_path / ".config/session-handoff/state.json").exists()


@pytest.mark.parametrize("state", ["declined", "asked", "do_not_track", "invalid"])
def test_setup_offer_is_independent_of_telemetry(tmp_path, state):
    extra = {}
    if state == "do_not_track":
        extra["DO_NOT_TRACK"] = "1"
    elif state == "invalid":
        path = tmp_path / telemetry.CONFIG_PATH
        path.parent.mkdir(parents=True)
        path.write_text("invalid")
    else:
        telemetry.write_config(tmp_path, telemetry.disabled_config() if state == "declined"
                               else telemetry.asked_config())
    result = run_start(tmp_path, extra_env=extra)
    assert "Automatic session switching (optional)" in result["systemMessage"]
    assert "Telemetry (optional)" not in result["systemMessage"]


def test_supervised_session_does_not_offer_setup(tmp_path):
    result = run_start(tmp_path, extra_env={"SESSION_HANDOFF_CONTROL": "/isolated/control"})
    assert "Automatic session switching" not in result["systemMessage"]
    assert "telemetry yes" in result["systemMessage"]


def test_setup_notice_quotes_paths_and_handles_unwritable_state(tmp_path, monkeypatch):
    from server.onboarding import launcher_notice

    root = tmp_path / "plugin with 'quotes' and $(commands)"
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SESSION_HANDOFF_CONTROL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    message = launcher_notice(root)
    assert shlex.join(["python3", str(root / "bin/session-handoff"),
                       "setup", "--client", "codex"]) in message
    assert launcher_notice(root) is None
    bad_home = tmp_path / "not-a-directory"
    bad_home.write_text("preserve")
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(bad_home))
    assert launcher_notice(root) is None
    assert bad_home.read_text() == "preserve"


@pytest.mark.parametrize("parent", [".config", ".config/session-handoff"])
def test_setup_notice_rejects_symlinked_state_parents(tmp_path, monkeypatch, parent):
    from server.onboarding import launcher_notice

    home, outside = tmp_path / "home", tmp_path / "outside"
    outside.mkdir()
    link = home / parent
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(home))
    monkeypatch.delenv("SESSION_HANDOFF_CONTROL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    assert launcher_notice(ROOT) is None
    assert not list(outside.iterdir())


def test_setup_notice_state_is_private_and_uses_explicit_home(tmp_path, monkeypatch):
    from server.onboarding import launcher_notice

    monkeypatch.setenv("SESSION_HANDOFF_HOME", str(tmp_path))
    monkeypatch.delenv("SESSION_HANDOFF_CONTROL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: (_ for _ in ()).throw(RuntimeError("no home")))
    previous = os.umask(0)
    try:
        assert launcher_notice(ROOT)
    finally:
        os.umask(previous)
    state = tmp_path / ".config/session-handoff"
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "launcher-notice-v1").stat().st_mode & 0o777 == 0o600


def test_displayed_setup_commands_work_without_node(tmp_path):
    from server.setup import BUNDLE_ENTRIES

    source = tmp_path / "marketplace plugin"
    for name in BUNDLE_ENTRIES:
        origin, target = ROOT / name, source / name
        if not origin.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_dir():
            shutil.copytree(origin, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(origin, target)
    home, bindir = tmp_path / "home", tmp_path / "home/bin"
    bindir.mkdir(parents=True)
    (bindir / "python3").symlink_to(sys.executable)
    for client in ("claude", "codex"):
        executable = bindir / client
        executable.write_text("#!/usr/bin/env python3\nimport sys\nprint('client ' + ' '.join(sys.argv[1:]))\n")
        executable.chmod(0o755)
    env = {"HOME": str(home), "SESSION_HANDOFF_HOME": str(home), "PATH": str(bindir),
           "PYTHONDONTWRITEBYTECODE": "1"}
    assert all(shutil.which(name, path=env["PATH"]) is None for name in ("node", "npm", "npx"))

    def run(args, payload=""):
        return subprocess.run(args, input=payload, text=True, capture_output=True,
                              env=env, timeout=15, check=True).stdout

    output = json.loads(run(["python3", str(source / "hooks/session-start.py")], "{}"))
    commands = [shlex.split(line) for line in output["systemMessage"].splitlines()
                if line.startswith("python3 ")]
    assert len(commands) == 2
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "handoff_setup", "arguments": {}}}
    reply = json.loads(run(["python3", str(source / "server/handoff_mcp.py")],
                           json.dumps(request) + "\n"))
    info = json.loads(reply["result"]["content"][0]["text"])
    assert [shlex.split(command) for command in info["setup_commands"].values()] == commands
    for command in commands:
        assert "Setup complete" in run([*command, "--yes"])
        client = command[-1]
        assert run([str(bindir / client), "--version"]).rstrip().endswith("--version")
    assert telemetry.load_config(home)["enabled"] is False
    cli = ["python3", str(source / "bin/session-handoff"), "telemetry"]
    assert "Telemetry enabled" in run([*cli, "yes"])
    assert telemetry.load_config(home)["enabled"] is True
    assert "Telemetry disabled" in run([*cli, "no"])
    assert telemetry.load_config(home)["enabled"] is False
