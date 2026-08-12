import json
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
    assert portable["version"] == codex["version"] == claude["version"] == "0.1.0"
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
