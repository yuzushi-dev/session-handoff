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
        if telemetry.load_config() is not None:
            print("{}")
            return 0

        cli = shlex.quote(str(PLUGIN_ROOT / "bin/session-handoff"))
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "systemMessage": (
                    "session-handoff can send anonymous aggregate telemetry "
                    "(opt-in, off by default). Run "
                    f"`python3 {cli} telemetry enable` to turn it on."
                ),
            }
        }))
    except Exception:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
