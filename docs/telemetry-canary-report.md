# Telemetry canary report

Date: 2026-08-26

Status: local release gates passed; canary and public release are blocked.

## Evidence collected

| Gate | Result |
| --- | --- |
| Full pytest suite | 525 passed, 3 skipped |
| Python compileall (`server`) | passed |
| `git diff --check` | passed |
| `npm pack --dry-run` | passed; 18 files, no deploy/publish |
| Strict plugin validation | passed |
| Provider-free doctor | passed; 0 provider calls, all 4 flows ready |
| Shared backend static tests | passed; loopback integration skipped |

The skipped integration is deliberate: it would start containers and create or
remove Docker volumes. No packet capture, Collector ingest, Loki query,
dashboard total, owner enrollment, endpoint, hosting account, or public
deployment was used in this run.

## Release evidence still required

- owner-provided canary enrollment for seven days;
- preview bytes compared with packet capture, Collector rows, Loki labels and
  dashboard totals;
- disposable raw-sample and backup purge evidence;
- independent privacy/security review of the closed schemas and abuse limits;
- approved hosting target, operating budget and public endpoint controls.

## Decision

Do not publish, push, deploy, or spend. Ask the project owner to approve or
reject public hosting and release after reviewing the privacy contract,
threat model and these remaining gates.
