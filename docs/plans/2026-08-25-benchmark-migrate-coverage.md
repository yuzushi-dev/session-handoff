# Benchmark Migration Coverage Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Execute and verify the benchmark's `migrate` condition against a native-format Codex paginated fixture and a real temporary Claude target.

**Architecture:** Bring the already-merged migration backend into this benchmark branch, then add one deterministic native Codex fixture builder and an integration test that invokes the internal migration writer. Keep the long context-rot cases as semantic benchmark inputs; do not use live sessions or credentials.

**Tech Stack:** Python 3.12, stdlib `sqlite3`/`json`, pytest, bundled migration engine.

---

### Task 1: Align the benchmark branch with the current migration backend

**Files:**
- Cherry-pick: `dc2be4f` from local `main`

**Step 1: Apply the existing migration commit**

Run: `git cherry-pick dc2be4f`

Expected: the paginated backend and its baseline integration test are present on this branch.

### Task 2: Add a native-format benchmark fixture

**Files:**
- Create: `benchmark/native_fixture.py`
- Test: `tests/test_benchmark_migrate.py`

**Step 1: Write the failing integration test**

Create a temporary Codex home containing a paginated rollout and `thread_history_1.sqlite`, then call `migrate_session` with the internal engine and a temporary Claude home. Assert the target UUID, native Claude JSONL, manifest, source hash, warnings, and `context_loss`.

Run: `python3 -m pytest -q tests/test_benchmark_migrate.py`

Expected: collection/import failure until the benchmark fixture module and test exist.

**Step 2: Implement the fixture builder**

Write the smallest valid native Codex layout with user message, assistant message, command execution, reasoning, and web search items. Do not include credentials or repository data.

**Step 3: Run the test**

Run: `python3 -m pytest -q tests/test_benchmark_migrate.py`

Expected: PASS using the installed native writer.

### Task 3: Verify the benchmark study includes executable migration runs

**Files:**
- Modify: `tests/test_benchmark.py`
- Modify: `benchmark/prepare_study.py` only if the test exposes a gap

**Step 1: Add an assertion that prepared study runs include all four conditions and valid migration metadata.**

Run: `python3 -m pytest -q tests/test_benchmark.py tests/test_benchmark_migrate.py`

Expected: PASS with `migrate` represented for every case/band/replicate.

### Task 4: Full verification

Run:

```bash
python3 -m pytest -q
python3 benchmark/prepare_study.py benchmark/fixtures/context_rot_cases.json --output /tmp/session-handoff-benchmark --runs-per-condition 1
python3 benchmark/score.py benchmark/evaluation.example.json --pretty
git diff --check
```

Expected: all tests pass; study preparation reports 72 runs; example scoring succeeds; no repository output is generated.
