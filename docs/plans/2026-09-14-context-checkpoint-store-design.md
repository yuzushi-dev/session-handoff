# Context checkpoint store — design

## Origin

`server/context_store.py` existed on the stale branch `feature/context-management-handoff`
(726 lines), alongside a decision-graph/superseding layer bolted onto the same file and
onto `server/handoff_state.py`. That branch predates the 0.7.3 central handoff store and
never landed. This document scopes a fresh port of the checkpoint/read/search core only —
no decision graph — into current `main`.

## Scope

**In**: `configure`, `checkpoint`, `read`, `search` — an opt-in, per-workspace, append-only
history of checkpoints, searchable by free text.

**Out**: `decision_records`, the decision graph (`_decision_graph`, `_validate_decision_graph`,
`_effective_replacements`, `_bounded_relation`), and any schema change to
`server/handoff_state.py`. Rationale: the graph is not a bolt-on — it's wired into both
`checkpoint()` (validated on every write) and `search()` (annotates every match), and it
introduces a new schema concept (`decision_id`/`status`/`supersedes`) that doesn't exist
anywhere else in the codebase. Estimated at +250-300 lines across two files versus the
core alone, for a capability with no proven demand yet. Can be revisited later as a
separate, explicitly-scoped addition.

This is distinct from the central handoff store (`server/handoff_store.py`, landed in
0.7.3): that is a one-shot archive for switching/importing/exporting handoffs between
clients. This is a local, streamable history of incremental checkpoints for one workspace.
No code or concept overlap; no changes needed to the central store.

## Data model and on-disk layout

Per workspace, under the workspace tree itself:

```
.session-handoff/context/
  settings.json                  # {"schema_version":1,"enabled":bool,"updated_at":...}
  <stream>/
    r00000001.json
    r00000002.json
    ...
```

- `stream` partitions history (default `"default"`), validated by
  `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.
- Checkpoint fields (schema_version 1, no `decision_records`): `schema_version`, `revision`,
  `stream`, `created_at`, `client` (`claude`/`codex`/`unknown`), `session_id`, `summary`,
  `evidence` (string array), `next_steps` (string array).
- Checkpoints are append-only: never overwritten, never deleted. `checkpoint()` takes
  `expected_revision` for optimistic concurrency (fails if the current revision has moved),
  under an `fcntl` lock on the stream directory.
- Recursive redaction (`_redact_tree`) on `summary`/`evidence`/`next_steps` before every
  write.
- Limits carried over unchanged from the source branch: 128 KB per checkpoint, 4 MB
  scanned per search, 64 KB search output, explicit caps on array/query sizes.
- Removing the decision graph also removes the need for `checkpoint()` to load full stream
  history before writing (it previously did so only to validate the graph) — `checkpoint()`
  is now a pure append, no full-history read on the write path.

## Surface: MCP tools + CLI

Two independent thin callers into `server/context_store.py` — no logic duplication, no
wrapping of one by the other.

**MCP tools** (`server/handoff_mcp.py`, dispatched via the existing `_call_tool`):
- `context_configure(workspace, enabled)`
- `context_checkpoint(workspace, summary, evidence, next_steps, expected_revision, stream?, client?, session_id?)`
- `context_read(workspace, stream?, max_bytes?)`
- `context_search(workspace, query, stream?, limit?, offset?)`

Named to avoid collision with the existing `search`/`_search` tool (markdown handoff
document search — a different index, kept separate rather than overloaded).

**CLI** (`bin/session-handoff`, new `_context(argv) -> int` following the existing
`_telemetry`/`_central_list` dispatch pattern — `sys.argv[1:2] == ["context"]` with
internal subparsers):

```
session-handoff context status     [--workspace PATH] [--stream NAME]
session-handoff context enable     [--workspace PATH]
session-handoff context disable    [--workspace PATH]
session-handoff context checkpoint --summary S [--evidence E]* [--next-step N]* [--stream NAME]
session-handoff context read       [--workspace PATH] [--stream NAME]
session-handoff context search     QUERY [--workspace PATH] [--stream NAME] [--limit N] [--offset N]
```

`--workspace` defaults to the cwd. Both surfaces call `server/context_store.py` directly;
neither shells out to the other.

**Agent guidance**: `skills/session-handoff/SKILL.md` documents all four MCP tools *and*
instructs the agent to prefer the CLI subcommands when a shell is available, falling back
to the MCP tools otherwise (some clients don't expose a shell to the agent).

## Errors and validation

- `ContextError(ValueError)` — the module's one exception, matching the existing
  per-module pattern (`HandoffStateError`, `SetupError`).
- Input validation carried over unchanged: non-empty bounded `summary`; bounded string
  arrays for `evidence`/`next_steps`; non-negative `expected_revision`; `stream` against
  the existing regex; explicit ranges for `max_bytes`/`limit`/`offset`.
- MCP surface: `ContextError` caught in `_call_tool`, mapped to `_error(message)` — same
  pattern as every other tool in `handoff_mcp.py`, never a raw traceback.
- CLI surface: `ContextError` caught in `_context(argv)`, printed to stderr, `return 1` —
  same pattern as `_setup`/`_central_list`.
- **Corrupted checkpoint files**: `read()` stays fail-closed (touches exactly one file —
  the requested revision must be valid). `search()` is fail-open: a checkpoint that fails
  `_validate_loaded_checkpoint` is skipped, not fatal, and counted in the response as
  `corrupted_count` so the anomaly stays visible instead of silently vanishing.

## Testing plan

Carried over from the source branch (6 of its 17 tests — the 11 decision-graph-specific
ones dropped), adapted to the schema without `decision_records`:

- `test_configure_toggles_and_is_idempotent`
- `test_checkpoint_and_read_roundtrip`
- `test_checkpoint_rejects_stale_expected_revision`
- `test_search_is_stream_scoped_and_paginated`
- `test_search_skips_a_corrupted_checkpoint_and_reports_it` (new — covers the skip-and-count
  decision above; had no equivalent in the source branch, which was fail-closed throughout)
- `test_read_rejects_too_small_output_limit` / `test_invalid_stream_is_rejected`

New, for the surfaces this port adds (the source branch only had the Python API, no MCP
tools or CLI):

- `tests/test_handoff_mcp.py`: one test per tool (`context_configure`/`checkpoint`/`read`/
  `search`) via `_call_tool`, success and `ContextError` → `_error(message)` propagation.
- CLI subcommand tests (new file or added to an existing CLI test module): one per
  subcommand, argparse parsing and non-zero exit on `ContextError`.
- `tests/test_setup.py`: assert `.session-handoff/context/` is excluded from
  `_stage_bundle`/`restore_setup` — it's workspace data, not plugin bundle content, same
  treatment already given to `handoffs/` in `.gitignore`.

## Out of scope / explicit non-goals

- Decision graph / `decision_records` (see Scope above) — revisit separately if real
  demand shows up.
- `server/state_v1_canary.py` and `server/codex_rollout.py` from the same stale branch —
  unrelated features, evaluated and planned separately.
