# Shared aggregate telemetry backend

This private loopback stack is Collector → Loki → Grafana for two independent
schemas:

- `service.name=session-handoff`: operation and context-feedback aggregates.
- `service.name=sando`: hook and proxy aggregates.

The Collector drops unknown services and event/aggregate shapes, strips all
resource metadata except `service.name`, and keeps only the allowlisted
attributes for the matching service. Loki receives structured OTLP
logs, retains aggregate rows for 13 months (`11232h`), and does not expose a
query endpoint outside loopback. Nginx disables access logs and permits only
loopback clients.

Grafana provisions two Loki datasources with fixed `X-Scope-OrgID` headers and
each dashboard uses only its matching datasource. Loki indexes only the
bounded `service.name` label; aggregate dimensions remain structured metadata.

Images use immutable digest references. Before any deployment, verify each
digest and image signature against the approved security review; this checkout
does not perform that review or start containers. No public endpoint, secret,
hosting account, or budget is configured here.

## Local inspection

```bash
docker compose -f deploy/telemetry/docker-compose.yml config
pytest -q tests/test_telemetry_deploy.py
```

Do not expose the gateway by changing the loopback binds without an approved
authentication, rate-limit, retention, and privacy review.
