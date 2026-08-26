#!/usr/bin/env python3
"""Validate blinded judgments and merge them into a new evaluation file."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path
from typing import Any

try:
    from .score import (
        COUNTER_FIELDS,
        FACT_STATUSES,
        validate_evaluation,
        validate_study_manifest,
    )
except ImportError:
    from score import (  # type: ignore[no-redef]
        COUNTER_FIELDS,
        FACT_STATUSES,
        validate_evaluation,
        validate_study_manifest,
    )


class JudgmentImportError(ValueError):
    """Judgment artifacts are incomplete or inconsistent."""


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JudgmentImportError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise JudgmentImportError(f"{label} must be an object")
    return payload


def _run_key(run: dict[str, Any]) -> tuple[str, str, str, int]:
    return (run["case"], run["band"], run["condition"], run["replicate"])


def _inside(root: Path, relative: Any, name: str, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise JudgmentImportError(f"{label} must be a non-empty string")
    root = root.resolve()
    path = (root / relative / name).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise JudgmentImportError(f"{label} resolves outside its result directory") from exc
    if not path.is_file():
        raise JudgmentImportError(f"{label} file is missing")
    return path


def _evidence(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JudgmentImportError(f"{label} requires non-empty evidence")
    return value


def _merge_items(
    target: list[dict[str, Any]],
    judged: Any,
    *,
    label: str,
    field: str,
    allowed: set[Any] | None = None,
) -> None:
    if not isinstance(judged, list) or len(judged) != len(target):
        raise JudgmentImportError(f"{label} items do not match the evaluation")
    if [item.get("id") for item in judged if isinstance(item, dict)] != [
        item["id"] for item in target
    ]:
        raise JudgmentImportError(f"{label} ids do not match the evaluation")
    for destination, source in zip(target, judged):
        if source.get("statement") != destination.get("statement"):
            raise JudgmentImportError(f"{label} statement changed: {destination['id']}")
        value = source.get(field)
        if allowed is not None and value not in allowed:
            raise JudgmentImportError(f"invalid {label} {field}: {destination['id']}")
        destination[field] = value
        destination["evidence"] = _evidence(
            source.get("evidence"), f"{label} {destination['id']}"
        )


def _merge_run(
    prepared: dict[str, Any], completed: dict[str, Any], judge: dict[str, Any]
) -> dict[str, Any]:
    if _run_key(completed) != _run_key(prepared):
        raise JudgmentImportError("evaluation-run selection does not match the study")
    merged = json.loads(json.dumps(prepared))
    _merge_items(
        merged["facts"],
        judge.get("facts"),
        label="fact",
        field="status",
        allowed=FACT_STATUSES,
    )
    _merge_items(
        merged["stale_traps"],
        judge.get("stale_traps"),
        label="stale trap",
        field="activated",
        allowed={True, False},
    )
    _merge_items(
        merged["dod"],
        judge.get("dod"),
        label="DoD",
        field="passed",
        allowed={True, False},
    )
    task_success = completed.get("task_success")
    if not isinstance(task_success, bool):
        raise JudgmentImportError("evaluation-run task_success must be boolean")
    if any(item.get("automated_pass") is not task_success for item in judge["dod"]):
        raise JudgmentImportError("judge automated_pass does not match evaluation-run")
    merged["task_success"] = task_success and all(item["passed"] for item in merged["dod"])
    for field in ("input_tokens", "output_tokens", "wall_seconds"):
        merged[field] = completed.get(field)

    counters = judge.get("counters")
    if not isinstance(counters, dict):
        raise JudgmentImportError("judge counters must be an object")
    _evidence(counters.get("evidence"), "judge counters")
    for field in COUNTER_FIELDS:
        value = counters.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise JudgmentImportError(
                f"judge counter must be a non-negative integer: {field}"
            )
        merged[field] = value
    return merged


def _validate_calibration(judge: dict[str, Any], judging: dict[str, Any]) -> None:
    calibration = judge.get("calibration")
    if not isinstance(calibration, dict) or calibration.get("rubric_version") != 1:
        raise JudgmentImportError("judge calibration must use rubric_version 1")
    for field in ("judge_id", "judge_model"):
        value = calibration.get(field)
        if not isinstance(value, str) or not value or value != judging.get(field):
            raise JudgmentImportError(
                f"judge calibration {field} does not match study metadata"
            )
    calibration_set = calibration.get("calibration_set")
    if calibration_set is not None and (
        not isinstance(calibration_set, str) or not calibration_set
    ):
        raise JudgmentImportError(
            "judge calibration_set must be null or a non-empty string"
        )
    if calibration_set != judging.get("calibration_set"):
        raise JudgmentImportError("judge calibration_set does not match study metadata")
    human_reviewed = calibration.get("human_reviewed")
    if not isinstance(human_reviewed, bool) or human_reviewed is not judging.get(
        "human_reviewed"
    ):
        raise JudgmentImportError("judge human_reviewed does not match study metadata")


def import_judgments(
    evaluation_path: Path,
    results_root: Path,
    judging_path: Path,
) -> dict[str, Any]:
    evaluation = _read_object(evaluation_path, "evaluation")
    validate_study_manifest(evaluation)
    judging = _read_object(judging_path, "judging metadata")
    mapping = _read_object(results_root / "private/blind-map.json", "blind map")
    prepared = {_run_key(run): run for run in evaluation["runs"]}
    merged: dict[tuple[str, str, str, int], dict[str, Any]] = {}

    for blind_id, entry in mapping.items():
        if not isinstance(entry, dict):
            raise JudgmentImportError("blind map entries must be objects")
        key = _run_key(entry)
        if key not in prepared or key in merged:
            raise JudgmentImportError("blind map contains an unexpected or duplicate run")
        judge = _read_object(
            _inside(results_root / "blinded", blind_id, "judge.json", "blind id"),
            "judge result",
        )
        if judge.get("schema_version") != 1 or judge.get("blind_id") != blind_id:
            raise JudgmentImportError("judge identity does not match the blind map")
        _validate_calibration(judge, judging)
        completed = _read_object(
            _inside(
                results_root,
                entry.get("run_id"),
                "evaluation-run.json",
                "run id",
            ),
            "evaluation run",
        )
        merged[key] = _merge_run(prepared[key], completed, judge)

    missing = set(prepared) - set(merged)
    if missing:
        raise JudgmentImportError(
            f"judgments are incomplete: {len(missing)} run(s) missing"
        )
    output = dict(evaluation)
    output["judging"] = judging
    output["runs"] = [merged[_run_key(run)] for run in evaluation["runs"]]
    validate_evaluation(output)
    return output


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise JudgmentImportError(f"output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise JudgmentImportError(f"output already exists: {path}") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evaluation", type=Path)
    parser.add_argument("results", type=Path)
    parser.add_argument("--judging", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = import_judgments(
            args.evaluation.resolve(), args.results.resolve(), args.judging.resolve()
        )
        output = args.output.expanduser().absolute()
        _write_private(output, payload)
    except (JudgmentImportError, KeyError, TypeError, ValueError) as exc:
        print(str(exc), file=os.sys.stderr)
        return 2
    print(json.dumps({"output": str(output), "runs": len(payload["runs"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
