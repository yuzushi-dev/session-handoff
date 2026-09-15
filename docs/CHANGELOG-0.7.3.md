# Changelog

Older history through the last published release (0.7.2) is archived at
[`docs/CHANGELOG-0.7.2.md`](CHANGELOG-0.7.2.md).

## Unreleased

- Install launcher setup automatically for the detected client on the first
  session, instead of only showing a manual command; the other client and any
  undetected client still get the manual command in the same notice. Gated so
  installs that already ran setup, or already saw the pre-upgrade notice, are
  never silently changed. Falls back to the manual notice if the detected
  client isn't actually runnable, or if both clients' session env vars are
  set at once.
- Serialize `setup`/`uninstall` with a per-home lock, so an automatic and a
  manual install started close together can no longer race and silently drop
  one client's registration.

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
