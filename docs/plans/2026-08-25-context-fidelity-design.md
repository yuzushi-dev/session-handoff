# Context Fidelity Design

## Product contracts

`session-handoff` has two distinct continuity contracts.

A semantic handoff starts a clean session in the same harness. It must preserve every operationally critical fact: active constraints, authoritative decisions, rejected attempts, completed and pending work, exact paths or symbols, verification evidence, and the next safe action. It should omit stale and irrelevant transcript material. The release gate is therefore semantic, not byte based: critical required-context recall must be `1.0`, incorrect-fact rate `0`, and stale-context intrusion `0` on the declared benchmark corpus.

A native migration changes harness while preserving the portable conversation. It cannot truthfully promise byte-for-byte or private-reasoning preservation because Claude and Codex store different event types. Its contract is: preserve every supported user, assistant, and tool event; never mutate the source; classify every transformation or omission; and never silently discard an unknown event. The benchmark treats unreported loss as failure. Private reasoning remains excluded from portability claims.

These contracts stay separate. A migration does not masquerade as a clean semantic handoff, and a compact handoff does not claim to preserve a native transcript.

## Architecture and data flow

The installed skill remains the command surface: `$session-handoff` in Codex and `/session-handoff` in Claude. `handoff_create` validates, redacts, writes atomically, and asks the managed launcher for a fresh same-harness session. `handoff_migrate` sends an authenticated control request; the supervisor stops the source, invokes the internal migration engine, and resumes either the target or the original source after failure.

The fidelity benchmark has four deterministic layers around provider runs:

1. A strict study manifest defines cases, size bands, conditions, replications, gold facts, stale traps, and Definition of Done.
2. Runnable fixture workspaces make continuation outcomes testable instead of relying on prose-only judgment.
3. Scoring validates exact study coverage, types, identifiers, non-negative counters, and critical-fact gates before reporting metrics.
4. A fail-closed importer joins completed blinded judgments to run results only after judging, validates the private mapping and evidence, and writes a new evaluation without mutating either source.

Provider adapters are opt-in. They receive only generated synthetic transcripts and temporary workspaces by default. Model, client version, prompt, repository snapshot, and seed are recorded. Real historical transcripts require a separate explicit authorization and never enter the repository.

## Evaluation and failure handling

Stage A uses reference-based direct classification because every fact has objective ground truth. Judges must return structured labels plus short evidence. Critical facts are explicit in the fixture rather than inferred from weight. Human calibration samples remain required before treating automated judgments as release evidence.

Stage B evaluates repository end state with deterministic tests. Agent paths may differ; only the final state, constraint violations, stale actions, repeated failed attempts, recovery reads, tokens, and time are scored. Every condition runs from an independent copy of the same fixture snapshot.

Native migration is tested in both directions with the real installed writer and temporary homes. The suite inventories source and target semantic events, hashes every authoritative source file, checks the fixed target UUID, and compares dry-run with apply loss reports. Unsupported event classes are adversarial fixtures, not ignored noise.

The full live study remains gated because it consumes provider quota: six cases, three bands, four conditions, and two replications produce 144 continuation runs, plus handoff generation and judging. A small synthetic pilot validates the runner before that spend.
