import json

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
