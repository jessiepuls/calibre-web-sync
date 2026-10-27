# calibre-web-sync

Sync books between a local Calibre library and Calibre-Web.

Currently supports incremental **local → server EPUB uploads**, content-based duplicate
detection, edition review, upload verification, and optional 1Password authentication.
Comparison hashes are cached between runs.

Python 3.9+ on macOS/Linux. No installation or third-party Python packages required.
Calibre-Web must be running, and its book directory must be mounted locally for
comparison and verification. Quit desktop Calibre before syncing.

## Configure

From this repository:

```sh
cp config.example.json config.local.json
```

Edit `config.local.json` to set your library paths, Calibre-Web URL, and username.
The example uses a private password prompt. To save a 1Password item reference
instead, follow [Authentication](#authentication). Your chosen method is saved in
the configuration, so ordinary runs need no authentication arguments. This file
is ignored by Git; leave passwords out of it.

CLI flags override configuration. Use `--config PATH` for another configuration.
Without a configured local path, the current directory is used. A remote path is
required for comparisons and uploads.

## Run

```sh
# Preview; no login or uploads (may populate the hash cache)
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

## Hash caching

Caching is enabled by default. SHA-256 hashes are reused while EPUB size and
modification time match, with no automatic expiry. Catalogs are read again every
run. The first run must calculate any needed hashes that are not already cached.

Size and modification time are a practical trust signal, not proof that contents
are unchanged. Timestamp-preserving edits can remain cached, and network shares
may report attributes with limited precision or delay. Use `--rehash` to refresh
hashes needed by the comparison, or `--no-cache` to compare using fresh hashes
without persistent storage. Server candidates are hashed only when their sizes
match a local EPUB, so neither option hashes every server EPUB.

```sh
python3 sync.py --no-cache             # No persistent cache reads or writes
python3 sync.py --rehash               # Refresh hashes needed for this comparison
python3 sync.py --cache-dir /path/to/cache
```

Set `"cache": false` in the JSON configuration to disable caching by default.
For example, a configuration without authentication settings can contain:

```json
{
  "local": "/path/to/local/Calibre Library",
  "remote": "/path/to/mounted/server/books",
  "cache": false
}
```

With that configuration:

```sh
python3 sync.py                       # Compare without persistent caching
python3 sync.py --cache               # Enable caching for this run
python3 sync.py --cache --rehash       # Enable caching and refresh needed hashes
```

`--cache` and `--no-cache` override configuration and cannot be combined.
`--rehash` requires enabled caching. Setting `cache_dir`
(a JSON string) or `--cache-dir` changes placement without enabling caching.

The database is `hashes.sqlite3` beneath the selected cache directory:

- macOS: `~/Library/Caches/calibre-web-sync/`
- Linux: `$XDG_CACHE_HOME/calibre-web-sync/`, or `~/.cache/calibre-web-sync/`
  when the variable is unset or is not an absolute path.

Preview runs may write the cache; `--check-login` never opens it. Cache failures
such as corruption, unsupported schemas, lock contention, or unwritable storage
produce one warning and comparison continues with fresh hashes. Unrecognized or
damaged databases are left intact. Errors reading the actual libraries still stop
the run. For a busy cache, wait for the other run to finish; for a damaged cache,
stop all sync runs using it, remove `hashes.sqlite3` from the selected cache
directory, and run again to rebuild it.

The cache is disposable acceleration state; it is not sync history and does not
record deletions or prove that a server book still exists. Missing library UUIDs
disable persistent caching for that library. In-run hash reuse works with caching
enabled or disabled.
Each upload is verified immediately from fresh server bytes before the next
upload starts; only a successful verification seeds the enabled cache.

Each comparison reports elapsed time, persistent cache hits/misses, in-run hash
reuse, and files/bytes freshly hashed. These numbers exclude upload staging and
verification. Catalog snapshots and file attribute checks still run on warm
comparisons. See [benchmarking](docs/benchmarking.md) for reproducible measurements.

## Authentication

Choose a login method in `config.local.json`:

- **Password prompt:** set `url` and `username`, and omit the `op_*` settings.
- **1Password:** set `op_item` and optionally `op_vault` and `op_account`.
  The item supplies the URL, username, and password.

The presence of `op_item` selects 1Password. Otherwise, the script prompts for
your password. Once configured, either method uses the same commands:

```sh
python3 sync.py --check-login          # Test the configured login method
python3 sync.py --apply                # Upload using the configured login method
```

Preview runs (`python3 sync.py`) do not authenticate. Both methods use standard
Calibre-Web username/password login; OAuth-only and SSO gateway logins are not
implemented.

### Password prompt

Keep these authentication fields alongside your library paths in the configuration:

```json
{
  "url": "https://books.example.com",
  "username": "your-name"
}
```

When switching from 1Password, remove `op_item`, `op_vault`, and `op_account`.
The script prompts privately when checking login or uploading; it does not save
the password in configuration.

### 1Password

Install `op`, unlock the desktop app, and enable
**Settings → Developer → Integrate with 1Password CLI**. The item should contain a
username, password, and website URL. With multiple URLs, designate a primary URL or
pass `--url` explicitly. Saved URLs ending in `/login` are handled automatically.

Replace the `url` and `username` authentication fields with:

```json
{
  "op_item": "Calibre-Web",
  "op_vault": "Private"
}
```

Keep your library paths and other settings. Add `op_account` if needed to select
a particular 1Password account. With this configuration, `python3 sync.py --apply`
uses 1Password automatically; no `--op-item` argument is needed.

### Command-line overrides

CLI arguments override their matching configuration fields for one run:

```sh
python3 sync.py --check-login --url 'https://books.example.com' --username 'your-name'
python3 sync.py --check-login --op-item 'Another login' --op-vault 'Private'
```

Setting `url` or `username` in configuration or on the CLI overrides that value
from the 1Password item; it does not switch off 1Password when `op_item` is set.
1Password CLI output is captured in memory, never printed or written to files.

In Calibre-Web, enable **Admin → Edit Basic Configuration → Feature Configuration →
Enable Uploads**, and grant the account **Upload** permission under **Admin → Users**.

## Current behavior

- Adds EPUBs through Calibre-Web's upload handler; does not write directly to either database.
- Keeps existing server entries, users, shelves, and reading status intact.
- Does not propagate local deletions, replace files, or update existing server metadata.
- Uploads the original EPUB bytes. Calibre-Web reads embedded metadata and covers;
  local database-only edits and separate cover files are not synchronized.
- Freshly verifies each uploaded EPUB using SHA-256 and a lookup of the returned
  server book ID, then seeds its verified hash into the enabled cache.
- Stops on failures without retrying a potentially committed upload. A fresh run
  checks server files again, allowing interrupted uploads to be resumed.
- Uses database snapshots for comparison and stops if a WAL/journal is present.
- Prevents concurrent runs from this checkout. Avoid other imports while syncing.
- Uses web form endpoints, not a versioned public API; compatibility may change
  with Calibre-Web upgrades.
- Warm comparisons avoid rereading unchanged EPUB contents; catalog and file
  attribute checks still require access to the shared folder.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use temporary catalogs and an in-memory HTTP transport; no real credentials
or server access are needed. See [architecture](docs/architecture.md).

Implementation references:
[Calibre-Web upload handler](https://github.com/janeczku/calibre-web/blob/master/cps/editbooks.py),
[login handler](https://github.com/janeczku/calibre-web/blob/master/cps/web.py),
[1Password CLI](https://developer.1password.com/docs/cli/reference/management-commands/item/).
