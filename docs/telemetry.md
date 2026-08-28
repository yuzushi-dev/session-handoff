# session-handoff telemetry

Opt-in, off by default. Nothing is sent unless you answer `yes` at the
interactive consent prompt -- shown once, either during `session-handoff
setup` (if run interactively and no choice was recorded yet), at an
interactive plugin session start, or later via `session-handoff telemetry
enable`.

## What's collected

The backend receives only daily aggregates and one `active_day` marker. It
never receives event sequences or partial counters for the current UTC day.

Operation aggregates contain the operation (`handoff`/`migrate`), one closed
`client_route` (`claude_to_claude`, `claude_to_codex`, `codex_to_claude`, or
`codex_to_codex`), result (`success`/`failure`/`fallback`), closed failure
stage, duration bucket, handoff-size bucket, dropped-event bucket, and
normalized-field bucket. Duration is `not_measured` when the operation did
not actually measure elapsed time; it is not reported as a sub-second
operation.
Failure stage is a closed value: `none`, `validation`, `control`,
`source_stop`, `conversion`, `target_resume`, `source_resume`, or `unknown`;
the normal paths currently emit `none`, `validation`, `control`,
`conversion`, `target_resume`, and `source_resume`.

Context-feedback aggregates contain only one closed category
(`constraint`, `decision`, `path`, `progress`, or `rejected_attempt`) and
severity (`recoverable`/`blocked`) reported with
`session-handoff telemetry report`. No free-text or `other` category is
accepted. Redaction counts are local-only diagnostics and are not sent.

The local queue can contain rows written by older plugin versions. On read,
the client converts old source/target fields to `client_route`, removes the
old redaction field, and strips obsolete feedback dimensions. Legacy
`other` feedback rows are discarded; they never make the queue fail or reach
the backend.

## What's never collected

Transcript, prompt, handoff text, tool trace, command, diff, file path,
session ID, installation ID, device ID, account ID, hostname, username, IP
address, user agent, locale, repository name, model name, arbitrary
metadata, exception text, stack trace, or any other free-form field.
Credentials, tokens, cookies, and authorization headers from the host
process are never read for telemetry.

## Where it goes

`https://telemetry.yuzushi.party/v1/logs` is a shared backend (OpenTelemetry
Collector → Loki → Grafana) also used by the Sando plugin, each with its
own closed schema. Full data inventory, processor list, and retention
table: `~/selfhosted/telemetry/docs/telemetry-privacy.md` (separate infra
repo, shared with Sando). Current release status is a canary with an
independent privacy review. Both remain open as of this writing:
`~/selfhosted/telemetry/docs/telemetry-canary-report.md`.

Retention: local counters/queue 30 days; uploaded aggregate rows 13 months.
The service stores no identifier that can answer "which user sent this
event". A contributor's rows cannot be individually deleted.

## Controlling it

`session-handoff setup` asks the same consent prompt once, right after
installation finishes, if run interactively (a real TTY) and no choice has
been recorded yet; reruns and upgrades never re-ask. Non-interactive setup
runs (CI, `--yes`, piped) never prompt and telemetry stays off. For
marketplace installs, the plugin shows a non-blocking reminder at session
start until a choice is recorded. On an interactive session start, when stdin
and stdout are TTYs, the hook asks the same one-time question; it keeps the
hook protocol on stdout and shows the question on stderr. Without TTYs it
does not ask or write a config and leaves the reminder for a later session.
The hook fails open on errors.

`DO_NOT_TRACK` is a runtime override. When set to any non-empty value other
than exactly `0`, telemetry is disabled: no consent prompt, collection, or
upload occurs, even if the config says `enabled: true`. The config is not
rewritten, so removing the variable restores the previously recorded opt-in.

```sh
session-handoff telemetry status
session-handoff telemetry enable    # interactive only, asks yes/no
session-handoff telemetry preview   # shows the exact next upload body, sends nothing
session-handoff telemetry flush
session-handoff telemetry report --category constraint --severity recoverable
session-handoff telemetry disable --purge
```

To disable telemetry persistently, run `session-handoff telemetry disable`.
Add `--purge` to remove queued local telemetry. To disable it for a process or
CI job without changing the config, set `DO_NOT_TRACK=1`.

This is an opt-in sample, not a population failure rate. Enabled users may
not represent everyone running session-handoff.
