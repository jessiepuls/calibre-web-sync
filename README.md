# calibre-web-sync

Sync books between a local Calibre library and Calibre-Web.

Currently supports incremental **local → server EPUB uploads**, content-based duplicate
detection, edition review, upload verification, and optional 1Password authentication.
Two-way sync and caching are planned.

Python 3.9+ on macOS/Linux. No installation or third-party Python packages required.
Calibre-Web must be running, and its book directory must be mounted locally for
comparison and verification. Quit desktop Calibre before syncing.

## Configure

From this repository:

```sh
cp config.example.json config.local.json
```

Edit `config.local.json` to set your library paths and 1Password item reference.
This file is ignored by Git. It contains references, not a password.
An existing local configuration has already been prepared for the original setup.

CLI flags override configuration. Use `--config PATH` for another configuration.
Without a configured local path, the current directory is used. A remote path is
required for comparisons and uploads.

## Run

```sh
# Preview; no login or uploads
python3 sync.py

# Test authentication only
python3 sync.py --check-login

# Upload and verify one book
python3 sync.py --apply --limit 1

# Upload remaining missing books
python3 sync.py --apply
```

`python3 -m calibre_web_sync` also works from the repository root. Existing commands
such as `python3 library-sync/sync.py` continue to work from the parent directory.

The comparison prints local IDs and titles:

- `MISSING`: selected for upload.
- `PRESENT`: identical EPUB bytes already exist on the server, even if titles differ.
- `REVIEW`: matching title and authors but different bytes; skipped by default.
- `DUPLICATE-LOCAL`: identical to another local file; skipped.

Explicitly include reviewed editions as separate server entries:

```sh
python3 sync.py --apply --include-review LOCAL_ID
```

Replace `LOCAL_ID` with an ID currently marked `REVIEW`. The script also uploads
ordinary missing books in that run. Use `--help` for all flags.

## Authentication

For 1Password, install `op`, unlock the desktop app, and enable
**Settings → Developer → Integrate with 1Password CLI**. The item should contain a
username, password, and website URL. With multiple URLs, designate a primary URL or
pass `--url` explicitly. Saved URLs ending in `/login` are handled automatically.

```sh
python3 sync.py --check-login --op-item 'Calibre-Web' --op-vault 'Private'
```

Add `--op-account ACCOUNT` for a particular 1Password account. Secret CLI output is
captured in memory, never printed or written to files. Dry runs do not read secrets.

Without 1Password, omit the `op_*` settings from your configuration and run:

```sh
python3 sync.py --check-login --url 'https://books.example.com' --username 'your-name'
```

The password is prompted privately. Standard Calibre-Web username/password login
is supported; OAuth-only and SSO gateway logins are not implemented.

In Calibre-Web, enable **Admin → Edit Basic Configuration → Feature Configuration →
Enable Uploads**, and grant the account **Upload** permission under **Admin → Users**.

## Current behavior

- Adds EPUBs through Calibre-Web's upload handler; does not write directly to either database.
- Keeps existing server entries, users, shelves, and reading status intact.
- Does not propagate local deletions, replace files, or update existing server metadata.
- Uploads the original EPUB bytes. Calibre-Web reads embedded metadata and covers;
  local database-only edits and separate cover files are not synchronized.
- Verifies uploaded EPUBs against the server file using SHA-256.
- Stops on failures without retrying a potentially committed upload. A fresh run
  checks server files again, allowing interrupted uploads to be resumed.
- Uses database snapshots for comparison and stops if a WAL/journal is present.
- Prevents concurrent runs from this checkout. Avoid other imports while syncing.
- Uses web form endpoints, not a versioned public API; compatibility may change
  with Calibre-Web upgrades.
- No persistent cache yet: comparison can take minutes on a slow share.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use temporary catalogs and an in-memory HTTP transport; no real credentials
or server access are needed. See [architecture and next steps](docs/architecture.md).

Implementation references:
[Calibre-Web upload handler](https://github.com/janeczku/calibre-web/blob/master/cps/editbooks.py),
[login handler](https://github.com/janeczku/calibre-web/blob/master/cps/web.py),
[1Password CLI](https://developer.1password.com/docs/cli/reference/management-commands/item/).
# calibre-web-sync
