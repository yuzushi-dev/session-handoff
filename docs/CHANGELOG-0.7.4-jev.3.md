# 0.7.4-jev.3 — 2026-09-19

Consent-gated installation status telemetry on the jev prerelease track.
Install this prerelease with npx session-handoff@jev setup or pin
session-handoff@0.7.4-jev.3.

## Added

- Add explicit consent v3 for a minimal pseudonymous installation registry.
- Record registration, daily or version-change observations, and managed
  uninstall status with a per-home random installation ID.
- Keep v1/v2 anonymous aggregates and install/uninstall lifecycle telemetry
  compatible; v3 never runs a daemon and observes only during setup,
  reconciliation, or session start.
- Document registry retention and the local purge and opt-out boundaries.

## Validation

- Python test suite and telemetry acceptance tests pass.
- Ruff, package metadata checks, and npm pack --ignore-scripts pass.
- The packed artifact excludes repository tests, worktrees, and graph output.
