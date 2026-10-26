"""Read library snapshots and hash ebook files."""
import collections
import hashlib
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
import unicodedata


def normalize(value):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', value).casefold()))


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def snapshot(root, destination):
    """Copy a quiescent database; never open or write the mounted SQLite database."""
    source = root / 'metadata.db'
    if not source.is_file():
        raise RuntimeError(f'Library database not found: {source}')
    for attempt in range(3):
        # Do not silently ignore uncheckpointed transactions.
        if any((root / ('metadata.db' + suffix)).exists()
               for suffix in ('-wal', '-journal')):
            raise RuntimeError(f'{root}: database has a WAL/journal; wait for activity to finish or stop the app briefly.')
        before = source.stat()
        shutil.copyfile(source, destination)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
            connection = sqlite3.connect(destination)
            try:
                if connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok':
                    return
            finally:
                connection.close()
        time.sleep(0.5)
    raise RuntimeError(f'Database was changing while copied: {source}. Try again when idle.')


def catalog(root):
    with tempfile.TemporaryDirectory(prefix='calibre-sync-db-') as temporary:
        copied = Path(temporary) / 'metadata.db'
        snapshot(root, copied)
        c = sqlite3.connect(copied)
        try:
            authors = collections.defaultdict(list)
            for bid, name in c.execute('SELECT l.book,a.name FROM books_authors_link l JOIN authors a ON a.id=l.author ORDER BY l.id'):
                authors[bid].append(name.replace('|', ','))
            books = []
            for bid, title, folder, name in c.execute("SELECT b.id,b.title,b.path,d.name FROM books b JOIN data d ON d.book=b.id WHERE upper(d.format)='EPUB' ORDER BY b.id"):
                path = (root / folder / (name + '.epub')).resolve()
                if not path.is_relative_to(root.resolve()):
                    raise RuntimeError(f'Book {bid}: path escapes library')
                if not path.is_file():
                    raise RuntimeError(f'Book {bid}: missing EPUB: {path}')
                books.append({'id': bid, 'title': title, 'authors': authors[bid], 'path': path,
                              'size': path.stat().st_size,
                              'key': (normalize(title), tuple(sorted(normalize(a) for a in authors[bid])))})
            return books
        finally:
            c.close()
