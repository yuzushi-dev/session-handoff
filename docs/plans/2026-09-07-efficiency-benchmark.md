# Offline efficiency benchmark

Requested: implementation by Luna (`gpt-5.6-luna`, reasoning `max`), independent verification by the parent agent. No paid model calls or product changes.

Executed: see [the verified efficiency report](../2026-09-07-efficiency-benchmark.md).
Final evidence is 659 samples across the primary pilot and two targeted runs,
with 560 supported results correct and 99 expected unsupported cases. The
report explicitly lists matrix extensions that were not implemented or run.
The later review qualified `correct` as passing the original limited oracle,
not validating exact search matches or continuation; the corrected rerun is
documented in [the 2026-09-08 report](../2026-09-08-efficiency-benchmark.md).

## Comparisons

- Shared legacy create/read/list: frozen main versus candidate, identical inputs.
- Candidate legacy versus central create/read: different storage guarantees, not interchangeable implementations.
- Candidate-only central search/list/pagination/rebuild: characterize absolute cost; main is unsupported.

Start with tiny smoke tests, then 8 KiB documents at N=10, 1,000 and 10,000, ten samples per selected cell. Keep setup outside request timing and restore N before each create. Separate process startup, persistent requests, first page, complete walks and catalog rebuild. Target absent/common/beyond-256 search terms; a targeted larger-document case is needed to exercise the 4 MiB scan budget.

## Verification gates

1. Review protocol handling, isolation, fixture validity, constant-N create, timeout cleanup, source provenance and oracle failures.
2. Run tests and independent tiny smoke without networking.
3. Inspect raw results before expanding fixture size. Sequential execution, temporary owned fixtures, 4 GiB disk budget; no global cache manipulation.
4. Retain raw samples, failures and sample counts. Report median/range and exploratory p95, no p99 or invented acceptance threshold.
5. Distinguish logical I/O from physical storage I/O and request counters from total process work. Record machine load and uncontrolled page-cache state.

## Measurement caveats

Linux `/proc/PID/io` includes the process and its waited-for children. `rchar` measures logical reads; `read_bytes` measures storage-layer reads. Search `scanned_bytes` is a document-payload counter, not total process I/O (catalog, manifests, Git). Contrary to the initial study inference, frozen candidate `_catalog_page` uses lightweight identities for search, not `_record_summary`: there is no established double payload read at that stage. See [proc_pid_io(5)](https://man7.org/linux/man-pages/man5/proc_pid_io.5.html).

`RUSAGE_CHILDREN` covers terminated, waited-for children; its `ru_maxrss` is the largest individual child peak, not simultaneous process-tree peak. Block-operation counters are not bytes. See [getrusage(2)](https://man7.org/linux/man-pages/man2/getrusage.2.html).

Environment preflight: repository and `/tmp` share ext4 `/`; approximately 25 GiB free, load average about 6–7. `time` and `strace` available; `perf` counters denied by host policy, hyperfine absent. No installation or kernel setting changes. Tracing, if needed, is a separate diagnostic run, excluded from latency distributions.

## Frozen inputs

Root: `/tmp/session-handoff-offline-20260907-gCo0KR`.

- Main source SHA256: `c8d8e7452662f922373dd181f37d7aa165e5719539dbf1e1ea71a71fe5079d71`.
- Candidate source SHA256: `2da90673c5ef6f7fd09dfcdf2fa66ac411c314251aa557b58efd11c2001ce904`.

Implementation ownership: Luna owns `benchmark/efficiency.py`, `tests/test_efficiency.py` and the benchmark README section. Parent owns review, measured runs and findings. A passing correctness suite is not evidence of improved efficiency.
