"""Disposable digest providers; no catalog, credential, or CLI dependencies.

Both providers expose digest(book, force=False), statistics, and context-manager
cleanup. CachedDigestProvider opens storage lazily. A storage failure disables
persistence for that provider; actual ebook read failures still propagate.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3
import time
import warnings

from .library import FileDigest, file_fingerprint, stable_digest

APPLICATION_ID = 0x43575348  # CWSH: identifies files owned by this cache.
SCHEMA_VERSION = 1
DIGEST_VERSION = 1
ALGORITHM = 'sha256'
KEY_COLUMNS = ('library_uuid', 'library_root', 'relative_path', 'format')
COLUMNS = KEY_COLUMNS + ('size', 'mtime_ns', 'algorithm', 'digest_version',
                         'sha256', 'hashed_at_ns', 'last_seen_ns')
CREATE_DIGESTS = '''CREATE TABLE digests (
    library_uuid TEXT NOT NULL,
    library_root TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    format TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    algorithm TEXT NOT NULL,
    digest_version INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    hashed_at_ns INTEGER NOT NULL,
    last_seen_ns INTEGER NOT NULL,
    PRIMARY KEY (library_uuid, library_root, relative_path, format)
) WITHOUT ROWID'''
WHERE_KEY = 'library_uuid=? AND library_root=? AND relative_path=? AND format=?'


@dataclass
class CacheStats:
    persistent_hits: int = 0
    persistent_misses: int = 0
    ephemeral_hits: int = 0
    files_hashed: int = 0
    bytes_hashed: int = 0


def _warn(message):
    warnings.warn(message, RuntimeWarning, stacklevel=3)


class UncachedDigestProvider:
    """No persistent I/O; reuse stable results only for this provider's lifetime."""

    persistence_enabled = False

    def __init__(self):
        self.stats = CacheStats()
        self._memory = {}
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self._memory.clear()
        self._closed = True

    def _key(self, book):
        identity = book['library']
        root = identity.root.resolve()
        path = Path(book['path']).resolve()
        relative = path.relative_to(root)
        if relative != Path(book['relative_path']):
            raise ValueError('Book relative path does not match its library root')
        return (identity.uuid, str(root), relative.as_posix(), book['format'].upper())

    def _lookup(self, key, fingerprint):
        return None

    def _store(self, key, result):
        pass

    def digest(self, book, force=False):
        if self._closed:
            raise RuntimeError('Digest provider is closed')
        key = self._key(book)
        path = Path(book['path'])
        # Never use catalog attributes as a substitute for a current stat.
        fingerprint = file_fingerprint(path)
        memory = self._memory.get(key)
        if not force and memory and memory.fingerprint == fingerprint:
            self._ensure_unchanged(path, fingerprint)
            self.stats.ephemeral_hits += 1
            return memory.sha256
        if not force:
            cached = self._lookup(key, fingerprint)
            if cached is not None:
                self._ensure_unchanged(path, fingerprint)
                self.stats.persistent_hits += 1
                self._memory[key] = cached
                return cached.sha256
        # Deliberately outside all cache error handlers: bad/unstable ebooks stop sync.
        result = stable_digest(path)
        self.stats.files_hashed += 1
        self.stats.bytes_hashed += result.fingerprint.size
        self._store(key, result)
        self._memory[key] = result
        return result.sha256

    @staticmethod
    def _ensure_unchanged(path, fingerprint):
        if file_fingerprint(path) != fingerprint:
            raise RuntimeError(f'File changed while checking its hash: {path}. Retry when idle.')


class CachedDigestProvider(UncachedDigestProvider):
    """SQLite-backed, size/mtime-validated hashes; never expires entries by age.

    rehash bypasses persisted hashes but retains per-run reuse. force=True also
    bypasses per-run reuse. Neither option changes the library catalogs.
    """

    def __init__(self, cache_path, *, rehash=False, warn=None, timeout=0.1):
        super().__init__()
        self.cache_path = Path(cache_path).expanduser()
        self.rehash = rehash
        self.warn = warn if warn is not None else _warn
        self.timeout = timeout
        self.persistence_enabled = True
        self._connection = None
        self._missing_identity = set()

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        super().close()

    def _disable(self):
        self.persistence_enabled = False
        # Discard entries previously loaded from persistence as well.
        self._memory.clear()
        if self._connection is not None:
            try:
                self._connection.rollback()
            except sqlite3.Error:
                pass
            try:
                self._connection.close()
            except sqlite3.Error:
                pass
            self._connection = None
        self.warn(f'Hash cache unavailable at {self.cache_path}; continuing without caching. '
                  'The cache was not replaced. Remove an invalid cache file manually to rebuild it.')

    def _eligible(self, key):
        if not self.persistence_enabled:
            return False
        if key[0] is None:
            if key[1] not in self._missing_identity:
                self._missing_identity.add(key[1])
                self.warn(f'Library at {key[1]} has no usable UUID; persistent hash caching is disabled for this library.')
            return False
        return True

    @staticmethod
    def _schema(connection):
        app_id = connection.execute('PRAGMA application_id').fetchone()[0]
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        objects = connection.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        columns = tuple(row[1] for row in connection.execute('PRAGMA table_info(digests)'))
        return app_id, version, set(objects), columns

    def _prepare(self, connection, created):
        state = self._schema(connection)
        current = (APPLICATION_ID, SCHEMA_VERSION, {('table', 'digests')}, COLUMNS)
        if state == current:
            return
        empty = (0, 0, set(), ())
        if not (created and state == empty):
            raise sqlite3.DatabaseError('Unrecognized cache schema')
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            # Another process may have initialized while this one waited.
            state = self._schema(connection)
            if state == current:
                return
            if not (created and state == empty):
                raise sqlite3.DatabaseError('Cache schema changed during initialization')
            connection.execute(CREATE_DIGESTS)
            connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
            connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    def _open(self):
        if self._connection is not None:
            return True
        if not self.persistence_enabled:
            return False
        try:
            self.cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.cache_path.is_symlink():
                raise OSError('Cache path is a symlink')
            created = False
            try:
                fd = os.open(self.cache_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
                created = True
            connection = sqlite3.connect(self.cache_path, timeout=self.timeout)
            self._connection = connection
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise sqlite3.DatabaseError('Cache integrity check failed')
            self._prepare(connection, created)
            return True
        except (OSError, sqlite3.Error):
            self._disable()
            return False

    def _lookup(self, key, fingerprint):
        if not self._eligible(key):
            return None
        self.stats.persistent_misses += 1
        if not self._open() or self.rehash:
            return None
        try:
            row = self._connection.execute(
                'SELECT size,mtime_ns,algorithm,digest_version,sha256 FROM digests WHERE ' + WHERE_KEY,
                key).fetchone()
            if row is None or row[:4] != (fingerprint.size, fingerprint.mtime_ns, ALGORITHM, DIGEST_VERSION):
                return None
            sha = row[4]
            if not isinstance(sha, str) or re.fullmatch(r'[0-9a-f]{64}', sha) is None:
                raise sqlite3.DatabaseError('Invalid cached digest')
            with self._connection:
                self._connection.execute('UPDATE digests SET last_seen_ns=? WHERE ' + WHERE_KEY,
                                         (time.time_ns(),) + key)
            self.stats.persistent_misses -= 1
            return FileDigest(sha, fingerprint)
        except sqlite3.Error:
            self._disable()
            return None

    def _store(self, key, result):
        if not self._eligible(key) or not self._open():
            return
        now = time.time_ns()
        values = key + (result.fingerprint.size, result.fingerprint.mtime_ns,
                        ALGORITHM, DIGEST_VERSION, result.sha256, now, now)
        try:
            with self._connection:
                self._connection.execute(
                    'INSERT INTO digests '
                    '(library_uuid,library_root,relative_path,format,size,mtime_ns,'
                    'algorithm,digest_version,sha256,hashed_at_ns,last_seen_ns) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(library_uuid,library_root,relative_path,format) DO UPDATE SET '
                    'size=excluded.size,mtime_ns=excluded.mtime_ns,algorithm=excluded.algorithm,'
                    'digest_version=excluded.digest_version,sha256=excluded.sha256,'
                    'hashed_at_ns=excluded.hashed_at_ns,last_seen_ns=excluded.last_seen_ns', values)
        except sqlite3.Error:
            self._disable()
