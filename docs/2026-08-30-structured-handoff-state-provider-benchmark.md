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

The full local test suite after the runner setting change passed: `642 passed,
2 skipped`. No push, publish, or deploy was performed.
