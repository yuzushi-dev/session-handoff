# Context Fidelity Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prove critical semantic continuity and verify same-harness handoff plus bidirectional Claude/Codex migration without silent context loss.

**Architecture:** Keep semantic handoff and native migration as separate contracts. Harden the deterministic benchmark first, add runnable fixture workspaces, then verify real native writers in both directions and expose an opt-in live-study runner.

**Tech Stack:** Python 3.10+ standard library, pytest, Claude Code, Codex CLI, internal migration engine.

---

### Task 1: Strict benchmark manifest and release gates

**Files:**
- Modify: `benchmark/prepare_study.py`
- Modify: `benchmark/score.py`
- Modify: `benchmark/fixtures/context_rot_cases.json`
- Test: `tests/test_benchmark.py`

1. Add failing tests for duplicate/missing runs, invalid booleans and counters, duplicate IDs, and incomplete critical facts.
2. Run `python3 -m pytest -q tests/test_benchmark.py` and confirm the new tests fail for missing validation.
3. Add a versioned study manifest, explicit `critical` fact flags, exact Cartesian coverage validation, and a release gate requiring critical RCR `1.0`, IFR `0`, and SCI `0` for handoff runs.
4. Re-run the focused tests and commit.

### Task 2: Runnable continuation fixtures

**Files:**
- Create: `benchmark/fixture_workspace.py`
- Modify: `benchmark/prepare_study.py`
- Test: `tests/test_benchmark_workspace.py`

1. Add failing tests that materialize each case into a temporary workspace and assert its focused test initially fails.
2. Implement the smallest deterministic generator for the six Python fixture workspaces and record each verification command in the study manifest.
3. Verify each initial failure is the intended pending task, not fixture breakage.
4. Run focused tests and commit.

### Task 3: Bidirectional native migration evidence

**Files:**
- Modify: `server/migration.py`
- Modify: `benchmark/native_fixture.py`
- Modify: `tests/test_benchmark_migrate.py`
- Modify: `tests/test_paginated_migration.py`

1. Add a failing real-writer round-trip test: paginated Codex → temporary Claude → temporary Codex.
2. Hash both the rollout and `thread_history_1.sqlite`; assert both remain unchanged.
3. Add an optional explicit `source_home` seam used by isolated tests while preserving default live behavior.
4. Reject inconsistent dry-run/apply loss reports and verify target IDs and semantic user/assistant/tool content.
5. Run focused migration tests and commit.

### Task 4: Command matrix contract

**Files:**
- Create: `benchmark/command_matrix.py`
- Create: `tests/test_command_matrix.py`
- Modify: `README.md`

1. Add failing tests for the four documented commands: Claude/Codex handoff and Claude→Codex/Codex→Claude migrate.
2. Validate installed skill invocation names, MCP tool exposure, managed-launcher requirement, exact active-session-ID requirement, and target resume arguments without calling a provider.
3. Add a provider-free command that emits a content-free JSON readiness matrix.
4. Run the matrix against temporary setup and installed client versions; commit.

### Task 5: Opt-in semantic study runner

**Files:**
- Create: `benchmark/run_study.py`
- Create: `tests/test_run_study.py`
- Modify: `benchmark/JUDGE.md`
- Modify: `benchmark/README.md`

1. Add failing tests using fake Claude/Codex executables; no provider calls.
2. Implement explicit client/model/condition selection, isolated workspaces, content-free logs, structured artifacts, retry-free failure reporting, and resumable run-state.
3. Require synthetic inputs by default; require a separate flag for any non-fixture source.
4. Add blinded reference-based judge payloads with evidence fields and calibration metadata.
5. Run a fake-executable pilot and commit.

### Task 6: Full audit

**Files:**
- Modify: `benchmark/README.md`
- Modify: `README.md`
- Update once: `/home/cristina/Obsidian/AgentMemory/10-projects/yuzushi-plugins.md`

1. Run `python3 -m pytest -q` with the real historical-session test disabled.
2. Run both real-writer migration directions in temporary homes.
3. Run `claude plugin validate --strict .`, `python3 -m compileall -q benchmark server tests`, and `git diff --check`.
4. Run the content-free command matrix against installed Claude and Codex.
5. Update documentation and the vault note once. Do not push, publish, deploy, or run paid provider evaluations without explicit authorization.
