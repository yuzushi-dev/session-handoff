# Live matrix: Codex Luna and Claude Haiku

Date: 2026-08-25

Status: execution complete. The formal release gate remains closed until a
condition-blind judge completes every run and a human reviews the calibration
sample.

## Scope

| Client | Model | CLI | Execution scope |
|---|---|---|---|
| Codex | `gpt-5.6-luna` | 0.149.1 | 6 cases, 3 bands, 4 conditions, 2 replicates: 144 runs |
| Claude | `claude-haiku-4-5-20251001` via `haiku` | 2.1.233 | 6 cases, `long`, 4 conditions, 1 replicate: 24 target runs |

The Claude scope follows the cost cap chosen during execution. Five target
runs reuse valid results from the first matrix segment. The reduced scope does
not qualify as release-grade evidence for Claude.

## Automatic results

| Client | Valid target runs | Hidden acceptance | Valid provider calls | Invalid attempts |
|---|---:|---:|---:|---:|
| Codex | 144/144 | 144/144 | 180 | 1 quota failure, then a clean rerun |
| Claude | 23/24 | 23/23 | 28 | 3 harness or quota attempts |

Each valid run passed the visible verifier and the fixture-specific acceptance
command hidden from the model. Codex has 36 valid runs for each of `full`,
`handoff`, `migrate`, and `oracle`. Claude has 6 runs for `full`, `migrate`,
and `oracle`, plus 5 `handoff` runs. A five-hour quota rejected the sixth
handoff continuation after its generation call; the cost cap excluded a retry.

Codex valid runs used 40,388,945 input tokens and 207,113 output tokens. The
median input per Codex run was 400,636 for `full` and 165,996 for `handoff`, a
58.6% reduction. The handoff count includes generation and continuation.

The Claude target sample has a median input of 462,265 for `full` and
163,236 for `handoff`, a 64.7% reduction. Claude reported $6.451271 across 68
calls from its pilot, matrix segments, and invalidated attempts.

## Runtime findings

The live matrix exposed four harness defects:

- Claude handoff generation used `plan`, which could return planning prose
  instead of the canonical handoff. Generation now uses `dontAsk` with no
  tools.
- Snapshot collection followed symlinks and decoded binary files as UTF-8. It
  now records link targets and binary SHA-256 values without dereferencing.
- Resumed runs lacked the revision of the runner that completed collection.
  Resume provenance now records that revision and digest.
- Local setup copied ignored benchmark results into the persistent plugin
  bundle. Setup now copies only the runtime entries declared for distribution.

Commit `9ef0c19` contains these fixes. The full repository verification after
the commit passed 136 tests with 1 skip, strict Claude plugin validation,
Python compilation, and `git diff --check`.

## Installed command matrix

The provider-free doctor checked the installed clients and migration backend:

| Flow | Command | Ready |
|---|---|---|
| Claude handoff | `/session-handoff` | yes |
| Codex handoff | `$session-handoff` | yes |
| Claude to Codex | `/session-handoff migrate codex` | yes |
| Codex to Claude | `$session-handoff migrate claude` | yes |

The doctor made zero provider calls. The live Codex matrix exercised 36
Claude-to-Codex migrations. The Claude sample exercised six Codex-to-Claude
migrations; each passed hidden acceptance.

## Evidence boundary

The automatic result proves that each completed continuation produced the
authoritative repository state. The earlier one-case pilots also scored
critical recall at 1.0, incorrect facts at 0.0, and stale intrusion at 0.0 for
both clients.

The study has no condition-blind, human-calibrated judgment set. The release
gate requires judgments for all 144 Codex runs plus a stratified human review
of at least 18 samples with agreement of 0.8 or higher. No judging provider
calls ran in this matrix.

Raw model output, native homes, credentials, and session identifiers remain in
gitignored local result directories. A scan of 196 blinded judge bundles found
no host-home paths, UUIDs, API-key shapes, or bearer tokens. The repository and
vault contain none of those artifacts.
