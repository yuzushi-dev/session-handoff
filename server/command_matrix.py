"""Emit a provider-free readiness matrix for handoff and migrate commands."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.request import urlopen

try:
    from . import handoff_store
    from .compatibility import LIVE_CASE_REQUIREMENTS, content_hash, normalize_version
except ImportError:  # Direct `python server/command_matrix.py` invocation.
    import handoff_store
    from compatibility import LIVE_CASE_REQUIREMENTS, content_hash, normalize_version


CLIENTS = ("codex", "claude")
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
INVOCATIONS = {"codex": "$session-handoff", "claude": "/session-handoff"}
SESSION_ID_NAMES = {"codex": "CODEX_THREAD_ID", "claude": "CLAUDE_CODE_SESSION_ID"}
COMPATIBILITY_REPORT_SCHEMA = "session-handoff.compatibility-report/v1"
CAPABILITIES = {
    "installation": "installation",
    "handoff": "handoff",
    "migration": "migration_format",
    "migration_format": "migration_format",
    "compaction": "compaction",
}
REQUIRED_LIVE_CASES = {
    case_id: (CAPABILITIES[capability], required_clients)
    for case_id, (capability, required_clients) in LIVE_CASE_REQUIREMENTS.items()
}


def _run_ok(
    argv: list[str], runner: Callable[..., Any], *, env: dict[str, str]
) -> bool:
    try:
        result = runner(
            argv, text=True, capture_output=True, check=False, timeout=5, env=env
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _probe_version(
    argv: list[str], runner: Callable[..., Any], *, env: dict[str, str]
) -> tuple[bool, str | None, str | None]:
    try:
        result = runner(
            argv, text=True, capture_output=True, check=False, timeout=5, env=env
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, None, None
    if result.returncode != 0:
        return False, None, None
    value = getattr(result, "stdout", "").strip()
    return True, normalize_version(value), value or None


def _load_state(home: Path) -> dict[str, Any]:
    path = home / ".config/session-handoff/state.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _default_compatibility_report(home: Path) -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    root = Path(state_home).expanduser() if state_home else home / ".local/state"
    return root / "session-handoff/compatibility-report.json"


def _installation_type(executable: Path) -> str:
    if executable.is_symlink():
        return "symlink"
    try:
        resolved = executable.resolve(strict=False).as_posix()
    except (OSError, RuntimeError):
        resolved = executable.as_posix()
    if "/node_modules/" in resolved or "/npm/" in resolved:
        return "npm"
    return "path"


def _resolve_probe_target(executable: Path, client: str) -> Path | None:
    """Resolve only wrappers emitted by setup; malformed wrappers fail closed."""
    current = executable
    seen: set[Path] = set()
    for _ in range(4):
        if current in seen:
            return None
        seen.add(current)
        try:
            content = current.read_bytes()[:8192].decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return current
        if not content.startswith("#!/bin/sh\n") or f" run {client} " not in content:
            return current
        lines = content.splitlines()
        if len(lines) != 2:
            return None
        try:
            tokens = shlex.split(lines[1], posix=True)
        except ValueError:
            return None
        if (
            len(tokens) != 8
            or tokens[:2] != ["exec", "python3"]
            or tokens[3:6] != ["run", client, "--executable"]
            or tokens[7] != "$@"
        ):
            return None
        candidate = Path(tokens[6])
        if not candidate.is_absolute() or not (
            candidate.is_file() or candidate.is_symlink()
        ):
            return None
        current = candidate
    return None


def _isolated_probe_environment(home: Path) -> tuple[Any, dict[str, str]]:
    temporary = tempfile.TemporaryDirectory(prefix="session-handoff-doctor-")
    try:
        probe_home = Path(temporary.name) / "home"
        probe_home.mkdir(mode=0o700)
        for relative in (Path(".codex/config.toml"), Path(".claude.json")):
            source = home / relative
            if source.is_file():
                destination = probe_home / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        directories = {
            "XDG_CONFIG_HOME": probe_home / ".config",
            "XDG_STATE_HOME": probe_home / ".local/state",
            "XDG_CACHE_HOME": probe_home / ".cache",
            "XDG_DATA_HOME": probe_home / ".local/share",
            "TMPDIR": probe_home / "tmp",
        }
        for directory in directories.values():
            directory.mkdir(parents=True, exist_ok=True)
        env = {
            key: value
            for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM")
            if (value := os.environ.get(key))
        }
        env.update({key: str(value) for key, value in directories.items()})
        env.update(
            {
                "HOME": str(probe_home),
                "CODEX_HOME": str(probe_home / ".codex"),
                "SESSION_HANDOFF_HOME": str(probe_home),
            }
        )
        return temporary, env
    except BaseException:
        temporary.cleanup()
        raise


def _load_certification(
    path: Path,
    versions: dict[str, str | None],
    package_identity: dict[str, str | None],
) -> tuple[dict[str, Any], dict[str, str]]:
    empty = {
        "status": "unverified",
        "reason": "no matching compatibility evidence",
        "capabilities": {
            "installation": "not-run",
            "handoff": "not-run",
            "migration_format": "not-run",
            "compaction": "not-run",
        },
        "tested_cases": [],
    }
    client_status = {client: "unverified" for client in CLIENTS}
    client_observed: dict[str, list[str]] = {client: [] for client in CLIENTS}
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty, client_status
    if (
        not isinstance(report, dict)
        or report.get("schema") != COMPATIBILITY_REPORT_SCHEMA
        or not isinstance(report.get("clients"), dict)
        or not isinstance(report.get("cases"), list)
    ):
        return {**empty, "reason": "invalid compatibility evidence"}, client_status

    report_build = report.get("build")
    report_environment = report.get("environment")
    expected_environment = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
    }
    if (
        not isinstance(report_build, dict)
        or not isinstance(report_environment, dict)
        or any(report_build.get(key) != value for key, value in package_identity.items())
        or any(report_environment.get(key) != value for key, value in expected_environment.items())
    ):
        return {
            **empty,
            "reason": "compatibility evidence is for a different build or platform",
        }, client_status
    live_ids = [
        case.get("id")
        for case in report["cases"]
        if isinstance(case, dict) and case.get("proof_level") == "live"
    ]
    if any(not isinstance(case_id, str) or not case_id for case_id in live_ids) or len(
        live_ids
    ) != len(set(live_ids)):
        return {**empty, "reason": "invalid compatibility evidence"}, client_status

    report_clients = report["clients"]
    matching = {
        client
        for client in CLIENTS
        if versions[client]
        and isinstance(report_clients.get(client), dict)
        and report_clients[client].get("version") == versions[client]
    }
    observed: dict[str, list[str]] = {name: [] for name in empty["capabilities"]}
    tested_cases: list[dict[str, Any]] = []
    passing_required: set[str] = set()
    for case in report["cases"]:
        if not isinstance(case, dict) or case.get("proof_level") != "live":
            continue
        case_clients = case.get("clients")
        if (
            not isinstance(case_clients, list)
            or not case_clients
            or any(client not in matching for client in case_clients)
            or case.get("status") not in {"passed", "failed", "not-run"}
        ):
            continue
        case_versions = case.get("client_versions")
        if not isinstance(case_versions, dict) or any(
            case_versions.get(client) != versions[client] for client in case_clients
        ):
            continue
        raw_capability = case.get("capability")
        if not isinstance(raw_capability, str) or raw_capability not in CAPABILITIES:
            continue
        capability = CAPABILITIES[raw_capability]
        observed[capability].append(case["status"])
        case_id = case.get("id")
        tested_cases.append(
            {
                "id": case_id if isinstance(case_id, str) else "unknown",
                "capability": capability,
                "clients": case_clients,
                "status": case["status"],
            }
        )
        required = REQUIRED_LIVE_CASES.get(case_id)
        if (
            required == (capability, frozenset(case_clients))
            and case["status"] == "passed"
        ):
            passing_required.add(case_id)
        for client in case_clients:
            client_observed[client].append(case["status"])

    capabilities = {}
    for capability, statuses in observed.items():
        if "failed" in statuses:
            capabilities[capability] = "failed"
        elif "passed" in statuses:
            capabilities[capability] = "passed"
        else:
            capabilities[capability] = "not-run"
    if "failed" in capabilities.values():
        overall = "failed"
    elif passing_required == set(REQUIRED_LIVE_CASES):
        overall = "verified"
    else:
        overall = "unverified"
    for client, statuses in client_observed.items():
        if "failed" in statuses:
            client_status[client] = "failed"
        elif {
            case_id
            for case_id, (_capability, required_clients) in REQUIRED_LIVE_CASES.items()
            if client in required_clients
        }.issubset(passing_required):
            client_status[client] = "verified"
    certification = {
        "status": overall,
        "report_schema": COMPATIBILITY_REPORT_SCHEMA,
        "capabilities": capabilities,
        "tested_cases": tested_cases,
    }
    if overall == "unverified":
        certification["reason"] = "required live compatibility matrix is incomplete"
    return certification, client_status


def probe_command_matrix(
    home: str | Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
    compatibility_report: str | Path | None = None,
) -> dict[str, Any]:
    """Check setup artifacts and CLI registrations without starting a model session."""

    root = Path(home).expanduser().resolve()
    try:
        package = json.loads((PACKAGE_ROOT / "package.json").read_text(encoding="utf-8"))
        package_identity = {
            "package_version": package["version"],
            "source_kind": "checkout" if (PACKAGE_ROOT / ".git").exists() else "installed-package",
            "content_sha256": content_hash(PACKAGE_ROOT),
        }
    except (OSError, ValueError, KeyError, TypeError):
        package_identity = {
            "package_version": None,
            "source_kind": None,
            "content_sha256": None,
        }
    state = _load_state(root)
    managed = state.get("clients") if isinstance(state.get("clients"), list) else []
    launchers = state.get("launchers") if isinstance(state.get("launchers"), dict) else {}
    targets = state.get("targets") if isinstance(state.get("targets"), dict) else {}
    clients: dict[str, dict[str, Any]] = {}
    versions: dict[str, str | None] = {}
    probe_directory, probe_env = _isolated_probe_environment(root)
    try:
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
            managed_client = client in managed
            launcher_value = launchers.get(client)
            launcher_path = Path(launcher_value) if isinstance(launcher_value, str) else None
            try:
                launcher_ready = launcher_path is not None and f" run {client} " in launcher_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                launcher_ready = False
            target_value = targets.get(client)
            discovered = shutil.which(client) if not managed_client else None
            target = Path(target_value) if isinstance(target_value, str) else Path(discovered) if discovered else None
            if target is not None:
                target = _resolve_probe_target(target, client)
            target_ready = target is not None and (target.is_file() or target.is_symlink())
            mcp_args = [str(target), "mcp", "get", "session-handoff"]
            if client == "codex":
                mcp_args.append("--json")
            mcp_ready = target_ready and _run_ok(mcp_args, runner, env=probe_env)
            version_ready, version, version_output = (
                _probe_version([str(target), "--version"], runner, env=probe_env)
                if target_ready
                else (False, None, None)
            )
            versions[client] = version
            if not managed_client:
                launcher_status = "unmanaged"
            elif launcher_path is None or (
                not launcher_path.exists() and not launcher_path.is_symlink()
            ):
                launcher_status = "missing"
            elif launcher_ready:
                launcher_status = "managed"
            else:
                launcher_status = "changed"
            status = {
                "managed": managed_client,
                "skill": skill_ready,
                "launcher": launcher_ready,
                "mcp": mcp_ready,
                "executable": version_ready,
                "version": version,
                "version_output": version_output,
                "installation_type": _installation_type(target) if target_ready else "unknown",
                "executable_origin": "managed_target" if managed_client else "path" if target_ready else "unknown",
                "launcher_status": launcher_status,
            }
            status["ready"] = all(
                status[key] for key in ("managed", "skill", "launcher", "mcp", "executable")
            )
            clients[client] = status
    finally:
        probe_directory.cleanup()

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
    central_store = probe_central_store()
    central_ready = central_store["status"] in {"absent", "healthy"} and central_store["writable"]
    report_path = Path(compatibility_report) if compatibility_report else _default_compatibility_report(root)
    certification, client_certification = _load_certification(
        report_path, versions, package_identity
    )
    for client in CLIENTS:
        clients[client]["certification"] = client_certification[client]
    ready = all(flow["ready"] for flow in flows.values()) and central_ready
    return {
        "schema_version": 2,
        "provider_calls": 0,
        "migration_engine": "internal",
        "package": package_identity,
        "clients": clients,
        "flows": flows,
        "local_readiness": {"ready": ready},
        "certification": certification,
        "central_store": central_store,
        "compaction_scoring": probe_compaction_scoring(),
        "compaction_scoring_typesafe": probe_typesafe_scoring(),
        "ready": ready,
    }


def probe_central_store() -> dict[str, Any]:
    """Return read-only central-store health, including a non-mutating catalog check."""
    return handoff_store.probe_health()


def probe_compaction_scoring(
    *, model: str = "smollm2:1.7b", base_url: str = "http://localhost:11434"
) -> dict[str, bool]:
    """Report local-scorer readiness distinctly: installed, reachable, model present."""
    installed = shutil.which("ollama") is not None
    if not installed:
        return {"installed": False, "reachable": False, "model_pulled": False}
    try:
        with urlopen(f"{base_url}/api/tags", timeout=1.0) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError):
        return {"installed": True, "reachable": False, "model_pulled": False}
    if not isinstance(payload, dict):
        return {"installed": True, "reachable": True, "model_pulled": False}
    names = {entry.get("name") for entry in payload.get("models", []) if isinstance(entry, dict)}
    return {"installed": True, "reachable": True, "model_pulled": model in names}


def probe_typesafe_scoring() -> dict[str, bool]:
    """Report only whether a TypeSafe credential is configured.

    Never makes a network call to verify the key works: this module's own
    "provider-free readiness matrix" contract (`provider_calls: 0`) covers
    TypeSafe too, unlike Ollama's `/api/tags` check, which stays local.
    """
    try:
        from .typesafe_client import TypeSafeClient
    except ImportError:  # Direct `python server/command_matrix.py` invocation.
        from typesafe_client import TypeSafeClient
    return {"configured": TypeSafeClient().is_configured()}


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
    parser.add_argument("--human", action="store_true", help="Render a human-readable readiness summary.")
    parser.add_argument(
        "--compatibility-report",
        type=Path,
        help="Read certification evidence from an H1 compatibility report.",
    )
    args = parser.parse_args(argv)
    result = probe_command_matrix(args.home, compatibility_report=args.compatibility_report)
    if args.human:
        print(render_human(result))
    else:
        print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if result["ready"] else 1


def render_human(result: dict[str, Any]) -> str:
    central = result["central_store"]
    lines = [f"Session-handoff doctor: {'ready' if result['ready'] else 'not ready'}"]
    for client, status in result["clients"].items():
        lines.append(
            f"- {client}: {'ready' if status['ready'] else 'not ready'}; "
            f"version={status['version'] or 'unknown'}; launcher={status['launcher_status']}; "
            f"certification={status['certification']}"
        )
    certification = result["certification"]
    capabilities = certification["capabilities"]
    lines.append(
        f"- certified capacity: {certification['status']} "
        f"(installation={capabilities['installation']}, handoff={capabilities['handoff']}, "
        f"migration_format={capabilities['migration_format']}, "
        f"compaction={capabilities['compaction']})"
    )
    lines.append(f"- central store: {central['status']} ({'writable' if central['writable'] else 'not writable'})")
    lines.append(f"  data: {central['data_root']['status']} — {central['data_root']['path']}")
    lines.append(f"  state: {central['state_root']['status']} — {central['state_root']['path']}")
    lines.append(f"  catalog: {central['catalog']['status']} — {central['catalog']['path']}")
    scoring = result["compaction_scoring"]
    lines.append(
        f"- checkpoint local scorer: installed={scoring['installed']} "
        f"reachable={scoring['reachable']} model_pulled={scoring['model_pulled']}"
    )
    typesafe = result["compaction_scoring_typesafe"]
    lines.append(f"- checkpoint TypeSafe scorer: configured={typesafe['configured']}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
