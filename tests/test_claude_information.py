import importlib
import importlib.util
import json
import copy
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
HANDOFF = """## Goal
Finish retry fix.
## Constraints & Preferences
Keep synchronous API and cap 3.
## Progress
### Done
Cap is 3.
### In Progress
Add TimeoutError.
### Pending
Verify both retry tests.
## Key Decisions
No broad Exception.
## Critical Context
src/retry/policy.py TRANSIENT_ERRORS lacks TimeoutError.
## Next Steps
Add TimeoutError and verify.
"""


def runner():
    assert importlib.util.find_spec("benchmark.claude_information"), "Claude paired runner not implemented"
    return importlib.import_module("benchmark.claude_information")


class FakeAdapter:
    def __init__(self, workspace, state_root, *, fail_task=False, noop=False, bad_compact=False):
        self.workspace = workspace
        self.calls = []
        self.seeds = []
        self.fail_task = fail_task
        self.noop = noop
        self.bad_compact = bad_compact
        self.fork_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def seed(self, conversation):
        self.seeds.append(conversation)
        return f"session-{len(self.seeds)}"

    def turn(self, session_id, prompt, *, write=False, fork=False):
        self.calls.append((session_id, prompt, write, fork))
        self.fork_count += int(fork)
        if write and self.fail_task:
            raise RuntimeError("synthetic task transport failure")
        if write and not self.noop:
            path = self.workspace / "src/retry/policy.py"
            path.write_text(path.read_text().replace("(ConnectionError,)", "(ConnectionError, TimeoutError)"))
        return {
            "completed": True,
            "output": "probe answer" if fork else HANDOFF,
            "session_id": f"{session_id}-fork-{self.fork_count}" if fork else session_id,
            "tool_items": [{"name": "Edit"}] if write and not self.noop else [],
            "usage": None,
        }

    def compact(self, session_id):
        return {"actual_compaction": not self.bad_compact, "session_id": session_id}


def factory(instances, **kwargs):
    def create(workspace, state_root):
        adapter = FakeAdapter(workspace, state_root, **kwargs)
        instances.append(adapter)
        return adapter
    return create


def test_pair_requires_authorization_before_creating_state(tmp_path):
    with pytest.raises(ValueError, match="authorization"):
        runner().execute_pair(product_root=ROOT, output=tmp_path / "out", adapter_factory=None)
    assert not (tmp_path / "out").exists()


def test_pair_uses_same_history_fresh_handoff_and_separate_probes(tmp_path):
    instances = []
    result = runner().execute_pair(product_root=ROOT, output=tmp_path / "out",
                                   adapter_factory=factory(instances), authorized=True)
    assert result["attempted_continuations"] == result["completed_continuations"] == 2
    assert result["generation_operations"] == result["native_compactions"] == 1
    assert result["probe_operations"] == 4
    assert result["fatal_failure"] is None
    assert all(row["task_success"] for row in result["results"])
    assert instances[0].seeds[0] == instances[1].seeds[0]
    assert "gold_facts" not in json.dumps(instances[0].seeds)
    assert len(instances[0].seeds[1]["messages"]) == 1
    assert instances[0].seeds[1]["messages"][0]["content"] == HANDOFF
    for adapter in instances:
        probes = [call for call in adapter.calls if call[3]]
        task = [call for call in adapter.calls if call[2]]
        assert len(probes) == 2 and len(task) == 1
        assert all(call[0] == task[0][0] for call in probes)
        assert "probe answer" not in task[0][1]


def test_task_failure_consumes_attempt_without_retry_or_second_arm(tmp_path):
    instances = []
    result = runner().execute_pair(product_root=ROOT, output=tmp_path / "out",
                                   adapter_factory=factory(instances, fail_task=True), authorized=True)
    assert result["attempted_continuations"] == 1
    assert result["completed_continuations"] == 0
    assert result["fatal_failure"]
    assert len(instances) == 1
    assert sum(call[2] for call in instances[0].calls) == 1
    assert json.loads((tmp_path / "out/results.json").read_text())["attempted_continuations"] == 1


def test_noop_completed_turn_is_not_task_success(tmp_path):
    result = runner().execute_pair(product_root=ROOT, output=tmp_path / "out",
                                   adapter_factory=factory([], noop=True), authorized=True)
    assert result["completed_continuations"] == 2
    assert not any(row["task_success"] for row in result["results"])


def test_missing_real_compact_stops_before_native_task(tmp_path):
    result = runner().execute_pair(product_root=ROOT, output=tmp_path / "out",
                                   adapter_factory=factory([], bad_compact=True), authorized=True)
    assert result["attempted_continuations"] == 1
    assert result["native_compactions"] == 0
    assert result["fatal_failure"]


def test_pair_refuses_existing_results(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "results.json").write_text("preserve me")
    with pytest.raises(ValueError, match="not empty"):
        runner().execute_pair(product_root=ROOT, output=output, adapter_factory=None, authorized=True)
    assert (output / "results.json").read_text() == "preserve me"


@pytest.mark.parametrize("fault", [
    "same_fork", "empty_probe", "incomplete_task", "tool_probe", "same_handoff",
    "truthy_generation", "truthy_probe", "empty_source", "empty_handoff",
])
def test_pair_fails_closed_on_invalid_native_results(tmp_path, fault):
    class InvalidAdapter(FakeAdapter):
        def seed(self, conversation):
            session = super().seed(conversation)
            if fault == "empty_source" and len(self.seeds) == 1:
                return None
            if fault == "empty_handoff" and len(self.seeds) == 2:
                return ""
            return "session-1" if fault == "same_handoff" else session

        def turn(self, session_id, prompt, *, write=False, fork=False):
            result = super().turn(session_id, prompt, write=write, fork=fork)
            if fork and fault == "same_fork":
                result["session_id"] = "reused-probe-session"
            if fork and fault == "empty_probe":
                result["output"] = ""
            if fork and fault == "tool_probe":
                result["tool_items"] = [{"name": "Read"}]
            if write and fault == "incomplete_task":
                result["completed"] = False
            if not write and ((fork and fault == "truthy_probe") or (not fork and fault == "truthy_generation")):
                result["completed"] = "yes"
            return result

    result = runner().execute_pair(product_root=ROOT, output=tmp_path / "out",
                                   adapter_factory=InvalidAdapter, authorized=True)
    assert result["fatal_failure"], fault
    assert result["attempted_continuations"] == int(fault == "incomplete_task")
    assert len(result["results"]) == 1


def test_each_arm_receives_an_independent_history_copy(tmp_path):
    received = []

    class MutatingAdapter(FakeAdapter):
        def seed(self, conversation):
            if conversation["case"] == "compound-rot":
                received.append(copy.deepcopy(conversation))
                conversation["messages"][0]["content"] = "adapter-local mutation"
            return super().seed(conversation)

    runner().execute_pair(product_root=ROOT, output=tmp_path / "out",
                          adapter_factory=MutatingAdapter, authorized=True)
    assert len(received) == 2 and received[0] == received[1]
