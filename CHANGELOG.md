# Changelog

## 0.7.2 — unreleased candidate

- Add fail-open automatic compaction checkpoints with redacted local Git state,
  lifecycle evidence, and pointer-only `SessionStart` reinjection.
- Preserve the 0.7.1 candidate telemetry, migration, and benchmark hardening;
  Markdown remains the default and structured state remains opt-in.
- Harden checkpoint timing/durability, strict MCP input and file bounds,
  setup-state path validation, and safe partial telemetry acknowledgements.
- Add the MIT license file from `origin/main`.

## 0.7.1 — unreleased candidate

- Harden telemetry event provenance and migrate legacy local counters without
  changing the opt-in default.
- Add Ruff CI coverage and a deterministic version-aware benchmark with its
  2026-09-02 pilot reports.
- Keep the Markdown handoff as the default; structured state remains opt-in.

Release gates still open: independent privacy/security review and backend
retention, purge, backup, hosting, contact, and abuse-control verification.
No npm publish, GitHub release, tag mutation, or deploy is performed by this
candidate preparation.

## 0.7.0

See the published [GitHub release](https://github.com/yuzushi-dev/session-handoff/releases/tag/v0.7.0).
