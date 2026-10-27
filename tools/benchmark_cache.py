"""Run a disposable, no-network benchmark: python3 -m tools.benchmark_cache."""
from collections import Counter
from contextlib import closing
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from uuid import uuid4

from calibre_web_sync.cache import DigestProvider
from calibre_web_sync.cache_store import SQLiteDigestStore
from calibre_web_sync.library import catalog
from calibre_web_sync.planner import make_plan


BOOKS = 32
BYTES_PER_BOOK = 256 * 1024


def create_library(root):
    """Create minimal synthetic catalogs; the payloads exercise hashing, not parsing."""
    root.mkdir()
    with closing(sqlite3.connect(root / 'metadata.db')) as connection:
        connection.executescript('''
            CREATE TABLE library_id(uuid TEXT);
            CREATE TABLE books(id INTEGER PRIMARY KEY,title TEXT,path TEXT);
            CREATE TABLE authors(id INTEGER PRIMARY KEY,name TEXT);
            CREATE TABLE books_authors_link(id INTEGER PRIMARY KEY,book INTEGER,author INTEGER);
            CREATE TABLE data(book INTEGER,format TEXT,name TEXT);
            INSERT INTO authors VALUES(1,'Benchmark author');
        ''')
        connection.execute('INSERT INTO library_id VALUES(?)', (str(uuid4()),))
        for bid in range(1, BOOKS + 1):
            folder = root / str(bid)
            folder.mkdir()
            payload = bid.to_bytes(8, 'big') + b'x' * (BYTES_PER_BOOK - 8)
            (folder / 'book.epub').write_bytes(payload)
            connection.execute('INSERT INTO books VALUES(?,?,?)', (bid, f'Book {bid}', str(bid)))
            connection.execute("INSERT INTO data VALUES(?,'EPUB','book')", (bid,))
            connection.execute('INSERT INTO books_authors_link(book,author) VALUES(?,1)', (bid,))
        connection.commit()


def compare(local, remote, cache_path=None):
    store = SQLiteDigestStore(cache_path) if cache_path is not None else None
    with DigestProvider(store) as provider:
        started = time.perf_counter()
        plan = make_plan(catalog(local), catalog(remote), digest_provider=provider)
        result = {'elapsed_seconds': time.perf_counter() - started,
                  **asdict(provider.stats),
                  'decisions': dict(Counter(book['status'] for book in plan))}
    return plan, result


def benchmark(root):
    """Compare stable states and one ordinary edit; leave live libraries untouched."""
    local, remote = root / 'local', root / 'remote'
    create_library(local)
    create_library(remote)
    cache_path = root / 'cache' / 'hashes.sqlite3'
    uncached_plan, uncached = compare(local, remote)
    cold_plan, cold = compare(local, remote, cache_path)
    warm_plan, warm = compare(local, remote, cache_path)
    if not (uncached_plan == cold_plan == warm_plan):
        raise RuntimeError('Cached and uncached plans disagree')
    if warm['files_hashed'] or warm['bytes_hashed']:
        raise RuntimeError('Warm comparison unexpectedly hashed EPUB contents')

    changed = local / '1' / 'book.epub'
    attributes = changed.stat()
    with changed.open('r+b') as stream:
        stream.write(b'changed!')
    os.utime(changed, ns=(attributes.st_atime_ns, attributes.st_mtime_ns + 1000000000))
    changed_plan, changed_result = compare(local, remote, cache_path)
    reference_plan, reference = compare(local, remote)
    if changed_plan != reference_plan:
        raise RuntimeError('Cached and uncached plans disagree after the edit')
    if changed_result['files_hashed'] != 1:
        raise RuntimeError('Expected only the changed EPUB to be freshly hashed')
    return {'books_per_library': BOOKS, 'bytes_per_book': BYTES_PER_BOOK,
            'uncached': uncached, 'cold': cold, 'warm': warm,
            'one_file_changed': changed_result, 'changed_uncached': reference,
            'plans_agree': True}


def main():
    with tempfile.TemporaryDirectory(prefix='calibre-sync-benchmark-') as temporary:
        result = benchmark(Path(temporary))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
