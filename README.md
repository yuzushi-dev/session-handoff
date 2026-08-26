# session-handoff

Create handoffs or migrate active sessions between Claude Code and Codex.

## Install the plugin

Claude Code:

```text
/plugin marketplace add yuzushi-dev/yuzushi-plugins
/plugin install session-handoff@yuzushi
```

Codex:

```bash
codex plugin marketplace add yuzushi-dev/yuzushi-plugins
```

Then run `/plugins` and install `session-handoff`.

## Managed setup

Use this alternative when you want managed launchers and automatic switching:

```bash
npx session-handoff@latest setup
```

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
