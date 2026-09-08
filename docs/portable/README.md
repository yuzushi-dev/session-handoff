# Archived portable manifest

`plugin.json` is retained as a reference template. Its paths assume placement
at the package root; it is not an active entry point or part of the runtime bundle.

Claude Code and Codex use their native manifests. Codex 0.153.4 gives a root
Agent Plugins manifest precedence over its native manifest and skips plugin hooks.
Do not restore this template to the root without rechecking native onboarding.

See [native onboarding verification](../2026-09-08-native-onboarding-check.md).
