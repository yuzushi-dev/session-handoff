import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1] / "deploy" / "telemetry"


def test_shared_backend_files_exist():
    expected = {
        "docker-compose.yml",
        "otel-collector.yaml",
        "loki.yaml",
        "nginx.conf",
        "grafana-dashboard-session-handoff.json",
        "grafana-dashboard-sando.json",
        "grafana-datasources.yaml",
        "grafana-dashboard-providers.yaml",
        "README.md",
    }
    assert {path.name for path in ROOT.iterdir()} >= expected


def test_images_are_immutable_and_not_latest():
    compose = (ROOT / "docker-compose.yml").read_text()
    images = re.findall(r"^\s+image:\s+(\S+)$", compose, re.MULTILINE)
    assert len(images) == 5
    assert all("latest" not in image for image in images)
    assert all(re.search(r"@sha256:[0-9a-f]{64}$", image) for image in images)


def test_collector_has_two_closed_service_allowlists():
    collector = (ROOT / "otel-collector.yaml").read_text()
    assert "value: session-handoff" in collector
    assert "value: sando" in collector
    assert "X-Scope-OrgID: session-handoff" in collector
    assert "X-Scope-OrgID: sando" in collector
    assert 'attributes["event"] != "daily_aggregate"' in collector
    assert 'attributes["aggregate"] != "operation"' in collector
    assert 'attributes["event"] != "hook_summary" and attributes["event"] != "proxy_summary"' in collector
    assert 'keep_keys(resource.attributes, ["service.name"])' in collector
    for field in ("feedback_category", "feedback_severity", "tool_calls_bucket", "prompt_cache_hit"):
        assert f'"{field}"' in collector
    assert "logging" not in collector
    assert "debug" not in collector


def test_loki_retention_and_bounded_limits():
    loki = (ROOT / "loki.yaml").read_text()
    assert "retention_enabled: true" in loki
    assert "retention_period: 11232h" in loki
    assert "max_query_series: 100" in loki
    assert "max_entries_limit_per_query: 1000" in loki
    assert "action: index_label" in loki
    assert "- service.name" in loki


def test_gateway_is_loopback_only_and_has_no_access_logs():
    # Docker's userland proxy rewrites the peer address for published ports, so an
    # nginx-level `allow 127.0.0.1` never matches and would 403 all real traffic.
    # The loopback boundary is enforced by the host port bind in docker-compose.yml
    # instead (asserted below); it's the only control that actually sees 127.0.0.1.
    nginx = (ROOT / "nginx.conf").read_text()
    assert "access_log off;" in nginx
    compose = (ROOT / "docker-compose.yml").read_text()
    published_ports = re.findall(r'"(\d+\.\d+\.\d+\.\d+):\d+:\d+"', compose)
    assert published_ports
    assert all(ip == "127.0.0.1" for ip in published_ports)


def test_cloudflared_is_opt_in_and_has_no_hardcoded_token():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "cloudflared" in compose
    match = re.search(r"cloudflared:\n(?:.+\n)+?(?=\n\S|\Z)", compose)
    assert match, "cloudflared service block not found"
    block = match.group(0)
    assert 'profiles: ["public"]' in block
    assert "CLOUDFLARE_TUNNEL_TOKEN" in block
    assert not re.search(r"token[\"']?\s*[:=]\s*[\"']?ey", block, re.IGNORECASE)
    assert not re.search(r'ports:\s*\n\s*-\s*"', block)
    env_example = (ROOT / ".env.example").read_text()
    assert env_example.strip() != "CLOUDFLARE_TUNNEL_TOKEN=" + "placeholder-token"
    assert "CLOUDFLARE_TUNNEL_TOKEN=" in env_example


def test_dashboards_scope_queries_and_sum_counts():
    for name, service in (
        ("grafana-dashboard-session-handoff.json", "session-handoff"),
        ("grafana-dashboard-sando.json", "sando"),
    ):
        dashboard = json.loads((ROOT / name).read_text())
        queries = [target["expr"] for panel in dashboard["panels"] for target in panel.get("targets", [])]
        assert queries
        assert all("sum" in query and "count" in query for query in queries)
        assert all(f'service_name=\\"{service}\\"' in json.dumps(query) for query in queries)
        assert all(f"loki-{service}" in json.dumps(panel.get("datasource")) for panel in dashboard["panels"] if panel.get("targets"))


@pytest.mark.skipif(
    not os.environ.get("SESSION_HANDOFF_RUN_DEPLOY_INTEGRATION"),
    reason="loopback telemetry stack integration is opt-in",
)
def test_loopback_stack_routes_two_tenants_and_drops_unknown_service():
    compose = ["docker", "compose", "-f", str(ROOT / "docker-compose.yml")]
    subprocess.run([*compose, "up", "-d", "--wait"], check=True)

    ATTRIBUTES_BY_SERVICE = {
        "session-handoff": [
            {"key": "schema_version", "value": {"intValue": "1"}},
            {"key": "event", "value": {"stringValue": "daily_aggregate"}},
            {"key": "aggregate", "value": {"stringValue": "operation"}},
            {"key": "day_utc", "value": {"stringValue": "2026-08-26"}},
            {"key": "plugin_version", "value": {"stringValue": "0.5"}},
            {"key": "count", "value": {"intValue": "1"}},
        ],
        "sando": [
            {"key": "schema_version", "value": {"intValue": "1"}},
            {"key": "event", "value": {"stringValue": "hook_summary"}},
            {"key": "day_utc", "value": {"stringValue": "2026-08-26"}},
            {"key": "plugin_version", "value": {"stringValue": "0.5"}},
            {"key": "host", "value": {"stringValue": "claude"}},
            {"key": "mode", "value": {"stringValue": "enforce"}},
            {"key": "tool_calls_bucket", "value": {"stringValue": "one"}},
        ],
        "unknown-service": [
            {"key": "schema_version", "value": {"intValue": "1"}},
            {"key": "event", "value": {"stringValue": "daily_aggregate"}},
        ],
    }

    def send(service):
        body = {
            "resourceLogs": [{
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                "scopeLogs": [{"logRecords": [{
                    "body": {"stringValue": "session_handoff.daily_aggregate"},
                    "attributes": ATTRIBUTES_BY_SERVICE[service],
                }]}],
            }],
        }
        request = urllib.request.Request(
            "http://127.0.0.1:4318/v1/logs",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        # `up --wait` only waits for the container to be running, not for the
        # collector's OTLP receiver to accept connections; retry through that gap.
        last_error = None
        for _ in range(15):
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    assert response.status in {200, 202}
                    return
            except (urllib.error.URLError, OSError) as error:
                last_error = error
                time.sleep(1)
        raise last_error

    try:
        send("session-handoff")
        send("sando")
        send("unknown-service")
        def loki_series(org, match):
            query = urllib.parse.quote(f"match[]={match}", safe="=")
            result = subprocess.run(
                [*compose, "exec", "-T", "loki", "wget", "-qO-", f"--header=X-Scope-OrgID: {org}", f"http://127.0.0.1:3100/loki/api/v1/series?{query}"],
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout

        # the Collector batches for up to 5s (`batch.timeout`) before exporting to Loki.
        for service in ("session-handoff", "sando"):
            match = f'{{service_name="{service}"}}'
            for attempt in range(10):
                stdout = loki_series(service, match)
                if service in stdout:
                    break
                time.sleep(1)
            assert service in stdout

        for service in ("session-handoff", "sando"):
            assert "unknown-service" not in loki_series(service, '{service_name="unknown-service"}')
    finally:
        subprocess.run([*compose, "down", "-v"], check=True)
