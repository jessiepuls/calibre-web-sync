# calibre-web-sync

Syncs EPUBs from a local Calibre library to an existing Calibre-Web server.
Reads catalog snapshots from local and mounted server libraries, compares files
and metadata, uploads missing books over HTTP, and verifies uploaded contents.
Authentication supports a password prompt or a named 1Password CLI item.

Python 3.9+ on macOS/Linux. Application runtime uses the standard library.

## Structure

Application code lives in `calibre_web_sync/`:

- `cli.py`: arguments, configuration, and workflow orchestration.
- `library.py`: catalog snapshots, library identity, file fingerprints, and hashing.
- `planner.py`: missing-book, duplicate, and edition-review decisions.
- `client.py`: Calibre-Web login and upload requests.
- `engine.py`: verification of uploaded files.
- `cache.py`: digest reuse and cache-failure policy.
- `cache_store.py`: SQLite hash storage and cache-file lifecycle.

`sync.py` is the command wrapper. Tests live in `tests/`. Machine-specific settings
live in ignored `config.local.json`; `config.example.json` is the portable example.
Cache providers exist but are not yet wired into the comparison/CLI workflow.

## Readability

Aim for no more than three nested control-flow levels inside a function. Count
conditions, loops, try blocks, and context managers; exclude class/function
indentation. Treat exceeding three as a reason to reconsider the structure.

Prefer guard clauses and helpers that name coherent operations. Keep meaningful
transaction/resource scopes together. Avoid hiding complexity behind flags or
indirect calls merely to reduce indentation.

Use named records and explicit SQL columns/parameters where they improve clarity.
Do not add abstractions or compatibility machinery without a current use case.

## Validation

Run from the repository root:

```sh
python3 -m unittest discover -s tests -v
git diff --check
```

Test affected supported Python versions when available. Use temporary fixtures;
tests must not use real credentials, modify live libraries, or upload real books.
Library database access uses temporary read snapshots; uploads go through Calibre-Web.
Never print or commit credentials or machine-specific local configuration.

## Workflow

Keep changes focused and pause at agreed review/commit checkpoints. The user
handles commits unless delegated. Reserve `docs/` for maintained documentation.

For requested broad reviews, use
[review-changes](.agents/skills/review-changes/SKILL.md). Do not run the full review
workflow automatically for every small edit.
