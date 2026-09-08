#!/usr/bin/env python3
"""Offline stdio-MCP efficiency benchmark for frozen session-handoff builds.

The runner never invokes a provider.  Its default mode only prints a plan;
``--execute`` is required before it creates fixtures or starts product
processes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import selectors
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark.version_aware import _minimal_environment, load_build


MAX_RECORDS = 256
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_SEARCH_BYTES = 4 * 1024 * 1024
MAX_SEARCH_SNIPPET_BYTES = 512
MAX_SEARCH_MATCHES_PER_FILE = 8
PAGE_LIMIT = 20
MAX_LIST_LIMIT = 100
DEFAULT_SIZES = (10, 1000, 10000)
DEFAULT_SAMPLES = 10
DEFAULT_DOCUMENT_BYTES = 8 * 1024
DEFAULT_DEADLINE = 300.0
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_DISK_BUDGET = 4 * 1024**3
QUERY_KINDS = ("absent", "common", "only_beyond_256")
DEFAULT_SCENARIOS = (
    "shared_legacy",
    "storage_comparison",
    "candidate_search",
    "candidate_central_list",
)


class EfficiencyError(RuntimeError):
    """A benchmark input, fixture, or execution error."""


class MCPProtocolError(EfficiencyError):
    """The product emitted an invalid JSON-RPC/MCP response."""


class UnsupportedCapability(EfficiencyError):
    """The selected frozen build does not advertise a requested tool."""


def parse_sizes(raw: str) -> tuple[int, ...]:
    if not isinstance(raw, str) or not raw.strip():
        raise EfficiencyError("sizes must be a comma-separated list of positive integers")
    values: list[int] = []
    for part in raw.split(","):
        try:
            value = int(part.strip())
        except ValueError as exc:
            raise EfficiencyError(f"invalid size: {part}") from exc
        if value < 1:
            raise EfficiencyError("sizes must be positive")
        if value in values:
            raise EfficiencyError("sizes must not contain duplicates")
        values.append(value)
    return tuple(values)


def parse_scenarios(raw: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not values:
        raise EfficiencyError("at least one scenario is required")
    unknown = sorted(set(values) - set(SCENARIO_NAMES))
    if unknown:
        raise EfficiencyError("unknown scenario: " + ", ".join(unknown))
    if len(values) != len(set(values)):
        raise EfficiencyError("scenarios must not contain duplicates")
    return values


def make_markdown(index: int, document_bytes: int = DEFAULT_DOCUMENT_BYTES) -> str:
    """Return deterministic canonical Markdown of exactly ``document_bytes``."""

    if index < 0 or not 512 <= document_bytes <= MAX_DOCUMENT_BYTES:
        raise EfficiencyError(
            f"fixture index must be non-negative and document must be between 512 and {MAX_DOCUMENT_BYTES} bytes"
        )
    prefix = (
        "## Goal\n"
        "Measure the offline synthetic handoff path.\n"
        "## Constraints & Preferences\n"
        "- Synthetic data only; preserve all product limits.\n"
        "## Progress\n"
        "### Done\n"
        f"- Prepared record {index:05d}.\n"
        "### In Progress\n"
        "- Measuring one isolated request sequence.\n"
        "### Pending\n"
        "- Inspect raw metrics.\n"
        "## Key Decisions\n"
        "- Use the public stdio MCP contract.\n"
        "## Critical Context\n"
        f"- synthetic-record-{index:05d}\n"
        "## Next Steps\n"
        "1. Compare absolute observations.\n"
        f"synthetic-record-{index:05d} common-token\n"
    )
    if index == 256:
        prefix += "only-beyond-256\n"
    filler = (f"fixture-padding-{index:05d} " * 32).encode("ascii") + b"\n"
    encoded = prefix.encode("utf-8")
    if len(encoded) > document_bytes:
        raise EfficiencyError("document size is too small for the canonical fixture")
    repeats = (document_bytes - len(encoded) + len(filler) - 1) // len(filler)
    return (encoded + filler * repeats)[:document_bytes].decode("utf-8")


def _sha256(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def aggregate_samples(samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for sample in samples:
        key = str(sample.get("cell_key", "unknown"))
        row = grouped.setdefault(key, {"attempted": 0, "samples": 0, "failures": 0, "unsupported": 0, "metrics": {}})
        row["attempted"] += 1
        status = sample.get("status")
        if status == "ok":
            row["samples"] += 1
            for metric, value in (sample.get("metrics") or {}).items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                row["metrics"].setdefault(metric, []).append(value)
        elif status == "unsupported":
            row["unsupported"] += 1
        else:
            row["failures"] += 1
    for row in grouped.values():
        for metric, values in list(row["metrics"].items()):
            row["metrics"][metric] = {
                "median": statistics.median(values),
                "range": {"min": min(values), "max": max(values)},
                "p95": round(_percentile([float(value) for value in values], 0.95), 6),
                "samples": len(values),
            }
    return grouped
def classify_search_coverage(
    result: dict[str, Any], *, query_kind: str, target_index: int | None, page_start: int = 0
) -> dict[str, Any]:
    scanned_files = result.get("scanned_files")
    scanned_bytes = result.get("scanned_bytes")
    scan_truncated = result.get("scan_truncated")
    has_more = result.get("has_more")
    skipped_count = result.get("skipped_count")
    items = result.get("items")
    if (
        isinstance(scanned_files, bool)
        or not isinstance(scanned_files, int)
        or scanned_files < 0
        or isinstance(scanned_bytes, bool)
        or not isinstance(scanned_bytes, int)
        or scanned_bytes < 0
        or not isinstance(scan_truncated, bool)
        or not isinstance(has_more, bool)
        or isinstance(skipped_count, bool)
        or not isinstance(skipped_count, int)
        or skipped_count < 0
        or not isinstance(items, list)
        or isinstance(page_start, bool)
        or not isinstance(page_start, int)
        or page_start < 0
    ):
        raise MCPProtocolError("search returned invalid coverage fields")
    target_covered = target_index is None or page_start <= target_index < page_start + scanned_files
    complete = not scan_truncated and not has_more and skipped_count == 0
    if not complete:
        status = "truncated"
    elif target_index is not None and not target_covered:
        status = "target_not_covered"
    else:
        status = "complete"
    if query_kind not in QUERY_KINDS:
        raise EfficiencyError(f"unknown query kind: {query_kind}")
    return {
        "status": status,
        "complete": complete,
        "target_covered": target_covered,
        "scanned_files": scanned_files,
        "scanned_bytes": scanned_bytes,
        "scan_truncated": scan_truncated,
        "has_more": has_more,
        "skipped_count": skipped_count,
        "returned_items": len(items),
    }


SCENARIO_NAMES = {
    "shared_legacy",
    "storage_comparison",
    "candidate_search",
    "candidate_search_walk",
    "candidate_central_list",
    "candidate_central_full_walk",
    "candidate_catalog_states",
    "startup",
}


def plan_cells(
    sizes: tuple[int, ...], build_labels: tuple[str, ...], scenarios: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Expand only the explicitly selected benchmark lanes into cells."""

    cells: list[dict[str, Any]] = []
    candidates = [label for label in build_labels if label == "candidate"] or list(build_labels[-1:])
    for scenario in scenarios:
        if scenario == "shared_legacy":
            for size in sizes:
                for operation in ("create", "read", "list_first_page"):
                    for label in build_labels:
                        cells.append({"scenario": scenario, "build": label, "size": size, "storage": "legacy", "operation": operation})
        elif scenario == "storage_comparison":
            for label in candidates:
                for size in sizes:
                    for storage in ("legacy", "central"):
                        for operation in ("create", "read"):
                            cells.append({"scenario": scenario, "build": label, "size": size, "storage": storage, "operation": operation})
        elif scenario == "candidate_search":
            search_builds = list(dict.fromkeys([*build_labels, *candidates]))
            for label in search_builds:
                for size in sizes:
                    storages = ("legacy", "central") if label in candidates else ("legacy",)
                    for storage in storages:
                        for query_kind in QUERY_KINDS:
                            cells.append({"scenario": scenario, "build": label, "size": size, "storage": storage, "operation": "search", "query_kind": query_kind})
        elif scenario == "candidate_search_walk":
            for label in candidates:
                for size in sizes:
                    cells.append({"scenario": scenario, "build": label, "size": size, "storage": "central", "operation": "search_full_walk", "query_kind": "only_beyond_256"})
        elif scenario == "candidate_central_list":
            for label in candidates:
                for size in sizes:
                    cells.append({"scenario": scenario, "build": label, "size": size, "storage": "central", "operation": "list_first_page", "page_mode": "first"})
        elif scenario == "candidate_central_full_walk":
            for label in candidates:
                for size in sizes:
                    cells.append({"scenario": scenario, "build": label, "size": size, "storage": "central", "operation": "list_full_walk", "page_mode": "full"})
        elif scenario == "candidate_catalog_states":
            for label in candidates:
                for size in sizes:
                    for catalog_state in ("healthy", "absent", "rebuild"):
                        cells.append({"scenario": scenario, "build": label, "size": size, "storage": "central", "operation": "list_first_page", "page_mode": "first", "catalog_state": catalog_state})
        elif scenario == "startup":
            for label in build_labels:
                cells.append({"scenario": scenario, "build": label, "size": None, "storage": "legacy", "operation": "startup"})
    return cells


def _ordered_cells(cells: list[dict[str, Any]], sample: int, build_labels: tuple[str, ...]) -> list[dict[str, Any]]:
    """Pair build arms and alternate AB/BA order on successive samples."""

    groups: dict[str, list[dict[str, Any]]] = {}
    for cell in cells:
        key = json.dumps({name: value for name, value in cell.items() if name != "build"}, sort_keys=True, separators=(",", ":"))
        groups.setdefault(key, []).append(cell)
    order = build_labels if sample % 2 else tuple(reversed(build_labels))
    result: list[dict[str, Any]] = []
    for group in groups.values():
        for label in order:
            result.extend(cell for cell in group if cell.get("build") == label)
    return result


@dataclass
class ProcSnapshot:
    cpu_user_ns: int | None = None
    cpu_system_ns: int | None = None
    rss_bytes: int | None = None
    peak_rss_bytes: int | None = None
    rchar_bytes: int | None = None
    wchar_bytes: int | None = None
    read_bytes: int | None = None
    write_bytes: int | None = None


def _proc_snapshot(pid: int) -> ProcSnapshot:
    if sys.platform != "linux":
        return ProcSnapshot()
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        tail = stat_text.rsplit(")", 1)[1].split()
        clock = os.sysconf("SC_CLK_TCK")
        cpu_user_ns = int(int(tail[11]) * 1_000_000_000 / clock)
        cpu_system_ns = int(int(tail[12]) * 1_000_000_000 / clock)
        status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
        memory: dict[str, int] = {}
        for line in status.splitlines():
            name, _, raw = line.partition(":")
            if name in {"VmRSS", "VmHWM"}:
                memory[name] = int(raw.strip().split()[0]) * 1024
        io_values: dict[str, int] = {}
        for line in Path(f"/proc/{pid}/io").read_text(encoding="ascii").splitlines():
            name, _, raw = line.partition(":")
            if name in {"rchar", "wchar", "read_bytes", "write_bytes"}:
                io_values[name] = int(raw.strip())
        return ProcSnapshot(
            cpu_user_ns=cpu_user_ns,
            cpu_system_ns=cpu_system_ns,
            rss_bytes=memory.get("VmRSS"),
            peak_rss_bytes=memory.get("VmHWM"),
            rchar_bytes=io_values.get("rchar"),
            wchar_bytes=io_values.get("wchar"),
            read_bytes=io_values.get("read_bytes"),
            write_bytes=io_values.get("write_bytes"),
        )
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return ProcSnapshot()


def _proc_group_member(pgid: int, session_id: int) -> bool:
    if sys.platform != "linux":
        return False
    try:
        entries = Path("/proc").iterdir()
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            tail = (entry / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
            if tail[0] == "Z":
                continue
            if int(tail[2]) == pgid and int(tail[3]) == session_id:
                return True
        except (FileNotFoundError, OSError, ValueError, IndexError):
            continue
    return False


def _owned_process_group_alive(process: subprocess.Popen[Any], pgid: int | None, session_id: int | None) -> bool:
    if pgid is None:
        return False
    try:
        process.poll()
    except (ChildProcessError, OSError):
        pass
    try:
        if os.getpgid(process.pid) == pgid and (session_id is None or os.getsid(process.pid) == session_id):
            return True
    except (ProcessLookupError, PermissionError):
        pass
    if sys.platform == "linux" and session_id is not None:
        return _proc_group_member(pgid, session_id)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_owned_process_group(
    process: subprocess.Popen[Any],
    pgid: int | None,
    session_id: int | None,
    *,
    term_timeout: float = 0.5,
    kill_timeout: float = 1.0,
) -> None:
    if not _owned_process_group_alive(process, pgid, session_id):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + term_timeout
    while _owned_process_group_alive(process, pgid, session_id) and time.monotonic() < deadline:
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
    if not _owned_process_group_alive(process, pgid, session_id):
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + kill_timeout
    while _owned_process_group_alive(process, pgid, session_id) and time.monotonic() < deadline:
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def _delta(before: int | None, after: int | None) -> int | None:
    if before is None or after is None:
        return None
    return max(0, after - before)


def _signed_delta(before: int | None, after: int | None) -> int | None:
    if before is None or after is None:
        return None
    return after - before


def _request_metrics(before: ProcSnapshot, after: ProcSnapshot, latency_ns: int) -> dict[str, Any]:
    return {
        "request_latency_ns": latency_ns,
        "cpu_user_ns": _delta(before.cpu_user_ns, after.cpu_user_ns),
        "cpu_system_ns": _delta(before.cpu_system_ns, after.cpu_system_ns),
        "request_rss_delta_bytes": _signed_delta(before.rss_bytes, after.rss_bytes),
        "response_utf8_bytes": None,
        "peak_process_rss_bytes": after.peak_rss_bytes,
        "proc_io_rchar_bytes": _delta(before.rchar_bytes, after.rchar_bytes),
        "proc_io_wchar_bytes": _delta(before.wchar_bytes, after.wchar_bytes),
        "proc_io_read_bytes": _delta(before.read_bytes, after.read_bytes),
        "proc_io_write_bytes": _delta(before.write_bytes, after.write_bytes),
    }


def _sum_metrics(metrics: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(metrics)
    result: dict[str, Any] = {}
    for key in (
        "request_latency_ns",
        "cpu_user_ns",
        "cpu_system_ns",
        "response_utf8_bytes",
        "proc_io_rchar_bytes",
        "proc_io_wchar_bytes",
        "proc_io_read_bytes",
        "proc_io_write_bytes",
    ):
        values = [row[key] for row in rows if isinstance(row.get(key), (int, float)) and not isinstance(row[key], bool)]
        result[key] = sum(values) if values else None
    peaks = [row["peak_process_rss_bytes"] for row in rows if isinstance(row.get("peak_process_rss_bytes"), (int, float))]
    result["peak_process_rss_bytes"] = max(peaks) if peaks else None
    result["request_count"] = len(rows)
    rss_values = [row.get("request_rss_delta_bytes") for row in rows]
    result["request_rss_delta_bytes"] = rss_values[0] if len(rows) == 1 else None
    result["request_rss_delta_bytes_per_request"] = rss_values
    return result


class PersistentMCP:
    """One actual product stdio process serving multiple JSON-RPC requests."""

    def __init__(
        self,
        *,
        root: Path,
        env: dict[str, str],
        server: Path | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        deadline: float | None = None,
        deadline_at: float | None = None,
    ) -> None:
        self.root = root.resolve()
        self.env = dict(env)
        self.server = server or self.root / "server/handoff_mcp.py"
        self.timeout = timeout
        self.deadline = deadline_at if deadline_at is not None else (time.monotonic() + deadline if deadline is not None else None)
        self.process: subprocess.Popen[bytes] | None = None
        self._process_group_id: int | None = None
        self._process_session_id: int | None = None
        self._selector: selectors.BaseSelector | None = None
        self._stdout_buffer = bytearray()
        self._stderr_file: Any = None
        self._request_id = 0
        self.capabilities: set[str] = set()
        self.tool_arguments: dict[str, set[str]] = {}
        self.startup_metrics: dict[str, Any] = {}

    def _remaining(self, deadline_at: float | None = None) -> float:
        limits = [limit for limit in (self.deadline, deadline_at) if limit is not None]
        if not limits:
            return self.timeout
        remaining = min(limits) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("benchmark deadline exceeded")
        return min(self.timeout, remaining)

    def _send(self, payload: dict[str, Any], deadline_at: float | None = None) -> None:
        if self.process is None or self.process.stdin is None:
            raise MCPProtocolError("MCP process is not running")
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor = self.process.stdin.fileno()
        offset = 0
        try:
            os.set_blocking(descriptor, False)
            while offset < len(encoded):
                try:
                    offset += os.write(descriptor, encoded[offset:])
                    continue
                except BlockingIOError:
                    ready = selectors.DefaultSelector()
                    try:
                        ready.register(descriptor, selectors.EVENT_WRITE)
                        if not ready.select(self._remaining(deadline_at)):
                            raise TimeoutError("MCP request write timed out")
                    finally:
                        ready.close()
        except TimeoutError:
            raise
        except (BrokenPipeError, OSError) as exc:
            raise MCPProtocolError("MCP process closed stdin") from exc

    def _receive(self, request_id: int, deadline_at: float | None = None) -> dict[str, Any]:
        if self.process is None or self.process.stdout is None or self._selector is None:
            raise MCPProtocolError("MCP process is not running")
        descriptor = self.process.stdout.fileno()
        while b"\n" not in self._stdout_buffer:
            try:
                ready = self._selector.select(self._remaining(deadline_at))
            except TimeoutError:
                raise
            except OSError as exc:
                raise MCPProtocolError("MCP stdout is unavailable") from exc
            if not ready:
                raise TimeoutError("MCP request timed out")
            try:
                chunk = os.read(descriptor, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                raise MCPProtocolError("MCP process closed stdout")
            self._stdout_buffer.extend(chunk)
            if len(self._stdout_buffer) > 8 * 1024 * 1024:
                raise MCPProtocolError("MCP response exceeded 8 MiB")
        line, _, remainder = self._stdout_buffer.partition(b"\n")
        self._stdout_buffer = bytearray(remainder)
        try:
            response = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise MCPProtocolError("MCP response was not JSON") from exc
        if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
            raise MCPProtocolError("MCP response was not JSON-RPC 2.0")
        if response.get("id") != request_id:
            raise MCPProtocolError("MCP response id did not match request")
        if "error" in response:
            error = response["error"]
            message = error.get("message", "MCP request failed") if isinstance(error, dict) else "MCP request failed"
            raise MCPProtocolError(str(message)[:512])
        if not isinstance(response.get("result"), dict):
            raise MCPProtocolError("MCP response result was not an object")
        return response

    def request(self, method: str, params: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.process is None:
            raise MCPProtocolError("MCP process is not running")
        self._request_id += 1
        request_id = self._request_id
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        before = _proc_snapshot(self.process.pid)
        started = time.perf_counter_ns()
        request_deadline = time.monotonic() + self.timeout
        if self.deadline is not None:
            request_deadline = min(request_deadline, self.deadline)
        self._send(request, request_deadline)
        response = self._receive(request_id, request_deadline)
        latency = time.perf_counter_ns() - started
        after = _proc_snapshot(self.process.pid)
        metrics = _request_metrics(before, after, latency)
        metrics["response_utf8_bytes"] = len(
            json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        return response, metrics

    def start(self) -> None:
        if self.process is not None:
            raise MCPProtocolError("MCP process already started")
        if not self.server.is_file():
            raise UnsupportedCapability(f"missing MCP server: {self.server}")
        try:
            startup_started = time.perf_counter_ns()
            self._stderr_file = tempfile.TemporaryFile(mode="w+b")
            self.process = subprocess.Popen(
                [sys.executable, str(self.server)],
                cwd=self.root,
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr_file,
                start_new_session=True,
                bufsize=0,
            )
            self._process_group_id = os.getpgid(self.process.pid)
            self._process_session_id = os.getsid(self.process.pid)
            self._selector = selectors.DefaultSelector()
            assert self.process.stdout is not None
            os.set_blocking(self.process.stdout.fileno(), False)
            self._selector.register(self.process.stdout.fileno(), selectors.EVENT_READ)
            response, _ = self.request(
                "initialize",
                {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "efficiency", "version": "1"}},
            )
            initialize_result = response["result"]
            if "protocolVersion" in initialize_result and not isinstance(initialize_result["protocolVersion"], str):
                raise MCPProtocolError("MCP initialize protocolVersion was invalid")
            self.startup_metrics["startup_initialize_ns"] = time.perf_counter_ns() - startup_started
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            ping_started = time.perf_counter_ns()
            _, ping_metrics = self.request("ping")
            self.startup_metrics["initialized_ping_ns"] = time.perf_counter_ns() - ping_started
            self.startup_metrics.update({
                "startup_ping_request_latency_ns": ping_metrics.get("request_latency_ns"),
                "startup_peak_process_rss_bytes": ping_metrics.get("peak_process_rss_bytes"),
            })
            try:
                response, _ = self.request("tools/list")
            except MCPProtocolError as exc:
                if "method not found" in str(exc):
                    self.capabilities = set()
                else:
                    raise
            else:
                tools = response["result"].get("tools")
                if not isinstance(tools, list):
                    raise MCPProtocolError("MCP tools/list returned invalid tools")
                names = {tool.get("name") for tool in tools if isinstance(tool, dict)}
                self.capabilities = {name for name in names if isinstance(name, str)}
                self.tool_arguments = {
                    tool["name"]: set((tool.get("inputSchema") or {}).get("properties", {}))
                    for tool in tools
                    if isinstance(tool, dict) and isinstance(tool.get("name"), str)
                }
            self.startup_metrics["startup_setup_elapsed_ns"] = time.perf_counter_ns() - startup_started
        except Exception:
            self.close()
            raise

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if name not in self.capabilities:
            raise UnsupportedCapability(f"MCP tool not advertised: {name}")
        response, metrics = self.request("tools/call", {"name": name, "arguments": arguments})
        result = response["result"]
        if result.get("isError") is True:
            content = result.get("content")
            message = content[0].get("text", "tool failed") if isinstance(content, list) and content and isinstance(content[0], dict) else "tool failed"
            raise MCPProtocolError(str(message)[:512])
        content = result.get("content")
        if not isinstance(content, list) or not content or not isinstance(content[0], dict) or not isinstance(content[0].get("text"), str):
            raise MCPProtocolError("MCP tool response omitted text content")
        try:
            data = json.loads(content[0]["text"])
        except json.JSONDecodeError as exc:
            raise MCPProtocolError("MCP tool response text was not JSON") from exc
        if not isinstance(data, dict):
            raise MCPProtocolError("MCP tool response was not an object")
        return data, metrics

    def close(self) -> None:
        process = self.process
        process_group_id = self._process_group_id
        process_session_id = self._process_session_id
        self.process = None
        self._process_group_id = None
        self._process_session_id = None
        if self._selector is not None:
            try:
                self._selector.close()
            except Exception:
                pass
            self._selector = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        _terminate_owned_process_group(process, process_group_id, process_session_id)
        if process.poll() is None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if self._stderr_file is not None:
            try:
                self._stderr_file.close()
            except OSError:
                pass
            self._stderr_file = None

    def __enter__(self) -> "PersistentMCP":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


@dataclass
class Fixture:
    workspace: Path
    home: Path
    data: Path
    state: Path
    config: Path
    storage: str
    documents: dict[str, str]
    identities: list[str]
    document_bytes: int


@dataclass
class CachedFixture:
    fixture: Fixture
    snapshot_root: Path
    dirty: bool = False


_FIXTURE_DIRS = ("workspace", "home", "data", "state", "config")


def _snapshot_fixture(fixture: Fixture, destination: Path) -> CachedFixture:
    destination.mkdir(mode=0o700, parents=True)
    for name in _FIXTURE_DIRS:
        shutil.copytree(getattr(fixture, name), destination / name)
    return CachedFixture(fixture, destination)


def _restore_fixture(cached: CachedFixture) -> Fixture:
    fixture = cached.fixture
    if not cached.dirty:
        return fixture
    if fixture.storage == "legacy":
        (fixture.workspace / "handoffs" / f"record-{len(fixture.identities):05d}.md").unlink()
        cached.dirty = False
        return fixture
    for name in _FIXTURE_DIRS:
        target = getattr(fixture, name)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(cached.snapshot_root / name, target)
    cached.dirty = False
    return fixture


def _isolated_environment(home: Path, data: Path, state: Path, config: Path) -> dict[str, str]:
    environment = _minimal_environment(home, do_not_track=True)
    environment.update(
        {
            "XDG_DATA_HOME": str(data),
            "XDG_STATE_HOME": str(state),
            "XDG_CONFIG_HOME": str(config),
            "DO_NOT_TRACK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _fixture_paths(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    workspace, home, data, state, config = (root / name for name in ("workspace", "home", "data", "state", "config"))
    for path in (workspace, home, data, state, config):
        _mkdir_private(path)
    return workspace, home, data, state, config


def _prepare_legacy(root: Path, size: int, document_bytes: int, *, check_budget: Any = None) -> Fixture:
    workspace, home, data, state, config = _fixture_paths(root)
    handoffs = workspace / "handoffs"
    _mkdir_private(handoffs)
    documents: dict[str, str] = {}
    for index in range(size):
        relative = f"handoffs/record-{index:05d}.md"
        content = make_markdown(index, document_bytes)
        target = workspace / relative
        target.write_text(content, encoding="utf-8")
        target.chmod(0o600)
        documents[relative] = content
        if check_budget is not None:
            check_budget()
    return Fixture(workspace, home, data, state, config, "legacy", documents, list(documents), document_bytes)


def _create_arguments(fixture: Fixture, storage: str, index: int, content: str) -> dict[str, Any]:
    arguments: dict[str, Any] = {"workspace": str(fixture.workspace), "content": content, "auto_switch": False}
    if storage == "legacy":
        arguments["path"] = f"handoffs/record-{index:05d}.md"
    else:
        arguments["name"] = f"record-{index:05d}.md"
    return arguments


_CENTRAL_SEED_SCRIPT = r'''
import json, sys
from pathlib import Path
from server import handoff_store

workspace = sys.argv[1]
seed_path = Path(sys.argv[2])
for raw in seed_path.read_text(encoding="utf-8").splitlines():
    item = json.loads(raw)
    record = handoff_store.create_record(workspace, item["name"], item["content"])
    print(json.dumps({"ref": record["ref"]}), flush=True)
'''


def _run_seed_helper(arguments: dict[str, Any], timeout: float) -> subprocess.CompletedProcess[str]:
    popen_arguments = dict(arguments)
    if popen_arguments.pop("capture_output", False):
        popen_arguments.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    popen_arguments.pop("check", None)
    process = subprocess.Popen(**popen_arguments, start_new_session=True)
    pgid = os.getpgid(process.pid)
    session_id = os.getsid(process.pid)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_owned_process_group(process, pgid, session_id)
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        raise subprocess.TimeoutExpired(process.args, timeout, output=stdout, stderr=stderr) from exc
    _terminate_owned_process_group(process, pgid, session_id)
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def _prepare_central(
    root: Path,
    build_root: Path,
    size: int,
    document_bytes: int,
    timeout: float,
    deadline: float | None,
    *,
    deadline_at: float | None = None,
    check_budget: Any = None,
) -> Fixture:
    workspace, home, data, state, config = _fixture_paths(root)
    fixture = Fixture(workspace, home, data, state, config, "central", {}, [], document_bytes)
    seed_path = root / "central-seed.jsonl"
    contents: list[str] = []
    with seed_path.open("w", encoding="utf-8") as seed:
        for index in range(size):
            content = make_markdown(index, document_bytes)
            contents.append(content)
            seed.write(json.dumps({"name": f"record-{index:05d}.md", "content": content}, ensure_ascii=False) + "\n")
            if check_budget is not None:
                check_budget()
    seed_path.chmod(0o600)
    environment = _isolated_environment(home, data, state, config)
    helper_kwargs = {
        "args": [sys.executable, "-c", _CENTRAL_SEED_SCRIPT, str(workspace), str(seed_path)],
        "cwd": str(build_root),
        "env": {**environment, "PYTHONPATH": str(build_root)},
        "capture_output": True,
        "text": True,
        "check": False,
    }
    if deadline_at is not None:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("benchmark deadline exceeded")
        helper_timeout = remaining
    elif deadline is not None:
        helper_timeout = deadline
    else:
        helper_timeout = timeout
    try:
        result = _run_seed_helper(helper_kwargs, helper_timeout)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("central fixture setup timed out") from exc
    finally:
        try:
            seed_path.unlink()
        except FileNotFoundError:
            pass
    if result.returncode != 0:
        message = (result.stderr or "central fixture setup failed").strip()[:512]
        raise EfficiencyError(message)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != size:
        raise MCPProtocolError("central fixture setup returned the wrong record count")
    for index, line in enumerate(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MCPProtocolError("central fixture setup returned invalid JSON") from exc
        ref = value.get("ref") if isinstance(value, dict) else None
        if not isinstance(ref, str):
            raise MCPProtocolError("central fixture setup omitted ref")
        fixture.documents[ref] = contents[index]
        fixture.identities.append(ref)
    return fixture


def _prepare_fixture(
    root: Path,
    build_root: Path,
    storage: str,
    size: int,
    document_bytes: int,
    timeout: float,
    deadline: float | None,
    *,
    deadline_at: float | None = None,
    check_budget: Any = None,
) -> Fixture:
    if storage == "legacy":
        return _prepare_legacy(root, size, document_bytes, check_budget=check_budget)
    if storage == "central":
        return _prepare_central(
            root,
            build_root,
            size,
            document_bytes,
            timeout,
            deadline,
            deadline_at=deadline_at,
            check_budget=check_budget,
        )
    raise EfficiencyError(f"unknown storage mode: {storage}")


def _write_catalog_state(fixture: Fixture, state: str) -> None:
    if state == "healthy":
        return
    catalog = fixture.state / "session-handoff" / "catalog.sqlite3"
    if state == "absent":
        try:
            catalog.unlink()
        except FileNotFoundError:
            pass
    elif state == "rebuild":
        catalog.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        catalog.write_bytes(b"not-a-sqlite-catalog")
        catalog.chmod(0o600)
    else:
        raise EfficiencyError(f"unknown catalog state: {state}")


def _validate_create(data: dict[str, Any], storage: str, expected: str) -> str:
    if data.get("storage") not in ({"central"} if storage == "central" else {None, "workspace"}):
        raise MCPProtocolError("create returned the wrong storage")
    if storage == "central":
        ref = data.get("ref")
        if not isinstance(ref, str) or not ref.startswith("handoff://"):
            raise MCPProtocolError("central create returned an invalid ref")
        return ref
    path = data.get("path")
    if path != expected:
        raise MCPProtocolError("legacy create returned the wrong path")
    return path


def _validate_read(data: dict[str, Any], expected: str, content: str) -> None:
    if data.get("content") != content or data.get("valid") is not True or data.get("missing_sections"):
        raise MCPProtocolError("read did not return the canonical fixture content")
    if _sha256(data["content"]) != _sha256(content):
        raise MCPProtocolError("read content hash did not match fixture")
    if expected.startswith("handoff://") and data.get("ref") != expected:
        raise MCPProtocolError("central read returned the wrong ref")
    if not expected.startswith("handoff://") and data.get("path") != expected:
        raise MCPProtocolError("legacy read returned the wrong path")


def _list_arguments(fixture: Fixture, storage: str, cursor: str | None, offset: int, *, supports_storage_argument: bool) -> dict[str, Any]:
    arguments: dict[str, Any] = {"workspace": str(fixture.workspace), "limit": PAGE_LIMIT}
    if storage == "central":
        arguments.update({"storage": "central", "scope": "project"})
        if cursor is not None:
            arguments["cursor"] = cursor
    else:
        if supports_storage_argument:
            arguments["storage"] = "workspace"
        arguments.update({"directory": "handoffs", "offset": offset})
    return arguments


def _validate_list(
    data: dict[str, Any],
    storage: str,
    seen: set[str],
    expected: Iterable[str] | None = None,
) -> tuple[list[str], bool, str | None]:
    items = data.get("items")
    count = data.get("count")
    has_more = data.get("has_more")
    if not isinstance(items, list) or count != len(items) or not isinstance(has_more, bool):
        raise MCPProtocolError("list returned invalid count/page fields")
    identities: list[str] = []
    page_seen: set[str] = set()
    for item in items:
        if storage == "central":
            value = item.get("ref") if isinstance(item, dict) else None
        else:
            value = item if isinstance(item, str) else None
        if not isinstance(value, str) or value in seen or value in page_seen:
            raise MCPProtocolError("list returned invalid or duplicate identity")
        identities.append(value)
        page_seen.add(value)
    expected_values = list(expected or ())
    if expected_values and identities != expected_values:
        raise MCPProtocolError("list returned unexpected identities or order")
    if storage == "central":
        next_cursor = data.get("next_cursor")
        if has_more and not isinstance(next_cursor, str):
            raise MCPProtocolError("central list omitted advancing cursor")
        if not has_more and next_cursor is not None:
            raise MCPProtocolError("central list returned cursor after final page")
    else:
        next_cursor = data.get("next_offset")
        if has_more and (isinstance(next_cursor, bool) or not isinstance(next_cursor, int) or next_cursor <= 0):
            raise MCPProtocolError("legacy list omitted advancing offset")
        if not has_more and next_cursor is not None:
            raise MCPProtocolError("legacy list returned offset after final page")
    return identities, has_more, next_cursor


def _ensure_advancing(previous: str | int | None, current: str | int | None, storage: str) -> None:
    if storage == "central" and current == previous:
        raise MCPProtocolError("central list cursor did not advance")
    if storage == "legacy" and isinstance(previous, int) and isinstance(current, int) and current <= previous:
        raise MCPProtocolError("legacy list offset did not advance")


def _query_for_kind(kind: str) -> str:
    return {"absent": "absent-token", "common": "common-token", "only_beyond_256": "only-beyond-256"}[kind]


def _truncate_search_snippet(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_SEARCH_SNIPPET_BYTES:
        return value
    suffix = b"..."
    return encoded[: MAX_SEARCH_SNIPPET_BYTES - len(suffix)].decode("utf-8", errors="ignore") + "..."


def _expected_search_matches(content: str, query: str) -> list[dict[str, Any]]:
    needle = query.casefold()
    matches: list[dict[str, Any]] = []
    for line_number, line in enumerate(content.splitlines(), 1):
        if needle not in line.casefold():
            continue
        matches.append({"line": line_number, "snippet": _truncate_search_snippet(line.strip())})
        if len(matches) >= MAX_SEARCH_MATCHES_PER_FILE:
            break
    return matches


def _validate_search(
    data: dict[str, Any],
    kind: str,
    size: int,
    expected_identities: Iterable[str] | None = None,
    storage: str | None = None,
    document_bytes: int = DEFAULT_DOCUMENT_BYTES,
    *,
    expected_documents: dict[str, str] | None = None,
    page_start: int = 0,
) -> dict[str, Any]:
    if data.get("query") != _query_for_kind(kind):
        raise MCPProtocolError("search returned a different query")
    items = data.get("items")
    if not isinstance(items, list) or data.get("count") != len(items):
        raise MCPProtocolError("search returned invalid items/count")
    scanned_files = data.get("scanned_files")
    scanned_bytes = data.get("scanned_bytes")
    scan_truncated = data.get("scan_truncated")
    has_more = data.get("has_more")
    skipped_count = data.get("skipped_count")
    output_truncated = data.get("output_truncated")
    if (
        isinstance(scanned_files, bool)
        or not isinstance(scanned_files, int)
        or scanned_files < 0
        or isinstance(scanned_bytes, bool)
        or not isinstance(scanned_bytes, int)
        or scanned_bytes < 0
        or not isinstance(scan_truncated, bool)
        or not isinstance(has_more, bool)
        or isinstance(skipped_count, bool)
        or not isinstance(skipped_count, int)
        or skipped_count < 0
        or not isinstance(output_truncated, bool)
    ):
        raise MCPProtocolError("search returned invalid coverage fields")
    if len(items) > PAGE_LIMIT:
        raise MCPProtocolError("search returned more items than its limit")
    if scanned_files > MAX_RECORDS or scanned_bytes > MAX_SEARCH_BYTES:
        raise MCPProtocolError("search exceeded its scan budget")
    if len(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise MCPProtocolError("search exceeded its output budget")
    if output_truncated and not has_more:
        raise MCPProtocolError("truncated search omitted continuation")
    next_cursor = data.get("next_cursor")
    next_offset = data.get("next_offset")
    if storage == "central":
        if next_offset is not None:
            raise MCPProtocolError("central search returned an offset")
        if has_more and (not isinstance(next_cursor, str) or not next_cursor):
            raise MCPProtocolError("central search omitted advancing cursor")
        if not has_more and next_cursor is not None:
            raise MCPProtocolError("central search returned cursor after final page")
    elif storage == "legacy":
        if next_cursor is not None:
            raise MCPProtocolError("legacy search returned a cursor")
        if has_more and (isinstance(next_offset, bool) or not isinstance(next_offset, int) or next_offset <= 0):
            raise MCPProtocolError("legacy search omitted advancing offset")
        if not has_more and next_offset is not None:
            raise MCPProtocolError("legacy search returned offset after final page")
    if page_start < 0 or page_start > size:
        raise MCPProtocolError("search page start was invalid")
    target_index = 256 if kind == "only_beyond_256" and size > 256 else None
    target_in_page = target_index is not None and page_start <= target_index < page_start + scanned_files
    expected_list = list(expected_identities or ())
    expected = set(expected_list)
    seen: set[str] = set()
    evidence: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("matches"), list):
            raise MCPProtocolError("search item was invalid")
        identity = item.get("ref") if storage == "central" else item.get("path")
        if expected and (not isinstance(identity, str) or identity not in expected or identity in seen):
            raise MCPProtocolError("search returned an unexpected or duplicate identity")
        if isinstance(identity, str):
            seen.add(identity)
        for match in item["matches"]:
            if (
                not isinstance(match, dict)
                or set(match) != {"line", "snippet"}
                or isinstance(match.get("line"), bool)
                or not isinstance(match.get("line"), int)
                or match["line"] < 1
                or not isinstance(match.get("snippet"), str)
            ):
                raise MCPProtocolError("search returned an invalid match")
        if expected_documents is not None:
            if not isinstance(identity, str) or identity not in expected_documents:
                raise MCPProtocolError("search returned an identity outside the fixture")
            if item["matches"] != _expected_search_matches(expected_documents[identity], data["query"]):
                raise MCPProtocolError("search returned incorrect matches")
        evidence.append({"identity": identity, "matches": item["matches"]})
        if kind == "absent":
            raise MCPProtocolError("absent search returned a match")
        if kind == "only_beyond_256" and size > 256 and expected and identity not in expected:
            raise MCPProtocolError("search returned a match outside the beyond-256 target")
    if kind == "absent" and items:
        raise MCPProtocolError("absent search returned a match")
    if kind == "common" and expected_list:
        expected_page = expected_list[: min(PAGE_LIMIT, size, MAX_DOCUMENT_BYTES // document_bytes)]
        actual = [item.get("ref") if storage == "central" else item.get("path") for item in items]
        if actual != expected_page:
            raise MCPProtocolError("common search did not return the expected first page")
    if kind == "common" and len(items) != min(PAGE_LIMIT, size, MAX_DOCUMENT_BYTES // document_bytes):
        raise MCPProtocolError("common search returned an incomplete first page")
    if kind == "only_beyond_256":
        actual = [item.get("ref") if storage == "central" else item.get("path") for item in items]
        expected_page = expected_list[:1] if target_in_page else []
        if actual != expected_page:
            raise MCPProtocolError("search identities did not match scanned page range")
    coverage = classify_search_coverage(
        data,
        query_kind=kind,
        target_index=target_index,
        page_start=page_start,
    )
    scan_limit = min(size, MAX_RECORDS, MAX_SEARCH_BYTES // document_bytes)
    remaining_limit = min(max(size - page_start, 0), MAX_RECORDS, MAX_SEARCH_BYTES // document_bytes)
    expected_scanned = min(remaining_limit, PAGE_LIMIT) if storage == "central" and kind == "common" else remaining_limit
    expected_count = min(PAGE_LIMIT, scan_limit) if kind == "common" else 0
    if kind in {"common", "absent"} and len(items) != expected_count:
        raise MCPProtocolError("search fixture count did not match")
    if coverage["skipped_count"]:
        raise MCPProtocolError("search fixture count or skipped records did not match")
    if coverage["scanned_files"] != expected_scanned or coverage["scanned_bytes"] != expected_scanned * document_bytes:
        raise MCPProtocolError("search scan counters did not match fixture budget")
    if storage is not None:
        expected_more = size > page_start + expected_scanned if storage == "central" else (kind == "common" and scan_limit > PAGE_LIMIT)
        expected_truncated = expected_more if storage == "central" else size > scan_limit
        if coverage["has_more"] != expected_more or coverage["scan_truncated"] != expected_truncated:
            raise MCPProtocolError("search pagination or truncation did not match fixture budget")
    if kind == "only_beyond_256" and size > 256 and coverage["target_covered"] and expected_list:
        actual = [item.get("ref") if storage == "central" else item.get("path") for item in items]
        if actual != expected_list[:1]:
            raise MCPProtocolError("beyond-256 search omitted its covered target")
    coverage["evidence"] = evidence
    return coverage


def _tool_read_arguments(fixture: Fixture, storage: str, identity: str) -> dict[str, Any]:
    return {"workspace": str(fixture.workspace), "ref" if storage == "central" else "path": identity}


def _run_operation(
    fixture: Fixture,
    build_root: Path,
    cell: dict[str, Any],
    *,
    timeout: float,
    deadline: float | None,
    deadline_at: float | None = None,
) -> dict[str, Any]:
    storage = str(cell.get("storage", "legacy"))
    operation = str(cell["operation"])
    catalog_state = cell.get("catalog_state")
    if catalog_state:
        _write_catalog_state(fixture, str(catalog_state))
    environment = _isolated_environment(fixture.home, fixture.data, fixture.state, fixture.config)
    metrics: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    client_kwargs: dict[str, Any] = {"root": build_root, "env": environment, "timeout": timeout}
    if deadline_at is not None:
        client_kwargs["deadline_at"] = deadline_at
    else:
        client_kwargs["deadline"] = deadline
    with PersistentMCP(**client_kwargs) as client:
        details["capabilities"] = sorted(client.capabilities)
        details["startup"] = dict(client.startup_metrics)
        details["operation_start"] = "after initialize, initialized notification, ping, and tools/list; filesystem cache is not cold"
        if operation == "startup":
            return {"metrics": {**client.startup_metrics, "request_count": 0}, "details": details}
        if operation == "create":
            index = int(cell["size"])
            content = make_markdown(index, fixture.document_bytes)
            data, request_metrics = client.call("handoff_create", _create_arguments(fixture, storage, index, content))
            identity = _validate_create(data, storage, f"handoffs/record-{index:05d}.md")
            if identity in fixture.documents:
                raise MCPProtocolError("create reused an existing fixture identity")
            metrics.append(request_metrics)
            read, _ = client.call("handoff_read", _tool_read_arguments(fixture, storage, identity))
            _validate_read(read, identity, content)
            details["records"] = 1
        elif operation == "read":
            identity = fixture.identities[0]
            content = fixture.documents[identity]
            data, request_metrics = client.call("handoff_read", _tool_read_arguments(fixture, storage, identity))
            _validate_read(data, identity, content)
            metrics.append(request_metrics)
            details["records"] = 1
        elif operation in {"list_first_page", "list_full_walk"}:
            cursor: str | None = None
            offset = 0
            seen: set[str] = set()
            pages = 0
            while True:
                data, request_metrics = client.call(
                    "handoff_list",
                    _list_arguments(
                        fixture,
                        storage,
                        cursor,
                        offset,
                        supports_storage_argument="storage" in getattr(client, "tool_arguments", {}).get("handoff_list", set()),
                        ),
                    )
                remaining = len(fixture.identities) - len(seen)
                expected_page = fixture.identities[len(seen) : len(seen) + min(PAGE_LIMIT, remaining)]
                strict_order = catalog_state not in {"absent", "rebuild"}
                identities, has_more, next_value = _validate_list(
                    data,
                    storage,
                    seen,
                    expected_page if strict_order else None,
                )
                if not strict_order:
                    if any(identity not in fixture.identities for identity in identities):
                        raise MCPProtocolError("catalog rebuild returned an unknown identity")
                elif identities != expected_page:
                    raise MCPProtocolError("list page did not match expected identities")
                if len(identities) != min(PAGE_LIMIT, remaining):
                    raise MCPProtocolError("list returned an incomplete page")
                expected_more = remaining > PAGE_LIMIT
                if has_more != expected_more:
                    raise MCPProtocolError("list returned an incorrect has_more value")
                seen.update(identities)
                metrics.append(request_metrics)
                pages += 1
                if operation == "list_first_page" or not has_more:
                    break
                if storage == "central":
                    assert isinstance(next_value, str)
                    _ensure_advancing(cursor, next_value, storage)
                    cursor = next_value
                else:
                    assert isinstance(next_value, int)
                    _ensure_advancing(offset, next_value, storage)
                    offset = next_value
            details.update({"pages": pages, "listed": len(seen), "expected": int(cell["size"])})
            if operation == "list_full_walk" and len(seen) != int(cell["size"]):
                raise MCPProtocolError("full list walk did not cover every fixture record")
        elif operation in {"search", "search_full_walk"}:
            query_kind = str(cell["query_kind"])
            if query_kind == "common":
                expected_search = fixture.identities
            elif query_kind == "only_beyond_256" and int(cell["size"]) > 256:
                expected_search = fixture.identities[256:257]
            else:
                expected_search = ()
            cursor: str | None = None
            offset = 0
            page_start = 0
            seen: set[str] = set()
            pages: list[dict[str, Any]] = []
            while True:
                data, request_metrics = client.call(
                    "handoff_search",
                    {
                        "workspace": str(fixture.workspace),
                        "storage": "central" if storage == "central" else "workspace",
                        "scope": "project",
                        "query": _query_for_kind(query_kind),
                        "limit": PAGE_LIMIT,
                        **({"cursor": cursor} if storage == "central" and cursor is not None else {}),
                        **({"offset": offset} if storage == "legacy" and offset else {}),
                    },
                )
                coverage = _validate_search(
                    data,
                    query_kind,
                    int(cell["size"]),
                    expected_search,
                    storage,
                    fixture.document_bytes,
                    expected_documents=fixture.documents,
                    page_start=page_start,
                )
                identities = [item["identity"] for item in coverage["evidence"] if isinstance(item.get("identity"), str)]
                if any(identity in seen for identity in identities):
                    raise MCPProtocolError("search walk returned a duplicate identity")
                seen.update(identities)
                pages.append(coverage)
                metrics.append(request_metrics)
                if operation == "search" or not data["has_more"]:
                    break
                next_value = data.get("next_cursor" if storage == "central" else "next_offset")
                previous = cursor if storage == "central" else offset
                _ensure_advancing(previous, next_value, storage)
                if storage == "central":
                    assert isinstance(next_value, str)
                    cursor = next_value
                else:
                    assert isinstance(next_value, int)
                    offset = next_value
                page_start += coverage["scanned_files"]
                if page_start > int(cell["size"]):
                    raise MCPProtocolError("search walk exceeded fixture size")
            if operation == "search_full_walk" and seen != set(expected_search):
                raise MCPProtocolError("search walk omitted or added a fixture match")
            details["coverage"] = pages[-1]
            details["search_evidence"] = pages
            details["search_pages"] = len(pages)
            details["query_kind"] = query_kind
        else:
            raise EfficiencyError(f"unsupported operation: {operation}")
    return {"metrics": _sum_metrics(metrics), "details": details}


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:512]}"


def _loadavg() -> list[float] | None:
    try:
        return list(os.getloadavg())
    except (AttributeError, OSError):
        return None


class DiskBudget:
    def __init__(self, limit: int, paths: Iterable[Path], deadline_at: float | None = None) -> None:
        if limit < 1:
            raise EfficiencyError("disk budget must be positive")
        self.limit = limit
        self.paths = tuple(paths)
        self.deadline_at = deadline_at

    def check(self, reserve: int = 0) -> None:
        if self.deadline_at is not None and time.monotonic() >= self.deadline_at:
            raise TimeoutError("benchmark deadline exceeded")
        if reserve < 0:
            raise EfficiencyError("disk budget reserve must not be negative")
        total = 0
        for root in self.paths:
            if not root.exists():
                continue
            if root.is_file():
                info = root.stat()
                total += max(info.st_size, info.st_blocks * 512)
                if total + reserve > self.limit:
                    raise EfficiencyError(f"disk budget exceeded ({total + reserve} > {self.limit} bytes)")
                continue
            for path in root.rglob("*"):
                if not path.is_symlink():
                    info = path.stat()
                    total += max(info.st_size, info.st_blocks * 512)
                    if total + reserve > self.limit:
                        raise EfficiencyError(f"disk budget exceeded ({total + reserve} > {self.limit} bytes)")


def _checkpoint_checker(budget: DiskBudget, document_bytes: int) -> Any:
    """Fixture disk space is reserved up front; check time without rescanning."""

    def check() -> None:
        if budget.deadline_at is not None and time.monotonic() >= budget.deadline_at:
            raise TimeoutError("benchmark deadline exceeded")

    return check


def _build_env(build: Any) -> dict[str, Any]:
    return {"label": build.label, "root": str(build.root), "identity": build.identity}


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _write_raw(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    path.chmod(0o600)


def _append_raw(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    path.chmod(0o600)


def _fresh_output(path: Path) -> None:
    if path.exists():
        raise EfficiencyError(f"output path already exists: {path}")
    if not path.parent.is_dir():
        raise EfficiencyError(f"output parent is not a directory: {path.parent}")


def _run(
    builds: dict[str, Any],
    cells: list[dict[str, Any]],
    *,
    output: Path,
    samples: int,
    document_bytes: int,
    timeout: float,
    deadline: float,
    disk_budget: int,
    plan: dict[str, Any],
) -> bool:
    _fresh_output(output)
    output.mkdir(mode=0o700)
    raw_path = output / "raw.jsonl"
    _write_json(output / "plan.json", plan)
    rows: list[dict[str, Any]] = []
    run_error: str | None = None
    run_deadline = time.monotonic() + deadline
    row_number = 0
    with tempfile.TemporaryDirectory(prefix="session-handoff-efficiency-", dir=str(output.parent)) as temp_name:
        temp_root = Path(temp_name)
        budget = DiskBudget(disk_budget, (temp_root, output), run_deadline)
        cache: dict[tuple[str, str, int, int], CachedFixture] = {}
        for sample in range(1, samples + 1):
            for execution_order, cell in enumerate(
                _ordered_cells(cells, sample, tuple(builds)),
                start=1,
            ):
                row_number += 1
                build = builds[str(cell["build"])]
                cell_key = json.dumps(cell, sort_keys=True, separators=(",", ":"))
                row: dict[str, Any] = {
                    "schema_version": 1,
                    "kind": "efficiency_sample",
                    "cell_key": cell_key,
                    "cell": cell,
                    "build": _build_env(build),
                    "sample": sample,
                    "execution_order": execution_order,
                    "host_loadavg": _loadavg(),
                    "status": "failed",
                    "started_at_unix_ns": time.time_ns(),
                }
                try:
                    setup_started = time.perf_counter_ns()
                    if time.monotonic() >= run_deadline:
                        raise TimeoutError("benchmark deadline exceeded")
                    storage = str(cell.get("storage", "legacy"))
                    size = int(cell.get("size") or 0)
                    cache_key = (build.label, storage, size, document_bytes)
                    cached = cache.get(cache_key)
                    if cached is None:
                        budget.check(reserve=max(1, size) * (document_bytes + 65_536) * 2)
                        cache_root = temp_root / f"cache-{len(cache):04d}"
                        fixture = _prepare_fixture(
                            cache_root,
                            build.root,
                            storage,
                            size,
                            document_bytes,
                            timeout,
                            None,
                            deadline_at=run_deadline,
                            check_budget=_checkpoint_checker(budget, document_bytes),
                        )
                        cached = _snapshot_fixture(
                            fixture,
                            cache_root.with_name(cache_root.name + "-snapshot"),
                        )
                        cache[cache_key] = cached
                        budget.check()
                    fixture = _restore_fixture(cached)
                    row["fixture_setup_ns"] = time.perf_counter_ns() - setup_started
                    result = _run_operation(
                        fixture,
                        build.root,
                        cell,
                        timeout=timeout,
                        deadline=None,
                        deadline_at=run_deadline,
                    )
                    row.update({"status": "ok", **result})
                    cached.dirty = cell["operation"] == "create" or cell.get("catalog_state") in {"absent", "rebuild"}
                except UnsupportedCapability as exc:
                    row.update({"status": "unsupported", "reason": _error_text(exc)})
                except Exception as exc:
                    row.update({"status": "failed", "error": _error_text(exc)})
                rows.append(row)
                row["finished_at_unix_ns"] = time.time_ns()
                _append_raw(raw_path, row)
                if row["status"] == "failed":
                    run_error = row.get("error", "sample failed")
                    break
                if time.monotonic() >= run_deadline:
                    run_error = "TimeoutError: benchmark deadline exceeded"
                    break
            if run_error is not None:
                break
            try:
                budget.check()
            except (EfficiencyError, TimeoutError) as exc:
                run_error = _error_text(exc)
                break
    aggregate = aggregate_samples(rows)
    failed = sum(row.get("status") == "failed" for row in rows)
    _write_json(
        output / "summary.json",
        {
            "schema_version": 1,
            "runner": "efficiency-v1",
            "aggregate": aggregate,
            "attempted": len(rows),
            "failed": failed,
            "unsupported": sum(row.get("status") == "unsupported" for row in rows),
            "run_error": run_error,
            "notes": plan["notes"],
        },
    )
    return failed == 0 and run_error is None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="append", default=[], metavar="LABEL=ROOT", help="frozen build (repeat exactly twice)")
    parser.add_argument("--main-root", type=Path, help="explicit baseline root")
    parser.add_argument("--candidate-root", type=Path, help="explicit candidate root")
    parser.add_argument("--output", type=Path, required=True, help="fresh output directory (execution only)")
    parser.add_argument("--sizes", default=",".join(str(value) for value in DEFAULT_SIZES))
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--document-bytes", type=int, default=DEFAULT_DOCUMENT_BYTES)
    parser.add_argument("--scenarios", default=",".join(DEFAULT_SCENARIOS))
    parser.add_argument("--timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT)
    parser.add_argument("--deadline", type=float, default=DEFAULT_DEADLINE)
    parser.add_argument("--disk-budget", type=int, default=DEFAULT_DISK_BUDGET)
    parser.add_argument("--execute", action="store_true", help="execute the offline benchmark")
    return parser


def _load_builds(args: argparse.Namespace) -> dict[str, Any]:
    specs: list[tuple[str, Path]] = []
    if args.build:
        if args.main_root is not None or args.candidate_root is not None or len(args.build) != 2:
            raise EfficiencyError("use exactly two --build values, or both explicit roots")
        for value in args.build:
            label, raw_root = value.split("=", 1) if "=" in value else ("", "")
            if not label or not raw_root:
                raise EfficiencyError("build must be LABEL=ROOT")
            specs.append((label, Path(raw_root)))
    elif args.main_root is not None and args.candidate_root is not None:
        specs = [("main", args.main_root), ("candidate", args.candidate_root)]
    else:
        raise EfficiencyError("provide exactly two --build values or both explicit roots")
    labels = [label for label, _ in specs]
    if len(set(labels)) != 2:
        raise EfficiencyError("build labels must be distinct")
    loaded = {label: load_build(label, root) for label, root in specs}
    if len({str(build.root) for build in loaded.values()}) != 2:
        raise EfficiencyError("build roots must be distinct")
    return loaded


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if (
            args.samples < 1
            or args.timeout <= 0
            or args.deadline <= 0
            or not 512 <= args.document_bytes <= MAX_DOCUMENT_BYTES
            or not 1 <= args.disk_budget <= DEFAULT_DISK_BUDGET
        ):
            raise EfficiencyError("samples, timeout, deadline, and document-bytes must be positive")
        sizes = parse_sizes(args.sizes)
        scenarios = parse_scenarios(args.scenarios)
        builds = _load_builds(args)
        cells = plan_cells(sizes, tuple(builds), scenarios)
        plan = {
            "schema_version": 1,
            "runner": "efficiency-v1",
            "builds": [_build_env(build) for build in builds.values()],
            "sizes": list(sizes),
            "samples": args.samples,
            "document_bytes": args.document_bytes,
            "scenarios": list(scenarios),
            "cells": cells,
            "provenance": {
                "runner_source_sha256": _sha256(Path(__file__).read_bytes()),
                "python": sys.version,
                "python_executable": sys.executable,
                "platform": sys.platform,
                "cwd": str(Path.cwd()),
                "argv": list(sys.argv if argv is None else [sys.argv[0], *argv]),
                "perf_counter": {
                    "implementation": time.get_clock_info("perf_counter").implementation,
                    "monotonic": time.get_clock_info("perf_counter").monotonic,
                    "adjustable": time.get_clock_info("perf_counter").adjustable,
                    "resolution": time.get_clock_info("perf_counter").resolution,
                },
                "arm_order": list(builds),
                "sample_order": "ABBA",
            },
            "metric_labels": {
                "peak_process_rss_bytes": "MCP server process VmHWM peak, not a per-request allocation peak",
                "request_rss_delta_bytes": "signed MCP server VmRSS after minus before request; not summed across requests",
                "proc_io_rchar_bytes": "MCP process /proc/PID/io logical bytes, including waited children where kernel accounts them",
                "proc_io_wchar_bytes": "MCP process /proc/PID/io logical bytes, including waited children where kernel accounts them",
                "proc_io_read_bytes": "MCP process /proc/PID/io storage bytes read",
                "proc_io_write_bytes": "MCP process /proc/PID/io storage bytes written",
                "cpu_user_ns": "MCP server CPU only, excluding all Git descendants; 10 ms ticks on this Linux host",
                "cpu_system_ns": "MCP server CPU only, excluding all Git descendants; 10 ms ticks on this Linux host",
            },
            "budgets": {
                "records": MAX_RECORDS,
                "document_bytes": MAX_DOCUMENT_BYTES,
                "output_bytes": MAX_OUTPUT_BYTES,
                "disk_budget_bytes": args.disk_budget,
                "deadline_seconds": args.deadline,
                "request_timeout_seconds": args.timeout,
            },
            "notes": [
                "No provider calls or network-dependent behavior are used.",
                "Fixture construction and output writes are outside request timing.",
                "Process-fresh does not mean cold OS cache; no global cache drop is performed.",
                "peak_process_rss_bytes is whole MCP-process peak RSS; request_rss_delta_bytes is only before/after RSS.",
                "/proc/PID/io rchar/wchar are logical process bytes and read_bytes/write_bytes are storage bytes; procfs child accounting is not summed separately.",
                "CPU is MCP-server /proc/PID/stat CPU; it excludes Git descendants, including exited descendants.",
                "Search coverage is reported explicitly; truncated or skipped work is not rewarded.",
                "Central N-record fixtures are populated by an isolated build-local storage seed outside request timing; measured operations use the public stdio MCP contract.",
                "The search lane probes legacy search on both builds; a baseline without handoff_search is retained as unsupported, while central search is candidate-only.",
                "Measured operations start after MCP handshake/tools discovery in one persistent process; this is not a cold operation.",
            ],
        }
        if not args.execute:
            _fresh_output(args.output)
            print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        success = _run(
            builds,
            cells,
            output=args.output,
            samples=args.samples,
            document_bytes=args.document_bytes,
            timeout=args.timeout,
            deadline=args.deadline,
            disk_budget=args.disk_budget,
            plan=plan,
        )
        print(json.dumps({"output": str(args.output.resolve()), "cells": len(cells), "samples": args.samples * len(cells)}, sort_keys=True))
        return 0 if success else 1
    except (EfficiencyError, OSError, ValueError) as exc:
        print(f"efficiency benchmark failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
