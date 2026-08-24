import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]


def load_json(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_portable_and_native_manifests_agree():
    portable = load_json("plugin.json")
    codex = load_json(".codex-plugin/plugin.json")
    claude = load_json(".claude-plugin/plugin.json")

    assert portable["$schema"].endswith("/schemas/1.0.0/plugin.schema.json")
    assert portable["name"] == codex["name"] == claude["name"] == "session-handoff"
    assert portable["version"] == codex["version"] == claude["version"] == "0.5.0"
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
    assert (ROOT / "server/setup.py").is_file()


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
