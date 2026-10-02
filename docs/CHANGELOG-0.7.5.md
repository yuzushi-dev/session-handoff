# 0.7.5 — 2026-10-02

Stable release consolidating the 0.7.4-jev prerelease line and adding the
Codex thread-status integration.

- Add the read-only `codex_thread_status` MCP tool, bound to the `threadId`
  supplied on each Codex request. Its optional app-server adapter reads status
  only, with no transcript turns or global token events.
- Bundle an MCP Apps UI resource while keeping the textual result useful in
  Codex CLI. Token counts remain explicitly unavailable where Codex exposes no
  thread-scoped snapshot.
- Keep supervised session migration unchanged; native history injection stays
  out until fidelity, retry, and recovery behavior can be verified.
- Include the Claude Code mod layer and lifecycle reliability improvements
  introduced during the 0.7.4-jev prerelease line.
