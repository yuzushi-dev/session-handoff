# Paginated Codex Backend Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Convert Codex paginated thread history into a new Claude native session without modifying the Codex source.

**Architecture:** Read the canonical `thread_items` projection from `thread_history_1.sqlite`, project supported items into a temporary legacy Codex JSONL view, then use the internal Claude writer. The source remains read-only; every unsupported paginated item is counted and the generated target uses a fresh UUID.

**Tech Stack:** Python 3.10+, stdlib `sqlite3`/`json`, pytest, bundled migration engine.

---

### Task 1: Add the paginated projection tests

**Files:**
- Create: `tests/test_paginated_migration.py`

Write failing tests for the SQLite projection: user/assistant messages become canonical Codex response items, command executions become linked tool call/output pairs, reasoning is counted and omitted, and the source database is opened read-only.

Run: `python3 -m pytest -q tests/test_paginated_migration.py`

Expected: collection failure until the projection module exists.

### Task 2: Implement the temporary Codex view

**Files:**
- Create: `server/paginated_migration.py`
- Modify: `server/migration.py`

Implement bounded read-only SQLite discovery, structural metadata validation, item projection, private temporary JSONL creation, and aggregation of loss counters. Extend the transfer command with optional `--source-home` and `--home` only for the paginated path; retain the existing external path for legacy sources.

Run: `python3 -m pytest -q tests/test_paginated_migration.py tests/test_migration.py`

Expected: all targeted tests pass.

### Task 3: Verify native installation and regressions

**Files:**
- Modify: `tests/test_migration.py`
- Modify: `README.md`

Add an isolated CLI integration test using a synthetic paginated Codex home and temporary Claude home. Verify dry-run/apply share one target UUID, the source hash is unchanged, the target JSONL is Claude-shaped, and warnings/counters are returned. Document the supported item mapping and known omissions.

Run: `python3 -m pytest -q`, `claude plugin validate --strict .`, and `git diff --check`.

Expected: full local suite passes; the isolated target can be resumed by the installed Claude CLI when credentials are available.
