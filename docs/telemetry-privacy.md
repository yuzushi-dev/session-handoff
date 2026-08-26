# Telemetry privacy notice

This notice describes the optional aggregate telemetry shared by
`session-handoff` and the Sando plugin, on one Collector/Loki/Grafana backend
scoped per product by `service.name`. It is a product privacy contract, not
legal advice. It must be completed and approved by each project's owner
before any public endpoint or release.

## Data inventory

### session-handoff

When telemetry is enabled, the client records only allowlisted daily counters.
An operation aggregate contains:

- schema version, UTC day, plugin major/minor version;
- operation (`handoff` or `migrate`), source and target client;
- result, failure stage, duration bucket, handoff-size bucket, redaction,
  dropped-event and normalized-field count buckets;
- aggregate count.

Optional context feedback contains the same safe dimensions plus one fixed
category and severity. The client uploads `daily_aggregate` rows, never an
individual operation event. The OTLP body is fixed and the only resource
attribute is `service.name=session-handoff`.

The local configuration additionally records the enabled/disabled marker,
consent version, consent timestamp and the fixed endpoint. The local counter,
queue and last-operation summary are aggregate/safe-dimension state only.

### Sando

Sando records only allowlisted daily counters, one row per event type per
day. A `hook_summary` row contains schema version, UTC day, plugin
major/minor version, host (`claude`/`codex`), mode (`enforce`/`observe`),
tool-call/redaction/capped-output count buckets and a bytes-saved bucket. A
`proxy_summary` row contains the same identifying fields plus
rewrite-applied/skipped-for-cache count buckets, an input-tokens-saved
bucket, and whether the request hit the prompt cache. Sando's rows carry no
`count` multiplier field: each row already represents one day of that
event's activity. The OTLP body's only resource attribute is
`service.name=sando`.

Sando's local configuration and lifecycle mirror session-handoff's: an
enabled/disabled marker, consent version and timestamp, and the fixed
endpoint, at `~/.config/sando/telemetry.json`.

The system does not collect or upload transcript, prompt, handoff text, tool
trace, commands, diffs, paths, session/installation/device/account IDs,
hostname, username, IP address, user agent, locale, repository or model name,
arbitrary metadata, exception text, stack traces, free text, credentials,
tokens, cookies or authorization data. The proxy may process a peer address
transiently for loopback access control; it does not store it in access logs.

## Purpose and limits

The purpose is to measure aggregate mechanical continuity outcomes and
voluntary, structured context-loss feedback. It is not user analytics,
profiling, a heartbeat, content inspection or a population failure rate.
Feedback is an opt-in sample and has no unique-user denominator.

## Consent and controls

Telemetry is off by default. Collection starts only after an interactive,
explicit `yes` to the disclosure. Blank input, `no`, EOF, interruption,
non-interactive setup, `--yes`, environment variables, reinstall and upgrade
cannot enable it. The consent timestamp stays local and is never uploaded.

The exact command surface is:

```text
session-handoff telemetry status
session-handoff telemetry enable
session-handoff telemetry preview
session-handoff telemetry flush
session-handoff telemetry report --category constraint --severity recoverable
session-handoff telemetry disable
session-handoff telemetry disable --purge
```

Sando exposes the equivalent surface for its own schema (no `report`, since
Sando has no feedback event type):

```text
sando telemetry status
sando telemetry enable
sando telemetry preview
sando telemetry flush
sando telemetry disable
sando telemetry disable --purge
```

`preview` does not upload. `disable` stops collection before network access.
`disable --purge` removes local counters, queue, last-operation summary,
consent timestamp and endpoint, then leaves only the disabled prompt marker.
It cannot delete an individual backend contributor because no contributor
identifier is stored.

## Retention and deletion

| Data | Retention | Deletion/control |
| --- | ---: | --- |
| Local counters and queue | 7 days, 256 rows maximum | `session-handoff telemetry disable --purge` immediately |
| Collector memory/batch | under 5 minutes | process expiry |
| Loki daily aggregate rows | 13 months (`11232h`) | configured object/chunk retention and operator purge |
| Backups | 30 days proposed operator maximum | backup lifecycle rule and operator purge |
| Reverse-proxy access logs | none | access logging is disabled |

The repository does not configure a public service, backup provider or backup
credentials. Therefore the backend and backup purge must be verified by the
approved operator in disposable storage before publication. For the local
raw state, the disposable verification is:

```bash
SESSION_HANDOFF_HOME="$PWD/.telemetry-disposable" \
  session-handoff telemetry disable --purge
```

For a deployed disposable stack, the approved operator must run the equivalent
volume/object and backup-lifecycle purge for the selected host, record the
result, and confirm that no raw samples remain. Do not invent a provider
command in this repository.

## Processing components and contacts

The technical processing path is the client, a first-party OpenTelemetry
Collector, Loki storage and private Grafana dashboards. Nginx is the private
loopback gateway. The Collector repeats the client allowlist; Loki receives
only the matching tenant stream; Grafana queries only its matching dashboard.

- Data controller / project owner: `[owner contact to be supplied by project owner]`
- Technical processor/operator: `[processor contact to be supplied by project owner]`
- Hosting provider and region: `[hosting details to be supplied by project owner]`

No contact, processor appointment or hosting location is asserted by this
repository. Fill these placeholders and obtain the required privacy/security
review before deployment.

## Boundary and release gate

The client is the consent and validation boundary. The Collector is the
schema, attribute and service-name boundary. Loki is tenant-scoped aggregate
storage. Grafana is private read access to bounded sums. Dashboard and storage
endpoints must remain private. The current deployment binds gateway ports to
loopback; there is no public endpoint in this checkout.

No public endpoint may be enabled until the owner approves hosting and spend,
an independent privacy/security review approves the closed schema and abuse
controls, retention and purge have been tested in disposable storage, and a
canary confirms the packet body contains only the inventory above.
