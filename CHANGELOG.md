# Changelog

Older history through the last published release (0.7.2) is archived at
[`docs/CHANGELOG-0.7.2.md`](docs/CHANGELOG-0.7.2.md).

## 0.7.3 — 2026-09-14

- Store handoffs centrally with reference-first retrieval and legacy compatibility.
- Use native Claude/Codex plugin manifests; archive the portable manifest outside
  the package root so Codex loads hooks and starts MCP correctly.
- Show launcher setup and separate telemetry consent notices during onboarding.
- Advertise Claude-compatible MCP tool schemas while enforcing mutually
  exclusive handoff arguments in the server.
- Let Claude auto-discover the standard hooks file without registering it twice.
- Attribute telemetry to the actual client, record fallback and failed migration
  outcomes, and preserve client identity across managed launches and switches.
- Reject benchmark runs when repository provenance cannot be verified.
- Clarify that pilot raw evidence is local and unpublished, preserving reported
  results and their limitations. Align the CI Codex smoke test to 0.153.4.
