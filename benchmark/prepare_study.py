#!/usr/bin/env python3
"""Materialize context-rot transcripts, oracle states, and evaluation skeletons."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from fixture_workspace import materialize_workspace
from render_fixture import load_spec, render, render_oracle

CONDITIONS = ("full", "handoff", "migrate", "oracle")
HANDOFF_FORMATS = ("markdown-v1", "state-v1")
DEFAULT_HANDOFF_FORMAT = "markdown-v1"


def fixture_seed(case_id: str, band: str, replicate: int) -> str:
    return f"context-rot-v1:{case_id}:{band}:replicate-{replicate}"


def handoff_arm_order(case_id: str, band: str, replicate: int) -> tuple[str, ...]:
    formats = list(HANDOFF_FORMATS)
    random.Random(fixture_seed(case_id, band, replicate)).shuffle(formats)
    return tuple(formats)


def evaluation_run(
    case,
    band: str,
    condition: str,
    replicate: int,
    handoff_format: str = DEFAULT_HANDOFF_FORMAT,
) -> dict:
    if handoff_format not in HANDOFF_FORMATS:
        raise ValueError(f"unsupported handoff format: {handoff_format}")
    if condition != "handoff" and handoff_format != DEFAULT_HANDOFF_FORMAT:
        raise ValueError("state-v1 is only valid for the handoff condition")
    return {
        "case": case["id"],
        "band": band,
        "condition": condition,
        "handoff_format": handoff_format,
        "fixture_seed": fixture_seed(case["id"], band, replicate),
        "replicate": replicate,
        "facts": [
            {
                "id": item["id"],
                "weight": item.get("weight", 1),
                "critical": item["critical"],
                "statement": item["statement"],
                "status": None,
            }
            for item in case["gold_facts"]
        ],
        "stale_traps": [
            {
                "id": item["id"],
                "weight": item.get("weight", 1),
                "statement": item["statement"],
                "activated": None,
            }
            for item in case["stale_traps"]
        ],
        "dod": [
            {
                "id": item["id"],
                "weight": item.get("weight", 1),
                "statement": item["statement"],
                "passed": None,
            }
            for item in case["dod"]
        ],
        "task_success": None,
        "repeated_failed_attempts": None,
        "stale_decisions_acted_on": None,
        "recovery_reads": None,
        "input_tokens": None,
        "output_tokens": None,
        "wall_seconds": None,
        "supplied_context_bytes": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--output", type=Path, default=Path("benchmark/generated"))
    parser.add_argument("--runs-per-condition", type=int, default=2)
    parser.add_argument(
        "--handoff-only",
        action="store_true",
        help="prepare only the paired Markdown/state handoff candidate",
    )
    args = parser.parse_args()

    if args.runs_per_condition < 1:
        raise SystemExit("--runs-per-condition must be at least 1")

    data = load_spec(args.spec)
    conditions = ("handoff",) if args.handoff_only else CONDITIONS
    args.output.mkdir(parents=True, exist_ok=True)
    evaluation = {
        "schema_version": 1,
        "study": {
            "cases": [case["id"] for case in data["cases"]],
            "bands": list(data["bands"]),
            "conditions": list(conditions),
            "handoff_formats": list(HANDOFF_FORMATS),
            "runs_per_condition": args.runs_per_condition,
        },
        "runs": [],
    }

    for case in data["cases"]:
        case_dir = args.output / case["id"]
        case_dir.mkdir(parents=True, exist_ok=True)
        fixture = materialize_workspace(case["id"], case_dir / "workspace")
        (case_dir / "oracle.md").write_text(render_oracle(case), encoding="utf-8")
        (case_dir / "gold.json").write_text(
            json.dumps({
                "case": case["id"],
                "task": case["task"],
                "gold_facts": case["gold_facts"],
                "stale_traps": case["stale_traps"],
                "dod": case["dod"],
            }, indent=2),
            encoding="utf-8",
        )
        for band, target_chars in data["bands"].items():
            transcript = render(case, int(target_chars))
            (case_dir / f"session-{band}.md").write_text(transcript, encoding="utf-8")
            for condition in conditions:
                for replicate in range(1, args.runs_per_condition + 1):
                    formats = (
                        handoff_arm_order(case["id"], band, replicate)
                        if condition == "handoff"
                        else (DEFAULT_HANDOFF_FORMAT,)
                    )
                    for handoff_format in formats:
                        run = evaluation_run(
                            case,
                            band,
                            condition,
                            replicate,
                            handoff_format,
                        )
                        run["workspace_template"] = f"{case['id']}/workspace"
                        run["verify_command"] = fixture["verify_command"]
                        run["acceptance_command"] = fixture["acceptance_command"]
                        if condition == "handoff":
                            run["arm_order"] = list(formats)
                            run["arm_position"] = formats.index(handoff_format) + 1
                        evaluation["runs"].append(run)

    (args.output / "evaluation.json").write_text(
        json.dumps(evaluation, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "cases": len(data["cases"]),
        "bands": len(data["bands"]),
        "conditions": len(conditions),
        "runs_per_condition": args.runs_per_condition,
        "total_runs": len(evaluation["runs"]),
        "output": str(args.output),
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
