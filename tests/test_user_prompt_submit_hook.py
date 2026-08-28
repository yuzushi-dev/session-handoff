import json
import os
import subprocess
import sys
from pathlib import Path

from server import telemetry


ROOT = Path(__file__).parents[1]
HOOK = ROOT / "hooks/user-prompt-submit.py"


def run_hook(home, prompt):
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": prompt}),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_exact_yes_response_enables_telemetry(tmp_path):
    telemetry.write_config(tmp_path, telemetry.asked_config())

    result = run_hook(tmp_path, "session-handoff telemetry yes")

    assert result.returncode == 0
    assert json.loads(result.stdout) == {}
    assert telemetry.load_config(tmp_path)["consent_state"] == "enabled"


def test_exact_no_response_records_a_final_decline(tmp_path):
    telemetry.write_config(tmp_path, telemetry.asked_config())

    result = run_hook(tmp_path, "session-handoff telemetry no")

    assert result.returncode == 0
    assert json.loads(result.stdout) == {}
    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()


def test_chat_response_is_exact_and_does_not_match_partial_text(tmp_path):
    telemetry.write_config(tmp_path, telemetry.asked_config())

    result = run_hook(tmp_path, "please session-handoff telemetry yes")

    assert result.returncode == 0
    assert json.loads(result.stdout) == {}
    assert telemetry.load_config(tmp_path) == telemetry.asked_config()


def test_chat_response_is_case_and_whitespace_sensitive(tmp_path):
    telemetry.write_config(tmp_path, telemetry.asked_config())

    result = run_hook(tmp_path, "Session-Handoff telemetry yes")

    assert result.returncode == 0
    assert json.loads(result.stdout) == {}
    assert telemetry.load_config(tmp_path) == telemetry.asked_config()


def test_decline_cannot_be_reversed_by_a_later_chat_yes(tmp_path):
    telemetry.write_config(tmp_path, telemetry.disabled_config())

    result = run_hook(tmp_path, "session-handoff telemetry yes")

    assert result.returncode == 0
    assert json.loads(result.stdout) == {}
    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()


def test_do_not_track_prevents_chat_consent_without_rewriting_state(tmp_path, monkeypatch):
    telemetry.write_config(tmp_path, telemetry.asked_config())
    monkeypatch.setenv("DO_NOT_TRACK", "1")

    result = run_hook(tmp_path, "session-handoff telemetry yes")

    assert result.returncode == 0
    assert json.loads(result.stdout) == {}
    assert telemetry.load_config(tmp_path) == telemetry.asked_config()


def test_invalid_hook_input_fails_open(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json",
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {}
    assert telemetry.load_config(tmp_path) is None
