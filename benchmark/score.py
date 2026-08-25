#!/usr/bin/env python3
"""Score context-rot handoff fidelity and continuation outcomes."""

from __future__ import annotations

import argparse
import itertools
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

FACT_STATUSES = {"preserved", "missing", "incorrect"}
COUNTER_FIELDS = (
    "repeated_failed_attempts",
    "stale_decisions_acted_on",
    "recovery_reads",
)
OPTIONAL_METRICS = ("input_tokens", "output_tokens", "wall_seconds")


def _weighted_ratio(items: list[dict[str, Any]], predicate) -> float | None:
    total = sum(float(item.get("weight", 1)) for item in items)
    if total <= 0:
        return None
    kept = sum(float(item.get("weight", 1)) for item in items if predicate(item))
    return kept / total


def _validate_items(items: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise ValueError(f"{label} must be a non-empty array")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"{label} items must be objects")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError(f"{label} id must be a non-empty string")
        if item_id in seen:
            singular = label[:-1] if label.endswith("s") else label
            raise ValueError(f"duplicate {singular} id: {item_id}")
        seen.add(item_id)
        weight = item.get("weight", 1)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
            raise ValueError(f"{label} item {item_id} must have a positive weight")
    return items


def _validate_run(run: Any) -> None:
    if not isinstance(run, dict):
        raise ValueError("run must be an object")
    for field in ("case", "band", "condition"):
        if not isinstance(run.get(field), str) or not run[field]:
            raise ValueError(f"run {field} must be a non-empty string")

    facts = _validate_items(run.get("facts"), "facts")
    traps = _validate_items(run.get("stale_traps"), "stale_traps")
    dod = _validate_items(run.get("dod"), "dod")
    for fact in facts:
        status = fact.get("status")
        if status not in FACT_STATUSES:
            raise ValueError(f"invalid fact status for {fact.get('id')}: {status}")
        if not isinstance(fact.get("critical"), bool):
            raise ValueError(f"fact {fact.get('id')} must have boolean critical")
    for trap in traps:
        if not isinstance(trap.get("activated"), bool):
            raise ValueError(f"stale trap {trap.get('id')} must have boolean activated")
    for item in dod:
        if not isinstance(item.get("passed"), bool):
            raise ValueError(f"DoD item {item.get('id')} must have boolean passed")
    if not isinstance(run.get("task_success"), bool):
        raise ValueError("task_success must be boolean")
    for field in COUNTER_FIELDS:
        value = run.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")
    for field in OPTIONAL_METRICS:
        value = run.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
        ):
            raise ValueError(f"{field} must be null or a non-negative number")


def score_run(run: dict[str, Any]) -> dict[str, Any]:
    _validate_run(run)
    facts = run.get("facts", [])
    traps = run.get("stale_traps", [])
    dod = run.get("dod", [])

    fact_counts = Counter(item["status"] for item in facts)
    rcr = fact_counts["preserved"] / len(facts) if facts else None
    incorrect_rate = fact_counts["incorrect"] / len(facts) if facts else None
    weighted_rcr = _weighted_ratio(facts, lambda item: item["status"] == "preserved")
    critical_facts = [item for item in facts if item["critical"]]
    critical_rcr = (
        sum(item["status"] == "preserved" for item in critical_facts) / len(critical_facts)
        if critical_facts
        else None
    )
    stale_context_intrusion = _weighted_ratio(traps, lambda item: item["activated"])
    dod_pass_rate = _weighted_ratio(dod, lambda item: item["passed"])

    return {
        "case": run.get("case"),
        "band": run.get("band"),
        "condition": run.get("condition"),
        "replicate": run.get("replicate"),
        "rcr": rcr,
        "weighted_rcr": weighted_rcr,
        "critical_rcr": critical_rcr,
        "incorrect_fact_rate": incorrect_rate,
        "stale_context_intrusion": stale_context_intrusion,
        "dod_pass_rate": dod_pass_rate,
        "task_success": run["task_success"],
        "repeated_failed_attempts": run["repeated_failed_attempts"],
        "stale_decisions_acted_on": run["stale_decisions_acted_on"],
        "recovery_reads": run["recovery_reads"],
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
        "critical_rcr",
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


def validate_study_manifest(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("evaluation schema_version must be 1")
    study = payload.get("study")
    if not isinstance(study, dict):
        raise ValueError("evaluation must contain a study object")

    axes: dict[str, list[str]] = {}
    for field in ("cases", "bands", "conditions"):
        values = study.get(field)
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not value for value in values)
            or len(set(values)) != len(values)
        ):
            raise ValueError(f"study {field} must contain unique non-empty strings")
        axes[field] = values
    replications = study.get("runs_per_condition")
    if isinstance(replications, bool) or not isinstance(replications, int) or replications < 1:
        raise ValueError("study runs_per_condition must be a positive integer")

    expected = set(
        itertools.product(
            axes["cases"],
            axes["bands"],
            axes["conditions"],
            range(1, replications + 1),
        )
    )
    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("evaluation must contain a non-empty runs array")
    actual: set[tuple[str, str, str, int]] = set()
    for run in runs:
        if not isinstance(run, dict):
            raise ValueError("run must be an object")
        for field in ("case", "band", "condition"):
            if not isinstance(run.get(field), str) or not run[field]:
                raise ValueError(f"run {field} must be a non-empty string")
        replicate = run.get("replicate")
        if isinstance(replicate, bool) or not isinstance(replicate, int) or replicate < 1:
            raise ValueError("run replicate must be a positive integer")
        key = (run["case"], run["band"], run["condition"], replicate)
        if key in actual:
            raise ValueError(f"duplicate run: {key}")
        if key not in expected:
            raise ValueError(f"unexpected run: {key}")
        actual.add(key)
    missing = sorted(expected - actual)
    if missing:
        raise ValueError(f"missing runs: {missing[:5]}")


def validate_evaluation(payload: Any) -> None:
    validate_study_manifest(payload)
    for run in payload["runs"]:
        _validate_run(run)


def release_gate(scored: list[dict[str, Any]]) -> dict[str, Any]:
    handoff = [row for row in scored if row.get("condition") == "handoff"]
    failures: list[dict[str, Any]] = []
    if not handoff:
        return {"passed": False, "failures": [{"metric": "handoff_runs", "value": 0}]}
    thresholds = {
        "critical_rcr": 1.0,
        "incorrect_fact_rate": 0.0,
        "stale_context_intrusion": 0.0,
    }
    for row in handoff:
        for metric, expected in thresholds.items():
            if row.get(metric) != expected:
                failures.append(
                    {
                        "case": row.get("case"),
                        "band": row.get("band"),
                        "replicate": row.get("replicate"),
                        "metric": metric,
                        "value": row.get(metric),
                        "expected": expected,
                    }
                )
    return {"passed": not failures, "failures": failures}


def score_evaluation(payload: dict[str, Any]) -> dict[str, Any]:
    validate_evaluation(payload)
    scored = [score_run(run) for run in payload["runs"]]
    return {
        "runs": scored,
        "aggregate_by_condition": aggregate(scored),
        "release_gate": release_gate(scored),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evaluation", type=Path)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    payload = json.loads(args.evaluation.read_text(encoding="utf-8"))
    try:
        result = score_evaluation(payload)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
