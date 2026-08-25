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
| `handoff` | Fresh session with generated `handoff.md` only | Measures the product behavior |
| `migrate` | Native migrated session | Controls for harness switching without semantic compression |
| `oracle` | Fresh session with a human-authored minimal state document | Estimates the upper bound for a clean state transfer |

The `oracle` condition is important. If `full` fails because of context rot while `oracle` succeeds, the task is sensitive to stale context. If `handoff` then approaches `oracle`, the handoff is doing useful compression rather than merely copying history.

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

The default suite contains 6 context-rot cases, 3 size bands, 4 conditions, and 2 replications, for 144 continuation runs. The command writes the rendered transcripts, oracle state documents, per-case gold annotations, and an `evaluation.json` skeleton. `benchmark/generated/` and `benchmark/results/` are gitignored so long transcripts and study outputs stay out of the repository.

## Planning and running one study cell

The runner selects exactly one manifest cell. Its default is a content-free plan: it does not create a result directory or call a provider.

```bash
python3 benchmark/run_study.py benchmark/generated/evaluation.json \
  --client codex \
  --model <exact-model-id> \
  --case superseded-decision \
  --band long \
  --condition handoff \
  --replicate 1
```

Execution needs both an action flag and a separate cost acknowledgement:

```bash
python3 benchmark/run_study.py benchmark/generated/evaluation.json \
  --client codex \
  --model <exact-model-id> \
  --case superseded-decision \
  --band long \
  --condition handoff \
  --replicate 1 \
  --execute \
  --acknowledge-provider-cost
```

Each run gets an independent workspace and native client home. `full` resumes a seeded target-client session; `migrate` seeds the opposite client, invokes the real migration backend, then resumes the target; `handoff` generates and validates the canonical Markdown handoff in a repository-blind sandbox, then starts fresh; `oracle` starts fresh from the gold state document. The runner passes prompts through stdin, captures normalized tool events, and prints a content-free summary. It does not retry provider failures. After the visible repository tests, a fixture-specific acceptance command hidden from the agent checks the authoritative semantics, so a coherently stale code-and-test edit cannot become an automated pass. Both checks run offline with host files and environment hidden; acceptance sees the fixture read-only. Raw model output, supplied context, verification output, native session data, trace, and workspace diff remain in the ignored run directory.

On Linux, the runner uses Bubblewrap to expose the fixture workspace and isolated client home while hiding the study source, host home, repository, and unrelated temporary files. It mounts an existing Claude or Codex OAuth credential read-only into the isolated home and never copies its content. Use `--credential-mode environment` to disable that mount. The runner passes a small environment allowlist; add a required provider variable with `--pass-env NAME`. Handoff generation fails closed when `bwrap` is unavailable. Claude continuation uses `bypassPermissions` inside this OS sandbox so non-interactive shell verification can run; the tool set remains limited to repository reads, edits, and Bash. See [Claude permission modes](https://code.claude.com/docs/en/permission-modes).

A non-fixture transcript requires both `--source <path>` and `--allow-non-fixture-source`; its content will be copied into the result directory, so do not use that mode for secrets or commit its artifacts.

`--resume` is accepted only at a recorded retry-free checkpoint, including a handoff setup failure before any provider call. It refuses completed, provider-failed, or ambiguous runs rather than risking a duplicate billed call.

For one client/model study, the default matrix is 144 continuations. The 36 `handoff` cells also need a generation call, so execution is 180 provider calls before any automated judging. Run a small synthetic pilot and inspect costs before authorizing the matrix.

### Run artifacts

- `state.json`: content-free selection, client and migration executable provenance, session IDs, phase, call counters, prompt, study-manifest, verifier, acceptance, seed, and snapshot hashes.
- `supplied-context.md`: the blinded condition input.
- `continuation.txt`, `trace.json`, `workspace.diff`, `verify.stdout`, `verify.stderr`, `acceptance.stdout`, `acceptance.stderr`: Stage B evidence.
- `handoff.md`: generated only for the handoff condition.
- `migration.json`: content-free loss report for the migrate condition.
- `evaluation-run.json`: deterministic outcomes and blank manual counters.
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
`session-migrate` writer into a temporary Claude home, and verifies the target
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

Fill the judge labels and continuation outcomes in the generated `evaluation.json`, following `benchmark/JUDGE.md`. Unjudged counters are deliberately `null`, so scoring refuses incomplete evidence. Then run:

```bash
python3 benchmark/score.py benchmark/generated/evaluation.json --pretty
```

For a study, keep model, model settings, repository snapshot, fixture seed, and continuation prompt fixed across conditions. Randomize condition order and blind the judge to the condition name when practical.

## Minimum study before changing the handoff format

Use all fixture archetypes at `short`, `long`, and `very_long`, with at least two independent runs per condition. Do not tune the handoff template against the test fixtures. Add new fixtures when a real failure mode is discovered and keep old fixtures as regression cases.
