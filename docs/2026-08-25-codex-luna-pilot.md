# Codex Luna live context-fidelity pilot — 2026-08-25

Status: exploratory evidence, not release-grade. The run used Codex CLI 0.149.1,
`gpt-5.6-luna`, one `superseded-decision` fixture at the `long` band, and one
replicate across `full`, `handoff`, `migrate`, and `oracle`. Raw transcripts,
native homes, session identifiers, credentials, and model output remain only in
gitignored local result directories.

## Final selected results

| Condition | Critical RCR | Incorrect facts | Stale intrusion | Hidden acceptance | Input tokens | Wall time |
|---|---:|---:|---:|---:|---:|---:|
| `full` | 1.0 | 0.0 | 0.0 | pass | 359,197 | 39.6 s |
| `handoff` | 1.0 | 0.0 | 0.0 | pass | 153,735 | 48.9 s |
| `migrate` | 1.0 | 0.0 | 0.0 | pass | 399,385 | 41.7 s |
| `oracle` | 1.0 | 0.0 | 0.0 | pass | 115,831 | 44.5 s |

All four selected continuations preserved the three critical facts, changed the
focused test from 60 to the authoritative 15 seconds, avoided the obsolete
decision, and passed both visible tests and the hidden semantic acceptance.
`benchmark/score.py` reported a passing handoff release gate.

The handoff total includes its generation and continuation calls. The token and
latency samples are single-run observations, not comparative estimates.

## Defects exposed and fixed during the pilot

1. Bubblewrap hid Codex's required sibling `codex-code-mode-host`. The sandbox
   now mounts the client and companion binaries explicitly.
2. The synthetic Codex `full` seed contained metadata plus an `event_msg` that
   current Codex does not materialize as conversation context. The seed now has
   required metadata and a native user `response_item`.
3. A visible test could pass after restoring both code and assertion to a stale
   value. Every fixture now supplies a gold-bearing acceptance command hidden
   from the model; malformed acceptance fails before any provider call.
4. `session-migrate` 0.7.1 emitted adjacent duplicate Claude→Codex user records.
   The adapter retains the native `response_item`, removes the duplicate
   `event_msg`, rehashes the target manifest, and reports the normalization.
5. Verification inherited host files, environment, and network access. Visible
   verification now runs offline in a writable fixture sandbox; hidden
   acceptance runs offline with the fixture read-only and no provider secrets.
6. Live ignored workspaces were collected by the repository test command.
   `pytest.ini` now limits discovery to the project `tests/` directory.

Eight authenticated Luna calls were made: five belong to the final four-cell
pilot and three were invalidated while diagnosing the harness defects above.
Two additional resume attempts failed locally before reaching the provider.

## Limits

- One case, one size band, one replicate, Codex target only.
- Assessment was condition-aware and not calibrated against a human sample.
- The passing scorer gate is therefore pilot evidence, not a release decision.
- Release-grade evidence still needs all six cases, three bands, two or more
  replicates, blinded calibrated judging, and a corresponding Claude live run.
