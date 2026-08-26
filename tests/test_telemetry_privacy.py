import copy
import hashlib
import json
import threading
from pathlib import Path

import pytest

import server.telemetry as telemetry


ROOT = Path(__file__).parents[1]

OPERATION = {
    "schema_version": 1,
    "event": "operation_summary",
    "day_utc": "2026-08-25",
    "plugin_version": "0.5",
    "operation": "handoff",
    "source_client": "codex",
    "target_client": "claude",
    "result": "success",
    "failure_stage": "none",
    "duration_bucket": "5_to_30s",
    "handoff_bytes_bucket": "16_to_64k",
    "redaction_bucket": "zero",
    "dropped_events_bucket": "zero",
    "normalized_fields_bucket": "zero",
}


def _row(count=1):
    return telemetry._aggregate_row(OPERATION, count)


def test_privacy_notice_covers_inventory_consent_processors_retention_and_commands():
    privacy = (ROOT / "docs/telemetry-privacy.md").read_text(encoding="utf-8")

    for phrase in (
        "Data inventory",
        "Purpose",
        "Consent",
        "Retention",
        "OpenTelemetry",
        "Collector",
        "Loki",
        "Grafana",
        "[owner contact to be supplied by project owner]",
        "session-handoff telemetry disable",
        "session-handoff telemetry disable --purge",
        "no public endpoint",
        "no unique-user denominator",
    ):
        assert phrase in privacy
    assert "@" not in privacy


@pytest.mark.parametrize(
    "field",
    sorted(telemetry.DENYLIST),
)
def test_secret_path_identity_and_tracking_fields_fail_closed(field):
    payload = OPERATION | {field: "sensitive-value"}

    with pytest.raises(ValueError):
        telemetry.validate_event(payload)


@pytest.mark.parametrize(
    "payload",
    [
        OPERATION | {"metadata": {"secret": "value"}},
        OPERATION | {"labels": ["user-provided"]},
        OPERATION | {"request_id": "a" * 64},
        OPERATION | {"plugin_version": "1.2." + "x" * 64},
        OPERATION | {"source_client": "a" * 33},
    ],
)
def test_nested_unknown_and_high_cardinality_strings_fail_closed(payload):
    with pytest.raises(ValueError):
        telemetry.validate_event(payload)


@pytest.mark.parametrize(
    "payload",
    [
        OPERATION | {"path": "../../etc/passwd"},
        OPERATION | {"file_path": "..\\..\\secrets"},
        OPERATION | {"plugin_version": "../../telemetry"},
        OPERATION | {"target_client": "../codex"},
    ],
)
def test_path_traversal_shapes_fail_closed(payload):
    with pytest.raises(ValueError):
        telemetry.validate_event(payload)


@pytest.mark.parametrize(
    "payload",
    [
        OPERATION | {"session_id": "8f14e45f-ea"},
        OPERATION | {"source_session_id": "8f14e45f-ea"},
        OPERATION | {"target_session_id": "8f14e45f-ea"},
        OPERATION | {"uuid": "8f14e45f-ea"},
    ],
)
def test_uuid_and_session_identifier_shapes_fail_closed(payload):
    with pytest.raises(ValueError):
        telemetry.validate_event(payload)


def test_oversized_upload_batch_fails_before_request_creation():
    with pytest.raises(ValueError, match="row limit"):
        telemetry.build_request(telemetry.ENDPOINT, [_row()] * (telemetry.MAX_UPLOAD_ROWS + 1))


def test_replayed_batch_is_rejected_and_idempotency_key_is_body_bound(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry._store_queue(tmp_path, [_row()])
    batch = telemetry.load_batch(tmp_path)
    replay = copy.copy(batch)
    request = telemetry.build_request(telemetry.ENDPOINT, batch)

    assert request.headers["Idempotency-key"] == hashlib.sha256(request.data).hexdigest()
    telemetry.ack_batch(tmp_path, batch, digest=telemetry._batch_digest(batch))
    with pytest.raises(telemetry.TelemetryConfigError, match="stale"):
        telemetry.ack_batch(tmp_path, replay, digest=telemetry._batch_digest(replay))


def test_concurrent_flush_burst_claims_one_batch(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry._store_queue(tmp_path, [_row()])
    entered = threading.Event()
    release = threading.Event()
    calls = []
    lock = threading.Lock()

    class Response:
        status = 200

        def read(self, _size):
            if getattr(self, "done", False):
                return b""
            self.done = True
            return b'{"accepted":1}'

        def close(self):
            return None

    def opener(_request, **_kwargs):
        with lock:
            calls.append(1)
        entered.set()
        release.wait(2)
        return Response()

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(telemetry.flush_queue(tmp_path, opener=opener)))
        for _ in range(8)
    ]
    threads[0].start()
    assert entered.wait(1)
    for thread in threads[1:]:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(3)

    assert all(not thread.is_alive() for thread in threads)
    assert len(calls) == 1
    assert sorted(results) == [0] * 7 + [1]


def test_tenant_and_schema_boundaries_drop_unknown_shapes():
    collector = (ROOT / "deploy/telemetry/otel-collector.yaml").read_text(encoding="utf-8")
    compose = (ROOT / "deploy/telemetry/docker-compose.yml").read_text(encoding="utf-8")

    for service in ("session-handoff", "sando"):
        assert f"value: {service}" in collector
        assert f"X-Scope-OrgID: {service}" in collector
    assert "match_type: strict" in collector
    assert "filter/session-handoff" in collector
    assert "filter/sando" in collector
    assert 'attributes["event"] != "daily_aggregate"' in collector
    assert 'attributes["aggregate"] != "operation"' in collector
    assert "logging" not in collector
    assert "debug" not in collector
    assert '"127.0.0.1:4318:4318"' in compose
    assert '"127.0.0.1:13000:3000"' in compose

    unknown = _row() | {"aggregate": "unknown", "bogus": "tenant-data"}
    mixed = _row() | {"aggregate": "context_feedback", "feedback_category": "other"}
    for payload in (unknown, mixed):
        with pytest.raises(ValueError):
            telemetry._validate_aggregate(payload)


def test_disable_purge_removes_local_telemetry_data_in_disposable_home(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry.increment_counter(OPERATION, tmp_path, now="2026-08-25T00:00:00Z")
    telemetry.close_day(tmp_path, now="2026-08-26T00:00:00Z")
    state = tmp_path / telemetry.STATE_PATH
    assert any(state.iterdir())

    telemetry.disable(tmp_path, purge=True)

    assert telemetry.load_config(tmp_path) == telemetry.disabled_config()
    assert not any(state.iterdir())
