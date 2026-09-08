"""One explicitly authorized Claude-native information-preservation pair."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from benchmark.fixture_workspace import materialize_workspace
from benchmark.information_preservation import (
    BenchmarkGateError,
    FINAL_TASK_PROMPT,
    _handoff_generation_prompt,
    _load_build_instructions,
    _run_evaluator,
    _sha256,
    _workspace_diff,
    _workspace_snapshot,
    _write_json,
    build_structured_conversation,
    create_read_handoff,
)
from benchmark.product_roundtrip import product_identity


PROBE_PROMPT = (
    "List the current authoritative constraints, decisions and values, completed and "
    "pending file/test targets, rejected approaches, and next steps from the retained "
    "state. Do not change files and do not use external context."
)


def execute_pair(*, product_root, output, adapter_factory, authorized=False, timeout=180):
    """No retries; persist attempt accounting before each downstream invocation.

    The caller must verify the native adapter and translated model/effort before
    supplying a real adapter_factory. Tests supply a provider-free adapter.
    """
    if not authorized:
        raise BenchmarkGateError("explicit runtime authorization required")
    if timeout <= 0:
        raise BenchmarkGateError("timeout must be positive")
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise BenchmarkGateError("output directory is not empty")
    root = Path(product_root).resolve()
    identity = product_identity(root)
    instructions = _load_build_instructions(root)
    conversation = build_structured_conversation("compound-rot", "long", 1)
    history_hash = _sha256(json.dumps(conversation, sort_keys=True, separators=(",", ":")))
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.chmod(0o700)
    result = {
        "case": "compound-rot", "band": "long", "replicate": 1,
        "arms": ["candidate", "nativecompact"],
        "model": "gpt-5.6-luna", "reasoning_effort": "xhigh",
        "product": identity, "history_sha256": history_hash,
        "planned_continuations": 2, "attempted_continuations": 0,
        "completed_continuations": 0, "generation_operations": 0,
        "compaction_operations": 0, "native_compactions": 0,
        "probe_operations": 0, "results": [], "fatal_failure": None,
        "semantic_scoring": "pending_manual", "monetary_cost": None,
    }

    def save():
        _write_json(output / "results.json", result)

    save()
    for arm in result["arms"]:
        arm_root = output / arm
        workspace = arm_root / "workspace"
        fixture = materialize_workspace("compound-rot", workspace)
        before = _workspace_snapshot(workspace)
        row = {
            "arm": arm, "history_sha256": history_hash, "phase": "prepare",
            "continuation_attempted": False, "continuation_completed": False,
            "task_success": False, "probes": [], "probe_context_injected": False,
        }
        result["results"].append(row)
        save()
        try:
            with adapter_factory(workspace, arm_root / "native") as adapter:
                source = adapter.seed(copy.deepcopy(conversation))
                if not isinstance(source, str) or not source.strip():
                    raise BenchmarkGateError("source session ID is missing")
                row["source_session_id"] = source
                if arm == "candidate":
                    row["phase"] = "generation"
                    result["generation_operations"] += 1
                    save()
                    contract = {
                        "generator": "claude-code-print",
                        "model": result["model"], "reasoning_effort": "xhigh",
                        "skill_sha256": instructions["sha256"],
                        "create_mode_instructions": instructions["create_mode"],
                    }
                    prompt = _handoff_generation_prompt(arm=arm, contract=contract)
                    row["generation_prompt_sha256"] = _sha256(prompt)
                    generation = adapter.turn(source, prompt)
                    row["generation"] = generation
                    if generation.get("completed") is not True or generation.get("tool_items"):
                        raise BenchmarkGateError("generation must complete without tools")
                    generated = generation.get("output")
                    if not isinstance(generated, str) or not generated.strip():
                        raise BenchmarkGateError("generation returned no handoff")
                    handoff = create_read_handoff(
                        root, storage="central", isolated_root=arm_root / "product",
                        workspace=workspace, content=generated,
                    )
                    row["handoff"] = handoff
                    task_session = adapter.seed({
                        "schema_version": 1, "case": "handoff", "band": "short",
                        "replicate": 1, "checkpoint": 1, "role_order": ["user"],
                        "messages": [{"id": "handoff", "role": "user", "content": handoff["content"]}],
                    })
                    if not isinstance(task_session, str) or not task_session.strip() or task_session == source:
                        raise BenchmarkGateError("handoff did not start a fresh session")
                else:
                    row["phase"] = "compact"
                    result["compaction_operations"] += 1
                    save()
                    compact = adapter.compact(source)
                    row["checkpoint"] = compact
                    if compact.get("actual_compaction") is not True or compact.get("session_id") != source:
                        raise BenchmarkGateError("real native compaction not verified")
                    result["native_compactions"] += 1
                    task_session = source
                row["task_session_id"] = task_session
                row["phase"] = "probes"
                probe_sessions = {task_session}
                for label in ("active_state", "superseded_decision"):
                    result["probe_operations"] += 1
                    save()
                    probe = adapter.turn(task_session, PROBE_PROMPT, fork=True)
                    row["probes"].append({"probe": label, **probe})
                    if probe.get("completed") is not True or probe.get("tool_items"):
                        raise BenchmarkGateError("probe must complete without tools")
                    answer = probe.get("output")
                    if not isinstance(answer, str) or not answer.strip():
                        raise BenchmarkGateError("probe returned no answer")
                    probe_session = probe.get("session_id")
                    if not isinstance(probe_session, str) or not probe_session or probe_session in probe_sessions:
                        raise BenchmarkGateError("probe did not create an independent fork")
                    probe_sessions.add(probe_session)
                    save()
                row["phase"] = "continuation"
                row["continuation_attempted"] = True
                result["attempted_continuations"] += 1
                save()
                continuation = adapter.turn(task_session, FINAL_TASK_PROMPT, write=True)
                row["continuation"] = continuation
                row["continuation_completed"] = continuation.get("completed") is True
                result["completed_continuations"] += int(row["continuation_completed"])
                if continuation.get("session_id") != task_session:
                    raise BenchmarkGateError("continuation changed the task session")
                if not row["continuation_completed"]:
                    raise BenchmarkGateError("task continuation did not complete")
            row["phase"] = "verification"
            row["verification"] = _run_evaluator(fixture["verify_command"], workspace, timeout)
            row["hidden_evaluation"] = _run_evaluator(fixture["acceptance_command"], workspace, timeout)
            row["task_success"] = (
                row["continuation_completed"]
                and row["verification"]["passed"] and row["hidden_evaluation"]["passed"]
            )
            row["phase"] = "complete"
        except Exception as exc:
            row["failure"] = {
                "type": type(exc).__name__, "phase": row["phase"],
                "message_sha256": _sha256(str(exc)),
            }
            result["fatal_failure"] = row["failure"]
        finally:
            row["workspace_diff"] = _workspace_diff(before, _workspace_snapshot(workspace))
            save()
        if result["fatal_failure"]:
            break
    return result
