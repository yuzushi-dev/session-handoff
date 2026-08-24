#!/usr/bin/env python3
"""Score context-rot handoff fidelity and continuation outcomes."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

FACT_STATUSES = {"preserved", "missing", "incorrect"}


def _weighted_ratio(items: list[dict[str, Any]], predicate) -> float | None:
    total = sum(float(item.get("weight", 1)) for item in items)
    if total <= 0:
        return None
    kept = sum(float(item.get("weight", 1)) for item in items if predicate(item))
    return kept / total


def score_run(run: dict[str, Any]) -> dict[str, Any]:
    facts = run.get("facts", [])
    traps = run.get("stale_traps", [])
    dod = run.get("dod", [])

    for fact in facts:
        status = fact.get("status")
        if status not in FACT_STATUSES:
            raise ValueError(f"invalid fact status for {fact.get('id')}: {status}")
    for trap in traps:
        if not isinstance(trap.get("activated"), bool):
            raise ValueError(f"stale trap {trap.get('id')} must have boolean activated")
    for item in dod:
        if not isinstance(item.get("passed"), bool):
            raise ValueError(f"DoD item {item.get('id')} must have boolean passed")

    fact_counts = Counter(item["status"] for item in facts)
    rcr = fact_counts["preserved"] / len(facts) if facts else None
    incorrect_rate = fact_counts["incorrect"] / len(facts) if facts else None
    weighted_rcr = _weighted_ratio(facts, lambda item: item["status"] == "preserved")
    stale_context_intrusion = _weighted_ratio(traps, lambda item: item["activated"])
    dod_pass_rate = _weighted_ratio(dod, lambda item: item["passed"])

    return {
        "case": run.get("case"),
        "band": run.get("band"),
        "condition": run.get("condition"),
        "rcr": rcr,
        "weighted_rcr": weighted_rcr,
        "incorrect_fact_rate": incorrect_rate,
        "stale_context_intrusion": stale_context_intrusion,
        "dod_pass_rate": dod_pass_rate,
        "task_success": bool(run.get("task_success", False)),
        "repeated_failed_attempts": int(run.get("repeated_failed_attempts", 0)),
        "stale_decisions_acted_on": int(run.get("stale_decisions_acted_on", 0)),
        "recovery_reads": int(run.get("recovery_reads", 0)),
        "input_tokens": run.get("input_tokens"),
        "output_tokens": run.get("output_tokens"),
        "wall_seconds": run.get("wall_seconds"),
    }


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def aggregate(scored: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scored:
        groups[str(row.get("condition"))].append(row)

    summary: dict[str, Any] = {}
    metric_names = (
        "rcr",
        "weighted_rcr",
        "incorrect_fact_rate",
        "stale_context_intrusion",
        "dod_pass_rate",
        "repeated_failed_attempts",
        "stale_decisions_acted_on",
        "recovery_reads",
        "input_tokens",
        "output_tokens",
        "wall_seconds",
    )
    for condition, rows in groups.items():
        item: dict[str, Any] = {
            "runs": len(rows),
            "task_success_rate": sum(1 for row in rows if row["task_success"]) / len(rows),
        }
        for metric in metric_names:
            values = [float(row[metric]) for row in rows if row.get(metric) is not None]
            item[f"mean_{metric}"] = _mean(values)
        summary[condition] = item
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evaluation", type=Path)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    payload = json.loads(args.evaluation.read_text(encoding="utf-8"))
    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        raise SystemExit("evaluation must contain a non-empty runs array")

    scored = [score_run(run) for run in runs]
    result = {"runs": scored, "aggregate_by_condition": aggregate(scored)}
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
