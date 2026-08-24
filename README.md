# Session Handoff

Create a clean implementation-state handoff, or move the active native session between Codex and Claude Code.

## Install

```bash
npx session-handoff@latest setup
```

The setup asks for confirmation, installs the MCP server and skill, and wraps
the client launchers. It supports Linux and macOS with Node.js 18+ and Python
3.10+.

Remove the setup with:

```bash
npx session-handoff@latest uninstall
```

## Clean handoff

Launch Codex or Claude normally after setup, then use:

- Codex: `$session-handoff`
- Claude: `/session-handoff`

The plugin writes a validated Markdown file under `handoffs/`, starts a fresh
session, and pre-fills the chat with `reference [handoffs/<name>.md] riparti da
qui`. Press Enter to send it when you are ready.

Use this mode when you want to leave transcript noise and stale context behind
while preserving the exact implementation state needed to continue.

## Native migration

The same skill can preserve the portable native conversation and switch harness:

```text
# From Claude Code
/session-handoff migrate codex

# From Codex
$session-handoff migrate claude
```

Migration is deliberately separate from a normal handoff. The managed launcher
stops the source client first, then delegates conversion to the external
`session-migrate` CLI. It runs a dry-run and apply with the same generated
target session ID and opens the target through its native resume mechanism.
If conversion fails after the source client is stopped, the launcher resumes
the original source session.

`session-migrate` is an optional backend and is not installed by
`session-handoff`. Install it separately when you want migrate mode. The current
upstream package supports Linux and Python 3.11+:

```bash
uv tool install session-migrate
```

Normal Markdown handoffs do not depend on `session-migrate` and keep working
when the backend is absent. Migrate mode currently exposes only Claude Code ↔
Codex even though the upstream migrator supports more formats.

## Safety

- Handoffs stay inside the workspace.
- Existing files are not overwritten by default.
- Common credentials are redacted before writing and reading handoff files.
- Native migration uses the exact active session ID; it never guesses from the newest transcript.
- The source client is stopped before its transcript is migrated.
- The source native session is left intact by `session-migrate`.
- Migration warnings and dropped/transformed event counters are printed before the target resumes.
- The MCP server has no third-party runtime dependencies.

The automatic switch and migration flows require the managed launcher. A client
started through a direct binary path uses the manual handoff fallback and does
not attempt to migrate a live transcript.

## Development

```bash
pytest -q
claude plugin validate --strict .
```
