# Telemetry verification — 2026-09-08

The existing installation collects and delivers telemetry. The measurement bugs
identified below have been corrected locally by Luna (max reasoning). The Grafana
correction was deployed with user approval on 2026-09-08. Receipt alone does not
establish release readiness.

## Approved dashboard deployment — 2026-09-08

- User approved applying the reviewed dashboard patch. Applied it to
  `/home/cristina/selfhosted/telemetry`, including its regression test.
- `git apply` replaced the host file, while Docker's single-file bind mount
  retained the old inode. Restarted only `telemetry-grafana-1`, preserving its
  existing configuration; the mounted file then matched the candidate SHA256
  `0934cac7deb772fd9160977cd2bcae48f7dfc581485ae80de93cf28d177f0a24`.
- Verified the provisioned dashboard in Grafana's database with a read-only
  query: panel titles, descriptions and queries match the candidate. Grafana
  health reports database `ok`. All seven live LogQL queries pass with a
  seven-day range.
- Backend suite: `27 passed, 4 skipped` for `tests/test_deploy.py` and
  `tests/test_disposable_privacy.py`; scoped diff check passed.
- Pre-deployment dashboard and test copies:
  `/tmp/telemetry-dashboard-predeploy-av9wg3h7`.
- Only the dashboard deployment was approved and performed. Local plugin fixes
  have not been installed personally or published. The unrelated full-suite
  provenance issue below remains open.

## Local corrections and validation

- Manual handoff attribution now uses a closed set of known MCP client identities
  or an explicit Claude/Codex environment value. Both native manifests and new
  managed setup registrations provide that value. Unknown/invalid identities are
  not guessed. The portable manifest remains client-neutral.
- Existing managed registrations are preserved on reinstall. The refreshed
  supervisor injects the per-child identity on launch/relaunch/migration/rollback;
  Codex also gets an explicit MCP environment override. The regression suite
  covers env-less registrations and the Claude-to-Codex identity transition.
- Persisted handoffs without automatic switching record `fallback/control` for
  both central and legacy storage. Unsupervised migration requests record
  `failure/control`. Successful requests handed to the supervisor do not also
  emit a terminal MCP outcome.
- The upload schema and consent are unchanged. Manual reading remains deliberately
  uninstrumented as a resume-success signal: it cannot prove continuation.
- The [backend patch](patches/2026-09-08-telemetry-dashboard.patch) selects real
  usage only, excludes successes from the failure panel, and replaces all-row
  adoption counts with active environment-days by version. It includes a
  regression test; [application instructions](patches/README.md) record the
  approved deployment and retain the original patch for review.
- Affected client tests after the upgrade-path correction: **461 passed** with
  `rtk pytest -q tests/test_handoff_mcp.py tests/test_package.py tests/test_setup.py tests/test_session_switch.py tests/test_session_migrate_supervisor.py tests/test_telemetry.py tests/test_telemetry_privacy.py`.
  Coverage includes real local counters with networking suppressed, identity
  reset/precedence, fallback, and no duplicate supervised outcome.
- Independent Luna max review identified the managed-upgrade gap; the follow-up
  review confirms that runtime injection resolves it without rewriting existing
  user registrations. No material review findings remain.
- Scoped Ruff and `git diff --check` passed. The isolated dashboard regression
  passed. All seven candidate queries executed successfully against live Loki
  read-only with a seven-day range. Native Claude 2.1.263 and Codex 0.153.4 help
  confirms support for the setup registration's `--env` option.
- Full suite: **989 passed, 9 failed, 3 skipped** in 241.71 seconds. The timing
  test `test_persistent_client_closes_child_after_leader_exits` passed on isolated
  rerun (0.54 seconds). Eight parameterizations of
  `test_fake_pilot_executes_isolated_condition_and_writes_blinded_artifacts`
  fail because `repository_sha256` is `None`: the existing repository hash
  implementation reads every tracked file, and `plugin.json` is tracked but
  absent following the earlier approved packaging move. That removal predates
  this telemetry task. No pre-task full-suite run was made; the full-suite gate
  remains unresolved. Do not restore the incompatible root manifest to mask it.
  Full failure log: `/home/cristina/.local/share/rtk/tee/1788888194_pytest.log`.
  This full run preceded the final supervisor injection change; only the affected
  suite was rerun afterward.
- Before deployment, the live dashboard was SHA256
  `be5a627fc1657522fd6c450db9bdac5e8d4be0eeab049139c69ebcacc22e389f`;
  candidate SHA256 is
  `0934cac7deb772fd9160977cd2bcae48f7dfc581485ae80de93cf28d177f0a24`.
  At that verification stage no deployment, personal installation, consent
  change, production POST, push, publication, or new handoff was performed.
  The subsequently approved dashboard deployment is recorded above.

## Original audit evidence (before corrections)

- Local CLI: consent enabled; `DO_NOT_TRACK` false; no recorded flush error.
- Collector, gateway, tunnel, Loki and Grafana containers running; actual mounts
  use `/home/cristina/selfhosted/telemetry`.
- Loki query succeeded. Version 0.7.2/schema 2 rows: September 6 handoffs
  23 success/3 failure; September 7 handoffs 2 success/1 failure; active-day
  markers September 6–8. These are recorded counts, not independently validated
  human usage. Latest returned timestamp: September 8, 15:51:21 UTC.
- Today's local counter contains one successful Codex-to-Codex handoff,
  size 4–16 KiB, duration >=120 seconds. Operation aggregates upload only after
  their UTC day closes, when another flush runs; there is no midnight daemon.
- Public read-only GET with the actual telemetry User-Agent returns 405,
  supported POST. Default urllib User-Agent returns Cloudflare 403/1010.
  This verifies routing, not a new POST acceptance. No test payload was sent.
- Loki `/ready` initially returned 503, then 200; queries succeeded. Cause of
  transient readiness failure was not established.
- Client tests: `428 passed, 1 skipped` across `test_telemetry.py`,
  `test_telemetry_cli.py`, `test_telemetry_privacy.py`.
- Backend tests: `26 passed, 4 skipped` across `test_deploy.py` and
  `test_disposable_privacy.py`; opt-in disposable integration was not run.

## Data and interpretation

Uploaded: UTC day, schema/plugin version, real/benchmark origin, active-day
marker; daily counts by handoff/migration, client route, success/failure/fallback,
failure stage, duration/size buckets, dropped-event/normalized-field buckets.
Explicit structured feedback adds category and recoverable/blocked severity.
Redaction counts remain local. No transcript, handoff content, paths, model,
tokens, cost, session/user/install identifiers are uploaded. Cloudflare sees
network metadata; payload exclusions do not imply that the edge sees no IP.

This measures mechanical outcomes, not semantic information preservation.
Active-day markers do not establish unique users or installations.
Curated panels hide aggregate cells below five, so an empty panel need not mean
failed collection. Local queue retention is 30 days/256 rows; configured Loki
retention is 11232 hours. Production expiration was not exercised.

## Original findings and follow-up

1. `server/handoff_mcp.py` hardcodes Codex-to-Codex for manual handoff outcomes;
   `record_terminal_outcome` preserves those values. Correct source attribution
   before interpreting marketplace client comparisons.
2. Central create with `auto_switch=true` and no launcher writes the handoff but
   records no terminal outcome. Unsupervised migration attempts and manual
   read/resume also lack coverage. Define and record accurate fallback outcomes;
   a successful file creation must not imply verified resume success.
3. Mounted Grafana dashboard queries use `origin!="canary"`, admitting benchmark
   and historical rows without origin. Separate real usage from benchmark data.
   “Release adoption” counts all matching log rows, not installations; rename
   or redefine it without claiming unique-user measurement.
4. After fixes, validate the candidate in an isolated client-to-storage exercise.
   Existing receipt and passing unit tests do not prove every candidate flow.
   The seven-day canary and release evidence remain unverified in this audit.

The original audit made no runtime/configuration changes, consent changes,
publication, or new handoff. The subsequent local corrections are recorded above.
Resume reference:
`handoff://c3e800a9-18d5-4394-abfb-43ac1a217f81/b71bbd9e-7d91-4a0b-926d-44f50b74fd1f`.
