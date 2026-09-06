# Central Handoff Cursor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make every central handoff reachable through stable list and search pagination beyond the current 256-record scan window.

**Architecture:** Keep immutable `document.md` and `manifest.json` records authoritative. Maintain a private SQLite catalog in XDG state as a rebuildable index with a monotonic sequence; expose opaque cursors bound to operation, scope, project, and search query. Rebuild the catalog from canonical records when it is absent or invalid.

**Tech Stack:** Python 3.10+ standard library (`sqlite3`, `base64`, `json`, `hashlib`), MCP JSON-RPC, pytest.

---

### Task 1: Specify list cursor behavior

**Files:**
- Modify: `tests/test_handoff_store.py`

1. Add a failing test that creates more records than the old scan cap and follows `next_cursor` until every reference is returned exactly once.
2. Add failing tests for malformed cursors and cursors reused with another scope or project.
3. Run `rtk pytest -q tests/test_handoff_store.py -k cursor`; expect failures because `list_records` has no cursor contract.

### Task 2: Add the rebuildable SQLite catalog

**Files:**
- Modify: `server/handoff_store.py`
- Modify: `tests/test_handoff_store.py`

1. Create the private catalog under `$XDG_STATE_HOME/session-handoff/catalog.sqlite3` with schema version 1 and a unique `(project_id, handoff_id)` row ordered by an autoincrement sequence.
2. Index a record only after its canonical directory is durably published.
3. Rebuild an absent, corrupt, or incomplete catalog from canonical record identities under the existing process lock; validate canonical content as pages are returned.
4. Reject unsafe catalog file types, ownership, or permissions; use SQLite's default rollback journal and filesystem mode `0600`.
5. Run focused store tests until green.

### Task 3: Implement opaque list cursors

**Files:**
- Modify: `server/handoff_store.py`
- Modify: `tests/test_handoff_store.py`

1. Encode only cursor version, operation, scope, bound project, sequence, and optional query digest as base64url JSON.
2. Validate types and context on every request; authorization remains workspace-derived.
3. Query `sequence > cursor.sequence`, return `limit` validated records and a cursor only when more catalog rows exist.
4. Preserve offset pagination for workspace-local storage; central storage rejects nonzero offset and cursor/offset combinations.

### Task 4: Add resumable central search

**Files:**
- Modify: `server/handoff_mcp.py`
- Modify: `tests/test_handoff_mcp.py`

1. Add failing tests showing a match beyond one file/byte scan budget is reachable on a later cursor page.
2. Bind search cursors to the redacted, case-folded query digest.
3. Advance the cursor only past records actually processed; stop at match, file, byte, or output budgets without skipping an unreturned match.
4. Keep legacy workspace search and offset behavior unchanged.

### Task 5: Publish the MCP contract and documentation

**Files:**
- Modify: `server/handoff_mcp.py`
- Modify: `README.md`
- Modify: `docs/2026-09-06-central-handoff-store-verification.md`

1. Add optional `cursor` to list/search schemas and describe it as central-only.
2. Document that the catalog is local, derived, and excluded from portable bundles.
3. Document `next_cursor`, invalidation rules, and recovery behavior.

### Task 6: Verify the release candidate

1. Run focused store and MCP tests.
2. Run the full pytest suite, Ruff, compileall, git diff-check, strict Claude plugin validation, and npm pack dry-run.
3. Review the final diff for authorization, pagination gaps/duplicates, recovery, and compatibility.
4. Commit only after all checks pass.
