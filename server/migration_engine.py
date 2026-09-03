"""Native Claude Code and Codex session conversion for session-handoff."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .version import PACKAGE_VERSION
except ImportError:
    from version import PACKAGE_VERSION

MAX_RECORDS = 100_000
MAX_LINE_BYTES = 16 * 1024 * 1024
UTC = timezone.utc


class EngineError(RuntimeError):
    """A native session could not be converted safely."""


def convert_native_session(
    source_path: Path,
    source_client: str,
    target_client: str,
    source_session_id: str,
    target_session_id: str,
    workspace: Path,
    target_home: Path,
) -> dict[str, Any]:
    source_bytes = source_path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    records = _read_jsonl(source_bytes)
    metadata, events, dropped = (
        _parse_claude(records, source_session_id)
        if source_client == "claude"
        else _parse_codex(records, source_session_id)
    )
    if not events:
        raise EngineError("source session has no portable conversation history")

    timestamp = _timestamp(metadata.get("timestamp"))
    if target_client == "claude":
        target_records, converted_dropped = _write_claude(
            events, target_session_id, workspace, timestamp
        )
        project = re.sub(r"[^A-Za-z0-9]", "-", str(workspace.resolve())) or "-"
        output = target_home / "projects" / project / f"{target_session_id}.jsonl"
    else:
        target_records, converted_dropped = _write_codex(
            events, target_session_id, workspace, timestamp
        )
        date = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        filename_time = date.strftime("%Y-%m-%dT%H-%M-%S")
        output = (
            target_home
            / "sessions"
            / date.strftime("%Y/%m/%d")
            / f"rollout-{filename_time}-{target_session_id}.jsonl"
        )
    dropped.update(converted_dropped)
    target_bytes = _encode_jsonl(target_records)
    target_sha256 = hashlib.sha256(target_bytes).hexdigest()
    manifest_path = target_home / "session-handoff/manifests" / f"{target_session_id}.json"
    manifest = {
        "schema_version": 2,
        "migration_version": "0.5.4",
        "source": {
            "format": source_client,
            "path": str(source_path.resolve()),
            "sha256": source_sha256,
            "session_id": source_session_id,
            "cli_version": metadata.get("cli_version"),
            "records": len(records),
        },
        "target": {
            "format": target_client,
            "path": str(output.resolve()),
            "sha256": target_sha256,
            "session_id": target_session_id,
            "cwd": str(workspace),
            "timestamp": timestamp,
            "records": len(target_records),
        },
        "dropped_events": dict(sorted(dropped.items())),
        "warnings": _warnings(dropped),
    }
    _write_pair(
        output,
        target_bytes,
        manifest_path,
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
    )
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_sha256:
        _unlink_created(output)
        _unlink_created(manifest_path)
        raise EngineError("source session changed during conversion")
    return {
        "source_format": source_client,
        "target_format": target_client,
        "session_id": target_session_id,
        "output": str(output.resolve()),
        "manifest": str(manifest_path.resolve()),
        "records": len(target_records),
        "sha256": target_sha256,
        "warnings": manifest["warnings"],
        "dropped_events": manifest["dropped_events"],
    }


def _read_jsonl(data: bytes) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for number, line in enumerate(data.splitlines(), 1):
        if not line.strip():
            continue
        if len(line) > MAX_LINE_BYTES:
            raise EngineError(f"JSONL record {number} exceeds the size limit")
        if len(records) >= MAX_RECORDS:
            raise EngineError("session exceeds the record limit")
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EngineError(f"invalid JSONL record {number}") from exc
        if not isinstance(record, dict):
            raise EngineError(f"JSONL record {number} is not an object")
        records.append(record)
    if not records:
        raise EngineError("session is empty")
    return records


def _parse_claude(
    records: list[dict[str, Any]], session_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]], Counter[str]]:
    selected = _claude_active_records(records)
    events: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()
    metadata: dict[str, Any] = {}
    for record in selected:
        if record.get("sessionId") != session_id:
            raise EngineError("Claude session metadata does not match the requested UUID")
        metadata.setdefault("timestamp", record.get("timestamp"))
        metadata.setdefault("cli_version", record.get("version"))
        record_type = record.get("type")
        message = record.get("message")
        if record_type not in {"user", "assistant"} or not isinstance(message, dict):
            dropped[str(record_type or "record")] += 1
            continue
        role = message.get("role") or record_type
        if role not in {"user", "assistant"}:
            dropped["message:role"] += 1
            continue
        _claude_content(events, dropped, role, message.get("content"), record.get("timestamp"))
    return metadata, events, dropped


def _claude_active_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [
        record
        for record in records
        if record.get("type") in {"user", "assistant"}
        and isinstance(record.get("message"), dict)
        and record.get("isMeta") is not True
        and record.get("isSidechain") is not True
    ]
    by_uuid: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = record.get("uuid")
        if not isinstance(record_id, str) or not record_id:
            continue
        if record_id in by_uuid:
            raise EngineError("Claude transcript contains a duplicate record UUID")
        by_uuid[record_id] = record
    leaf = next(
        (
            record.get("leafUuid")
            for record in reversed(records)
            if record.get("type") == "last-prompt"
            and isinstance(record.get("leafUuid"), str)
        ),
        candidates[-1].get("uuid") if candidates else None,
    )
    if not isinstance(leaf, str) or leaf not in by_uuid:
        return candidates
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    while leaf:
        if leaf in seen:
            raise EngineError("Claude active graph contains an ancestry cycle")
        seen.add(leaf)
        record = by_uuid.get(leaf)
        if record is None:
            raise EngineError("Claude active graph references a missing parent UUID")
        chain.append(record)
        parent = record.get("parentUuid")
        leaf = parent if isinstance(parent, str) and parent else None
    return list(reversed(chain))


def _claude_content(
    events: list[dict[str, Any]],
    dropped: Counter[str],
    role: str,
    content: Any,
    timestamp: Any,
) -> None:
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    if not isinstance(blocks, list):
        dropped["message:content"] += 1
        return
    for block in blocks:
        if not isinstance(block, dict):
            dropped["message:block"] += 1
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            events.append({"kind": "text", "role": role, "text": block["text"], "timestamp": timestamp})
        elif kind == "tool_use" and role == "assistant":
            events.append({
                "kind": "tool_call",
                "id": block.get("id"),
                "name": block.get("name"),
                "input": block.get("input", {}),
                "timestamp": timestamp,
            })
        elif kind == "tool_result" and role == "user":
            events.append({
                "kind": "tool_result",
                "id": block.get("tool_use_id"),
                "output": block.get("content", ""),
                "is_error": block.get("is_error") is True,
                "timestamp": timestamp,
            })
        elif kind == "image":
            image_url = _claude_image_url(block.get("source"))
            if image_url and role == "user":
                events.append({"kind": "image", "role": role, "url": image_url, "timestamp": timestamp})
            else:
                dropped["message:image"] += 1
        elif kind in {"thinking", "redacted_thinking"}:
            dropped["reasoning"] += 1
        else:
            dropped[f"message:{kind or 'block'}"] += 1


def _parse_codex(
    records: list[dict[str, Any]], session_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]], Counter[str]]:
    first = records[0]
    metadata = first.get("payload")
    if first.get("type") != "session_meta" or not isinstance(metadata, dict):
        raise EngineError("Codex session does not begin with metadata")
    if session_id not in {metadata.get("id"), metadata.get("session_id")}:
        raise EngineError("Codex session metadata does not match the requested UUID")
    if metadata.get("history_mode") not in {None, "", "legacy"}:
        raise EngineError("Codex source is not a legacy rollout")
    events: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()
    response_messages = 0
    for record in records[1:]:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            dropped["record:payload"] += 1
            continue
        timestamp = record.get("timestamp")
        if record.get("type") == "response_item":
            response_messages += _codex_item(events, dropped, payload, timestamp)
        elif record.get("type") == "event_msg" and payload.get("type") in {"user_message", "agent_message"}:
            text = payload.get("message")
            if isinstance(text, str):
                fallback.append({
                    "kind": "text",
                    "role": "user" if payload["type"] == "user_message" else "assistant",
                    "text": text,
                    "timestamp": timestamp,
                })
        elif record.get("type") == "compacted":
            text = payload.get("message")
            if isinstance(text, str) and text:
                events.append({"kind": "text", "role": "assistant", "text": text, "timestamp": timestamp})
            else:
                dropped["compaction"] += 1
        else:
            dropped[str(record.get("type") or "record")] += 1
    if not response_messages:
        events.extend(fallback)
    return metadata, events, dropped


def _codex_item(
    events: list[dict[str, Any]], dropped: Counter[str], payload: dict[str, Any], timestamp: Any
) -> int:
    item_type = payload.get("type")
    if item_type == "message":
        role = payload.get("role")
        content = payload.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, list):
            dropped["message"] += 1
            return 0
        found = 0
        for block in content:
            if not isinstance(block, dict):
                dropped["message:block"] += 1
                continue
            block_type = block.get("type")
            if block_type in {"input_text", "output_text", "text"} and isinstance(block.get("text"), str):
                events.append({"kind": "text", "role": role, "text": block["text"], "timestamp": timestamp})
                found = 1
            elif block_type in {"input_image", "image"} and role == "user":
                url = block.get("image_url") or block.get("url")
                if isinstance(url, str):
                    events.append({"kind": "image", "role": role, "url": url, "timestamp": timestamp})
                    found = 1
                else:
                    dropped["message:image"] += 1
            else:
                dropped[f"message:{block_type or 'block'}"] += 1
        return found
    if item_type in {"function_call", "custom_tool_call"}:
        arguments = payload.get("arguments", payload.get("input", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                pass
        events.append({
            "kind": "tool_call",
            "id": payload.get("call_id") or payload.get("id"),
            "name": payload.get("name"),
            "input": arguments,
            "timestamp": timestamp,
        })
    elif item_type in {"function_call_output", "custom_tool_call_output"}:
        events.append({
            "kind": "tool_result",
            "id": payload.get("call_id"),
            "output": payload.get("output", ""),
            "timestamp": timestamp,
        })
    elif item_type == "reasoning":
        dropped["reasoning"] += 1
    else:
        dropped[f"response_item:{item_type or 'missing'}"] += 1
    return 0


def _write_claude(
    events: list[dict[str, Any]], session_id: str, workspace: Path, timestamp: str
) -> tuple[list[dict[str, Any]], Counter[str]]:
    records: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()
    parent: str | None = None
    pending_role: str | None = None
    pending_blocks: list[dict[str, Any]] = []
    pending_timestamp = timestamp

    def flush() -> None:
        nonlocal parent, pending_role, pending_blocks, pending_timestamp
        if not pending_role or not pending_blocks:
            return
        record_id = str(uuid.uuid4())
        content: Any = pending_blocks
        if pending_role == "user" and len(content) == 1 and content[0].get("type") == "text":
            content = content[0]["text"]
        message: dict[str, Any] = {"role": pending_role, "content": content}
        if pending_role == "assistant":
            message.update({"id": f"msg_{uuid.uuid4().hex}", "type": "message", "model": "unknown"})
        records.append({
            "parentUuid": parent,
            "isSidechain": False,
            "userType": "external",
            "cwd": str(workspace),
            "sessionId": session_id,
            "version": "2.1.233",
            "gitBranch": "",
            "uuid": record_id,
            "timestamp": _timestamp(pending_timestamp),
            "type": pending_role,
            "message": message,
        })
        parent = record_id
        pending_role = None
        pending_blocks = []
        pending_timestamp = timestamp

    for event in events:
        kind = event["kind"]
        role = event.get("role")
        if kind == "text":
            block = {"type": "text", "text": event["text"]}
        elif kind == "image":
            block = {"type": "image", "source": {"type": "url", "url": event["url"]}}
        elif kind == "tool_call":
            role = "assistant"
            call_id = event.get("id")
            if not isinstance(call_id, str) or not call_id:
                call_id = f"toolu_{uuid.uuid4().hex}"
                dropped["tool_call:missing_id"] += 1
            name = event.get("name")
            if not isinstance(name, str) or not name:
                name = "unknown_tool"
                dropped["tool_call:missing_name"] += 1
            block = {"type": "tool_use", "id": call_id, "name": name, "input": event.get("input", {})}
        elif kind == "tool_result":
            role = "user"
            call_id = event.get("id")
            if not isinstance(call_id, str) or not call_id:
                call_id = f"toolu_missing_{uuid.uuid4().hex}"
                dropped["tool_result:missing_id"] += 1
            block = {"type": "tool_result", "tool_use_id": call_id, "content": event.get("output", "")}
            if event.get("is_error") is True:
                block["is_error"] = True
        else:
            dropped[kind] += 1
            continue
        if pending_role != role:
            flush()
            pending_role = role
        pending_timestamp = event.get("timestamp") or pending_timestamp
        pending_blocks.append(block)
    flush()
    return records, dropped


def _write_codex(
    events: list[dict[str, Any]], session_id: str, workspace: Path, timestamp: str
) -> tuple[list[dict[str, Any]], Counter[str]]:
    records = [{
        "timestamp": timestamp,
        "type": "session_meta",
        "payload": {
            "session_id": session_id,
            "id": session_id,
            "timestamp": timestamp,
            "cwd": str(workspace),
            "originator": "session-handoff",
            "cli_version": PACKAGE_VERSION,
            "source": "cli",
            "model_provider": "openai",
            "history_mode": "legacy",
        },
    }]
    dropped: Counter[str] = Counter()
    for event in events:
        timestamp_value = _timestamp(event.get("timestamp") or timestamp)
        kind = event["kind"]
        if kind == "text":
            role = event["role"]
            payload = {
                "type": "message",
                "role": role,
                "content": [{
                    "type": "input_text" if role == "user" else "output_text",
                    "text": event["text"],
                }],
            }
        elif kind == "image":
            payload = {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": event["url"]}]}
        elif kind == "tool_call":
            call_id = event.get("id")
            if not isinstance(call_id, str) or not call_id:
                call_id = f"call_{uuid.uuid4().hex}"
                dropped["tool_call:missing_id"] += 1
            name = event.get("name")
            if not isinstance(name, str) or not name:
                name = "unknown_tool"
                dropped["tool_call:missing_name"] += 1
            arguments = event.get("input", {})
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            payload = {"type": "function_call", "name": name, "arguments": arguments, "call_id": call_id}
        elif kind == "tool_result":
            call_id = event.get("id")
            if not isinstance(call_id, str) or not call_id:
                call_id = f"call_missing_{uuid.uuid4().hex}"
                dropped["tool_result:missing_id"] += 1
            output = event.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
            payload = {"type": "function_call_output", "call_id": call_id, "output": output}
            if event.get("is_error") is True:
                dropped["tool_result:is_error"] += 1
        else:
            dropped[kind] += 1
            continue
        records.append({"timestamp": timestamp_value, "type": "response_item", "payload": payload})
    return records, dropped


def _claude_image_url(source: Any) -> str | None:
    if not isinstance(source, dict):
        return None
    if source.get("type") == "url" and isinstance(source.get("url"), str):
        return source["url"]
    if source.get("type") == "base64" and all(isinstance(source.get(key), str) for key in ("media_type", "data")):
        return f"data:{source['media_type']};base64,{source['data']}"
    return None


def _timestamp(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _encode_jsonl(records: list[dict[str, Any]]) -> bytes:
    return ("\n".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in records) + "\n").encode()


def _warnings(dropped: Counter[str]) -> list[dict[str, Any]]:
    return [
        {
            "code": "dropped_event_kind",
            "event_kind": kind,
            "count": count,
            "message": "target conversion omitted or normalized this source detail",
        }
        for kind, count in sorted(dropped.items())
        if count
    ]


def _write_pair(output: Path, data: bytes, manifest: Path, manifest_data: bytes) -> None:
    collisions = [path for path in (output, manifest) if os.path.lexists(path)]
    if collisions:
        raise EngineError("refusing to overwrite existing target(s): " + ", ".join(map(str, collisions)))
    written: list[Path] = []
    try:
        for path, content in ((output, data), (manifest, manifest_data)):
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            written.append(path)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
    except BaseException:
        for path in written:
            _unlink_created(path)
        raise


def _unlink_created(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
