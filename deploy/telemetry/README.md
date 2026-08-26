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

## Public ingress (Cloudflare Tunnel, opt-in profile)

An optional `cloudflared` service under the `public` Compose profile carries
OTLP ingest out through Cloudflare instead of an open inbound port. It never
starts with a plain `docker compose up` — only `--profile public` includes
it, and it reads `CLOUDFLARE_TUNNEL_TOKEN` from `.env` (see
`.env.example`); no token is stored in this repo.

Setup (owner-only, outside this checkout):

1. Cloudflare dashboard → Zero Trust → Networks → Tunnels → Create a tunnel.
2. Add a Public Hostname pointing at `http://gateway:4318` (the OTLP HTTP
   receiver inside this Compose network — not `localhost`, not a host port).
3. Copy the generated tunnel token into `deploy/telemetry/.env` as
   `CLOUDFLARE_TUNNEL_TOKEN=...`.
4. Start it explicitly: `docker compose --profile public up cloudflared`.

**Do this only after every gate in `docs/telemetry-canary-report.md` is
closed** — the seven-day owner canary, the packet-capture/Collector/Loki
comparison, the independent privacy and security review, and the approved
hosting budget. That report is the release decision; this section only
describes the mechanism, it does not authorize using it.
