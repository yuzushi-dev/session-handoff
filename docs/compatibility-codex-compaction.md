# Codex native compaction compatibility

This bounded pilot exercised Codex CLI `0.159.2` on Linux with `gpt-5.6-luna`, reasoning `low`, final-C `session-handoff` `0.7.4-jev.3`, and Sando `0.7.0`. Both plugins were installed and enabled together in an isolated `CODEX_HOME`. Authentication came from the existing monthly subscription; no API credential environment was inherited.

The tested final-C artifact has content SHA-256 `05268602f4f5228b684d726470025562ce9187cb873176e6310dcaf79d518d9f` and bundle SHA-256 `9cd6cefc7574c0fd4e43bdaa5d28752f541355f3002aea2529817aa0ec2715b0`.

## Observed compaction lifecycle

Codex documents `PreCompact` for manual and automatic compaction and `SessionStart` with source `compact` before the next model request. It also warns that transcript format is unstable. Plugin hooks are loaded alongside other hook sources. These are normative client statements; the table below contains only events observed in this pilot. See the official [hooks reference](https://learn.chatgpt.com/docs/hooks) and [plugin packaging guide](https://developers.openai.com/plugins/build/plugins).

| Mode | Initial hook event | Checkpoint event | Native final event | Transcript | Reinjection observed | Deduplication |
|---|---|---|---|---|---|---|
| Manual `/compact` | `SessionStart(startup)`, not injected | `PreCompact(manual)`, 1,569 bytes | one native `compacted` record | available, 136,816 bytes | `SessionStart(compact)`, 360 bytes, exact path consumed in the same TUI process | one injected event and one mode-0600 marker for checkpoint `0008f527…0668` |
| Automatic | `SessionStart(startup)`, not injected | `PreCompact(auto)`, 1,567 bytes | one native `compacted` record | available | `SessionStart(compact)`, 360 bytes; exact path and 3/3 facts recovered | one injected event and one mode-0600 marker for checkpoint `a0a2876f…f9b` |

Each transition also emitted a non-injecting `SessionStart(resume)` before the injecting compact event. The compact event remained the sole event that claimed the checkpoint marker. This is observed behavior for CLI `0.159.2`, not a stable transcript contract.

The automatic case used CLI overrides `model_context_window=16000` and `model_auto_compact_token_limit=10000` with `--strict-config`. The initial 33,423-token turn did not compact. The second turn crossed the configured local threshold and completed the full automatic cycle. These overrides were confined to the isolated lab.

## Combined Sando and handoff session

A separate real Codex session loaded both plugins. A shell read of the 160,256-byte synthetic fixture traversed Sando and created a mode-0600, 160,336-byte artifact with handle `sando:sha256:0aba07379f84c7e4`. The session then:

1. created and validated a 1,947-byte handoff containing the exact handle and recovery command;
2. read the handoff back successfully;
3. recovered the existing artifact and its expected first line with the installed Sando CLI;
4. requested `sando:sha256:0000000000000000`, which exited `2` with `artifact handle is unavailable`.

The Sando MCP subprocess used a different workspace root and could not resolve the workspace-local artifact. The installed Sando CLI in the same Codex session performed the successful recovery. No missing content was invented.

## Evidence and limits

The structured result is [receipt.json](../.orca/compat-pilot/codex-native-gates/receipt.json). The sanitized event summaries are [manual-observed.json](../.orca/compat-pilot/codex-native-gates/manual-observed.json), [automatic-observed.json](../.orca/compat-pilot/codex-native-gates/automatic-observed.json), and [combined-observed.json](../.orca/compat-pilot/codex-native-gates/combined-observed.json).

Several PTY attempts were invalid because of zero-width rendering, fixed timing, or sending the command and Enter in one burst. They are recorded as harness failures in the receipt and do not count as plugin failures. The valid manual driver used a 160×40 PTY, typed `/compact` character by character, waited three seconds, sent a separate carriage return, waited for the fresh `Context compacted` output, then submitted `checkpoint?` the same way.

This is a single-host, single-client-version, single-model pilot. It supports the stated Codex CLI surfaces only. It does not establish behavior for the app-server API, desktop or IDE clients, other operating systems, or future transcript formats.
