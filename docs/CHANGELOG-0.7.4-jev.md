# Changelog

This is a parallel prerelease alongside [0.7.4](CHANGELOG-0.7.4.md), not its
successor: same base as 0.7.4, plus an opt-in TypeSafe/Jev integration.
Everything below is off by default and requires an explicit choice plus your
own TypeSafe credentials; a plain 0.7.4 install is unaffected.

## 0.7.4-jev — 2026-09-18

- Add an opt-in TypeSafe/Jev backend for the checkpoint tool-summary scorer
  (`SESSION_HANDOFF_CHECKPOINT_SCORER=typesafe`), alongside the existing
  local-Ollama option — unset behaves exactly as plain 0.7.4.
- Add `server/typesafe_client.py`: a fail-open TypeSafe (Jev) client with
  bidirectional secret redaction, used by both the checkpoint scorer and the
  benchmark judge below.
- Add a TypeSafe/Jev benchmark judge (`benchmark/typesafe_judge.py`) that can
  batch-fill `judge.json` for every blind run under a study, without
  weakening the existing release gate (18+ calibration samples, 80%+
  agreement, human-reviewed) documented in `benchmark/JUDGE.md`.
- Add an opt-in compact-advisor Stop hook
  (`SESSION_HANDOFF_COMPACT_HINT=typesafe`) that suggests, never forces,
  running `/compact` at a good checkpoint. Score and floor math ported and
  attributed from [kunchenguid/compact-adviser](https://github.com/kunchenguid/compact-adviser)
  (MIT), whose eval harness validated these thresholds against real session
  checkpoints.
- `doctor` reports TypeSafe configuration status (`configured: bool` only,
  no network call, keeping its `provider_calls: 0` contract).
- Accept an optional semver prerelease suffix in the package version format,
  needed for this release's own version string.
