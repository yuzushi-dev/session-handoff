<p align="center">

  <img src="assets/session-handoff-mark.png" alt="session-handoff logo" width="96">

</p>

# session-handoff

**Handoff and migration plugin for Claude Code and Codex. Start a clean session with your decisions and pending work carried over, or migrate the whole native session to the other client instead.**

**Claude Code**:

```text
/session-handoff the next task is to run full pre-release suite
```

**Codex**:

```text
$session-handoff the next task is to refactor the benchmarking tool
```

Or migrate the whole session to the other client instead:

**Claude Code**:

```text
/session-handoff migrate codex
```

**Codex**:

```text
$session-handoff migrate claude
```

## Measured recovery


| Harness     | Benchmark result          | session-handoff | Native compact |
| ----------- | ------------------------- | ---------------: | --------------: |
| Codex       | Critical facts recovered  | **37/37**       | 37/37          |
| Codex       | Task success (valid runs) | **9/9**         | 9/9            |
| Claude Code | Critical facts recovered  | **6/6**         | 6/6            |
| Claude Code | Task success (valid runs) | **1/1**         | 1/1            |


Zero critical facts lost in either pilot: session-handoff matched native compact on
critical-fact recovery and task success, while starting from a fresh session. Measured
September 8, 2026, with Luna xhigh. Codex: ten synthetic case/context-length combinations.
Claude Code: one paired comparison on `compound-rot/long`. Each fact had to be recoverable in
both probe answers, assessed for meaning, and recorded in internal pilot reports.

## Install

Requirements: Linux or macOS (tested on Linux; macOS validation is pending), Python 3.10 or
newer, and Claude Code, Codex, or both. Git is needed for the marketplace install; Node.js 18
or newer and npm are needed only for the npm alternative below. Your Claude Code or Codex
installation may have its own separate dependencies.

The [yuzushi-plugins](https://github.com/yuzushi-dev/yuzushi-plugins) marketplace installs the
plugin from GitHub, on Python; it does not install or require Node/npm.

**Claude Code** — run in the session:

```text
/plugin marketplace add yuzushi-dev/yuzushi-plugins
/plugin install session-handoff@yuzushi
```

**Codex**:

```bash
codex plugin marketplace add yuzushi-dev/yuzushi-plugins
codex plugin add session-handoff@yuzushi
```

Start a new session with the plugin and its hooks enabled in a client that supports plugin
hooks, and approve the hook trust request if prompted. In the tested clients (Claude Code
2.1.263 and Codex 0.153.4) a notice then offers two independent, one-time choices: automatic
session switching, and telemetry — reply in chat with exactly `session-handoff telemetry yes` or
`session-handoff telemetry no`; it stays off without an explicit yes. Copy the Python setup
command the notice shows for your client into a terminal to enable
switching; setup displays its changes and asks for confirmation before making them, and skipping
it just means resuming handoffs manually. If you skipped it or your client does not show plugin
hooks, ask "Show session-handoff setup and telemetry commands" — the read-only `handoff_setup`
tool finds its own installation and returns the exact commands, so there is no path to look up.
After running setup, restart your terminal and launch `claude` or `codex` from it; supervision
covers those CLI processes, not a desktop app or IDE session. Setup itself never enables
telemetry.

Install with npm instead if you'd rather not use the marketplace (also needs Python 3.10+):

```bash
npx session-handoff@latest setup
```

Add `--client claude` or `--client codex` to select one client, and `--yes` for non-interactive
setup (it still does not grant telemetry consent). Restart your terminal and client afterward.

## What you get

- **Ref-first handoff records.** `handoff_create` stores an immutable record outside the
workspace and returns a `handoff://<project-uuid>/<handoff-uuid>` reference; a clean
repository needs no project file or dependency to use it.
- **Automatic session switching.** Once set up, the managed launcher can open the fresh session
for you after a handoff; migration between Claude Code and Codex works the same way.
- **Compaction recovery, kept separate.** A fail-open `PreCompact` hook writes a small redacted
checkpoint before compaction and reinjects only a pointer to it — recovery evidence, not a
semantic handoff.
- **Secrets redacted before storage.** The MCP server redacts common credential forms in every
handoff, on top of the model being told never to copy secrets into one.
- **Doctor without spending a turn.** `session-handoff doctor --human` reports client and
central-store readiness without starting a model session.
- **Telemetry off until you say yes.** No collection happens without an explicit choice; see
[docs/telemetry.md](docs/telemetry.md) for the full inventory and controls.

## Architecture in brief

```
host (Claude Code | Codex)
        │  session start / first use
        ▼
   skill + MCP server ──► central handoff store (ref-first, immutable)
        │                          ▲
        │                          └── handoff_read / handoff_list / handoff_search
        ▼
  managed launcher ──► auto-switch to a fresh session, or migrate to the other client
        │
        ▼
PreCompact hook ──► local recovery checkpoint (redacted, pointer-only reinjection)
```

The managed bundle carries `hooks/`, `server/` (MCP server and migration engine), `skills/`,
`bin/`, and `commands/`, copied once into a persistent per-user location by setup.

Release notes: [CHANGELOG.md](CHANGELOG.md).

---

<details>
<summary><b>Reference: first use and create mode</b></summary>

After restarting the client:

Claude Code:

```text
/session-handoff
```

Codex:

```text
$session-handoff
```

Use `migrate claude` or `migrate codex` to preserve the native session while changing clients.
A normal handoff starts a clean session and keeps the implementation state in the handoff file.

Create mode is central and ref-first: `handoff_create(name="next.md", workspace=...)` stores an
immutable record outside the workspace and returns its `handoff://<project-uuid>/<handoff-uuid>`
reference. If the MCP server is unavailable, use the explicit legacy fallback
`handoff_create(path="handoffs/next.md", workspace=...)`; that is the mode that writes a
workspace file.

</details>

<details>
<summary><b>Reference: compaction recovery checkpoint</b></summary>

Before Claude Code or Codex compacts a session, the plugin writes a small deterministic recovery
checkpoint under `~/.local/state/session-handoff/checkpoints/`. After
`SessionStart(source=compact)` it injects only a pointer to that file. The checkpoint contains
redacted Git state and a local lifecycle event log; it is recovery evidence, not a semantic
summary, and manual `$session-handoff` remains the semantic handoff path. Lifecycle events
record only hook names, IDs, paths, timestamps, and byte counts; they never contain prompts or
tool payloads.

The checkpoint hook is fail-open: a write or Git-read failure does not block compaction. Do not
treat its transcript path or Git output as secret-free project content; verify the live
repository and transcript before acting.

After managed setup, check it without starting a model session:

```bash
python3 "$HOME/.local/share/session-handoff/plugin/bin/session-handoff" doctor --pretty
python3 "$HOME/.local/share/session-handoff/plugin/bin/session-handoff" doctor --human
```

The default doctor output is machine-readable JSON. `--human` adds a compact read-only summary
of client readiness plus central data/state/catalog health; an absent store is reported
separately from an unsafe or corrupt one. Doctor does not create or repair the store.

</details>

<details>
<summary><b>Reference: setup recovery and uninstall</b></summary>

If a Codex update replaces the managed launcher while it is supervised, the supervisor restores
it when Codex exits and keeps the updated executable as its target. Claude version updates are
reconciled to the newest validated executable in its native versions directory. An installer run
outside a supervised session cannot be repaired automatically; rerun setup afterward for the
affected client.

Run the setup command for the client you want to configure, or both commands for both clients.
The client executable must already be on `PATH`. Setup installs a persistent user-scoped bundle,
registers the MCP server and skill, and saves the original launcher as
`*.session-handoff-original` before wrapping it.

Without chat hooks, run the returned `telemetry enable` command in a terminal to review the
consent prompt. If a choice is already pending, it prints the Python commands to answer yes or
no.

To remove the managed setup:

```bash
python3 "$HOME/.local/share/session-handoff/plugin/bin/session-handoff" uninstall
```

This restores the saved client launchers and removes the managed bundle and registrations.
Central projects and records under XDG data, bindings under XDG state, checkpoints, and saved
legacy handoffs are preserved. The command prints the exact `Central data`, `Central state`, and
`Checkpoints` paths.

</details>
