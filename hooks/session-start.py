#!/usr/bin/env python3
"""Show the one-time telemetry opt-in reminder for marketplace installs."""

import json
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from server import telemetry  # noqa: E402


def main() -> int:
    try:
        if telemetry.do_not_track_enabled():
            print("{}")
            return 0
        config = telemetry.load_config()
        if telemetry.consent_state(config) == "enabled":
            telemetry.session_start_flush()
            print("{}")
            return 0
        if not telemetry.claim_consent_prompt():
            print("{}")
            return 0

        print(json.dumps({
            "systemMessage": (
                "session-handoff telemetry is off by default. Reply with exactly one of: "
                "`session-handoff telemetry yes` to enable anonymous aggregate telemetry, or "
                "`session-handoff telemetry no` to decline. "
                f"Details: {telemetry.TELEMETRY_DETAILS_URL}"
            ),
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
            }
        }))
    except Exception:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
