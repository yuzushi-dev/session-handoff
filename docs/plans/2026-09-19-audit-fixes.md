# Session Handoff Audit Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix six confirmed audit defects while preserving compatibility and fail-open behavior.

**Architecture:** Keep existing redaction, checkpoint, migration, supervisor, and onboarding boundaries. Add identity-aware handling only at affected boundaries, with failing regressions before each minimal fix.

**Tech Stack:** Python 3, pytest, temporary fixtures, mocked network/processes, Ruff.

---

### Task 1: Structured TypeSafe redaction

Files: server/typesafe_client.py, server/redaction.py, tests/test_typesafe_judge.py

1. Add a failing intercepted HTTP payload test for nested sensitive dict/list values.
2. Implement shared key-aware recursive redaction.
3. Run TypeSafe and redaction tests.

### Task 2: YAML credential block redaction

Files: server/redaction.py, tests/test_redaction.py, tests/test_handoff_store.py

1. Add failing block-scalar tests for chomping/indent indicators, quoted keys, siblings, and idempotence.
2. Implement bounded line-based block consumption.
3. Verify temporary create/read/export persistence.

### Task 3: Remaining audit fixes

Files: server/session_switch.py, server/checkpoint.py, server/onboarding.py, server/migration.py, server/migration_engine.py, README.md, related tests

1. Add failing regressions for rollback, identity-bound checkpoints, retriable onboarding, provenance, and scorer wording.
2. Implement minimal fixes and run focused suites.
3. Run full pytest, Ruff, diff check, and graphify if available; leave uncommitted.
