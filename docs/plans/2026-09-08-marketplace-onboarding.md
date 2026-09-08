# Marketplace onboarding implementation plan

**Goal:** A marketplace user can discover and activate automatic session switching and independently choose telemetry, without Node/npm.

**Architecture:** Extend the existing SessionStart hook with an optional launcher setup notice containing shell-quoted Python commands rooted in the installed plugin. Keep telemetry consent separate and explicit. The hook never installs launchers or enables telemetry by itself. Reuse the existing Python setup and supervisor. Preserve checkpoint injection.

**Stack:** Python standard library, native plugin hooks, existing pytest suite.

## Steps

1. Verify current Codex hook support before claiming a first-start experience on both clients.
2. Add failing tests for setup discovery, independent telemetry state, command quoting, suppressed repeat notices, and managed-session behavior. Implement a small onboarding helper and integrate SessionStart.
3. Repair advertised telemetry yes/no CLI responses and replace runtime npm-only guidance with direct Python commands; test explicit consent and DO_NOT_TRACK.
4. Document marketplace prerequisites, setup confirmation and restart, independent consent, and npm as an alternative channel. Update the bundled skill instructions.
5. Run scoped tests and isolated no-Node checks; review the final flow and record evidence in the project note.

User clarification: installation discovery belongs to the plugin. Add read-only `handoff_setup({})` to derive commands from the running MCP server's location, including when the notice was skipped. No user path, guessed cache path, or workspace checkout fallback. Reuse the same command renderer in the startup notice and MCP tool; prove relocated plugin commands without Node.

No global installation, paid model run, commit, push, or publication is part of this task. Existing dirty work is preserved. The design was approved in the conversation; no further design approval is needed.

## Verification

- Full suite before the final self-discovery tool and review fixes: 982 passed, 2 skipped (`SESSION_HANDOFF_BENCHMARK_SOCAT=/tmp/session-handoff-claude-deps-pYc8AnFs/extracted/usr/bin/socat python3 -m pytest -q tests`).
- Final affected suites: 251 passed, 1 skipped (`python3 -m pytest -q tests/test_onboarding.py tests/test_session_start_hook.py tests/test_handoff_mcp.py tests/test_user_prompt_submit_hook.py tests/test_telemetry_cli.py tests/test_session_switch.py`).
- Relocated plugin integration: hook and MCP return the same installation-derived commands; setup for both simulated clients, supervised launch, and explicit telemetry yes/no work with only Python and the simulated clients on PATH. No Node/npm/npx.
- Review regressions: symlinked state parents rejected; notice state private under umask 0; explicit home does not depend on Path.home; onboarding exceptions preserve checkpoint context. Reused existing secure directory traversal.
- Scoped Ruff and `git diff --check` pass. README, telemetry docs, and bundled skill explain independent consent and automatic path discovery.
- Native Codex 0.153.4 supports the required hook schemas, but the isolated unauthenticated startup probe did not reach lifecycle hook execution. Actual user-visible delivery in an authenticated marketplace-installed client remains unverified; no paid calls or personal installation changes were made. Evidence: `/tmp/session-handoff-codex-native-WH3I6a/`.
- Marketplace catalogs still point to v0.7.0. This implementation is local and unpublished.
