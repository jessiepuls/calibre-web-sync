# Architecture

The command-line interface coordinates catalog comparison, authentication,
EPUB uploads, and verification. Library reads use temporary database snapshots;
uploads go through Calibre-Web's web interface.

```text
calibre_web_sync/
  library.py       Read catalog snapshots, normalize metadata, hash EPUBs
  planner.py       Classify missing books, identical files, and edition conflicts
  cache.py         Coordinate digest reuse, fingerprints, and cache fallback
  cache_store.py   Persist disposable hashes in a per-user SQLite database
  credentials.py   Retrieve the named 1Password item without persisting secrets
  client.py        Calibre-Web HTTP login, CSRF, uploads, response handling
  engine.py        Verify uploaded files through the mounted server library
  cli.py           Arguments, local config, authentication, progress, execution
  __main__.py      python3 -m calibre_web_sync entry point
sync.py            Compatibility command wrapper
config.example.json  Portable configuration example
config.local.json    Ignored machine-specific settings
tests/              Regression tests (no real library or network access)
```

## Comparison

`library.catalog` returns a complete `LibraryCatalog` containing library identity
and book dictionaries. The planner returns a list of book dictionaries with
digests, status, and comparison reasons. It compares EPUB contents, uses matching
title/author metadata to flag edition review, and identifies local duplicates.
Neither catalog discovery nor planning modifies either library.

## Digest caching

The CLI owns a digest provider for comparison and upload verification, and lends
it to the planner and verification function.
Storage is opened lazily and failures fall back to fresh hashing. `--no-cache`
uses the same provider without a persistent store. Cache keys include library
UUID, resolved root, relative path, and format; size and nanosecond modification
time determine reuse. Complete fresh catalogs authorize pruning stale EPUB
entries. Upload verification reads fresh bytes independently of cache hits and
seeds the provider only after the digest matches and the file fingerprint is
still unchanged. Disabled caching also disables persistence of verified hashes.

The CLI reports comparison statistics immediately after planning. Elapsed time
includes catalog snapshots, file checks, hashing, cache access, and pruning.
Hash counters describe completed fresh content reads by the comparison provider;
they exclude upload staging and verification. `tools/benchmark_cache.py` measures
these phases against disposable synthetic libraries; see [benchmarking](benchmarking.md).

## Uploads and verification

The CLI selects missing books and explicitly included review entries. For each
upload it stages a copy, freshly hashes it against the planned digest, and sends
it through the authenticated client. Upload requests are not automatically retried
because a failed response may follow a committed upload.

After each upload, `engine.verify_upload` uses `library.lookup_epub` to read the
returned server book ID from a fresh database snapshot. Only that book's EPUB is
resolved, checked, and hashed against the planned digest; unrelated book files
are not accessed. This partial lookup cannot authorize cache pruning. It retries
verification for delayed visibility and stops the run if verification fails.
The next upload starts only after the previous one is verified. A checkout lock
prevents overlapping runs from the same repository.
