#!/usr/bin/env python3
"""Score context-rot handoff fidelity and continuation outcomes."""

from __future__ import annotations

import argparse
import itertools
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any

FACT_STATUSES = {"preserved", "missing", "incorrect"}
COUNTER_FIELDS = (
    "repeated_failed_attempts",
    "stale_decisions_acted_on",
    "recovery_reads",
)
OPTIONAL_METRICS = (
    "input_tokens",
    "output_tokens",
    "wall_seconds",
    "supplied_context_bytes",
)
HANDOFF_FORMATS = ("markdown-v1", "state-v1")
DEFAULT_HANDOFF_FORMAT = "markdown-v1"
PAIRED_METRICS = (
    "supplied_context_bytes",
    "input_tokens",
    "recovery_reads",
    "wall_seconds",
    "critical_rcr",
    "incorrect_fact_rate",
    "stale_context_intrusion",
    "dod_pass_rate",
)
RELEASE_CASES = (
    "buried-constraint",
    "superseded-decision",
    "failed-attempt-trap",
    "partial-state",
    "late-correction",
    "compound-rot",
)
RELEASE_BANDS = ("short", "long", "very_long")
RELEASE_CONDITIONS = ("full", "handoff", "migrate", "oracle")
MIN_RELEASE_REPLICATIONS = 2
MIN_CANDIDATE_REPLICATIONS = 2
PAIR_IDENTITY_FIELDS = (
    "pair_fingerprint",
    "client",
    "model",
    "revision",
    "source_sha256",
)
MIN_CALIBRATION_SAMPLE_SIZE = 18
MIN_CALIBRATION_AGREEMENT = 0.8


def _handoff_format(run: dict[str, Any]) -> str:
    value = run.get("handoff_format", DEFAULT_HANDOFF_FORMAT)
    if value not in HANDOFF_FORMATS:
        raise ValueError(f"invalid handoff_format: {value}")
    if run.get("condition") != "handoff" and value != DEFAULT_HANDOFF_FORMAT:
        raise ValueError("state-v1 is only valid for the handoff condition")
    return value


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
    _handoff_format(run)

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
    for field in PAIR_IDENTITY_FIELDS:
        value = run.get(field)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"{field} must be null or a non-empty string")
    timestamp = run.get("execution_started_at_ns")
    if timestamp is not None and (
        isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0
    ):
        raise ValueError("execution_started_at_ns must be null or a non-negative integer")
    if "arm_order" in run or "arm_position" in run:
        arm_order = run.get("arm_order")
        arm_position = run.get("arm_position")
        if (
            not isinstance(arm_order, list)
            or not arm_order
            or any(value not in HANDOFF_FORMATS for value in arm_order)
            or len(set(arm_order)) != len(arm_order)
        ):
            raise ValueError("arm_order must be a permutation of supported handoff formats")
        if (
            isinstance(arm_position, bool)
            or not isinstance(arm_position, int)
            or arm_position < 1
            or arm_position > len(arm_order)
        ):
            raise ValueError("arm_position must identify a handoff arm")
        if run.get("handoff_format") != arm_order[arm_position - 1]:
            raise ValueError("arm_position does not match handoff_format")


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
        "handoff_format": _handoff_format(run),
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
        "supplied_context_bytes": run.get("supplied_context_bytes"),
        **{field: run.get(field) for field in PAIR_IDENTITY_FIELDS},
        "arm_order": run.get("arm_order"),
        "arm_position": run.get("arm_position"),
        "execution_started_at_ns": run.get("execution_started_at_ns"),
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
        "supplied_context_bytes",
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


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _median(values: list[Any]) -> int | float | None:
    numeric = [value for value in values if _numeric(value)]
    return median(numeric) if numeric else None


def _pair_identity_error(
    markdown: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any] | None:
    for field in PAIR_IDENTITY_FIELDS:
        expected = markdown.get(field)
        actual = state.get(field)
        if not isinstance(expected, str) or not expected:
            return {
                "metric": "pair_identity",
                "field": field,
                "value": expected,
                "expected": "same non-empty value",
            }
        if actual != expected:
            return {
                "metric": "pair_identity",
                "field": field,
                "value": actual,
                "expected": expected,
            }
    if markdown.get("arm_order") != state.get("arm_order"):
        return {
            "metric": "pair_identity",
            "field": "arm_order",
            "value": state.get("arm_order"),
            "expected": markdown.get("arm_order"),
        }
    if not isinstance(markdown.get("arm_order"), list):
        return {
            "metric": "pair_identity",
            "field": "arm_order",
            "value": markdown.get("arm_order"),
            "expected": "same recorded arm order",
        }
    markdown_position = markdown.get("arm_position")
    state_position = state.get("arm_position")
    markdown_started = markdown.get("execution_started_at_ns")
    state_started = state.get("execution_started_at_ns")
    if (
        markdown_position not in (1, 2)
        or state_position not in (1, 2)
        or markdown_position == state_position
        or not isinstance(markdown_started, int)
        or not isinstance(state_started, int)
    ):
        return {
            "metric": "pair_identity",
            "field": "execution_started_at_ns",
            "value": {"markdown": markdown_started, "state": state_started},
            "expected": "recorded timestamps for distinct arm positions",
        }
    first_started, second_started = (
        (markdown_started, state_started)
        if markdown_position < state_position
        else (state_started, markdown_started)
    )
    if first_started >= second_started:
        return {
            "metric": "pair_identity",
            "field": "execution_started_at_ns",
            "value": {"markdown": markdown_started, "state": state_started},
            "expected": "arm positions start in recorded order",
        }
    return None


def paired_handoff_summary(scored: list[dict[str, Any]]) -> dict[str, Any]:
    """Report only comparable Markdown/state pairs and their raw deltas."""
    groups: dict[tuple[str, str, int], dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in scored:
        if row.get("condition") != "handoff":
            continue
        handoff_format = row.get("handoff_format", DEFAULT_HANDOFF_FORMAT)
        if handoff_format in HANDOFF_FORMATS:
            key = (row["case"], row["band"], row["replicate"])
            groups[key][handoff_format].append(row)

    pairs: list[dict[str, Any]] = []
    pairing_errors: list[dict[str, Any]] = []
    complete_pairs = 0
    for case, band, replicate in sorted(groups):
        format_rows = groups[(case, band, replicate)]
        formats = {
            handoff_format: rows[0]
            for handoff_format, rows in format_rows.items()
            if rows
        }
        pair: dict[str, Any] = {
            "case": case,
            "band": band,
            "replicate": replicate,
        }
        for handoff_format in HANDOFF_FORMATS:
            if handoff_format in formats:
                pair[handoff_format] = formats[handoff_format]
        errors: list[dict[str, Any]] = []
        for handoff_format, rows in format_rows.items():
            if len(rows) != 1:
                errors.append(
                    {
                        "metric": "pairing",
                        "case": case,
                        "band": band,
                        "replicate": replicate,
                        "format": handoff_format,
                        "value": len(rows),
                        "expected": 1,
                    }
                )
        markdown = (
            formats.get("markdown-v1")
            if len(format_rows.get("markdown-v1", [])) == 1
            else None
        )
        state = (
            formats.get("state-v1")
            if len(format_rows.get("state-v1", [])) == 1
            else None
        )
        delta: dict[str, int | float] = {}
        if markdown is not None and state is not None:
            identity_error = _pair_identity_error(markdown, state)
            if identity_error is not None:
                errors.append(
                    {
                        **identity_error,
                        "case": case,
                        "band": band,
                        "replicate": replicate,
                    }
                )
            else:
                complete_pairs += 1
                for metric in PAIRED_METRICS:
                    if _numeric(markdown.get(metric)) and _numeric(state.get(metric)):
                        delta[metric] = state[metric] - markdown[metric]
                delta["task_success"] = int(state["task_success"]) - int(
                    markdown["task_success"]
                )
        elif set(format_rows) != set(HANDOFF_FORMATS):
            errors.append(
                {
                    "metric": "pairing",
                    "case": case,
                    "band": band,
                    "replicate": replicate,
                    "value": sorted(format_rows),
                    "expected": list(HANDOFF_FORMATS),
                }
            )
        if errors:
            pairing_errors.extend(errors)
            pair["pairing_errors"] = errors
        pair["delta_state_minus_markdown"] = delta
        pairs.append(pair)

    by_format: dict[str, dict[str, Any]] = {}
    for handoff_format in HANDOFF_FORMATS:
        rows = [
            format_rows[handoff_format][0]
            for format_rows in groups.values()
            if len(format_rows.get(handoff_format, [])) == 1
        ]
        summary: dict[str, Any] = {
            "runs": len(rows),
            "complete_pairs": complete_pairs,
            "task_success_rate": (
                sum(1 for row in rows if row["task_success"]) / len(rows)
                if rows
                else None
            ),
        }
        for metric in PAIRED_METRICS:
            summary[f"median_{metric}"] = _median([row.get(metric) for row in rows])
        by_format[handoff_format] = summary
    return {
        "pairs": pairs,
        "complete_pairs": complete_pairs,
        "pairing_errors": pairing_errors,
        "by_format": by_format,
    }


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

    configured_formats = study.get("handoff_formats")
    if configured_formats is None:
        handoff_formats = (DEFAULT_HANDOFF_FORMAT,)
    elif (
        not isinstance(configured_formats, list)
        or not configured_formats
        or any(value not in HANDOFF_FORMATS for value in configured_formats)
        or len(set(configured_formats)) != len(configured_formats)
    ):
        raise ValueError("study handoff_formats must contain unique supported values")
    else:
        handoff_formats = tuple(configured_formats)

    expected = set(
        (
            case,
            band,
            condition,
            handoff_format,
            replicate,
        )
        for case, band, condition, replicate in itertools.product(
            axes["cases"],
            axes["bands"],
            axes["conditions"],
            range(1, replications + 1),
        )
        for handoff_format in (
            handoff_formats if condition == "handoff" else (DEFAULT_HANDOFF_FORMAT,)
        )
    )
    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("evaluation must contain a non-empty runs array")
    actual: set[tuple[str, str, str, str, int]] = set()
    arm_orders: dict[tuple[str, str, int], tuple[str, ...]] = {}
    arm_metadata_formats: dict[tuple[str, str, int], set[str]] = defaultdict(set)
    for run in runs:
        if not isinstance(run, dict):
            raise ValueError("run must be an object")
        for field in ("case", "band", "condition"):
            if not isinstance(run.get(field), str) or not run[field]:
                raise ValueError(f"run {field} must be a non-empty string")
        handoff_format = _handoff_format(run)
        replicate = run.get("replicate")
        if isinstance(replicate, bool) or not isinstance(replicate, int) or replicate < 1:
            raise ValueError("run replicate must be a positive integer")
        key = (run["case"], run["band"], run["condition"], handoff_format, replicate)
        if key in actual:
            raise ValueError(f"duplicate run: {key}")
        if key not in expected:
            raise ValueError(f"unexpected run: {key}")
        actual.add(key)
        has_arm_metadata = "arm_order" in run or "arm_position" in run
        if has_arm_metadata:
            if (
                run["condition"] != "handoff"
                or "arm_order" not in run
                or "arm_position" not in run
            ):
                raise ValueError("handoff arm metadata is incomplete")
            arm_order = run["arm_order"]
            if (
                not isinstance(arm_order, list)
                or len(arm_order) != len(handoff_formats)
                or any(value not in handoff_formats for value in arm_order)
                or len(set(arm_order)) != len(arm_order)
            ):
                raise ValueError("arm_order must be a permutation of study handoff formats")
            arm_position = run["arm_position"]
            if (
                isinstance(arm_position, bool)
                or not isinstance(arm_position, int)
                or arm_position < 1
                or arm_position > len(arm_order)
                or arm_order[arm_position - 1] != handoff_format
            ):
                raise ValueError("arm_position does not match handoff_format")
            arm_key = (run["case"], run["band"], replicate)
            previous = arm_orders.setdefault(arm_key, tuple(arm_order))
            if previous != tuple(arm_order):
                raise ValueError(f"handoff arm order differs within pair: {arm_key}")
            arm_metadata_formats[arm_key].add(handoff_format)
    for arm_key, formats in arm_metadata_formats.items():
        if formats != set(handoff_formats):
            raise ValueError(f"handoff arm metadata is incomplete: {arm_key}")
    missing = sorted(expected - actual)
    if missing:
        raise ValueError(f"missing runs: {missing[:5]}")


def validate_evaluation(payload: Any) -> None:
    validate_study_manifest(payload)
    for run in payload["runs"]:
        _validate_run(run)


def handoff_fidelity_gate(scored: list[dict[str, Any]]) -> dict[str, Any]:
    handoff = [
        row
        for row in scored
        if row.get("condition") == "handoff"
        and row.get("handoff_format", DEFAULT_HANDOFF_FORMAT) == DEFAULT_HANDOFF_FORMAT
    ]
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


def structured_state_gate(scored: list[dict[str, Any]]) -> dict[str, Any]:
    """Fail closed unless the complete paired candidate meets the gate."""
    state_runs = [
        row
        for row in scored
        if row.get("condition") == "handoff" and row.get("handoff_format") == "state-v1"
    ]
    failures: list[dict[str, Any]] = []
    if not state_runs:
        return {"passed": False, "failures": [{"metric": "state_v1_runs", "value": 0}]}
    paired = paired_handoff_summary(scored)
    expected_pairs = set(
        itertools.product(
            RELEASE_CASES,
            RELEASE_BANDS,
            range(1, MIN_CANDIDATE_REPLICATIONS + 1),
        )
    )
    complete_pair_keys = {
        (pair["case"], pair["band"], pair["replicate"])
        for pair in paired["pairs"]
        if "pairing_errors" not in pair
        and all(handoff_format in pair for handoff_format in HANDOFF_FORMATS)
    }
    thresholds = {
        "critical_rcr": 1.0,
        "incorrect_fact_rate": 0.0,
        "stale_context_intrusion": 0.0,
        "dod_pass_rate": 1.0,
        "task_success": True,
    }
    if missing_pairs := sorted(expected_pairs - complete_pair_keys):
        failures.append(
            {
                "metric": "complete_pairs",
                "value": len(complete_pair_keys),
                "expected": len(expected_pairs),
                "missing": missing_pairs[:5],
            }
        )
    for row in state_runs:
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


def release_gate(
    payload: dict[str, Any], scored: list[dict[str, Any]]
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    study = payload["study"]

    for field, required in (
        ("cases", RELEASE_CASES),
        ("bands", RELEASE_BANDS),
        ("conditions", RELEASE_CONDITIONS),
    ):
        actual = study[field]
        if not set(required).issubset(actual):
            failures.append(
                {"metric": f"study_{field}", "value": actual, "expected": list(required)}
            )
    if study["runs_per_condition"] < MIN_RELEASE_REPLICATIONS:
        failures.append(
            {
                "metric": "runs_per_condition",
                "value": study["runs_per_condition"],
                "expected": f">={MIN_RELEASE_REPLICATIONS}",
            }
        )

    judging = payload.get("judging")
    if not isinstance(judging, dict):
        judging = {}
    for field in (
        "condition_blind",
        "human_reviewed",
        "critical_disagreements_adjudicated",
    ):
        if judging.get(field) is not True:
            failures.append({"metric": field, "value": judging.get(field), "expected": True})
    for field in ("judge_id", "judge_model", "calibration_set"):
        if not isinstance(judging.get(field), str) or not judging[field]:
            failures.append(
                {"metric": field, "value": judging.get(field), "expected": "non-empty string"}
            )
    sample_size = judging.get("calibration_sample_size")
    if (
        isinstance(sample_size, bool)
        or not isinstance(sample_size, int)
        or sample_size < MIN_CALIBRATION_SAMPLE_SIZE
    ):
        failures.append(
            {
                "metric": "calibration_sample_size",
                "value": sample_size,
                "expected": f">={MIN_CALIBRATION_SAMPLE_SIZE}",
            }
        )
    agreement = judging.get("agreement")
    if (
        isinstance(agreement, bool)
        or not isinstance(agreement, (int, float))
        or not math.isfinite(agreement)
        or agreement < MIN_CALIBRATION_AGREEMENT
        or agreement > 1
    ):
        failures.append(
            {
                "metric": "calibration_agreement",
                "value": agreement,
                "expected": f"{MIN_CALIBRATION_AGREEMENT}..1.0",
            }
        )
    for field, required in (
        ("covered_cases", RELEASE_CASES),
        ("covered_bands", RELEASE_BANDS),
        ("covered_conditions", RELEASE_CONDITIONS),
    ):
        actual = judging.get(field)
        if (
            not isinstance(actual, list)
            or any(not isinstance(value, str) for value in actual)
            or not set(required).issubset(actual)
        ):
            failures.append(
                {
                    "metric": f"calibration_{field}",
                    "value": actual,
                    "expected": list(required),
                }
            )

    fidelity = handoff_fidelity_gate(scored)
    failures.extend(fidelity["failures"])
    for row in scored:
        if row["condition"] not in {"handoff", "migrate", "oracle"}:
            continue
        if row["condition"] == "handoff" and row.get(
            "handoff_format", DEFAULT_HANDOFF_FORMAT
        ) != DEFAULT_HANDOFF_FORMAT:
            continue
        for metric, expected in (("task_success", True), ("dod_pass_rate", 1.0)):
            if row[metric] != expected:
                failures.append(
                    {
                        "case": row["case"],
                        "band": row["band"],
                        "condition": row["condition"],
                        "replicate": row["replicate"],
                        "metric": metric,
                        "value": row[metric],
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
        "paired_handoff": paired_handoff_summary(scored),
        "handoff_fidelity_gate": handoff_fidelity_gate(scored),
        "structured_state_gate": structured_state_gate(scored),
        "release_gate": release_gate(payload, scored),
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
