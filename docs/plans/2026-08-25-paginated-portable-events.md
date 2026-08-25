# Codex Paginated Portable Events Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Preserve content-bearing Codex paginated events during Claude migration instead of reporting and discarding them.

**Architecture:** Translate each verified Codex `ThreadItem` into the existing legacy message or function-call representation consumed by `session-migrate`. Keep private reasoning and unknown item types out of the target, with exact loss counters. Serialize structured tool results as JSON text so `session-migrate` carries them without `opaque` blocks.

**Tech Stack:** Python 3.10+ standard library, SQLite read-only projection, `session-migrate` 0.7.1, pytest.

---

## Design

The installed Codex database contains content-bearing item types that `server/paginated_migration.py` currently drops: `fileChange`, `mcpToolCall`, and `collabAgentToolCall`. The local aggregate on 2026-08-25 contained 2,331 file changes, 867 MCP calls, and 912 collaboration calls. Counts alone do not preserve operational context.

The projector will whitelist verified item types. It will map file edits, MCP calls, collaboration calls, subagent activity, image paths/results, and compaction markers into paired `function_call` and `function_call_output` records. Arguments remain JSON objects. Results become deterministic JSON text because `session-migrate` treats arbitrary result objects as opaque. The projector will continue to omit private reasoning and unknown item types, recording each omission.

This avoids two rejected approaches. Keeping only counters leaves useful context behind. Copying arbitrary unknown JSON can expose unstable or private client state and gives the target model no stable interpretation.

Schema references:

- OpenAI Codex app-server `ThreadItem` contract: https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md#items
- OpenAI Codex thread-history materializer: https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/src/protocol/thread_history.rs

## Task 1: Prove the current loss

**Files:**

- Modify: `tests/test_paginated_migration.py`
- Modify: `benchmark/native_fixture.py`
- Modify: `tests/test_benchmark_migrate.py`

1. Add synthetic file-change, MCP, collaboration, subagent, image, and compaction items with unique sentinel values.
2. Assert the projected legacy records retain every sentinel and report only private reasoning as dropped.
3. Assert the real Claude writer and Claude-to-Codex round trip retain the same sentinels without `opaque` loss.
4. Run `python3 -m pytest -q tests/test_paginated_migration.py tests/test_benchmark_migrate.py` and confirm failures show the unsupported item types.

## Task 2: Add the portable projection

**Files:**

- Modify: `server/paginated_migration.py`

1. Add explicit mappings for the verified Codex item types.
2. Preserve command status and web-search structure as JSON text tool results.
3. Keep reasoning and unknown types fail-visible through `dropped_events`.
4. Re-run the focused tests until they pass.

## Task 3: Document and verify

**Files:**

- Modify: `README.md`
- Modify: `benchmark/README.md`
- Modify: `/home/cristina/Obsidian/AgentMemory/10-projects/yuzushi-plugins.md` once at the milestone end

1. Document the expanded portable event set and the remaining loss boundary.
2. Run the full provider-free suite with the historical-session smoke disabled.
3. Run plugin validation, compileall, diff-check, and the command doctor.
4. Commit locally. Do not push, publish, or run provider evaluations.
