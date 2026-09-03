import json
import os
import subprocess
import sys
from pathlib import Path

from server import handoff_mcp, session_switch


ROOT = Path(__file__).parents[1]


def load_json(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_portable_and_native_manifests_agree():
    portable = load_json("plugin.json")
    codex = load_json(".codex-plugin/plugin.json")
    claude = load_json(".claude-plugin/plugin.json")

    assert portable["$schema"].endswith("/schemas/1.0.0/plugin.schema.json")
    assert portable["name"] == codex["name"] == claude["name"] == "session-handoff"
    assert portable["version"] == codex["version"] == claude["version"] == load_json("package.json")["version"]
    assert codex["hooks"] == claude["hooks"] == "./hooks/hooks.json"
    assert set(portable) <= {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "extensions",
    }


def test_all_package_version_sources_agree():
    package = load_json("package.json")
    sources = {
        "package.json": package["version"],
        "plugin.json": load_json("plugin.json")["version"],
        ".codex-plugin/plugin.json": load_json(".codex-plugin/plugin.json")["version"],
        ".claude-plugin/plugin.json": load_json(".claude-plugin/plugin.json")["version"],
        "server/handoff_mcp.py": handoff_mcp.SERVER_VERSION,
        "server/session_switch.py": session_switch.TELEMETRY_PLUGIN_VERSION,
    }

    assert len(set(sources.values())) == 1, sources
    event = session_switch._operation_event({
        "operation": "handoff",
        "source_client": "codex",
        "target_client": "claude",
        "result": "success",
        "failure_stage": "none",
    })
    assert event["plugin_version"] == package["version"]


def test_portable_mcp_config_uses_agent_plugins_paths():
    config = load_json("mcp.json")
    server = config["mcpServers"]["session-handoff"]

    assert config["$schema"].endswith("/schemas/1.0.0/mcp.schema.json")
    assert server["type"] == "stdio"
    assert server["command"] == "python3"
    assert "${PLUGIN_ROOT}" in server["args"][0]


def test_native_mcp_config_supports_claude_and_codex():
    config = load_json(".mcp.json")
    server = config["mcpServers"]["session-handoff"]

    assert server["command"] == "python3"
    assert "${CLAUDE_PLUGIN_ROOT}" in server["args"][0]
    assert server["args"][0].endswith("server/handoff_mcp.py")


def test_all_hooks_support_native_and_legacy_plugin_root_variables():
    hooks = load_json("hooks/hooks.json")["hooks"]

    commands = [
        entry["command"]
        for events in hooks.values()
        for event in events
        for entry in event["hooks"]
        if entry["type"] == "command"
    ]

    assert commands
    assert all("${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}" in command for command in commands)


def test_skill_documents_create_and_resume_workflows():
    skill = (ROOT / "skills/session-handoff/SKILL.md").read_text(encoding="utf-8")

    assert skill.startswith("---\n")
    assert "name: session-handoff" in skill
    assert "description:" in skill
    assert "handoff_create" in skill
    assert "auto_switch: true" in skill
    assert "resume" in skill.lower()
    for section in (
        "## Goal",
        "## Constraints & Preferences",
        "## Progress",
        "## Key Decisions",
        "## Critical Context",
        "## Next Steps",
    ):
        assert section in skill


def test_claude_command_requests_supervised_handoff():
    command = (ROOT / "commands/handoff.md").read_text(encoding="utf-8")

    assert command.startswith("---\n")
    assert "auto_switch: true" in command
    assert "auto_switch_requested" in command


def test_supervisor_entrypoint_is_executable():
    entrypoint = ROOT / "bin/session-handoff"

    assert entrypoint.stat().st_mode & 0o111
    assert "SessionSupervisor" in entrypoint.read_text(encoding="utf-8")


def test_npx_installer_exposes_setup_command():
    package = load_json("package.json")

    assert package["bin"]["session-handoff"] == "bin/session-handoff"
    assert package["os"] == ["linux", "darwin"]
    assert package["engines"]["python"] == ">=3.10"
    assert "setup" in (ROOT / "bin/session-handoff").read_text(encoding="utf-8")
    assert "doctor" in (ROOT / "bin/session-handoff").read_text(encoding="utf-8")
    assert (ROOT / "server/setup.py").is_file()
    assert (ROOT / "server/command_matrix.py").is_file()


def test_npm_install_preflights_python_runtime(tmp_path):
    package = load_json("package.json")
    preinstall = package["scripts"]["preinstall"]
    assert "python3 -c" in preinstall
    no_python = tmp_path / "bin"
    no_python.mkdir()
    result = subprocess.run(
        ["/bin/sh", "-c", preinstall],
        text=True,
        capture_output=True,
        check=False,
        env={"PATH": str(no_python)},
    )

    assert result.returncode == 1
    assert "Python 3.10+" in result.stderr


def test_mcp_server_version_matches_package():
    package = load_json("package.json")

    assert handoff_mcp.SERVER_VERSION == package["version"]


def test_runtime_contains_only_the_internal_migration_engine():
    package = load_json("package.json")

    assert (ROOT / "server/migration_engine.py").is_file()
    assert (ROOT / "server/checkpoint.py").is_file()
    assert not (ROOT / "server/session_migrate").exists()
    assert not (ROOT / "THIRD_PARTY_NOTICES.md").exists()
    assert package.get("dependencies", {}) == {}
    assert package.get("optionalDependencies", {}) == {}


def test_entrypoint_resolves_package_root_when_called_through_npm_bin(tmp_path):
    npm_bin = tmp_path / "node_modules/.bin"
    npm_bin.mkdir(parents=True)
    entrypoint = npm_bin / "session-handoff"
    entrypoint.symlink_to(ROOT / "bin/session-handoff")

    result = subprocess.run(
        [sys.executable, str(entrypoint), "setup", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Install the user-scoped" in result.stdout


def test_npx_setup_uses_the_resolved_package_root(tmp_path):
    npm_bin = tmp_path / "node_modules/.bin"
    npm_bin.mkdir(parents=True)
    entrypoint = npm_bin / "session-handoff"
    entrypoint.symlink_to(ROOT / "bin/session-handoff")
    fake_client = tmp_path / "bin/codex"
    fake_client.parent.mkdir()
    fake_client.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_client.chmod(0o755)

    env = {
        **os.environ,
        "PATH": str(fake_client.parent),
        "SESSION_HANDOFF_HOME": str(tmp_path / "home"),
    }
    result = subprocess.run(
        [sys.executable, str(entrypoint), "setup", "--client", "codex", "--yes"],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "home/.codex/skills/session-handoff/SKILL.md").is_file()
    assert not (tmp_path / "home/.config/session-handoff/telemetry.json").exists()


def test_setup_reinstall_reports_reconciliation_instead_of_no_change(tmp_path):
    fake_client = tmp_path / "bin/codex"
    fake_client.parent.mkdir()
    fake_client.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_client.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(fake_client.parent),
        "SESSION_HANDOFF_HOME": str(tmp_path / "home"),
    }
    command = [sys.executable, str(ROOT / "bin/session-handoff"), "setup", "--client", "codex", "--yes"]

    first = subprocess.run(command, text=True, capture_output=True, check=False, env=env)
    second = subprocess.run(command, text=True, capture_output=True, check=False, env=env)

    assert first.returncode == second.returncode == 0
    assert "reconciled" in second.stdout
    assert "nothing changed" not in second.stdout


def test_packed_tarball_runs_through_npx_setup(tmp_path):
    destination = tmp_path / "dist"
    destination.mkdir()
    packed = subprocess.run(
        ["npm", "pack", "--ignore-scripts", "--pack-destination", str(destination)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert packed.returncode == 0, packed.stderr
    tarball = next(destination.glob("session-handoff-*.tgz"))
    contents = subprocess.run(
        ["tar", "-tzf", str(tarball)],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    assert "package/hooks/hooks.json" in contents
    assert not any(path.startswith("package/hooks/__pycache__/") for path in contents)
    assert "package/docs/telemetry.md" in contents
    assert "package/server/migration_engine.py" in contents

    client_dir = tmp_path / "bin"
    client_dir.mkdir()
    client = client_dir / "codex"
    client.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    client.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(client_dir) + os.pathsep + os.environ.get("PATH", os.defpath),
        "SESSION_HANDOFF_HOME": str(tmp_path / "home"),
    }
    result = subprocess.run(
        [
            "npm",
            "exec",
            "--offline",
            "--yes",
            "--package",
            str(tarball),
            "--",
            "session-handoff",
            "setup",
            "--client",
            "codex",
            "--yes",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "home/.config/session-handoff/state.json").is_file()
