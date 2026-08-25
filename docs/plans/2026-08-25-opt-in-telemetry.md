# Opt-in Telemetry Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Measure handoff and migration reliability in real installations without collecting session content or persistent user identifiers.

**Architecture:** Keep telemetry off by default. The client aggregates allowlisted, bucketed outcomes by UTC day and uploads them only after explicit consent. A first-party OpenTelemetry Collector rejects unknown attributes and forwards OTLP logs to Loki; Grafana sums the aggregate rows without storing per-operation records.

**Tech Stack:** Python 3.10+ standard library, JSON, pytest, OTLP/HTTP JSON, OpenTelemetry Collector, Loki, Grafana, Docker Compose.

---

## Status, estimate, and ownership

This capability does not exist. The project lacks an event schema, consent
flow, uploader, backend, retention policy, and dashboard.

- Unverified estimate for an aggregate MVP: 2–4 engineering days.
- Unverified estimate for a publishable service: 1–2 weeks, including
  security hardening, privacy documentation, retention, abuse controls, and a
  canary.
- The repository owner must approve hosting, public endpoint exposure,
  operating spend, the privacy notice, and release. Those actions remain
  outside implementation authority.

The largest risks are accidental content leakage, re-identification through
rare attribute combinations, opt-in sample bias, endpoint abuse, and ongoing
storage or monitoring cost.

## Product contract

Telemetry measures two different claims:

1. Mechanical reliability: validation, switch, conversion, resume, fallback,
   duration, and reported migration loss.
2. Semantic reliability: explicit user feedback that the continuation missed
   a constraint, decision, path, progress item, or rejected attempt.

The plugin cannot infer semantic loss from a successful relaunch. It does not
observe later model tool calls and must not inspect them. Dashboards must label
feedback rates as an opt-in sample, not a population failure rate.

### Non-negotiable privacy boundary

The client and backend must reject these fields at every layer:

- transcript, prompt, handoff text, tool trace, command, diff, file path;
- source or target session ID, installation ID, device ID, account ID;
- hostname, username, IP address, user agent, locale, repository name;
- model name, arbitrary metadata, exception text, stack trace, free text;
- credentials, tokens, cookies, authorization headers from the host process.

The ingress proxy may use an IP address in memory for rate limiting. It must not
write access logs containing IP addresses or user agents. The service stores no
identifier that can answer “which user sent this event.”

## Existing implementations considered

| Option | Fit | Decision |
|---|---|---|
| OpenTelemetry Collector → Loki → Grafana | Vendor-neutral ingest and filtering; Loki accepts OTLP logs and Grafana can sum the uploaded counts | Recommended |
| Umami self-hosted | Fast custom-event dashboards, but its event API expects web/session properties and accepts unauthenticated ingest | Reject for MVP |
| PostHog | Mature event schemas and privacy controls, but a larger SDK, identity model, and data surface | Reject for MVP |
| Local export only | Lowest privacy risk, no fleet view | Phase 0 and permanent fallback |

OpenTelemetry supplies transport and processing, not consent or product
semantics. `session-handoff` still owns the closed schema, local queue, privacy
contract, and feedback categories.

Primary references:

- [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/)
- [OTLP specification](https://opentelemetry.io/docs/specs/otlp/)
- [Loki OTLP ingestion](https://grafana.com/docs/loki/latest/send-data/otel/)
- [Umami server-side event API](https://docs.umami.is/docs/api/sending-stats)
- [PostHog event capture](https://posthog.com/docs/product-analytics/capture-events)
- [PostHog privacy controls](https://posthog.com/docs/privacy)

## Closed event schema, version 1

After a terminal outcome, the client builds one `operation_summary` in memory,
validates it, and increments the matching daily counter. It never persists
per-operation events and never records start and end events that need a
correlation identifier.

```json
{
  "schema_version": 1,
  "event": "operation_summary",
  "day_utc": "2026-08-25",
  "plugin_version": "0.5",
  "operation": "handoff",
  "source_client": "codex",
  "target_client": "codex",
  "result": "success",
  "failure_stage": "none",
  "duration_bucket": "5_to_30s",
  "handoff_bytes_bucket": "16_to_64k",
  "redaction_bucket": "zero",
  "dropped_events_bucket": "zero",
  "normalized_fields_bucket": "zero"
}
```

Allowed values:

| Field | Values |
|---|---|
| `schema_version` | integer `1` |
| `event` | `operation_summary`, `context_feedback` |
| `day_utc` | UTC date only; no time |
| `plugin_version` | numeric `major.minor`; reject prerelease labels and patch |
| `operation` | `handoff`, `migrate` |
| `source_client`, `target_client` | `claude`, `codex` |
| `result` | `success`, `failure`, `fallback` |
| `failure_stage` | `none`, `validation`, `control`, `source_stop`, `conversion`, `target_resume`, `source_resume`, `unknown` |
| `duration_bucket` | `lt_1s`, `1_to_5s`, `5_to_30s`, `30_to_120s`, `gte_120s` |
| byte/count buckets | `zero`, `one`, `2_to_5`, `6_to_20`, `gt_20`; handoff bytes use `lt_4k`, `4_to_16k`, `16_to_64k`, `gte_64k` |

`context_feedback` reuses the date, version, operation, and client fields from
the most recent local summary, then adds only:

```json
{
  "feedback_category": "constraint",
  "feedback_severity": "recoverable"
}
```

Allowed categories are `constraint`, `decision`, `path`, `progress`,
`rejected_attempt`, and `other`. Severity is `recoverable` or `blocked`.
`other` never accepts an explanation.

The schema rejects unknown fields, unknown enum values, booleans where integers
are expected, strings longer than 32 characters, nested objects, and arrays.
The serialized local event limit is 2 KiB.

The uploader sends only `daily_aggregate` rows:

```json
{
  "schema_version": 1,
  "event": "daily_aggregate",
  "aggregate": "operation",
  "day_utc": "2026-08-25",
  "plugin_version": "0.5",
  "operation": "handoff",
  "source_client": "codex",
  "target_client": "codex",
  "result": "success",
  "failure_stage": "none",
  "duration_bucket": "5_to_30s",
  "handoff_bytes_bucket": "16_to_64k",
  "redaction_bucket": "zero",
  "dropped_events_bucket": "zero",
  "normalized_fields_bucket": "zero",
  "count": 12
}
```

`count` is an integer from 1 through 10,000. The uploader maps each row to an
OTLP LogRecord whose body is the fixed string
`session_handoff.daily_aggregate`; the row fields become attributes. It sets
only `service.name=session-handoff` on the OTLP resource. It runs no host,
process, network, or environment resource detector.

`aggregate` is `operation` or `context_feedback`. An operation row contains
the result, failure, duration, byte, redaction, dropped-event, and normalization
fields. A feedback row omits those fields and contains only
`feedback_category`, `feedback_severity`, and their shared dimensions. The
validator rejects rows that mix both shapes.

## Consent and local lifecycle

Configuration lives at
`~/.config/session-handoff/telemetry.json`, mode `0600`:

```json
{
  "schema_version": 1,
  "enabled": true,
  "prompted_consent_version": 1,
  "consent_version": 1,
  "consented_at": "2026-08-25T21:00:00Z",
  "endpoint": "https://telemetry.session-handoff.example/v1/logs"
}
```

The timestamp and prompt state remain local. The uploader never includes them.
A declined or disabled choice stores only this marker so upgrades do not nag:

```json
{
  "schema_version": 1,
  "enabled": false,
  "prompted_consent_version": 1
}
```

Command surface:

```text
session-handoff telemetry status
session-handoff telemetry enable
session-handoff telemetry preview
session-handoff telemetry flush
session-handoff telemetry report --category constraint --severity recoverable
session-handoff telemetry disable --purge
```

On the first interactive `session-handoff setup`, after base setup succeeds,
the installer prints the exact field list, retention, endpoint, opt-in sample
bias, and the inability to delete one contributor's aggregate rows. It then
asks `Enable anonymous aggregate telemetry? [y/N]`. Only an explicit `yes`
enables collection; blank input, `no`, EOF, or interruption writes the disabled
marker. A non-interactive setup, `--yes`, environment variable, reinstall, or
upgrade cannot enable telemetry or repeat the prompt.

`enable` presents the same disclosure and requires an interactive `yes`.
`preview` does not upload; it renders the exact next OTLP request body and
header names, excluding the authorization value. `disable` stops collection
before touching the network. `--purge` removes counters, queue, last-operation
summary, consent timestamp, and configured endpoint while preserving the
disabled prompt marker.

The local counters live at
`~/.local/state/session-handoff/telemetry-counters.json`, mode `0600`. A
bounded queue at `telemetry-queue.jsonl` holds only closed daily aggregates. It
keeps at most 256 rows, 256 KiB, or 7 days. Atomic replacement prevents partial
JSON. Overflow drops the oldest row and increments a local-only counter. A
failed upload leaves the queue intact; telemetry failure never changes a
handoff result or exit code.

## Upload and backend contract

After the supervisor records a terminal result, the main process validates an
in-memory `operation_summary` and atomically increments its local daily
counter. It never persists a per-operation event. A short-lived detached
process then closes prior UTC days and flushes the queue. The detached process
receives only the queue path and telemetry configuration path. It inherits no
provider credential variables, reads at most 32 rows, maps them to OTLP/HTTP
JSON logs, uses HTTPS, applies a 3-second total timeout, and retries only on the
next invocation. It sends gzip only if the standard library can produce the
body before the size check.

Ingress requirements:

- maximum request body: 64 KiB; maximum 32 aggregate rows;
- TLS 1.2 or newer at the reverse proxy;
- in-memory IP rate limit, no access logs, no request-body logs;
- reject unknown schema versions, fields, enums, or content types;
- collector allowlist repeats the client validator;
- the public write token is a routing key, not a secret or trust boundary; it
  grants no read, query, or administration access;
- dashboard and storage endpoints remain private.

The Collector repeats the attribute allowlist and forwards accepted rows to
Loki's native OTLP endpoint. Loki indexes only `service.name`, `event`,
`aggregate`, `operation`, `source_client`, and `target_client`; it stores
`result` and the remaining allowlisted fields as structured metadata. Grafana
uses LogQL `sum_over_time` queries over `count` and displays:

- operation count and success/fallback/failure rate by mode and client pair;
- failure stage, duration bucket, handoff-size bucket, and migration-loss bucket;
- explicit context-feedback category and severity;
- plugin `major.minor` adoption among events;
- a permanent “opt-in event sample; no unique-user denominator” notice.

## Retention

| Data | Retention | Deletion |
|---|---:|---|
| Local counters and queue | 7 days or 256 aggregate rows | Immediate on `disable --purge` |
| Collector memory/batch | Under 5 minutes | Process expiry |
| Loki daily aggregate rows | 13 months | Object/chunk retention and tested purge command |
| Backups | 30 days | Backup lifecycle rule |
| Reverse-proxy access logs | Disabled | No stored record |

The service cannot delete one person’s events because it stores no person or
installation identifier. The privacy notice must state this before consent.
Any future diagnostic bundle needs a separate consent, receipt identifier,
deletion endpoint, and retention schedule. Version 1 excludes that feature.

## Success gates

The MVP can ship to project-owner canaries when all gates pass:

- default-off product runs create no counters or queue and make no network
  calls; a declined install writes only the disabled prompt marker;
- payload golden tests prove the denylist and exact schema;
- consent, disable, purge, queue bounds, and failure isolation pass tests;
- a packet capture matches the `telemetry preview` OTLP body byte for byte and
  has the same header names;
- ingress rejects a seeded secret, path, UUID, session ID, and unknown field;
- retention deletion runs in a disposable backend and leaves no raw samples;
- Loki labels stay bounded and dashboard panels show the opt-in sample warning;
- a reviewer approves the threat model and privacy notice;
- a seven-day owner canary reports no product-path regression.

Publication remains gated on user approval, hosting approval, and an independent
privacy/security review. Do not present this plan as legal advice.

## Implementation tasks

### Task 1: Freeze schema and denylist

**Files:**
- Create: `server/telemetry.py`
- Create: `tests/test_telemetry.py`

1. Write failing tests for the two valid events, each invalid enum, unknown
   fields, nested data, overlong strings, UUIDs, paths, credentials, session
   IDs, and payloads over 2 KiB.
2. Run `rtk pytest -q tests/test_telemetry.py`; expect failures because the
   module does not exist.
3. Add immutable enum sets, bucket functions, `validate_event(payload)`, and
   `serialize_event(payload)` using only the standard library.
4. Re-run the focused test; expect all cases to pass.
5. Commit: `feat: define privacy-safe telemetry schema`.

### Task 2: Add explicit consent commands

**Files:**
- Modify: `bin/session-handoff`
- Modify: `server/telemetry.py`
- Create: `tests/test_telemetry_cli.py`
- Modify: `tests/test_package.py`
- Modify: `tests/test_setup.py`

1. Add failing tests for `status`, interactive `enable`, declined consent,
   `disable --purge`, invalid config, first interactive setup `yes`/`no`,
   non-interactive setup, and upgrade without a repeated prompt.
2. Run `rtk pytest -q tests/test_telemetry_cli.py tests/test_setup.py`; expect
   unknown-command and missing-prompt failures.
3. Implement `status`, `enable`, `disable`, the shared disclosure prompt, and
   atomic mode-`0600` configuration writes.
4. Assert blank input defaults to no; setup and reinstall preserve a recorded
   choice; non-interactive setup and upgrades cannot opt in.
5. Run `rtk pytest -q tests/test_telemetry_cli.py tests/test_setup.py
   tests/test_package.py`; expect all cases to pass.
6. Commit: `feat: add explicit telemetry consent`.

### Task 3: Build the bounded queue and uploader

**Files:**
- Modify: `server/telemetry.py`
- Modify: `bin/session-handoff`
- Modify: `tests/test_telemetry.py`
- Modify: `tests/test_telemetry_cli.py`

1. Add failing tests for daily aggregation, queue permissions, atomic writes,
   age/count/byte limits, oldest-first batches, OTLP mapping, HTTP timeout,
   partial acceptance, retryable failure, purge, and an empty inherited
   environment.
2. Use a local `http.server.ThreadingHTTPServer` fixture; do not contact a real
   endpoint.
3. Implement `increment_counter`, `close_day`, `load_batch`, `to_otlp_logs`,
   `ack_batch`, `flush_queue`, `telemetry preview`, `telemetry flush`, and the
   detached flush entrypoint with `urllib.request`.
4. Prove that every uploader exception leaves the product result unchanged.
5. Run `rtk pytest -q tests/test_telemetry.py tests/test_telemetry_cli.py`;
   expect all cases to pass.
6. Commit: `feat: queue opt-in telemetry safely`.

### Task 4: Instrument terminal handoff and migration outcomes

**Files:**
- Modify: `server/handoff_mcp.py`
- Modify: `server/session_switch.py`
- Modify: `server/migration.py`
- Modify: `tests/test_handoff_mcp.py`
- Modify: `tests/test_session_switch.py`
- Modify: `tests/test_migration.py`

1. Add failing tests for success, validation failure, conversion failure,
   target-resume failure, source fallback, dropped events, and normalized
   fields.
2. Pass only numeric safe summaries through the authenticated local control
   request. Never pass handoff content, paths, or session IDs to telemetry.
3. Build one in-memory summary after the supervisor reaches a terminal state.
   When consent is enabled, validate it and synchronously increment the safe
   local counter before starting the detached uploader. Skip all telemetry
   calls when consent is disabled.
4. Run `rtk pytest -q tests/test_handoff_mcp.py tests/test_session_switch.py
   tests/test_migration.py`; expect all cases to pass.
5. Commit: `feat: record terminal continuity outcomes`.

### Task 5: Add structured context-loss feedback

**Files:**
- Modify: `bin/session-handoff`
- Modify: `server/telemetry.py`
- Modify: `tests/test_telemetry_cli.py`
- Modify: `README.md`

1. Add failing tests for the fixed category and severity enums, disabled
   telemetry, and rejection of free text or extra flags.
2. Implement `telemetry report` using only the safe dimensions from a local
   last-operation summary, kept for at most 24 hours and never uploaded by
   itself.
3. Document that feedback is voluntary and that `other` has no text field.
4. Run `rtk pytest -q tests/test_telemetry_cli.py`; expect all cases to pass.
5. Commit: `feat: accept structured context feedback`.

### Task 6: Deploy an existing aggregate backend

**Files:**
- Create: `deploy/telemetry/docker-compose.yml`
- Create: `deploy/telemetry/otel-collector.yaml`
- Create: `deploy/telemetry/loki.yaml`
- Create: `deploy/telemetry/nginx.conf`
- Create: `deploy/telemetry/grafana-dashboard.json`
- Create: `deploy/telemetry/README.md`
- Create: `tests/test_telemetry_deploy.py`

1. Add standard-library static tests that inspect the configuration, assert
   the attribute allowlist, reject debug/logging exporters, check retention
   values, and confirm that dashboard queries sum `count` over bounded labels.
2. Pin container image digests after a security review. Do not use `latest`.
3. Configure OTLP/HTTP ingest, allowlist processing, Loki's native OTLP
   endpoint, private Grafana access, 13-month aggregate retention, and disabled
   proxy access logs.
4. Run `rtk pytest -q tests/test_telemetry_deploy.py`; expect static checks to
   pass.
5. In an opt-in integration test, run the stack on loopback with synthetic
   aggregates; prove rejected payloads do not reach Loki.
6. Commit: `ops: add aggregate telemetry backend`.

### Task 7: Privacy, abuse, and retention tests

**Files:**
- Create: `docs/telemetry-privacy.md`
- Create: `docs/telemetry-threat-model.md`
- Create: `tests/test_telemetry_privacy.py`
- Modify: `README.md`

1. Write the data inventory, purpose, consent text, retention table, processor
   list, owner contact, and exact disable/purge commands.
2. Add adversarial payload tests for secret shapes, path traversal, UUIDs,
   high-cardinality strings, oversized batches, replay, and rate-limit bursts.
3. Execute the raw-sample and backup purge procedures in disposable storage.
4. Run `rtk pytest -q tests/test_telemetry_privacy.py`; expect all adversarial
   cases to pass.
5. Obtain independent privacy/security review before any public endpoint or
   release.
6. Commit: `docs: define telemetry privacy contract`.

### Task 8: Canary and release decision

**Files:**
- Create: `docs/telemetry-canary-report.md`
- Modify: `docs/2026-08-25-live-release-matrix.md`
- Update once: `/home/cristina/Obsidian/AgentMemory/10-projects/yuzushi-plugins.md`

1. Run:

   ```bash
   rtk pytest -q
   rtk run 'python3 -m compileall -q server'
   rtk run 'git diff --check'
   rtk run 'npm pack --dry-run'
   ```

   Then run strict plugin validation and the provider-free command matrix with
   the repository's documented commands.
2. Use project-owner installs for seven days. Compare `preview`, packet capture,
   Collector output, Loki labels, and dashboard totals.
3. Record opt-in count only from owner-provided canary enrollment; do not add a
   user heartbeat or installation identifier.
4. Report leakage findings, queue loss, backend cost, feedback usability, and
   product-path latency. Stop on any privacy-boundary violation.
5. Ask the user to approve or reject public hosting and release. Do not push,
   deploy, publish, or spend without that approval.

## Release decision

The development team may start Tasks 1–5 without a public service. Task 6 needs
an approved hosting target and operating budget. Tasks 7–8 decide whether the
feature is safe enough to publish.

The MVP estimate covers Tasks 1–5 with a mock endpoint. The 1–2 week estimate
covers backend deployment, threat modeling, privacy review, retention, canary,
and release fixes. Re-estimate after Task 1 and the backend configuration spike.
