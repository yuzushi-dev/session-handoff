import json
import builtins
import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

from server import telemetry


ROOT = Path(__file__).parents[1]
HOOK = ROOT / "hooks/session-start.py"


class TTYStream(io.StringIO):
    def isatty(self):
        return True


def load_hook():
    spec = importlib.util.spec_from_file_location("session_start_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_hook(home):
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_session_start_hook_nudges_until_consent_is_recorded(tmp_path):
    result = run_hook(tmp_path)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    message = payload["hookSpecificOutput"]["systemMessage"]
    assert "anonymous aggregate telemetry" in message
    assert "telemetry enable" in message
    assert str(ROOT / "bin/session-handoff") in message


def test_session_start_hook_prompts_once_when_both_streams_are_tty(tmp_path, monkeypatch):
    hook = load_hook()
    output = TTYStream()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hook.sys, "stdin", TTYStream("yes\n"))
    monkeypatch.setattr(hook.sys, "stdout", output)

    assert hook.main() == 0
    assert telemetry.load_config(tmp_path)["enabled"] is True
    assert json.loads(output.getvalue()) == {}


def test_session_start_hook_decline_is_persisted_without_reminder(tmp_path, monkeypatch):
    hook = load_hook()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hook.sys, "stdin", TTYStream("no\n"))
    monkeypatch.setattr(hook.sys, "stdout", TTYStream())

    assert hook.main() == 0
    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()


def test_session_start_hook_reprompts_for_an_older_consent_version(tmp_path, monkeypatch):
    hook = load_hook()
    old_config = telemetry.disabled_config()
    old_config["prompted_consent_version"] = telemetry.CONSENT_VERSION - 1
    telemetry.write_config(tmp_path, old_config)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hook.sys, "stdin", TTYStream("no\n"))
    monkeypatch.setattr(hook.sys, "stdout", TTYStream())

    assert hook.main() == 0
    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()


def test_session_start_hook_does_not_prompt_without_tty(tmp_path, monkeypatch):
    hook = load_hook()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hook.sys, "stdin", TTYStream())
    monkeypatch.setattr(hook.sys, "stdout", io.StringIO())
    monkeypatch.setattr(builtins, "input", lambda _prompt: pytest.fail("unexpected prompt"))

    assert hook.main() == 0
    assert telemetry.load_config(tmp_path) is None


def test_session_start_hook_does_not_prompt_or_flush_when_do_not_track_is_set(tmp_path, monkeypatch):
    hook = load_hook()
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    calls = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setattr(hook.telemetry, "session_start_flush", lambda: calls.append(True))
    monkeypatch.setattr(hook.sys, "stdin", TTYStream())
    monkeypatch.setattr(hook.sys, "stdout", TTYStream())
    monkeypatch.setattr(builtins, "input", lambda _prompt: pytest.fail("unexpected prompt"))

    assert hook.main() == 0
    assert calls == []
    assert telemetry.load_config(tmp_path)["enabled"] is True


def test_session_start_hook_is_silent_after_declining(tmp_path):
    telemetry.write_config(tmp_path, telemetry.disabled_config())

    result = run_hook(tmp_path)

    assert result.returncode == 0
    assert json.loads(result.stdout) == {}


def test_session_start_hook_spawns_flush_for_enabled_consent(tmp_path, monkeypatch):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    calls = []
    monkeypatch.setattr(telemetry, "spawn_detached_flush", lambda *args: calls.append(args))
    assert telemetry.session_start_flush(tmp_path) is None
    assert calls == [(tmp_path / telemetry.STATE_PATH / telemetry._QUEUE_NAME, telemetry.config_path(tmp_path))]


def test_session_start_hook_fails_open_on_invalid_config(tmp_path):
    config = tmp_path / telemetry.CONFIG_PATH
    config.parent.mkdir(parents=True)
    config.write_text("not json", encoding="utf-8")

    result = run_hook(tmp_path)

    assert result.returncode == 0
    assert json.loads(result.stdout) == {}


def test_plugin_manifest_surfaces_include_session_start_hook():
    hook_config = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
    session_start = hook_config["hooks"]["SessionStart"]

    assert session_start
    command = session_start[0]["hooks"][0]["command"]
    assert "hooks/session-start.py" in command
    assert "CLAUDE_PLUGIN_ROOT" in command

    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert "hooks/hooks.json" in package["files"]
    assert "hooks/session-start.py" in package["files"]


def test_consent_notice_links_to_the_public_details_url(tmp_path):
    """A consent request the user cannot verify is not an informed one: the notice
    must carry a resolvable URL, not a repo-relative path that an installed plugin
    does not have."""
    assert telemetry.TELEMETRY_DETAILS_URL.startswith("https://")
    assert telemetry.TELEMETRY_DETAILS_URL in telemetry.CONSENT_PROMPT

    result = run_hook(tmp_path)

    assert result.returncode == 0
    message = json.loads(result.stdout)["hookSpecificOutput"]["systemMessage"]
    assert telemetry.TELEMETRY_DETAILS_URL in message
