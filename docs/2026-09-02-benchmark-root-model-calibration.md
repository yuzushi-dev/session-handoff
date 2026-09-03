# Context-rot benchmark: independent root-model calibration

Date: 2026-09-02

Status: 36 condition-blind items cover every release axis; human calibration
gate remains open.

## Protocol

The judge used rubric version 1 from `benchmark/JUDGE.md`. A deterministic
selection chose the lexicographically first blind ID for every case × band
cell from the completed provider-hardening study. The judge saw only each
`blinded/<id>` bundle. Case, band, handoff representation, replicate and the
private mapping were revealed only after all 18 decisions were locked.

That first sample covers all six cases, all three bands, and both handoff
representations (9 Markdown, 9 state-v1), but only the `handoff` condition.

The machine-readable decisions and SHA-256 bundle fingerprints are in
`benchmark/calibration/2026-09-02-root-model.json`.

An additional condition-blind sample uses 36 held-out bundles from
`release-luna-d5fe976`. The lexicographically first 36 blind IDs and their
bundle hashes were committed before scoring. The decisions were then committed
separately, before the private map was opened. After unblinding, the sample was
confirmed to cover all six cases, all three bands, and all four conditions:
`full` (9), `handoff` (8), `migrate` (9), and `oracle` (10).

The reproducible trail is:

- `benchmark/calibration/2026-09-02-root-model-all-conditions.selection.json`;
- `benchmark/calibration/2026-09-02-root-model-all-conditions.blind.json`;
- `benchmark/calibration/2026-09-02-root-model-all-conditions.json`.

## Result

- 36/36 items retained every gold fact;
- 0 stale traps activated;
- 36/36 continuations satisfied every DoD item and hidden acceptance;
- recovery reads, repeated failed attempts and stale decisions acted on: 0;
- no judgment was changed after unblinding.

The failed shell probes visible in some traces were environment/tooling misses
such as `python` not being on `PATH`. They did not repeat a rejected product
approach and were followed by successful `python3` checks.

## Gate decision

This is an independent model review, not human calibration. The exact runtime
model revision is not exposed, so the recorded model identity says so. There
is no human reference label set; agreement is therefore `null`, not an invented
score, and `human_reviewed` is `false`.

The axis-coverage gap is closed. The release calibration gate remains open
because a human reviewer has not produced independent reference labels:
agreement against the required `0.8` threshold is unmeasurable and critical
disagreements cannot yet be adjudicated.

The 36-run source predates the current 0.7.1 candidate. It calibrates consistent
application of rubric v1 across benchmark cases, bands, and conditions; it does
not establish the current candidate's comparative efficacy.

No fixture, rubric, prompt, handoff template, or held-out output was modified.
No provider call was made for this calibration.
