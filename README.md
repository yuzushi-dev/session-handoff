# Session Handoff

An Agent Plugins MVP for carrying exact coding context from one Codex or Claude session into another.

The workflow is inspired by [oh-my-pi's handoff document](https://github.com/can1357/oh-my-pi/blob/main/packages/agent/src/compaction/prompts/handoff-document.md): it preserves goal, constraints, completed/in-progress/pending work, decisions, critical technical context, and next steps. The package adds a portable Agent Plugins 1.0.0 manifest, a shared Agent Skill, and a dependency-free local MCP server that validates files, scopes paths to the workspace, writes atomically, and redacts common credential formats.

## Use it

In an active session:

```text
Create a handoff focused on the remaining API migration work.
```

Start a fresh session and continue with:

```text
Resume from handoffs/2026-08-12-api-migration.md
```

The skill creates and reads the handoff; the MCP server does not switch sessions automatically.

## Examples

Create a handoff at the end of a debugging session:

```text
Create a handoff focused on the failing integration test. Include the exact error,
files inspected, hypotheses already rejected, and the safest next experiment.
```

Resume from a handoff in another client:

```text
Resume from handoffs/2026-08-12-integration-test.md and continue with the first
pending next step after checking that the repository state still matches.
```

Inspect available handoffs before choosing one:

```text
List the handoffs under handoffs/ and validate the one related to the release work.
```

## Codex

The local Codex marketplace entry is at `~/.agents/plugins/marketplace.json`. Install `session-handoff` from that marketplace, or load the package directly while developing. Codex uses `.codex-plugin/plugin.json`, `skills/`, and `.mcp.json`.

## Claude Code

Load the package for a development session with:

```bash
claude --plugin-dir /path/to/session-handoff
```

Then invoke `/session-handoff:session-handoff`, or ask for a handoff in natural language. Claude uses `.claude-plugin/plugin.json`, the shared `skills/` directory, and `.mcp.json`.

## Safety and scope

- Writes are restricted to the workspace path passed to the MCP tool.
- Existing files are never overwritten by default.
- Secrets are redacted before persistence and before read results are returned.
- The plugin does not delete, rename, or mutate project files other than the requested handoff file.
- It does not automatically start, stop, or switch a Codex/Claude session.

## Development checks

```bash
pytest -q
python3 -m json.tool plugin.json >/dev/null
python3 -m json.tool mcp.json >/dev/null
python3 -m json.tool .codex-plugin/plugin.json >/dev/null
python3 -m json.tool .claude-plugin/plugin.json >/dev/null
python3 -m json.tool .mcp.json >/dev/null
```

The MCP server has no third-party runtime dependency.
