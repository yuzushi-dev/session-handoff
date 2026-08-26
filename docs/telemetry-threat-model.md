# Telemetry threat model

Scope: the opt-in aggregate path from the local client through the private
Collector, Loki and Grafana deployment. Version 1 has no public endpoint in
this checkout.

## Assets and trust boundaries

| Boundary | Trust decision | Required invariant |
| --- | --- | --- |
| Client → Collector | consented, validated aggregates only | no content, identifier or arbitrary attribute leaves the client |
| Collector → Loki | accepted service/schema only | allowlist is repeated; tenant is fixed to `session-handoff` |
| Loki → Grafana | private operator read path | queries sum bounded aggregate rows; no cross-tenant dashboard |
| Gateway → network | loopback only until release approval | no public bind, access log or request-body log |

The client stores a 0600 config and bounded local state. The Collector keeps
only bounded attributes and routes `session-handoff` separately from the Sando
schema. Loki indexes only bounded service labels; other dimensions remain
structured metadata. Grafana has no write or administration role in the
telemetry contract.

## Threats and controls

| Threat | Control | Failure mode / residual risk |
| --- | --- | --- |
| Transcript, path, secret or session-ID leakage | closed fields, denylist, enum/bucket validation, 2 KiB event and 64 KiB upload limits | a future field or instrumentation change could leak data; golden/adversarial tests and review are required |
| Path traversal or local state substitution | no paths in payload; private directories/files, `O_NOFOLLOW`, regular-file and hardlink checks, atomic replacement | platform support for secure filesystem primitives is required; otherwise fail closed |
| Re-identification by rare values | no identifiers, fixed enums, major/minor version, bounded labels and aggregate counts | small cells and opt-in sample bias can still reveal operational patterns; dashboards must show the sample warning |
| Replay, duplicate upload or stale acknowledgement | deterministic body-bound `Idempotency-Key`, batch digest, queue snapshot token, lease and stale-batch rejection | a processor may accept duplicate rows; counts remain aggregate and no identity is available for correction |
| Oversized request or queue exhaustion | bounded queue/rows/bytes, bounded physical reader, bounded response reader and controlled rejection | a local attacker can still consume disk/CPU within configured bounds |
| Flush burst or endpoint abuse | one active batch lease, short timeout, no redirects, loopback-only gateway and no access logs | no public ingress rate limiter is configured in this checkout; one is required before exposure |
| Cross-tenant or schema confusion | strict `service.name` filters, separate schema filters, allowlists, fixed Loki `X-Scope-OrgID`, scoped dashboards | a deployment misconfiguration can join pipelines; static checks and disposable two-tenant integration are release gates |
| Unauthorized querying | storage and dashboard endpoints private; gateway loopback binds | there is no public authentication contract yet; public exposure is prohibited |
| Retention or backup over-collection | 7-day local bound, 13-month Loki retention, 30-day backup lifecycle, no proxy access logs | configured backup tooling is outside this repository and needs operator evidence |
| Consent bypass or product regression | default-off config, interactive explicit `yes`, sanitized detached environment, telemetry errors isolated from product result | owner canary and packet capture remain necessary |

## Abuse and release tests

The dedicated privacy tests exercise controlled failures for secret-shaped
fields, traversal, UUID/session identifiers, high-cardinality strings,
oversized batches, replay/idempotency, concurrent flush bursts and mixed or
unknown tenant/schema rows. Deployment checks confirm strict tenant filters,
fixed tenant headers, bounded dashboards, loopback-only ports and no debug or
logging exporter.

Before any public endpoint or release, the owner must obtain independent
privacy/security review and provide evidence for:

1. disposable raw-sample and backup purge;
2. Collector rejection of unknown service and schema shapes;
3. packet capture matching `telemetry preview` without credentials;
4. rate-limit burst behavior and no stored client address;
5. seven-day canary with no product-path regression.

Until those gates are complete, the only approved deployment is private
loopback inspection. This document does not authorize hosting, publication,
spend or release.
