"""SQLite persistence for named digest entries, independent of file hashing."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import errno
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Optional

from .library import FileDigest, FileFingerprint

APPLICATION_ID = 0x43575348  # CWSH: identifies files owned by this cache.
SCHEMA_VERSION = 1
DIGEST_VERSION = 1
ALGORITHM = 'sha256'
COLUMNS = ('library_uuid', 'library_root', 'relative_path', 'format', 'size',
           'mtime_ns', 'algorithm', 'digest_version', 'sha256', 'hashed_at_ns', 'last_seen_ns')
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
WHERE_KEY = ('library_uuid=:library_uuid AND library_root=:library_root '
             'AND relative_path=:relative_path AND format=:format')


@dataclass(frozen=True)
class CacheKey:
    library_uuid: Optional[str]
    library_root: str
    relative_path: str
    format: str

    @classmethod
    def from_book(cls, book):
        identity = book['library']
        root = identity.root.resolve()
        relative = Path(book['path']).resolve().relative_to(root)
        if relative != Path(book['relative_path']):
            raise ValueError('Book relative path does not match its library root')
        return cls(identity.uuid, str(root), relative.as_posix(), book['format'].upper())


@dataclass(frozen=True)
class CacheEntry:
    fingerprint: FileFingerprint
    sha256: str
    hashed_at_ns: int
    last_seen_ns: int
    algorithm: str = ALGORITHM
    digest_version: int = DIGEST_VERSION

    @classmethod
    def from_digest(cls, result):
        now = time.time_ns()
        return cls(result.fingerprint, result.sha256, now, now)

    def matches(self, fingerprint):
        return (self.fingerprint == fingerprint and self.algorithm == ALGORITHM
                and self.digest_version == DIGEST_VERSION)

    def as_digest(self):
        return FileDigest(self.sha256, self.fingerprint)


@dataclass(frozen=True)
class CacheSchema:
    application_id: int
    version: int
    objects: frozenset
    columns: tuple

    @property
    def is_current(self):
        return (self.application_id == APPLICATION_ID and self.version == SCHEMA_VERSION
                and self.objects == {('table', 'digests')} and self.columns == COLUMNS)

    @property
    def is_empty(self):
        return (self.application_id == 0 and self.version == 0
                and not self.objects and not self.columns)


class CacheStorageError(RuntimeError):
    """Storage failed; the caller decides whether to continue without persistence."""

    def __init__(self, message, advice='Check the cache location and retry.'):
        super().__init__(message)
        self.advice = advice

    @classmethod
    def from_exception(cls, error):
        """Describe known failures without exposing arbitrary SQL or file contents."""
        # Standard SQLite base codes; numeric values also work on Python 3.9,
        # which does not expose all the newer sqlite3 error-code attributes.
        code = getattr(error, 'sqlite_errorcode', 0) & 0xff
        message = str(error).lower()
        # SQLITE_BUSY / SQLITE_LOCKED
        if code in (5, 6) or message in ('database is locked', 'database table is locked'):
            return cls('Hash cache is locked', 'Retry after the other cache operation finishes.')
        # SQLITE_PERM / SQLITE_READONLY / SQLITE_AUTH
        if isinstance(error, PermissionError) or code in (3, 8, 23):
            return cls('Hash cache permission denied', 'Check cache file and directory permissions.')
        # SQLITE_CORRUPT / SQLITE_NOTADB
        if code in (11, 26) or message in ('file is not a database', 'database disk image is malformed'):
            return cls('Hash cache is corrupt or invalid',
                       'Remove the disposable cache file to rebuild it on a later run.')
        # SQLITE_FULL
        if code == 13 or getattr(error, 'errno', None) == errno.ENOSPC:
            return cls('Hash cache storage is full', 'Free space on the cache filesystem and retry.')
        if isinstance(error, OSError):
            return cls('Hash cache filesystem access failed')
        return cls('Hash cache database operation failed')


class ThreadConfinementError(RuntimeError):
    """A provider/store was used outside the thread that owns its state."""


class SQLiteDigestStore:
    """Lazy SQLite lookup/save operations; errors never decide hashing policy.

    lookup returns an entry without updating it. save atomically writes exactly
    the supplied entry. Neither method reads ebook contents or emits diagnostics.
    Create, use, and close each store on a single thread.
    """

    def __init__(self, path, *, timeout=0.1):
        self.path = Path(path).expanduser()
        self.timeout = timeout
        self._connection = None
        self._closed = False
        self._owner = threading.current_thread()

    def _check_thread(self):
        if threading.current_thread() is not self._owner:
            raise ThreadConfinementError('Use and close the cache store on its creating thread.')

    def __enter__(self):
        self._check_thread()
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self._check_thread()
        try:
            if self._connection is not None:
                self._connection.close()
        except sqlite3.Error as error:
            raise CacheStorageError.from_exception(error) from error
        else:
            self._connection = None
            self._closed = True

    @contextmanager
    def _database(self):
        self._check_thread()
        if self._closed:
            raise CacheStorageError('Hash cache store is closed')
        try:
            self._open()
            yield self._connection
        except (OSError, sqlite3.Error) as error:
            raise CacheStorageError.from_exception(error) from error

    @staticmethod
    def _schema(connection):
        return CacheSchema(
            application_id=connection.execute('PRAGMA application_id').fetchone()[0],
            version=connection.execute('PRAGMA user_version').fetchone()[0],
            objects=frozenset(tuple(row) for row in connection.execute(
                "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")),
            columns=tuple(row['name'] for row in connection.execute('PRAGMA table_info(digests)')),
        )

    def _prepare(self, connection, created):
        schema = self._schema(connection)
        if schema.is_current:
            return
        if not (created and schema.is_empty):
            raise CacheStorageError('Unrecognized cache schema',
                                    'Choose a different cache location; this file was left unchanged.')
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            schema = self._schema(connection)
            if schema.is_current:
                return
            if not (created and schema.is_empty):
                raise CacheStorageError('Cache schema changed during initialization',
                                        'Retry after the other initialization finishes.')
            connection.execute(CREATE_DIGESTS)
            connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
            connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    def _open(self):
        if self._connection is not None:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise CacheStorageError('Cache path is a symlink', 'Choose a non-symlink cache file.')
        created = False
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
            created = True
        connection = sqlite3.connect(self.path, timeout=self.timeout)
        connection.row_factory = sqlite3.Row
        try:
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise CacheStorageError('Cache integrity check failed',
                                        'Remove the disposable cache file to rebuild it on a later run.')
            self._prepare(connection, created)
        except (OSError, sqlite3.Error, CacheStorageError):
            connection.close()
            raise
        self._connection = connection

    def lookup(self, key):
        with self._database() as connection:
            row = connection.execute(
                'SELECT size,mtime_ns,algorithm,digest_version,sha256,hashed_at_ns,last_seen_ns '
                'FROM digests WHERE ' + WHERE_KEY, asdict(key)).fetchone()
            if row is None:
                return None
            if not isinstance(row['sha256'], str) or re.fullmatch(r'[0-9a-f]{64}', row['sha256']) is None:
                raise CacheStorageError('Invalid cached digest',
                                        'Remove the disposable cache file to rebuild it on a later run.')
            return CacheEntry(
                fingerprint=FileFingerprint(row['size'], row['mtime_ns']),
                sha256=row['sha256'], hashed_at_ns=row['hashed_at_ns'],
                last_seen_ns=row['last_seen_ns'], algorithm=row['algorithm'],
                digest_version=row['digest_version'],
            )

    def save(self, key, entry):
        if key.library_uuid is None:
            raise ValueError('Persistent entries require a library UUID')
        values = dict(asdict(key), size=entry.fingerprint.size,
                      mtime_ns=entry.fingerprint.mtime_ns, algorithm=entry.algorithm,
                      digest_version=entry.digest_version, sha256=entry.sha256,
                      hashed_at_ns=entry.hashed_at_ns, last_seen_ns=entry.last_seen_ns)
        with self._database() as connection:
            with connection:
                connection.execute(
                    'INSERT INTO digests '
                    '(library_uuid,library_root,relative_path,format,size,mtime_ns,'
                    'algorithm,digest_version,sha256,hashed_at_ns,last_seen_ns) '
                    'VALUES (:library_uuid,:library_root,:relative_path,:format,:size,:mtime_ns,'
                    ':algorithm,:digest_version,:sha256,:hashed_at_ns,:last_seen_ns) '
                    'ON CONFLICT(library_uuid,library_root,relative_path,format) DO UPDATE SET '
                    'size=excluded.size,mtime_ns=excluded.mtime_ns,algorithm=excluded.algorithm,'
                    'digest_version=excluded.digest_version,sha256=excluded.sha256,'
                    'hashed_at_ns=excluded.hashed_at_ns,last_seen_ns=excluded.last_seen_ns', values)
