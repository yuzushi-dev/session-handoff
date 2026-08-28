# session-handoff telemetry

Telemetry is optional and off by default. This page is the complete public
notice for the current canary. It is a technical privacy contract, not legal
advice.

## What's collected

When enabled, the client records daily aggregates and one `active_day` marker.
It never uploads an individual operation or a partial current-day counter.

An operation aggregate contains only:

- schema version, UTC day, and plugin version;
- operation (`handoff` or `migrate`) and one closed client route;
- result, failure stage, duration bucket, and handoff-size bucket;
- dropped-event and normalized-field count buckets;
- aggregate count.

Structured context feedback contains one closed category
(`constraint`, `decision`, `path`, `progress`, or `rejected_attempt`) and one
severity (`recoverable` or `blocked`). It is recorded only through the
explicit `telemetry report` command. Redaction counts stay local.

The local configuration and state contain the consent state, consent timestamp,
fixed endpoint, aggregate counters, queue, and last-operation summary. The
consent timestamp is never uploaded.

## What's never collected

Transcript, prompt, handoff text, tool trace, command, diff, file path, session
ID, installation ID, device ID, account ID, hostname, username, IP address,
user agent, locale, repository name, model name, arbitrary metadata, exception
text, stack trace, free text, credentials, tokens, cookies, or authorization
headers. There is no installation identifier and no pseudonymous user ID.

## Purpose and limits

The purpose is to measure aggregate mechanical continuity outcomes and
voluntary structured context-loss feedback. This is not user analytics,
profiling, a heartbeat, content inspection, or a population failure rate. It
is an opt-in sample with no unique-user denominator.

## Where it goes and how it is processed

Enabled clients send HTTPS OTLP logs to:

`https://telemetry.yuzushi.party/v1/logs`

The current processing path is:

`session-handoff client → OpenTelemetry Collector → Loki → Grafana`

The configured Collector processors for `session-handoff` are:

- `memory_limiter`;
- `filter/session-handoff`, a strict `service.name` filter for
  `service.name=session-handoff`;
- `filter/session-schema`, strict event and aggregate shape filters;
- `transform/session-handoff-allowlist`, the `session-handoff` attribute allowlist,
  which keeps only the fields listed above;
- a batch processor with a 5-second timeout and a 32-row maximum.

The Collector forwards the allowlisted stream to the Loki tenant
`session-handoff`. Grafana dashboards are private and read bounded aggregates;
they are not a public read endpoint. The public ingest endpoint is currently a
rate-limited canary. A proxy may process a peer IP transiently for routing and
rate limiting; it is not part of the telemetry payload or an identifier.

## Retention and deletion

| Data | Retention | Control |
| --- | --- | --- |
| Local counters and queue | 30 days maximum, 256 rows maximum | `session-handoff telemetry disable --purge` immediately |
| Collector batch memory | Up to the configured 5-second batch timeout | Process expiry |
| Loki aggregate rows | 13 months (`11232h`) | Operator storage retention and purge |
| Backups | Not specified or verified by this repository | Must be defined and tested by the operator |
| Proxy access logs | No retention contract is asserted here | Must be verified for the deployed proxy |

No contributor or installation identifier is stored, so an individual backend
row cannot be located or deleted. Disabling stops new collection. Purging
removes local counters, queue, summaries, and consent metadata; it cannot
remove rows already uploaded, which expire under the backend retention rule.

## Consent and controls

Telemetry stays off until an explicit answer. The local state is one of:

`unasked → asked → enabled`

`                 ↘ declined`

The SessionStart hook atomically records `asked` before showing the notice.
It shows the notice once; it never asks again after `asked`, `enabled`, or
`declined`, including after an upgrade or consent-version change. A decline is
final. Blank, interrupted, or unrecognized terminal input leaves telemetry off
and records `asked`, not `declined`.

In chat, only these complete strings are recognized, with exact spelling and
case:

```text
session-handoff telemetry yes
session-handoff telemetry no
```

They resolve a pending choice locally. No natural-language interpretation or
partial matching occurs. The terminal prompt accepts `y`, `yes`, `n`, and
`no`, case-insensitively; other input is ambiguous and leaves the state at
`asked`.

The controls are:

```sh
session-handoff telemetry status
session-handoff telemetry enable
session-handoff telemetry preview
session-handoff telemetry flush
session-handoff telemetry report --category constraint --severity recoverable
session-handoff telemetry disable
session-handoff telemetry disable --purge
```

`preview` sends nothing. `DO_NOT_TRACK` is a runtime override: any non-empty
value other than exactly `0` suppresses collection, upload, and consent
handling without rewriting the user's configuration. `disable` revokes an
enabled choice and stops future collection.

## Open release checks

The canary remains open. An independent privacy review remains open. Backend
retention and purge, backup purge, packet-to-storage equivalence, hosting
details, rate-limit abuse controls, and owner/processor contacts have not been
verified by this repository and must be checked before treating the endpoint
as release-ready.

- Data controller / project owner: `[owner contact to be supplied by project owner]`
- Technical processor/operator: `[processor contact to be supplied by project owner]`
- Hosting provider and region: `[hosting details to be supplied by project owner]`
