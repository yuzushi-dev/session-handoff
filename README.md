# session-handoff

`session-handoff` creates handoff documents and migrates active sessions between Claude Code and Codex.

The main product is the managed plugin bundle: skills, the MCP server, client launchers, and the internal migration engine. The npm package distributes that bundle and exposes the setup CLI. It is not a separate migration dependency.

## Requirements

- Linux or macOS
- Node.js 18 or newer and npm
- Python 3.10 or newer
- Claude Code, Codex, or both, depending on which client you want to configure

## Install

Use the managed setup command:

```bash
npx session-handoff@latest setup
```

The command installs a user-scoped plugin bundle, registers the MCP server, installs the client skill, and wraps each selected client launcher. Existing launchers are saved as `*.session-handoff-original`. Restart the client and any open shell after setup.

To configure one client only:

```bash
npx session-handoff@latest setup --client codex
npx session-handoff@latest setup --client claude
```

Use `--yes` for a non-interactive setup:

```bash
npx session-handoff@latest setup --client codex --yes
```

The client executable must already be on `PATH`. Without a client, setup stops with an error and changes nothing.

## Native plugin files

The repository and npm package include native Claude and Codex plugin manifests, the portable plugin manifest, the command, the skill, hooks, and the MCP configuration.

This repository is not itself a marketplace. The published wrapper is [yuzushi-plugins](https://github.com/yuzushi-dev/yuzushi-plugins).

Claude Code:

```text
/plugin marketplace add yuzushi-dev/yuzushi-plugins
/plugin install session-handoff@yuzushi
```

Codex:

```bash
codex plugin marketplace add yuzushi-dev/yuzushi-plugins
codex plugin add session-handoff@yuzushi
```

The marketplace path installs the plugin source from GitHub. The npm path above distributes the published package and managed setup independently.

## First use

After restarting the client:

Claude Code:

```text
/session-handoff
```

Codex:

```text
$session-handoff
```

Use `migrate claude` or `migrate codex` when you want to preserve the native session while changing clients. A normal handoff starts a clean session and keeps the implementation state in the handoff file.

Check the local setup without starting a model session:

```bash
npx session-handoff@latest doctor --pretty
```

Create mode writes handoffs under the workspace, usually in `handoffs/`. A clean repository does not need any project dependency or configuration file.

## Remove the managed setup

```bash
npx session-handoff@latest uninstall --yes
```

This restores the saved client launchers and removes the managed bundle and registrations.

## Telemetry

Telemetry is off by default. An interactive npm install asks once. Marketplace installs show a non-blocking reminder at session start. See [docs/telemetry.md](docs/telemetry.md) for the data inventory and controls.
