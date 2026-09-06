# Central handoff store verification

Offline verification (2026-09-06):

```text
rtk pytest -q tests/test_handoff_store.py tests/test_handoff_mcp.py tests/test_session_switch.py
36 focused tests passed (the legacy MCP schema test expects the pre-central exact tool set).
rtk ruff check server/handoff_store.py server/handoff_mcp.py server/session_switch.py
clean
rtk proxy python3 -m compileall -q server
passed
rtk git diff --check
clean
```

The MCP stdio protocol was exercised with isolated temporary HOME/XDG roots for initialize, tools/list, central create/read/list/search, project association, and bundle import/export. No user home, credentials, hosted model, install, publish, or push was used.

Pending acceptance: Linux Claude, Linux Codex, macOS Claude, macOS Codex real-client discovery, supervised fresh-session switching, worktree/clone matrix, and real local setup refresh/uninstall. These require client/device access and remain unclaimed.
