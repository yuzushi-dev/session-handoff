# Information preservation pilot — final result

Completed 2026-09-08: ten three-arm comparisons, exactly 30 authorized downstream
attempts, no retries. **Candidate preserves the scored core information, but
this pilot shows no advantage over main or native compact.** All three reach
the same ceiling on this synthetic sample.

## Information recovery

Parent manually read all 60 tool-free probe answers against the external gold
facts in `benchmark/fixtures/context_rot_cases.json`. A fact counts as recovered
when its core meaning is recoverable in **both** independently forked answers;
paraphrases and accurate reported/not-reverified qualifications are accepted.
This scores actionable meaning, not verbatim retention or identical confidence.
The assessment was unblinded and not independently human-calibrated.

| Case | Band | Main | Candidate | Native compact |
|---|---|---:|---:|---:|
| buried-constraint | short | 3/3 | 3/3 | 3/3 |
| superseded-decision | short | 3/3 | 3/3 | 3/3 |
| failed-attempt-trap | short | 3/3 | 3/3 | 3/3 |
| partial-state | short | 4/4 | 4/4 | 4/4 |
| late-correction | short | 3/3 | 3/3 | 3/3 |
| compound-rot | short | 6/6 | 6/6 | 6/6 |
| buried-constraint | long | 3/3 | 3/3 | 3/3 |
| superseded-decision | long | 3/3 | 3/3 | 3/3 |
| failed-attempt-trap | long | 3/3 | 3/3 | 3/3 |
| compound-rot | long | 6/6 | 6/6 | 6/6 |
| Total core critical facts | | **37/37** | **37/37** | **37/37** |

Each arm therefore has 74/74 recovered fact-answer opportunities across its
20 answers. These are correlated observations, not 74 independent trials.
No scored core fact was judged missing or incorrect. This does **not** mean
100% of all conversation information was preserved.

Audit rationale by case, applicable to both answers in each measured arm/band:

- Buried constraint: exact synchronous public signature, helper/source target,
  and expired-token regression are recoverable.
- Superseded decision: 15 supersedes 60; config is already 15; stale test needs
  alignment. The first short-case probes remain usable despite later task failure.
- Failed attempt: regex approach is rejected as insufficient; NFC precedes the
  unchanged sanitizer; decomposed-accent regression is identified.
- Partial state: completed writer and validated v3 fixture remain distinct from
  pending reader compatibility and the failing v2 test.
- Late correction: plural v2 endpoint supersedes v1; implementation and test
  targets are recovered.
- Compound rot: synchronous API, cap 3 instead of 5, rejected broad Exception,
  already-updated cap, missing TimeoutError classification and both regression
  requirements are recovered together.

Native answers sometimes call source/test targets “suspected” or progress
“reported/not independently verified”; the actionable targets remain correct.
Some main/candidate answers carry generation-only procedural constraints into
their inventories. Occasional broad statements about no prior file changes or
verification should not be read as faithful provenance inventories. These are
unscored wording/context-quality limitations, not evidence of superior retention.
The saved exact-match flags often say false despite correct semantic answers;
they are diagnostic proxies, **not** the verdict in this table.

## Task execution and authorized ledger

| Artifact directory | Downstream attempted | Completed turns | Valid task passes | Harness-invalid tasks |
|---|---:|---:|---:|---:|
| gate | 0 | 0 | 0 | 0 |
| gate-fixed | 1 | 0 | 0 | 1 |
| gate-remaining | 2 | 2 | 0 | 2 |
| short-smoke | 3 | 3 | 3 | 0 |
| short-remaining | 12 | 12 | 12 | 0 |
| long | 12 | 12 | 12 | 0 |
| Total | **30** | **29** | **27** | **3** |

Each arm has nine valid successful tasks and one initial harness-invalid task.
Do not report 30/30 successful tasks or treat the invalid three as model failures.
Success requires completed continuation AND visible verification AND hidden
acceptance. All 27 valid tasks have commandExecution and fileChange evidence.

Parent inspected source diffs: NFC is added before the sanitizer; partial-state
changes only the reader; the corrected endpoint is used; TTL test changes to 15
without reverting config; TimeoutError is explicitly classified without broad
Exception. These are final-state checks, not exhaustive scoring of every
intermediate action or every possible regression.

The small refresh fixture accepts differing implementations: short/main keeps
access-valid OR refresh-valid, while short/candidate and native use refresh-valid
alone. All long refresh arms use refresh-valid alone. Passing these fixtures is
not proof of production-level behavioral equivalence or exhaustive correctness.

Actual additional operations: **21 handoff-generation outputs, 10 real native
compactions, 60 probe answers**. The extra generation belongs to the initial
preparation-only failure. Logical operations are not a cap on underlying
provider requests within autonomous tool loops. Monetary cost and token usage
are unknown, not zero; no efficiency or cost superiority is established.

Measured batch elapsed times, including preparation/probes/orchestration:
gate-remaining 364.897 s (task-invalid), short-smoke 327.403 s,
short-remaining 1303.246 s, long 1354.462 s. These are not isolated product
latencies and must not be used as a per-arm efficiency comparison.

## Protocol, provenance and limitations

Main/candidate generate from identical structured histories using their frozen
build-specific Create-mode instructions, roundtrip through the respective MCP
create/read, then resume in a new thread from the read handoff. Native performs
a real correlated app-server compaction and resumes that thread. Probe forks
are separate and answers are not reinjected into the task. Parent verified
30 unique cell IDs, ten complete three-arm groups, identical history hashes
within each group, and no tool-use violations across all 60 probes.

Six short and four long cases, one replicate each; bands target roughly
32,000/160,000 serialized JSON characters, **not tokens**. No verified
near-window-limit condition was exercised. Repetitive synthetic diagnostic
noise and tiny code fixtures can create a ceiling; real-session superiority,
statistical equivalence and native internal retention are not established.
The compaction backend uses its opaque native default. Generation, probes and
continuations are configured `gpt-5.6-luna` / `xhigh`.

Frozen identities:

- Main: `/tmp/session-handoff-offline-20260907-gCo0KR/main`;
  tree SHA256 `c8d8e7452662f922373dd181f37d7aa165e5719539dbf1e1ea71a71fe5079d71`.
- Candidate: `/tmp/session-handoff-efficiency-20260908-Hy4eEq/candidate`;
  tree SHA256 `c9d2c80b9b26b019cd7a1c4ea89fcdac626850f3075f801670cba9d7cf4c17b8`.
- Final runner: `benchmark/information_preservation.py`;
  SHA256 `784e3f20002fc25b4942a3f56f6ecaf229a297c2c8cce5eff35de4b16e4e02ee`.
- Pinned native ELF: `/home/cristina/.codex/packages/standalone/releases/0.153.4-x86_64-unknown-linux-musl/bin/codex`.
- Final runner offline suite: **930 passed, 2 skipped**, network disabled;
  Ruff clean. Runner hash rechecked after paid execution; diff check clean.

Earlier harness revisions are not silently pooled as equivalent task conditions:

1. `gate/`: ephemeral source could not fork (no rollout); one generation,
   zero downstream attempts. Persistent threads remain inside isolated temporary
   native state. Real offline regression verifies inherited history references.
2. `gate-fixed/`: main task aborted on foreign-thread notification rejection.
   Original rejected payload was not saved; its exact origin is not established.
   Fix correlates target thread/turn, ignores foreign notifications, retains
   content-free event metadata and supports explicit already-attempted exclusions.
3. `gate-remaining/`: candidate/native had no local tools because
   `environments=[]` disables execution. Both were no-ops. Visible checks failed,
   while a pre-existing hidden TTL invariant passed. Saved `task_success=true`
   is a **false positive**, overridden by this report; original artifacts remain
   untouched. Removing the override restores native local Bash; scoring now
   requires both checks. The subsequent 27 tasks use this final runner.

The results and manual assessment above are reported from this run. The report
and repository methodology text are publicly reviewable materials; raw runtime
evidence is local and unpublished and cannot be publicly verified from this
report.

Raw evidence is local and unpublished, gitignored under
`benchmark/results-version/2026-09-08-information-pilot/`; each directory has
`results.json`, per-cell JSON, probe answers, tool metadata and workspace diffs.
The table above is the manual semantic assessment; original machine-score
fields remain pending/proxy rather than being rewritten as independently scored.

No further paid run is authorized by this completed cap. No install, commit,
push, publish, deploy or new session handoff was performed. A useful next study
would stress harder, independently assessed real histories and near-limit
contexts; it is not part of this completed pilot and has not been launched.
