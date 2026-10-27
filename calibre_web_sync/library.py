"""Read library snapshots and hash ebook files."""
import collections
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import unicodedata
from typing import Optional
from uuid import UUID


@dataclass(frozen=True)
class LibraryIdentity:
    uuid: Optional[str]
    root: Path

    @property
    def namespace(self):
        """Missing UUIDs explicitly disable future persistent-cache reuse."""
        return (self.uuid, str(self.root)) if self.uuid else None


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class FileDigest:
    sha256: str
    fingerprint: FileFingerprint


@dataclass(frozen=True)
class LibraryCatalog:
    """A completed EPUB scan, retaining identity even when no books remain."""

    library: LibraryIdentity
    books: tuple

    def __iter__(self):
        return iter(self.books)

    def __len__(self):
        return len(self.books)

    def __getitem__(self, index):
        return self.books[index]


def library_identity(connection, root):
    """Read identity from the catalog snapshot without inventing a fallback UUID."""
    root = Path(root).resolve()
    columns = {row[1] for row in connection.execute('PRAGMA table_info(library_id)')}
    if 'uuid' not in columns:
        return LibraryIdentity(None, root)
    rows = connection.execute('SELECT uuid FROM library_id LIMIT 2').fetchall()
    if len(rows) != 1:
        return LibraryIdentity(None, root)
    try:
        value = str(UUID(rows[0][0]))
    except (ValueError, TypeError, AttributeError):
        value = None
    return LibraryIdentity(value, root)


def file_fingerprint(path):
    """Use current filesystem attributes, never the catalog's stored file size."""
    return _fingerprint(Path(path).stat())


def _fingerprint(attributes):
    return FileFingerprint(attributes.st_size, attributes.st_mtime_ns)


def _read_state(attributes):
    # Inode/ctime catch replacements and edits during a read. They are not part
    # of the persistent-cache trust policy, which remains size plus mtime.
    return (attributes.st_dev, attributes.st_ino, attributes.st_size,
            attributes.st_mtime_ns, attributes.st_ctime_ns)


def normalize(value):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', value).casefold()))


def digest(path):
    """Compatibility helper for callers that only need a verified SHA-256."""
    return stable_digest(path).sha256


def stable_digest(path):
    """Return a digest with the attributes of the stable file actually read."""
    path = Path(path)
    before = path.stat()
    changed = RuntimeError(f'File changed while hashing: {path}. Close other writers and retry.')
    h = hashlib.sha256()
    with path.open('rb') as stream:
        if _read_state(before) != _read_state(os.fstat(stream.fileno())):
            raise changed
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
        after_read = os.fstat(stream.fileno())
        try:
            after_path = path.stat()
        except FileNotFoundError:
            raise changed from None
        if (_read_state(before) != _read_state(after_read)
                or _read_state(before) != _read_state(after_path)):
            raise changed
    return FileDigest(h.hexdigest(), _fingerprint(after_read))


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
    """Return a complete EPUB snapshot, or raise without returning a partial scan."""
    root = Path(root).resolve()
    with tempfile.TemporaryDirectory(prefix='calibre-sync-db-') as temporary:
        copied = Path(temporary) / 'metadata.db'
        snapshot(root, copied)
        c = sqlite3.connect(copied)
        try:
            identity = library_identity(c, root)
            authors = collections.defaultdict(list)
            for bid, name in c.execute('SELECT l.book,a.name FROM books_authors_link l JOIN authors a ON a.id=l.author ORDER BY l.id'):
                authors[bid].append(name.replace('|', ','))
            books = []
            for bid, title, folder, name in c.execute("SELECT b.id,b.title,b.path,d.name FROM books b JOIN data d ON d.book=b.id WHERE upper(d.format)='EPUB' ORDER BY b.id"):
                try:
                    books.append(_epub_book(identity, (bid, title, folder, name), authors[bid]))
                except FileNotFoundError as error:
                    raise RuntimeError(f'Book {bid}: missing EPUB: {error.filename}') from None
            return LibraryCatalog(identity, tuple(books))
        finally:
            c.close()


def _epub_book(identity, row, authors):
    """Resolve one EPUB with the same containment and file checks for every caller."""
    bid, title, folder, name = row
    root = identity.root
    path = (root / folder / (name + '.epub')).resolve()
    if not path.is_relative_to(root):
        raise RuntimeError(f'Book {bid}: path escapes library')
    attributes = path.stat()
    if not stat.S_ISREG(attributes.st_mode):
        raise RuntimeError(f'Book {bid}: missing EPUB: {path}')
    return {'id': bid, 'title': title, 'authors': authors, 'path': path,
            'library': identity, 'relative_path': path.relative_to(root), 'format': 'EPUB',
            'size': attributes.st_size, 'mtime_ns': attributes.st_mtime_ns,
            'key': (normalize(title), tuple(sorted(normalize(a) for a in authors)))}


def lookup_epub(root, book_id):
    """Read one book from a fresh snapshot; absent rows/files are not yet visible.

    This is not a complete catalog and cannot authorize cache pruning. Other
    library, permission, and containment errors propagate.
    """
    root = Path(root).resolve()
    with tempfile.TemporaryDirectory(prefix='calibre-sync-db-') as temporary:
        copied = Path(temporary) / 'metadata.db'
        snapshot(root, copied)
        connection = sqlite3.connect(copied)
        try:
            row = connection.execute(
                "SELECT b.id,b.title,b.path,d.name FROM books b JOIN data d ON d.book=b.id "
                "WHERE b.id=? AND upper(d.format)='EPUB'", (book_id,)).fetchone()
            if row is None:
                return None
            identity = library_identity(connection, root)
            authors = [name.replace('|', ',') for (name,) in connection.execute(
                'SELECT a.name FROM books_authors_link l JOIN authors a ON a.id=l.author '
                'WHERE l.book=? ORDER BY l.id', (book_id,))]
            return _epub_book(identity, row, authors)
        except FileNotFoundError:
            return None
        finally:
            connection.close()
