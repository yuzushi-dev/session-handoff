# session-handoff telemetry

Telemetry is optional and off by default. This page is the complete public
notice for the current canary. It is a technical privacy contract, not legal
advice.

## What's collected

When enabled, the client records daily aggregates and one `active_day` marker.
It never uploads an individual operation or a partial current-day counter.

An operation aggregate contains only:

- schema version, UTC day, and plugin version;
- origin (`real` or `benchmark`);
- operation (`handoff` or `migrate`) and one closed client route;
- result, failure stage, duration bucket, and handoff-size bucket;
- dropped-event and normalized-field count buckets;
- aggregate count (`aggregate_count`, carried as the closed-schema `count` field).

Curated Grafana dashboards and any explicitly implemented export query apply an
aggregate threshold of 5 after summing rows: cells with `aggregate_count < 5`
are omitted from those results. Loki may retain the individual allowlisted rows
that sum to a cell, privately. There is no public query or export API beyond
private Grafana. A trusted operator with private Loki/Grafana access can query
raw allowlisted aggregate rows, including rows below 5; the threshold is not a
universal authorization policy for every private query. This threshold protects
small aggregate cells; it is not k-anonymity and does not count users. No stable
identifier exists and no unique-user denominator is computed; no stable identifier
is ever sent.

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
headers. There is no installation identifier and no pseudonymous user ID. Each
OTLP batch request also carries an `Idempotency-Key`, derived from that request
body for retry-safe batch deduplication; it is not a user or installation ID
and is not a telemetry attribute.

## Purpose and limits

The purpose is to measure aggregate usage/performance and mechanical continuity
outcomes, plus voluntary structured context-loss feedback. This is not user analytics,
profiling, a heartbeat, content inspection, or a population failure rate. It
is an opt-in sample with no unique-user denominator.

## Where it goes and how it is processed

Enabled clients send HTTPS OTLP logs to:

`https://telemetry.yuzushi.party/v1/logs`

The current processing path is:

`session-handoff client → Cloudflare Tunnel → OpenTelemetry Collector → Loki → Grafana`

The public write path uses the Cloudflare Tunnel `sando-telemetry` on the
`yuzushi.party` Free Website zone, with origin `http://gateway:4318`. A
read-only inventory found no account/zone Logpush job. Cloudflare is a global
edge/tunnel processor alongside the private self-hosted origin in Italy/UE.
The application payload does not contain IP addresses or stable identifiers.
Local nginx has `access_log off`, and cloudflared has no access-log sink or
persistent access-log volume in this repository. Cloudflare may nevertheless
process peer IP and edge metadata and produce aggregate analytics, including
Unique Visitors, under its own terms and retention. Cloudflare edge-data
retention is not verified here; owner acceptance is an operational acceptance
gate before release, and no Cloudflare retention period is asserted.
See [Zone Analytics](https://developers.cloudflare.com/analytics/account-and-zone-analytics/zone-analytics/)
and [Cloudflare analytics FAQ](https://developers.cloudflare.com/analytics/faq/about-analytics/).

The configured Collector processors for `session-handoff` are:

- `memory_limiter`;
- `filter/session-handoff`, a strict `service.name` filter for
  `service.name=session-handoff`;
- `filter/session-schema`, strict event and aggregate shape filters;
- Loki/Grafana post-aggregation queries, which omit `count`/`aggregate_count`
  cells below 5 without dropping rows needed to reach the threshold;
- `transform/session-handoff-allowlist`, the `session-handoff` attribute allowlist,
  which keeps only the fields listed above;
- a batch processor with a 5-second timeout and a 32-row maximum.

If the Collector returns an OTLP partial-success response, it reports only a
rejected-record count and not the rejected record identities. The client keeps
the whole batch queued and records the count locally; it acknowledges a batch
only when the response identifies full acceptance.

The Collector forwards the allowlisted stream to the Loki tenant
`session-handoff`. Grafana dashboards are private and read bounded aggregates;
they are not a public read endpoint. There is no query or export API beyond
private Grafana for public callers: OTLP is write-only ingest and Loki is
Docker-network-private. A trusted operator with private Loki/Grafana access
can query raw allowlisted rows, including rows below 5; the curated threshold
is not universal authorization for every private query.
The public ingest endpoint is currently a rate-limited canary. A proxy may
process a peer IP transiently for routing and rate limiting; it is not part of
the telemetry payload or an identifier.

## Retention and deletion

| Data | Retention | Control |
| --- | --- | --- |
| Local counters and queue | 30 days maximum, 256 rows maximum | `session-handoff telemetry disable --purge` immediately |
| Collector batch memory | Up to the configured 5-second batch timeout | Process expiry |
| Loki aggregate rows | 13 months (`11232h`) | Operator storage retention and purge |
| Backups | no backups exist for this self-hosted backend | No backup purge is applicable; any future backup requires a new review |
| Proxy access logs | access logs are disabled (`access_log off`) | Static config and disposable exercise |
| Cloudflare edge/tunnel metadata and analytics | owner acceptance recorded 2026-09-03; provider retention period not asserted | Owner acceptance and read-only tunnel inventory |

No contributor or installation identifier is stored, so an individual backend
row cannot be located or deleted. Disabling stops new collection. Purging
removes local counters, queue, summaries, and consent metadata; it cannot
remove rows already uploaded, which expire under the backend retention rule.

## Consent and controls

Telemetry stays off until an explicit answer. The local state is one of:

`unasked → asked → enabled`

`                 ↘ declined`

Each client has one surface. On Claude the SessionStart hook asks in chat. That
hook does not run on Codex, so the managed launcher asks there instead, on a
terminal, after the client exits. Both atomically record `asked` before showing
the notice. It is shown once; it never asks again after `asked`, `enabled`, or
`declined`, including after an upgrade or consent-version change. A decline is
final. Blank, interrupted, or unrecognized terminal input leaves telemetry off
and records `asked`, not `declined`.

In Claude's chat, only these complete strings are recognized, with exact
spelling and case:

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
npx session-handoff telemetry status
npx session-handoff telemetry enable
npx session-handoff telemetry preview
npx session-handoff telemetry flush
npx session-handoff telemetry report --category constraint --severity recoverable
npx session-handoff telemetry disable
npx session-handoff telemetry disable --purge
```

`preview` sends nothing. `DO_NOT_TRACK` is a runtime override: any non-empty
value other than exactly `0` suppresses collection, upload, and consent
handling without rewriting the user's configuration. `disable` revokes an
enabled choice and stops future collection.

## Open release checks

The local root privacy/backend review is complete with fresh tests. Owner
acceptance of Cloudflare edge-data/retention and the documented backup/retention
policy was recorded on 2026-09-03; no provider retention period is asserted.
The seven-day canary remains open. Human benchmark review and remote CI still
require owner evidence before treating the endpoint as release-ready.

- Data controller / project owner: `privacy@yuzushi.party`
- Technical processor/operator: `privacy@yuzushi.party`
- Origin hosting and region: private infrastructure, self-hosted in Italy/UE
- Public edge/tunnel processor: Cloudflare global network (Free Website plan)
