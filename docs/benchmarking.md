# Measuring comparison caching

Run the disposable fixture benchmark from the repository root:

```sh
python3 -m tools.benchmark_cache
```

It creates two temporary libraries with 32 synthetic EPUB payloads of 256 KiB
each and a separate temporary cache. It runs uncached, cold-cache, and warm-cache
comparisons, then changes one local payload and compares again with and without
caching. It checks that plans agree for each state, warm hashing reads are zero,
and only the changed file is rehashed. All fixtures are removed on exit. It does
not use local configuration, credentials, network access, or actual book libraries.
The payloads exercise catalog comparison and hashing rather than EPUB parsing.

The JSON output includes timing, hash counters, and decision counts for every run.
Timings are observations, not pass/fail thresholds. Small files on fast local disks
can make cache bookkeeping slower than fresh hashing. This benchmark demonstrates
content-read savings; it does not simulate SMB latency or prove a speedup there.

## Measure mounted libraries

Keep both libraries idle and the server share mounted throughout the measurements.
Use ordinary preview runs with a new, disposable cache directory:

```sh
benchmark_dir=$(mktemp -d)
python3 sync.py --no-cache
python3 sync.py --cache --cache-dir "$benchmark_dir/cache"
python3 sync.py --cache --cache-dir "$benchmark_dir/cache"
```

These commands use the configured libraries and make no uploads. The second run
has a cold hash cache, and the third has a warm one. Compare the decision counts
and hash counters as well as elapsed time. Keep measurements and machine details
in your own scratch space. Remove the disposable directory when finished.
Use the synthetic benchmark for the changed-file experiment; do not edit actual
books for a performance test.

## Interpret the output

- **Persistent hits:** valid stored digests reused on first access in this run.
- **Misses:** persistent reuse attempts that needed a fresh hash, including
  forced rehashes. Disabled caching and libraries without usable UUIDs do not
  count persistent misses.
- **In-run reuse:** repeated requests served from memory after file checks.
  This also works with `--no-cache`.
- **Hashed files/bytes:** completed fresh EPUB content reads during comparison.
  They exclude metadata database reads, file attribute checks, staging, and upload
  verification. They are not physical disk or network byte counters; OS caching
  may satisfy reads locally.
- **Elapsed time:** catalog discovery through completed planning and cache pruning.
  It excludes authentication, uploads, staging, and verification.

The cache mode reports `disabled` for `--no-cache` or configuration disabling it,
and `unavailable` after a cache failure causes fallback. Missing UUIDs produce a
separate warning for the affected library. Review warnings alongside the summary.

Warm comparisons still copy/check database snapshots and resolve/stat book files.
On SMB, these operations and filesystem attribute caching can remain substantial.
The OS file cache is not cleared between benchmark runs. Repeat measurements when
assessing timing, and avoid attributing all differences to digest caching.
