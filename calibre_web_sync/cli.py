"""Command-line interface; no installation required."""
import argparse
import collections
import fcntl
import getpass
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import urllib.error

from .client import Client
from .credentials import onepassword_credentials
from .engine import verify_upload
from .library import catalog, digest
from .planner import make_plan

HERE = Path(__file__).resolve().parent.parent


def authenticated_client(args):
    if args.op_item:
        url, username, password = onepassword_credentials(
            args.op_item, args.op_vault, args.op_account, args.url, args.username)
    else:
        if not args.url or not args.username:
            raise RuntimeError('Provide --op-item, or both --url and --username')
        url, username = args.url, args.username
        password = getpass.getpass('Calibre-Web password: ')
    try:
        client = Client(url)
        client.login(username, password)
        return client
    finally:
        del password


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='Local JSON configuration file')
    parser.add_argument('--local', type=Path)
    parser.add_argument('--remote', type=Path)
    parser.add_argument('--url', help='Calibre-Web base URL, including any reverse-proxy path')
    parser.add_argument('--username')
    parser.add_argument('--op-item', help='1Password Login item name or ID')
    parser.add_argument('--op-vault', help='Vault containing the 1Password item')
    parser.add_argument('--op-account', help='1Password account, if needed')
    parser.add_argument('--check-login', action='store_true', help='Test authentication only; do not scan or upload')
    parser.add_argument('--apply', action='store_true', help='Upload missing books; default is dry-run')
    parser.add_argument('--include-review', type=int, nargs='+', default=[], metavar='LOCAL_ID', help='Explicitly upload these edition-review entries as separate books')
    parser.add_argument('--limit', type=int, help='Maximum number of uploads in this run')
    config_args, _ = parser.parse_known_args()
    config_path = config_args.config or HERE / 'config.local.json'
    if config_args.config or config_path.is_file():
        try:
            config = json.loads(config_path.read_text())
        except (OSError, ValueError):
            parser.error('Cannot read configuration JSON')
        allowed = {'local', 'remote', 'url', 'username', 'op_item', 'op_vault', 'op_account'}
        if not isinstance(config, dict) or set(config) - allowed:
            parser.error('Configuration contains unknown settings')
        if any(not isinstance(value, str) for value in config.values()):
            parser.error('Configuration values must be strings')
        parser.set_defaults(**config)
    args = parser.parse_args()
    args.local = Path(args.local) if args.local else Path.cwd()
    args.remote = Path(args.remote) if args.remote else None
    if not args.check_login and args.remote is None:
        parser.error('Provide --remote or set remote in config.local.json')
    if (args.op_vault or args.op_account) and not args.op_item:
        parser.error('--op-vault/--op-account require --op-item')
    if args.check_login and args.apply:
        parser.error('--check-login cannot be combined with --apply')
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    lock = (HERE / '.sync.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('Another sync is already running') from None
    try:
        if args.check_login:
            authenticated_client(args)
            print('Calibre-Web login verified. No books uploaded.')
            return
        local, remote = args.local.expanduser().resolve(), args.remote.expanduser().resolve()
        if local == remote:
            parser.error('Local and remote library must differ')
        print('Reading catalogs and comparing EPUBs (the shared folder must be mounted)...', flush=True)
        plan = make_plan(catalog(local), catalog(remote),
                         progress=lambda done, total: print(f'Comparing {done}/{total}...', flush=True))
        counts = collections.Counter(b['status'] for b in plan)
        print(' | '.join(f'{key}: {counts[key]}' for key in ('missing', 'present', 'review', 'duplicate-local')))
        allowed = set(args.include_review)
        valid = {b['id'] for b in plan if b['status'] == 'review'}
        if allowed - valid:
            parser.error(f'--include-review IDs must be current review entries: {sorted(allowed - valid)}')
        for book in plan:
            if book['status'] in ('missing', 'review'):
                print(f"{book['status'].upper():7} {book['id']:5}  {book['title']} — {', '.join(book['authors'])}")
        selected = [b for b in plan if b['status'] == 'missing' or (b['status'] == 'review' and b['id'] in allowed)]
        if args.limit:
            selected = selected[:args.limit]
        if not args.apply:
            print(f'Dry run only. {len(selected)} EPUBs selected; use --apply to upload.')
            return
        if not selected:
            print('Nothing to upload.')
            return
        client = authenticated_client(args)
        with tempfile.TemporaryDirectory(prefix='calibre-sync-upload-') as temporary:
            for number, book in enumerate(selected, 1):
                # Stage stable bytes and refuse changes made after planning.
                path = Path(temporary) / 'book.epub'
                shutil.copyfile(book['path'], path)
                if digest(path) != book['sha256']:
                    raise RuntimeError(f"Book {book['id']} changed since planning; rerun after closing Calibre.")
                print(f"Uploading {number}/{len(selected)}: {book['title']}", flush=True)
                # POST is never automatically retried: it might already have committed.
                server_id = client.upload(path)
                verify_upload(remote, server_id, book['sha256'])
                print(f'  Verified server book {server_id}', flush=True)
        print(f'Finished: uploaded and verified {len(selected)} EPUBs. Existing server entries were left unchanged.')
    finally:
        lock.close()


def run():
    """Process entry point with concise, credential-safe errors."""
    try:
        main()
    except (RuntimeError, OSError, sqlite3.Error, urllib.error.URLError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1
    return 0
