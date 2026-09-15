"""Launcher setup guidance and automatic first-session install for marketplace installs."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
import shlex

try:
    from . import telemetry, setup
    from .command_matrix import SESSION_ID_NAMES
except ImportError:  # direct MCP server execution
    import telemetry
    import setup
    from command_matrix import SESSION_ID_NAMES


def installation_commands(plugin_root: Path) -> dict:
    cli = str(plugin_root / "bin/session-handoff")
    return {
        "setup_commands": {
            client: shlex.join(["python3", cli, "setup", "--client", client])
            for client in ("claude", "codex")
        },
        "uninstall_command": shlex.join(["python3", cli, "uninstall"]),
        "telemetry_commands": {
            choice: shlex.join(["python3", cli, "telemetry", choice])
            for choice in ("enable", "yes", "no", "status")
        },
        "instructions": (
            "Show these commands to the user. Telemetry is a separate optional choice; never "
            "choose consent for them. A first session normally installs automatic switching for "
            "the detected client on its own; these setup commands are for the other client, or "
            "for a client the hook could not detect. After setup, restart the terminal and "
            "launch claude or codex from it. Desktop app and IDE sessions are not supervised."
        ),
    }


def _detect_client() -> str | None:
    detected = [client for client, var in SESSION_ID_NAMES.items() if os.environ.get(var)]
    if len(detected) != 1:
        return None  # absent or ambiguous (both host env vars set): don't guess
    return detected[0]


def _spawn_detached_setup(plugin_root: Path, client: str) -> None:
    command = [
        sys.executable,
        str(plugin_root / "bin/session-handoff"),
        "setup",
        "--client",
        client,
        "--yes",
    ]
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def launcher_notice(plugin_root: Path) -> str | None:
    if os.environ.get("SESSION_HANDOFF_CONTROL"):
        return None
    selected = os.environ.get("SESSION_HANDOFF_HOME")
    home = (Path(selected).expanduser() if selected is not None else Path.home()).resolve()
    if (home / setup.STATE_PATH).is_file():
        return None  # setup already ran for this install; respect what's already configured
    directory_fd = None
    try:
        # Reuse the consent store's private, no-symlink directory traversal.
        _, directory_fd = telemetry._open_secure_directory(
            home, Path(".config/session-handoff"), create=True,
        )
        descriptor = os.open(
            "launcher-notice-v1", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory_fd,
        )
        os.close(descriptor)
    except (OSError, telemetry.TelemetryConfigError):
        return None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)

    commands = installation_commands(plugin_root)
    client = _detect_client()
    if client is not None and shutil.which(client) is None:
        client = None  # detected via env, but not runnable here: don't spawn a doomed install
    if client is None:
        return (
            "session-handoff — Automatic session switching (optional)\n"
            "Enable this to open a fresh CLI session after a handoff or migrate between clients. "
            "Run the command for your client in a terminal:\n"
            f"Claude Code:\n{commands['setup_commands']['claude']}\n"
            f"Codex:\n{commands['setup_commands']['codex']}\n"
            "Setup shows the changes and asks before wrapping the selected client command. "
            "Restart your terminal, then launch claude or codex from it. "
            "This does not restart a desktop app or IDE session. "
            "Skip setup to keep resuming handoffs manually. Telemetry is a separate choice."
        )

    _spawn_detached_setup(plugin_root, client)
    other = "codex" if client == "claude" else "claude"
    return (
        "session-handoff — Automatic session switching\n"
        f"Installing it for {client} in the background; restart your terminal after this "
        "session to pick it up. This does not restart a desktop app or IDE session.\n"
        f"To also enable it for {other}, run:\n{commands['setup_commands'][other]}\n"
        f"To undo either: {commands['uninstall_command']}\n"
        "Telemetry is a separate choice."
    )
