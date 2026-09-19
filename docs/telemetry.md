# session-handoff telemetry

Telemetry is optional and off by default. This page is the complete public
notice for the current canary. It is a technical privacy contract, not legal
advice.

## What's collected

With consent version 1, 2, or 3, the client records anonymous daily aggregates
and one `active_day` marker. It never uploads an individual operation or a
partial current-day counter. Explicit consent version 2 additionally enables schema3 pseudonymous lifecycle records; explicit consent version 3 additionally enables schema4 installation registry status records.

An operation aggregate contains only:

- schema version, UTC day, and plugin version;
- origin (`real` or `benchmark`);
- operation (`handoff` or `migrate`) and one closed client route;
- result, failure stage, duration bucket, and handoff-size bucket;
- dropped-event and normalized-field count buckets;
- aggregate count (`aggregate_count`, carried as the closed-schema `count` field).

Manual handoff routes use only the fixed Claude Code and Codex MCP identities or
the explicit `SESSION_HANDOFF_CLIENT` manifest value. An unknown identity is not
attributed or recorded. A `fallback` handoff means the document was persisted
but automatic switching was unavailable; it does not verify that a later session
resumed. `handoff_read` has no terminal success event because reading a document
cannot prove that a session used it successfully. Managed launchers set the
closed client marker for every child, including relaunch and migration targets;
they leave existing user MCP registrations untouched. Unmanaged connections
use only the known initialize names and remain un-attributed when the name is
unknown.

Curated Grafana dashboards and any explicitly implemented export query apply an
aggregate threshold of 5 after summing rows: cells with `aggregate_count < 5`
are omitted from those results. Loki may retain the individual allowlisted rows
that sum to a cell, privately. There is no public query or export API beyond
private Grafana. A trusted operator with private Loki/Grafana access can query
raw allowlisted aggregate rows, including rows below 5; the threshold is not a
universal authorization policy for every private query. This threshold protects
small aggregate cells; it is not k-anonymity and does not count people.
Lifecycle panels apply the same threshold after counting distinct installation
IDs, not submitted rows. No unique-user denominator is computed. Operation,
activity and feedback rows never contain a stable identifier.

Structured context feedback contains one closed category
(`constraint`, `decision`, `path`, `progress`, or `rejected_attempt`) and one
severity (`recoverable` or `blocked`). It is recorded only through the
explicit `telemetry report` command. Redaction counts stay local.

### Installation registrations and managed uninstalls (consent v2)

After new explicit consent and successful setup, the client generates a random
installation ID for that local home. Codex and Claude configured in the same
home share it. Reconciliation, upgrades and adding the second client retain it.
It is not derived from a username, hostname, device, account, repository or
session, and it is never attached to operation, activity or feedback records.

Each lifecycle row has exactly seven fields:

- `schema_version`: integer `3`;
- `event`: `installation_lifecycle`;
- `day_utc`: event day in UTC;
- `plugin_version`: installed package version;
- `origin`: `real` or `benchmark`;
- `installation_id`: 32 cryptographically random lowercase hexadecimal characters;
- `lifecycle_action`: `registered` or `uninstalled`.

Its OTLP body is `session_handoff.installation_lifecycle`. The identifier links
lifecycle records with each other: these records are pseudonymous, not anonymous.
Loki stores the ID as structured metadata, never as an indexed stream label.
Private operators can inspect it; dashboard queries return distinct counts only.

An already configured installation registers when its user grants v2 consent;
the event day is the enrollment day, not a recovered historical installation
date. Only successful `session-handoff uninstall` emits a removal. Failed,
cancelled and no-op removals do not. Delivery is bounded and best effort:
offline removal, package-manager or marketplace removal, and manual deletion
may never be observed. Inactivity is never treated as an uninstall.

Successful managed uninstall ends that identity. Reinstall or local identity
purge may create a new one; cloning a home can copy an ID. These metrics describe
observed consenting configured local profiles, not people, downloads, all
installations, or a current installed population. The public write endpoint
does not independently attest the installation. Counts cover events received
within the selected time window and backend retention, not permanent all-time
totals. Version/route operation metrics cannot be joined to individual IDs.

The local configuration and state contain the consent state, consent timestamp,
fixed endpoint, aggregate counters, queue, and last-operation summary. With v2
consent, local setup state also retains the random lifecycle ID and registration
marker. The consent timestamp is never uploaded.

## What's never collected

Transcript, prompt, handoff text, tool trace, command, diff, file path, session
ID, device ID, account ID, hostname, username, IP address,
user agent, locale, repository name, model name, arbitrary metadata, exception
text, stack trace, free text, credentials, tokens, cookies, or authorization
headers. The only installation-ID exception is the explicitly consented
lifecycle schema above. Each OTLP batch request also carries an
`Idempotency-Key` derived from that request body; it is not an installation ID
or a telemetry attribute. The current backend does not enforce request-level
deduplication, so aggregate retransmissions can repeat. Lifecycle panels count
distinct IDs per action and receipt window, avoiding retransmission inflation.

## Purpose and limits

The purpose is to measure aggregate usage/performance and mechanical continuity
outcomes, voluntary structured context-loss feedback, and observed installation
registrations and managed removals. This is not user profiling or an autonomous daemon; v3 observations occur only at setup/reconciliation and SessionStart, not a heartbeat. It is not content inspection, or a population failure rate. It
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
The application payload contains no IP addresses. Only the new-consent
lifecycle records contain a stable random installation ID.
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
| Local lifecycle identity | Until successful managed uninstall or local purge | No automatic remote deletion |
| Collector batch memory | Up to the configured 5-second batch timeout | Process expiry |
| Loki schema2 aggregates and schema3/schema4 event rows | 13 months (`11232h`) | Operator storage retention and purge |
| Installation registry state | While the service operates; minimal first/last observation, last version, and removal state | Separate operator registry policy and purge |
| Backups | no backups exist for this self-hosted backend | No backup purge is applicable; any future backup requires a new review |
| Proxy access logs | access logs are disabled (`access_log off`) | Static config and disposable exercise |
| Cloudflare edge/tunnel metadata and analytics | owner acceptance recorded 2026-09-03; provider retention period not asserted | Owner acceptance and read-only tunnel inventory |

Disabling stops new collection and upload. It does not emit uninstall. Purging removes local counters,
queue, summaries, lifecycle identity and consent metadata. Anonymous aggregates
cannot be attributed to a contributor; lifecycle rows can be located privately
by their random ID. There is no automated client-side remote deletion endpoint.
Local purge cannot remove rows already uploaded. Raw schema2/schema3/schema4 rows follow backend retention; the durable registry follows its separate operator policy and is not erased by local purge.

## Consent and controls

Telemetry stays off until an explicit answer. Existing v1 consent continues to authorize only anonymous schema2 rows. Existing v2 consent continues to authorize schema3 lifecycle rows. Setup, hooks, postinstall and upgrades never silently change either scope to v3. An explicit telemetry enable or telemetry yes accepts the disclosed v3 scope and enrolls an existing configured home.

The local state is one of:

`unasked → asked → enabled`

`                 ↘ declined`

The plugin's SessionStart hook asks in chat on Claude and Codex versions with
plugin hook support. The managed Codex launcher provides a terminal fallback
after the client exits if no notice was recorded. Both atomically record `asked`
before showing the notice. It is shown once per consent scope. Existing v1/v2 consent is never silently upgraded to v3 by setup, startup hooks, upgrades, or postinstall. Setup and
reinstall do not change a declined choice. An explicit `session-handoff telemetry enable` command may later opt in
after a decline without showing the prompt again. Blank, interrupted, or
unrecognized terminal input leaves telemetry off and records `asked`, not
`declined`.

In chat, only these complete strings are recognized, with exact
spelling and case:

```text
session-handoff telemetry yes
session-handoff telemetry no
```

They resolve a pending choice locally. No natural-language interpretation or
partial matching occurs. The terminal prompt accepts `y`, `yes`, `n`, and
`no`, case-insensitively; other input is ambiguous and leaves the state at
`asked`.

After managed setup, the controls work without Node/npm:

```sh
python3 "$HOME/.local/share/session-handoff/plugin/bin/session-handoff" telemetry status
python3 "$HOME/.local/share/session-handoff/plugin/bin/session-handoff" telemetry yes
python3 "$HOME/.local/share/session-handoff/plugin/bin/session-handoff" telemetry no
python3 "$HOME/.local/share/session-handoff/plugin/bin/session-handoff" telemetry enable
python3 "$HOME/.local/share/session-handoff/plugin/bin/session-handoff" telemetry preview
python3 "$HOME/.local/share/session-handoff/plugin/bin/session-handoff" telemetry flush
python3 "$HOME/.local/share/session-handoff/plugin/bin/session-handoff" telemetry report --category constraint --severity recoverable
python3 "$HOME/.local/share/session-handoff/plugin/bin/session-handoff" telemetry disable
python3 "$HOME/.local/share/session-handoff/plugin/bin/session-handoff" telemetry disable --purge
```

Before setup, ask for session-handoff telemetry commands: the plugin's read-only
`handoff_setup` tool supplies the full paths automatically.
If `SESSION_HANDOFF_HOME` customizes the setup location, use that directory in
place of `$HOME`. The npm alternative is `npx session-handoff telemetry …`.
The CLI's exact `yes` records explicit consent, including after a prior decline;
`no` disables telemetry without purging stored data. Neither needs an interactive
terminal. An agent must not choose either on the user's behalf. Launcher setup
and its `--yes` flag do not grant telemetry consent.

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

## Installation registry and consent versions

Consent version 1 records only anonymous schema2 daily aggregates.
Consent version 2 additionally permits schema3 lifecycle rows. Each row has exactly seven fields: `schema_version`, `event=installation_lifecycle`, `day_utc`, `plugin_version`, `origin`, `installation_id` (32 lowercase hexadecimal characters), and `lifecycle_action` (`registered` or `uninstalled`).
Consent version 3 additionally permits schema4 installation status rows. Each row has exactly eight fields: `schema_version=4`, `event=installation_status`, `day_utc`, `plugin_version`, `origin`, `installation_id`, `lifecycle_action` (`registered`, `observed`, or `uninstalled`), and `observed_at` (UTC RFC3339 seconds matching `day_utc`).

Existing v1 and v2 consent is never silently upgraded by setup, startup hooks, upgrades, or postinstall. `telemetry enable` or exact `telemetry yes` is required for each higher consent scope.

With v3, a newly enrolled configured home emits one `registered` status; a v2 home upgraded to v3 may first emit `observed` for its existing identity. It then emits at most one `observed` status per UTC day or when the installed version changes. Observations run during setup/reconciliation and SessionStart; there is no daemon and no inference of uninstall from inactivity. A successful managed removal emits `uninstalled`. The registry retains only minimal first/last observation, last version, and uninstall state while the service operates. This registry retention is separate from the 13-month retention for raw schema2/schema3/schema4 telemetry rows.

These measurements cover consenting configured homes and observed managed removals. They do not count downloads, people, all package-manager removals, offline uninstallations, or complete churn. One person may have multiple homes, and a shared home may represent multiple people. Disabling stops future updates; local purge removes the local ID and queue but cannot erase records already received by the server.
