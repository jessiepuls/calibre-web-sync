# Architecture

Keep sync behavior independent of its user interface so a future Calibre plugin
can share the same comparison, transfer, and conflict logic as the CLI.

```text
calibre_web_sync/
  library.py       Read catalog snapshots, normalize metadata, hash EPUBs
  planner.py       Classify missing books, identical files, and edition conflicts
  credentials.py   Retrieve the named 1Password item without persisting secrets
  client.py        Calibre-Web HTTP login, CSRF, uploads, response handling
  engine.py        Verify uploaded files; home for future transfer orchestration
  cli.py           Arguments, local config, authentication, progress, execution
  __main__.py      python3 -m calibre_web_sync entry point
sync.py            Compatibility command wrapper
config.example.json  Portable configuration example
config.local.json    Ignored machine-specific settings
tests/              Regression tests (no real library or network access)
```

The catalog and plan currently use dictionaries. Keep these stable while extracting
shared execution logic from the CLI when a second interface needs it. Avoid
placeholder abstractions until there is working behavior to share.

## Next steps

1. Persistent hash cache outside the code tree, keyed by library identity, path,
   file size, and modification time. Re-read catalogs every run; invalidate changed
   files; offer a full rehash. Do not cache passwords, sessions, or conclusions that
   a server entry exists without checking the current catalog.
2. Verify just the uploaded book instead of rescanning the entire remote catalog.
3. Extract the upload loop from the CLI into the engine as shared execution logic.
4. Two-way transfer planning with explicit edition and metadata conflict policies.
5. Optional Calibre plugin UI calling the same core. No plugin stub or packaging
   is required yet.

No persistent cache or two-way sync is implemented in this initial version.
Choose a license before distributing the project publicly.
