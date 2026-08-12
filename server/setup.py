"""One-time user-scope setup for the Codex and Claude adapters."""

from __future__ import annotations

import json
import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable


CLIENTS = ("codex", "claude")
STATE_PATH = Path(".config/session-handoff/state.json")
BUNDLE_PATH = Path(".local/share/session-handoff/plugin")
BACKUP_SUFFIX = ".session-handoff-original"


class SetupError(ValueError):
    """A safe, actionable setup error."""


def _skill_path(home: Path, client: str) -> Path:
    parent = ".codex" if client == "codex" else ".claude"
    return home / parent / "skills/session-handoff/SKILL.md"


def _validate_clients(clients: list[str]) -> list[str]:
    selected = list(dict.fromkeys(clients))
    if not selected or any(client not in CLIENTS for client in selected):
        raise SetupError("clients must contain codex, claude, or both")
    return selected


def setup_plan(
    package_root: Path,
    home: Path,
    clients: list[str],
    *,
    executable_paths: dict[str, Path] | None = None,
) -> dict[str, object]:
    selected = _validate_clients(clients)
    paths = executable_paths or {
        client: Path(shutil.which(client) or "") for client in selected
    }
    return {
        "bundle": str(home / BUNDLE_PATH),
        "server": str(home / BUNDLE_PATH / "server/handoff_mcp.py"),
        "skills": {client: str(_skill_path(home, client)) for client in selected},
        "launchers": {client: str(paths[client]) for client in selected},
        "clients": selected,
    }


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    _write_text(path, json.dumps(payload, indent=2) + "\n")


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _load_state(path: Path) -> dict[str, object]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"invalid session-handoff state: {path}") from exc
    if not isinstance(state, dict):
        raise SetupError(f"invalid session-handoff state: {path}")
    return state


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_wrapper(path: Path, client: str) -> bool:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return content.startswith("#!/bin/sh\n") and f" run {client} " in content


def _mcp_command(client: str, executable: Path, action: str) -> list[str]:
    command = [str(executable), "mcp", action]
    if client == "claude":
        command += ["--scope", "user"]
    return command + ["session-handoff"]


def _stage_bundle(package_root: Path, bundle: Path) -> Path:
    bundle.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{bundle.name}.staging-", dir=bundle.parent))
    try:
        shutil.copytree(
            package_root,
            staging,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
    except BaseException:
        _remove(staging)
        raise
    return staging


def _capture(path: Path) -> tuple[str, bytes | str, int] | None:
    if path.is_symlink():
        return ("symlink", os.readlink(path), 0)
    if path.is_file():
        return ("file", path.read_bytes(), path.stat().st_mode & 0o777)
    return None


def _restore(path: Path, snapshot: tuple[str, bytes | str, int] | None) -> None:
    _remove(path)
    if snapshot is None:
        return
    kind, value, mode = snapshot
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        path.symlink_to(value)
    else:
        path.write_bytes(value)
        path.chmod(mode)


def _wrapper(client: str, supervisor: Path, executable: Path) -> str:
    return (
        "#!/bin/sh\n"
        f"exec python3 {shlex.quote(str(supervisor))} run {client} "
        f"--executable {shlex.quote(str(executable))} \"$@\"\n"
    )


def _run_default(argv: list[str]) -> None:
    subprocess.run(argv, check=True)


def install_setup(
    package_root: Path,
    home: Path,
    clients: list[str],
    *,
    executable_paths: dict[str, Path],
    runner: Callable[[list[str]], None] = _run_default,
) -> dict[str, object]:
    """Install the persistent plugin bundle and user-scoped client adapters."""

    selected = _validate_clients(clients)
    state_path = home / STATE_PATH
    previous_state = state_path.read_bytes() if state_path.is_file() else None
    state = _load_state(state_path) if previous_state is not None else {}
    managed = state.get("clients", [])
    if not isinstance(managed, list) or any(client not in CLIENTS for client in managed):
        raise SetupError(f"invalid session-handoff state: {state_path}")
    managed = list(dict.fromkeys(managed))
    all_clients = list(dict.fromkeys([*managed, *selected]))
    new_clients = [client for client in all_clients if client not in managed]
    skill_source = package_root / "skills/session-handoff/SKILL.md"
    if not skill_source.is_file():
        raise SetupError(f"plugin skill not found: {skill_source}")
    skill_content = skill_source.read_text(encoding="utf-8")
    skill_hashes = state.get("skill_hashes", {})
    if not isinstance(skill_hashes, dict):
        raise SetupError(f"invalid session-handoff state: {state_path}")
    plan = setup_plan(package_root, home, all_clients, executable_paths={
        client: executable_paths[client]
        for client in selected
        if client in executable_paths
    } | {
        client: Path(state["launchers"][client])
        for client in managed
        if isinstance(state.get("launchers"), dict) and client in state["launchers"]
    })
    bundle = Path(plan["bundle"])
    server = Path(plan["server"])
    supervisor = bundle / "bin/session-handoff"
    old_launchers = state.get("launchers", {})
    old_backups = state.get("backups", {})
    old_targets = state.get("targets", {})
    if not all(isinstance(value, dict) for value in (old_launchers, old_backups, old_targets)):
        raise SetupError(f"invalid session-handoff state: {state_path}")

    launchers: dict[str, Path] = {}
    backups: dict[str, Path] = {}
    targets: dict[str, Path] = {}

    # Preflight all user-owned files before changing anything.
    for client in all_clients:
        skill = Path(plan["skills"][client])
        if (
            skill.exists()
            and skill.read_text(encoding="utf-8") != skill_content
            and not (client in managed and skill_hashes.get(client) == _digest(skill.read_text(encoding="utf-8")))
        ):
            raise SetupError(f"refusing to overwrite user-owned skill: {skill}")
        launcher = Path(old_launchers[client]) if client in old_launchers else Path(executable_paths[client])
        backup = Path(old_backups[client]) if client in old_backups else launcher.with_name(launcher.name + BACKUP_SUFFIX)
        target = Path(old_targets[client]) if client in old_targets else backup
        launchers[client], backups[client], targets[client] = launcher, backup, target
        executable = launcher
        if not executable.is_file() and not executable.is_symlink():
            if not (client in managed and backup.is_file()):
                raise SetupError(f"client executable not found: {executable}")
        if client in new_clients and backup.exists():
            raise SetupError(f"launcher backup already exists: {backup}")
        if not launcher.parent.exists() or not os.access(launcher.parent, os.W_OK):
            raise SetupError(f"launcher directory is not writable: {launcher.parent}")

    staging: Path | None = _stage_bundle(package_root, bundle)
    old_bundle: Path | None = None
    skill_changes: list[tuple[Path, tuple[str, bytes | str, int] | None]] = []
    launcher_changes: list[tuple[Path, Path | None, tuple[str, bytes | str, int] | None]] = []
    registered: list[tuple[str, Path]] = []
    try:
        if bundle.exists():
            old_bundle = Path(tempfile.mkdtemp(prefix=f".{bundle.name}.rollback-", dir=bundle.parent))
            old_bundle.rmdir()
            os.replace(bundle, old_bundle)
        os.replace(staging, bundle)
        staging = None

        for client in all_clients:
            skill = Path(plan["skills"][client])
            snapshot = _capture(skill)
            if snapshot is not None and skill.read_text(encoding="utf-8") == skill_content:
                continue
            skill_changes.append((skill, snapshot))
            _write_text(skill, skill_content)

        for client in new_clients:
            executable = launchers[client]
            command = _mcp_command(client, executable, "add") + ["--", "python3", str(server)]
            runner(command)
            registered.append((client, executable))

        for client in all_clients:
            launcher, backup, target = launchers[client], backups[client], targets[client]
            snapshot = _capture(launcher)
            moved_to: Path | None = None
            if client in new_clients:
                os.replace(launcher, backup)
                target = backup
                targets[client] = target
                moved_to = backup
            elif not _is_wrapper(launcher, client):
                if launcher.exists() or launcher.is_symlink():
                    if target.exists():
                        active = launcher.with_name(launcher.name + ".session-handoff-active")
                        suffix = 1
                        while active.exists():
                            active = launcher.with_name(
                                f"{launcher.name}.session-handoff-active-{suffix}"
                            )
                            suffix += 1
                        target = active
                        targets[client] = target
                    os.replace(launcher, target)
                    moved_to = target
                elif not target.exists():
                    raise SetupError(f"managed launcher is missing: {launcher}")
            launcher_changes.append((launcher, moved_to, snapshot))
            _write_text(launcher, _wrapper(client, supervisor, target))
            launcher.chmod(0o755)

        new_state = {
            "version": 2,
            "bundle": str(bundle),
            "clients": all_clients,
            "backups": {client: str(backups[client]) for client in all_clients},
            "launchers": {client: str(launchers[client]) for client in all_clients},
            "targets": {client: str(targets[client]) for client in all_clients},
            "skill_hashes": {client: _digest(skill_content) for client in all_clients},
        }
        _write_json(state_path, new_state)
    except BaseException:
        for launcher, moved_to, snapshot in reversed(launcher_changes):
            _remove(launcher)
            if moved_to is not None and moved_to.exists():
                os.replace(moved_to, launcher)
            else:
                _restore(launcher, snapshot)
        for client, executable in reversed(registered):
            try:
                runner(_mcp_command(client, executable, "remove"))
            except (OSError, subprocess.CalledProcessError, RuntimeError):
                pass
        for skill, snapshot in reversed(skill_changes):
            _restore(skill, snapshot)
        _remove(bundle)
        if old_bundle is not None and old_bundle.exists():
            os.replace(old_bundle, bundle)
        if staging is not None and staging.exists():
            _remove(staging)
        if previous_state is None:
            _remove(state_path)
        else:
            state_path.write_bytes(previous_state)
        raise
    else:
        if old_bundle is not None and old_bundle.exists():
            _remove(old_bundle)
        return {
            "installed": True,
            "already_configured": not new_clients,
            "clients": all_clients,
        }


def restore_setup(
    home: Path,
    *,
    runner: Callable[[list[str]], None] = _run_default,
) -> dict[str, object]:
    """Restore launchers created by setup and remove its managed files."""

    state_path = home / STATE_PATH
    if not state_path.is_file():
        return {"restored": False, "already_clean": True}
    state = _load_state(state_path)
    clients = state.get("clients", [])
    launchers = state.get("launchers", {})
    backups = state.get("backups", {})
    targets = state.get("targets", {})
    skill_hashes = state.get("skill_hashes", {})
    if (
        not isinstance(clients, list)
        or any(client not in CLIENTS for client in clients)
        or not all(isinstance(value, dict) for value in (launchers, backups, targets, skill_hashes))
        or any(client not in launchers or client not in backups for client in clients)
    ):
        raise SetupError(f"invalid session-handoff state: {state_path}")

    restore_plan: list[tuple[str, Path, Path, Path]] = []
    for client in clients:
        launcher = Path(launchers[client])
        backup = Path(backups[client])
        target = Path(targets.get(client, backup))
        if not _is_wrapper(launcher, client):
            raise SetupError(f"managed launcher changed externally: {launcher}")
        if not (target.exists() or target.is_symlink() or backup.exists() or backup.is_symlink()):
            raise SetupError(f"original launcher is missing: {backup}")
        restore_plan.append((client, launcher, backup, target))

    for client, launcher, backup, target in restore_plan:
        try:
            runner(_mcp_command(client, target if target.exists() else backup, "remove"))
        except (OSError, subprocess.CalledProcessError):
            raise SetupError(f"could not remove MCP registration for {client}")

    for client, launcher, backup, target in restore_plan:
        _remove(launcher)
        os.replace(target if target.exists() or target.is_symlink() else backup, launcher)
        if target != backup:
            _remove(backup)

    for client in clients:
        skill = _skill_path(home, client)
        if skill.is_file() and skill_hashes.get(client) == _digest(skill.read_text(encoding="utf-8")):
            _remove(skill)
    bundle = Path(state.get("bundle", home / BUNDLE_PATH))
    _remove(bundle)
    _remove(state_path)
    return {"restored": True, "already_clean": False, "clients": clients}


def render_plan(plan: dict[str, object]) -> str:
    lines = ["Session Handoff setup", "", "Changes:" ]
    lines.append(f"- persistent plugin: {plan['bundle']}")
    for client in plan["clients"]:
        lines.append(f"- {client} skill: {plan['skills'][client]}")
        lines.append(f"- {client} managed launcher: {plan['launchers'][client]}")
    lines.append("- user-scoped MCP server: session-handoff")
    lines.append("")
    lines.append("Existing launchers are backed up as *.session-handoff-original.")
    return "\n".join(lines)
