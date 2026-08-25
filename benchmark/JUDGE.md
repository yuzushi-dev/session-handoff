# Context-Rot Benchmark Judge Rubric

Judge one run at a time. Do not infer missing facts from what a competent developer might do. Score only what the artifact or continuation demonstrates.

## Inputs

For Stage A you receive:

1. the generated handoff;
2. the fixture's `gold_facts` and `stale_traps`.

For Stage B you also receive:

3. the final repository diff or implementation result;
4. test evidence;
5. the fixture's Definition of Done;
6. an execution trace containing reads, edits, commands, and token counts when available.

The condition label (`full`, `handoff`, `migrate`, `oracle`) should be hidden from the judge when practical.

`run_study.py` writes `blinded/<blind-id>/judge.json` without the condition label. Give the judge that directory only. It contains the supplied context, continuation, normalized tool trace, workspace diff, and verification output. Keep `private/blind-map.json`, condition-named run directories, and `state.json` private until scoring.

## Gold-fact labels

For each gold fact choose exactly one label.

### `preserved`

The handoff contains the current fact with enough specificity for a new agent to act correctly. Paraphrase is acceptable. A file path, symbol, test, decision, constraint, failure reason, or next action must remain identifiable when the gold fact requires it.

### `missing`

The handoff omits the fact or reduces it to wording that cannot guide the next action. Do not mark a fact preserved because the continuation agent later rediscovered it from the repository.

### `incorrect`

The handoff states a conflicting value, treats stale state as current, reverses a constraint, or attributes completion/pending status incorrectly.

If the handoff contains both the current and stale fact without making the current one authoritative, label the fact `incorrect` and activate the matching stale trap.

## Stale-trap labels

Set `activated=true` when the handoff or continuation treats an obsolete decision, rejected approach, completed side task, or superseded fact as current enough to influence action.

Mentioning a stale fact as historical evidence does not activate the trap when the artifact clearly marks it obsolete or rejected.

## Continuation labels

For each Definition-of-Done item set `passed=true` only when the final implementation and evidence satisfy the criterion.

Set `task_success=true` only when every mandatory DoD item passes and no fixture constraint is violated.

Count `repeated_failed_attempts` when the continuation materially repeats an approach that the source session had already tried and rejected. Reading about the old attempt does not count.

Count `stale_decisions_acted_on` when the continuation edits code, tests, configuration, or its plan based on a superseded fact before correcting course.

Count `recovery_reads` when the continuation must inspect repository state to recover a gold fact missing from its supplied context. Normal verification reads after the agent already knows the fact do not count.

## Evidence and calibration

Every fact, stale-trap, DoD, and counter label needs concise evidence: an exact path or symbol, a short artifact excerpt, or a concrete action in the execution trace. `automated_pass` is test evidence, not permission to infer missing context recall.

Before using model judgments as release evidence, human-score a stratified calibration sample covering every case, condition, and size band. Record the judge identity/model, calibration-set identifier, sample size, agreement, and adjudicated disagreements. Set `human_reviewed=true` only after that comparison. Review every disagreement involving a critical fact; do not tune the fixture or handoff template against the held-out study cells.

Record the study-level result in the evaluation root:

```json
{
  "judging": {
    "condition_blind": true,
    "judge_id": "<reviewer-or-system-id>",
    "judge_model": "<exact-model-id>",
    "calibration_set": "<stable-set-id>",
    "calibration_sample_size": 18,
    "agreement": 0.8,
    "human_reviewed": true,
    "critical_disagreements_adjudicated": true,
    "covered_cases": ["<every release case>"],
    "covered_bands": ["short", "long", "very_long"],
    "covered_conditions": ["full", "handoff", "migrate", "oracle"]
  }
}
```

The release gate requires at least 18 calibration samples and agreement of at
least `0.8`; the reported coverage must include every required case, band, and
condition.

After judgment, save each completed `judge.json` in its original blinded
directory. Keep the private map hidden until all labels are final. Put the
study-level `judging` object above in a separate JSON file, then import the
complete set without overwriting the prepared evaluation:

```bash
python3 benchmark/import_judgments.py \
  benchmark/generated/evaluation.json \
  benchmark/results/<study> \
  --judging benchmark/generated/judging.json \
  --output benchmark/generated/evaluation.judged.json
```

The importer validates exact run coverage, blind/run mappings, unchanged IDs
and statements, non-empty evidence, automatic test results, counters, and
per-run calibration metadata. It derives final task success from automatic
acceptance plus judged DoD and writes a new mode-0600 file. The scorer
intentionally rejects remaining `null` labels or counters.

## Context-rot focus

The benchmark is intended to detect context rot, so give special care to:

- old facts repeated more often than their replacements;
- early constraints that remain active after many unrelated turns;
- failed fixes that still look locally plausible;
- partial work where completed and pending items sit close together;
- late corrections that conflict with older tool output;
- irrelevant side investigations that dominate transcript volume.

Do not reward a handoff for retaining these items unless they help establish current state. Compression is desirable when it removes them.
