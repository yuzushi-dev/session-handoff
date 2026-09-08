# Claude information-preservation pilot implementation plan

**Goal:** One authorized paired comparison of current session-handoff against
Claude Code native compact, using gpt-5.6-luna with verified xhigh effort.

**Architecture:** Reuse the frozen candidate build, compound-rot/long structured
fixture, external gold and visible/hidden checks from the Codex pilot. Add a
separate Claude native adapter; keep the validated Codex runner unchanged.
Claude runs in isolated temporary state. Native tools cannot read that state,
the original fixture source, gold or sibling-arm artifacts. A dedicated invocation
uses the existing proxy backend without changing the user's normal profile.

**Tech stack:** Python stdlib, Bubblewrap, installed Claude Code 2.1.263,
claude-code-proxy 0.1.22. Reuse existing product create/read helpers.

## Authorization and measurement boundary

- User approved offline preparation followed by one two-arm comparison if gates
  pass. Exactly two downstream task attempts, no retries or extra cells.
- Additional expected logical operations: one handoff generation, one real native
  compact, two independent probes per arm. Underlying provider requests in a tool
  loop are additional; monetary cost is not yet exposed or quantified.
- The previous 30-attempt Codex authorization is exhausted and separate.
- No global profile/service changes, credential copying, installs, commits,
  push, publication or new session handoff.
- No heuristic compact substitute or silently downgraded effort. If a verified
  gate fails, preserve evidence and stop before paid execution.

## Tasks and acceptance gates

1. Verify translated effort without provider calls. Identify proxy mapping of
   Claude effort to OpenAI request; require xhigh on the actual translated path.
   An accepted CLI option alone is insufficient. Read no credential contents.
2. Add test-first native adapter in benchmark/claude_native.py and
   tests/test_claude_native.py. Offline real-binary/fake-provider tests cover
   role-preserving seed/resume, actual compact boundary, fork isolation,
   postcompact context and native tool filesystem boundaries. Fake responses
   test the harness, never information preservation.
3. Add minimal two-arm orchestration reusing existing fixture/product/evaluator
   functions. Test cost gates, attempt accounting, failure preservation, no
   retries, no probe reinjection and task success = completed + both checks.
4. Run focused offline tests, relevant regression tests and independent review.
   Freeze hashes before any paid call. Existing dirty changes remain untouched.
5. If all gates pass, execute compound-rot/long once per arm. Same initial
   structured history/workspace, same model/effort/task instructions. Record
   genuine compact event, raw probes, tool evidence, diffs and check results.
6. Manually assess six gold facts in both answers per arm. Report one-pair
   evidence separately from the ten Codex comparisons; do not extend README
   claims until evidence exists. Update project memory once at task end.

## Current state

Planning and offline discovery in progress. Claude CLI supports --effort xhigh,
--fork-session and stream-json. Existing run_study.py supports Claude tasks but
rejects Claude reasoning_effort and does not implement this preservation test.
No provider calls made for this pilot.

Proxy gate verified dynamically against the installed 0.1.22 binary with a fake
loopback Codex endpoint and synthetic auth. tests/test_claude_proxy_effort.py
covers normal xhigh, default compact downgrade to low, and compact preserving
xhigh with CCP_COMPACT_EFFORT=off: 3 passed independently. The dedicated proxy
must receive CCP_CODEX_EFFORT=xhigh and CCP_COMPACT_EFFORT=off; setting those only
on the client does not affect an existing proxy. Force CCP_BIND_ADDRESS=127.0.0.1
and use CCP_CODEX_TRANSPORT=http, the dynamically verified transport. Mount
existing proxy auth read-only in an isolated CCP_CONFIG_DIR; never copy it or
allow refresh writes into the user's credentials. Bypass the normal Sando front
proxy to isolate Claude harness behavior. No normal profile/service changes.

Sources: exact proxy tag v0.1.22, commit
0b79fbc231a8c5a45f035c56e9f7ed89ab5d64c4;
https://github.com/raine/claude-code-proxy/blob/v0.1.22/src/providers/codex/translate/request.rs
and https://github.com/raine/claude-code-proxy/blob/v0.1.22/src/config.rs.
Claude native commands/sandbox references:
https://code.claude.com/docs/en/agent-sdk/slash-commands and
https://code.claude.com/docs/en/sandboxing.

Paired orchestration now exists in benchmark/claude_information.py (execute_pair);
tests/test_claude_information.py covers authorization, same-history copies,
fresh handoff, independent probes, realcompact requirement, strict native
results, attempt accounting, no retries, no-op rejection and output preservation.
Relevant prior helpers plus initial pair tests: 70 passed. Codex runner SHA256
remains 784e3f20002fc25b4942a3f56f6ecaf229a297c2c8cce5eff35de4b16e4e02ee.
Native adapter gate still in progress; do not interpret proxy/unit tests as a
completed preservation benchmark.

Latest parent verification: paired orchestration and proxy effort tests together
pass (19 tests); scoped Ruff and repository diff check pass. Native write-mode
isolation is blocked: `socat` is absent, and write mode must fail before any API
request. Do not disable the native Bash sandbox to bypass this prerequisite.
Provisioning a temporary benchmark-only dependency requires user approval; no
system install or normal Claude/proxy profile change is planned. Paid attempts
remain 0/2. Resume/fork/compact checks with tools disabled are independent of
this prerequisite and do not establish write-mode isolation.

A second native gate is blocked: the real CLI emits `compact_boundary`, but the
subsequent print/resume invocation does not receive the expected compact summary.
The current per-invocation adapter is therefore NOT accepted for the paid pair.
Preserve this regression and require persisted compact state before reporting
`actual_compaction`; a boundary event alone is insufficient. Investigate the
native session lifecycle before proceeding, without substituting a generated
summary for Claude's native compact. Missing `socat` is not the only blocker.

Parent native verification: 6 passed, 1 skipped (tool sandbox prerequisite),
1 strict xfailed (compact persistence); scoped Ruff and diff check pass. This is
NO-GO, not a passing native gate. Structured seed/resume and independent fork
work offline. Explicit `--model gpt-5.6-luna --effort xhigh` sends both values
correctly on the Anthropic request; the earlier observed effort `high` belonged
to the default Claude model, not Luna. Adapter rejects overlapping native-state
and workspace directories and mounts no-tool workspaces read-only.

## Approved follow-up: temporary dependency and native lifecycle fix

User approved provisioning `socat` only inside the temporary benchmark environment
and continuing the compact-persistence fix. Downloaded Ubuntu noble-updates
`socat=1.8.0.0-4ubuntu0.1` using `apt-get download` and extracted it without
installation at `/tmp/session-handoff-claude-deps-pYc8AnFs/extracted/usr/bin/socat`.
Package SHA256 matches apt metadata:
`46e854289b6b1c97e28be5d9293bea61e8633d00d6a65d9513f019c7232696fe`.
`ldd` resolves all dependencies and `socat -V` succeeds. Mount this executable
read-only for the native sandbox; do not change the system PATH/profile.
Both native gates still require verification before any paid call.

## Offline gates resolved; authorized pair started

- Shared fixture conversion now preserves ordered text and tool call/result
  identity in Anthropic blocks, merging adjacent same-role blocks only.
- Synthetic seed records need usage metadata for Claude's compact trigger.
  The deterministic estimate is serialized UTF-8 bytes divided by four, marked
  `estimated_serialized_utf8_bytes_div_4`; it is NOT measured token usage.
- One-shot `/compact` did not persist the generated summary. Sending `/compact`
  then local `/context` in the same stream flushes native state. Real CLI tests
  on the exact compound-rot/long fixture prove seed -> compact -> fork, persisted
  boundary and summary, dropped old history, and exactly summary+probe API calls.
  No pre-compaction model turn and no substitute summary are used.
- Positive Bash/Edit canaries pass. Native state cannot be read directly or via
  `/proc/1/root` and `/proc/self/root`; tool loopback networking is denied.
  Source state must be under masked HOME or /tmp; workspace/state overlap fails.
- Disposable proxy pins the verified executable hash, verifies loopback listener
  ownership, mounts auth read-only, and keeps runtime state in tmpfs. No profile
  or system installation changes. Independent bounded reviews are closed.
- Parent verification: 960 non-native tests passed, 2 skipped; 8 native tests
  passed with temporary socat; scoped Ruff and diff check passed.
- The authorized pair is recorded under
  `benchmark/results-version/2026-09-08-claude-information-pilot/` with frozen
  runtime/code hashes in `manifest.json` and persisted attempt accounting in
  `pair/results.json`. Do not infer completion from this start marker.

## Completed pair

The run finished with 2 attempted and 2 completed downstream tasks, both passing
visible and hidden checks. One generation, one genuine native compact, four
probes, zero task retries, no fatal failure. Both arms recovered six critical
facts in both probes (12/12 fact-answer observations per arm), assessed by the
parent assistant and a separate reviewer. Candidate continued in a new session;
native compact continued in its source session. Ten frozen hashes still match.
The handoff carried over a generation-only instruction; task execution still
passed. No product fix or extra paid attempt was made for that finding.
Full result: [Claude pilot report](../2026-09-08-claude-information-pilot.md).
