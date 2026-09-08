# Efficiency benchmark rerun — 2026-09-08

Primary matrix complete and independently verified: 620 samples, 530 supported
results accepted, 90 expected unsupported searches on main, zero failures.
Supplementary probes also passed: 27 byte-budget samples (18 supported,
9 expected unsupported), 12 catalog/list-walk samples and 3 search-walk samples.
Total measured evidence excluding smoke: 662 samples, 563 accepted supported
results, 99 expected unsupported, zero failures.

The requested rerun compares the original frozen main with the updated candidate.
Keep the [previous measurements](2026-09-07-efficiency-benchmark.md) intact.
Historical before/after timings are descriptive, not a controlled estimate of
the effect of one fix: the runner and host conditions also change.

## Results

The updated candidate shows lower central-search latency and logical I/O than
the previous candidate run. It is not an across-the-board efficiency win.
All figures below are medians; primary cells have ten samples each.

### Candidate at N=10,000: historical comparison

| Operation / measurement | Previous candidate | Updated candidate |
|---|---:|---:|
| Central absent search, 256 documents | 3,271.09 ms | 2,062.01 ms |
| Central common search, 20 documents | 272.18 ms | 151.84 ms |
| Central first-page list | 180.51 ms | 157.95 ms |
| Central create one record | 49.72 ms | 54.22 ms |
| Central read one record | 17.17 ms | 18.55 ms |
| Central absent search: logical reads (`rchar`) | 4.55 MiB | 2.17 MiB |
| Central absent search: server user CPU | 2,420 ms | 1,865 ms |
| Central absent search: server peak RSS | 29.11 MiB | 32.96 MiB |

Absent/common central search latency is descriptively about 37%/44% lower;
logical reads are about 52% lower. Both absent-search runs inspect the same
256 documents / 2 MiB payload. Their observed latency ranges are
2,915–3,895 ms before versus 1,584–2,771 ms now. This remains an exploratory
historical comparison, not randomized evidence isolating the fix.

At N=10,000 the updated central first-page list is about 13.3× lower latency
than the updated legacy path (158 ms versus 2,098 ms). The two paths return
different metadata and provide different storage guarantees.

### Shared legacy operations in this run

| Documents | Operation | Main ms | Updated candidate ms |
|---:|---|---:|---:|
| 10 | Create one | 23.73 | 19.75 |
| 10 | Read one | 10.36 | 7.84 |
| 10 | List first page | 3.47 | 3.10 |
| 1,000 | Create one | 18.91 | 16.64 |
| 1,000 | Read one | 20.47 | 7.49 |
| 1,000 | List first page | 184.92 | 205.37 |
| 10,000 | Create one | 21.18 | 26.32 |
| 10,000 | Read one | 11.38 | 6.47 |
| 10,000 | List first page | 2,165.05 | 2,098.38 |

Median paired candidate/main read ratios are 0.79, 0.50 and 0.70, but their
ranges all cross 1 and main read timings changed markedly from yesterday.
Do not infer a reliable general read speedup from these noisy ten-pair cells.
Ratios of marginal medians are not medians of paired ratios.

Startup medians: main 198.05 ms, candidate 281.00 ms, with ranges
157–288 / 245–486 ms. Candidate central create/read remain around
50–54 / 14–19 ms. Memory and startup show no demonstrated improvement.

Primary wall time: 1,155.22 seconds (~19m15s), including 601.94 seconds of
fixture preparation/restoration, versus 974.50 / 479.81 seconds previously.
Host one-minute load ranged 1.99–7.67 on four CPUs (previously 2.24–4.72).
No outliers were removed. Total runner wall time is not a product latency metric.

The raw first-page searches still have bounded coverage: at large N, absence
means absent in the scanned page, not in the archive. Successful continuation
is established separately by the N=300 full-walk probe, not assumed from a cursor.

### Supplementary probes and unresolved performance cost

- Byte budget: N=300, 64 KiB documents, three samples per search cell. Absent
  and beyond-256 queries scan exactly 64 documents / 4 MiB. All 18 supported
  samples pass; main's nine searches remain unsupported.
- Central search walk: N=300, three samples, two requests each. The first page
  scans 256 records without finding the target; the second scans 44 and returns
  exactly the target at index 256, with the expected line/snippet. Median sum
  of request latencies 2.981 seconds (range 2.869–4.704), not end-to-end wall time.
- Central list walk: N=1,000, all 50 pages / 1,000 distinct records verified
  in each of three samples. Median request-latency sum **11.908 seconds**, range
  11.289–12.579, versus 6.683 seconds previously: an observed 78% worsening.
  Server user CPU also rises from 6.24 to 9.46 seconds; logical reads are
  identical at 10,682,523 bytes. This is not explained merely by extra I/O.
- Catalog first-page medians: healthy 249.46 ms, absent 406.91 ms, rebuild
  385.28 ms, versus 150.82 / 187.32 / 251.93 ms previously. Three samples each;
  ranges are available in the raw/summary artifacts.

The catalog probe's one-minute load was 3.61–6.15, versus 1.66–1.94 previously;
both used Python 3.12.3. The higher CPU time and latency deserve a controlled
old-candidate/new-candidate profile of list validation before calling the
optimization broadly successful. No causal attribution or further product fix
is established by these measurements. The correctness gate is green; the
remaining performance question is explicitly open.

## Evidence and validation

Results live under `benchmark/results-version/`:

| Directory suffix (prefix `2026-09-08-efficiency-`) | Samples | Raw SHA256 |
|---|---:|---|
| `primary` | 620 | `5a07d49957ce4ac2f7da5ceb844f5aa5c044733fe132f8235e8b6280bf31e628` |
| `byte-budget` | 27 | `19c3d8a94dd3edd832977efba420756115f279ee74004b30f5b2971d571ab85b` |
| `catalog` | 12 | `b54d80995a20fb254e18df9766a30e977f702cc403e6a842961c4a6a0acc17c1` |
| `search-walk` | 3 | `1ca50c5c4ec3473b2eb767d80787402fa82ae26dc3a26ab566449835fbb64054` |

Each has plan/raw/summary/verification artifacts. The independent verifier in
`2026-09-08-efficiency-inputs/verify_results.py` checks pinned build/runner hashes,
complete cell/sample sets, status expectations, scan counters, exact fixture
lines/snippets, per-page target coverage, request counts, median/range/p95.
It does not import product or runner code. Central reference membership and
cursor semantics are checked against live fixtures by the runner: raw evidence
preserves matches and coverage, not full MCP responses or cursor payloads.

The 22-sample smoke is retained separately and excluded from the 662 total.
The failed search-walk CLI attempt exited before fixture/product execution;
it produced no measured sample and triggered runner-v2 rather than overwriting
the original runner. No outliers, failed samples, or unsupported cases were
silently discarded from a measured run.

Final parent verification: **879 passed, 2 skipped** with networking disabled;
Ruff and `git diff --check` pass. Frozen source hashes are unchanged after the
runs, and live server/runner sources match the measured copies. Temporary
synthetic fixtures were automatically removed; evidence and source archives
remain. No install, provider benchmark call, commit, push, publish, deployment,
or new session handoff was performed.

## Gate

Luna max implemented the fixes identified by Astra max: redaction backtracking
and malformed quotes, dirty-project recovery before rebinding, a bindings-write
size bound, request-local search context, stronger search validation, and
process-group cleanup. The parent independently ran the full suite offline:
875 passed, 2 skipped before the follow-up corrections below.

The targeted follow-up review found three remaining blockers:

- Cleanup treated a zombie process as live, adding about 1.5 seconds per sample.
- Unquoted assignment redaction regressed for backticks and opening brackets.
- Search validation accepted a target outside the scanned page and mislabeled
  coverage on the next page.

The three corrections passed the parent's fresh offline suite (878 passed,
2 skipped) and Astra's targeted real-process/redaction/pagination checks.
A 22-cell MCP smoke passed (19 supported, 3 expected unsupported).
The opt-in search-walk CLI then exposed a missing scenario registration.
The runner-only correction passed 24 parent benchmark tests and the real
N=300 search walk (3/3, two pages each), including independent artifact checks.
The original smoke used runner v1; all subsequent runs use runner v2.
The original benchmark's 560 `ok` samples passed its limited oracle; this is not
proof of exact search snippets or successful search continuation.

## Rerun scope

Same primary matrix: N=10/1,000/10,000, 8 KiB, ten samples per cell, shared
legacy operations, candidate storage comparison/search/central list, startup.
Repeat the 64 KiB byte-budget and N=1,000 catalog/list-walk probes. Add an
N=300 central search walk crossing the 256-record boundary.

Use public stdio MCP with isolated HOME/XDG and networking disabled. Preserve
the Git-workspace profile, ABBA order, uncontrolled OS cache, server-only CPU
and RSS caveats, and setup outside request timing. No paid model workload,
installation, publication, or new session handoff is part of this rerun.

## Frozen provenance

- Main: original `/tmp/session-handoff-offline-20260907-gCo0KR/main`, source
  SHA256 `c8d8e7452662f922373dd181f37d7aa165e5719539dbf1e1ea71a71fe5079d71`.
- Updated candidate: `/tmp/session-handoff-efficiency-20260908-Hy4eEq/candidate`,
  source SHA256 `c9d2c80b9b26b019cd7a1c4ea89fcdac626850f3075f801670cba9d7cf4c17b8`.
- Common runner: `/tmp/session-handoff-efficiency-20260908-Hy4eEq/runner-v2`,
  `benchmark/efficiency.py` SHA256
  `670e8ed22af9928e8678a369617ce9e2df58c19760df582f85dc3a35cfae586e`.
- Durable inputs: `benchmark/results-version/2026-09-08-efficiency-inputs/`.
  Candidate archive SHA256
  `fdb1aad42f865ad0c1e4347e4cf4cb51c63844f5864c45f7f7370bbaf6fa1f15`;
  runner-v2 archive SHA256
  `80ca05f65c6f378438cb852ee7ea0a4950b913997a5a7da2315a44d5097998bf`.
  Main remains recoverable from the original 2026-09-07 inputs archive.

The original checkout index was not changed. Untracked source was included in
the disposable candidate index before hashing. Runner-v2 differs from that
candidate only in runner CLI wiring and its regression test, not product code.
