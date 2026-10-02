import json
import io
from pathlib import Path

from server.compact_advisor import COMPACT_HINT_ENV, main as compact_advisor_main
from server.codex_context import parse_request_context


FIXTURES = Path(__file__).parent / "compat/fixtures/openai-ui"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_mcp_metadata_keeps_thread_and_family_ids_distinct() -> None:
    request = _fixture("mcp-thread-call.json")
    context = parse_request_context(request["params"])

    assert context.thread_id == "0199c0de-0000-7000-8000-000000000001"
    assert context.session_id == "0199c0de-0000-7000-8000-000000000002"


def test_legacy_mcp_call_has_no_inferred_binding() -> None:
    request = _fixture("mcp-legacy-call.json")

    assert parse_request_context(request["params"]).thread_id is None
    assert parse_request_context(request["params"]).session_id is None


def test_hook_session_id_is_not_a_thread_id() -> None:
    payload = _fixture("codex-stop-hook.json")

    assert payload["hook_event_name"] == "Stop"
    assert parse_request_context(payload).thread_id is None
    assert parse_request_context(payload).session_id is None


def test_codex_stop_payload_keeps_existing_advisor_opt_in(monkeypatch) -> None:
    monkeypatch.delenv(COMPACT_HINT_ENV, raising=False)
    stdout = io.StringIO()

    assert compact_advisor_main(
        stdin_text=json.dumps(_fixture("codex-stop-hook.json")),
        stdout=stdout,
        stderr=io.StringIO(),
    ) == 0
    assert stdout.getvalue().strip() == "{}"
