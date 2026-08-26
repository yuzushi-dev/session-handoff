# Session Handoff

Create a clean implementation-state handoff, or move the active native session between Codex and Claude Code.

## Install

```bash
npx session-handoff@latest setup
```

The setup asks for confirmation, installs the MCP server and skill, and wraps
the client launchers. It supports Linux and macOS with Node.js 18+ and Python
3.10+.

Remove the setup with:

```bash
npx session-handoff@latest uninstall
```

Check both installed command surfaces without starting a model session or using
provider quota:

```bash
npx session-handoff@latest doctor --pretty
```

`ready: true` requires the managed launcher, installed skill, MCP registration,
client executable, and optional migration backend for all four documented flows.

### Telemetry

`npm install` asks once, interactively, whether to enable anonymous
aggregate telemetry — opt-in, off by default, no transcript/path/session
content ever collected. See [docs/telemetry.md](docs/telemetry.md).

## Clean handoff

Launch Codex or Claude normally after setup, then use:

- Codex: `$session-handoff`
- Claude: `/session-handoff`

The plugin writes a validated Markdown file under `handoffs/`, starts a fresh
session, and pre-fills the chat with `reference [handoffs/<name>.md] riparti da
qui`. Press Enter to send it when you are ready.

Use this mode when you want to leave transcript noise and stale context behind
while preserving the exact implementation state needed to continue.

## Native migration

The same skill can preserve the portable native conversation and switch harness:

```text
# From Claude Code
/session-handoff migrate codex

# From Codex
$session-handoff migrate claude
```

Migration is deliberately separate from a normal handoff. The managed launcher
stops the source client first, then delegates conversion to the external
`session-migrate` CLI. It runs a dry-run and apply with the same generated
target session ID and opens the target through its native resume mechanism.
If conversion fails after the source client is stopped, the launcher resumes
the original source session.

`session-migrate` is an optional backend and is not installed by
`session-handoff`. Install it separately when you want migrate mode. The current
upstream package supports Linux and Python 3.11+:

```bash
uv tool install session-migrate
```

Normal Markdown handoffs do not depend on `session-migrate` and keep working
when the backend is absent. Migrate mode currently exposes only Claude Code ↔
Codex even though the upstream migrator supports more formats. For current Codex
sessions using paginated history, `session-handoff` reads the canonical
`thread_history_1.sqlite` items and projects them into a private temporary view
before invoking the native Claude writer. It preserves messages, command
outputs and status, file diffs, web results, MCP calls and results,
collaboration/subagent state, plans, hook prompts, reviews, image references,
image-generation results, and compaction markers. Remote image URLs remain
image blocks; local media, audio, skill, and mention inputs become exact
references that the target can resolve in the same workspace. Private reasoning
and unknown item types are omitted and reported in `dropped_events`. The Codex
source is opened read-only and is never rewritten. The migration result also
exposes `context_loss.normalized_fields` for fields translated into the portable
tool representation. Claude→Codex output is normalized to the current native
Codex representation by removing an adjacent duplicate `event_msg` when the
same user message is already present as a `response_item`; the target manifest
record count and checksum are updated, and the normalization is reported.

## Safety

- Handoffs stay inside the workspace.
- Existing files are not overwritten by default.
- Common credentials are redacted before writing and reading handoff files.
- Native migration uses the exact active session ID; it never guesses from the newest transcript.
- The source client is stopped before its transcript is migrated.
- The source native session is left intact by `session-migrate`.
- Migration warnings and dropped/transformed event counters are printed before the target resumes.
- The MCP server has no third-party runtime dependencies.

The automatic switch and migration flows require the managed launcher. A client
started through a direct binary path uses the manual handoff fallback and does
not attempt to migrate a live transcript.

Anonymous telemetry is opt-in. After a recent operation, voluntary context
feedback may be recorded with `session-handoff telemetry report --category
constraint --severity recoverable`; categories and severities are fixed enums,
and `other` has no explanation field. Feedback contains no transcript, path,
session ID, or free text, and is stored locally until the normal aggregate
upload lifecycle.
See [docs/telemetry.md](docs/telemetry.md) for what's collected, and
`~/selfhosted/telemetry/docs/telemetry-privacy.md` (separate infra repo,
shared with Sando) for the full data inventory, retention, processing
boundary, and release gate.

## Context fidelity benchmark

The [benchmark protocol](benchmark/README.md) keeps two claims separate:

- semantic handoff must retain every critical active fact and exclude stale state;
- native migration must preserve supported events, leave the source unchanged, and report each omission or normalization.

The offline suite covers six context-rot cases at three transcript sizes,
runnable continuation workspaces, hidden semantic acceptance checks, both
native migration directions, strict release gates, and a fake-client pilot for
all four study conditions. The provider study remains opt-in because the
default matrix needs 180 execution calls before judging.

```bash
python3 benchmark/prepare_study.py \
  benchmark/fixtures/context_rot_cases.json \
  --output benchmark/generated \
  --runs-per-condition 2

python3 benchmark/run_study.py benchmark/generated/evaluation.json \
  --client codex \
  --model <exact-model-id> \
  --case superseded-decision \
  --band long \
  --condition handoff \
  --replicate 1
```

The second command prints a plan and makes no provider call unless you add both `--execute` and `--acknowledge-provider-cost`. Live runner execution currently requires Linux and Bubblewrap for repository-blind generation and isolated continuation. See the protocol for artifacts, blinded judging, calibration, and scoring.

## Development

```bash
pytest -q
claude plugin validate --strict .
python3 bin/session-handoff doctor --pretty
```
