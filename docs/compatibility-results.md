# Compatibility verification — 2026-09-30

This is an implementation-candidate report, not a release certification.
The starting checkout was `ce0109a83cbd65c8705a3d79994cdb885f2b2340`,
package `0.7.4-jev.3`. The candidate contains uncommitted changes; the starting
SHA does not identify its effective contents. Exact package and evidence hashes
belong in the generated compatibility receipts.

## Starting verification

The full starting suite produced **1187 passed, 8 failed, 14 skipped**.
All eight failures used August telemetry rows with an implicit current date;
the rows had expired under the existing 30-day retention policy. Test-only
changes now supply a fixed date or freeze the retention clock. The telemetry
suite then produced **430 passed, 1 skipped**. Product retention was preserved.
The original failed run is retained separately from subsequent results.

The H1 compatibility and package tests produced **40 passed**. An early npm
tarball installed in an isolated HOME/XDG environment. Its packaged receipt
runner worked without checkout modules. Doctor did not create or alter state,
and postinstall produced no telemetry counters with tracking disabled. These
early checks do not certify the final artifact or native client workflows.

## Native observations

| Combination / case | Observed result | Limit |
| --- | --- | --- |
| Codex 0.159.2, Linux, native start and resume | Passed with `gpt-5.6-luna`, low reasoning | Same thread resumed; this alone does not certify plugin handoff, migration or compaction |
| Codex 0.159.2, `gpt-5.4-mini` | Failed: model unsupported by the subscription account | Retained as a failed attempt; no successful-model claim |
| Initial Claude subscription probes | Invalid laboratory invocation: `--bare` disables credential reads | Retained as harness failures; they do not establish an account authentication failure |
| Claude Code 2.1.285, normal Sonnet invocation | Passed; reply matched exactly with `claude-sonnet-5-5` | Subscription authentication worked; isolated native flows require the corrected invocation |
| Existing information-preservation harness with Codex 0.159.2 | Not run: adapter explicitly accepts only 0.153.4 | Its app-server guard was preserved; it cannot certify the requested CLI surface |
| macOS | Not run | No macOS host was available; no verified support claim |

Live tests use synthetic repositories, isolated HOME/XDG directories,
an environment allowlist, and small models on the authorized subscription
accounts. API keys are excluded. Authentication material and raw account
configuration are not published or included in npm packages.

The initial Claude probes used `--bare` while excluding authentication
environment variables. Installed CLI help explicitly says bare mode skips
keychain reads and requires `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`.
A normal `claude -p --model sonnet --no-session-persistence` control succeeded
with the existing Pro subscription. The earlier conclusion that a fresh login
was required was incorrect. Subsequent laboratory runs omit `--bare` and
preserve isolation through HOME/XDG and explicit settings instead.

## Integration gates

The independent review checks durable switch recovery, migration output
ownership, SQLite WAL source changes, checkpoint reinjection deduplication,
and build-bound certification. A missing
native proof remains `not-run`; deterministic or simulated passes do not
certify the corresponding client capability.

## Final-C artifact and offline gate

- Package version: `0.7.4-jev.3`.
- Effective content SHA-256: `05268602f4f5228b684d726470025562ce9187cb873176e6310dcaf79d518d9f`.
- npm tarball SHA-256: `9cd6cefc7574c0fd4e43bdaa5d28752f541355f3002aea2529817aa0ec2715b0`.
- Host: Linux `6.14.0-37-generic`, `x86_64`.
- Full suite: **1273 passed, 14 skipped**.
- Final independent code review: no remaining HIGH/MED findings.
- npm installation and standalone runner passed. Checkout, extracted package
  and npm-installed package have the same effective content hash.
- Packaged doctor with real clients on PATH preserved caller HOME/XDG and
  the daily launcher hashes. Probes use disposable configuration snapshots
  and resolve managed wrappers to their native executable.
- Offline receipt: 8 deterministic fixture checks passed, 7 native matrix
  cases not run by that provider-free invocation, 0 failures.
- No telemetry counters, private laboratory trees or personal home paths
  were included in the package verification.

Superseded candidate A and B packages and failed attempts remain separate.
They are not silently relabeled as final-C evidence. During integration,
Sando-wrapped command output accidentally replaced migration source files;
the invalid collection run is retained. The files were recovered from raw
baseline sources, semantic changes reapplied, and the final suite rerun.

## Final-C native evidence available so far

Codex `0.159.2` with `gpt-5.6-luna`/low created and read a central handoff
through the exact final-C plugin. Native resume recovered the same marker and
reference. MCP initialize reported `0.7.4-jev.3` and the installed package
matched the final-C content hash.

The final-C migration engine converted a real synthetic Claude `2.1.285`
session without modifying its source. Codex resumed the generated target and
recovered the expected marker. Omitted native metadata and reasoning remain
explicit in the migration manifest.

The reverse continuation attempted earlier on candidate A was blocked by
the Anthropic subscription session limit, despite native target-ID acceptance.
It does not count as a completed migration proof. The user subsequently
authorized the existing `cc-fleet` Codex provider profile for Claude; that
backend is a separate test cohort and is not evidence that the Anthropic quota
or native Anthropic continuation succeeded.

The final-C reverse migration subsequently passed in Claude Code `2.1.285`
with the existing Codex provider profile and `gpt-5.6-luna`: 14 records were
converted, five reasoning records explicitly omitted, the source remained
unchanged, and continuation recovered `FINAL-C-HANDOFF-314`. This proves
Claude client-format acceptance with a Codex backend.

Profile-backed Claude handoff failed in two bounded attempts. The model
supplied both a valid `name` and `path: ""`, even when explicitly instructed
to omit `path`; the server correctly rejected the mutually exclusive fields.
Local bridge source forwards the tool schema and arguments unchanged. No
global bridge configuration or server validation was changed to hide the
failure. Text-only requests through that profile succeeded.

### Anthropic retry after the requested 16-minute wait

After 11:01:08 UTC on 2026-09-30, Claude Code `2.1.285` with first-party
`claude-sonnet-5-5` passed final-C canonical handoff create/read and native
same-session resume, recovering `FINAL-C-ANTHROPIC-RETRY-901` and the same
reference. Final-C Codex-to-Claude migration also passed native Anthropic
continuation, recovering `FINAL-C-HANDOFF-314` with the source unchanged.
One preceding handoff payload lacked required canonical sections and was
rejected; it remains an invalid invocation, separate from the valid pass.
The dated retry receipt preserves the earlier quota-blocked receipt and the
separate Codex-profile failure. The subsequent compaction gates are below.

### Subsequent native compaction and composition gates

Claude `2.1.285` with native `claude-sonnet-5-5` passed both manual
`/compact` and automatic `--autocompact 100k` tests. Native transcript
boundaries agree with `PreCompact` and `SessionStart(source=compact)`;
the corresponding references were injected once. The automatic stream
process first exited before completion; resuming the same native session
completed the boundary and reinjection. See the
[Claude event table](compatibility-claude-compaction.md).

Codex `0.159.2` with Luna/low passed automatic compaction using laboratory
overrides `model_context_window=16000` and
`model_auto_compact_token_limit=10000`. A native compact record and a
nonempty compact-session injection were observed; the model reported the
checkpoint path. Manual compaction also passed in the same TUI process:
one manual checkpoint, one native compact record, and one nonempty compact
injection. Earlier terminal drivers had left `/compact` unsubmitted in the
composer; the valid driver separates typing from Enter and waits for observed
completion before submitting the next prompt. See the
[Codex event table](compatibility-codex-compaction.md).

A real Codex session loaded final-C session-handoff and Sando `0.7.0`
together. Long output produced a Sando artifact; handoff create/read
preserved its exact handle and recovery command, and artifact recovery
returned actual content. An unavailable handle returned an explicit error.
This extends the earlier component probe to both installed plugins in one
native client session. It does not establish persistent shared storage or
cross-machine artifact availability.

### Consolidated current native cohort

The final receipt is `.orca/compat-final-native/compatibility-report.json`:
**16 passed, 0 failed, 0 not-run** (eight deterministic fixture checks,
seven required native cases, and the combined-plugin case). It covers this
final-C build on Linux with Codex `0.159.2`/Luna low and Claude
`2.1.285`/native Anthropic Sonnet. It does not include the separate failed
Claude/Codex-provider cohort or claim a full historical-version or macOS matrix.
All earlier receipts and invalid harness attempts remain preserved.

The packaged doctor consumed this receipt and reported `verified` for both
clients and all four required capabilities. Its disposable empty `--home`
target was not configured as an installed user environment, so local readiness remained
false; the probe exit code was 1 for that separate readiness condition.
The first checkout probe rejected the installed-package receipt because its
source provenance differs. The validated probe ran the exact packaged doctor
with native client paths. No daily launcher was executed or modified.
The process HOME/XDG were inherited for this final probe; client subprocesses
used the doctor's disposable configuration snapshots. Central-store paths
in its diagnostic output therefore refer to the host configuration.

Observed one-injection/one-claim evidence does not constitute a deliberate
duplicate-hook delivery experiment. At-most-once behavior has deterministic
coverage; the claim-before-output crash window remains a stated limitation.

### Version-bumped release validation

Release `0.7.4-jev.4` updates npm and both plugin manifests, the portable
manifest, release documentation and version-independent test expectations.
All 23 runtime files (CLI, server modules and hooks) are byte-identical to
final-C. The full suite was rerun: **1273 passed, 14 skipped**; Ruff and
diff checks passed.

- Release content SHA-256: `421495132a2f6545d427b78580357e8df19cd48951680eae6dfb359c60897600`.
- Release tarball SHA-256: `6484a2e5689c02b3159754b3fe846045242da37d1cdbd48796018ede2d654709`.
- npm pack/install passed; checkout, extracted and installed content match.
- The new package's offline receipt records 8 passed, 7 native not-run,
  0 failed. Native final-C receipts retain their original `.jev.3` identity.

Release validation artifacts are local under `.orca/release-0.7.4-jev.4/`.
The version bump does not publish the package to the npm registry.

The [recovery and Sando pilot](compatibility-pilot.md) records the bounded
6/6 recovery result, its two-compaction limitation, and the final-C
provider-free Sando/MCP component probe. Native manual/automatic checkpoint
delivery with both clients and a combined two-plugin Codex session remain
separate certification gates.
