# Changelog

Hotfix on top of [0.7.4-jev](CHANGELOG-0.7.4-jev.md), same parallel-prerelease
track: 0.7.4 stays the stable "Latest" release, unaffected by any of this.

## 0.7.4-jev.1 — 2026-09-19

- Fix: the checkpoint tool-summary scorer and the compact-advisor Stop hook
  are declared in the same `hooks/hooks.json` that both Claude Code and
  Codex load, and Codex genuinely fires the same `PreCompact`/`Stop`
  events — but both features hardcoded Claude's transcript parser, so under
  Codex they always failed to parse the transcript and silently fell back
  to their no-op path (fail-open, so nothing broke — it just never worked
  for Codex sessions). Checked this machine's real, current Codex sessions:
  all of them use Codex's "paginated" (SQLite-backed) history format, not
  the older "legacy" one.
- Adds automatic transcript-format detection (Claude vs. Codex) and, for
  Codex's paginated history specifically, reuses this project's own
  existing, already-relied-upon SQLite projection (the same one
  `session-handoff migrate` uses) to read it. No new parser was written;
  both formats already produced the same normalized event shape internally.
- Known, accepted limitation: Codex's tool-result events don't carry an
  `is_error` flag the way Claude's do, so the scorer's "never drop an
  error" guarantee can't specifically trigger for Codex tool errors — they
  get ordinary scoring instead. Not fixable without Codex's own transcript
  format recording that signal.
