# Information preservation pilot — 30 continuations

Final status (2026-09-08): COMPLETED within exactly 30 downstream attempts.
Ten three-arm information comparisons assessed; 37/37 core critical facts
recoverable in both probes for each arm, no demonstrated candidate advantage.
Task outcomes: 27 valid passes (9 per arm), 3 initial harness-invalid attempts;
29 completed turns. Additional actual operations: 21 generation outputs,
10 native compactions, 60 probe answers. No retries or further paid work.
Final report: docs/2026-09-08-information-preservation-pilot.md.
The entries below are chronological execution history, not current blockers.

User explicitly authorized ten three-arm comparisons (30 downstream
continuations), including the previously explained preparation/compaction/probe
operations. Model: gpt-5.6-luna, xhigh. No runner retries or model substitution.

Run superseded-decision/short first as the live gate; stop on transport,
isolation, model or harness failure. It counts toward the ten comparisons.
Then run all other short cases and long cases buried-constraint,
superseded-decision, failed-attempt-trap and compound-rot: six short plus four
long comparisons, one replicate each. This exploratory sample covers all six
cases but is not balanced across bands and cannot establish statistical
superiority. Maximum 30 downstream continuations; preparation is additional.

Use frozen main /tmp/session-handoff-offline-20260907-gCo0KR/main and candidate
/tmp/session-handoff-efficiency-20260908-Hy4eEq/candidate. Pinned Codex 0.153.4 ELF,
isolated native state, readonly existing auth mount. Timeout 180 seconds per
operation. No new paid judge calls. Preserve raw evidence and score against
external gold after the execution gate succeeds. No install/push/deploy.

Status: first live gate STOPPED; no downstream continuations started.

Artifacts: benchmark/results-version/2026-09-08-information-pilot/gate/.
Main completed one handoff-generation turn and public MCP create/read, then
thread/fork failed. Candidate and nativecompact were not started. No retries.
Provider monetary cost/usage is unavailable, not zero.

Offline reproduction with pinned Codex 0.153.4, network disabled and no auth:
ephemeral source thread + injected history + fork returns -32600,
"no rollout found for thread id ...". Changing only source thread creation to
ephemeral=false makes the same fork succeed. State remains temporary and
isolated; no production session is affected. Production runner not modified.

Required correction before resuming: persistent source threads inside isolated
temporary Codex state; real offline start/inject/fork regression test. Preserve
deny-read protection for tool access to native state. Also correct the summary
counter: executed_continuations currently counts an attempted arm (reports 1),
although failure occurred before its downstream turn (actual count 0).
Previous offline readiness did not cover this real native fork lifecycle.

User subsequently approved Luna max fixing the runner and parent verification,
then resuming within the same 30-continuation authorization. The failed first
generation remains additional recorded preparation, not a completed comparison.
Resume into new output directories; never overwrite the failed gate artifacts.
Execution batches: corrected gate 3, remaining five short cases 15, four long
cases 12. Check actual started-continuation counts and fatal_failure after each
batch before starting the next. No automatic retry of failed live batches.

Evaluation fixed before resuming: review the two independent probe answers
against each external gold fact. Count a fact as recovered when its current
meaning is correctly stated (paraphrases accepted); distinguish omitted from
incorrect or contradicted facts. Report critical facts separately. Do not use
the generated handoff alone to claim native-state retention. Assess stale
decisions/repeated failed approaches from task tool evidence and final diff,
and report visible tests and hidden acceptance separately from fact recovery.
Manual parent assessment is not a separately paid judge or independently
calibrated human assessment; disclose that limitation. Do not treat literal
substring misses as semantic failures or unknown usage as zero cost.

Planned additional operations for the ten comparisons: 20 handoff generations,
10 native compactions, 60 probe turns. The previous failed gate consumed one
extra handoff-generation turn, no task continuation. Underlying provider
requests inside a tool loop are not bounded by the count of logical turns.

Luna fix delivered: source and fork threads persist only inside isolated
temporary native state; preparation/attempted/completed counters are separate.
Real offline regression covers start/inject/fork/read/list and validates the
fork's history_base reference to the source records containing injected anchors.
An empty paginated items list alone is not evidence of lost model context:
the native fork stores its inherited prefix by reference. Parent verified this
against actual temporary rollout metadata and matching local Codex source.
Focused tests: 44 passed. Runner SHA256:
dca039dd197b43cbcf6db086fa31db4315657fa256a8001d1a8f82f114c039db.
Full suite is being rerun after delivery freeze; a prior concurrent collection
used an intermediate test assertion and is not the final acceptance run.

Stable full suite passed: 923 passed, 2 skipped. Corrected live gate then stopped:
generation + MCP + two probes main succeeded; downstream main attempted but
aborted on `turn item completion belongs to another thread`. Rejected event
payload was not preserved. No candidate/native arm and no native compaction.
Actual downstream ledger: 1 attempted, 0 completed, 29 remaining under user cap.
Artifacts `gate-fixed/` preserved; no further retry. Protocol-event diagnosis
delegated read-only to Luna; do not assume the exact event origin without evidence.
Resuming requires handling legitimate interleaving without accepting the wrong
turn, recording rejected event metadata, and selecting only unattempted cells;
the failed main attempt remains in the authorized denominator.

Routing/resume fix delivered and frozen: thread+turn correlation ignores foreign
notifications, queues pre-ack completions, retains content-free event metadata,
and keeps target errors fatal. Explicit --exclude-cell validates IDs and runs
only remaining cells, without changing full-plan counts. Runner SHA256 now
db9efb24439c190ab56f2ee7f0fb98d62cbf4dbfc7d213d7c6b845176957d335.
Agent focused tests: 50 passed; parent Ruff and exclusion dry-run passed.
Parent full offline suite running before provider resumption.
Exclude exactly 49085896643926495bf307ca from the superseded-decision/short run.

Gate-remaining completed candidate/native turns and real native compaction, but
both tasks were no-ops with unavailable local tools. Visible verification failed;
hidden starting-state TTL invariant passed. Saved task_success=true is invalid.
Schema proves environments=[] disables execution access. Luna removed that
override and corrected task success to completed AND visible AND hidden.
Parent offline environment/info confirms local Bash and file:///mnt/work.
Latest runner SHA256:784e3f20002fc25b4942a3f56f6ecaf229a297c2c8cce5eff35de4b16e4e02ee.

Ledger is now 3 attempted, not 1; 27 remain. All first-case probes recover the
three gold facts; task outcomes remain harness-invalid, not successful work.
Next run buried-constraint/short (3 attempts) as the local-execution gate;
then remaining four short cases (12) and four long cases (12). No retry of any
already-attempted cell. Preserve initial short-plan arm ordering by selecting the
original five short cases and excluding buried-constraint cells after the smoke.

Final execution: short-smoke 3/3, short-remaining 12/12, long 12/12 valid task
passes, visible AND hidden checks. Parent inspected all final source diffs and
all 60 raw probe answers; semantic assessment is unblinded, not independently
calibrated. Final consistency assertions verify 30 unique cells, ten complete
triples, identical history hashes within triples, no probe tool-use violations,
and commandExecution/fileChange evidence for all 27 valid tasks. No failed cell
was retried. The generation-only gate is not a downstream attempt.
Runner SHA256 unchanged; full final-runner offline suite 930 passed, 2 skipped;
Ruff/diff check clean. Final report records corpus-size, scoring/provenance,
mixed-runner, shallow-fixture and unknown-cost limitations. No install, commit,
push, publish, deploy or new session handoff. Thirty-attempt authorization spent.
