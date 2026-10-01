# Changelog

Older history through the last published release (0.7.3) is archived at
[`docs/CHANGELOG-0.7.3.md`](CHANGELOG-0.7.3.md).

## 0.7.4-jev.5 — 2026-10-02

- Add a Claude Code mod (Claude Code 2.1.287 or newer) over the lifecycle
  hooks: it runs the same Python scripts for `SessionStart`,
  `UserPromptSubmit`, `PreCompact` and `Stop`, then marks the classic hook
  payload so the command hooks exit early. The mark is set only after the
  mod's own run happened, so a failed spawn falls back to the classic hook.
- Show the opt-in compact hint as a band above the prompt, and a toast after
  the pre-compaction checkpoint is saved. The `SessionStart` notice (launcher
  setup, telemetry consent) appears as a transcript line instead of a system
  message; the model still does not receive it.
- The mod is registered from `hooks/claude-mod.json`; `hooks/hooks.json` is
  unchanged, since Codex rejects a `modules` key there. Codex and older Claude
  Code keep using the classic hooks.
- Not yet verified: a live run with the TypeSafe compact hint configured, a
  marketplace install, and loading on Claude Code releases older than
  2.1.287.

## 0.7.4-jev.4 — 2026-09-30

- Add isolated compatibility fixtures and receipts bound to package content,
  client versions and platform; separate doctor readiness from certification.
- Make launcher transitions durable with request IDs, a journal, locking and
  conservative recovery. Claim checkpoint reinjection atomically.
- Harden migration ancestry, tool ordering, source-change detection, output
  ownership and provenance/loss manifests.
- Isolate doctor probes, resolve managed launchers without executing them,
  and support uninstalling either client independently.
- Record native Linux acceptance of Codex 0.159.2/Luna and Claude
  2.1.285/Anthropic Sonnet, including both compaction modes, two-way migration
  and Sando 0.7.0 composition. These live receipts describe the tested
  0.7.4-jev.3 final-C build; the version-bumped package receives separate
  packaging validation. macOS and the full historical matrix remain unverified.

## 0.7.4 — 2026-09-18

- Add a scored tool summary to the `PreCompact` recovery checkpoint, replacing
  the "unavailable from lifecycle hook" placeholder with real, redacted
  output: each tool call before compaction is kept verbatim, truncated, or
  dropped by a deterministic heuristic, with an optional local Ollama model
  resolving ambiguous cases (fail-open to keep on any failure, timeout, or
  missing model; never sent to a third-party service). `doctor` reports
  local-scorer availability (installed/reachable/model pulled) separately
  from readiness.
- Fix a pre-existing test-isolation bug that let a shared bindings store grow
  unbounded across unrelated test runs on the same machine.
- Use `Path.home()` instead of a hardcoded developer path as the fallback
  default for pinned-binary environment variables in the benchmark harness
  and its tests.
