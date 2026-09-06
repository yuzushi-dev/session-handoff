# Central handoff store verification

Offline verification (2026-09-06):

```text
rtk pytest -q
724 passed, 2 skipped in 61.68s (0:01:01)
rtk pytest -q tests/test_handoff_store.py tests/test_handoff_mcp.py tests/test_session_switch.py
40 focused tests passed
rtk pytest -q tests/test_handoff_store.py tests/test_handoff_mcp.py -k 'concurrent or stdio'
2 passed
rtk pytest -q tests/test_package.py -k central_store
1 passed
rtk proxy claude plugin validate --strict .
Validation passed
rtk npm pack --dry-run --json
entryCount=28; server/handoff_store.py included
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

Pending acceptance: Linux Claude, Linux Codex, macOS Claude, macOS Codex real-client discovery, supervised fresh-session switching, worktree/clone matrix. These require client/device access and remain unclaimed.
