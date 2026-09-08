# Central Handoff Store Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make newly created semantic handoffs private, centrally stored and discoverable across projects, with explicit portable exports and permanent support for existing workspace files.

**Architecture:** A small Python storage module owns durable projects and immutable handoff records in XDG data, keeping machine-local path bindings and their lock in XDG state. Existing MCP tools retain their legacy path contract and gain explicit central references; the shipped skill selects central creation. The supervisor resolves the same references through the storage module and resumes through MCP instead of requiring client filesystem access outside the workspace.

**Tech Stack:** Python 3.10+ standard library, POSIX descriptor-relative filesystem operations and `fcntl.flock`, MCP JSON-RPC, pytest, Ruff; supported Linux/macOS.

---

## Execution and scope

User requested Astra planning, Luna medium implementation, then independent verification. Execute in that order without another design approval. The current feature branch already contains bounded literal search and redaction fixes; preserve them. Only this plan is committed by the planner. The implementation worker must inspect current `git status` and preserve unrelated edits. Use @test-driven-development and @verification-before-completion; apply Honey throughout.

The audit estimated 5–8 technical days including integration; this is an estimate, not evidence of completion. At each task boundary report concrete completion or a new material blocker. Do not call a missing real-client or macOS gate passed.

In scope: semantic Markdown handoffs, central creation/read/validation/list/search, UUID identity and explicit rebinding, copy-only import of legacy files, export, supervised fresh-session switching, docs and packaging. Native conversation migration and automatic pre-compaction checkpoints keep their existing paths and lifecycle: checkpoints are recovery evidence rather than semantic handoffs. Explain this distinction in user documentation.

Out of scope: mirror, synchronization, vector search, embeddings, FTS/BM25, deletion/retention automation, automatic scanning of unrelated repositories, native transcript centralization, remote sharing, and release/push/deploy. No new third-party dependency. No LLM call in storage or retrieval.

## Storage and identity contract

```text
${XDG_DATA_HOME:-~/.local/share}/session-handoff/
  projects/<project-uuid>/
    project.json
    handoffs/<handoff-uuid>/
      document.md
      manifest.json
${XDG_STATE_HOME:-~/.local/state}/session-handoff/
  bindings.json
  bindings.lock
```

Use absolute XDG values; ignore unset/empty/relative values and fall back independently to `Path.home() / '.local/share'` and `Path.home() / '.local/state'`. Do not relocate or overwrite the existing `plugin/` installation or `checkpoints/`. Root/project/record directories created here are mode 0700; bindings, lock, metadata and documents are 0600. Do not chmod pre-existing XDG ancestors. A backup of the data store contains no absolute workspace anchors; restoring it to a different machine requires explicit local association.

Bindings schema v1: `{"schema_version":1,"bindings":{"<canonical-anchor>":"<uuid>"}}`, stored only in XDG state. Project metadata v1: `{"schema_version":1,"project_id":"<uuid>","label":"<workspace basename>"}`. Labels are display data, never identity or path components. No remote URL, absolute workspace path, credentials, session transcript or telemetry payload in durable metadata.

Identity anchor is `git:<absolute resolved git-common-dir>` from `git -C <workspace> rev-parse --path-format=absolute --git-common-dir`, with a short subprocess timeout. Git absent/non-repository falls back to `directory:<resolved workspace>`; distinguish actual non-repository from timeout/permission/malformed Git output, which fail explicitly. A corrupt `.git` marker in the workspace or a trusted private ancestor also fails; do not inspect markers beyond an untrusted world-writable ancestor. Worktrees sharing the common directory share one UUID. Independent clones stay separate even when their remote, basename or content match. Paths locate bindings; UUID is the artifact identity. Moving an anchor requires explicit rebind to its existing UUID; do not infer by remote or scan the disk.

Read/list/search on an unregistered workspace return an empty central result and must not create bindings, lock or project directories. First successful central creation/import registers a project. Serialize registration, rebind and immutable-record publication under one permanent descriptor-opened `bindings.lock` in XDG state; never replace the lock inode. Fail corrupt/unknown bindings versions without replacing them. Write/fsync project metadata in data before publishing its state binding. An interruption can leave an unbound project directory but must never publish a binding to absent metadata. Rebinding changes only the anchor mapping, never moves/deletes/merges handoffs. Require explicit `replace=true` to replace an existing different mapping and return its previous UUID. Independent machines concurrently writing the same shared data directory are outside scope; restore/backup is explicit, not shared-drive synchronization.

Central reference grammar is `handoff://<canonical project UUID>/<canonical handoff UUID>`, with no extension, query, fragment, percent-encoding or extra path segments. Create handoff IDs with UUID4; names are separate bounded display metadata, never filesystem paths. Retain a conservative name rule (ASCII letters/digits, `_`, `-`, `.`, maximum 128 bytes; reject leading `.`, slash, backslash and NUL) for straightforward examples. Parse references strictly; never turn arbitrary external absolute paths into trusted references. A reference is an identifier, not a bearer authorization token.

Each record contains the canonical redacted UTF-8 document and a small manifest: `{"schema_version":1,"handoff_id":"<uuid>","name":"next.md","created_at":"<UTC RFC3339>","sha256":"<document SHA-256>","origin":{"project_id":"<uuid>","handoff_id":"<uuid>","kind":"create"}}`. A legacy import uses origin kind `legacy` and may include its normalized workspace-relative `source_path`; never absolute paths. Portable imports preserve this manifest and origin unchanged. The enclosing project is the local collection/routing scope; the handoff UUID and original provenance survive import into a different project's collection. Read results include local `project_id` separately from manifest origin.

Publish the pair atomically: write both files into a private staging directory under the target handoffs directory, fsync files and staging directory, then descriptor-relative rename the complete directory to its UUID under the permanent lock after checking that no target exists; fsync its parent. Readers ignore staging names. Reject a target directory even if empty, and validate existing records before treating them as idempotent. No metadata-first/document-later visibility. Cleanup touches only this operation's staging entries. Record bytes and manifest are immutable after publication: central `overwrite=true` is rejected. A new create, even with the same display name, generates a new UUID. Revision links/edit APIs are out of scope; legacy `overwrite=true` retains existing behavior.

Portable export is an atomic workspace-local bundle directory containing exactly `document.md` and `manifest.json`. Import accepts either a legacy Markdown file or this bundle. Validate manifest size (maximum 16 KiB), exact schema/types, canonical UUIDs, bounded name/time/origin fields, regular-file/no-symlink constraints and document hash before publication; UUIDs and provenance from a bundle remain untrusted data and never select or rebind a local project. Import targets the workspace's local project, preserving the handoff UUID and manifest. Existing same UUID plus identical document/manifest is idempotent; any difference is an explicit conflict, with no replacement or automatic new ID. For deterministic copy-only retries of a legacy file lacking identity, derive UUID5 using the local project UUID as namespace and `legacy:<normalized workspace-relative path>:<redacted-document SHA-256>` as its name. Changed legacy contents therefore create a new immutable record; use deterministic `created_at` from the source file mtime for the first import and return an already matching legacy record without recomputing its timestamp. No source file or exported manifest is rewritten.

Resolve against configured store only, validating project metadata UUID and handoff manifest UUID against their corresponding directory UUIDs. Project-scoped operations require the reference's project UUID to match the workspace binding. Global search/list may return other project references; reading one requires explicit `scope="all"`. Supervisor switches always require the current workspace binding; cross-project search does not authorize switching a different project. Explicit `handoff_project` association/rebind reconciles the selected project's authoritative records once; repairing an arbitrary manually deleted catalog row on every page would require an unbounded authoritative scan and defeat cursor/catalog bounds, so it is outside the recovery contract.

## MCP and workflow contract

Preserve existing legacy calls and result fields. Do not silently reinterpret `path` as central. Add:

| Tool | Central behavior |
| --- | --- |
| `handoff_create` | Existing `path` creates workspace-local as before, including legacy overwrite. New `name` creates a fresh immutable central UUID record. Require exactly one of `path`/`name`; retain content/state exclusivity and auto_switch, reject central `overwrite=true`. Central result contains `ref`, `project_id`, `handoff_id`, `name`, `storage="central"`, bytes/validation/redaction fields; do not put a URI in the legacy `path` field. |
| `handoff_read`, `handoff_validate` | Exactly one `path`/`ref`; legacy path logic unchanged. Reference reads default to project scope; `scope="all"` explicitly permits other registered projects. |
| `handoff_list`, `handoff_search` | Add `storage="workspace"|"central"`, default workspace for legacy compatibility. Central `scope="project"|"all"` defaults project. Reject `directory` for central mode. Central items include `ref` and `project_id`; workspace result shape unchanged. |
| `handoff_import` | `workspace`, workspace-local `path` to a legacy Markdown file or exported bundle; optional `name` only for legacy input, otherwise preserve manifest name. Validates/redacts legacy input and copies only. Bundle IDs/provenance are preserved under the local project; a UUID collision with different document/manifest is a conflict. No overwrite option. |
| `handoff_export` | `workspace`, project-scoped `ref`, workspace-local `directory`; publishes a portable document+manifest bundle atomically. Existing identical bundle is idempotent; any different existing destination errors. No overwrite option. |
| `handoff_project` | `workspace`, optional `project_id`, optional `replace=false`. Without UUID, read-only binding/status plus bounded project UUID/label pages using `limit` and opaque `cursor`/`next_cursor` for explicit association. A `scan_truncated=true` result is terminal (`has_more=false`) at the project-entry scan cap. With UUID, bind/rebind to existing validated project. Never create arbitrary supplied UUID. |

No per-tool store-root override. Enforce argument combinations at runtime as well as JSON Schema. Error responses must not expose token, secret-bearing content or unrelated paths. The public helper API may remain functions, not a plugin/backend framework.

Skill create mode calls `handoff_create(name=..., workspace=...)`; resume of central references calls `handoff_read(ref=..., workspace=...)`. If MCP is unavailable, report the central operation unavailable and provide an explicit legacy workspace fallback; do not silently create a second canonical copy. Import/export are user-directed copy operations. List/search central project by default in the skill; use all-project scope only for a user's cross-project request.

Maintain existing search limits across the whole operation, not per project: 256 scanned documents, 4 MiB scanned canonical document bytes, 64 KiB response, 512-byte query/snippet, 8 matches/file. Manifest and other metadata reads have their own fixed bounds (`MAX_MANIFEST_BYTES`) and are not included in the `scanned_bytes` document counter. Redact before matching. Order by project UUID then handoff UUID, then line; count inaccessible/invalid records and flag truncation. Scan limits apply before allocating unbounded file/project lists. Central list/project discovery also need bounded results and pagination (reuse existing list limit); do not advertise exact global totals when scanning truncates. New central results may use `total_count=null` plus `scan_truncated=true` for incomplete totals.

## Implementation tasks

Each numbered step is a separate small action. Where a step lists multiple cases, repeat the red/green cycle per case rather than writing the whole subsystem at once. Commit only explicitly touched files after each task, using `rtk git add <listed files>` then the stated commit command.

### Task 1: Descriptor-safe primitives for the shared store

**Files:** create `server/handoff_store.py`, `tests/test_handoff_store.py`; modify `server/handoff_mcp.py` only if sharing an existing primitive requires it.

1. Add `test_store_root_uses_absolute_xdg_and_home_fallback`, covering unset, empty, relative and absolute values for both XDG variables with isolated temporary homes; assert bindings/lock live only in state and durable records only in data.
2. Run `rtk pytest -q tests/test_handoff_store.py -k store_root`; expect missing store implementation failure.
3. Implement separate data/state root calculation and minimal store error type. Add strict reference parser and tests for two canonical UUIDs, display-name independence and every rejection above.
4. Add failing tests for directory/file modes, symlink ancestor/final-file rejection, FIFO/device rejection, oversize/invalid UTF-8 reads and unsupported secure primitives.
5. Implement descriptor-relative opening with `O_NOFOLLOW`, regular-file `fstat`, bounded reads and private directory creation. Existing `_read_file` lacks regular-file checking and `_open_relative_parent` creates 0755 directories: do not directly assume they satisfy central guarantees. If extracting shared code, keep a single implementation and preserve legacy behavior/tests.
6. Implement atomic files for bindings/project metadata, plus complete record-directory staging and locked no-clobber publication as specified above. Test readers never see only one record file. Only local bindings and legacy files permit explicit replacement; central records never do. Failure cleanup must not follow a swapped symlink. Never use path-based `resolve` + `write_text` as a security boundary.
7. Run `rtk pytest -q tests/test_handoff_store.py tests/test_handoff_mcp.py`; expect pass. Commit `feat: add secure central handoff storage primitives`.

### Task 2: Stable project registration

**Files:** `server/handoff_store.py`, `tests/test_handoff_store.py`.

1. Add `test_read_only_lookup_does_not_create_store` and `test_worktrees_share_project_but_clones_do_not`, using temporary real Git repositories and `git worktree add`/clone.
2. Run `rtk pytest -q tests/test_handoff_store.py -k 'lookup or worktrees'`; expect absent registration behavior failure.
3. Implement anchor discovery, read-only lookup and locked registration with UUID4. Avoid creating anything before input validation succeeds.
4. Add one test each for no Git/non-Git directories, same basename, changed remote, Unicode/space paths, subprocess failure/timeout and unknown/corrupt JSON version. Require bounded bindings reads and validate all UUID mappings. Copy only data to a fresh isolated data root and assert no bindings/absolute anchors are present; explicit association recreates only local state.
5. Run `rtk pytest -q tests/test_handoff_store.py`; expect pass. Commit `feat: register central handoff projects by workspace anchor`.

### Task 3: Explicit association and rebind

**Files:** `server/handoff_store.py`, `server/handoff_mcp.py`, `tests/test_handoff_store.py`, `tests/test_handoff_mcp.py`.

1. Add failing `test_project_status_is_read_only`, `test_rebind_requires_explicit_replace`, and `test_rebind_preserves_both_project_archives`.
2. Run `rtk pytest -q tests/test_handoff_store.py tests/test_handoff_mcp.py -k 'project_status or rebind'`; expect fail.
3. Implement bounded project status/list and association to an existing UUID under the state bindings lock. Return previous binding when replaced. Reject nonexistent/corrupt targets and non-boolean `replace`.
4. Register MCP `handoff_project`, with runtime validation and discovery tests. Test moved directory recovery via rebind without automatic path guessing.
5. Run the two affected test modules; expect pass. Commit `feat: expose explicit handoff project association`.

### Task 4: Central creation and reference reading

**Files:** `server/handoff_mcp.py`, `server/handoff_store.py`, `tests/test_handoff_mcp.py`, `tests/test_handoff_store.py`.

1. Add `test_create_name_returns_central_ref_without_workspace_files`. Reuse existing valid Markdown fixture; call `_create` with `name="next.md"`, assert two-UUID `ref`, valid manifest/document hash and origin, redacted persisted content, no workspace `handoffs/` and no legacy `path` in central response.
2. Run `rtk pytest -q tests/test_handoff_mcp.py -k create_name`; expect required-path failure.
3. Extend create schema and handler with name/path exclusivity; validate content/state/name/flags fully before registration and write. Reuse existing redaction and canonical section checks; no second redaction implementation in the store.
4. Add failing central read/validate tests: round trip, missing ref, mismatched workspace, explicit all-project read, invalid argument combinations, central overwrite rejection, repeated display name producing distinct immutable refs, manifest/directory UUID mismatch, content hash mismatch, and unchanged legacy overwrite/absolute-inside-workspace behavior.
5. Implement strict reference resolution shared by read/validate; retain external absolute path rejection. Preserve existing numeric-only telemetry and never add UUID/name/content to telemetry.
6. Run `rtk pytest -q tests/test_handoff_mcp.py tests/test_handoff_store.py tests/test_telemetry_privacy.py`; expect pass. Commit `feat: create and read central handoffs by reference`.

### Task 5: Bounded central discovery and search

**Files:** `server/handoff_mcp.py`, `server/handoff_store.py`, `tests/test_handoff_mcp.py`.

1. Add failing tests for project scope, explicit global scope, deterministic pagination across UUIDs, unregistered workspace emptiness, and preservation of the original workspace result schema.
2. Run `rtk pytest -q tests/test_handoff_mcp.py -k 'central_list or central_search'`; expect fail.
3. Extend list/search source selection and central result metadata. Reuse literal matching and redaction; apply one global budget across project iterators. Reject unsupported storage/scope/directory combinations.
4. Add tests splitting the file/byte/response budgets across several projects, redacted-only matches, unreadable entries and very many empty project directories. Count/limit visited project entries too, so empty projects cannot bypass bounded scanning; report truncation honestly.
5. Run `rtk pytest -q tests/test_handoff_mcp.py`; expect pass. Commit `feat: search central handoffs with project scope and global limits`.

### Task 6: Copy-only import and export

**Files:** `server/handoff_mcp.py`, `tests/test_handoff_mcp.py`.

1. Add `test_import_legacy_is_copy_only_and_idempotent`, capturing source bytes before/after and asserting identical second import returns the same ref even if only source mtime changes; changed content creates a new UUID and preserves the previous record.
2. Run `rtk pytest -q tests/test_handoff_mcp.py -k import_legacy`; expect missing handler failure.
3. Add legacy import handler/schema using secure workspace read, current redaction/canonical validation, UUID5 retry identity and immutable record publication. Add bundle import with strict bounded manifest validation and document hash verification. Preserve source manifest/UUID/origin under the local project; an existing same UUID must match document and manifest exactly or fail. Source is never rewritten/deleted.
4. Repeat red/green for export bundle round trip into another isolated project/store, preservation of UUID/origin, same-UUID/different-content and same-UUID/different-metadata conflicts, identical rerun, conflicting destination, manifest-supplied traversal/UUIDs, symlinks, oversized/unknown-schema/hash-mismatched bundles and atomic visibility of both bundle files. Imported IDs never select a destination project or mutate bindings. If current redaction would alter a supposedly portable document, reject the bundle rather than silently changing bytes under its immutable ID; legacy import can produce a fresh redacted record. Export likewise fails actionable if redaction would change a stored record, preserving the immutable original.
5. Document bulk migration as explicit per-file imports from a selected workspace, with individual results and safe retries. Do not add another migration engine or recursively discover all home-directory projects.
6. Run `rtk pytest -q tests/test_handoff_mcp.py tests/test_handoff_store.py`; expect pass. Commit `feat: import and export handoffs without moving source files`.

### Task 7: Supervisor reference transport and resume

**Files:** `server/session_switch.py`, `server/handoff_mcp.py`, `tests/test_session_switch.py`, `tests/test_session_migrate_supervisor.py`, `tests/test_handoff_mcp.py`.

1. Add failing `test_switch_request_accepts_bound_central_ref` and `test_switch_rejects_other_project_ref`. Preserve legacy request payload `path`; central payload uses separate `ref`, exactly one required.
2. Run `rtk pytest -q tests/test_session_switch.py -k central_ref`; expect fail.
3. Update writer and authenticated reader to use the same store resolver for central references. Revalidate binding/existence at consume time. Preserve token validation, request size, telemetry filtering and existing migration payloads.
4. Update create auto-switch call and supervisor consumers to carry either path or ref. Central draft says to use `$session-handoff`/the installed adapter and `handoff_read` for that exact reference and workspace. Never inject the full handoff content in argv or require broad client directory access. Keep the draft unsent.
5. Add tests for rebind/file removal between write and consume, forged refs, changed target symlink, malformed request, legacy draft preservation and central create success with failed switch reported separately. Invalid request must not terminate the active client.
6. Run `rtk pytest -q tests/test_session_switch.py tests/test_session_migrate_supervisor.py tests/test_handoff_mcp.py`; expect pass. Commit `feat: resume supervised sessions from central handoff references`.

### Task 8: Concurrency, interrupted writes and privacy regression

**Files:** `tests/test_handoff_store.py`, `tests/test_handoff_mcp.py`; fixes only in the owning runtime modules when tests expose defects.

1. Add multiprocessing tests registering the same workspace concurrently: all successful results share a project UUID, state bindings remain valid. Concurrent same-name creates yield distinct handoff UUIDs; concurrent imports of one UUID yield one complete record and idempotent results, while differing payloads yield an explicit conflict.
2. Run focused concurrency tests; fix only demonstrated defects.
3. Inject failures between document/manifest staging writes, before record directory rename, before state binding publish, after publication and during fsync. Assert existing records/bindings remain readable, readers ignore unpublished staging, no partial record is exposed, sources stay intact and retry semantics are safe. A failure after publication may leave a complete artifact; report uncertainty instead of claiming nothing was written.
4. Add descriptor-race tests swapping an ancestor/final target between validation and open; outside sentinel files must remain unchanged/unread. Add restrictive ownership/permissions checks where applicable; do not silently accept another user's store.
5. Run `rtk pytest -q tests/test_handoff_store.py tests/test_handoff_mcp.py tests/test_telemetry_privacy.py`; expect pass. Commit `test: cover central store races recovery and privacy`.

### Task 9: Skill, command, setup and package contract

**Files:** `skills/session-handoff/SKILL.md`, `commands/handoff.md`, `README.md`, `tests/test_package.py`, `tests/test_setup.py`; `server/setup.py` or `package.json` only if packaging tests require changes.

1. Update create/resume/list/search and import/export/project association examples to the exact contracts above. Explain data/state separation and portable backups, immutable handoff IDs versus display names, manifest provenance/conflicts, UUID/worktree/clone semantics, explicit rebind after moves, legacy fallback and checkpoint separation.
2. Update command adapter to select central creation. Keep the semantic document format and native migration contract unchanged.
3. Add package/setup tests proving new module ships in tarball and installed bundle; setup refresh/uninstall must preserve data `projects/`, state `bindings.json`/lock, checkpoints and saved handoffs. Use temporary homes; never uninstall the user's actual installation.
4. Run `rtk pytest -q tests/test_package.py tests/test_setup.py`; expect pass. Existing `server/*.py` package inclusion should suffice; no gratuitous manifest/version change.
5. Commit `docs: document central handoff workflows and portability`.

### Task 10: Automated verification and real-client acceptance

**Files:** create `docs/2026-09-06-central-handoff-store-verification.md` with commands, results and explicit pending gates.

1. Run `rtk pytest -q`, `rtk ruff check .`, `rtk proxy python3 -m compileall -q bin server hooks`, and `rtk git diff --check`. Expect all tests/checks pass; record actual counts rather than the previous branch count.
2. Run `rtk proxy claude plugin validate --strict .` and `rtk npm pack --dry-run --json`; expect validation and package inclusion pass. These commands do not publish/install.
3. Perform isolated MCP stdio JSON-RPC initialize/list-tools/create/read/list/search/import/export round trips with temporary HOME, XDG_DATA_HOME and XDG_STATE_HOME, including execution from the staged installed bundle. Store creation must occur outside test workspace; no user bindings or credentials touched. Include bundle transfer into a fresh project, UUID/provenance preservation, conflict rejection and restored-data explicit rebind.
4. Real acceptance matrix: Linux Claude, Linux Codex, macOS Claude, macOS Codex. In each, verify installed MCP discovery, central create/read with normal client sandbox, supervised fresh session with exact unsent reference, resume through the tool, legacy resume, and local export/import. Record executable versions, OS, commands, timestamp, expected/observed behavior. Also test a worktree and a second clone binding once on each OS.
5. Real hosted-model calls may spend money and real setup changes affect user configuration: obtain the user's explicit authorization before those actions. Prepare a deterministic fixture/script and the exact minimal requested operation first. Complete all isolated offline gates meanwhile. If macOS/device/client/access/spend authorization is unavailable, mark those cells pending and report the precise release blocker; never substitute mocks for a passing real gate.
6. Review final diff against the starting branch, including security boundaries and backward compatibility. Parent verifies independently and sends specific corrections to Luna as needed. Commit verification evidence only after recording actual results: `docs: record central handoff store verification`.

## Completion criteria

Implementation is reviewable when tasks 1–9 and automated/stdio gates pass with no writes to user archives. Release-ready additionally requires the four real-client/OS acceptance cells or an explicit user decision about a documented unsupported target. No push, release, deletion, binding migration on the real home, or install performed by implication. Parent records the consolidated result and remaining gates once in the project vault note using its required MCP write workflow.
