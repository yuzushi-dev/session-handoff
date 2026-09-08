"""One-time, optional launcher setup guidance for marketplace installs."""

import os
from pathlib import Path
import shlex

try:
    from . import telemetry
except ImportError:  # direct MCP server execution
    import telemetry


def installation_commands(plugin_root: Path) -> dict:
    cli = str(plugin_root / "bin/session-handoff")
    return {
        "setup_commands": {
            client: shlex.join(["python3", cli, "setup", "--client", client])
            for client in ("claude", "codex")
        },
        "telemetry_commands": {
            choice: shlex.join(["python3", cli, "telemetry", choice])
            for choice in ("enable", "yes", "no", "status")
        },
        "instructions": (
            "Show these commands to the user. Setup and telemetry are separate optional choices. "
            "Run setup only after the user chooses automatic switching; never choose telemetry "
            "consent for them. After setup, restart the terminal and launch claude or codex "
            "from it. Desktop app and IDE sessions are not supervised."
        ),
    }


def launcher_notice(plugin_root: Path) -> str | None:
    if os.environ.get("SESSION_HANDOFF_CONTROL"):
        return None
    selected = os.environ.get("SESSION_HANDOFF_HOME")
    home = (Path(selected).expanduser() if selected is not None else Path.home()).resolve()
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
    commands = installation_commands(plugin_root)["setup_commands"]
    return (
        "session-handoff — Automatic session switching (optional)\n"
        "Enable this to open a fresh CLI session after a handoff or migrate between clients. "
        "Run the command for your client in a terminal:\n"
        f"Claude Code:\n{commands['claude']}\nCodex:\n{commands['codex']}\n"
        "Setup shows the changes and asks before wrapping the selected client command. "
        "Restart your terminal, then launch claude or codex from it. "
        "This does not restart a desktop app or IDE session. "
        "Skip setup to keep resuming handoffs manually. Telemetry is a separate choice."
    )
