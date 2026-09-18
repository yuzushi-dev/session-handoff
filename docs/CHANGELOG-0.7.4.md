# Changelog

Older history through the last published release (0.7.3) is archived at
[`docs/CHANGELOG-0.7.3.md`](CHANGELOG-0.7.3.md).

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
