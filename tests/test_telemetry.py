import json
import copy
import hashlib
import http.server
import multiprocessing
import os
import time
import threading
from datetime import datetime, timezone

import pytest

import server.telemetry as telemetry
from server.telemetry import (
    bucket_count,
    bucket_duration,
    bucket_handoff_bytes,
    serialize_event,
    validate_event,
)


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

FEEDBACK = {
    "schema_version": 1,
    "event": "context_feedback",
    "day_utc": "2026-08-25",
    "plugin_version": "0.5",
    "operation": "migrate",
    "source_client": "claude",
    "target_client": "codex",
    "feedback_category": "constraint",
    "feedback_severity": "recoverable",
}


@pytest.mark.parametrize("payload", [OPERATION, FEEDBACK])
def test_valid_events_validate_and_serialize(payload):
    assert validate_event(payload) == payload
    serialized = serialize_event(payload)
    assert json.loads(serialized) == payload
    assert len(serialized.encode("utf-8")) <= 2048


def test_increment_and_close_day_aggregate_daily_rows(tmp_path):
    first = OPERATION.copy()
    second = OPERATION | {"result": "failure", "failure_stage": "conversion"}
    telemetry.write_config(tmp_path, telemetry.enabled_config())

    assert telemetry.increment_counter(first, tmp_path, now="2026-08-25T23:00:00Z") == 1
    assert telemetry.increment_counter(first, tmp_path, now="2026-08-25T23:30:00Z") == 2
    assert telemetry.increment_counter(second, tmp_path, now="2026-08-25T23:45:00Z") == 1

    closed = telemetry.close_day(tmp_path, now="2026-08-26T00:00:00Z")

    assert [row["count"] for row in closed] == [2, 1]
    assert all(row["event"] == "daily_aggregate" for row in closed)
    assert not (tmp_path / telemetry.STATE_PATH / "telemetry-counters.json").exists()


def test_queue_is_private_atomic_and_bounded_by_age_count_and_bytes(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    row = telemetry._aggregate_row(OPERATION, 1)
    telemetry._store_queue(tmp_path, [row] * 300)

    queue = tmp_path / telemetry.STATE_PATH / "telemetry-queue.jsonl"
    rows = telemetry.load_batch(tmp_path, now="2026-08-10T00:00:00Z", limit=256)

    assert queue.stat().st_mode & 0o777 == 0o600
    assert len(rows) <= 256
    assert queue.stat().st_size <= 256 * 1024
    assert all(row["day_utc"] >= "2026-08-03" for row in rows)
    assert not list(queue.parent.glob(".telemetry-queue.jsonl.*.tmp"))


def test_load_batch_is_oldest_first_and_limited_to_32(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    row = telemetry._aggregate_row(OPERATION, 1)
    telemetry._store_queue(tmp_path, [row] * 40)

    batch = telemetry.load_batch(tmp_path, limit=32, now="2026-08-26T00:00:00Z")

    assert len(batch) == 32
    assert batch == sorted(batch, key=lambda row: row["day_utc"])


def test_physical_batch_reader_does_not_parse_rows_after_bounded_snapshot(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    row = telemetry._aggregate_row(OPERATION, 1)
    line = json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    queue = tmp_path / telemetry.STATE_PATH / telemetry._QUEUE_NAME
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_bytes(line * telemetry.MAX_UPLOAD_ROWS + (b"x" * 512 + b"\n") * 224)
    queue.chmod(0o600)

    with telemetry._state_lock(tmp_path) as (_config_directory, _config_fd, state_directory, state_fd):
        batch = telemetry._read_queue_snapshot_locked(state_directory, state_fd, telemetry.MAX_UPLOAD_ROWS)

    assert len(batch) == telemetry.MAX_UPLOAD_ROWS


def test_load_batch_purges_stale_queue_rows_without_counters(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    stale = telemetry._aggregate_row(OPERATION | {"day_utc": "2026-08-01"}, 1)
    fresh = telemetry._aggregate_row(OPERATION | {"day_utc": "2026-08-26"}, 1)
    telemetry._store_queue(tmp_path, [stale] * 33 + [fresh])

    batch = telemetry.load_batch(tmp_path, now="2026-08-26T00:00:00Z")

    assert batch == [fresh]
    assert telemetry._read_queue(tmp_path) == [fresh]


def test_load_batch_purges_stale_rows_after_first_upload_batch(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    fresh = telemetry._aggregate_row(OPERATION | {"day_utc": "2026-08-26"}, 1)
    stale = telemetry._aggregate_row(OPERATION | {"day_utc": "2026-08-01"}, 1)
    telemetry._store_queue(tmp_path, [fresh] * 33 + [stale])

    telemetry.load_batch(tmp_path, now="2026-08-26T00:00:00Z")

    assert stale not in telemetry._read_queue(tmp_path)


def test_load_batch_purges_stale_rows_anywhere_within_bounded_queue(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    fresh = telemetry._aggregate_row(OPERATION | {"day_utc": "2026-08-26"}, 1)
    stale = telemetry._aggregate_row(OPERATION | {"day_utc": "2026-08-01"}, 1)
    queue = tmp_path / telemetry.STATE_PATH / telemetry._QUEUE_NAME
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in [fresh] * 32 + [stale, fresh]),
        encoding="utf-8",
    )
    queue.chmod(0o600)

    batch = telemetry.load_batch(tmp_path, now="2026-08-26T00:00:00Z")

    assert len(batch) == telemetry.MAX_UPLOAD_ROWS
    assert stale not in telemetry._read_queue(tmp_path)


def test_copying_batch_gets_fresh_nonce_and_cannot_ack_replayed_batch(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    row = telemetry._aggregate_row(OPERATION, 1)
    telemetry._store_queue(tmp_path, [row])
    batch = telemetry.load_batch(tmp_path)
    replay = copy.copy(batch)

    assert replay.queue_token != batch.queue_token
    telemetry.ack_batch(tmp_path, batch, digest=telemetry._batch_digest(batch))
    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.ack_batch(tmp_path, replay, digest=telemetry._batch_digest(replay))


def test_load_batch_reads_only_bounded_unbuffered_queue_snapshot(tmp_path, monkeypatch):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    row = telemetry._aggregate_row(OPERATION, 1)
    telemetry._store_queue(tmp_path, [row] * 40)
    queue_fds = set()
    observed = {"bytes": 0, "lines": 0}
    original_open = telemetry._open_relative
    original_read = telemetry.os.read

    def capture_open(directory_fd, directory, name, flags):
        descriptor = original_open(directory_fd, directory, name, flags)
        if name == telemetry._QUEUE_NAME:
            queue_fds.add(descriptor)
        return descriptor

    def capture_read(descriptor, size):
        data = original_read(descriptor, size)
        if descriptor in queue_fds:
            observed["bytes"] += len(data)
            observed["lines"] += data.count(b"\n")
        return data

    monkeypatch.setattr(telemetry, "_open_relative", capture_open)
    monkeypatch.setattr(telemetry.os, "read", capture_read)
    monkeypatch.setattr(telemetry.os, "fdopen", lambda *_args, **_kwargs: pytest.fail("buffered queue reader"))

    with telemetry._state_lock(tmp_path) as (_config_directory, _config_fd, state_directory, state_fd):
        batch = telemetry._read_queue_snapshot_locked(state_directory, state_fd, telemetry.MAX_UPLOAD_ROWS)

    assert len(batch) == telemetry.MAX_UPLOAD_ROWS
    assert observed["lines"] <= telemetry.MAX_UPLOAD_ROWS
    assert observed["bytes"] <= telemetry.MAX_UPLOAD_BYTES


def test_otlp_mapping_contains_only_fixed_resource_and_row_attributes():
    row = OPERATION | {"event": "daily_aggregate", "aggregate": "operation", "count": 3}

    payload = telemetry.to_otlp_logs([row])

    assert payload["resourceLogs"][0]["resource"] == {
        "attributes": [{"key": "service.name", "value": {"stringValue": "session-handoff"}}]
    }
    record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert record["body"] == {"stringValue": "session_handoff.daily_aggregate"}
    attributes = {item["key"]: item["value"] for item in record["attributes"]}
    assert attributes["count"] == {"intValue": "3"}
    assert attributes["operation"] == {"stringValue": "handoff"}
    assert "consented_at" not in attributes


def test_ack_batch_removes_only_accepted_oldest_rows(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    row = telemetry._aggregate_row(OPERATION, 1)
    telemetry._store_queue(tmp_path, [row, row | {"count": 2}])
    batch = telemetry.load_batch(tmp_path, limit=2, now="2026-08-03T00:00:00Z")

    telemetry.ack_batch(
        tmp_path,
        batch,
        accepted=1,
        digest=telemetry._batch_digest(batch),
    )

    assert telemetry.load_batch(tmp_path, limit=2, now="2026-08-03T00:00:00Z") == batch[1:]


def test_ack_batch_rejects_legacy_plain_list_without_mutating_queue(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    row = telemetry._aggregate_row(OPERATION, 1)
    telemetry._store_queue(tmp_path, [row, row])
    batch = telemetry.load_batch(tmp_path, limit=1, now="2026-08-03T00:00:00Z")

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.ack_batch(tmp_path, list(batch), accepted=1)

    assert telemetry.load_batch(tmp_path, now="2026-08-03T00:00:00Z") == [row, row]


def _increment_process(home, result):
    result.put(telemetry.increment_counter(OPERATION, home, now="2026-08-25T00:00:00Z"))


def _claim_batch_process(home, result):
    with telemetry._state_lock(home) as (_config_directory, _config_fd, state_directory, state_fd):
        batch = telemetry._load_batch_locked(state_directory, state_fd, telemetry.MAX_UPLOAD_ROWS, "2026-08-26T00:00:00Z")
        claimed = telemetry._claim_batch_lease_locked(state_directory, state_fd, batch)
        result.put(claimed is not None)


def test_two_process_increments_are_not_lost(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    context = multiprocessing.get_context("fork")
    result = context.Queue()
    processes = [context.Process(target=_increment_process, args=(tmp_path, result)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(3)
    assert all(not process.is_alive() for process in processes)
    assert sorted(result.get(timeout=1) for _ in processes) == [1, 2]


def test_two_process_flushes_claim_one_persistent_batch_lease(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry._store_queue(tmp_path, [telemetry._aggregate_row(OPERATION, 1)])
    context = multiprocessing.get_context("fork")
    result = context.Queue()
    processes = [context.Process(target=_claim_batch_process, args=(tmp_path, result)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(3)
    assert all(not process.is_alive() for process in processes)
    assert sorted(result.get(timeout=1) for _ in processes) == [False, True]


def test_queue_limits_are_checked_before_json_parse(tmp_path, monkeypatch):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    queue = tmp_path / telemetry.STATE_PATH / telemetry._QUEUE_NAME
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_bytes(b"x" * (telemetry.MAX_QUEUE_BYTES + 1))

    def unexpected_parse(*_args, **_kwargs):
        raise AssertionError("queue was parsed before the byte limit")

    monkeypatch.setattr(telemetry.json, "loads", unexpected_parse)
    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.load_batch(tmp_path)


def test_queue_row_limit_is_checked_before_json_parse(tmp_path, monkeypatch):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    row = telemetry._aggregate_row(OPERATION, 1)
    queue = tmp_path / telemetry.STATE_PATH / telemetry._QUEUE_NAME
    queue.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
    queue.write_text(line * (telemetry.MAX_QUEUE_ROWS + 1), encoding="utf-8")

    def unexpected_parse(*_args, **_kwargs):
        raise AssertionError("queue was parsed before the row limit")

    monkeypatch.setattr(telemetry.json, "loads", unexpected_parse)
    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.load_batch(tmp_path)


def test_load_batch_rejects_world_readable_queue_before_reading(tmp_path, monkeypatch):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    queue = tmp_path / telemetry.STATE_PATH / telemetry._QUEUE_NAME
    telemetry._store_queue(tmp_path, [telemetry._aggregate_row(OPERATION, 1)])
    queue.chmod(0o644)
    monkeypatch.setattr(
        telemetry,
        "_read_limited_descriptor",
        lambda *_args, **_kwargs: pytest.fail("private queue was read"),
    )

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.load_batch(tmp_path)
    assert queue.stat().st_mode & 0o777 == 0o644


def test_increment_counter_rejects_world_readable_counters_before_reading(tmp_path, monkeypatch):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry.increment_counter(OPERATION, tmp_path)
    counters = tmp_path / telemetry.STATE_PATH / telemetry._COUNTERS_NAME
    counters.chmod(0o644)
    original_read = telemetry._read_limited_descriptor

    def reject_state_read(descriptor, limit, label):
        if label.name == telemetry._COUNTERS_NAME:
            pytest.fail("private counters were read")
        return original_read(descriptor, limit, label)

    monkeypatch.setattr(
        telemetry,
        "_read_limited_descriptor",
        reject_state_read,
    )

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.increment_counter(OPERATION, tmp_path)
    assert counters.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("malformed", ["day", "event", "key"])
def test_close_day_rejects_malformed_counters_without_stale_upload(tmp_path, malformed):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    event = OPERATION.copy()
    entry = {"key": telemetry._event_key(event), "event": event, "count": 1}
    counters = {"schema_version": 1, "dropped_events": 0, "days": {event["day_utc"]: [entry]}}
    if malformed == "day":
        counters["days"] = {"not-a-day": [entry]}
    elif malformed == "event":
        entry["event"] = event | {"event": "unknown"}
    else:
        entry["key"] = "mismatched-key"
    counter_path = tmp_path / telemetry.STATE_PATH / telemetry._COUNTERS_NAME
    counter_path.parent.mkdir(parents=True)
    counter_path.write_text(json.dumps(counters), encoding="utf-8")
    counter_path.chmod(0o600)
    row = telemetry._aggregate_row(OPERATION, 1)
    telemetry._store_queue(tmp_path, [row])
    before = telemetry.load_batch(tmp_path, now="2026-08-26T00:00:00Z")

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.close_day(tmp_path, now="2026-08-26T00:00:00Z")

    assert telemetry.load_batch(tmp_path, now="2026-08-26T00:00:00Z") == before


def test_increment_counter_rejects_duplicate_counter_entries_before_write(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    event = OPERATION.copy()
    entry = {"key": telemetry._event_key(event), "event": event, "count": 1}
    counters = {"schema_version": 1, "dropped_events": 0, "days": {event["day_utc"]: [entry, entry.copy()]}}
    counter_path = tmp_path / telemetry.STATE_PATH / telemetry._COUNTERS_NAME
    counter_path.parent.mkdir(parents=True)
    counter_path.write_text(json.dumps(counters), encoding="utf-8")
    counter_path.chmod(0o600)

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.increment_counter(event, tmp_path)

    assert json.loads(counter_path.read_text(encoding="utf-8")) == counters


def test_increment_counter_reports_corrupt_config_as_controlled_error(tmp_path):
    config_path = tmp_path / telemetry.CONFIG_PATH
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{", encoding="utf-8")

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.increment_counter(OPERATION, tmp_path)


@pytest.mark.parametrize("mutation", ["unknown_top_level", "missing_dropped_events", "missing_bucket"])
def test_close_day_rejects_invalid_counter_shape_before_processing(tmp_path, mutation):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    event = OPERATION.copy()
    entry = {"key": telemetry._event_key(event), "event": event, "count": 1}
    counters = {"schema_version": 1, "dropped_events": 0, "days": {event["day_utc"]: [entry]}}
    if mutation == "unknown_top_level":
        counters["unexpected"] = True
    elif mutation == "missing_dropped_events":
        del counters["dropped_events"]
    else:
        event.pop("duration_bucket")
        entry["key"] = telemetry._event_key(event)
        entry["event"] = event
    counter_path = tmp_path / telemetry.STATE_PATH / telemetry._COUNTERS_NAME
    counter_path.parent.mkdir(parents=True)
    counter_path.write_text(json.dumps(counters), encoding="utf-8")
    counter_path.chmod(0o600)

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.close_day(tmp_path, now="2026-08-26T00:00:00Z")


def test_ack_batch_rejects_stale_duplicate_row_after_aba(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    row = telemetry._aggregate_row(OPERATION, 1)
    telemetry._store_queue(tmp_path, [row, row])
    batch = telemetry.load_batch(tmp_path, limit=1, now="2026-08-03T00:00:00Z")
    digest = telemetry._batch_digest(batch)

    telemetry.ack_batch(tmp_path, batch, accepted=1, digest=digest)

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.ack_batch(tmp_path, batch, accepted=1, digest=digest)
    assert telemetry.load_batch(tmp_path, now="2026-08-03T00:00:00Z") == [row]


def test_ack_batch_rejects_stale_snapshot_after_queue_aba(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    row_a = telemetry._aggregate_row(OPERATION, 1)
    row_b = row_a | {"count": 2}
    telemetry._store_queue(tmp_path, [row_a])
    batch = telemetry.load_batch(tmp_path, now="2026-08-26T00:00:00Z")
    digest = telemetry._batch_digest(batch)

    telemetry._store_queue(tmp_path, [row_b])
    telemetry._store_queue(tmp_path, [row_a])

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.ack_batch(tmp_path, batch, accepted=1, digest=digest)
    assert telemetry._read_queue(tmp_path) == [row_a]

    current = telemetry.load_batch(tmp_path, now="2026-08-26T00:00:00Z")
    telemetry.ack_batch(tmp_path, current, accepted=1, digest=telemetry._batch_digest(current))
    assert telemetry._read_queue(tmp_path) == []


@pytest.mark.parametrize(
    "field_value",
    [
        ("day_utc", 20260825),
        ("schema_version", True),
    ],
)
def test_load_batch_rejects_invalid_queue_types_with_controlled_error(tmp_path, field_value):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    row = telemetry._aggregate_row(OPERATION, 1)
    row[field_value[0]] = field_value[1]
    queue = tmp_path / telemetry.STATE_PATH / telemetry._QUEUE_NAME
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(json.dumps(row) + "\n", encoding="utf-8")
    queue.chmod(0o600)

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.load_batch(tmp_path)


def test_ack_batch_requires_exact_batch_identity(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    row = telemetry._aggregate_row(OPERATION, 1)
    telemetry._store_queue(tmp_path, [row, row | {"count": 2}])
    batch = telemetry.load_batch(tmp_path, limit=2, now="2026-08-03T00:00:00Z")

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.ack_batch(
            tmp_path,
            batch,
            accepted=1,
            digest="stale-batch-digest",
        )

    assert telemetry.load_batch(tmp_path, limit=2, now="2026-08-03T00:00:00Z") == batch


def test_build_request_rejects_more_than_32_rows(tmp_path):
    rows = [telemetry._aggregate_row(OPERATION, 1)] * 33

    with pytest.raises(ValueError):
        telemetry.build_request(telemetry.ENDPOINT, rows)


def test_counter_count_is_capped_at_10000(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    event = OPERATION.copy()
    assert telemetry.increment_counter(event, tmp_path) == 1
    counters = {
        "schema_version": 1,
        "dropped_events": 0,
        "days": {event["day_utc"]: [{"key": telemetry._event_key(event), "event": event, "count": 10000}]},
    }
    counter_path = tmp_path / telemetry.STATE_PATH / telemetry._COUNTERS_NAME
    counter_path.write_text(json.dumps(counters), encoding="utf-8")
    assert telemetry.increment_counter(event, tmp_path) == 10000


def test_detached_flush_closes_prior_utc_day_and_preserves_current_counter(tmp_path, monkeypatch):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry.increment_counter(OPERATION, tmp_path, now="2026-08-25T23:00:00Z")
    telemetry.increment_counter(OPERATION | {"day_utc": "2026-08-26"}, tmp_path, now="2026-08-26T00:00:00Z")
    called = []
    monkeypatch.setattr(telemetry, "flush_queue", lambda home: called.append(home) or 0)

    assert telemetry.detached_flush(
        tmp_path / telemetry.STATE_PATH / telemetry._QUEUE_NAME,
        tmp_path / telemetry.CONFIG_PATH,
        now="2026-08-26T00:00:00Z",
    ) == 0

    assert called == [tmp_path]
    assert len(telemetry.load_batch(tmp_path, now="2026-08-26T00:00:00Z")) == 1
    counters = json.loads((tmp_path / telemetry.STATE_PATH / telemetry._COUNTERS_NAME).read_text())
    assert set(counters["days"]) == {"2026-08-26"}


def test_close_day_discards_stale_only_counters(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry.increment_counter(OPERATION | {"day_utc": "2026-08-01"}, tmp_path, now="2026-08-01T00:00:00Z")

    assert telemetry.close_day(tmp_path, now="2026-08-10T00:00:00Z") == []
    assert not (tmp_path / telemetry.STATE_PATH / telemetry._COUNTERS_NAME).exists()


def test_close_day_deduplicates_rows_after_queue_commit_retry(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    row = telemetry._aggregate_row(OPERATION, 1)
    telemetry._store_queue(tmp_path, [row])
    telemetry.increment_counter(OPERATION, tmp_path, now="2026-08-25T00:00:00Z")

    telemetry.close_day(tmp_path, now="2026-08-26T00:00:00Z")

    assert telemetry.load_batch(tmp_path, now="2026-08-26T00:00:00Z") == [row]


def test_config_read_is_capped_before_json_allocation(tmp_path, monkeypatch):
    path = tmp_path / telemetry.CONFIG_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{" + b"x" * (telemetry.MAX_CONFIG_BYTES + 1))
    monkeypatch.setattr(telemetry.json, "load", lambda *_args, **_kwargs: pytest.fail("uncapped json.load"))

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.load_config(tmp_path)


def test_config_hardlink_is_rejected_before_read_or_chmod(tmp_path):
    telemetry.write_config(tmp_path, telemetry.disabled_config())
    config = tmp_path / telemetry.CONFIG_PATH
    external = tmp_path / "external-telemetry.json"
    os.link(config, external)
    config.chmod(0o644)

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.load_config(tmp_path)
    assert config.stat().st_mode & 0o777 == 0o644
    assert external.stat().st_mode & 0o777 == 0o644


def test_mutated_batch_is_rejected_before_digest_validation(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    row = telemetry._aggregate_row(OPERATION, 1)
    telemetry._store_queue(tmp_path, [row])
    batch = telemetry.load_batch(tmp_path)
    digest = telemetry._batch_digest(batch)
    batch.append(row)

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.ack_batch(tmp_path, batch, accepted=1, digest=digest)
    assert telemetry.load_batch(tmp_path) == [row]


def test_empty_batch_is_rejected_before_digest_validation(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    batch = telemetry._TelemetryBatch([])

    with pytest.raises(telemetry.TelemetryConfigError):
        telemetry.ack_batch(tmp_path, batch, digest="not-a-digest")


def test_empty_success_response_does_not_ack_batch(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry.increment_counter(OPERATION, tmp_path, now="2026-08-25T00:00:00Z")
    telemetry.close_day(tmp_path, now="2026-08-26T00:00:00Z")
    closed = {"value": False}

    class Response:
        status = 204

        def read(self, _size):
            return b""

        def close(self):
            closed["value"] = True

    assert telemetry.flush_queue(tmp_path, opener=lambda *_args, **_kwargs: Response()) == 0
    assert closed["value"]
    assert telemetry.load_batch(tmp_path, now="2026-08-26T00:00:00Z")


@pytest.mark.parametrize(
    "body",
    [
        b'{"partialSuccess":{"rejectedLogRecords":true}}',
        b'{"partialSuccess":{"rejectedLogRecords":-1}}',
        b'{"partialSuccess":{"rejectedLogRecords":2}}',
        b'{"partialSuccess":"oops"}',
        b'{"partialSuccess":{},"partialSuccess":{}}',
        b'[]',
    ],
)
def test_invalid_acceptance_response_does_not_ack_batch(tmp_path, body):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry.increment_counter(OPERATION, tmp_path, now="2026-08-25T00:00:00Z")
    telemetry.close_day(tmp_path, now="2026-08-26T00:00:00Z")

    class Response:
        status = 200

        def read(self, _size):
            if getattr(self, "done", False):
                return b""
            self.done = True
            return body

        def close(self):
            return None

    assert telemetry.flush_queue(tmp_path, opener=lambda *_args, **_kwargs: Response()) == 0
    assert telemetry.load_batch(tmp_path, now="2026-08-26T00:00:00Z")


def test_flush_records_and_clears_last_flush_error(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry.increment_counter(OPERATION, tmp_path, now="2026-08-25T00:00:00Z")
    telemetry.close_day(tmp_path, now="2026-08-26T00:00:00Z")

    class ForbiddenResponse:
        status = 403

        def read(self, _size):
            return b""

        def close(self):
            return None

    assert telemetry.flush_queue(
        tmp_path, opener=lambda *_args, **_kwargs: ForbiddenResponse(), now="2026-08-26T00:00:00Z"
    ) == 0
    error = telemetry.last_flush_error(tmp_path)
    assert error["error"] == "_FlushStatusError"
    assert error["status"] == 403
    assert error["at"] == "2026-08-26T00:00:00Z"
    assert "accepted" not in json.dumps(error)

    class OkResponse:
        status = 200

        def read(self, size):
            if getattr(self, "done", False):
                return b""
            self.done = True
            return b'{"partialSuccess":{}}'

        def close(self):
            return None

    assert telemetry.flush_queue(
        tmp_path, opener=lambda *_args, **_kwargs: OkResponse(), now="2026-08-26T00:00:00Z"
    ) == 1
    assert telemetry.last_flush_error(tmp_path) is None


def test_flush_closes_response_reads_bounded_body_and_avoids_eager_getcode(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry.increment_counter(OPERATION, tmp_path, now="2026-08-25T00:00:00Z")
    telemetry.close_day(tmp_path, now="2026-08-26T00:00:00Z")
    closed = {"value": False}

    class Response:
        status = 200
        done = False

        def read(self, size):
            assert size <= telemetry.MAX_RESPONSE_BYTES
            if self.done:
                return b""
            self.done = True
            return b'{"partialSuccess":{}}'

        def getcode(self):
            raise AssertionError("getcode fallback evaluated eagerly")

        def close(self):
            closed["value"] = True

    response = Response()
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        return response

    assert telemetry.flush_queue(tmp_path, opener=opener) == 1
    assert closed["value"]
    assert requests[0].headers["Idempotency-key"] == hashlib.sha256(requests[0].data).hexdigest()


def test_two_flushes_claim_one_batch(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry.increment_counter(OPERATION, tmp_path, now="2026-08-25T00:00:00Z")
    telemetry.close_day(tmp_path, now="2026-08-26T00:00:00Z")
    entered = threading.Event()
    release = threading.Event()
    calls = []
    call_lock = threading.Lock()

    class Response:
        status = 200

        def read(self, _size):
            if getattr(self, "done", False):
                return b""
            self.done = True
            return b'{"partialSuccess":{}}'

        def close(self):
            return None

    def opener(_request, **_kwargs):
        with call_lock:
            calls.append(1)
            first = len(calls) == 1
        if first:
            entered.set()
            release.wait(2)
        return Response()

    results = []
    threads = [threading.Thread(target=lambda: results.append(telemetry.flush_queue(tmp_path, opener=opener))) for _ in range(2)]
    threads[0].start()
    assert entered.wait(1)
    threads[1].start()
    time.sleep(0.05)
    release.set()
    for thread in threads:
        thread.join(2)
    assert sorted(results) == [0, 1]
    assert len(calls) == 1


def test_flush_releases_state_lock_during_http_and_preserves_new_rows(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    old = telemetry._aggregate_row(OPERATION, 1)
    new = old | {"count": 2}
    telemetry._store_queue(tmp_path, [old])
    entered = threading.Event()
    release = threading.Event()
    incremented = threading.Event()

    class Response:
        status = 200

        def read(self, _size):
            if getattr(self, "done", False):
                return b""
            self.done = True
            return b'{"partialSuccess":{}}'

        def close(self):
            return None

    def opener(_request, **_kwargs):
        entered.set()
        release.wait(2)
        return Response()

    flush = threading.Thread(target=lambda: telemetry.flush_queue(tmp_path, opener=opener))
    flush.start()
    assert entered.wait(1)
    writer = threading.Thread(target=lambda: (telemetry._store_queue(tmp_path, [old, new]), incremented.set()))
    writer.start()
    assert incremented.wait(1)
    release.set()
    flush.join(2)
    writer.join(2)

    assert not flush.is_alive()
    assert telemetry.load_batch(tmp_path, limit=2, now="2026-08-26T00:00:00Z") == [old, new]
    assert incremented.is_set()


def test_batch_nonce_is_unique_and_not_sent(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    row = telemetry._aggregate_row(OPERATION, 1)
    telemetry._store_queue(tmp_path, [row])

    first = telemetry.load_batch(tmp_path)
    second = telemetry.load_batch(tmp_path)
    request = telemetry.build_request(telemetry.ENDPOINT, first)

    assert first.queue_token != second.queue_token
    assert first.queue_token.encode() not in request.data


def test_flush_rejects_same_origin_redirect(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry._store_queue(tmp_path, [telemetry._aggregate_row(OPERATION, 1)])
    endpoint = telemetry.ENDPOINT

    class Response:
        status = 200
        done = False

        def geturl(self):
            return endpoint + "/redirected"

        def read(self, _size):
            if self.done:
                return b""
            self.done = True
            return b'{"partialSuccess":{}}'

        def close(self):
            return None

    assert telemetry.flush_queue(tmp_path, opener=lambda *_args, **_kwargs: Response()) == 0
    assert telemetry.load_batch(tmp_path)


def test_flush_rejects_response_host_change(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry.increment_counter(OPERATION, tmp_path, now="2026-08-25T00:00:00Z")
    telemetry.close_day(tmp_path, now="2026-08-26T00:00:00Z")

    class Response:
        status = 200

        def geturl(self):
            return "https://other.example/v1/logs"

        def close(self):
            return None

    assert telemetry.flush_queue(tmp_path, opener=lambda *_args, **_kwargs: Response()) == 0
    assert telemetry.load_batch(tmp_path, now="2026-08-26T00:00:00Z")


def test_flush_uses_total_deadline_for_response_read(tmp_path, monkeypatch):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    telemetry.increment_counter(OPERATION, tmp_path, now="2026-08-25T00:00:00Z")
    telemetry.close_day(tmp_path, now="2026-08-26T00:00:00Z")
    clock = iter((100.0, 100.0, 100.0, 104.0))
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: next(clock))

    class Response:
        status = 200

        def read(self, _size):
            raise AssertionError("expired response was read")

        def close(self):
            return None

    assert telemetry.flush_queue(tmp_path, opener=lambda *_args, **_kwargs: Response()) == 0


class _TelemetryHandler(http.server.BaseHTTPRequestHandler):
    response_status = 200
    response_body = b'{"partialSuccess": {"rejectedLogRecords": "1"}}'
    requests = []
    block = None

    def do_POST(self):  # noqa: N802
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        self.__class__.requests.append((dict(self.headers), body))
        if self.__class__.block is not None:
            self.__class__.block.wait(5)
        self.send_response(self.__class__.response_status)
        self.end_headers()
        self.wfile.write(self.__class__.response_body)

    def log_message(self, *_args):
        return


def _telemetry_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TelemetryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_flush_queue_supports_partial_acceptance_and_leaves_remainder(tmp_path):
    server, thread = _telemetry_server()
    try:
        telemetry.write_config(tmp_path, telemetry.enabled_config())
        for day in ("2026-08-01", "2026-08-02"):
            telemetry.increment_counter(OPERATION | {"day_utc": day}, tmp_path, now="2026-08-03T00:00:00Z")
            telemetry.close_day(tmp_path, now="2026-08-03T00:00:00Z")
        original_endpoint = telemetry.ENDPOINT
        telemetry.ENDPOINT = f"http://127.0.0.1:{server.server_port}/logs"
        try:
            config = telemetry.enabled_config()
            config["endpoint"] = telemetry.ENDPOINT
            telemetry.write_config(tmp_path, config)
            assert telemetry.flush_queue(tmp_path, now="2026-08-03T00:00:00Z") == 1
        finally:
            telemetry.ENDPOINT = original_endpoint
        assert len(telemetry.load_batch(tmp_path, limit=2, now="2026-08-03T00:00:00Z")) == 1
        sent_headers, _sent_body = _TelemetryHandler.requests[-1]
        assert sent_headers["User-Agent"] == telemetry._USER_AGENT
        assert "python-urllib" not in sent_headers["User-Agent"].lower()
    finally:
        server.shutdown()
        thread.join(2)


def test_flush_queue_retries_next_invocation_and_timeout_is_bounded(tmp_path):
    server, thread = _telemetry_server()
    block = threading.Event()
    _TelemetryHandler.block = block
    try:
        telemetry.write_config(tmp_path, telemetry.enabled_config())
        telemetry.increment_counter(OPERATION, tmp_path, now="2026-08-25T00:00:00Z")
        telemetry.close_day(tmp_path, now="2026-08-26T00:00:00Z")
        original_endpoint = telemetry.ENDPOINT
        telemetry.ENDPOINT = f"http://127.0.0.1:{server.server_port}/logs"
        config = telemetry.enabled_config()
        config["endpoint"] = telemetry.ENDPOINT
        telemetry.write_config(tmp_path, config)
        started = datetime.now(timezone.utc)
        try:
            assert telemetry.flush_queue(tmp_path) == 0
        finally:
            telemetry.ENDPOINT = original_endpoint
        assert (datetime.now(timezone.utc) - started).total_seconds() < 4
        assert telemetry.load_batch(tmp_path, limit=1)
    finally:
        block.set()
        _TelemetryHandler.block = None
        server.shutdown()
        thread.join(2)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event", "unknown"),
        ("operation", "unknown"),
        ("source_client", "unknown"),
        ("target_client", "unknown"),
        ("result", "unknown"),
        ("failure_stage", "invalid"),
        ("duration_bucket", "unknown"),
        ("handoff_bytes_bucket", "unknown"),
        ("redaction_bucket", "unknown"),
        ("dropped_events_bucket", "unknown"),
        ("normalized_fields_bucket", "unknown"),
    ],
)
def test_operation_rejects_every_invalid_enum(field, value):
    payload = OPERATION.copy()
    payload[field] = value
    with pytest.raises(ValueError):
        validate_event(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [("feedback_category", "unknown"), ("feedback_severity", "unknown")],
)
def test_feedback_rejects_every_invalid_enum(field, value):
    payload = FEEDBACK.copy()
    payload[field] = value
    with pytest.raises(ValueError):
        validate_event(payload)


def test_rejects_unknown_fields_and_mixed_event_shapes():
    unknown = OPERATION | {"unexpected": "value"}
    mixed = FEEDBACK | {"result": "success"}
    with pytest.raises(ValueError):
        validate_event(unknown)
    with pytest.raises(ValueError):
        validate_event(mixed)


@pytest.mark.parametrize("value", [{"nested": "value"}, ["nested"]])
def test_rejects_nested_objects_and_arrays(value):
    payload = OPERATION.copy()
    payload["operation"] = value
    with pytest.raises(ValueError):
        validate_event(payload)


@pytest.mark.parametrize(
    ("base", "field"),
    [(payload, field) for payload in (OPERATION, FEEDBACK) for field in payload],
)
@pytest.mark.parametrize("value", [{"nested": "value"}, ["nested"], True])
def test_rejects_non_scalar_fields_with_value_error(base, field, value):
    payload = base.copy()
    payload[field] = value
    with pytest.raises(ValueError):
        validate_event(payload)


def test_rejects_overlong_strings():
    payload = OPERATION.copy()
    payload["plugin_version"] = "x" * 33
    with pytest.raises(ValueError):
        validate_event(payload)


SENSITIVE_FIELDS = (
    "transcript",
    "prompt",
    "handoff_text",
    "tool_trace",
    "command",
    "diff",
    "file_path",
    "path",
    "source_session_id",
    "target_session_id",
    "session_id",
    "installation_id",
    "device_id",
    "account_id",
    "hostname",
    "username",
    "ip_address",
    "user_agent",
    "locale",
    "repository_name",
    "model_name",
    "metadata",
    "exception",
    "exception_text",
    "stack_trace",
    "free_text",
    "credential",
    "credentials",
    "token",
    "tokens",
    "cookie",
    "cookies",
    "authorization",
    "authorization_header",
    "uuid",
)


@pytest.mark.parametrize("field", SENSITIVE_FIELDS)
def test_rejects_every_privacy_sensitive_field(field):
    with pytest.raises(ValueError):
        validate_event(OPERATION | {field: "private value"})


@pytest.mark.parametrize(
    "day_utc",
    ["2026-02-30", "2026-8-25", "2026-08-25T00:00:00Z", "٢٠٢٦-٠٨-٢٥"],
)
def test_rejects_invalid_utc_days(day_utc):
    with pytest.raises(ValueError):
        validate_event(OPERATION | {"day_utc": day_utc})


@pytest.mark.parametrize("schema_version", [0, 2, True, 1.0, "1", None])
def test_rejects_invalid_schema_versions(schema_version):
    with pytest.raises(ValueError):
        validate_event(OPERATION | {"schema_version": schema_version})


def test_rejects_non_ascii_plugin_version():
    with pytest.raises(ValueError):
        validate_event(OPERATION | {"plugin_version": "١.٢"})


@pytest.mark.parametrize(
    ("bucket", "value"),
    [
        (bucket_duration, -1),
        (bucket_duration, True),
        (bucket_duration, "1"),
        (bucket_duration, float("inf")),
        (bucket_duration, float("nan")),
        (bucket_count, -1),
        (bucket_count, True),
        (bucket_count, 1.0),
        (bucket_count, "1"),
        (bucket_handoff_bytes, -1),
        (bucket_handoff_bytes, True),
        (bucket_handoff_bytes, 1.0),
        (bucket_handoff_bytes, "1"),
    ],
)
def test_rejects_invalid_bucket_inputs(bucket, value):
    with pytest.raises(ValueError):
        bucket(value)


def test_rejects_serialized_payload_over_2_kib(monkeypatch):
    monkeypatch.setattr(telemetry.json, "dumps", lambda *args, **kwargs: "x" * 2049)
    with pytest.raises(ValueError):
        serialize_event(OPERATION.copy())


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "lt_1s"),
        (0.999, "lt_1s"),
        (1, "1_to_5s"),
        (4.999, "1_to_5s"),
        (5, "5_to_30s"),
        (29.999, "5_to_30s"),
        (30, "30_to_120s"),
        (119.999, "30_to_120s"),
        (120, "gte_120s"),
    ],
)
def test_bucket_duration(seconds, expected):
    assert bucket_duration(seconds) == expected


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "zero"),
        (1, "one"),
        (2, "2_to_5"),
        (5, "2_to_5"),
        (6, "6_to_20"),
        (20, "6_to_20"),
        (21, "gt_20"),
    ],
)
def test_bucket_count(count, expected):
    assert bucket_count(count) == expected


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "lt_4k"),
        (4095, "lt_4k"),
        (4096, "4_to_16k"),
        (16383, "4_to_16k"),
        (16384, "16_to_64k"),
        (65535, "16_to_64k"),
        (65536, "gte_64k"),
    ],
)
def test_bucket_handoff_bytes(size, expected):
    assert bucket_handoff_bytes(size) == expected
