"""Privacy-safe, closed telemetry event schema."""

from datetime import date
import json
import math
import re
from numbers import Real


EVENTS = frozenset({"operation_summary", "context_feedback"})
OPERATIONS = frozenset({"handoff", "migrate"})
CLIENTS = frozenset({"claude", "codex"})
RESULTS = frozenset({"success", "failure", "fallback"})
FAILURE_STAGES = frozenset(
    {"none", "validation", "control", "source_stop", "conversion", "target_resume", "source_resume", "unknown"}
)
DURATION_BUCKETS = frozenset({"lt_1s", "1_to_5s", "5_to_30s", "30_to_120s", "gte_120s"})
COUNT_BUCKETS = frozenset({"zero", "one", "2_to_5", "6_to_20", "gt_20"})
HANDOFF_BYTES_BUCKETS = frozenset({"lt_4k", "4_to_16k", "16_to_64k", "gte_64k"})
FEEDBACK_CATEGORIES = frozenset({"constraint", "decision", "path", "progress", "rejected_attempt", "other"})
FEEDBACK_SEVERITIES = frozenset({"recoverable", "blocked"})

DENYLIST = frozenset(
    {
        "transcript", "prompt", "handoff_text", "tool_trace", "command", "diff", "file_path", "path",
        "source_session_id", "target_session_id", "session_id", "installation_id", "device_id", "account_id",
        "hostname", "username", "ip_address", "user_agent", "locale", "repository_name", "model_name",
        "metadata", "exception", "exception_text", "stack_trace", "free_text", "credential", "credentials",
        "token", "tokens", "cookie", "cookies", "authorization", "authorization_header", "uuid",
    }
)

_COMMON_FIELDS = frozenset({"schema_version", "event", "day_utc", "plugin_version", "operation", "source_client", "target_client"})
_OPERATION_FIELDS = _COMMON_FIELDS | frozenset(
    {"result", "failure_stage", "duration_bucket", "handoff_bytes_bucket", "redaction_bucket", "dropped_events_bucket", "normalized_fields_bucket"}
)
_FEEDBACK_FIELDS = _COMMON_FIELDS | frozenset({"feedback_category", "feedback_severity"})
_VERSION = re.compile(r"[0-9]+\.[0-9]+\Z")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def bucket_duration(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, Real) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("duration must be a finite non-negative number")
    if seconds < 1:
        return "lt_1s"
    if seconds < 5:
        return "1_to_5s"
    if seconds < 30:
        return "5_to_30s"
    if seconds < 120:
        return "30_to_120s"
    return "gte_120s"


def bucket_count(count):
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("count must be a non-negative integer")
    if count == 0:
        return "zero"
    if count == 1:
        return "one"
    if count <= 5:
        return "2_to_5"
    if count <= 20:
        return "6_to_20"
    return "gt_20"


def bucket_handoff_bytes(size):
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("handoff bytes must be a non-negative integer")
    if size < 4096:
        return "lt_4k"
    if size < 16384:
        return "4_to_16k"
    if size < 65536:
        return "16_to_64k"
    return "gte_64k"


def validate_event(payload):
    if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
        raise ValueError("event must be a JSON object")
    if set(payload) & DENYLIST:
        raise ValueError("privacy-sensitive field")
    if not set(payload) <= (_OPERATION_FIELDS | _FEEDBACK_FIELDS):
        raise ValueError("unknown field")
    if not isinstance(payload.get("event"), str) or payload["event"] not in EVENTS:
        raise ValueError("invalid event")

    fields = _OPERATION_FIELDS if payload["event"] == "operation_summary" else _FEEDBACK_FIELDS
    if set(payload) != fields:
        raise ValueError("wrong event shape")

    if payload["schema_version"] != 1 or isinstance(payload["schema_version"], bool) or not isinstance(payload["schema_version"], int):
        raise ValueError("invalid schema version")
    for field in fields - {"schema_version"}:
        if not isinstance(payload[field], str) or len(payload[field]) > 32:
            raise ValueError("invalid field type or length")

    if not _DAY.fullmatch(payload["day_utc"]):
        raise ValueError("invalid UTC day")
    try:
        date.fromisoformat(payload["day_utc"])
    except ValueError as exc:
        raise ValueError("invalid UTC day") from exc
    if not _VERSION.fullmatch(payload["plugin_version"]):
        raise ValueError("invalid plugin version")
    if payload["operation"] not in OPERATIONS or payload["source_client"] not in CLIENTS or payload["target_client"] not in CLIENTS:
        raise ValueError("invalid shared enum")

    if payload["event"] == "operation_summary":
        enums = {
            "result": RESULTS,
            "failure_stage": FAILURE_STAGES,
            "duration_bucket": DURATION_BUCKETS,
            "handoff_bytes_bucket": HANDOFF_BYTES_BUCKETS,
            "redaction_bucket": COUNT_BUCKETS,
            "dropped_events_bucket": COUNT_BUCKETS,
            "normalized_fields_bucket": COUNT_BUCKETS,
        }
    else:
        enums = {"feedback_category": FEEDBACK_CATEGORIES, "feedback_severity": FEEDBACK_SEVERITIES}
    if any(payload[field] not in values for field, values in enums.items()):
        raise ValueError("invalid enum")
    return payload


def serialize_event(payload):
    validated = validate_event(payload)
    serialized = json.dumps(validated, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > 2048:
        raise ValueError("serialized event exceeds 2 KiB")
    return serialized
