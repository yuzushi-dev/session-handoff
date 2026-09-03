#!/usr/bin/env python3
"""Show the one-time telemetry opt-in reminder for marketplace installs."""

import json
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from server import checkpoint, telemetry  # noqa: E402


def _hook_input() -> dict:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError, UnicodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _output(*, context: str | None = None, system_message: str | None = None) -> None:
    if not context and not system_message:
        print("{}")
        return
    output = {"hookSpecificOutput": {"hookEventName": "SessionStart"}}
    if context:
        output["hookSpecificOutput"]["additionalContext"] = context
    if system_message:
        output["systemMessage"] = system_message
    print(json.dumps(output))


def main() -> int:
    event = _hook_input()
    try:
        context = checkpoint.compact_context(event)
        checkpoint.record_session_start(event, context)
    except Exception:
        context = None

    try:
        if telemetry.do_not_track_enabled():
            _output(context=context)
            return 0
        config = telemetry.load_config()
        if telemetry.consent_state(config) == "enabled":
            telemetry.session_start_flush()
            _output(context=context)
            return 0
        if not telemetry.claim_consent_prompt():
            _output(context=context)
            return 0

        _output(
            context=context,
            system_message=(
                "session-handoff telemetry is off by default. Reply with exactly one of: "
                "`session-handoff telemetry yes` to enable anonymous aggregate telemetry, or "
                "`session-handoff telemetry no` to decline. "
                f"Details: {telemetry.TELEMETRY_DETAILS_URL}"
            ),
        )
    except Exception:
        _output(context=context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
