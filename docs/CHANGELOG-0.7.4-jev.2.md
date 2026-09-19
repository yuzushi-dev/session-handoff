# 0.7.4-jev.2 — 2026-09-19

Audit fixes on the existing parallel prerelease track. The stable GitHub
release and npm `latest` remain 0.7.4. Install this prerelease with
`npx session-handoff@jev setup` or pin `session-handoff@0.7.4-jev.2`.

## Fixes

- Redact sensitive structured values recursively before optional TypeSafe
  scoring, including values inside lists and nested objects.
- Remove the complete body of sensitive YAML block scalars during redaction,
  preserving adjacent fields.
- Bind checkpoints and latest pointers to both workspace and session identity,
  preventing another session's checkpoint from being selected.
- Allow onboarding to retry after launcher errors or an unsuccessful setup child.
- Resume the exact source session when a migration target fails to launch or
  exits unsuccessfully, with one rollback attempt and one terminal outcome.
- Keep native transcript provenance separate from temporary projections.
  Paginated migrations record a digest of the selected SQLite rows from the
  same read snapshot, including committed WAL data.
- Clarify transcript coverage and optional model calls in the documentation.

## Validation

- Full Python suite: 1112 passed, 3 skipped.
- Independent acceptance checks: 33 passed.
- Ruff and whitespace checks passed.
- Real interactive client migration, the live TypeSafe API, and macOS were
  not exercised.
