# Claude compaction compatibility — 2026-09-30

These live tests exercised the frozen final-C package with Claude Code `2.1.285`, the native Anthropic backend, and resolved model `claude-sonnet-5-5` at low effort. The package identity was `session-handoff 0.7.4-jev.3`, packaged-content SHA-256 `05268602f4f5228b684d726470025562ce9187cb873176e6310dcaf79d518d9f`.

## Results

| Case | Initial event | Native transcript boundary | Checkpoint | Post-compact reinjection | Result |
| --- | --- | --- | --- | --- | --- |
| Manual `/compact` | `SessionStart(startup)`, session `aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaae` | `compact_boundary(trigger=manual)`: 31,308 pre-tokens, 2,732 post-tokens, 28,576 dropped; one compact summary | `PreCompact(manual)`, 2,680 bytes | `SessionStart(compact)`, `injected=true`, 403 bytes; one matching `.injected-*` claim marker | `passed` |
| Automatic `--autocompact 100k` | `SessionStart(startup)`, then native resume of session `bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb` | `compact_boundary(trigger=auto)`: 327,731 pre-tokens, 2,042 post-tokens, 325,689 dropped; one compact summary | Native `PreCompact(auto)` events; final checkpoint 2,690 bytes | `SessionStart(compact)`, `injected=true`, 406 bytes; one matching `.injected-*` claim marker | `passed` |

The automatic threshold is a documented Claude CLI option: `--autocompact` accepts `auto` or `100k–1M` tokens. The initial stream process wrote real `PreCompact(auto)` events. Continuing the same native session with the same `100k` threshold completed the compact boundary and emitted `SessionStart(source=compact)`.

The lifecycle JSONL and native transcript agree on trigger and session ID. Reinjection is established by the plugin event with nonzero `injected_bytes` and the checkpoint-specific claim file, not by the model returning the synthetic marker. The automatic continuation returned `READY3`, but that response is only supplemental evidence. Each tested checkpoint has one compact-session injection event and one matching claim marker; Claude did not emit a duplicate `SessionStart(compact)` for either checkpoint.

## Evidence

The local receipt is `.orca/compat-lab/claude-compaction-new/receipt.json`. It retains host paths for replay and requires path redaction before sharing. Replayable synthetic Claude transcripts and lifecycle JSONL files remain under the same ignored lab directory. The receipt includes client, model, build, platform, transcript hashes, compact metadata, checkpoint sizes, injection counts, and marker modes.

The first terminal attempt used the outer command runner's pseudo-terminal and Claude treated it as noninteractive print mode. A local `pexpect` PTY reached the native UI. Its first input used line feed, which Ink displayed without submitting; the corrected driver used carriage return. These harness attempts created no compaction evidence and are excluded from the passing sessions.

All workspaces and HOME/XDG roots were synthetic and isolated. Account files were copied with mode `0600`, never included in receipts, and removed after the tests.
