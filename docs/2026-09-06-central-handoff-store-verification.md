# Central handoff store verification

Offline verification (2026-09-06):

```text
rtk pytest -q
716 passed, 2 skipped in 44.65s
rtk pytest -q tests/test_handoff_store.py tests/test_handoff_mcp.py tests/test_session_switch.py
40 focused tests passed
rtk ruff check server/handoff_store.py server/handoff_mcp.py server/session_switch.py
clean
rtk proxy python3 -m compileall -q server
passed
rtk git diff --check
clean
```

The MCP stdio protocol was exercised in isolated temporary HOME/XDG roots for `initialize`, `tools/list`, central `handoff_create`, and `handoff_project`; captured create output returned `storage: "central"`, a two-UUID `handoff://` reference, and `registered: true`. Read/list/search/import/export/switch round-trip remains pending a deterministic fixture runner.

Pending acceptance: Linux Claude, Linux Codex, macOS Claude, macOS Codex real-client discovery, supervised fresh-session switching, worktree/clone matrix, and real local setup refresh/uninstall. These require client/device access and remain unclaimed.
