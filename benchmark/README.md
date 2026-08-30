# Session Handoff Context-Rot Benchmark

This benchmark measures whether a semantic handoff recovers the **current operational state** of a long coding-agent session after context rot has had time to accumulate.

It does not reward transcript similarity. A good handoff should discard most of a long session. The benchmark rewards retaining active facts, excluding stale facts, avoiding repeated failed work, and completing the next task with less context.

## Research question

Given the same long session at the same interruption point, does a fresh session started from `session-handoff` continue the work as well as or better than the original long session while using less context?

A secondary question is whether native migration preserves the same context-rot burden because it preserves the transcript instead of compressing it.

## Conditions

Run every fixture under the same model and repository snapshot.

| Condition | Input to continuation agent | Purpose |
| --- | --- | --- |
| `full` | Original long session continued in place | Measures the context-rot baseline |
| `handoff` | Fresh session with generated `handoff.md` only | Measures the product behavior; paired as `markdown-v1` and `state-v1` |
| `migrate` | Native migrated session | Controls for harness switching without semantic compression |
| `oracle` | Fresh session with a human-authored minimal state document | Estimates the upper bound for a clean state transfer |

The `oracle` condition is important. If `full` fails because of context rot while `oracle` succeeds, the task is sensitive to stale context. If `handoff` then approaches `oracle`, the handoff is doing useful compression rather than merely copying history.

The `handoff` condition keeps one product label and has two manifest arms:
`markdown-v1` asks for the existing Markdown contract; `state-v1` asks for one
strict typed JSON object and renders the same canonical Markdown. Older manifests
without `handoff_format` mean `markdown-v1`.

## What fixtures must contain

Fixtures are designed around context rot, not generic summarization. Each long-session fixture should contain several of these stressors:

- **Buried active constraint**: a requirement introduced early and still valid at the interruption point.
- **Superseded decision**: an earlier decision that was later changed. The old choice remains prominent in the history.
- **Failed-attempt trap**: a plausible approach already tried and rejected with evidence.
- **Late correction**: a factual correction close to the interruption point that conflicts with older context.
- **Partial implementation state**: some files or tests are complete while adjacent work remains pending.
- **Similar names**: files, symbols, branches, or APIs that are easy to confuse after a long session.
- **Tool-output noise**: long logs, diffs, test output, dependency output, or unrelated diagnostics.
- **Topic drift**: side investigations that consumed context but are irrelevant to the next action.
- **Repeated stale mentions**: obsolete facts appear more often than the current fact, so frequency is a bad retrieval heuristic.

Fixture specs remain compact. `render_fixture.py` expands deterministic noise to produce long session transcripts at the requested size.

## Two-stage evaluation

### Stage A: handoff fidelity

Generate a handoff at the fixture interruption point. A blinded judge compares only the generated handoff with the fixture gold state.

For every gold fact, assign one of:

- `preserved`: the handoff carries the fact with enough precision to act on it.
- `missing`: the fact is absent or too vague to use.
- `incorrect`: the handoff states a conflicting fact.

For every stale trap, record whether the handoff reintroduced it as current state.

Primary fidelity metrics:

- **Required Context Recall (RCR)** = preserved required facts / required facts.
- **Weighted RCR** = preserved fact weight / total fact weight.
- **Incorrect Fact Rate (IFR)** = incorrect required facts / required facts.
- **Stale Context Intrusion (SCI)** = activated stale-trap weight / total stale-trap weight.

The benchmark reports these separately. It does not collapse them into one opaque score.

### Stage B: continuation utility

Continue the same task under `full`, `handoff`, `migrate`, and `oracle`.

Record:

- Definition-of-Done criteria passed.
- Final task success.
- Repeated failed attempts.
- Stale decisions acted on.
- Repository re-reads needed to recover missing state.
- Input/output token usage when the harness exposes it.
- Wall-clock time when available.

The main product comparison is `handoff` versus `full`. `migrate` shows whether retaining the full transcript carries context rot across harnesses. `oracle` shows how close the generated handoff gets to an ideal compact state transfer.

## Fixture size bands

Use at least three bands. The renderer reports only an approximate token count because the repository has no tokenizer dependency.

- `short`: about 8k tokens, a control where context rot should be limited.
- `long`: about 40k tokens.
- `very_long`: about 80k tokens.

The same semantic fixture can be rendered at multiple bands. This isolates the effect of accumulated irrelevant history from task difficulty.

## Running one fixture

```bash
python3 benchmark/render_fixture.py \
  benchmark/fixtures/context_rot_cases.json \
  --case superseded-decision \
  --band long \
  --output /tmp/session.md
```

The output ends at the interruption point and includes the continuation request. It never includes the gold annotations.

## Preparing the full study

```bash
python3 benchmark/prepare_study.py \
  benchmark/fixtures/context_rot_cases.json \
  --output benchmark/generated \
  --runs-per-condition 2
```

The default suite contains 6 context-rot cases, 3 size bands, 4 conditions, two
handoff formats, and 2 replications, for 180 continuation runs. The command
writes the rendered transcripts, oracle state documents, per-case gold
annotations, and an `evaluation.json` skeleton. `benchmark/generated/` and
`benchmark/results/` are gitignored so long transcripts and study outputs stay
out of the repository.

To prepare only the paired handoff candidate, use `--handoff-only`. With two
replications this creates 72 manifest rows; executing them requires 144
provider calls (generation plus continuation per arm). The default matrix is
unchanged.

## Planning and running one study cell

The runner selects exactly one manifest cell. Its default is a content-free plan: it does not create a result directory or call a provider.

```bash
python3 benchmark/run_study.py benchmark/generated/evaluation.json \
  --client codex \
  --model <exact-model-id> \
  --reasoning-effort high \
  --case superseded-decision \
  --band long \
  --condition handoff \
  --handoff-format markdown-v1 \
  --replicate 1
```

Execution needs both an action flag and a separate cost acknowledgement:

```bash
python3 benchmark/run_study.py benchmark/generated/evaluation.json \
  --client codex \
  --model <exact-model-id> \
  --reasoning-effort high \
  --case superseded-decision \
  --band long \
  --condition handoff \
  --handoff-format state-v1 \
  --replicate 1 \
  --execute \
  --acknowledge-provider-cost
```

Each run gets an independent workspace and native client home. `full` resumes a seeded target-client session; `migrate` seeds the opposite client, invokes the internal migration engine, then resumes the target; `handoff/markdown-v1` generates and validates the existing Markdown contract; `handoff/state-v1` requires exactly one validated JSON state object and renders it to canonical Markdown. Codex state-v1 generation also passes the same contract as Codex's native `--output-schema`; the local validator remains authoritative. Both handoff arms then start fresh; `oracle` starts fresh from the gold state document. The continuation prompt tells the agent that the fixture workspace is not a Git repository and that the harness runs verification, so it does not waste calls on unavailable `git` or `pytest` commands. The runner passes prompts through stdin, captures normalized tool events, and prints a content-free summary. It rejects invalid state output and does not retry provider failures. After the visible repository tests, a fixture-specific acceptance command hidden from the agent checks the authoritative semantics, so a coherently stale code-and-test edit cannot become an automated pass. Both checks run offline with host files and environment hidden; acceptance sees the fixture read-only. Raw model output, supplied context, verification output, native session data, trace, and workspace diff remain in the ignored run directory.

On Linux, the runner uses Bubblewrap to expose the fixture workspace and isolated client home while hiding the study source, host home, repository, and unrelated temporary files. It mounts an existing Claude or Codex OAuth credential read-only into the isolated home and never copies its content. Use `--credential-mode environment` to disable that mount. The runner passes a small environment allowlist; add a required provider variable with `--pass-env NAME`. Handoff generation fails closed when `bwrap` is unavailable. Claude continuation uses `bypassPermissions` inside this OS sandbox so non-interactive shell verification can run; the tool set remains limited to repository reads, edits, and Bash. See [Claude permission modes](https://code.claude.com/docs/en/permission-modes).

A non-fixture transcript requires both `--source <path>` and `--allow-non-fixture-source`; its content will be copied into the result directory, so do not use that mode for secrets or commit its artifacts.

`--resume` is accepted only at a recorded retry-free checkpoint, including a handoff setup failure before any provider call. It refuses completed, provider-failed, or ambiguous runs rather than risking a duplicate billed call.

For Codex, pass `--reasoning-effort` explicitly when comparing runs. The runner
records it in the run identity and provenance; it is not inherited from the
user configuration because live runs ignore that configuration.

For one client/model study, the default matrix is 180 continuations. The 72
paired `handoff` cells also need a generation call, so execution is 252 provider
calls before any automated judging. Run a small synthetic pilot and inspect
costs before authorizing the matrix.

### Run artifacts

- `state.json`: content-free selection, handoff format, recorded arm order and execution timestamp, client and internal migration provenance, session IDs, phase, call counters, prompt, study-manifest, verifier, acceptance, fixture seed, schema hash, and snapshot hashes.
- `supplied-context.md`: the blinded condition input.
- `continuation.txt`, `trace.json`, `workspace.diff`, `verify.stdout`, `verify.stderr`, `acceptance.stdout`, `acceptance.stderr`: Stage B evidence.
- `handoff.md`: generated only for the handoff condition.
- `migration.json`: content-free loss report for the migrate condition.
- `evaluation-run.json`: deterministic outcomes, trace failure categories, and blank manual counters.
- `blinded/<blind-id>/judge.json`: condition-free review bundle with evidence fields and calibration metadata.
- `private/blind-map.json`: mode-0600 mapping from blind IDs to run conditions inside a mode-0700 directory; do not give it to judges.

The offline pilot uses fake executables and no provider:

```bash
python3 -m pytest -q tests/test_run_study.py
```

The first live Codex/Luna exploratory pilot is recorded in
[the 2026-08-25 pilot report](../docs/2026-08-25-codex-luna-pilot.md).

## Native migration integration

The benchmark also has an isolated end-to-end migration test:

```bash
python3 -m pytest -q tests/test_benchmark_migrate.py
```

It creates a native-format paginated Codex home, runs the installed
bundled migration engine into a temporary Claude home, and verifies the target
JSONL, manifest, target UUID, source immutability, dropped events, normalized
fields, and content sentinels for edits, MCP, collaboration, subagents, plans,
hooks, reviews, web results, media references, and compaction. The round-trip
test writes the same portable content back to a temporary Codex home. It uses
synthetic benchmark data and never opens a live client session.

An opt-in smoke test exercises the same path with a real local historical
session. It reads the source in place, writes only to temporary directories,
and never prints or commits transcript data:

```bash
SESSION_HANDOFF_REAL_CODEX_SESSION_ID=<historical-session-id> \
  python3 -m pytest -q tests/test_real_long_migration.py
```

The test requires at least 1,000 canonical items by default; override that
threshold with `SESSION_HANDOFF_REAL_MIN_ITEMS`. Do not pass an active session.

## Scoring

Complete the blinded `judge.json` files and study-level judging metadata,
following `benchmark/JUDGE.md`. Import them into a new evaluation; the command
fails on missing runs, mismatched mappings, altered definitions, or empty
evidence:

```bash
python3 benchmark/import_judgments.py \
  benchmark/generated/evaluation.json \
  benchmark/results/<study> \
  --judging benchmark/generated/judging.json \
  --output benchmark/generated/evaluation.judged.json
```

Unjudged counters are deliberately `null`, so scoring refuses incomplete
evidence. Then run:

```bash
python3 benchmark/score.py benchmark/generated/evaluation.judged.json --pretty
```

`handoff_fidelity_gate` checks the existing `markdown-v1` arm. The separate
`structured_state_gate` requires both recorded handoff arms for every one of
the six release cases, three bands, and two minimum replications; it then checks
state-v1 for critical recall, zero incorrect/stale facts, hidden
acceptance/task success, and a complete DoD. A pair with a missing or mismatched
fingerprint, or an execution timestamp that contradicts the recorded arm order,
is not scored as a delta. `paired_handoff` reports raw
state-minus-Markdown deltas and per-arm medians and p95 values for context
bytes, tokens, recovery reads, wall time, semantic metrics, and task success.
`release_gate`
additionally requires all six release cases, all three bands, all four
conditions, at least two replications, successful handoff, migrate, and oracle
continuations, and condition-blind human-calibrated judging with documented
axis coverage and agreement of at least `0.8`. A pilot can pass the fidelity
gate but cannot pass the release gate. Calibration needs at least 18
human-reviewed samples.

Before authorizing a rerun, record the decision rule: state-v1 must have zero
invalid generations, no incorrect or stale facts, complete hidden acceptance
and DoD, and no task-success regression versus Markdown. Any overhead must be
justified by a measurable semantic benefit; otherwise Markdown remains the
default. Report the paired median and p95, but do not call an exploratory sample
statistically significant without a predeclared analysis and an adequate sample.

For a study, keep model, model settings, repository snapshot, fixture seed, and continuation prompt fixed across paired arms. Randomize arm order and blind the judge to the condition name when practical. The handoff judge bundle is presentation-blind: it normalizes headings, whitespace, list markers, and empty markers, but content choices can still reveal the arm, so it is not condition-blind.

## Minimum study before changing the handoff format

Use all fixture archetypes at `short`, `long`, and `very_long`, with at least two independent runs per condition. Do not tune the handoff template against the test fixtures. Add new fixtures when a real failure mode is discovered and keep old fixtures as regression cases.
