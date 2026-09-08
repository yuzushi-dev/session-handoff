# Product-path pilot — Luna xhigh

Status: completed. Both builds passed all six cells; all twelve product
roundtrips preserved the generated text exactly.

## Results

The approved matrix completed with `gpt-5.6-luna`, reasoning `xhigh`, through
Codex CLI 0.153.4. Exactly 24 provider CLI invocations were started, with no
runner retry, model substitution, timeout, or additional paid judge invocation.

| Metric | Main / legacy | Candidate / central |
| --- | ---: | ---: |
| Completed cells | 6 | 6 |
| Verification + hidden acceptance passed | 6/6 | 6/6 |
| Generated text read back byte-identically | 6/6 | 6/6 |
| Provider CLI invocations | 12 | 12 |
| Reported input tokens | 591,006 | 520,896 |
| Reported cached input tokens | 415,488 | 342,016 |
| Reported output tokens | 9,447 | 7,641 |
| Reported reasoning output tokens | 4,535 | 3,310 |
| Cumulative cell wall time | 358.44 s | 309.17 s |
| Supplied handoff bytes, sum | 8,563 | 8,225 |
| Failed shell commands, recovered | 2 | 1 |

Totals: **1,111,902 reported input tokens**, 757,504 cached input tokens,
17,088 output tokens and 7,845 reasoning output tokens. Token categories are
reported as emitted by Codex, not added together into an inferred billing
total. Cumulative cell time was **667.61 seconds** (about 11 minutes).
The client exposes no monetary invoice for these runs; actual currency cost
is unreported, not zero and not estimated from an unverified tariff.

All three failed shell commands were `python: command not found` (exit 127):
main `buried-constraint/long`, main `failed-attempt-trap/short`, and candidate
`failed-attempt-trap/short`. Each continuation recovered using the available
Python interpreter and passed verification and hidden acceptance. These were
model tool invocations inside a cell, not retries of the paid benchmark cell.

`verify_results.py` independently checked all twelve states, the six paired
source/template/prompt hashes, expected product tree hashes, common runner and
adapter hashes, storage identities, returned content hashes, and the exact
invocation count. `summary.json` contains the per-cell metrics.

No product-path regression was observed in this matrix. The lower candidate
time/token totals are descriptive: the generated handoffs and tool trajectories
differ, and there is only one replication. They do not establish a performance
or quality advantage. Exact product roundtrip also does not establish that the
model preserved every fact in the source transcript; no gold-fact judge scores
or human calibration are claimed.

## Authorization and fixed protocol

After the offline gate passed, the user authorized necessary spend and explicitly
selected `gpt-5.6-luna` with `xhigh` reasoning for the adapted benchmark.

- Codex client; synthetic fixtures only; read-only OAuth credential mount.
- Cases: `buried-constraint`, `superseded-decision`, `failed-attempt-trap`.
- Bands: `short` and `long`; Markdown; one replication per build/case/band.
- Twelve cells, generation plus continuation: 24 planned CLI invocations.
  This is not a count or hard cap of the underlying model's tool-loop requests.
- Alternate which build runs first in each pair. No retries or additional
  paid judge calls. Stop on model, transport, isolation or harness failure.
- Finite per-invocation timeout; no model substitution if Luna/xhigh fails.
- Use the exact product snapshots from the passed offline comparison.
  Both arms use one common adapted runner, with its identity recorded.
- Main: public MCP legacy create/read. Candidate: public MCP central create/read.
  Use an isolated synthetic product home/workspace and no supervised switch.
  Continue only from the content returned by that build's read operation.
- Do not pre-redact the generated content through the candidate's implementation
  before sending it to main: each product applies its own persistence boundary.
- Report product roundtrip, hidden acceptance, DoD, tokens and wall time separately.
  Report monetary cost only if the provider exposes it; do not invent a conversion
  from tokens for an account/model with no verified tariff.

## Meaning and limits

This pilot checks a real create/read path followed by model continuation. It is
an exploratory regression smoke, not evidence of statistical superiority or of
an agent discovering MCP tools in a real resumed client. No inference about
context-rot advantage is justified without full-context controls and replication.

## Artifacts

Product snapshots: `/tmp/session-handoff-offline-20260907-gCo0KR/{main,candidate}`.
Prepared fixtures: `benchmark/generated-2026-09-07-luna-xhigh/` (the prepared
manifest is larger; only the fixed twelve cells above will execute).
Run artifacts and exact orchestration script:
`benchmark/results-version/2026-09-07-luna-xhigh/`.
All are local; generated fixtures/results are Git-ignored.

## Preflight evidence

The adapted runner passed 47 focused tests and the frozen copy passed the full
suite with networking disabled: 843 passed, 2 skipped (86.43 seconds). Ruff,
diff checks and the twelve-cell dry run passed. Both real frozen MCP builds
passed an offline exact-content roundtrip before the first paid invocation.

Common runner:
`/tmp/session-handoff-offline-20260907-gCo0KR/pilot-runner/benchmark/run_study.py`.
SHA-256: `e332be0e5006c4b15f8cb5c4d91d5afd33960fc28d0e6eec66126ebc27db8753`.
Adapter SHA-256:
`b24eec4c3c416623922fc28a536be1da79e3da92948b8cbe4e0b33fd85abb7c1`.
The artifact directory contains `runner.tar.gz`, `fixtures.tar.gz`,
`verification.json`, the exact `run_pilot.py`, per-cell state and raw output,
and `product-roundtrip.json` with content and response hashes.

The implementation and report remain uncommitted. Product snapshots and the
previous product edits were preserved; no install, merge, push, publication,
deployment, or new session handoff was performed.
