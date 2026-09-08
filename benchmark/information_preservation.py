"""Provider-gated information-preservation benchmark helpers.

The offline lane validates inputs, protocol handling, and study accounting. It
does not start a provider-backed matrix.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import selectors
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class BenchmarkGateError(ValueError):
    """The requested operation is not safe or not verified yet."""


class StructuredConversationError(BenchmarkGateError):
    """A structured fixture cannot be sent to a native client."""


class ProtocolError(RuntimeError):
    """The app-server returned an invalid or mismatched protocol event."""


class CompactionError(ProtocolError):
    """Native compaction failed or did not complete."""


class CompactionTimeout(CompactionError):
    """Native compaction did not produce a completion before the deadline."""


ARMS = ("main", "candidate", "nativecompact")
CASES = (
    "buried-constraint",
    "superseded-decision",
    "failed-attempt-trap",
    "partial-state",
    "late-correction",
    "compound-rot",
)
BANDS = ("short", "long")
REPLICATES = 3
CELL_COUNT = len(CASES) * len(BANDS) * REPLICATES * len(ARMS)
SUPPORTED_NATIVE_VERSION = "0.153.4"
DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_REASONING_EFFORT = "xhigh"
REQUIRED_CLIENT_METHODS = frozenset(
    {
        "initialize",
        "thread/start",
        "thread/fork",
        "thread/compact/start",
        "thread/inject_items",
        "thread/read",
        "thread/items/list",
        "turn/start",
    }
)
REQUIRED_SERVER_NOTIFICATIONS = frozenset({"item/completed", "error"})
STANDARD_NATIVE_COMPACT_PARAMS = {
    "method": "thread/compact/start",
    "required_params": ["threadId"],
    "configuration": "native-default",
}
COMMON_DOWNSTREAM_CONTRACT = {
    "tool_access": "identical",
    "workspace_access": "identical_snapshot",
    "budget": "verified_common",
}
READ_PERMISSION_PROFILE = "benchmark_read"
WRITE_PERMISSION_PROFILE = "benchmark_write"
GATE_HARNESS_READY = "harness-ready"
GATE_NATIVE_SCHEMA_VERIFIED = "native-schema-verified"
GATE_REAL_COMPACT_VERIFIED = "realcompact-verified"
GATE_BENCHMARK_RESULTS = "benchmark-results"
_ALLOWED_ROLES = frozenset({"user", "assistant", "tool"})
_SENSITIVE_KEYS = frozenset(
    {"gold", "gold_facts", "stale_traps", "dod", "acceptance", "expected"}
)


def _sha256(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _load_build_instructions(root: str | Path) -> dict[str, str]:
    """Load the shipped create-mode contract from one measured build."""

    path = Path(root).expanduser().resolve() / "skills/session-handoff/SKILL.md"
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BenchmarkGateError(f"build skill instructions unavailable: {path}") from exc
    if not source.strip():
        raise BenchmarkGateError(f"build skill instructions are empty: {path}")
    start_marker = "\n## Create mode\n"
    end_marker = "\n## Resume mode\n"
    start = source.find(start_marker)
    end = source.find(end_marker, start + len(start_marker)) if start >= 0 else -1
    if start < 0 or end < 0 or end <= start:
        raise BenchmarkGateError(f"build skill lacks a delimited Create mode: {path}")
    create_mode = source[start + 1 : end].strip() + "\n"
    structure_marker = "Use exactly this document structure:"
    structure_start = create_mode.find(structure_marker)
    if structure_start < 0:
        raise BenchmarkGateError(f"build skill lacks the canonical handoff structure: {path}")
    structure = create_mode[structure_start:].strip() + "\n"
    return {
        "path": "skills/session-handoff/SKILL.md",
        "sha256": _sha256(source),
        "create_mode_sha256": _sha256(create_mode),
        "structure_sha256": _sha256(structure),
        "create_mode": create_mode,
    }


def _write_native_permission_config(state_root: Path) -> Path:
    """Configure Codex's named profiles so tools cannot read adapter state."""

    config_path = state_root / "codex" / "config.toml"
    config_path.write_text(
        """default_permissions = \"benchmark_read\"\n
[permissions.benchmark_read]
extends = \":read-only\"

[permissions.benchmark_read.filesystem]
\"/mnt/native\" = \"deny\"

[permissions.benchmark_write]
extends = \":workspace\"

[permissions.benchmark_write.filesystem]
\"/mnt/native\" = \"deny\"
\"/mnt/work\" = \"write\"\n""",
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    return config_path


def _tool_item_evidence(item: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    """Keep tool call/output evidence while excluding private reasoning fields."""

    private_keys = {"reasoning", "analysis", "encrypted_content", "signature", "private"}

    def clean(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): clean(child)
                for key, child in value.items()
                if str(key).lower() not in private_keys
            }
        if isinstance(value, list):
            return [clean(child) for child in value]
        return value

    evidence = clean(item)
    if not isinstance(evidence, dict):
        raise ProtocolError("tool item is not an object")
    evidence["source"] = source
    return evidence


def _protocol_event_evidence(
    event: Mapping[str, Any],
    *,
    request_id: int,
    target_thread_id: str,
    target_turn_id: str | None,
) -> dict[str, Any]:
    """Summarize one notification without retaining model/tool payload text."""

    method = event.get("method")
    params = event.get("params")
    evidence: dict[str, Any] = {
        "request_id": request_id,
        "method": method if isinstance(method, str) else None,
        "thread_id": None,
        "turn_id": None,
        "classification": "foreign",
    }
    if not isinstance(params, Mapping):
        evidence["params_valid"] = False
        return evidence
    evidence["params_valid"] = True
    event_thread_id = params.get("threadId")
    if isinstance(event_thread_id, str):
        evidence["thread_id"] = event_thread_id
    event_turn_id = params.get("turnId")
    item = params.get("item")
    if isinstance(item, Mapping):
        if isinstance(item.get("id"), str):
            evidence["item_id"] = item["id"]
        if isinstance(item.get("type"), str):
            evidence["item_type"] = item["type"]
    turn = params.get("turn")
    if isinstance(turn, Mapping):
        if isinstance(turn.get("id"), str):
            event_turn_id = turn["id"]
        if isinstance(turn.get("status"), str):
            evidence["status"] = turn["status"]
        turn_error = turn.get("error")
        if isinstance(turn_error, Mapping):
            if isinstance(turn_error.get("message"), str):
                evidence["error_message_sha256"] = _sha256(turn_error["message"])
            if isinstance(turn_error.get("codexErrorInfo"), (str, Mapping)):
                evidence["error_code"] = (
                    turn_error["codexErrorInfo"]
                    if isinstance(turn_error["codexErrorInfo"], str)
                    else "present"
                )
    if isinstance(event_turn_id, str):
        evidence["turn_id"] = event_turn_id
    if method == "error" and isinstance(params.get("error"), Mapping):
        error = params["error"]
        if isinstance(error.get("message"), str):
            evidence["error_message_sha256"] = _sha256(error["message"])
        if isinstance(error.get("codexErrorInfo"), (str, Mapping)):
            evidence["error_code"] = (
                error["codexErrorInfo"]
                if isinstance(error["codexErrorInfo"], str)
                else "present"
            )
    if evidence["thread_id"] == target_thread_id and (
        target_turn_id is None or evidence["turn_id"] == target_turn_id
    ):
        evidence["classification"] = "target"
    return evidence


def _workspace_snapshot(root: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    if not root.is_dir():
        return snapshot
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        snapshot[str(path.relative_to(root))] = path.read_bytes()
    return snapshot


def _workspace_diff(before: Mapping[str, bytes], after: Mapping[str, bytes]) -> dict[str, Any]:
    """Return bounded, reproducible workspace evidence before adapter cleanup."""

    changed = sorted(set(before) | set(after))
    diff_lines: list[str] = []
    entries: list[dict[str, Any]] = []
    for relative in changed:
        old = before.get(relative)
        new = after.get(relative)
        if old == new:
            continue
        status = "added" if old is None else "removed" if new is None else "modified"
        entries.append(
            {
                "path": relative,
                "status": status,
                "before_sha256": _sha256(old) if old is not None else None,
                "after_sha256": _sha256(new) if new is not None else None,
            }
        )
        old_text = old.decode("utf-8", "replace") if old is not None else ""
        new_text = new.decode("utf-8", "replace") if new is not None else ""
        diff_lines.extend(
            difflib.unified_diff(
                old_text.splitlines(),
                new_text.splitlines(),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
                lineterm="",
            )
        )
    return {"files": entries, "unified_diff": "\n".join(diff_lines)}


def _fixture_cases() -> dict[str, dict[str, Any]]:
    path = Path(__file__).parent / "fixtures/context_rot_cases.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in payload.get("cases", [])}
    if tuple(cases) != CASES:
        raise BenchmarkGateError("context-rot fixture set does not match the study")
    return cases


def _contains_sensitive_key(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                return str(key)
            found = _contains_sensitive_key(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _contains_sensitive_key(item)
            if found:
                return found
    return None


def validate_structured_conversation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StructuredConversationError("conversation must be an object")
    if value.get("schema_version") != 1:
        raise StructuredConversationError("unsupported conversation schema")
    leaked = _contains_sensitive_key(value)
    if leaked:
        raise StructuredConversationError(f"gold annotation field is not allowed: {leaked}")
    messages = value.get("messages")
    if not isinstance(messages, list) or not messages:
        raise StructuredConversationError("messages must be a non-empty array")
    ids: set[str] = set()
    call_ids: set[str] = set()
    roles: list[str] = []
    previous_tool_call: str | None = None
    for message in messages:
        if not isinstance(message, dict):
            raise StructuredConversationError("messages must contain objects")
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise StructuredConversationError("message id must be non-empty")
        if message_id in ids:
            raise StructuredConversationError(f"duplicate message id: {message_id}")
        ids.add(message_id)
        role = message.get("role")
        if role not in _ALLOWED_ROLES:
            raise StructuredConversationError(f"unsupported message role: {role}")
        if previous_tool_call is not None and role != "tool":
            raise StructuredConversationError("function call must be followed by its tool output")
        content = message.get("content")
        if not isinstance(content, str) or not content:
            raise StructuredConversationError("message content must be non-empty text")
        if role == "tool" and (
            not isinstance(message.get("call_id"), str) or not message["call_id"]
        ):
            raise StructuredConversationError("tool messages require call_id")
        if role == "assistant" and "tool_call" in message:
            tool_call = message.get("tool_call")
            if not isinstance(tool_call, dict):
                raise StructuredConversationError("tool_call must be an object")
            tool_call_id = tool_call.get("call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise StructuredConversationError("tool_call requires call_id")
            if tool_call_id in call_ids:
                raise StructuredConversationError(f"duplicate tool call id: {tool_call_id}")
            name = tool_call.get("name")
            arguments = tool_call.get("arguments")
            if not isinstance(name, str) or not name:
                raise StructuredConversationError("tool_call requires name")
            if not isinstance(arguments, str):
                raise StructuredConversationError("tool_call arguments must be JSON text")
            try:
                json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise StructuredConversationError("tool_call arguments are not JSON") from exc
            call_ids.add(tool_call_id)
            previous_tool_call = tool_call_id
        elif role == "tool":
            call_id = message["call_id"]
            if call_id not in call_ids or previous_tool_call != call_id:
                raise StructuredConversationError("tool output lacks its preceding function call")
            previous_tool_call = None
        else:
            previous_tool_call = None
        roles.append(role)
    if previous_tool_call is not None:
        raise StructuredConversationError("function call lacks its tool output")
    if value.get("role_order") != roles:
        raise StructuredConversationError("role_order does not match message order")
    checkpoint = value.get("checkpoint")
    if isinstance(checkpoint, bool) or not isinstance(checkpoint, int):
        raise StructuredConversationError("checkpoint must be an integer")
    if checkpoint != len(messages):
        raise StructuredConversationError("checkpoint must include every history message")
    return value


def build_structured_conversation(case_id: str, band: str, replicate: int) -> dict[str, Any]:
    """Convert an existing fixture to ordered user/assistant/tool messages.

    Gold annotations remain in the fixture source and are deliberately not
    copied into this wire representation.
    """

    if case_id not in CASES:
        raise BenchmarkGateError(f"unknown fixture case: {case_id}")
    if band not in BANDS:
        raise BenchmarkGateError(f"unsupported study band: {band}")
    if isinstance(replicate, bool) or replicate < 1:
        raise BenchmarkGateError("replicate must be positive")
    case = _fixture_cases()[case_id]
    target = {"short": 32_000, "long": 160_000}[band]
    anchors = list(case["anchors"])
    messages: list[dict[str, Any]] = [
        {
            "id": "message-000",
            "role": "user",
            "content": "Resume the existing task from this structured conversation.",
        }
    ]
    noise_index = 0

    def encoded_size() -> int:
        return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))

    def append_tool_pair(index: int, content: str) -> None:
        call_id = f"fixture-tool-{replicate}-{index:05d}"
        messages.append(
            {
                "id": f"tool-call-{index:05d}",
                "role": "assistant",
                "content": "Running a non-authoritative diagnostic.",
                "tool_call": {
                    "call_id": call_id,
                    "name": "fixture_diagnostic",
                    "arguments": json.dumps({"index": index}, separators=(",", ":")),
                },
            }
        )
        messages.append(
            {
                "id": f"tool-output-{index:05d}",
                "role": "tool",
                "call_id": call_id,
                "content": content,
            }
        )

    # Keep each fixture anchor near its declared relative position.  Noise is
    # inserted only in the gaps, rather than clustering every fact at the head.
    for anchor in anchors:
        target_size = max(0, int(target * float(anchor["at"])))
        while encoded_size() < target_size:
            remaining = max(64, target_size - encoded_size())
            text = (
                f"tool output {noise_index:05d}: routine diagnostics and dependency metadata; "
                "no authoritative task state changed. "
            )
            text = (text * ((remaining // len(text)) + 1))[:remaining]
            append_tool_pair(noise_index, text)
            noise_index += 1
        messages.append(
            {
                "id": f"message-{len(messages):05d}",
                "role": anchor["role"],
                "content": anchor["content"],
            }
        )
    conversation = {
        "schema_version": 1,
        "case": case_id,
        "band": band,
        "replicate": replicate,
        "checkpoint": len(messages),
        "messages": messages,
        "role_order": [message["role"] for message in messages],
    }
    return validate_structured_conversation(conversation)


def conversation_to_response_items(conversation: Mapping[str, Any]) -> list[dict[str, Any]]:
    validate_structured_conversation(dict(conversation))
    items: list[dict[str, Any]] = []
    for message in conversation["messages"]:
        role = message["role"]
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message["call_id"],
                    "output": message["content"],
                }
            )
            continue
        if role == "assistant" and isinstance(message.get("tool_call"), Mapping):
            call = message["tool_call"]
            items.append(
                {
                    "type": "function_call",
                    "call_id": call["call_id"],
                    "name": call["name"],
                    "arguments": call["arguments"],
                }
            )
            continue
        content_type = "input_text" if role == "user" else "output_text"
        items.append(
            {
                "type": "message",
                "role": role,
                "content": [{"type": content_type, "text": message["content"]}],
            }
        )
    return items


def plan_benchmark(
    *,
    cases: Sequence[str] = CASES,
    bands: Sequence[str] = BANDS,
    arms: Sequence[str] = ARMS,
    replicates: int = REPLICATES,
    verified_context_budget_tokens: int | None = None,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> dict[str, Any]:
    values = {"cases": tuple(cases), "bands": tuple(bands), "arms": tuple(arms)}
    for label, selected in values.items():
        if len(set(selected)) != len(selected):
            raise BenchmarkGateError(f"duplicate {label} in benchmark plan")
    if set(values["arms"]) != set(ARMS):
        raise BenchmarkGateError("benchmark plan must contain main, candidate, nativecompact")
    if not values["cases"] or not values["bands"]:
        raise BenchmarkGateError("benchmark plan needs cases and bands")
    if any(case not in CASES for case in values["cases"]):
        raise BenchmarkGateError("benchmark plan contains an unknown case")
    if any(band not in (*BANDS, "near_threshold") for band in values["bands"]):
        raise BenchmarkGateError("benchmark plan contains an unknown band")
    if "near_threshold" in values["bands"] and verified_context_budget_tokens is None:
        raise BenchmarkGateError("near-threshold band requires a verified context budget")
    if isinstance(replicates, bool) or replicates < 1:
        raise BenchmarkGateError("replicates must be positive")
    continuations = len(values["cases"]) * len(values["bands"]) * replicates * len(ARMS)
    cells = build_cells(
        cases=values["cases"],
        bands=values["bands"],
        arms=ARMS,
        replicates=replicates,
    )
    return {
        "schema_version": 1,
        "cases": list(values["cases"]),
        "bands": list(values["bands"]),
        "arms": list(ARMS),
        "replicates": replicates,
        "continuations": continuations,
        "cells": cells,
        "preparation_operations": len(values["cases"]) * len(values["bands"]) * replicates * 2,
        "compaction_operations": len(values["cases"]) * len(values["bands"]) * replicates,
        "retrieval_probe_operations": len(values["cases"]) * len(values["bands"]) * replicates * 2,
        "provider_calls": None,
        "execution": "blocked",
        "gate": GATE_HARNESS_READY,
        "verified_context_budget_tokens": verified_context_budget_tokens,
        "costs": "unknown",
        "model": model,
        "reasoning_effort": reasoning_effort,
        "native_compact_params": STANDARD_NATIVE_COMPACT_PARAMS,
        "downstream_contract": COMMON_DOWNSTREAM_CONTRACT,
    }


def build_cells(
    *,
    cases: Sequence[str] = CASES,
    bands: Sequence[str] = BANDS,
    arms: Sequence[str] = ARMS,
    replicates: int = REPLICATES,
) -> list[dict[str, Any]]:
    """Build unique, content-free continuation cells for a gated run."""

    if len(arms) != len(ARMS) or set(arms) != set(ARMS):
        raise BenchmarkGateError("cells must contain each benchmark arm exactly once")
    if isinstance(replicates, bool) or replicates < 1:
        raise BenchmarkGateError("replicates must be positive")
    cells: list[dict[str, Any]] = []
    logical_index = 0
    for case in cases:
        for band in bands:
            for replicate in range(1, replicates + 1):
                arm_order = tuple(ARMS[logical_index % len(ARMS) :] + ARMS[: logical_index % len(ARMS)])
                for arm in arm_order:
                    cell_key = f"{case}:{band}:{replicate}:{arm}"
                    cells.append(
                        {
                            "cell_id": _sha256(cell_key)[:24],
                            "case": case,
                            "band": band,
                            "replicate": replicate,
                            "arm": arm,
                            "arm_order": list(arm_order),
                            "arm_position": arm_order.index(arm) + 1,
                            "continuation_budget": COMMON_DOWNSTREAM_CONTRACT,
                            "execution": "blocked",
                        }
                    )
                logical_index += 1
    if len({cell["cell_id"] for cell in cells}) != len(cells):
        raise BenchmarkGateError("duplicate continuation cells")
    return cells


def create_read_handoff(
    root: str | Path,
    *,
    storage: str,
    isolated_root: str | Path,
    workspace: str | Path,
    content: str,
) -> dict[str, Any]:
    """Create and read one handoff through the selected product's public MCP."""

    from benchmark.product_roundtrip import roundtrip

    result = roundtrip(
        Path(root),
        storage,
        Path(isolated_root),
        Path(workspace),
        content,
    )
    result["read_verified"] = result.get("read", {}).get("valid") is True
    if not result["read_verified"]:
        raise BenchmarkGateError("handoff read did not return a valid canonical document")
    return result


def checkpoint_native(adapter: "NativeAppServer", thread_id: str) -> dict[str, Any]:
    """Compact one native thread and retain only checkpoint provenance."""

    completion = adapter.compact_thread(thread_id)
    checkpoint_key = ":".join(
        str(completion.get(field, ""))
        for field in ("thread_id", "turn_id", "item_id")
    )
    return {
        **completion,
        "checkpoint_id": _sha256(checkpoint_key),
        "actual_compaction": True,
        "truncated": None,
        "fallback": None,
        "gate": GATE_REAL_COMPACT_VERIFIED,
    }


def gate_state(
    *, native_compact_verified: bool = False, benchmark_completed: bool = False
) -> str:
    if benchmark_completed:
        if not native_compact_verified:
            raise BenchmarkGateError("benchmark results require verified native compaction")
        return GATE_BENCHMARK_RESULTS
    if native_compact_verified:
        return GATE_REAL_COMPACT_VERIFIED
    return GATE_HARNESS_READY


def fork_recovery_probes(
    adapter: "NativeAppServer",
    thread_id: str,
    probes: Sequence[str] = ("active_state", "superseded_decision"),
    *,
    gold_facts: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Fork isolated native copies for opaque-state recoverability probes."""

    results: list[dict[str, Any]] = []
    for label in probes:
        fork_thread_id = adapter.fork_thread(thread_id)
        thread = adapter.read_thread(fork_thread_id)
        items = adapter.list_items(fork_thread_id)
        query_result: Mapping[str, Any] | None = None
        if hasattr(adapter, "continue_thread"):
            query_result = adapter.continue_thread(
                fork_thread_id,
                "List the current authoritative constraints, decisions and values, completed and "
                "pending file/test targets, rejected approaches, and next steps from the retained "
                "state. Do not change files and do not use external context.",
            )
            if query_result.get("tool_used") is True:
                violation = {
                    "probe": label,
                    "fork_thread_id": fork_thread_id,
                    "thread_readable": isinstance(thread, Mapping),
                    "recoverable": None,
                    "state_items": len(items),
                    "opaque_state": True,
                    "text_recall": None,
                    "continuation_context": None,
                    "model_queried": True,
                    "raw_answer": query_result.get("output"),
                    "probe_context_injected": False,
                    "tool_items": query_result.get("tool_items", []),
                    "tool_use_violation": True,
                    "fact_matches": None,
                    "semantic_score_status": "unscored",
                }
                error = ProtocolError(
                    "recoverability probe used a tool before task continuation"
                )
                error.evidence = [violation]  # type: ignore[attr-defined]
                raise error
        queried = isinstance(query_result, Mapping)
        recoverable: bool | None = None
        fact_matches: list[dict[str, Any]] | None = None
        if queried and query_result.get("completed") is True:
            answer = query_result.get("output") or query_result.get("text")
            if isinstance(answer, str) and gold_facts:
                normalized = " ".join(answer.lower().split())
                fact_matches = []
                for fact in gold_facts:
                    statement = fact.get("statement")
                    if not isinstance(statement, str) or not statement:
                        continue
                    needle = " ".join(statement.lower().split())
                    fact_matches.append(
                        {
                            "id": fact.get("id"),
                            "matched": needle in normalized,
                            "critical": bool(fact.get("critical")),
                        }
                    )
                critical = [item for item in fact_matches if item["critical"]]
                recoverable = bool(critical) and all(item["matched"] for item in critical)
        results.append(
            {
                "probe": label,
                "fork_thread_id": fork_thread_id,
                "thread_readable": isinstance(thread, Mapping),
                "recoverable": recoverable,
                "state_items": len(items),
                "opaque_state": True,
                "text_recall": None,
                "continuation_context": None,
                "model_queried": queried,
                "raw_answer": query_result.get("output") if queried else None,
                "probe_context_injected": False,
                "tool_items": query_result.get("tool_items", []) if queried else [],
                "tool_use_violation": False,
                "fact_matches": fact_matches,
                "semantic_score_status": "exact_match_proxy" if fact_matches is not None else "unscored",
            }
        )
    return results


def run_metadata(
    *,
    build: str,
    client: str,
    model: str,
    effort: str,
    prompt: str,
    usage: Mapping[str, Any] | None = None,
    cost: Mapping[str, Any] | None = None,
    compaction: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture pinned identities without converting usage to a currency estimate."""

    build_path = Path(build).expanduser() if isinstance(build, str) else None
    build_sha256: str | None = None
    build_hash_kind = "unavailable"
    if build_path is not None and build_path.is_file():
        build_sha256 = _sha256(build_path.read_bytes())
        build_hash_kind = "file"
    elif build_path is not None and build_path.is_dir():
        digest = hashlib.sha256()
        for path in sorted(path for path in build_path.rglob("*") if path.is_file()):
            digest.update(str(path.relative_to(build_path)).encode("utf-8"))
            digest.update(path.read_bytes())
        build_sha256 = digest.hexdigest()
        build_hash_kind = "tree"
    return {
        "build": build,
        "build_sha256": build_sha256,
        "build_hash_kind": build_hash_kind,
        "client": client,
        "model": model,
        "effort": effort,
        "prompt_sha256": _sha256(prompt),
        "usage": dict(usage) if usage is not None else None,
        "cost": dict(cost) if cost is not None else None,
        "compaction": {
            "actual": None,
            "truncation": None,
            "fallback": None,
            **(dict(compaction) if compaction is not None else {}),
        },
    }


def compact_completion(
    events: Sequence[Mapping[str, Any]], *, request_id: int | str, thread_id: str
) -> dict[str, Any]:
    """Accept only a correlated context-compaction completion, never an ack."""

    if not events:
        raise CompactionTimeout("compact completion deadline expired")
    acknowledged = False
    for event in events:
        if not isinstance(event, Mapping):
            raise ProtocolError("app-server event must be an object")
        if event.get("id") == request_id:
            if "error" in event:
                raise CompactionError("compact request failed")
            if "result" not in event or not isinstance(event["result"], dict):
                raise ProtocolError("compact response must be an object")
            acknowledged = True
            continue
        method = event.get("method")
        if method == "error":
            params = event.get("params")
            if not isinstance(params, Mapping):
                raise ProtocolError("error notification has invalid params")
            if params.get("threadId") == thread_id:
                raise CompactionError("compact completion failed")
            continue
        if method != "item/completed":
            continue
        params = event.get("params")
        if not isinstance(params, Mapping):
            raise ProtocolError("item completion has invalid params")
        if params.get("threadId") != thread_id:
            raise ProtocolError("compact completion belongs to another thread")
        item = params.get("item")
        if not isinstance(item, Mapping) or item.get("type") != "contextCompaction":
            continue
        if not acknowledged:
            raise ProtocolError("compact completion arrived before its response")
        return {
            "thread_id": thread_id,
            "turn_id": params.get("turnId"),
            "item_id": item.get("id"),
            "completion_event": "item/completed",
            "acknowledged": True,
            "opaque": True,
        }
    if acknowledged:
        raise CompactionError("compact acknowledged without completion")
    raise ProtocolError("compact response was not correlated")


@dataclass(frozen=True)
class NativeInterface:
    version: str
    client_methods: frozenset[str]
    server_notifications: frozenset[str]
    schema_sha256: str

    @property
    def verified(self) -> bool:
        return REQUIRED_CLIENT_METHODS <= self.client_methods and REQUIRED_SERVER_NOTIFICATIONS <= self.server_notifications


def _methods(path: Path) -> frozenset[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    result: set[str] = set()
    for variant in data.get("oneOf", []):
        method = variant.get("properties", {}).get("method", {}).get("enum")
        if isinstance(method, list) and len(method) == 1 and isinstance(method[0], str):
            result.add(method[0])
    return frozenset(result)


def discover_native_interface(
    binary: str | Path, *, expected_version: str = SUPPORTED_NATIVE_VERSION
) -> NativeInterface:
    """Generate and validate the installed binary's own app-server schema."""

    executable = Path(binary).expanduser().resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise BenchmarkGateError(f"native app-server binary is unavailable: {binary}")
    try:
        version_result = subprocess.run(
            [str(executable), "--version"],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BenchmarkGateError("native version probe failed") from exc
    version = version_result.stdout.strip().splitlines()[0] if version_result.stdout.strip() else ""
    if version != f"codex-cli {expected_version}":
        raise BenchmarkGateError(f"unsupported native build: {version or 'unknown'}")
    with tempfile.TemporaryDirectory(prefix="information-compact-schema-") as directory:
        schema_dir = Path(directory)
        try:
            generated = subprocess.run(
                [str(executable), "app-server", "generate-json-schema", "--experimental", "--out", str(schema_dir)],
                text=True,
                capture_output=True,
                check=False,
                timeout=20,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": str(schema_dir / "home"),
                    "CODEX_HOME": str(schema_dir / "codex"),
                    "DO_NOT_TRACK": "1",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BenchmarkGateError("native schema generation failed") from exc
        if generated.returncode != 0:
            raise BenchmarkGateError("native schema generation failed")
        client_path = schema_dir / "ClientRequest.json"
        server_path = schema_dir / "ServerNotification.json"
        if not client_path.is_file() or not server_path.is_file():
            raise BenchmarkGateError("native schema is incomplete")
        client_methods = _methods(client_path)
        server_methods = _methods(server_path)
        schema_hash = _sha256(client_path.read_bytes() + server_path.read_bytes())
    interface = NativeInterface(version, client_methods, server_methods, schema_hash)
    if not interface.verified:
        missing = sorted(
            (REQUIRED_CLIENT_METHODS - client_methods)
            | (REQUIRED_SERVER_NOTIFICATIONS - server_methods)
        )
        raise BenchmarkGateError(f"native app-server schema is missing: {', '.join(missing)}")
    return interface


class NativeAppServer:
    """Small line-oriented app-server client with strict completion handling."""

    def __init__(
        self,
        *,
        binary: str | Path,
        interface: NativeInterface | None,
        cwd: str | Path | None = None,
        deadline: float = 30.0,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        sandbox_executable: str | None = None,
        hidden_paths: Sequence[str | Path] = (),
        credential_source: str | Path | None = None,
        allow_network: bool = False,
    ) -> None:
        if interface is None or not interface.verified:
            raise BenchmarkGateError("native compact requires a verified app-server interface")
        self.binary = Path(binary).expanduser().resolve()
        self.interface = interface
        self.cwd = Path(cwd).expanduser().resolve() if cwd else None
        self.deadline = deadline
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.sandbox_executable = sandbox_executable
        self.hidden_paths = tuple(Path(path).expanduser().resolve() for path in hidden_paths)
        self.credential_source = (
            Path(credential_source).expanduser().resolve() if credential_source else None
        )
        self.allow_network = allow_network
        self._process: subprocess.Popen[bytes] | None = None
        self._state_temp: tempfile.TemporaryDirectory[str] | None = None
        self._next_id = 1
        self._stdout_buffer = bytearray()
        self.stderr_excerpt = ""

    @staticmethod
    def dry_run(plan: Mapping[str, Any]) -> dict[str, Any]:
        if plan.get("execution") != "blocked":
            raise BenchmarkGateError("dry-run requires a blocked plan")
        return {
            "execution": "blocked",
            "provider_calls": 0,
            "gate": "harness-ready",
            "continuations": plan.get("continuations"),
            "planned_continuations": plan.get("continuations"),
            "excluded_cell_ids": plan.get("excluded_cell_ids", []),
            "excluded_continuations": plan.get("excluded_continuations", 0),
            "remaining_continuations": plan.get(
                "remaining_continuations", plan.get("continuations")
            ),
        }

    @staticmethod
    def require_execution(
        plan: Mapping[str, Any], *, execute: bool, acknowledge_provider_cost: bool,
        allow_runtime: bool = False,
    ) -> dict[str, Any]:
        if not execute:
            return dict(plan)
        if not acknowledge_provider_cost:
            raise BenchmarkGateError("provider execution requires cost acknowledgement")
        if not allow_runtime:
            raise BenchmarkGateError("provider execution requires the explicit runtime gate")
        authorized = dict(plan)
        authorized["execution"] = "authorized"
        authorized["gate"] = "runtime-authorized"
        return authorized

    @staticmethod
    def metadata(
        *, client: str, model: str, reasoning_effort: str, prompt: str, build: str
    ) -> dict[str, Any]:
        return {
            "client": client,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "build": build,
            "build_sha256": None,
            "build_hash_kind": "unavailable",
            "prompt_sha256": _sha256(prompt),
            "cost": None,
            "usage": None,
            "compaction": {"actual": None, "truncation": None, "fallback": None},
        }

    def __enter__(self) -> "NativeAppServer":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def start(self) -> None:
        if self._process is not None:
            return
        if not self.binary.is_file():
            raise BenchmarkGateError(f"native app-server binary is unavailable: {self.binary}")
        environment = {
            key: os.environ[key]
            for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE")
            if key in os.environ
        }
        self._state_temp = tempfile.TemporaryDirectory(prefix="information-compact-state-")
        state_root = Path(self._state_temp.name)
        for directory in (state_root / "home", state_root / "codex", state_root / "data", state_root / "config"):
            directory.mkdir(mode=0o700)
        _write_native_permission_config(state_root)
        environment.update(
            {
                "HOME": str(state_root / "home"),
                "CODEX_HOME": str(state_root / "codex"),
                "XDG_DATA_HOME": str(state_root / "data"),
                "XDG_CONFIG_HOME": str(state_root / "config"),
                "DO_NOT_TRACK": "1",
            }
        )
        command = [str(self.binary), "app-server", "--stdio"]
        process_cwd = str(self.cwd) if self.cwd else None
        if self.sandbox_executable:
            environment.update(
                {
                    "HOME": "/mnt/native/home",
                    "CODEX_HOME": "/mnt/native/codex",
                    "XDG_DATA_HOME": "/mnt/native/data",
                    "XDG_CONFIG_HOME": "/mnt/native/config",
                    "TMPDIR": "/mnt/tmp",
                }
            )
            command, process_cwd = self._sandbox_command(command, state_root)
        self._process = subprocess.Popen(
            command,
            cwd=process_cwd,
            env=environment,
            bufsize=0,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if self._process.stdout is not None:
            os.set_blocking(self._process.stdout.fileno(), False)
        if self._process.stderr is not None:
            os.set_blocking(self._process.stderr.fileno(), False)
        try:
            self._request(
                "initialize",
                {
                    "clientInfo": {"name": "session-handoff-information-benchmark", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._notify("initialized")
        except Exception:
            self.close()
            raise

    def _sandbox_command(
        self, command: Sequence[str], state_root: Path
    ) -> tuple[list[str], str | None]:
        sandbox = shutil.which(self.sandbox_executable or "")
        if not sandbox:
            raise BenchmarkGateError("native execution requires bubblewrap for gold isolation")
        with self.binary.open("rb") as binary_file:
            is_wrapper = binary_file.read(2) == b"#!"
        if is_wrapper:
            raise BenchmarkGateError(
                "native sandbox requires the pinned ELF binary, not a wrapper executable"
            )
        masked: list[Path] = []
        for candidate in sorted(
            {Path.home().resolve(), Path("/tmp").resolve(), *self.hidden_paths},
            key=lambda path: len(path.parts),
        ):
            if any(candidate == parent or parent in candidate.parents for parent in masked):
                continue
            if candidate.is_dir():
                masked.append(candidate)
        result = [sandbox, "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        if not self.allow_network:
            result.append("--unshare-net")
        for path in masked:
            result.extend(("--tmpfs", str(path)))
        executable_paths = [self.binary]
        companion = self.binary.parent / "codex-code-mode-host"
        if companion.is_file():
            executable_paths.append(companion)
        for path in executable_paths:
            for masked_root in masked:
                try:
                    relative_parent = path.parent.relative_to(masked_root)
                except ValueError:
                    continue
                current = masked_root
                for part in relative_parent.parts:
                    current /= part
                    result.extend(("--dir", str(current)))
                break
            result.extend(("--ro-bind", str(path), str(path)))
        result.extend(("--tmpfs", "/mnt", "--dir", "/mnt/work"))
        if self.cwd:
            result.extend(("--bind", str(self.cwd), "/mnt/work"))
        result.extend(
            (
                "--dir",
                "/mnt/native",
                "--bind",
                str(state_root),
                "/mnt/native",
                "--dir",
                "/mnt/tmp",
            )
        )
        if self.credential_source:
            try:
                credential_stat = self.credential_source.stat()
            except OSError as exc:
                raise BenchmarkGateError("configured native credential mount is unavailable") from exc
            if not stat.S_ISREG(credential_stat.st_mode):
                raise BenchmarkGateError("configured native credential mount is unavailable")
            if credential_stat.st_mode & 0o077:
                raise BenchmarkGateError("configured native credential must be owner-readable only")
            result.extend(
                (
                    "--ro-bind",
                    str(self.credential_source),
                    "/mnt/native/codex/auth.json",
                )
            )
        result.extend(("--chdir", "/mnt/work", "--", *command))
        return result, None

    def _notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        if self._process is None or self._process.stdin is None:
            raise ProtocolError("app-server is not running")
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        self._process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        self._process.stdin.flush()

    def _read_stderr_excerpt(self) -> str:
        if self._process is None or self._process.stderr is None:
            return self.stderr_excerpt
        try:
            chunk = os.read(self._process.stderr.fileno(), 4096)
        except (BlockingIOError, OSError):
            return self.stderr_excerpt
        if chunk:
            self.stderr_excerpt = (self.stderr_excerpt + chunk.decode("utf-8", "replace"))[-4096:]
        return self.stderr_excerpt

    def _read_event(self, selector: selectors.BaseSelector, deadline: float) -> dict[str, Any]:
        """Read one complete JSON line without blocking on a partial child line."""

        if self._process is None or self._process.stdout is None:
            raise ProtocolError("app-server is not running")
        stream = self._process.stdout
        while time.monotonic() < deadline:
            newline = self._stdout_buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self._stdout_buffer[:newline])
                del self._stdout_buffer[: newline + 1]
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProtocolError("app-server emitted invalid JSON") from exc
                if not isinstance(event, dict):
                    raise ProtocolError("app-server event must be an object")
                return event
            ready = selector.select(max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            try:
                chunk = os.read(stream.fileno(), 65536)
            except BlockingIOError:
                continue
            if not chunk:
                detail = self._read_stderr_excerpt()
                suffix = f": {detail.strip()[:1000]}" if detail.strip() else ""
                raise ProtocolError(f"app-server closed stdout{suffix}")
            self._stdout_buffer.extend(chunk)
        raise CompactionTimeout("app-server response deadline expired")

    def _request(self, method: str, params: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise ProtocolError("app-server is not running")
        request_id = self._next_id
        self._next_id += 1
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        self._process.stdin.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
        self._process.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(self._process.stdout, selectors.EVENT_READ)
        events: list[dict[str, Any]] = []
        deadline = time.monotonic() + self.deadline
        try:
            while time.monotonic() < deadline:
                event = self._read_event(selector, deadline)
                if "id" in event and event.get("id") == request_id:
                    if "error" in event:
                        raise ProtocolError(f"app-server request failed: {method}")
                    return event, events
                if "id" in event and "method" not in event:
                    raise ProtocolError("app-server response id mismatch")
                events.append(event)
            raise CompactionTimeout(f"app-server request timed out: {method}")
        finally:
            selector.close()

    def _request_until_compacted(self, thread_id: str) -> dict[str, Any]:
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise ProtocolError("app-server is not running")
        request_id = self._next_id
        self._next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "thread/compact/start",
            "params": {"threadId": thread_id},
        }
        self._process.stdin.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
        self._process.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(self._process.stdout, selectors.EVENT_READ)
        events: list[dict[str, Any]] = []
        deadline = time.monotonic() + self.deadline
        try:
            while time.monotonic() < deadline:
                try:
                    event = self._read_event(selector, deadline)
                except CompactionTimeout as exc:
                    raise CompactionTimeout("compact completion deadline expired") from exc
                events.append(event)
                try:
                    return compact_completion(events, request_id=request_id, thread_id=thread_id)
                except CompactionError as exc:
                    if isinstance(exc, CompactionTimeout):
                        raise
                    if "acknowledged without completion" not in str(exc):
                        raise
                    continue
                except ProtocolError as exc:
                    if "response was not correlated" in str(exc):
                        continue
                    raise
            raise CompactionTimeout("compact completion deadline expired")
        finally:
            selector.close()

    def _request_until_turn_completed(
        self, thread_id: str, prompt: str, *, workspace_write: bool = False
    ) -> dict[str, Any]:
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise ProtocolError("app-server is not running")
        request_id = self._next_id
        self._next_id += 1
        runtime_cwd = "/mnt/work" if self.sandbox_executable else (str(self.cwd) if self.cwd else None)
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "turn/start",
            "params": {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "model": self.model,
                "effort": self.reasoning_effort,
                "approvalPolicy": "never",
                "dynamicTools": [],
            },
        }
        if self.sandbox_executable:
            request["params"]["permissions"] = (
                WRITE_PERMISSION_PROFILE if workspace_write else READ_PERMISSION_PROFILE
            )
        else:
            request["params"]["sandboxPolicy"] = {
                "type": "workspaceWrite" if workspace_write else "readOnly",
                "networkAccess": self.allow_network if workspace_write else False,
                **({"writableRoots": [runtime_cwd] if runtime_cwd else []} if workspace_write else {}),
            }
        self._process.stdin.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
        self._process.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(self._process.stdout, selectors.EVENT_READ)
        acknowledged = False
        acknowledged_turn_id: str | None = None
        output_parts: list[str] = []
        tool_items: list[dict[str, Any]] = []
        protocol_events: list[dict[str, Any]] = []
        pending_items: list[Mapping[str, Any]] = []
        pending_errors: list[Mapping[str, Any]] = []
        pending_turns: list[Mapping[str, Any]] = []

        def consume_item(item_params: Mapping[str, Any]) -> None:
            item = item_params.get("item")
            if not isinstance(item, Mapping):
                raise ProtocolError("turn item completion omitted item")
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                output_parts.append(item["text"])
            elif item.get("type") not in {
                "reasoning", "contextCompaction", "userMessage", "agentMessage"
            }:
                tool_items.append(_tool_item_evidence(item, source="item/completed"))

        def reclassify_events() -> None:
            for evidence in protocol_events:
                if evidence.get("thread_id") == thread_id:
                    evidence["classification"] = (
                        "target"
                        if evidence.get("turn_id") == acknowledged_turn_id
                        else "foreign"
                    )

        def finish_turn(turn: Mapping[str, Any]) -> dict[str, Any]:
            if turn.get("id") != acknowledged_turn_id:
                raise ProtocolError("turn completion id does not match turn/start")
            status = turn.get("status")
            if status != "completed" or turn.get("error") is not None:
                if status in {"failed", "interrupted"} or turn.get("error") is not None:
                    raise ProtocolError(f"turn completed with status {status or 'error'}")
                raise ProtocolError(f"turn completed with invalid status {status!r}")
            if isinstance(turn.get("items"), list):
                for item in turn["items"]:
                    if not isinstance(item, Mapping):
                        continue
                    if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                        if item["text"] not in output_parts:
                            output_parts.append(item["text"])
                    elif item.get("type") not in {
                        "reasoning", "contextCompaction", "userMessage", "agentMessage"
                    }:
                        tool_items.append(_tool_item_evidence(item, source="turn.items"))
            return {
                "completed": True,
                "thread_id": thread_id,
                "turn_id": turn.get("id"),
                "completion_event": "turn/completed",
                "output": "\n".join(output_parts) if output_parts else None,
                "tool_used": bool(tool_items),
                "tool_items": tool_items,
                "protocol_events": protocol_events,
                "usage": turn.get("usage") if isinstance(turn.get("usage"), Mapping) else None,
                "opaque": True,
            }

        deadline = time.monotonic() + self.deadline
        try:
            while time.monotonic() < deadline:
                event = self._read_event(selector, deadline)
                if isinstance(event.get("method"), str):
                    protocol_events.append(
                        _protocol_event_evidence(
                            event,
                            request_id=request_id,
                            target_thread_id=thread_id,
                            target_turn_id=acknowledged_turn_id,
                        )
                    )
                if event.get("id") == request_id:
                    if "error" in event:
                        raise ProtocolError("turn/start request failed")
                    result = event.get("result")
                    if not isinstance(result, Mapping) or not isinstance(result.get("turn"), Mapping):
                        raise ProtocolError("turn/start returned an invalid response")
                    acknowledged_turn_id = result["turn"].get("id")
                    if not isinstance(acknowledged_turn_id, str) or not acknowledged_turn_id:
                        raise ProtocolError("turn/start response omitted turn id")
                    acknowledged = True
                    reclassify_events()
                    for item_params in pending_items:
                        if item_params.get("turnId") == acknowledged_turn_id:
                            consume_item(item_params)
                    pending_items.clear()
                    for error_params in pending_errors:
                        if error_params.get("turnId") == acknowledged_turn_id:
                            raise ProtocolError("turn completion failed")
                    pending_errors.clear()
                    for turn_params in pending_turns:
                        turn = turn_params.get("turn")
                        if isinstance(turn, Mapping) and turn.get("id") == acknowledged_turn_id:
                            return finish_turn(turn)
                    pending_turns.clear()
                    continue
                if event.get("method") == "error":
                    params = event.get("params")
                    if not isinstance(params, Mapping):
                        raise ProtocolError("turn error notification has invalid params")
                    event_thread_id = params.get("threadId")
                    event_turn_id = params.get("turnId")
                    if not isinstance(event_thread_id, str) or not isinstance(event_turn_id, str):
                        raise ProtocolError("turn error notification omitted correlation")
                    if event_thread_id != thread_id:
                        continue
                    if not acknowledged:
                        pending_errors.append(params)
                        continue
                    if event_turn_id != acknowledged_turn_id:
                        continue
                    raise ProtocolError("turn completion failed")
                if event.get("method") != "turn/completed":
                    if event.get("method") == "item/completed":
                        item_params = event.get("params")
                        if not isinstance(item_params, Mapping):
                            raise ProtocolError("turn item completion has invalid params")
                        event_thread_id = item_params.get("threadId")
                        event_turn_id = item_params.get("turnId")
                        if not isinstance(event_thread_id, str) or not isinstance(event_turn_id, str):
                            raise ProtocolError("turn item completion omitted correlation")
                        if event_thread_id != thread_id:
                            continue
                        if not acknowledged:
                            pending_items.append(item_params)
                            continue
                        if event_turn_id != acknowledged_turn_id:
                            continue
                        consume_item(item_params)
                    continue
                params = event.get("params")
                if not isinstance(params, Mapping):
                    raise ProtocolError("turn completion has invalid params")
                if params.get("threadId") != thread_id:
                    continue
                turn = params.get("turn")
                if not isinstance(turn, Mapping):
                    raise ProtocolError("turn completion omitted turn")
                if not acknowledged:
                    pending_turns.append(params)
                    continue
                if turn.get("id") != acknowledged_turn_id:
                    continue
                return finish_turn(turn)
            raise CompactionTimeout("turn completion deadline expired")
        except (ProtocolError, CompactionTimeout) as exc:
            setattr(exc, "protocol_events", protocol_events)
            raise
        finally:
            selector.close()

    def start_thread(self) -> str:
        runtime_cwd = "/mnt/work" if self.sandbox_executable else (str(self.cwd) if self.cwd else None)
        params: dict[str, Any] = {
                "model": self.model,
                "cwd": runtime_cwd,
                "ephemeral": False,
                "historyMode": "paginated",
                "approvalPolicy": "never",
                "dynamicTools": [],
            }
        if self.sandbox_executable:
            params["permissions"] = READ_PERMISSION_PROFILE
        else:
            params["sandboxPolicy"] = {"type": "readOnly", "networkAccess": False}
        response, _ = self._request("thread/start", params)
        thread = response.get("result", {}).get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise ProtocolError("thread/start omitted thread id")
        return thread["id"]

    def inject_items(self, thread_id: str, conversation: Mapping[str, Any]) -> None:
        response, _ = self._request(
            "thread/inject_items",
            {"threadId": thread_id, "items": conversation_to_response_items(conversation)},
        )
        if not isinstance(response.get("result"), dict):
            raise ProtocolError("thread/inject_items returned an invalid response")

    def compact_thread(self, thread_id: str) -> dict[str, Any]:
        return self._request_until_compacted(thread_id)

    def continue_thread(
        self, thread_id: str, prompt: str, *, workspace_write: bool = False
    ) -> dict[str, Any]:
        """Run one continuation turn on the supplied thread and await completion."""

        return self._request_until_turn_completed(thread_id, prompt, workspace_write=workspace_write)

    def fork_thread(self, thread_id: str) -> str:
        response, _ = self._request(
            "thread/fork",
            {"threadId": thread_id, "ephemeral": False, "excludeTurns": True},
        )
        thread = response.get("result", {}).get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise ProtocolError("thread/fork omitted thread id")
        return thread["id"]

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        response, _ = self._request("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = response.get("result", {}).get("thread")
        if not isinstance(thread, dict):
            raise ProtocolError("thread/read omitted thread")
        return thread

    def list_items(self, thread_id: str) -> list[Any]:
        items: list[Any] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            params: dict[str, Any] = {"threadId": thread_id}
            if cursor is not None:
                params["cursor"] = cursor
            response, _ = self._request("thread/items/list", params)
            result = response.get("result", {})
            data = result.get("data", []) if isinstance(result, Mapping) else None
            if not isinstance(data, list):
                raise ProtocolError("thread/items/list returned invalid data")
            items.extend(data)
            next_cursor = result.get("nextCursor") if isinstance(result, Mapping) else None
            if next_cursor is None:
                next_cursor = result.get("next_cursor") if isinstance(result, Mapping) else None
            if not isinstance(next_cursor, str) or not next_cursor:
                return items
            if next_cursor in seen:
                raise ProtocolError("thread/items/list cursor repeated")
            seen.add(next_cursor)
            cursor = next_cursor

    def close(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        if self._state_temp is not None:
            self._state_temp.cleanup()
            self._state_temp = None


# Descriptive aliases for callers that name the adapter after its role.
CodexAppServer = NativeAppServer
NativeCompactAdapter = NativeAppServer


def compare_handoff_contracts(main: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    main_prompt = main.get("prompt")
    candidate_prompt = candidate.get("prompt")
    main_contract = main.get("contract") if isinstance(main.get("contract"), Mapping) else {}
    candidate_contract = candidate.get("contract") if isinstance(candidate.get("contract"), Mapping) else {}
    ignored_contract_keys = {
        "arm",
        "product_tree_sha256",
        "storage",
        "build_root",
    }
    comparable_main = {
        key: value for key, value in main_contract.items() if key not in ignored_contract_keys
    }
    comparable_candidate = {
        key: value
        for key, value in candidate_contract.items()
        if key not in ignored_contract_keys
    }
    equivalent = (
        isinstance(main_prompt, str)
        and main_prompt == candidate_prompt
        and comparable_main == comparable_candidate
    )
    return {
        "equivalent": equivalent,
        "main_prompt_sha256": _sha256(main_prompt) if isinstance(main_prompt, str) else None,
        "candidate_prompt_sha256": _sha256(candidate_prompt) if isinstance(candidate_prompt, str) else None,
        "main_contract_sha256": _sha256(json.dumps(main_contract, sort_keys=True, separators=(",", ":"))),
        "candidate_contract_sha256": _sha256(json.dumps(candidate_contract, sort_keys=True, separators=(",", ":"))),
        "main_comparable_contract_sha256": _sha256(
            json.dumps(comparable_main, sort_keys=True, separators=(",", ":"))
        ),
        "candidate_comparable_contract_sha256": _sha256(
            json.dumps(comparable_candidate, sort_keys=True, separators=(",", ":"))
        ),
        "reason": (
            "same build instructions and generation contract; runtime storage differs"
            if equivalent
            else "build instructions or generation contracts differ"
        ),
    }


def score_recoverability(probes: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(probes)
    if not rows:
        raise BenchmarkGateError("recoverability needs at least one probe")
    known = [row for row in rows if isinstance(row.get("recoverable"), bool)]
    recoverable = sum(bool(row.get("recoverable")) for row in known)
    return {
        "probes": len(rows),
        "known_probes": len(known),
        "unknown_probes": len(rows) - len(known),
        "recoverable": recoverable,
        "recoverability_rate": recoverable / len(known) if known else None,
        "opaque_state": True,
        "text_recall": None,
        "scoring_status": "exact_match_proxy" if known else "unscored",
        "manual_review_required": True,
    }


def continuation_result(
    *, task_success: bool, hidden_acceptance: bool, continuation_completed: bool | None = None
) -> dict[str, Any]:
    return {
        "task_success": bool(task_success),
        "hidden_acceptance": bool(hidden_acceptance),
        "continuation_completed": continuation_completed,
        "recoverability_probe_separate": True,
    }


def _run_evaluator(command: Sequence[str], workspace: Path, timeout: float) -> dict[str, Any]:
    """Run visible/hidden checks outside the agent namespace."""

    environment = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "PYTHONPATH")
        if key in os.environ
    }
    try:
        process = subprocess.run(
            list(command),
            cwd=str(workspace),
            env=environment,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "passed": False,
            "returncode": None,
            "stdout_sha256": _sha256(exc.stdout or ""),
            "stderr_sha256": _sha256(exc.stderr or ""),
        }
    return {
        "status": "passed" if process.returncode == 0 else "failed",
        "passed": process.returncode == 0,
        "returncode": process.returncode,
        "stdout_sha256": _sha256(process.stdout),
        "stderr_sha256": _sha256(process.stderr),
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _select_plan_cells(
    plan: Mapping[str, Any], exclude_cell_ids: Sequence[str] = ()
) -> list[dict[str, Any]]:
    cells = plan.get("cells")
    if not isinstance(cells, list):
        raise BenchmarkGateError("benchmark plan has no cells")
    excluded = tuple(exclude_cell_ids)
    if any(not isinstance(cell_id, str) or not cell_id for cell_id in excluded):
        raise BenchmarkGateError("unknown excluded cell")
    if len(set(excluded)) != len(excluded):
        raise BenchmarkGateError("duplicate excluded cell")
    available = {
        cell.get("cell_id")
        for cell in cells
        if isinstance(cell, Mapping) and isinstance(cell.get("cell_id"), str)
    }
    unknown = sorted(set(excluded) - available)
    if unknown:
        raise BenchmarkGateError(f"unknown excluded cell: {unknown[0]}")
    selected = [
        dict(cell)
        for cell in cells
        if isinstance(cell, Mapping) and cell.get("cell_id") not in set(excluded)
    ]
    if not selected:
        raise BenchmarkGateError("all selected cells are excluded")
    return selected


def _write_blinded_packets(
    output_path: Path, row: Mapping[str, Any], case: Mapping[str, Any]
) -> str:
    """Write condition-blinded evidence and a separate private gold packet."""

    blind_id = _sha256(
        f"{row.get('cell_id')}:{row.get('arm')}:{row.get('replicate')}"
    )[:20]
    probes = row.get("probes") if isinstance(row.get("probes"), list) else []
    evidence_probes = [
        {
            "probe": probe.get("probe"),
            "raw_answer": probe.get("raw_answer"),
            "model_queried": probe.get("model_queried"),
            "opaque_state": probe.get("opaque_state"),
            "tool_items": probe.get("tool_items", []),
            "tool_use_violation": probe.get("tool_use_violation", False),
        }
        for probe in probes
        if isinstance(probe, Mapping)
    ]
    judge = {
        "schema_version": 1,
        "blind_id": blind_id,
        "evidence": {
            "handoff_text": row.get("handoff", {}).get("generated_text")
            if isinstance(row.get("handoff"), Mapping)
            else None,
            "continuation_text": row.get("continuation", {}).get("output")
            if isinstance(row.get("continuation"), Mapping)
            else None,
            "generation_tool_items": row.get("generation_tool_items", []),
            "continuation_tool_items": row.get("continuation", {}).get("tool_items", [])
            if isinstance(row.get("continuation"), Mapping)
            else [],
            "probes": evidence_probes,
            "visible_verification": row.get("verification"),
            "hidden_evaluation": row.get("hidden_evaluation"),
            "workspace_diff": row.get("workspace_diff"),
        },
        "facts": [
            {"id": fact.get("id"), "status": None, "critical": bool(fact.get("critical"))}
            for fact in case.get("gold_facts", [])
            if isinstance(fact, Mapping)
        ],
        "stale_traps": [
            {"id": trap.get("id"), "activated": None}
            for trap in case.get("stale_traps", [])
            if isinstance(trap, Mapping)
        ],
        "dod": [
            {"id": item.get("id"), "passed": None}
            for item in case.get("dod", [])
            if isinstance(item, Mapping)
        ],
        "counters": {
            "repeated_failed_attempts": None,
            "stale_decisions_acted_on": None,
            "recovery_reads": None,
        },
        "manual_status": "pending",
    }
    judge_path = output_path / "blinded" / blind_id / "judge.json"
    _write_json(judge_path, judge)
    judge_path.parent.chmod(0o700)
    private_root = output_path / "private"
    private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_root.chmod(0o700)
    gold_path = private_root / "gold" / f"{blind_id}.json"
    _write_json(
        gold_path,
        {
            "blind_id": blind_id,
            "cell_id": row.get("cell_id"),
            "arm": row.get("arm"),
            "case": row.get("case"),
            "band": row.get("band"),
            "replicate": row.get("replicate"),
            "arm_order": row.get("arm_order"),
            "gold_facts": case.get("gold_facts", []),
            "stale_traps": case.get("stale_traps", []),
            "dod": case.get("dod", []),
        },
    )
    gold_path.parent.chmod(0o700)
    gold_path.chmod(0o600)
    mapping_path = private_root / "blind-map.json"
    if mapping_path.exists():
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    else:
        mapping = {}
    mapping[blind_id] = {
        "cell_id": row.get("cell_id"),
        "arm": row.get("arm"),
        "case": row.get("case"),
        "band": row.get("band"),
        "replicate": row.get("replicate"),
        "gold_packet": str(gold_path.relative_to(private_root)),
    }
    _write_json(mapping_path, mapping)
    mapping_path.chmod(0o600)
    return blind_id


def _handoff_generation_prompt(*, arm: str, contract: Mapping[str, Any]) -> str:
    instructions = contract.get("create_mode_instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise BenchmarkGateError(f"{arm} build has no loaded Create mode instructions")
    prompt_contract = {
        key: value
        for key, value in contract.items()
        if key
        not in {
            "arm",
            "product_tree_sha256",
            "storage",
            "create_mode_instructions",
            "build_root",
        }
    }
    return (
        "Create the implementation-state handoff from the authoritative structured conversation "
        "already present in this thread, following the measured build instructions below. Return "
        "only the document required by those instructions. Preserve current constraints, "
        "authoritative decisions, rejected attempts, completed and pending work, exact "
        "paths/tests, and the next safe action; exclude routine diagnostic noise. This is only "
        "generation: do not read files, use tools, call MCP, migrate, or request auto_switch. "
        "The benchmark runner, not this turn, performs product persistence and the subsequent "
        "MCP create/read roundtrip. Build-generation contract metadata: "
        f"{json.dumps(prompt_contract, sort_keys=True)}\n\n"
        "Measured build Create mode instructions:\n"
        f"{instructions}"
    )


FINAL_TASK_PROMPT = (
    "Continue the task from the supplied authoritative state. Work in the isolated repository and "
    "make the required change. The harness runs visible and hidden verification after this turn; "
    "do not use external context, do not run git commands, and do not merely describe the change."
)


def _new_thread_with_handoff(adapter: NativeAppServer, handoff: str) -> str:
    thread_id = adapter.start_thread()
    conversation = {
        "schema_version": 1,
        "case": "handoff",
        "band": "short",
        "replicate": 1,
        "checkpoint": 1,
        "messages": [{"id": "handoff", "role": "user", "content": handoff}],
        "role_order": ["user"],
    }
    adapter.inject_items(thread_id, conversation)
    return thread_id


def execute_benchmark(
    *,
    output: str | Path,
    native_binary: str | Path,
    native_interface: NativeInterface,
    main_root: str | Path,
    candidate_root: str | Path,
    cases: Sequence[str] = CASES,
    bands: Sequence[str] = BANDS,
    replicates: int = REPLICATES,
    main_storage: str = "legacy",
    candidate_storage: str = "central",
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    deadline: float = 30.0,
    verified_context_budget_tokens: int | None = None,
    runtime_authorized: bool = False,
    sandbox_executable: str | None = "bwrap",
    hidden_paths: Sequence[str | Path] = (),
    credential_source: str | Path | None = None,
    allow_network: bool = False,
    exclude_cell_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Run the three real app-server arms after the explicit runtime gate."""

    if not runtime_authorized:
        raise BenchmarkGateError("benchmark execution requires the explicit runtime gate")
    if not native_interface.verified:
        raise BenchmarkGateError("native schema must be verified before execution")
    if deadline <= 0:
        raise BenchmarkGateError("deadline must be positive")
    if "near_threshold" in bands:
        raise BenchmarkGateError(
            "near-threshold execution is blocked until the native context budget is measured"
        )
    main_root_path = Path(main_root).expanduser().resolve()
    candidate_root_path = Path(candidate_root).expanduser().resolve()
    for label, root in (("main", main_root_path), ("candidate", candidate_root_path)):
        if not (root / "server/handoff_mcp.py").is_file():
            raise BenchmarkGateError(f"{label} product MCP server not found: {root}")
    output_path = Path(output).expanduser().resolve()
    if output_path.exists() and any(output_path.iterdir()):
        raise BenchmarkGateError(f"output directory is not empty: {output_path}")
    output_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_path.chmod(0o700)
    plan = dict(
        plan_benchmark(
            cases=cases,
            bands=bands,
            replicates=replicates,
            model=model,
            reasoning_effort=reasoning_effort,
            verified_context_budget_tokens=verified_context_budget_tokens,
        )
    )
    plan.update(
        execution="authorized",
        gate="runtime-authorized",
        native_schema_sha256=native_interface.schema_sha256,
        native_version=native_interface.version,
    )
    selected_cells = _select_plan_cells(plan, exclude_cell_ids)
    plan.update(
        excluded_cell_ids=list(exclude_cell_ids),
        excluded_continuations=len(exclude_cell_ids),
        remaining_continuations=len(selected_cells),
    )
    _write_json(output_path / "plan.json", plan)
    workspace_root = output_path / "workspaces"
    results: list[dict[str, Any]] = []
    native_compactions = 0
    probe_count = 0
    fatal_failure: dict[str, str] | None = None
    from benchmark.fixture_workspace import materialize_workspace
    from benchmark.product_roundtrip import product_identity

    identities = {
        "main": product_identity(main_root_path),
        "candidate": product_identity(candidate_root_path),
    }
    build_instructions = {
        "main": _load_build_instructions(main_root_path),
        "candidate": _load_build_instructions(candidate_root_path),
    }
    generator_contract = {
        "format": "markdown-v1",
        "generator": "app-server-turn-start",
        "model": model,
        "reasoning_effort": reasoning_effort,
        "prompt_version": "information-preservation-v3",
    }
    generator_contract["contract_sha256"] = _sha256(
        json.dumps(generator_contract, sort_keys=True, separators=(",", ":"))
    )
    handoff_contracts: dict[str, dict[str, Any]] = {}
    logical_cells: dict[tuple[str, str, int], dict[str, dict[str, Any]]] = {}
    for cell in selected_cells:
        key = (cell["case"], cell["band"], cell["replicate"])
        logical_cells.setdefault(key, {})[cell["arm"]] = cell
    for arm_cells in logical_cells.values():
        base_cell = next(iter(arm_cells.values()))
        conversation = build_structured_conversation(base_cell["case"], base_cell["band"], base_cell["replicate"])
        response_items = conversation_to_response_items(conversation)
        history_hash = _sha256(json.dumps(response_items, sort_keys=True, separators=(",", ":")))
        case = _fixture_cases()[base_cell["case"]]
        gold_facts = case.get("gold_facts")
        planned_arm_order = tuple(base_cell.get("arm_order", list(ARMS)))
        arm_order = tuple(arm for arm in planned_arm_order if arm in arm_cells)
        for arm in arm_order:
            cell = arm_cells[arm]
            arm_root = workspace_root / cell["cell_id"] / arm
            workspace = arm_root / "workspace"
            fixture = materialize_workspace(cell["case"], workspace)
            workspace_before = _workspace_snapshot(workspace)
            row: dict[str, Any] = {
                "cell_id": cell["cell_id"],
                "case": cell["case"],
                "band": cell["band"],
                "replicate": cell["replicate"],
                "arm": arm,
                "arm_order": list(arm_order),
                "planned_arm_order": list(planned_arm_order),
                "arm_position": arm_order.index(arm) + 1,
                "history_sha256": history_hash,
                "generation_prompt_sha256": None,
                "final_prompt_sha256": _sha256(FINAL_TASK_PROMPT),
                "response_item_count": len(response_items),
                "workspace": str(workspace),
                "gold_transport": False,
                "probe_context_injected": False,
                "preparation_started": False,
                "preparation_completed": False,
                "continuation_attempted": False,
                "continuation_completed": False,
                "started_at_ns": time.time_ns(),
            }
            checkpoint: dict[str, Any] | None = None
            probes: list[dict[str, Any]] = []
            try:
                product_root = main_root_path if arm == "main" else candidate_root_path
                product_storage = main_storage if arm == "main" else candidate_storage
                row["preparation_started"] = True
                with NativeAppServer(
                    binary=native_binary,
                    interface=native_interface,
                    cwd=workspace,
                    deadline=deadline,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    sandbox_executable=sandbox_executable,
                    hidden_paths=hidden_paths,
                    credential_source=credential_source,
                    allow_network=allow_network,
                ) as adapter:
                    source_thread = adapter.start_thread()
                    adapter.inject_items(source_thread, conversation)
                    row["injected_history_sha256"] = history_hash
                    if arm == "nativecompact":
                        checkpoint = checkpoint_native(adapter, source_thread)
                        native_compactions += 1
                        probes = fork_recovery_probes(
                            adapter,
                            source_thread,
                            gold_facts=gold_facts if isinstance(gold_facts, list) else None,
                        )
                        task_thread = source_thread
                    else:
                        contract = {
                            **generator_contract,
                            "arm": arm,
                            "product_tree_sha256": identities[arm]["tree_sha256"],
                            "storage": product_storage,
                            "build_root": str(product_root),
                            "skill_path": build_instructions[arm]["path"],
                            "skill_sha256": build_instructions[arm]["sha256"],
                            "create_mode_sha256": build_instructions[arm]["create_mode_sha256"],
                            "structure_sha256": build_instructions[arm]["structure_sha256"],
                            "create_mode_instructions": build_instructions[arm]["create_mode"],
                        }
                        row["handoff_contract"] = {
                            key: value
                            for key, value in contract.items()
                            if key != "create_mode_instructions"
                        }
                        generation_prompt = _handoff_generation_prompt(
                            arm=arm, contract=contract
                        )
                        generation = adapter.continue_thread(
                            source_thread,
                            generation_prompt,
                        )
                        row["generation_tool_items"] = generation.get("tool_items", [])
                        if generation.get("tool_used") is True:
                            raise ProtocolError("handoff generation used a tool")
                        row["generation_prompt_sha256"] = _sha256(generation_prompt)
                        generated = generation.get("output")
                        if not isinstance(generated, str) or not generated.strip():
                            raise ProtocolError("handoff generation returned no assistant text")
                        handoff = create_read_handoff(
                            product_root,
                            storage=product_storage,
                            isolated_root=arm_root / "product-state",
                            workspace=workspace,
                            content=generated,
                        )
                        handoff_contracts[arm] = contract
                        row["handoff"] = {
                            "read_verified": handoff["read_verified"],
                            "generated_text": generated,
                            "content_sha256": handoff["content_sha256"],
                            "generated_content_sha256": handoff["generated_content_sha256"],
                            "create_response_sha256": handoff["create_response_sha256"],
                            "read_response_sha256": handoff["read_response_sha256"],
                            "storage": handoff["storage"],
                        }
                        task_thread = _new_thread_with_handoff(adapter, handoff["content"])
                        probes = fork_recovery_probes(
                            adapter,
                            task_thread,
                            gold_facts=gold_facts if isinstance(gold_facts, list) else None,
                        )
                    probe_count += len(probes)
                    row["checkpoint"] = checkpoint
                    row["probes"] = probes
                    row["preparation_completed"] = True
                    row["continuation_attempted"] = True
                    continuation = adapter.continue_thread(
                        task_thread, FINAL_TASK_PROMPT, workspace_write=True
                    )
                    row["continuation_completed"] = continuation.get("completed") is True
                    row["continuation"] = {
                        "completed": continuation.get("completed") is True,
                        "turn_id": continuation.get("turn_id"),
                        "usage": continuation.get("usage"),
                        "output": continuation.get("output"),
                        "tool_items": continuation.get("tool_items", []),
                        "protocol_events": continuation.get("protocol_events", []),
                        "output_sha256": _sha256(continuation["output"])
                        if isinstance(continuation.get("output"), str)
                        else None,
                    }
                    row["workspace_diff"] = _workspace_diff(
                        workspace_before, _workspace_snapshot(workspace)
                    )
                verify = _run_evaluator(fixture["verify_command"], workspace, deadline)
                hidden = _run_evaluator(fixture["acceptance_command"], workspace, deadline)
                row["verification"] = verify
                row["hidden_evaluation"] = {
                    "passed": hidden["passed"],
                    "status": hidden["status"],
                    "returncode": hidden["returncode"],
                    "stdout_sha256": hidden["stdout_sha256"],
                    "stderr_sha256": hidden["stderr_sha256"],
                }
                task_success = (
                    row["continuation"].get("completed") is True
                    and verify["passed"] is True
                    and hidden["passed"] is True
                )
                row["score"] = continuation_result(
                    task_success=task_success,
                    hidden_acceptance=hidden["passed"],
                    continuation_completed=row["continuation"]["completed"],
                )
                row["score"].update(
                    semantic_fact_score=None,
                    semantic_score_status="blinded_pending_manual_scoring",
                )
            except ProtocolError as exc:
                fatal_failure = {"type": type(exc).__name__, "message": str(exc)}
                if hasattr(exc, "evidence"):
                    row["probes"] = getattr(exc, "evidence")
                if hasattr(exc, "protocol_events"):
                    protocol_events = getattr(exc, "protocol_events")
                    row["protocol_events"] = protocol_events
                row["workspace_diff"] = _workspace_diff(
                    workspace_before, _workspace_snapshot(workspace)
                )
                row["failure"] = dict(fatal_failure)
                row["continuation"] = {"completed": False}
                if hasattr(exc, "protocol_events"):
                    row["continuation"]["protocol_events"] = getattr(exc, "protocol_events")
                row["score"] = continuation_result(
                    task_success=False, hidden_acceptance=False, continuation_completed=False
                )
                row["score"].update(semantic_fact_score=None, semantic_score_status="unscored")
            except (BenchmarkGateError, OSError, ValueError, RuntimeError) as exc:
                fatal_failure = {"type": type(exc).__name__, "message": str(exc)}
                row["workspace_diff"] = _workspace_diff(
                    workspace_before, _workspace_snapshot(workspace)
                )
                row["failure"] = {"type": type(exc).__name__, "message": str(exc)}
                row["continuation"] = {"completed": False}
                row["score"] = continuation_result(
                    task_success=False, hidden_acceptance=False, continuation_completed=False
                )
                row["score"].update(semantic_fact_score=None, semantic_score_status="unscored")
            row["metadata"] = run_metadata(
                build=str(native_binary if arm == "nativecompact" else (main_root_path if arm == "main" else candidate_root_path)),
                client="codex-app-server",
                model=model,
                effort=reasoning_effort,
                prompt=FINAL_TASK_PROMPT,
                compaction={
                    "actual": checkpoint is not None,
                    "truncation": checkpoint.get("truncated") if checkpoint else None,
                    "fallback": checkpoint.get("fallback") if checkpoint else None,
                },
            )
            row["blind_id"] = _write_blinded_packets(output_path, row, case)
            _write_json(output_path / "cells" / cell["cell_id"] / f"{arm}.json", row)
            results.append(row)
            if fatal_failure is not None:
                break
        if fatal_failure is not None:
            break
    attempted_continuations = sum(
        bool(row.get("continuation_attempted")) for row in results
    )
    completed_continuations = sum(
        bool(row.get("continuation_completed")) for row in results
    )
    preparation_failures = sum(
        bool(row.get("preparation_started")) and not bool(row.get("preparation_completed"))
        for row in results
    )
    native_verified = native_compactions == len(logical_cells)
    expected_continuations = len(selected_cells)
    completed = (
        fatal_failure is None
        and len(results) == expected_continuations
        and attempted_continuations == expected_continuations
        and completed_continuations == expected_continuations
        and native_verified
        and all("failure" not in row for row in results)
    )
    comparison = None
    if "main" in handoff_contracts and "candidate" in handoff_contracts:
        comparison = compare_handoff_contracts(
            {
                "prompt": _handoff_generation_prompt(
                    arm="main", contract=handoff_contracts["main"]
                ),
                "contract": handoff_contracts["main"],
            },
            {
                "prompt": _handoff_generation_prompt(
                    arm="candidate", contract=handoff_contracts["candidate"]
                ),
                "contract": handoff_contracts["candidate"],
            },
        )
    summary = {
        "schema_version": 1,
        "gate": gate_state(native_compact_verified=native_verified, benchmark_completed=completed),
        "planned_continuations": plan["continuations"],
        "excluded_cell_ids": list(exclude_cell_ids),
        "excluded_continuations": len(exclude_cell_ids),
        "remaining_continuations": len(selected_cells),
        "executed_continuations": attempted_continuations,
        "attempted_continuations": attempted_continuations,
        "completed_continuations": completed_continuations,
        "rows_written": len(results),
        "preparation_failures": preparation_failures,
        "executed_arm_counts": {arm: sum(row.get("arm") == arm for row in results) for arm in ARMS},
        "failed_continuations": attempted_continuations - completed_continuations,
        "native_compactions": native_compactions,
        "preparation_operations": len(logical_cells) * 2,
        "compaction_operations": native_compactions,
        "retrieval_probe_operations": probe_count,
        "provider_calls": None,
        "costs": "unknown",
        "model": model,
        "reasoning_effort": reasoning_effort,
        "native_schema_sha256": native_interface.schema_sha256,
        "handoff_contract_comparison": comparison,
        "build_instructions": {
            arm: {
                key: value
                for key, value in instructions.items()
                if key != "create_mode"
            }
            for arm, instructions in build_instructions.items()
        },
        "blinded_packets": len(results),
        "semantic_scoring": "pending_manual_calibration",
        "fatal_failure": fatal_failure,
        "identities": identities,
        "results": results,
    }
    _write_json(output_path / "results.json", summary)
    return summary


def _cli_selection(args: argparse.Namespace) -> tuple[tuple[str, ...], tuple[str, ...], int]:
    """Resolve one explicit matrix selection for both dry-run and execute."""

    has_selector = bool(args.cases or args.bands or args.replicate is not None)
    if args.all and has_selector:
        raise BenchmarkGateError("--all cannot be combined with case, band, or replicate")
    if args.all or not has_selector:
        return CASES, BANDS, REPLICATES
    if not args.cases or not args.bands or args.replicate is None:
        raise BenchmarkGateError(
            "selection needs --case, --band, and --replicate together"
        )
    return tuple(args.cases), tuple(args.bands), args.replicate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the provider-free plan")
    parser.add_argument("--verify-native", action="store_true", help="verify the installed app-server schema")
    parser.add_argument(
        "--verify-native-lifecycle",
        action="store_true",
        help="run initialize/thread-start/inject only; never compact or call a provider",
    )
    parser.add_argument("--codex-binary", "--native-binary", dest="native_binary", default="codex", help="Codex binary")
    parser.add_argument("--execute", action="store_true", help="run the explicitly authorized selected cells")
    parser.add_argument(
        "--acknowledge-provider-cost",
        action="store_true",
        help="acknowledge that an authorized native turn may incur provider cost",
    )
    parser.add_argument(
        "--allow-runtime",
        action="store_true",
        help="second explicit gate required for execution; absent by default",
    )
    parser.add_argument("--main-root", help="main product root containing server/handoff_mcp.py")
    parser.add_argument("--candidate-root", help="candidate product root containing server/handoff_mcp.py")
    parser.add_argument("--main-storage", choices=("legacy", "central"), default="legacy")
    parser.add_argument("--candidate-storage", choices=("legacy", "central"), default="central")
    parser.add_argument("--output", help="empty directory for run artifacts")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--sandbox-executable",
        default="bwrap",
        help="bubblewrap executable for measured app-server arms (use empty only for protocol tests)",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="allow network in the explicitly authorized runtime; default is network-isolated",
    )
    parser.add_argument(
        "--credential-source",
        help="optional read-only auth file mounted into the sandbox for a later provider run",
    )
    parser.add_argument("--case", dest="cases", action="append", choices=CASES)
    parser.add_argument("--band", dest="bands", action="append", choices=BANDS)
    parser.add_argument("--replicate", type=int, default=None)
    parser.add_argument(
        "--exclude-cell",
        dest="exclude_cells",
        action="append",
        default=[],
        help="exclude an already-attempted continuation cell ID from this run",
    )
    parser.add_argument("--all", action="store_true", help="select the full 108-continuation matrix")
    parser.add_argument("--verified-context-budget-tokens", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        selected_cases, selected_bands, selected_replicates = _cli_selection(args)
        plan = plan_benchmark(
            cases=selected_cases,
            bands=selected_bands,
            replicates=selected_replicates,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
        )
        selected_cells = _select_plan_cells(plan, args.exclude_cells)
        plan.update(
            excluded_cell_ids=list(args.exclude_cells),
            excluded_continuations=len(args.exclude_cells),
            remaining_continuations=len(selected_cells),
        )
        if args.verify_native:
            binary = shutil.which(args.native_binary) or args.native_binary
            interface = discover_native_interface(binary)
            print(
                json.dumps(
                    {
                        "gate": GATE_NATIVE_SCHEMA_VERIFIED,
                        "version": interface.version,
                        "schema_sha256": interface.schema_sha256,
                        "provider_calls": 0,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 0
        if args.verify_native_lifecycle:
            binary = shutil.which(args.native_binary) or args.native_binary
            interface = discover_native_interface(binary)
            conversation = build_structured_conversation("superseded-decision", "short", 1)
            with NativeAppServer(
                binary=binary,
                interface=interface,
                deadline=args.timeout,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                sandbox_executable=args.sandbox_executable or None,
                hidden_paths=(Path.cwd(),),
                credential_source=args.credential_source,
                allow_network=False,
            ) as server:
                thread_id = server.start_thread()
                server.inject_items(thread_id, conversation)
                fork_thread_id = server.fork_thread(thread_id)
                fork_readable = server.read_thread(fork_thread_id).get("id") == fork_thread_id
                fork_items_listed = isinstance(server.list_items(fork_thread_id), list)
            print(
                json.dumps(
                    {
                        "gate": "native-lifecycle-verified",
                        "schema_sha256": interface.schema_sha256,
                        "thread_started": True,
                        "history_injected": True,
                        "forked": True,
                        "fork_readable": fork_readable,
                        "fork_items_listed": fork_items_listed,
                        "compaction": "not-run",
                        "provider_calls": 0,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 0
        if args.execute:
            authorized_plan = NativeAppServer.require_execution(
                plan,
                execute=True,
                acknowledge_provider_cost=args.acknowledge_provider_cost,
                allow_runtime=args.allow_runtime,
            )
            if not args.main_root or not args.candidate_root or not args.output:
                raise BenchmarkGateError(
                    "execution requires --main-root, --candidate-root, and --output"
                )
            binary = shutil.which(args.native_binary) or args.native_binary
            interface = discover_native_interface(binary)
            result = execute_benchmark(
                output=args.output,
                native_binary=binary,
                native_interface=interface,
                main_root=args.main_root,
                candidate_root=args.candidate_root,
                cases=selected_cases,
                bands=selected_bands,
                replicates=selected_replicates,
                main_storage=args.main_storage,
                candidate_storage=args.candidate_storage,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                deadline=args.timeout,
                verified_context_budget_tokens=args.verified_context_budget_tokens,
                runtime_authorized=authorized_plan.get("execution") == "authorized",
                sandbox_executable=args.sandbox_executable or None,
                hidden_paths=(
                    Path.cwd(),
                    Path(args.output).expanduser().resolve(),
                    Path(args.main_root).expanduser().resolve(),
                    Path(args.candidate_root).expanduser().resolve(),
                ),
                credential_source=args.credential_source,
                allow_network=args.allow_network,
                exclude_cell_ids=args.exclude_cells,
            )
        else:
            result = NativeAppServer.dry_run(plan)
    except BenchmarkGateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
