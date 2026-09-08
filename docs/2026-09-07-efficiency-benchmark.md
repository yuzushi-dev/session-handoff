# Offline efficiency: main versus candidate

2026-09-08 clarification: `correct` below means accepted by this run's limited
oracle. The subsequent Astra review found it did not establish exact search
matches or successful continuation. Timings and raw artifacts are preserved;
see the [corrected rerun](2026-09-08-efficiency-benchmark.md).

Primary pilot complete: 620 samples, 530 supported and correct, 90 explicitly unsupported on main, zero failures. Independent artifact verification passed. Ten samples per cell; approximately 16 min 14 s wall time, including 8 min of fixture preparation. Final-run one-minute load average ranged from 2.24 to 4.72.

Implementation: Luna (`gpt-5.6-luna`, reasoning `max`); independent review, corrective checks and execution by the parent agent. This benchmark does not call a model/provider. Product source was not changed.

## Findings

**No across-the-board efficiency improvement.** Central listing scales much better for large archives; central read/create have extra cost; content search remains expensive when it must scan a full page. The shared legacy read path is slower in the candidate in this sample.

### Shared operations: median milliseconds

| Documents | Operation | Main | Candidate |
|---:|---|---:|---:|
| 10 | Create one | 12.41 | 16.18 |
| 10 | Read one | 3.74 | 6.97 |
| 10 | List first page | 3.34 | 3.33 |
| 1,000 | Create one | 13.25 | 26.13 |
| 1,000 | Read one | 4.18 | 7.79 |
| 1,000 | List first page | 199.44 | 210.43 |
| 10,000 | Create one | 15.70 | 19.10 |
| 10,000 | Read one | 5.04 | 7.13 |
| 10,000 | List first page | 2,207.00 | 1,990.14 |

Median *paired* candidate/main read ratios are 1.84, 2.09 and 1.85 respectively. These are not ratios of the two marginal medians. Creation is noisy: for N=1,000, main ranges 5.01–131.33 ms and candidate 8.10–156.96 ms. Legacy listing scales roughly with N in both builds; no strong shared-list regression is established by this exploratory sample.

### Candidate central path: median milliseconds

| Documents | Create one | Read one | List first page |
|---:|---:|---:|---:|
| 10 | 51.46 | 15.36 | 87.54 |
| 1,000 | 60.21 | 15.38 | 143.56 |
| 10,000 | 49.72 | 17.17 | 180.51 |

At N=10,000, central first-page list ranges 131.85–213.28 ms versus candidate legacy 1,620.40–2,683.57 ms: about 11× lower median latency in this sample. It returns richer metadata and different storage guarantees, so this is a comparison of useful product paths, not identical implementations. For ten records, central listing is substantially more expensive than legacy listing.

Central read stays around 15–17 ms across these sizes, versus roughly 7 ms for candidate legacy read. Central create stays around 50–60 ms in median; durable storage and validation are not free.

### Search: median milliseconds and actual coverage

| Documents | Query | Workspace | Central | Files scanned: workspace / central |
|---:|---|---:|---:|---:|
| 1,000 | Absent | 1,598.55 | 3,455.64 | 256 / 256 |
| 1,000 | Common | 1,857.08 | 247.11 | 256 / 20 |
| 10,000 | Absent | 1,825.58 | 3,271.09 | 256 / 256 |
| 10,000 | Common | 1,732.14 | 272.18 | 256 / 20 |

Central common-query latency benefits from stopping after 20 matches; it does **not** prove a faster complete search. The absent central query costs roughly 3.3 seconds per 256-record page. The beyond-256 term is correctly absent from the first page, with incomplete coverage reported; this is not proof of archive-wide absence.

At N=10,000, central absent search reports 2 MiB of document payload but approximately 4.55 MiB median logical process I/O, versus 2.00 MiB for workspace search. Both have zero median storage-layer bytes read in this cache-uncontrolled run: that does not mean zero I/O work. Server-only user/system CPU medians are approximately 2,420/205 ms for central absent search and 1,685/40 ms for workspace search; Git child CPU is excluded.

Code inspection explains work worth profiling next: each central `read_record` resolves the Git project anchor, reads metadata/manifest and validates the document. Search invokes it for each scanned record. This is a hypothesis for optimization priorities, not a measured decomposition of latency. Contrary to an initial study inference, the search catalog page uses lightweight identities and does not first validate all documents via `_record_summary`.

Startup initialize medians: main 183.92 ms, candidate 215.16 ms, with overlapping ranges. Typical server peak RSS is approximately 26.6 MiB for main read and 28.2 MiB for candidate legacy read; N=10,000 list peaks are approximately 31.0/32.7 MiB, versus 29.0 MiB for central list. These are server peaks, not process-tree memory.

### Targeted byte-budget check

N=300, 64 KiB documents, three samples per cell: 18 supported cases correct, nine main search cases unsupported, zero failures. Absent/beyond queries stop after exactly 64 documents (4 MiB) and declare incomplete coverage. Central common queries stop after 20 matches. Independent artifact verification passed; these samples are not mixed into the 8 KiB pilot.

### Catalog and complete list check

N=1,000, three samples per cell, 12/12 correct. Median first-page request latency: healthy catalog 150.82 ms; absent catalog 187.32 ms; deliberately corrupt catalog/rebuild 251.93 ms. Healthy/absent/corrupt ranges overlap and three samples do not support tail or causal overhead claims.

Complete central listing validates all 1,000 distinct expected records across 50 pages. Median sum of request latencies: 6.683 s, range 6.627–8.007 s. This excludes between-request harness work and is not total elapsed walk time. Catalog tests are at N=1,000 only; recovery scaling at N=10,000 remains unmeasured.

Overall final evidence: **659 samples, 560 supported and correct, 99 expected unsupported, zero failures**, across the main pilot and two separately reported targeted runs.

## What is being measured

| Lane | Comparison | Meaning |
|---|---|---|
| Shared legacy create/read/list | Frozen main versus candidate | Same operation, document size and archive size |
| Candidate legacy/central create/read | Two storage paths | Different guarantees, not interchangeable implementations |
| Candidate search/central list | Absolute cost and coverage | Main search is explicitly unsupported |
| MCP startup | New process through initialize; initialized ping | Separate from the measured tool request |

Pilot: valid synthetic 8 KiB documents; N=10/1,000/10,000; ten samples per cell; list/search page limit 20. Each create adds one record to N; each read reads one known document. Create is independently reread outside its timing. Mutated fixtures are restored outside timing to keep N fixed; intact read-only fixtures are reused. Central fixture seeding uses the frozen build's `create_record` API, preserving insertion sequence and validation; measured operations use public stdio MCP.

Raw request latency includes transport, serialization and response parsing. The process is initialized and tools discovered before the measured request, but this is not a repeatedly warmed operation batch. Full-walk request sums, when reported, exclude harness validation and between-request measurement overhead; they are not end-to-end wall time.

## Limits and interpretation

- AB/BA arm ordering across samples; no randomized cell seed or multiple independent host blocks. Machine shared, four CPUs, varying external load; load is retained per sample. No outliers removed.
- Repository and fixtures use ext4 on the same filesystem. Copies warm the page cache; cache state is uncontrolled. No global cache drops, installs or kernel changes.
- Fixtures are nested under the working repository and inherit its Git common-directory anchor. Each fixture has isolated HOME/XDG and its own binding/catalog. This is a Git-workspace profile, not a plain-directory comparison; no user store is accessed.
- CPU counters are **server-only**, excluding Git descendants, with 10 ms tick resolution on this host. A zero delta is not zero work. RSS peak is server lifetime VmHWM, not allocation per request or simultaneous process-tree memory.
- Linux `/proc/PID/io` includes waited-for children. `rchar` is logical I/O, while `read_bytes` is storage-layer I/O; document `scanned_bytes` is a narrower product budget counter. See [proc_pid_io(5)](https://man7.org/linux/man-pages/man5/proc_pid_io.5.html). Child peak RSS is not a process-tree peak: [getrusage(2)](https://man7.org/linux/man-pages/man2/getrusage.2.html).
- Search limits remain 256 files, 4 MiB document payload and 64 KiB output. A bounded page is not full recall. In particular workspace search can report `has_more=false` while `scan_truncated=true`.
- No product latency/RAM SLO was supplied. Median, range and exploratory p95 describe this sample; no statistical superiority or release threshold is inferred.
- Not implemented in this pilot: multi-project scope matrix, complete central search walks, repeatedly warmed homogeneous request batches, concurrency and process-tree CPU measurement. These are not silently counted as passed.
- `response_utf8_bytes` is the compact reserialized JSON response size, not an exact wire-byte counter.

## Verification history

Parent review found and reproduced four initial defects: batch create/read instead of one operation, page size 100 instead of 20, and an absent-query false match accepted by the oracle. Four regression tests initially failed and passed after correction. Further independent checks reject missing common-query matches and empty list pages.

- Smoke 01: N=3, 17 supported cases passed.
- Smoke 02: correctly stopped on a benchmark oracle that incorrectly required insertion order after catalog rebuild; corrected to membership/count checks for rebuilt catalogs.
- Smoke 03: N=10, 46 samples passed, including startup, full list walk and catalog states.
- First N=1,000 preflight: stopped at 180 seconds during setup. An O(N²) disk-budget scan in the runner caused unnecessary preparation work; not a product latency result. Retained as a failed preflight.
- Second N=1,000 preflight: 17 supported cases passed, three main search cases unsupported. Single samples, not final performance estimates.
- Final full offline suite: **861 passed, two skipped**. Focused benchmark tests: **18 passed**. Ruff and diff-check passed. Frozen product hashes remained unchanged; final checkout runner source matches the measured frozen runner.
- After cache correction: 18 focused tests and a 52-sample smoke passed (46 supported, six unsupported). The final pilot's raw matrix, per-cell counts, build identities, search budget counters, medians, ranges and p95 were independently checked by `verify_results.py`.

## Reproducibility

Primary artifacts: `benchmark/results-version/2026-09-07-efficiency-pilot-v2/`: plan, raw JSONL, aggregate summary, independent verification script and frozen runner archive. Earlier smoke/preflight directories are retained separately and excluded from final statistics. The first pilot was interrupted by the reviewer after 47 samples because repeated whole-cache budget scans and unnecessary restores dominated harness runtime; its raw samples and interruption record are retained under `2026-09-07-efficiency-pilot/`.

Targeted artifacts: `benchmark/results-version/2026-09-07-efficiency-byte-budget/` and `benchmark/results-version/2026-09-07-efficiency-catalog/`, each with plan, raw samples, summary and independent `verification.json`. All three final runs use the same frozen runner source hash. Primary raw JSONL SHA256: `f9edb6f6eea6b6126b0e69c7bee710047377cd009bd43c2dc1bbc1cda42d4bf7`.

Frozen runner source SHA256: `9955a7305a3cd7e856f5d0237b52fb082f8c8d2e543a46a94ae3baa7190e34fc`.
Runner archive SHA256: `4eb643a4c5474a501932c7b29a5f9f0361728d47b9f20e03732f3396f1a90bce`.

Product source hashes:

- Main: `c8d8e7452662f922373dd181f37d7aa165e5719539dbf1e1ea71a71fe5079d71`.
- Candidate: `2da90673c5ef6f7fd09dfcdf2fa66ac411c314251aa557b58efd11c2001ce904`.

Products can be recovered from `benchmark/results-version/2026-09-07-main-candidate/inputs.tar.gz`. Network disabled with `bwrap --unshare-net`; isolated HOME/XDG, telemetry opt-out and bytecode disabled. Temporary synthetic fixtures are removed by the runner; raw evidence and user storage are retained. No new session handoff, commit, push, publish or deployment.
