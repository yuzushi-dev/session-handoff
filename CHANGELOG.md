# Changelog

## 0.7.3 — unreleased candidate

- Store handoffs centrally with reference-first retrieval and legacy compatibility.
- Use native Claude/Codex plugin manifests; archive the portable manifest outside
  the package root so Codex loads hooks and starts MCP correctly.
- Show launcher setup and separate telemetry consent notices during onboarding.
- Attribute telemetry to the actual client, record fallback and failed migration
  outcomes, and preserve client identity across managed launches and switches.
- Reject benchmark runs when repository provenance cannot be verified.
- Clarify that pilot raw evidence is local and unpublished, preserving reported
  results and their limitations. Align the CI Codex smoke test to 0.153.4.

## 0.7.2 — 2026-09-03

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

Operational follow-ups: the post-push CI run and the seven-day telemetry
canary still need to complete. npm publishing remains owner-controlled.

## 0.7.0

See the published [GitHub release](https://github.com/yuzushi-dev/session-handoff/releases/tag/v0.7.0).
