# session-handoff

<p align="center">
  <img src="assets/session-handoff-mark.png" alt="session-handoff logo" width="96">
</p>

## Start fresh. Keep what matters.

Pick up where you left off in a fresh Claude Code or Codex session. Save the decisions you've made and the work still to do in a handoff your next session can read.

**Zero critical facts lost in our pilots.**

In both pilots, session-handoff matched native compact on critical-fact recovery and task success, while starting a fresh session.

| Harness | Benchmark result | session-handoff | Native compact |
|---|---|---:|---:|
| Codex | Critical facts recovered | **37/37** | 37/37 |
| Codex | Task success (valid runs) | **9/9** | 9/9 |
| Claude Code | Critical facts recovered | **6/6** | 6/6 |
| Claude Code | Task success (valid runs) | **1/1** | 1/1 |

September 8, 2026, with Luna xhigh. Codex: ten synthetic case/context-length combinations. Claude Code: one paired comparison on `compound-rot/long`. Each fact had to be recoverable in both probe answers, assessed for meaning. Results and methodology: [Codex](docs/2026-09-08-information-preservation-pilot.md) · [Claude Code](docs/2026-09-08-claude-information-pilot.md).

You can also migrate a native session between Claude Code and Codex when you want to switch clients without starting over.

The plugin includes the skills, MCP server, client launchers, and migration engine.

## Requirements

- Linux or macOS (this release candidate has been tested on Linux; macOS validation is pending)
- Python 3.10 or newer
- Claude Code, Codex, or both, depending on which client you want to configure
- Git for installation from the marketplace
- Node.js 18 or newer and npm **only for the npm installation option**

These are session-handoff's requirements. Your Claude Code or Codex installation may have its own dependencies.

## Install from the marketplace

The [yuzushi-plugins](https://github.com/yuzushi-dev/yuzushi-plugins) marketplace installs the plugin from GitHub. The plugin and its launcher setup run on Python; they do not install or require Node/npm.

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

Start a new session with the plugin and its hooks enabled in a client that supports plugin hooks. Approve the client's hook trust request if prompted. In the tested clients (Claude Code 2.1.263 and Codex 0.153.4), the notice appears at startup in Claude and after submitting the first message in Codex. It offers two independent choices:

- **Automatic session switching:** copy the Python setup command shown for your client into a terminal. It already contains the installed plugin's full path. Setup shows its changes and asks for confirmation. Skip it to resume handoffs manually.
- **Telemetry:** reply in chat with exactly `session-handoff telemetry yes` or `session-handoff telemetry no`. It stays off without an explicit yes.

After setup, restart your terminal and launch `claude` or `codex` from it. The launcher can then open a fresh CLI session after a handoff or migrate between clients. It does not restart desktop app or IDE sessions.

The notices appear once. If you skipped setup or your client does not display plugin hooks, ask “Show session-handoff setup and telemetry commands.” The plugin's read-only `handoff_setup` tool finds its own installation and returns complete commands. You do not need to find a directory or fill in a path.

Run the setup command for the client you want to configure, or both commands for both clients. The client executable must already be on `PATH`. Setup installs a persistent user-scoped bundle, registers the MCP server and skill, and saves the original launcher as `*.session-handoff-original` before wrapping it. A setup confirmation never enables telemetry.

Without chat hooks, run the returned `telemetry enable` command in a terminal to review the consent prompt. If a choice is already pending, it prints the Python commands to answer yes or no.

## Install with npm

This alternative requires Node.js 18 or newer, npm, and Python 3.10 or newer:

```bash
npx session-handoff@latest setup
```

Use `--client claude` or `--client codex` to select one client. Add `--yes` for non-interactive setup; it does not grant telemetry consent. Restart your terminal and client afterward.

If a Codex update replaces the managed launcher while it is supervised, the supervisor restores it when Codex exits and keeps the updated executable as its target. Claude version updates are reconciled to the newest validated executable in its native versions directory. An installer run outside a supervised session cannot be repaired automatically; rerun setup afterward for the affected client.

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

Before Claude Code or Codex compacts a session, the plugin writes a small
deterministic recovery checkpoint under
`~/.local/state/session-handoff/checkpoints/`. After `SessionStart(source=compact)`
it injects only a pointer to that file. The checkpoint contains redacted Git
state and a local lifecycle event log; it is recovery evidence, not a semantic
summary, and manual `$session-handoff` remains the semantic handoff path.
Lifecycle events record only hook names, IDs, paths, timestamps, and byte
counts; they never contain prompts or tool payloads.

The checkpoint hook is fail-open: a write or Git-read failure does not block
compaction. Do not treat its transcript path or Git output as secret-free
project content; verify the live repository and transcript before acting.

After managed setup, check it without starting a model session:

```bash
python3 "$HOME/.local/share/session-handoff/plugin/bin/session-handoff" doctor --pretty
python3 "$HOME/.local/share/session-handoff/plugin/bin/session-handoff" doctor --human
```

The default doctor output is machine-readable JSON. `--human` adds a compact
read-only summary of client readiness plus central data/state/catalog health;
an absent store is reported separately from an unsafe or corrupt one. Doctor
does not create or repair the store.

Create mode is central and ref-first: `handoff_create(name="next.md", workspace=...)` stores an immutable record outside the workspace and returns its `handoff://<project-uuid>/<handoff-uuid>` reference. If the MCP server is unavailable, choose the explicit legacy fallback `handoff_create(path="handoffs/next.md", workspace=...)`; that is the mode that writes a workspace file. A clean repository does not need any project dependency or configuration file.

## Remove the managed setup

```bash
python3 "$HOME/.local/share/session-handoff/plugin/bin/session-handoff" uninstall
```

This restores the saved client launchers and removes the managed bundle and
registrations. Central projects and records under XDG data, bindings under XDG
state, checkpoints, and saved legacy handoffs are preserved. The command prints
the exact `Central data`, `Central state`, and `Checkpoints` paths.

## Telemetry

Telemetry is off by default and independent of automatic session switching. With plugin hooks enabled, marketplace installs show a non-blocking consent notice at session start; interactive setup or npm installation can ask if no choice has been recorded. See [docs/telemetry.md](docs/telemetry.md) for the data inventory and Python controls.
