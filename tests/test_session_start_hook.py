import json
import os
import subprocess
import sys
from pathlib import Path

from server import telemetry


ROOT = Path(__file__).parents[1]
HOOK = ROOT / "hooks/session-start.py"


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


def test_session_start_hook_is_silent_after_declining(tmp_path):
    telemetry.write_config(tmp_path, telemetry.disabled_config())

    result = run_hook(tmp_path)

    assert result.returncode == 0
    assert json.loads(result.stdout) == {}


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
    assert "hooks/" in package["files"]
