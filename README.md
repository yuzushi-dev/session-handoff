# Session Handoff

An Agent Plugins MVP for carrying exact coding context from one Codex or Claude session into another.

The workflow is inspired by [oh-my-pi's handoff document](https://github.com/can1357/oh-my-pi/blob/main/packages/agent/src/compaction/prompts/handoff-document.md): it preserves goal, constraints, completed/in-progress/pending work, decisions, critical technical context, and next steps. The package adds a portable Agent Plugins 1.0.0 manifest, a shared Agent Skill, and a dependency-free local MCP server that validates files, scopes paths to the workspace, writes atomically, and redacts common credential formats.

## Install once

The supported setup path installs a persistent copy of the plugin, registers
the MCP server for the selected client, installs the user-scoped skill, and
creates managed launchers for automatic switching:

```bash
npx session-handoff@latest setup
```

The command is available after the package is published to npm. From a local
checkout or downloaded tarball, use `npm exec --package <tarball> --
session-handoff setup` instead.

The TUI shows the files and client configuration it will change and asks for
confirmation. Existing client launchers are backed up as
`*.session-handoff-original`. Restart the shell and the client once after the
setup.

The supported runtime is Python 3.10+ on Linux or macOS. If a client updater
replaces its launcher, run setup again; the installer preserves the original
backup and wraps the updated executable.

To undo the setup:

```bash
npx session-handoff@latest uninstall
```

## Use it

In an active session:

```text
Create a handoff focused on the remaining API migration work.
```

When the client is launched through the managed launcher, the handoff automatically starts a fresh session and continues with:

```text
Resume from handoffs/2026-08-12-api-migration.md
```

The automatic path is opt-in at process level: the supervisor owns the current Codex or Claude process and relaunches it after `handoff_create` succeeds.

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

Codex uses `.codex-plugin/plugin.json`, `skills/`, and `.mcp.json`. The setup installs the `session-handoff` skill in the user scope; invoke it explicitly with `$session-handoff` or from the skill menu when available.

The setup configures automatic switching for normal Codex launches. For a manual development run:

```bash
python3 /path/to/session-handoff/bin/session-handoff codex --
```

## Claude Code

The setup installs a user-scoped `/session-handoff` skill adapter. For a manual development session, load the package with:

```bash
python3 /path/to/session-handoff/bin/session-handoff claude -- --plugin-dir /path/to/session-handoff
```

Claude uses `.claude-plugin/plugin.json`, `commands/`, the shared `skills/` directory, and `.mcp.json`. Without the setup adapter, invoke `/session-handoff:handoff`, or ask for a handoff in natural language.

The command requests the automatic switch through the supervisor. A direct `claude --plugin-dir ...` invocation intentionally falls back to manual resume because it is not running through the managed launcher.

## Safety and scope

- Writes are restricted to the workspace path passed to the MCP tool.
- Existing files are never overwritten by default.
- Secrets are redacted before persistence and before read results are returned.
- The plugin does not delete, rename, or mutate project files other than the requested handoff file.
- Automatic switching requires the managed supervisor launcher; launching an unconfigured client directly keeps the safe manual-resume fallback.

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
