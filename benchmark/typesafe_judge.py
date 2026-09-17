"""TypeSafe AI / Jev Benchmark Judge for context-rot evaluation.

Evaluates blinded run artifacts against the formal JUDGE.md rubric using
structured choice and noul questions, with robust word-boundary matching,
per-item DoD resolution, and trace extraction.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.typesafe_client import (
    Answer,
    ChoiceQuestion,
    NoulQuestion,
    Question,
    TypeSafeClient,
)

_REJECTION_RE = re.compile(
    r"\b(obsolete|rejected|superseded|deprecated|avoid|don't|do not|never|instead of)\b",
    re.IGNORECASE,
)
_FAILURE_RE = re.compile(
    r"\b(failed|failure|failing|error|exceptions?|traceback)\b",
    re.IGNORECASE,
)
_SUCCESS_RE = re.compile(
    r"\b(\d+ passed|success|ok|all tests passed)\b",
    re.IGNORECASE,
)


def _load_text(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _count_word_matches(tokens: list[str], text: str) -> int:
    """Count token matches respecting word boundaries."""
    count = 0
    for token in tokens:
        if re.search(rf"\b{re.escape(token)}\b", text, re.IGNORECASE):
            count += 1
    return count


def _offline_rubric_heuristic(
    state: dict[str, Any], questions: dict[str, Question]
) -> dict[str, Answer]:
    """Calibrated offline heuristic evaluator matching JUDGE.md rules."""
    context = state.get("supplied_context") or ""
    continuation = state.get("continuation") or ""
    diff = state.get("workspace_diff") or ""
    verify = state.get("verification") or ""
    acceptance = state.get("acceptance") or ""

    verify_passed = bool(_SUCCESS_RE.search(verify)) and not bool(re.search(r"\b\d+ failed\b", verify, re.IGNORECASE))
    acceptance_passed = bool(_SUCCESS_RE.search(acceptance)) and not bool(re.search(r"\b\d+ failed\b", acceptance, re.IGNORECASE))

    answers: dict[str, Answer] = {}

    for q_id, q in questions.items():
        # 1. Gold facts
        if q_id.startswith("fact_"):
            statement = state.get(f"{q_id}_statement", "")
            tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9_]{3,}", statement)]
            meaningful_tokens = [t for t in tokens if t not in {"the", "and", "must", "for", "with", "this"}]

            ctx_matches = _count_word_matches(meaningful_tokens, context)
            cont_matches = _count_word_matches(meaningful_tokens, continuation)
            max_matches = max(ctx_matches, cont_matches)
            total = max(1, len(meaningful_tokens))
            ratio = max_matches / total

            if ratio >= 0.50:
                answers[q_id] = Answer(
                    value="preserved",
                    confidence=min(0.98, 0.70 + (ratio * 0.28)),
                    probabilities={"preserved": ratio, "missing": (1.0 - ratio) * 0.7, "incorrect": (1.0 - ratio) * 0.3},
                )
            elif ratio >= 0.25:
                answers[q_id] = Answer(
                    value="missing",
                    confidence=0.75,
                    probabilities={"preserved": 0.25, "missing": 0.65, "incorrect": 0.10},
                )
            else:
                answers[q_id] = Answer(
                    value="missing",
                    confidence=0.92,
                    probabilities={"preserved": 0.05, "missing": 0.90, "incorrect": 0.05},
                )

        # 2. Stale traps
        elif q_id.startswith("trap_"):
            statement = state.get(f"{q_id}_statement", "")
            tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9_]{4,}", statement)]
            # Filter out common verbs
            distinctive_tokens = [t for t in tokens if t not in {"convert", "change", "make", "with", "from", "into"}]

            has_rejection = bool(_REJECTION_RE.search(continuation))
            matches = _count_word_matches(distinctive_tokens, continuation)
            total = max(1, len(distinctive_tokens))

            # Activated if all distinctive terms appear without an explicit rejection marker
            is_active = (matches == total) and not has_rejection
            val = 0.85 if is_active else 0.10
            answers[q_id] = Answer(
                value=val,
                confidence=0.88,
                probabilities={"true": val, "false": 1.0 - val},
            )

        # 3. Definition of Done (Per-Item Evaluation)
        elif q_id.startswith("dod_"):
            stmt = state.get(f"{q_id}_statement", "")
            automated = state.get("automated_pass", False)

            # Check if this specific item refers to a passing test
            test_terms = re.findall(r"test_[a-zA-Z0-9_]+|[A-Za-z0-9_]+\.py::[a-zA-Z0-9_]+", stmt)
            item_passed = True

            if test_terms:
                for t in test_terms:
                    # Test must appear in verify output and not be flagged as failed
                    if t.lower() in verify.lower():
                        if re.search(rf"\bFAILED\s+{re.escape(t)}\b", verify, re.IGNORECASE):
                            item_passed = False
                    else:
                        item_passed = False
            else:
                # General DoD item relies on verify + acceptance outcome
                item_passed = automated and (verify_passed or acceptance_passed)

            val = 0.95 if item_passed else 0.08
            answers[q_id] = Answer(
                value=val,
                confidence=0.90,
                probabilities={"true": val, "false": 1.0 - val},
            )

    return answers


def _extract_trace_counters(trace_path: Path) -> tuple[int, int]:
    """Extract repeated failed attempts and recovery reads from trace.json."""
    if not trace_path.is_file():
        return 0, 0
    try:
        data = json.loads(trace_path.read_text(encoding="utf-8"))
        events = data.get("events", data) if isinstance(data, dict) else data
        if not isinstance(events, list):
            return 0, 0

        failed_attempts = 0
        recovery_reads = 0
        last_error_cmd = ""

        for ev in events:
            if not isinstance(ev, dict):
                continue
            tool = ev.get("tool") or ev.get("name") or ""
            status = ev.get("status") or ev.get("exit_code")
            if tool in ("read", "file_read", "fetch", "sando_read"):
                recovery_reads += 1
            if status not in (0, "success", None):
                cmd = str(ev.get("input") or "")[:50]
                if cmd and cmd == last_error_cmd:
                    failed_attempts += 1
                last_error_cmd = cmd
            else:
                last_error_cmd = ""

        return failed_attempts, recovery_reads
    except Exception:
        return 0, 0


def judge_blind_run(
    blind_dir: Path, client: TypeSafeClient | None = None
) -> dict[str, Any]:
    """Evaluate one blinded run directory and update its judge.json."""
    judge_file = blind_dir / "judge.json"
    if not judge_file.is_file():
        raise FileNotFoundError(f"Missing judge.json in {blind_dir}")

    payload = json.loads(judge_file.read_text(encoding="utf-8"))
    client = client or TypeSafeClient()

    # Load artifacts
    supplied_context = _load_text(blind_dir / "supplied-context.md")
    continuation = _load_text(blind_dir / "continuation.txt")
    workspace_diff = _load_text(blind_dir / "workspace.diff")
    verification = _load_text(blind_dir / "verify.stdout")
    acceptance = _load_text(blind_dir / "acceptance.stdout")
    trace_path = blind_dir / "trace.json"

    automated_pass = all(d.get("automated_pass") is True for d in payload.get("dod", []))

    state: dict[str, Any] = {
        "blind_id": payload.get("blind_id"),
        "supplied_context": supplied_context[:8000],
        "continuation": continuation[:8000],
        "workspace_diff": workspace_diff[:4000],
        "verification": verification[:2000],
        "acceptance": acceptance[:2000],
        "automated_pass": automated_pass,
    }

    questions: dict[str, Question] = {}

    # 1. Gold facts questions
    for fact in payload.get("facts", []):
        fid = fact["id"]
        stmt = fact.get("statement", "")
        state[f"fact_{fid}_statement"] = stmt
        questions[f"fact_{fid}"] = ChoiceQuestion(
            instructions=f"Does the context or continuation preserve this fact: '{stmt}'?",
            criteria={
                "preserved": "Fact is accurately preserved with specific details",
                "missing": "Fact is omitted or too vague to act on",
                "incorrect": "Fact states conflicting, obsolete, or reversed information",
            },
        )

    # 2. Stale traps questions
    for trap in payload.get("stale_traps", []):
        tid = trap["id"]
        stmt = trap.get("statement", "")
        state[f"trap_{tid}_statement"] = stmt
        questions[f"trap_{tid}"] = NoulQuestion(
            instructions=f"Does the handoff or continuation treat this obsolete fact as active: '{stmt}'?"
        )

    # 3. DoD questions (evaluated per-item)
    for dod in payload.get("dod", []):
        did = dod["id"]
        stmt = dod.get("statement", "")
        state[f"dod_{did}_statement"] = stmt
        questions[f"dod_{did}"] = NoulQuestion(
            instructions=f"Is this definition of done satisfied in continuation/diff: '{stmt}'?"
        )

    # Evaluate via Jev (live or calibrated offline fallback)
    eval_result = client.evaluate(
        state, questions, offline_handler=_offline_rubric_heuristic
    )

    # Benchmark correctness: fail-closed if evaluation itself failed
    if not eval_result.ok:
        raise RuntimeError(f"TypeSafe benchmark evaluation failed: {eval_result.error}")

    # Update payload facts
    for fact in payload.get("facts", []):
        fid = fact["id"]
        ans = eval_result.answers.get(f"fact_{fid}")
        if ans:
            fact["status"] = ans.value
            fact["evidence"] = f"TypeSafe Jev conf={ans.confidence:.2f} ({eval_result.model})"

    # Update stale traps
    for trap in payload.get("stale_traps", []):
        tid = trap["id"]
        ans = eval_result.answers.get(f"trap_{tid}")
        if ans:
            trap["activated"] = float(ans.value) > 0.5
            trap["evidence"] = f"TypeSafe Jev p={ans.value:.2f} conf={ans.confidence:.2f}"

    # Update DoD
    for dod in payload.get("dod", []):
        did = dod["id"]
        ans = eval_result.answers.get(f"dod_{did}")
        if ans:
            dod["passed"] = float(ans.value) > 0.5
            dod["evidence"] = f"TypeSafe Jev p={ans.value:.2f} conf={ans.confidence:.2f}"

    # Extract real trace counters
    failed_attempts, recovery_reads = _extract_trace_counters(trace_path)
    counters = payload.setdefault("counters", {})
    counters["repeated_failed_attempts"] = failed_attempts
    counters["stale_decisions_acted_on"] = sum(1 for t in payload.get("stale_traps", []) if t.get("activated"))
    counters["recovery_reads"] = recovery_reads
    counters["evidence"] = f"Evaluation completed via {eval_result.model} in {eval_result.elapsed_ms:.1f}ms"

    # Calibration metadata
    calibration = payload.setdefault("calibration", {})
    calibration["rubric_version"] = 1
    calibration["judge_id"] = "typesafe-jev"
    calibration["judge_model"] = eval_result.model
    calibration["calibration_set"] = "context-rot-v1"
    calibration["human_reviewed"] = False

    judge_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate blinded run with TypeSafe Jev")
    parser.add_argument("blind_dir", type=Path, help="Directory containing blinded run artifacts")
    args = parser.parse_args()
    res = judge_blind_run(args.blind_dir)
    print(json.dumps(res, indent=2))
