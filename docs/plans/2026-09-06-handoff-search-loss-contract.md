# Handoff Search and Migration Loss Contract Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add bounded search over saved handoffs, harden redaction, and state the portable migration boundary without importing client-private memory.

**Architecture:** Keep all search behavior behind one read-only MCP tool in `server/handoff_mcp.py`, reusing `main`'s secure file reader. Add only a generic paginated-migration warning; do not inspect undocumented Codex item formats.

**Tech Stack:** Python 3.10+, MCP JSON-RPC, pytest, Ruff.

---

### Task 1: Redaction hardening

**Files:** `server/handoff_mcp.py`, `tests/test_handoff_mcp.py`

1. Add a failing test for an attached secret after `[REDACTED]`.
2. Run the focused test and confirm the secret leaks.
3. Add the minimal malformed-marker redaction.
4. Run the focused test and existing redaction tests.

### Task 2: Bounded `handoff_search`

**Files:** `server/handoff_mcp.py`, `tests/test_handoff_mcp.py`, `skills/session-handoff/SKILL.md`

1. Add failing tests for tool discovery, redacted matches, traversal rejection, pagination, and global scan truncation.
2. Confirm failures are due to the missing tool.
3. Implement one read-only MCP tool restricted to `handoffs/`, with query/snippet/match, file, byte, and output budgets.
4. Run focused tests and Ruff.

### Task 3: Migration contract

**Files:** `server/paginated_migration.py`, `tests/test_paginated_migration.py`, `skills/session-handoff/SKILL.md`

1. Add a failing test requiring a generic additive warning while preserving `dropped_events`.
2. Confirm the warning is absent.
3. Add the warning without parsing or naming undocumented item types.
4. Run paginated migration tests.

### Task 4: Verification

1. Run focused tests.
2. Run the full suite, Ruff, compileall, and `git diff --check`.
3. Review the final diff against `main`; commit locally without pushing.
