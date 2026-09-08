# Main/candidate offline comparison — 2026-09-07

Status: offline gate passed. The subsequently authorized Luna xhigh product
pilot is complete; see [the paid-pilot report](2026-09-07-luna-xhigh-product-pilot.md).

## Result

All 13 scenarios are valid on both inputs: 8 shared scenarios have identical
observations, and 5 additional scenarios pass on the candidate while main
reports unsupported. No unexplained regression was observed.

| Scenario group | Main | Candidate |
| --- | --- | --- |
| Manifests, hook/consent behavior, DNT, legacy create/read (8) | pass | same |
| Literal search | unsupported | pass |
| Central same-name immutable roundtrip | unsupported | pass |
| Central pagination and project/all scope | unsupported | pass |
| CLI list/read/doctor, no file/content/directory changes | unsupported | pass |
| Exact central resume draft | unsupported | pass |

Candidate suite on the frozen copy: **838 passed, 2 skipped**, 59.28 seconds.
The focused existing product suite also passed (299 passed, 1 skipped).
Runner failure-path tests passed (11 tests total in `test_version_aware.py`);
Ruff on the changed Python files and staged/unstaged diff checks passed.
The comparison and candidate suite ran with networking disabled. Provider
invocations: **0**; provider spend: **€0**.

The probe does not separately benchmark actionable cross-project CLI guidance,
rebind, import/export, or uninstall retention. Existing candidate tests cover
those paths; their test counts are not comparative quality evidence.

## Paid-pilot readiness

The user authorized the offline comparison and a paid pilot conditional on its
success. The proposed 24-invocation pilot does not test the new product flow:

- `benchmark/run_study.py` and the context-rot fixtures are unchanged from main.
- `HANDOFF_PROMPT` is fixed; `_prepare_context()` generates text and writes a
  local `handoff.md`. Continuation receives that text directly.
- The runner does not call central create/read, the CLI, the installed skill,
  or `session_switch.handoff_prompt()`.
- Its relevant product imports are redaction and heading validation. Redaction
  changed, but the synthetic fixtures do not contain credential-shaped values.
  This does not rule out differences on arbitrary model output; it means the
  proposed matrix has no controlled stimulus for the change.
- `_invoke_agent()` has no timeout or token/spend cap. The reported 24 calls
  mean CLI invocations, not a bound on underlying agent-loop model requests.

Therefore, a successful offline run cannot make this matrix evidence for
central-store or resume quality. No paid calls are being made. A meaningful
follow-up must exercise each build's actual create/read path and specify
bounded execution and operational outcomes before spending. This is a change
to the proposed study, not a withdrawal of the user's conditional approval.

## Verification notes

The first baseline test run used a Git archive: 696 passed, 8 failed, 2 skipped.
All eight failures were the same provenance assertion (`repository_sha256`
was null). The isolated baseline was given local Git metadata at the exact
baseline commit, without changing source content. Rerunning those eight tests
passed (8 passed, 34 deselected); the initial failures are retained here as
environmental setup evidence, not hidden or counted as product regressions.

## Reproduction

Artifact root: `/tmp/session-handoff-offline-20260907-gCo0KR`.
The baseline is `6bcc4e7bae4af4c7be2e42308f7afb9495f1b61f`; the candidate
includes the uncommitted changes based on
`b5bee57ec5b5063e7c68eabdf5ad2a2a1be9ea2f`.

Durable local artifacts (Git-ignored):
`benchmark/results-version/2026-09-07-main-candidate/comparison.json` and
`benchmark/results-version/2026-09-07-main-candidate/inputs.tar.gz`.
The archive preserves both input trees and their local Git metadata; no
provider credentials or real handoff store are included. It was frozen before
this report was updated with results.

| Identity | SHA-256 |
| --- | --- |
| Main indexed source tree | `c8d8e7452662f922373dd181f37d7aa165e5719539dbf1e1ea71a71fe5079d71` |
| Candidate indexed source tree | `2da90673c5ef6f7fd09dfcdf2fa66ac411c314251aa557b58efd11c2001ce904` |
| Input archive | `147af8caff075a88a7d3ea3cf50c71f684878d3cd9b1b31593df25af756483be` |
| Common runner | `ac0471573466ac2e0fd3fcb7314885e7412474d54e9039aed66d3aeb37f4817d` |
| Common product probe | `bc8ad800400a904bd2962b79b5e2dede44014a550493ffa44d4cd3bb6891e65f` |

```bash
bwrap --unshare-net --bind / / --dev /dev -- \
  python3 /tmp/session-handoff-offline-20260907-gCo0KR/candidate/benchmark/version_aware.py \
  --build main=/tmp/session-handoff-offline-20260907-gCo0KR/main \
  --build candidate=/tmp/session-handoff-offline-20260907-gCo0KR/candidate \
  --output /tmp/session-handoff-offline-20260907-gCo0KR/comparison.json
```

The comparison is executed inside `bwrap --unshare-net --bind / /` so network
access is disabled. Each scenario uses a temporary home and workspace.
Synthetic fixture records do not touch the user's central store.

These checks establish operational behavior on Linux, not model quality,
statistical superiority, macOS support, or a real supervised client switch.

## Concrete pilot adaptation

Keep 3 cases × 2 bands × 2 builds × 1 Markdown format × 1 replication,
with generation and continuation (24 harness invocations, no judging calls).
Use `buried-constraint`, `superseded-decision`, and `failed-attempt-trap`,
at `short` and `long`, alternating which build runs first across paired cells.

Between generation and continuation, invoke the selected frozen build's real
stdio MCP server in a fresh home/workspace:

- Main: `handoff_create(workspace, path="handoffs/pilot.md", content)`,
  then `handoff_read(workspace, path="handoffs/pilot.md")`.
- Candidate: `handoff_create(workspace, name="pilot.md", content)`,
  then `handoff_read(workspace, ref=<returned ref>)`.
- Do not request a supervised switch. Persist create/read responses and
  content hashes in the run artifacts. Validate the returned content before
  allowing continuation, and feed continuation only that read result.
- Fail a cell on transport, validation, roundtrip, or hidden-acceptance errors;
  stop the study on setup or cost-control failures; do not retry paid calls.

This would be a live end-to-end regression smoke, not a measure of central
storage superiority. An agent actually discovering and using the resume tool
requires a different study with the plugin exposed inside its sandbox.
At the end of the offline stage, the adapted runner had not been implemented
and a budget question remained open. The user subsequently authorized necessary
spend with Luna xhigh. The adapter and finite invocation timeout were implemented,
verified offline, and the fixed 24-invocation pilot completed; details and limits
are recorded in the linked paid-pilot report.
