# Central handoff store verification

Offline verification (2026-09-06):

```text
rtk pytest -q
743 passed, 2 skipped
rtk pytest -q tests/test_handoff_store.py tests/test_handoff_mcp.py tests/test_session_switch.py
95 focused tests passed
rtk pytest -q tests/test_handoff_store.py tests/test_handoff_mcp.py -k 'concurrent or stdio'
2 passed
rtk pytest -q tests/test_package.py -k central_store
1 passed
rtk proxy claude plugin validate --strict .
Validation passed
rtk npm pack --dry-run --json
entryCount=29; server/handoff_store.py and server/redaction.py included
rtk ruff check server/handoff_store.py server/handoff_mcp.py server/session_switch.py
clean
rtk proxy python3 -m compileall -q server
passed
rtk git diff --check
clean
```

The MCP stdio protocol was exercised in isolated temporary HOME/XDG roots: central create returned a two-UUID reference, followed by separate read/list/search requests (3 responses captured). `handoff_project` was also exercised.
Legacy import→export→idempotent reimport stdio test: `1 passed`; source bytes, manifest/hash and ref preservation verified. Central switch request test: `1 passed`; payload contains `ref` and no legacy `path`.

The consumer revalidates the central reference against the current workspace binding and record at consume time; forged, cross-project, missing-record, and exact-one path/ref payloads are rejected. Export requires exactly the two regular bundle files and compares both bytes on idempotent retry.

The store rejects secret-bearing documents and metadata, unsafe ownership/modes, corrupt manifests, symlinked records, and non-UTF-8 bundles. Project/record discovery is capped and reports `scan_truncated`; the capped subset is deterministic but does not yet expose a continuation cursor beyond the scan window.

## Real-client acceptance (Linux, 2026-09-06)

- Claude Code `2.1.263`, effective model `claude-sonnet-5`: MCP discovery (9 tools), central create/read/list/search/export/import, idempotent re-import, second-invocation resume, legacy create/read, and supervised fresh-session switch all passed. The supervised run terminated the source, opened a new session with the exact ref as an unsent draft, then submitted it and read the expected handoff.
- Codex CLI `0.153.4`, model `gpt-5.6-luna`, reasoning `medium`: MCP discovery, central create/read/validate/list/search/export/import, second-invocation resume, legacy create/read/import, worktree-shared identity, and separate-clone identity passed. The supervised run terminated the source and opened a fresh TUI with the exact ref as an unsent draft. The draft was submitted, but the controlled PTY ended before a readable post-submit tool result was captured; that last evidence remains partial.
- Real-client testing exposed and fixed two compatibility bugs: Claude sends the standard MCP `_meta` tool-call parameter, and managed setup previously left the shared application data root at `0775` while the store requires `0700`. Setup now validates ownership/type and makes only its own application root private. The managed bundle and both launchers are installed locally; original launchers remain backed up as `*.session-handoff-original`.

Pending acceptance: a continuation contract beyond the bounded discovery window; readable post-submit evidence for the supervised Codex run; macOS Claude and macOS Codex. Linux worktree/clone identity is complete. No push, publish, or deploy was performed.
