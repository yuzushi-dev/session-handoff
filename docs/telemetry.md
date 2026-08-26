# session-handoff telemetry

Opt-in, off by default. Nothing is sent unless you run
`session-handoff telemetry enable` and answer `yes` at the interactive
prompt.

## What's collected

After each handoff/migration reaches a terminal state, and once a day of
counts is closed, one bucketed row per event type:

- **operation_summary**: operation (`handoff`/`migrate`), source/target
  client, result (`success`/`failure`/`fallback`), failure stage, duration
  bucket, handoff-size bucket, redaction/dropped-event/normalized-field
  counts — all bucketed (`zero`, `one`, `2_to_5`, `6_to_20`, `gt_20`; byte
  ranges like `16_to_64k`).
- **context_feedback**: an optional, voluntary category (`constraint`,
  `decision`, `path`, `progress`, `rejected_attempt`, `other`) and severity
  (`recoverable`/`blocked`) you report yourself with
  `session-handoff telemetry report`. `other` never accepts free text.

## What's never collected

Transcript, prompt, handoff text, tool trace, command, diff, file path,
session ID, installation ID, device ID, account ID, hostname, username, IP
address, user agent, locale, repository name, model name, arbitrary
metadata, exception text, stack trace, or any other free-form field.
Credentials, tokens, cookies, and authorization headers from the host
process are never read for telemetry.

## Where it goes

`https://telemetry.yuzushi.party/v1/logs` — a shared backend (OpenTelemetry
Collector → Loki → Grafana) also used by the Sando plugin, each with its
own closed schema. Full data inventory, processor list, and retention
table: `~/selfhosted/telemetry/docs/telemetry-privacy.md` (separate infra
repo, shared with Sando). Current release status (canary, independent
privacy review — both open as of writing):
`~/selfhosted/telemetry/docs/telemetry-canary-report.md`.

Retention: local counters/queue 7 days; uploaded aggregate rows 13 months.
The service stores no identifier that can answer "which user sent this
event" — a contributor's rows can't be individually deleted.

## Controlling it

```sh
session-handoff telemetry status
session-handoff telemetry enable    # interactive only, asks yes/no
session-handoff telemetry preview   # shows the exact next upload body, sends nothing
session-handoff telemetry flush
session-handoff telemetry report --category constraint --severity recoverable
session-handoff telemetry disable --purge
```

This is an opt-in sample, not a population failure rate — enabled users may
not be representative of everyone running session-handoff.
