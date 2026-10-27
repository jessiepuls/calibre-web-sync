"""SQLite persistence for named digest entries, independent of file hashing."""
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
import errno
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
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
    the supplied entry. touch advances last-seen only for the observed digest.
    These operations do not read ebook contents or emit diagnostics.
    Create, use, and close each store on a single thread.

    Use a private per-user cache tree. Directory/file ownership and permission bits
    are checked; pinned descriptors detect pathname replacement before operations.
    Processes running as the same user are not treated as adversaries. New caches
    are built privately, then published without replacing an existing pathname.
    """

    def __init__(self, path, *, timeout=0.1):
        self.path = Path(path).expanduser().absolute()
        self.timeout = timeout
        self._connection = None
        self._directory_fd = None
        self._file_fd = None
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
            self._close_file_handles()
            self._closed = True

    def _close_file_handles(self):
        for name in ('_file_fd', '_directory_fd'):
            descriptor = getattr(self, name)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)

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

    def _build_database(self, staged):
        """Build a complete database before its final pathname becomes visible."""
        with closing(sqlite3.connect(staged, timeout=self.timeout)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(CREATE_DIGESTS)
            connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
            connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    def _publish_database(self, staged):
        try:
            # A hard link publishes completed bytes atomically and never replaces
            # a winner from another initializer. Both paths are on one filesystem.
            os.link(staged, self.path.name, dst_dir_fd=self._directory_fd,
                    follow_symlinks=False)
        except FileExistsError:
            pass

    def _initialize(self):
        with tempfile.TemporaryDirectory(prefix='.hash-cache-init-', dir=self.path.parent) as directory:
            staged = Path(directory) / 'hashes.sqlite3'
            self._build_database(staged)
            staged.chmod(0o600)
            self._validate_directory()
            self._publish_database(staged)

    @staticmethod
    def _safe_attributes(attributes, *, directory):
        correct_type = stat.S_ISDIR if directory else stat.S_ISREG
        return (correct_type(attributes.st_mode) and attributes.st_uid == os.getuid()
                and not attributes.st_mode & 0o022)

    @staticmethod
    def _same_file(left, right):
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)

    def _validate_directory(self):
        opened = os.fstat(self._directory_fd)
        current = self.path.parent.lstat()
        if not self._safe_attributes(current, directory=True):
            raise CacheStorageError('Unsafe cache directory',
                                    'Use a user-owned cache directory not writable by other users.')
        if not self._same_file(opened, current):
            raise CacheStorageError('Cache directory changed while opening it',
                                    'Retry with a stable, private cache directory.')

    def _validate_location(self):
        self._validate_directory()
        current = self.path.lstat()
        opened = os.fstat(self._file_fd)
        if not self._safe_attributes(current, directory=False) or not self._same_file(opened, current):
            raise CacheStorageError('Cache file changed or has unsafe permissions',
                                    'Use a stable, user-owned regular cache file not writable by others.')

    def _open_directory(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.path.parent.resolve() / self.path.name
        self._directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._validate_directory()

    def _open_file(self):
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        return os.open(self.path.name, flags, dir_fd=self._directory_fd)

    def _open_or_initialize_file(self):
        try:
            self._file_fd = self._open_file()
        except FileNotFoundError:
            self._initialize()
            self._file_fd = self._open_file()
        self._validate_location()

    def _validate_database(self, connection):
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise CacheStorageError('Cache integrity check failed',
                                    'Remove the disposable cache file to rebuild it on a later run.')
        if not self._schema(connection).is_current:
            raise CacheStorageError('Unrecognized cache schema',
                                    'Choose a different cache location; this file was left unchanged.')

    def _open(self):
        if self._connection is not None:
            self._validate_location()
            return
        try:
            self._open_directory()
            self._open_or_initialize_file()
            # mode=rw prevents SQLite from recreating a pathname removed between
            # our descriptor open and its own pathname-based open.
            self._connection = sqlite3.connect(self.path.as_uri() + '?mode=rw',
                                               uri=True, timeout=self.timeout)
            self._connection.row_factory = sqlite3.Row
            self._validate_location()  # Before SQLite reads or writes the database.
            self._validate_database(self._connection)
        except (OSError, sqlite3.Error, CacheStorageError):
            self.close()
            raise

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

    def touch(self, key, entry, last_seen_ns):
        """Advance observation time only if the stored digest is still the same.

        Replacements and deletions since lookup are harmless no-ops. Concurrent
        observations of the same digest retain the greatest last-seen timestamp.
        """
        if key.library_uuid is None:
            raise ValueError('Persistent entries require a library UUID')
        values = dict(asdict(key), size=entry.fingerprint.size,
                      mtime_ns=entry.fingerprint.mtime_ns, algorithm=entry.algorithm,
                      digest_version=entry.digest_version, sha256=entry.sha256,
                      hashed_at_ns=entry.hashed_at_ns, last_seen_ns=last_seen_ns)
        with self._database() as connection, connection:
            connection.execute(
                'UPDATE digests SET last_seen_ns=MAX(last_seen_ns,:last_seen_ns) '
                'WHERE ' + WHERE_KEY + ' AND size=:size AND mtime_ns=:mtime_ns '
                'AND algorithm=:algorithm AND digest_version=:digest_version '
                'AND sha256=:sha256 AND hashed_at_ns=:hashed_at_ns', values)

    def prune_epubs(self, library, keys, *, before_ns):
        """Atomically prune one library's EPUBs using its complete observed set.

        Temporary rows avoid SQLite's parameter limit for large catalogs. Other
        namespaces and formats are never affected. The caller supplies scan proof.
        Entries hashed or observed at/after scan start survive this older scan.
        """
        self._check_thread()
        if library.namespace is None:
            return 0
        observed = []
        for key in keys:
            if (key.library_uuid, key.library_root) != library.namespace or key.format != 'EPUB':
                raise ValueError('Pruning keys must belong to the scanned library and EPUB format')
            observed.append((key.relative_path, key.format))

        with self._database() as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('CREATE TEMP TABLE observed_epubs '
                               '(relative_path TEXT, format TEXT, PRIMARY KEY(relative_path,format))')
            connection.executemany('INSERT OR IGNORE INTO observed_epubs '
                                   '(relative_path,format) VALUES (?,?)', observed)
            deleted = connection.execute(
                "DELETE FROM digests WHERE library_uuid=? AND library_root=? AND format='EPUB' "
                'AND hashed_at_ns<? AND last_seen_ns<? '
                'AND NOT EXISTS (SELECT 1 FROM observed_epubs '
                'WHERE observed_epubs.relative_path=digests.relative_path '
                'AND observed_epubs.format=digests.format)',
                (*library.namespace, before_ns, before_ns)).rowcount
            connection.execute('DROP TABLE observed_epubs')
            return deleted
