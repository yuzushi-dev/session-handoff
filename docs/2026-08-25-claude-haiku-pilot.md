# Claude Haiku live context-fidelity pilot — 2026-08-25

Status: exploratory evidence, not release-grade. The run used Claude Code
2.1.233, alias `haiku` resolved to `claude-haiku-4-5-20251001`, one
`superseded-decision` fixture at the `long` band, and one replicate across
`full`, `handoff`, `migrate`, and `oracle`. Raw transcripts, native homes,
session identifiers, credentials, and model output remain only in gitignored
local result directories.

## Results

| Condition | Critical RCR | Incorrect facts | Stale intrusion | Hidden acceptance | Input tokens | Wall time |
|---|---:|---:|---:|---:|---:|---:|
| `full` | 1.0 | 0.0 | 0.0 | pass | 349,438 | 22.8 s |
| `handoff` | 1.0 | 0.0 | 0.0 | pass | 169,434 | 45.9 s |
| `migrate` | 1.0 | 0.0 | 0.0 | pass | 349,342 | 21.0 s |
| `oracle` | 1.0 | 0.0 | 0.0 | pass | 86,396 | 21.8 s |

All four continuations preserved the three critical facts, changed the focused
test from the superseded 60 seconds to the authoritative 15 seconds, avoided
the stale decision, and passed both the current stdlib verification and hidden
semantic acceptance in the offline sandbox. Five authenticated provider calls
were made: one each for `full`, `migrate`, and `oracle`, plus handoff generation
and continuation. Claude reported a total cost of $0.584518.

Input totals include uncached, cache-creation, and cache-read input tokens. The
handoff total combines both calls. These single-run samples do not establish a
performance or cost advantage over another client or model.

## Harness findings

The matching local Luna pilot manifest predated the stdlib verifier and still
requested `pytest`. The current hardened sandbox masks user site-packages, so
the stored automated result recorded four false negatives even though the
independent hidden acceptance passed. Rechecking the unchanged workspaces with
the current manifest's stdlib verifier produced four visible and four hidden
passes. Raw result files were retained unchanged for provenance.

The run also exposed that Claude cache-read and cache-creation input tokens were
omitted from `evaluation-run.json`. The parser now sums all three Claude input
token fields while preserving the existing metric schema.

## Limits

- One case, one size band, one replicate, Claude target only.
- Assessment was condition-aware and not calibrated against a human sample.
- The release gate remains closed: it requires all six cases, three bands, two
  or more replicates, and blinded calibrated judging.
