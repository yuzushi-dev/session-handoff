# Main/candidate benchmark

User authorization: execute the offline comparison; proceed to the paid pilot
if it passes. No merge, publication, deployment, or new session handoff.

## Inputs and method

- Baseline: `main` at `6bcc4e7bae4af4c7be2e42308f7afb9495f1b61f`.
- Candidate: working tree based on `b5bee57ec5b5063e7c68eabdf5ad2a2a1be9ea2f`,
  including the existing uncommitted product fixes.
- Freeze source copies under `/tmp/session-handoff-offline-20260907-gCo0KR`;
  record content hashes, including nonignored untracked source files.
- Run one common deterministic runner against both copies, with temporary
  homes/workspaces and no provider credentials. Synthetic test records are
  confined to those temporary homes.
- Check shared legacy behavior and the candidate's central storage, pagination,
  scope boundaries, CLI read/list, read-only diagnostics, and resume prompt.
  Missing baseline capabilities are reported as unsupported, not failures.

## Offline gate

All supported scenarios must satisfy explicit behavioral assertions. Inspect
every difference; a changed observation alone is neither a regression nor an
improvement. Run candidate tests and lint. Any unexplained behavioral failure
blocks the paid stage. Test totals are verification evidence, not efficacy.

## Conditional pilot

Proposed matrix: 3 cases × 2 size bands × 2 builds × 1 Markdown format ×
1 replication = 12 cells, generation plus continuation = 24 harness provider
invocations, with no retries or extra judge calls. Agent tool loops can entail
more model requests than this invocation count.

Before execution, verify that the live runner actually exercises a differing
product path, identify the exact model/provider and cost controls, and record
the final cases, bands, order, expected token usage and spend envelope.
If the runner bypasses the changed product behavior, report that limitation
before spending or changing the study's scope.

The pilot is descriptive: one replication cannot establish statistical
superiority. Fidelity and continuation success remain separate outcomes.
