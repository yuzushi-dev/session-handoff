"""Emit a provider-free readiness matrix for handoff and migrate commands."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable


CLIENTS = ("codex", "claude")
INVOCATIONS = {"codex": "$session-handoff", "claude": "/session-handoff"}
SESSION_ID_NAMES = {"codex": "CODEX_THREAD_ID", "claude": "CLAUDE_CODE_SESSION_ID"}


def _run_ok(argv: list[str], runner: Callable[..., Any]) -> bool:
    try:
        result = runner(argv, text=True, capture_output=True, check=False)
    except OSError:
        return False
    return result.returncode == 0


def _load_state(home: Path) -> dict[str, Any]:
    path = home / ".config/session-handoff/state.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def probe_command_matrix(
    home: str | Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Check setup artifacts and CLI registrations without starting a model session."""

    root = Path(home).expanduser().resolve()
    state = _load_state(root)
    managed = state.get("clients") if isinstance(state.get("clients"), list) else []
    launchers = state.get("launchers") if isinstance(state.get("launchers"), dict) else {}
    targets = state.get("targets") if isinstance(state.get("targets"), dict) else {}
    clients: dict[str, dict[str, bool]] = {}
    for client in CLIENTS:
        parent = ".codex" if client == "codex" else ".claude"
        skill_path = root / parent / "skills/session-handoff/SKILL.md"
        try:
            skill = skill_path.read_text(encoding="utf-8")
        except OSError:
            skill = ""
        skill_ready = all(
            marker in skill
            for marker in (
                INVOCATIONS[client],
                "handoff_create",
                "handoff_migrate",
                SESSION_ID_NAMES[client],
            )
        )
        launcher_path = Path(launchers.get(client, ""))
        try:
            launcher_ready = f" run {client} " in launcher_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            launcher_ready = False
        target = Path(targets.get(client, ""))
        target_ready = target.is_file() or target.is_symlink()
        mcp_args = [str(target), "mcp", "get", "session-handoff"]
        if client == "codex":
            mcp_args.append("--json")
        mcp_ready = target_ready and _run_ok(mcp_args, runner)
        version_ready = target_ready and _run_ok([str(target), "--version"], runner)
        status = {
            "managed": client in managed,
            "skill": skill_ready,
            "launcher": launcher_ready,
            "mcp": mcp_ready,
            "executable": version_ready,
        }
        status["ready"] = all(status.values())
        clients[client] = status

    codex_ready = clients["codex"]["ready"]
    claude_ready = clients["claude"]["ready"]
    migration_ready = codex_ready and claude_ready
    flows = {
        "claude_handoff": {"command": "/session-handoff", "ready": claude_ready},
        "codex_handoff": {"command": "$session-handoff", "ready": codex_ready},
        "claude_to_codex": {
            "command": "/session-handoff migrate codex",
            "ready": migration_ready,
        },
        "codex_to_claude": {
            "command": "$session-handoff migrate claude",
            "ready": migration_ready,
        },
    }
    return {
        "schema_version": 1,
        "provider_calls": 0,
        "migration_engine": "internal",
        "clients": clients,
        "flows": flows,
        "ready": all(flow["ready"] for flow in flows.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="session-handoff doctor",
        description="Check handoff and migrate readiness without provider calls.",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(os.environ.get("SESSION_HANDOFF_HOME", str(Path.home()))),
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    result = probe_command_matrix(args.home)
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
