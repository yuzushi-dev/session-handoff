import json
import builtins
import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from server import telemetry
from server.checkpoint import capture_checkpoint


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


def run_hook(home, payload=None):
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        text=True,
        capture_output=True,
        check=False,
        env=env,
        input=json.dumps(payload) if payload is not None else None,
    )


@pytest.mark.parametrize(
    "error",
    [UnicodeError("invalid input"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")],
)
def test_session_start_hook_parsing_fails_open_on_unicode_errors(error, monkeypatch):
    hook = load_hook()

    class RaisingInput:
        def read(self):
            raise error

    monkeypatch.setattr(hook.sys, "stdin", RaisingInput())

    assert hook._hook_input() == {}


def test_session_start_hook_nudges_until_consent_is_recorded(tmp_path):
    result = run_hook(tmp_path)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    message = payload["systemMessage"]
    assert payload["hookSpecificOutput"] == {"hookEventName": "SessionStart"}
    assert "systemMessage" not in payload["hookSpecificOutput"]
    assert "anonymous aggregate telemetry" in message
    assert "session-handoff telemetry yes" in message
    assert "session-handoff telemetry no" in message
    assert telemetry.load_config(tmp_path)["consent_state"] == "asked"

    second = run_hook(tmp_path)
    assert json.loads(second.stdout) == {}


def test_session_start_hook_uses_protocol_message_even_when_streams_are_tty(tmp_path, monkeypatch):
    hook = load_hook()
    output = TTYStream()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hook.sys, "stdin", TTYStream("unexpected input\n"))
    monkeypatch.setattr(hook.sys, "stdout", output)

    assert hook.main() == 0
    payload = json.loads(output.getvalue())
    assert payload["hookSpecificOutput"] == {"hookEventName": "SessionStart"}
    assert telemetry.load_config(tmp_path)["consent_state"] == "asked"


def test_session_start_hook_does_not_reprompt_after_decline_when_consent_version_changes(tmp_path, monkeypatch):
    hook = load_hook()
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hook.telemetry, "CONSENT_VERSION", telemetry.CONSENT_VERSION + 1)
    monkeypatch.setattr(hook.sys, "stdin", TTYStream("unexpected input\n"))
    monkeypatch.setattr(hook.sys, "stdout", TTYStream())

    assert hook.main() == 0
    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()


def test_session_start_hook_claims_consent_before_emitting_message(tmp_path, monkeypatch):
    hook = load_hook()
    monkeypatch.setenv("HOME", str(tmp_path))
    output = io.StringIO()
    monkeypatch.setattr(hook.sys, "stdout", output)
    observed = []
    original_claim = hook.telemetry.claim_consent_prompt

    def claim():
        result = original_claim()
        observed.append(telemetry.load_config(tmp_path)["consent_state"])
        return result

    monkeypatch.setattr(hook.telemetry, "claim_consent_prompt", claim)

    assert hook.main() == 0
    assert observed == ["asked"]
    assert json.loads(output.getvalue())["systemMessage"]


def test_session_start_hook_does_not_emit_question_when_state_write_fails(tmp_path, monkeypatch):
    hook = load_hook()
    monkeypatch.setenv("HOME", str(tmp_path))
    output = io.StringIO()
    monkeypatch.setattr(hook.sys, "stdout", output)
    monkeypatch.setattr(
        hook.telemetry,
        "claim_consent_prompt",
        lambda: (_ for _ in ()).throw(telemetry.TelemetryConfigError("write failed")),
    )

    assert hook.main() == 0
    assert json.loads(output.getvalue()) == {}


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

    user_prompt_submit = hook_config["hooks"]["UserPromptSubmit"]
    assert "hooks/user-prompt-submit.py" in user_prompt_submit[0]["hooks"][0]["command"]
    assert "CLAUDE_PLUGIN_ROOT" in user_prompt_submit[0]["hooks"][0]["command"]

    pre_compact = hook_config["hooks"]["PreCompact"]
    assert pre_compact[0]["matcher"] == "auto|manual"
    assert "server/checkpoint.py" in pre_compact[0]["hooks"][0]["command"]

    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert "hooks/hooks.json" in package["files"]
    assert "hooks/session-start.py" in package["files"]
    assert "hooks/user-prompt-submit.py" in package["files"]
    assert "server/*.py" in package["files"]


def test_consent_notice_links_to_the_public_details_url(tmp_path):
    """A consent request the user cannot verify is not an informed one: the notice
    must carry a resolvable URL, not a repo-relative path that an installed plugin
    does not have."""
    assert telemetry.TELEMETRY_DETAILS_URL.startswith("https://")
    assert telemetry.TELEMETRY_DETAILS_URL in telemetry.CONSENT_PROMPT

    result = run_hook(tmp_path)

    assert result.returncode == 0
    message = json.loads(result.stdout)["systemMessage"]
    assert telemetry.TELEMETRY_DETAILS_URL in message


def test_session_start_hook_reinjects_checkpoint_after_compact(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    checkpoint = capture_checkpoint(
        {
            "cwd": str(workspace),
            "session_id": "session-123",
            "hook_event_name": "PreCompact",
            "trigger": "auto",
        },
        home=home,
    )

    result = run_hook(
        home,
        {
            "cwd": str(workspace),
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": "session-123",
        },
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert checkpoint["path"] in context
    assert "non-semantic" in context

    events = next((home / ".local/state/session-handoff/checkpoints").iterdir()) / "events.jsonl"
    record = json.loads(events.read_text(encoding="utf-8").splitlines()[-1])
    assert record["event"] == "session_start"
    assert record["trigger"] == "compact"
    assert record["checkpoint_path"] == checkpoint["path"]
    assert record["checkpoint_bytes"] == Path(checkpoint["path"]).stat().st_size
    assert record["injected"] is True
    assert record["injected_bytes"] > 0


def test_session_start_hook_records_non_compact_without_injection(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"

    result = run_hook(
        home,
        {
            "cwd": str(workspace),
            "hook_event_name": "SessionStart",
            "source": "startup",
            "session_id": "session-123",
        },
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["systemMessage"]
    events = next((home / ".local/state/session-handoff/checkpoints").iterdir()) / "events.jsonl"
    record = json.loads(events.read_text(encoding="utf-8"))
    assert record["event"] == "session_start"
    assert record["trigger"] == "startup"
    assert record["checkpoint_path"] is None
    assert record["checkpoint_bytes"] == 0
    assert record["injected"] is False
    assert record["injected_bytes"] == 0
