# Central handoff store verification

Offline verification (2026-09-06):

```text
rtk pytest -q
742 passed, 2 skipped
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

Pending acceptance: a continuation contract beyond the bounded discovery window; Linux Claude, Linux Codex, macOS Claude, macOS Codex real-client discovery; supervised fresh-session switching; worktree/clone matrix. These remain unclaimed.
