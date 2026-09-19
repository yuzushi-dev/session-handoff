import pytest
from server import telemetry as t


def test_schema4_status_row_has_exact_shape_and_timestamp():
    row = t.installation_status_event("a" * 32, "observed", "2026-09-19T12:34:56Z")
    assert set(row) == {
        "schema_version", "event", "day_utc", "plugin_version", "origin",
        "installation_id", "lifecycle_action", "observed_at",
    }
    assert row["schema_version"] == 4
    t.validate_event(row)


@pytest.mark.parametrize("timestamp", [
    "2026-09-19T12:34:56+00:00",
    "2026-09-19T12:34:56.123Z",
    "2026-09-18T23:59:59Z",
])
def test_schema4_rejects_bad_or_mismatched_timestamp(timestamp):
    row = t.installation_status_event("a" * 32, "observed", "2026-09-19T12:34:56Z")
    row["observed_at"] = timestamp
    with pytest.raises(ValueError):
        t.validate_event(row)
