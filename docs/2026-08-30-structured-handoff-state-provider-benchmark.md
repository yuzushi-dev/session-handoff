# Provider benchmark: structured handoff state-v1

Date: 2026-08-30  
Status: exploratory automatic evidence; not a release decision.

## Configuration

- Client: Codex CLI `0.151.0`
- Model: `gpt-5.6-luna`
- Reasoning effort: `high`
- Runner revision: `60e7836`
- Matrix: 6 context-rot cases × 3 bands × 2 replications × 2 handoff formats
- Planned cells: 72; planned provider calls: 144
- Result directory: `/tmp/session-handoff-provider-smoke-20260830`

The two existing smoke cells were reused. The runner stopped the first invalid
generation and did not retry it.

## Automatic results

| Format | Completed cells | Task success on completed cells | Median context bytes | Median input tokens | Median wall time |
|---|---:|---:|---:|---:|---:|
| `markdown-v1` | 36/36 | 36/36 | 1,444 | 183,003 | 70.62 s |
| `state-v1` | 35/36 | 35/35 | 1,839 | 192,371 | 74.45 s |

Overall: 71/72 cells completed and 143/144 provider calls started. Every
completed cell passed the hidden acceptance and visible verification.

The 35 clean paired cells show `state-v1` minus Markdown medians of:

- supplied context: **+465 bytes** (+33.4%);
- input tokens: **+22,847** (+12.5%);
- output tokens: **+197** (+9.7%);
- wall time: **+5.40 s** (+8.6%).

Local validation/rendering remains below the mechanical target: p95 **1.55 ms**
over 1,000 iterations.

## Exploratory semantic judgment

Sol Medium performed a read-only, model-judged pass over the completed
artifacts. This is exploratory evidence, not human-calibrated release scoring,
and its counts were not imported into `evaluation.json`:

- facts preserved: **261/261**;
- stale-trap activations: **0/95**;
- DoD checks: **166/166**;
- task success: **71/71** completed cells;
- recovery reads: **0**.

It found no observable semantic advantage for `state-v1` over Markdown. All 71
continuation traces also contained at least one internal command failure (**251**
in total), mostly missing `pytest` commands and `git` commands run outside the
repository. These failures are harness inefficiency, not a judged handoff
correctness failure, but they can inflate latency and token use.

## Failure

`state-v1` failed generation at `late-correction / very_long / replicate 2`.
The model returned malformed JSON with an extra closing bracket after the
`verification` array. The runner rejected it before continuation and recorded
the cell as failed; it did not repair or retry the response.

## Gate assessment

- Correctness gate: **fail** — one generation validation failure and one missing
  paired state arm; the required failure count is zero.
- Efficiency: local render gate passes; context and input-token hypotheses fail;
  median wall-time regression remains within the 10% limit.
- Continuation efficacy: **100%** on completed cells, but overall state-v1
  generation completion is 35/36 (97.2%) versus Markdown 36/36 (100%).
- Exploratory Sol judgment found **261/261** facts preserved, **0/95** stale
  traps, **166/166** DoD checks, and no recovery reads; it found no semantic
  advantage for state-v1. It is not human-calibrated and therefore is not
  release-grade evidence.

Decision: do not make `state-v1` the product default. In this run it was not
more efficient than Markdown and exposed a strict-JSON reliability failure;
the remaining release-grade step is blinded, human-calibrated judgment.

The full local test suite after the hardening changes passed: `649 passed,
2 skipped`. No push, publish, or deploy was performed.

## Post-run hardening probe

After this study, the runner was hardened in local commit `8114def`:

- Codex `state-v1` generation now passes the contract through native
  `--output-schema`, while the local validator remains authoritative.
- Continuation prompts avoid unavailable `git` and `pytest` commands in the
  fixture workspace; trace artifacts classify environment failures.
- Paired medians and p95 values exclude incomplete or invalid pairs.

The previously failed cell was rerun as a two-call regression probe with the
same model and `high` effort. It completed **2/2** calls, produced valid JSON,
passed task verification and acceptance, and recorded zero trace failures.
This probe is not added to the original aggregate because the runner revision
changed; a fresh full comparison would require the complete 144-call matrix.

## Full post-hardening rerun (2026-08-31)

- Runner revision: `45e9dcf`; same Codex CLI `0.151.0`, `gpt-5.6-luna`, and
  `high` effort. New result directory:
  `/tmp/session-handoff-provider-full-hardening-20260831`.
- The complete candidate matrix finished: **72/72 cells**, **144/144 provider
  calls**, all cells `completed`, all `task_success=true`. Visible verification
  and hidden acceptance passed in every cell.
- All 36 `state-v1` responses passed the local `server.handoff_state` validator;
  the native schema hash matched in 36/36 runs. Pairing passed for **36/36**
  Markdown/state pairs with no identity or timestamp errors.

| Format | Cells | Median context | Median input tokens | Median output tokens | Median wall |
|---|---:|---:|---:|---:|---:|
| `markdown-v1` | 36/36 | 1,456 B | 95,179 | 1,124 | 36.82 s |
| `state-v1` | 36/36 | 2,007.5 B | 101,205.5 | 1,355.5 | 41.22 s |

Paired state-v1 minus Markdown medians: **+555 B context (+38.1%)**,
**+807.5 input tokens (+0.85%)**, **+217 output tokens (+19.3%)**, and
**+3.11 s wall time (+8.4%)**. Paired wall overhead by band was +13.7% at
`short`, +8.5% at `long`, and +5.8% at `very_long`. Paired p95 deltas were
+904 B, +16,182 input tokens, +855 output tokens, and +17.68 s wall time.

The trace classifier recorded 24 internal command failures (14 Markdown, 10
state): 0 `pytest_unavailable`, 0 `git_outside_repository`, and 24 `other`.
Inspection shows 17 `python`-unavailable commands, one missing `rg`/fixture
path, and six exploratory no-match or pre-fix assertions. These did not affect
the verification/acceptance result, but remain harness inefficiency.

This rerun removes the prior generation-reliability failure, but it does not
establish semantic superiority: semantic counters remain unjudged and no
condition-blind human calibration was performed. `state-v1` therefore remains
non-default; the automatic evidence still shows materially larger context and
output, with wall overhead below 10% overall but above it for `short`.
