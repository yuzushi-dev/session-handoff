#!/usr/bin/env python3
"""Show the one-time telemetry opt-in reminder for marketplace installs."""

import json
import shlex
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
        if config is not None and config["enabled"]:
            telemetry.session_start_flush()
            print("{}")
            return 0
        if config is not None and config["prompted_consent_version"] >= telemetry.CONSENT_VERSION:
            print("{}")
            return 0

        if sys.stdin.isatty() and sys.stdout.isatty():
            def read_consent(prompt):
                print(prompt, file=sys.stderr, end="", flush=True)
                return sys.stdin.readline()

            telemetry.request_consent(None, interactive=True, input_fn=read_consent)
            print("{}")
            return 0

        cli = shlex.quote(str(PLUGIN_ROOT / "bin/session-handoff"))
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "systemMessage": (
                    "session-handoff can send anonymous aggregate telemetry "
                    "(opt-in, off by default). Run "
                    f"`python3 {cli} telemetry enable` to turn it on. "
                    f"Details: {telemetry.TELEMETRY_DETAILS_URL}"
                ),
            }
        }))
    except Exception:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
