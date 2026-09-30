# Live compatibility results — 2026-09-30

This report separates client format, package build, provider backend, and proof level. A result from a superseded package or different provider cohort does not certify final C.

## Artifact under test

- Package: `session-handoff 0.7.4-jev.3`
- Final-C packaged-content SHA-256: `05268602f4f5228b684d726470025562ce9187cb873176e6310dcaf79d518d9f`
- Final-C tarball SHA-256: `9cd6cefc7574c0fd4e43bdaa5d28752f541355f3002aea2529817aa0ec2715b0`
- Platform: Linux `6.14.0-37-generic`, x86_64
- Current clients: Codex `0.159.2`, Claude Code `2.1.285`

The extracted package and the isolated Codex installation had the same packaged-content hash. The packaged MCP server initialized as `session-handoff 0.7.4-jev.3`.

## Final-C live matrix

| Capability | Client and provider cohort | Status | Observed evidence and limit |
| --- | --- | --- | --- |
| Package load | Codex `0.159.2`; Claude Code `2.1.285` with the `CODEX` provider profile | `passed` | Codex installed final C from an isolated local marketplace. Claude loaded final C and exposed its MCP tools. This row does not imply every tool call succeeded. |
| Semantic handoff | Codex `0.159.2`, `gpt-5.6-luna`, low reasoning | `passed` | A central handoff was created and read. Native resume on the same thread returned the exact goal marker and identical handoff reference. |
| Semantic handoff | Claude Code `2.1.285`, Anthropic Sonnet | `passed` | After the quota reset, final C created and read a central handoff with `claude-sonnet-5-5`. Native resume on session `99999999-9999-4999-8999-999999999998` returned the exact marker and identical handoff reference. |
| Semantic handoff | Claude Code `2.1.285`, `CODEX` provider profile, `gpt-5.6-luna` | `failed` | The provider emitted `path: ""` together with a valid `name`, plus other optional defaults. The server correctly rejected the ambiguous request with `exactly one of path or name must be provided`. A second prompt explicitly forbidding those fields produced the same raw arguments. No record was created. |
| Claude to Codex migration | Claude Code `2.1.285` Sonnet source to Codex `0.159.2` Luna destination | `passed` | Final C converted 11 records, preserved the source hash, declared every dropped event kind, and Codex resumed the generated target with the exact source goal marker. |
| Codex to Claude migration | Codex `0.159.2` Luna source to Claude Code `2.1.285` with the `CODEX` provider profile | `passed` | Final C converted 14 records, preserved the source hash, declared five dropped reasoning events, and Claude Code resumed the generated target with the exact source goal marker. This proves Claude client-format acceptance with the Codex backend; it is not an Anthropic inference result. |
| Codex to Claude migration | Codex `0.159.2` Luna source to Claude Code `2.1.285`, Anthropic Sonnet destination | `passed` | Final C converted 14 records, preserved the source SHA-256, declared five dropped reasoning events, and native Claude resumed target `88888888-8888-4888-8888-888888888888` with `claude-sonnet-5-5`, returning the exact source marker. |
| Manual compaction | Codex `0.159.2` Luna low; Claude `2.1.285` native Sonnet | `passed` | Both clients produced native compaction boundaries, checkpoints and one nonempty compact-session injection. Corrected terminal drivers submitted commands separately from typing. See [Codex events](compatibility-codex-compaction.md) and [Claude events](compatibility-claude-compaction.md). |
| Automatic compaction | Codex `0.159.2` Luna low; Claude `2.1.285` native Sonnet | `passed` | Both clients completed the native checkpoint/reinjection cycle with explicit laboratory thresholds: Codex 16k context/10k compact limit, Claude 100k. Claude completed on same-session resume after the initial stream process exited. |

The earlier dated Anthropic retry receipt retains **17 passed, 2 not-run, and 1 failed**, reflecting the tests available at that stage and the separate `claude-codex-profile-handoff` failure. It is unchanged.

The completed current native-provider cohort has a separate receipt at `.orca/compat-final-native/compatibility-report.json`: **16 passed, 0 not-run, 0 failed** (eight fixtures, seven required native cases and one combined Sando/session-handoff case). It covers this Linux build, Codex/Luna and Claude/native Anthropic Sonnet. It excludes the failed Codex-backed Claude cohort and incomplete historical-version matrix. See [consolidated results](compatibility-results.md).

## Historical client comparison

| Client | Provider/model | Result | Scope |
| --- | --- | --- | --- |
| Codex `0.153.4` | `gpt-5.6-luna`, low reasoning | Native start and same-thread resume passed | Client comparison only; it does not certify final-C plugin flows on this client. |
| Claude Code `2.1.263` | `CODEX` provider profile, `gpt-5.6-luna` | Native start and same-session resume passed | Claude client comparison with Codex backend; no Anthropic inference claim. |

## Preserved failed attempts

- Initial Claude probes used `--bare`. Claude Code documents that mode as skipping account credential reads, so those failures were laboratory invocation errors and are not authentication failures.
- Codex rejected `gpt-5.4-mini` for the ChatGPT account. Successful Codex probes used the available small `gpt-5.6-luna` model.
- A first isolated store used group-writable directories. The server rejected it; retries used private `0700` directories.
- Claude `--strict-mcp-config` hid plugin MCP servers. Removing that flag exposed the plugin tools.
- The first Claude tool allowlist used the wrong prefix. The observed native name is `mcp__plugin_session-handoff_session-handoff__...`.
- The final-C `CODEX` profile handoff failure is not a cc-fleet configuration rewrite: the proxy forwards the tool schema and raw model arguments. Server validation remains strict.
- The first post-reset Anthropic call proved provider access but used an incomplete handoff document and was rejected for missing canonical sections. One corrected payload passed; the invalid invocation is retained separately and is not a compatibility failure.

## Evidence boundaries

All native sessions used synthetic repositories and isolated HOME/XDG trees. Credential files were copied with mode `0600`, never printed, and are removed from the retained lab at cleanup. Reports contain sanitized summaries and hashes, not raw private transcripts.

Local evidence is under `.orca/compat-lab/results/`; it is excluded from Git. The post-reset final-C receipt is `.orca/compat-lab/results/final-C-anthropic-retry-20260930T110120Z/compatibility-report.json`; the earlier final-C receipt remains unchanged at `.orca/compat-lab/results/final-C/compatibility-report.json`. Candidate A and the `CODEX` provider cohort remain separate from native final-C Anthropic proof.

The Sando composition component probe is documented separately in [compatibility-pilot.md](compatibility-pilot.md). macOS was not tested.
