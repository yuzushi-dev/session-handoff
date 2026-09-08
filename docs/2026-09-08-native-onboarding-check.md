# Native onboarding check — 2026-09-08

The current checkout displays onboarding in Claude Code 2.1.263. It does **not** display onboarding in Codex 0.153.4 with its current packaging. The Python/unit tests did not catch this native manifest interaction.

## Method

Installed the current plugin snapshot through temporary local marketplaces, with isolated HOME/client configuration and native binaries, bypassing personal launchers. The PTY supplies terminal size and responds to terminal capability/cursor/color queries. Claude uses an explicitly approved synthetic API key and loopback endpoint; Codex uses a synthetic custom provider pointing at `127.0.0.1:1`. No real provider credentials or paid model calls. Codex's ordinary plugin catalog metadata requests are separate from inference.

Evidence directory: `/tmp/handoff-ui-probe-wa5xeibc/`. Only temporary copies were changed for the packaging experiments; project runtime/packaging files remain unchanged by this check.

## Results

| Case | Observed behavior |
|---|---|
| Claude, current snapshot, local marketplace install | The native TUI displays `SessionStart:startup says:` followed by both setup commands and separate telemetry consent. |
| Codex, current snapshot, local marketplace install | MCP starts, but `/hooks` and `hooks/list` show zero plugin hooks. No onboarding notice. |
| Codex, root portable manifest moved aside in temporary copy | Three plugin hooks become visible. Trusting them in `/hooks` enables them. Existing MCP command then fails because its root variable is not expanded by this manifest format. |
| Codex, native manifest plus relative MCP working directory in temporary copy | MCP starts and onboarding displays on the first submitted message, after hook trust. Both commands point to the actual plugin cache. The subsequent loopback connection failure is expected; no model response is needed to display the notice. |

Claude startup evidence: `claude-market-terminal.txt` and `claude-market-debug.log`. Current Codex failure: `terminal.txt` and isolated `codex/logs_2.sqlite`. Native-only intermediate: `native-only-terminal.txt`. Working Codex candidate: `fixed-terminal.txt` and `fixed-probe.py`. Scripts and configuration are preserved beside these files. The scripts' historical final `marker` print uses the original home path in some variants; actual notice rendering and each configured home are the evidence.

## Cause and tested correction

Codex prioritizes root `plugin.json` as the Agent Plugins manifest. Its exact release source explicitly loads no hook sources for that format: [0.153.4 loader](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core-plugins/src/loader.rs#L954). The `.codex-plugin/plugin.json` overlay does not restore hook loading in that branch.

The temporary candidate keeps `.codex-plugin/plugin.json` and `.claude-plugin/plugin.json`, moves root `plugin.json` aside, and defines this Codex-specific MCP entry:

```json
"mcpServers": {
  "session-handoff": {
    "command": "python3",
    "args": ["server/handoff_mcp.py"],
    "cwd": "."
  }
}
```

The native parser resolves relative `cwd` against the plugin installation. It does not expand `${CLAUDE_PLUGIN_ROOT}` or `${PLUGIN_ROOT}` in the tested legacy MCP arguments: [0.153.4 MCP parser](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/codex-mcp/src/plugin_config.rs#L281). Claude can retain its existing `.mcp.json`.

Applying this correction requires updating packaging/setup/version checks consistently and deciding to stop exposing the portable Agent Plugins manifest at the bundle root. This is a compatibility tradeoff, not merely a wording fix. The candidate is proven only in the isolated copy; no packaging change has been applied to the project or published.

## Limits

- Codex displays the SessionStart notice when the first message creates the session, not necessarily when the empty TUI opens. Hook trust must be granted first.
- This checks actual native notice rendering and MCP startup, not another automatic handoff/migration cycle or production telemetry delivery.
- The prior unauthenticated probe's blank UI was not proof authentication was required: it had not answered terminal queries. Synthetic local-provider configuration suffices for this check.
# Applied correction

The user approved the compatibility change. The repository now uses native
Claude/Codex manifests. The portable root manifest is archived at
`docs/portable/plugin.json` and excluded from npm and managed runtime bundles.
Codex's native manifest launches Python with `cwd: "."` and the relative
`server/handoff_mcp.py` argument, matching the successful native UI probe below.
The following test observations describe the original package and isolated fix.

Post-change checks: 135 packaging/setup/version/onboarding/hook/MCP tests and
24 efficiency regression tests passed. Scoped Ruff and `git diff --check` passed.
The updated repository manifest was copied into the isolated Codex installation
and tested again with a fresh onboarding home: MCP started and both notices
rendered at the first message. Evidence: `applied-terminal.txt` and
`applied-probe.py` under the same temporary evidence directory below.
