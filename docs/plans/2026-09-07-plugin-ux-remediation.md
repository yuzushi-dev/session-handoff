# Plugin UX Remediation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make central handoff, consent, diagnostics, uninstall, and CLI recovery flows explicit, actionable, and safe without changing legacy compatibility or adding a second backend.

**Architecture:** Keep `server/handoff_store.py` as the only central-store API. Extend existing MCP/CLI surfaces with bounded read-only operations and human-readable messages while preserving current JSON fields and privacy boundaries. Keep telemetry setup one-time and privacy-preserving; only an explicit `telemetry enable` command may reopen a prior decline.

**Tech Stack:** Python 3.10+ standard library, argparse, JSON-RPC MCP, pytest, Ruff, npm package checks.

---

### Task 1: Make relaunch prompts exact and safe

**Files:**
- Modify: `server/session_switch.py`
- Test: `tests/test_session_switch.py`

1. Add failing tests for exact central and legacy one-line prompt strings, including quotes/newlines/backslashes in values.
2. Run `rtk pytest -q tests/test_session_switch.py -k handoff_prompt`; expect the old `Resume, do not create:` wording to fail.
3. Implement JSON-string quoting with deterministic double quotes and the exact `Resume task: call handoff_read(...) ... Do not create a new handoff.` shape; append the legacy `Legacy uses path="...".` guidance only for path prompts.
4. Run the focused tests and the existing supervisor prompt tests; expect all pass.

### Task 2: Clarify the `handoff_create` MCP contract

**Files:**
- Modify: `server/handoff_mcp.py`
- Test: `tests/test_handoff_mcp.py`
- Docs: `README.md`, `skills/session-handoff/SKILL.md`, `docs/plans/2026-09-06-central-handoff-store.md`

1. Add a schema/description assertion covering central `name`, legacy `path`, legacy-only `overwrite`, and managed-launcher-only `auto_switch` semantics.
2. Run the focused assertion; expect the current generic description to fail.
3. Replace only the tool description/schema field descriptions with the explicit central/legacy contract; preserve runtime compatibility and existing argument validation.
4. Run MCP contract tests and confirm the docs use the same wording.

### Task 3: Make cross-project read errors actionable

**Files:**
- Modify: `server/handoff_store.py` or `server/handoff_mcp.py` (single shared error boundary)
- Test: `tests/test_handoff_mcp.py`

1. Add bound and unbound workspace tests that read a valid foreign ref without `scope="all"`, asserting the error names `scope='all'` and `handoff_project`, while excluding workspace paths and document content.
2. Run them RED against the current opaque outside-project error.
3. Add one stable, privacy-safe remediation message; preserve successful explicit `scope="all"` reads and explicit rebind behavior.
4. Run the focused read/rebind tests GREEN.

### Task 4: Allow explicit CLI telemetry re-enable

**Files:**
- Modify: `server/telemetry.py`, `bin/session-handoff`
- Test: `tests/test_telemetry_cli.py`

1. Change/add a test showing `telemetry enable` transitions `disabled_config()` to enabled without prompting again; retain a setup/reinstall test proving decline is not re-prompted or changed by setup.
2. Run focused CLI/setup consent tests RED.
3. Add a narrowly scoped atomic explicit-enable transition used only by the CLI; leave `request_consent` and setup's no-reprompt path unchanged.
4. Run all telemetry CLI and setup consent tests GREEN, including `DO_NOT_TRACK` and noninteractive cases.

### Task 5: Extend doctor with read-only store health

**Files:**
- Modify: `server/command_matrix.py`, `server/handoff_store.py`, `bin/session-handoff`
- Test: `tests/test_command_matrix.py`, `tests/test_handoff_store.py`
- Docs: `README.md`, `skills/session-handoff/SKILL.md`

1. Add tests for absent-but-creatable, healthy, unreadable/unsafe, and corrupt central data/state/catalog cases; assert no store/binding creation and unchanged default JSON keys.
2. Run the doctor health tests RED because the current matrix has no store checks.
3. Add a read-only health probe using existing store roots/private checks and SQLite/native primitives; report machine-readable health fields while distinguishing absent, healthy, unsafe, and corrupt states. Add an opt-in human-readable rendering only if it does not alter default JSON behavior.
4. Run doctor and store health tests GREEN, including provider-free behavior.

### Task 6: Make uninstall preservation explicit

**Files:**
- Modify: `bin/session-handoff`
- Test: `tests/test_package.py` or `tests/test_setup.py`
- Docs: `README.md`, `skills/session-handoff/SKILL.md`

1. Add a test asserting successful uninstall output includes exact XDG data, XDG state, and checkpoint paths, including `SESSION_HANDOFF_HOME`/XDG overrides where applicable.
2. Run it RED against the current generic success message.
3. Print the resolved preserved locations; do not add deletion or purge behavior.
4. Run uninstall/setup tests GREEN.

### Task 7: Add bounded central CLI list/read visibility

**Files:**
- Modify: `bin/session-handoff`, `server/handoff_store.py`
- Test: `tests/test_package.py` or a new `tests/test_cli_central.py`
- Docs: `README.md`, `skills/session-handoff/SKILL.md`

1. Add help, list, read, pagination, scope, explicit-workspace, machine-readable, unregistered-workspace, and no-store-creation tests; reject search, mutation, arbitrary roots, and absolute import/export arguments.
2. Run focused CLI tests RED because no central list/read subcommands exist.
3. Add minimal argparse subcommands reusing `list_records`/`read_record`, with bounded `--limit`, opaque `--cursor`, `--scope`, required `--workspace`, `--ref` for read, and JSON output consistent with existing commands.
4. Run CLI tests GREEN and verify legacy default command behavior is unchanged.

### Task 8: Align UX documentation and run gates

**Files:**
- Modify only the docs touched above plus the new plan.

1. Check README, skill, and central-store plan for the exact ref-first create/resume, actionable scope/rebind, consent, doctor, uninstall, and CLI contracts.
2. Run `rtk pytest -q`, `python3 -m ruff check .`, `python3 -m compileall -q server tests`, `git diff --check`, `claude plugin validate --strict .`, and `npm pack --dry-run --json`.
3. Report exact RED/GREEN evidence, changed files, rejected deviations, and remaining risks. Leave all changes uncommitted for independent verification.
