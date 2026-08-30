# Provider Benchmark Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stabilize structured-handoff provider evidence before any rerun, while preserving Markdown as the baseline.

**Architecture:** Trace the invalid provider response through the existing runner and validator. Add only the regression test and the smallest fail-closed handling needed. Classify continuation command failures from existing artifacts without changing semantic scoring, then add reproducible benchmark gates for validity, task success, and paired efficiency.

**Tech Stack:** Python, pytest, existing benchmark runner/scorer, JSON artifacts.

---

### Task 1: Reproduce and localize the malformed JSON

**Files:**
- Inspect: `benchmark/run_study.py`, `benchmark/real_session.py`, relevant tests and failed result artifacts.

**Step 1:** Identify the exact response boundary and current parse/validation behavior.

**Step 2:** Reproduce the failure with a local fixture or the saved response, without a provider call.

**Step 3:** Record the root cause and the narrowest safe behavior.

### Task 2: Add a failing regression test

**Files:**
- Modify: the existing benchmark/runner test module covering handoff generation.

**Step 1:** Add a test for the exact malformed response and assert rejection or safe handling according to Task 1.

**Step 2:** Run that test and confirm the expected failure.

### Task 3: Implement the minimal fix

**Files:**
- Modify: only the parser/runner module identified in Task 1.

**Step 1:** Implement the smallest change that makes the regression test pass.

**Step 2:** Run the focused test and then the affected test module.

### Task 4: Make harness failures measurable without changing scoring

**Files:**
- Modify: existing trace/reporting code and its tests only if needed.

**Step 1:** Classify existing internal command failures by stable category.

**Step 2:** Add deterministic reporting for the categories, preserving raw traces and existing correctness gates.

**Step 3:** Verify that the classification does not alter task-success or semantic scores.

### Task 5: Verify and decide on rerun

**Files:**
- Modify: benchmark documentation/report only after verification.

**Step 1:** Run focused tests, the full suite, compileall, and diff checks.

**Step 2:** Recompute the existing benchmark report from unchanged artifacts.

**Step 3:** Rerun the provider matrix only if the validity and harness gates pass; otherwise document the blocker and keep Markdown as baseline.

**Step 4:** Update the project note and commit only scoped repository changes.

## Outcome (2026-08-30)

Tasks 1–4 were completed in commit `8114def`. The exact failed `state-v1` cell
was rerun as a two-call provider regression probe with Luna `high`: 2/2 calls,
valid JSON, successful verification/acceptance, and zero classified trace
failures. The 144-call full matrix was not mixed with the previous aggregate;
it remains an owner-gated rerun after a semantically more discriminating study
is defined.
