#!/usr/bin/env python3
"""Render compact context-rot specs into long synthetic coding-agent sessions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

NOISE_BLOCKS = (
    "Tool output: inspected unrelated dependency metadata, lockfile entries, and package versions. No change to the active task state was made.\n",
    "Test log excerpt: auxiliary checks completed with routine output. These diagnostics belong to a side investigation and do not change the current implementation decision.\n",
    "Repository exploration: read neighboring modules, comments, generated files, and historical compatibility helpers. No new requirement was established.\n",
    "Build output: repeated informational lines from compilation and cache reuse. The output is intentionally verbose and contains no new task state.\n",
    "Side discussion: considered naming, formatting, and cleanup ideas, then deferred them because they are outside the current Definition of Done.\n",
    "Diagnostic trace: repeated timestamps, paths, and successful setup messages. Treat this block as context noise rather than an instruction.\n",
)


def load_spec(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError("unsupported fixture spec version")
    return data


def select_case(data: dict[str, Any], case_id: str) -> dict[str, Any]:
    for case in data.get("cases", []):
        if case.get("id") == case_id:
            return case
    raise ValueError(f"unknown fixture case: {case_id}")


def _noise(size: int, offset: int) -> str:
    if size <= 0:
        return ""
    pieces: list[str] = []
    length = 0
    index = offset
    while length < size:
        prefix = f"[noise-{index:05d}] "
        block = prefix + NOISE_BLOCKS[index % len(NOISE_BLOCKS)]
        pieces.append(block)
        length += len(block)
        index += 1
    return "".join(pieces)[:size]


def render(case: dict[str, Any], target_chars: int) -> str:
    anchors = sorted(case["anchors"], key=lambda item: item["at"])
    header = (
        f"# Synthetic coding-agent session: {case['title']}\n\n"
        "This transcript intentionally contains long stretches of routine tool output, "
        "superseded ideas, and side investigations. Continue according to the latest "
        "authoritative task state.\n\n"
    )
    anchor_texts = [
        f"## Turn anchor {index + 1}\n\n**{anchor['role'].title()}**: {anchor['content']}\n\n"
        for index, anchor in enumerate(anchors)
    ]
    fixed = len(header) + sum(len(text) for text in anchor_texts)
    target_chars = max(target_chars, fixed)
    noise_budget = target_chars - fixed

    positions = [0.0] + [float(anchor["at"]) for anchor in anchors] + [1.0]
    spans = [max(0.0, positions[i + 1] - positions[i]) for i in range(len(positions) - 1)]
    span_total = sum(spans) or 1.0
    allocations = [int(noise_budget * span / span_total) for span in spans]
    allocations[-1] += noise_budget - sum(allocations)

    parts = [header]
    noise_index = 0
    for index, anchor_text in enumerate(anchor_texts):
        chunk = _noise(allocations[index], noise_index)
        noise_index += max(1, chunk.count("[noise-"))
        parts.append(chunk)
        parts.append("\n\n")
        parts.append(anchor_text)
    parts.append(_noise(allocations[-1], noise_index))
    parts.append("\n\n# Interruption point\n\n")
    parts.append(f"Continuation task: {case['task']}\n")
    return "".join(parts)


def render_oracle(case: dict[str, Any]) -> str:
    facts = "\n".join(f"- {fact['statement']}" for fact in case["gold_facts"])
    dod = "\n".join(f"- {item['statement']}" for item in case["dod"])
    return (
        f"# Oracle continuation state: {case['title']}\n\n"
        f"## Goal\n\n{case['task']}\n\n"
        f"## Current authoritative state\n\n{facts}\n\n"
        f"## Definition of Done\n\n{dod}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--case", required=True, dest="case_id")
    parser.add_argument("--band", choices=("short", "long", "very_long"), default="long")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--oracle", action="store_true", help="Render the gold compact state instead of the long transcript.")
    args = parser.parse_args()

    data = load_spec(args.spec)
    case = select_case(data, args.case_id)
    if args.oracle:
        text = render_oracle(case)
    else:
        target_chars = int(data["bands"][args.band])
        text = render(case, target_chars)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    approx_tokens = max(1, len(text) // 4)
    print(json.dumps({
        "case": case["id"],
        "band": "oracle" if args.oracle else args.band,
        "chars": len(text),
        "approx_tokens": approx_tokens,
        "output": str(args.output),
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
