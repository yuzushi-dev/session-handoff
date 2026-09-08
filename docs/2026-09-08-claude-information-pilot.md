# Claude Code information-preservation pilot

On 8 September 2026, Luna xhigh recovered all six critical facts with both
session-handoff and Claude Code native compact. It completed the task in both
arms. Session-handoff resumed in a fresh session.

| Measure | Current session-handoff | Native compact |
|---|---:|---:|
| Critical facts recovered | 6/6 | 6/6 |
| Fact-answer observations, two probes | 12/12 | 12/12 |
| Task success | 1/1 | 1/1 |
| Visible and hidden checks | Pass | Pass |
| Continuation | Fresh session | Same session, compacted |

This is one paired comparison on `compound-rot/long`, separate from the
[Codex pilot](2026-09-08-information-preservation-pilot.md). It supports parity
on these six facts and this task. It does not measure token or cost savings.

## Setup

- Claude Code **2.1.263**, direct binary, with `gpt-5.6-luna --effort xhigh`.
- Dedicated claude-code-proxy **0.1.22**, `CCP_CODEX_EFFORT=xhigh`,
  `CCP_COMPACT_EFFORT=off`, HTTP transport. Offline request capture verified the
  model and effort mapping before the paid run.
- The same frozen candidate as the Codex pilot: tree SHA-256
  `c9d2c80b9b26b019cd7a1c4ea89fcdac626850f3075f801670cba9d7cf4c17b8`.
- The same 25-record synthetic history and starting workspace in both arms.
  The Anthropic conversion preserves text order and tool call/result pairs;
  it merges adjacent blocks with the same role.
- Session-handoff: generation from the build's create instructions, public MCP
  create/read, then a fresh session seeded with the returned handoff alone.
- Native compact: `/compact` followed by local `/context` in one process to
  flush native state. The adapter verified a persisted compact boundary and
  summary before resuming. No preparatory model turn or substitute summary.
- Two separate probe forks per arm before the task. The runner did not inject
  their answers into either task session.

The native seed includes a token estimate based on serialized UTF-8 bytes
divided by four, because Claude needs usage metadata to trigger compaction on
synthetic history. The adapter labels this estimate in the result. Neither
that estimate nor the compact metadata establishes measured token savings.

## Semantic assessment

The parent assistant and a separate reviewer assessed the four probe answers
against these facts. The assessment was not blinded; neither reviewer used
keyword counts as the score.

1. Keep `RetryPolicy.should_retry(error)` public and synchronous.
2. Retry cap **3** supersedes **5**.
3. Reject broad `Exception` handling because it retries programming errors,
   including `TypeError`.
4. The retained implementation note reports cap **3** in `src/retry/policy.py`.
5. Add `TimeoutError` to `TRANSIENT_ERRORS`.
6. Verify `test_timeout_is_retried` and `test_type_error_is_not_retried` in
   `tests/retry/test_policy.py`.

Both reviewers found all six facts in each answer. Native compact preserved
the reported status of the existing code rather than claiming a fresh file
inspection. Both task continuations added `TimeoutError` to the transient
exception tuple and passed the visible and hidden evaluators.

One handoff-quality issue remains: the generated handoff included
“This turn is generation-only” and its no-tools restriction. Both candidate
probes repeated it. The later task instruction still led to a successful edit
and verification. This instruction carryover did not change the six-fact score;
the pilot did not fix or retest it.

## Accounting and evidence

The run consumed **2/2 authorized downstream attempts**, completed both, and
used no task retries. It performed one handoff generation, one native compact,
and four probe operations. Tool loops can involve additional provider requests;
monetary cost remains unmeasured.

Before the run, 968 tests passed and 2 tests skipped. The eight native
tests passed with temporary `socat`, including successful Bash/Edit operations,
denied direct and `/proc`-alias reads of native state, and denied tool networking.
The proxy used read-only credentials and temporary in-memory state. The run
left normal profiles and services unchanged.

The results and assessment above are reported from this run. The preparation
and gates document is the publicly reviewable repository material; raw runtime
evidence is local and unpublished and cannot be publicly verified from this
report.

Raw runtime evidence, stored locally and unpublished:

- Frozen manifest: `benchmark/results-version/2026-09-08-claude-information-pilot/manifest.json`
- Results, probe answers, compact event and workspace diffs: `benchmark/results-version/2026-09-08-claude-information-pilot/pair/results.json`
- [Preparation and gates](plans/2026-09-08-claude-information-pilot.md)

Results SHA-256:
`182f2f6a91fa2bbb5bf0efab31e1cf20a45f7a0ece8f597166770abe90f3c5d1`.
All ten runtime/code hashes in the manifest still matched after the run.
The original results retain `semantic_scoring: pending_manual`; the assessment
above is separate from the runner's raw output.
