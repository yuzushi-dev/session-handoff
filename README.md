# Session Handoff

Create a handoff and continue in a fresh Codex or Claude session.

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

## Use

Launch Codex or Claude normally after setup, then use:

- Codex: `$session-handoff`
- Claude: `/session-handoff`

The plugin writes a validated Markdown file under `handoffs/`, starts a fresh
session, and pre-fills the chat with `reference [handoffs/<name>.md] riparti da
qui`. Press Enter to send it when you are ready.

The automatic switch requires the managed launcher. A client started through a
direct binary path uses the manual-resume fallback.

## Safety

- Handoffs stay inside the workspace.
- Existing files are not overwritten by default.
- Common credentials are redacted before writing and reading.
- The MCP server has no third-party runtime dependencies.

## Development

```bash
pytest -q
claude plugin validate --strict .
```
