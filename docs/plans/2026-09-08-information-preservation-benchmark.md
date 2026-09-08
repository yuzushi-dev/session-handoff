# Information Preservation Benchmark Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Compare useful information retained by main handoff, candidate handoff,
and real native Codex compaction, separately from storage efficiency.

**Architecture:** Reuse existing fixtures, isolated execution, public MCP
roundtrips and hidden acceptance tests. Add a version-checked app-server
adapter and structured conversation inputs; never replace native compaction
with a simulated summary or full-context baseline. Evaluate recovery of facts
on separate state copies from downstream task execution.

**Tech Stack:** Existing Python benchmark, stdlib, installed Codex app-server.

## Authorization and ownership

User approved implementation by `gpt-5.6-luna`, reasoning `max`, following the
Astra medium study. Luna owns benchmark code, its tests and benchmark README;
parent owns this plan and independent verification. Preserve the dirty feature
branch and prior source/results. No product changes, commit, push, install,
deployment, new session handoff or provider calls in this implementation stage.
Real provider preflight and the paid matrix require a separate execution gate.

## 1. Verify the native interface before implementation

- Inspect installed Codex 0.153.4 and matching local source under
  `/tmp/codex-src-rust-v0.153.4/codex-rs/`.
- Confirm concrete request/event schemas for structured history injection,
  thread copies, compaction and continuation. Current online documentation is
  not proof that the installed binary supports an entrypoint.
- A safe offline protocol/schema probe may run with networking disabled and
  isolated state. No production session or credentials may be modified.
- Report the interface evidence before implementation. If an indispensable
  endpoint is absent, stop the affected path and report the blocker; do not
  quietly substitute a one-message transcript or fabricated checkpoint.

## 2. Structured fixtures and fair input contract

Relevant existing files: `benchmark/native_seed.py`,
`benchmark/fixtures/context_rot_cases.json`, `benchmark/run_study.py`.

- First add failing tests for retained user/assistant/tool roles and event order,
  deterministic input identity, and exclusion of hidden gold annotations.
- Implement the smallest structured representation/conversion supported by the
  verified native interface. Preserve existing benchmark modes.
- Three arms see identical history up to the same interruption point, identical
  workspace snapshots and instructions. Record each actual handoff contract;
  if main and candidate have the same prompt, report that equivalence.
- A near-threshold band requires a verified context budget. Unknown limits must
  block that label rather than silently treating arbitrary long text as equivalent.

## 3. Real compaction adapter and offline tests

- Add failing transport tests for ack-without-completion, wrong-thread events,
  failed compaction, timeout, unavailable methods and premature continuation.
- Implement bounded stdio request/event handling using the verified schema.
  Wait for actual compaction completion and retain checkpoint provenance.
- Isolate owned processes/state; terminate descendants on failure. Do not log
  credentials. No fallback to full context or another model.
- Native compaction uses its normal configuration. Record automatic extra
  compactions or truncation; manual compact results do not establish automatic
  trigger behavior.

## 4. Three-arm runner and measurement

Reuse `benchmark/product_roundtrip.py`, `benchmark/version_aware.py` and
`benchmark/score.py` where their contracts match.

- Test dry-run cell counts and an explicit execution gate before provider work.
- Plan six cases × two bands × three replicates × three arms = 108 downstream
  continuations. Preparation, compaction and recovery probes are additional
  operations, not included in that count and not a provider request/spend cap.
- Main/candidate generate under their actual handoff contracts, persist/read via
  their own public MCP, then continue from the returned content.
- Native continues from the actual compacted state. Record pinned build/client,
  model, effort, history/prompt hashes, checkpoint identity, usage and failures.
- Balance arm order and downstream budgets. No hidden retries or removed failures.
- Probe recoverability on separate copies, without tools or gold access, so the
  probe cannot refresh the context used by the task continuation. For opaque
  native state report recoverability, not an invented internal retention ratio.
- Score active/critical facts, incorrect facts, obsolete decisions, repeated
  failed approaches, provenance, hidden acceptance and task completion separately.
  Include token/time costs without inventing a currency conversion.

## 5. Verification and handover

- Each code change follows failing regression test → minimal fix → passing test.
- Run targeted tests and the complete suite with networking disabled; parent
  independently reviews the diff, tests and a provider-free CLI dry-run.
- Document exact run/preflight commands and machine-readable readiness reasons.
- Distinguish `offline harness verified`, `real native compact verified`, and
  `comparative benchmark completed`. Mock protocol success proves only the first.
- Stop before the provider preflight. Report implementation gaps, if any, and
  the next gated action; do not declare semantic superiority without results.

## Completion review — 2026-09-08

The first Luna delivery was not accepted as complete. The parent independently
ran its 29 focused tests, but inspection found missing execution orchestration
and a schema-only check mislabeled as real compaction verification.
The user explicitly requested that Luna finish the implementation.

Acceptance requires checking the real CLI path, not only individual helpers:

- Authorized execution actually dispatches all three arms and writes verifiable
  results; unauthorised execution performs no provider work.
- Schema discovery never sets `realcompact-verified`.
- History preserves anchor placement and valid function-call/output pairing.
- Recoverability depends on answers to fact probes, not successful thread reads.
- Build provenance hashes source contents, not a build label.
- Provider-free end-to-end tests exercise orchestration through subprocess
  protocol peers, including failures and deadlines.

Status: completion work delegated to `/root/information_benchmark_implementation`.
No real compaction or new comparative model benchmark has been run.

Independent intermediate verification:

- Parent: 39 focused tests passed; complete network-disabled suite: 918 passed,
  2 skipped. These precede the remaining review fixes, not final acceptance.
- Pinned Codex 0.153.4 ELF: initialize, thread creation and structured-history
  injection passed inside the actual runner sandbox, with networking disabled.
  This is lifecycle evidence, not compaction or provider evidence.
- Astra verified native permission profiles offline: deny `/mnt/native` blocks
  reading original/probe rollout state; workspace remains writable for the task
  and read-only for probes. Profiles still require integration and regression tests.
- Remaining acceptance fixes: use actual per-build generation instructions,
  integrate state deny-read, retain tool evidence and final workspace differences,
  mount readonly auth at the configured Codex home, and honor dry-run cell filters.

Final offline verification of those fixes:

- Parent: 42 focused tests passed; network-disabled complete suite: 921 passed,
  2 skipped; Ruff passed. Actual inner-sandbox lifecycle passed again with the
  new named permission profiles. No provider calls were made.
- Build Create-mode instructions load from both frozen product trees; their
  instruction hashes differ while the canonical structure hash is identical.
  Candidate defaults to central storage; benchmark model effort defaults to
  Luna xhigh (Luna max was the implementer, not the selected benchmark effort).
- Tool evidence and final workspace differences are retained for manual scoring;
  probe exact matches remain explicitly diagnostic, not semantic verdicts.
- Selected dry-run case/band/replicate reports three continuations, not the
  full 108-cell matrix. Authentication mount targets the isolated Codex home.
- Remaining next phase: authorized provider preflight, then selected comparison
  and calibrated manual scoring. Native compaction and information-preservation
  superiority remain unverified; offline protocol peers cannot establish them.
