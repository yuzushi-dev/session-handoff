#!/usr/bin/env python3
"""Materialize context-rot transcripts, oracle states, and evaluation skeletons."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from render_fixture import load_spec, render, render_oracle

CONDITIONS = ("full", "handoff", "migrate", "oracle")


def evaluation_run(case, band: str, condition: str, replicate: int) -> dict:
    return {
        "case": case["id"],
        "band": band,
        "condition": condition,
        "replicate": replicate,
        "facts": [
            {
                "id": item["id"],
                "weight": item.get("weight", 1),
                "critical": item["critical"],
                "status": None,
            }
            for item in case["gold_facts"]
        ],
        "stale_traps": [
            {"id": item["id"], "weight": item.get("weight", 1), "activated": None}
            for item in case["stale_traps"]
        ],
        "dod": [
            {"id": item["id"], "weight": item.get("weight", 1), "passed": None}
            for item in case["dod"]
        ],
        "task_success": None,
        "repeated_failed_attempts": 0,
        "stale_decisions_acted_on": 0,
        "recovery_reads": 0,
        "input_tokens": None,
        "output_tokens": None,
        "wall_seconds": None
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--output", type=Path, default=Path("benchmark/generated"))
    parser.add_argument("--runs-per-condition", type=int, default=2)
    args = parser.parse_args()

    if args.runs_per_condition < 1:
        raise SystemExit("--runs-per-condition must be at least 1")

    data = load_spec(args.spec)
    args.output.mkdir(parents=True, exist_ok=True)
    evaluation = {
        "schema_version": 1,
        "study": {
            "cases": [case["id"] for case in data["cases"]],
            "bands": list(data["bands"]),
            "conditions": list(CONDITIONS),
            "runs_per_condition": args.runs_per_condition,
        },
        "runs": [],
    }

    for case in data["cases"]:
        case_dir = args.output / case["id"]
        case_dir.mkdir(parents=True, exist_ok=True)
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
            for condition in CONDITIONS:
                for replicate in range(1, args.runs_per_condition + 1):
                    run = evaluation_run(case, band, condition, replicate)
                    evaluation["runs"].append(run)

    (args.output / "evaluation.json").write_text(
        json.dumps(evaluation, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "cases": len(data["cases"]),
        "bands": len(data["bands"]),
        "conditions": len(CONDITIONS),
        "runs_per_condition": args.runs_per_condition,
        "total_runs": len(evaluation["runs"]),
        "output": str(args.output),
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
