#!/usr/bin/env python3
"""Record an exact in-chat telemetry consent response without changing the prompt."""

import json
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from server import telemetry  # noqa: E402


YES = "session-handoff telemetry yes"
NO = "session-handoff telemetry no"


def main() -> int:
    try:
        if telemetry.do_not_track_enabled():
            print("{}")
            return 0
        payload = json.load(sys.stdin)
        prompt = payload.get("prompt") if isinstance(payload, dict) else None
        # Normalizza solo gli spazi ai bordi: non introduce ambiguita' (la stringa
        # resta esatta) ed evita di perdere risposte genuine incollate con spazi.
        if isinstance(prompt, str):
            prompt = prompt.strip()
        if prompt == YES:
            telemetry.record_consent_response(enabled=True)
        elif prompt == NO:
            telemetry.record_consent_response(enabled=False)
    except Exception:
        pass
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
