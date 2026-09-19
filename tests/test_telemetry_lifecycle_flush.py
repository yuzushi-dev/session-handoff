import server.telemetry as telemetry


def _response():
    class Response:
        status = 200
        done = False

        def read(self, size):
            if self.done:
                return b""
            self.done = True
            return b'{"partialSuccess":{}}'

        def close(self):
            pass

    return Response()


def test_flush_ack_removes_selected_lifecycle_row_after_nonprefix_selection(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config())
    first = telemetry.installation_lifecycle_event("a" * 32, "registered")
    second = telemetry.installation_lifecycle_event("b" * 32, "registered")
    with telemetry._state_lock(tmp_path) as (_c, _cf, state, fd):
        telemetry._store_queue_locked(state, fd, [first, second])
    assert telemetry.flush_queue(tmp_path, opener=lambda *_a, **_k: _response()) == 2
    assert telemetry.load_batch(tmp_path) == []


def test_v1_consent_preserves_lifecycle_row_while_uploading_aggregate(tmp_path):
    telemetry.write_config(tmp_path, telemetry.enabled_config(consent_version=1))
    lifecycle = telemetry.installation_lifecycle_event("c" * 32, "registered")
    aggregate = {
        "schema_version": 2, "event": "active_day", "day_utc": lifecycle["day_utc"],
        "plugin_version": telemetry.PACKAGE_VERSION, "origin": "real",
    }
    with telemetry._state_lock(tmp_path) as (_c, _cf, state, fd):
        telemetry._store_queue_locked(state, fd, [lifecycle, aggregate])
    assert telemetry.flush_queue(tmp_path, opener=lambda *_a, **_k: _response()) == 1
    assert lifecycle in telemetry.load_batch(tmp_path)
