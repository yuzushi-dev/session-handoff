# session-handoff

Create handoffs or migrate active sessions between Claude Code and Codex.

## Managed setup

```bash
npx session-handoff@latest setup
```

This installs the managed launchers, MCP server, skills, and internal Claude↔Codex migration engine. No separate migration package or executable is required.

## Use

Claude Code:

```text
/session-handoff
```

Codex:

```text
$session-handoff
```

## Telemetry

Telemetry is off by default. npm install asks once; plugin installs show a reminder until you choose.
