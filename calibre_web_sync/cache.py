"""Coordinate fingerprint checks, optional storage, hashing, and statistics."""
from contextlib import contextmanager
from dataclasses import dataclass, replace
import logging
import os
from pathlib import Path
import sys
import threading
import time

from .cache_store import CacheEntry, CacheKey, CacheStorageError, ThreadConfinementError
from .library import LibraryCatalog, file_fingerprint, stable_digest


@dataclass
class CacheStats:
    persistent_hits: int = 0
    persistent_misses: int = 0
    ephemeral_hits: int = 0
    files_hashed: int = 0
    bytes_hashed: int = 0


LOGGER = logging.getLogger(__name__)


def default_cache_dir():
    """Return the per-user cache location without creating directories."""
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Caches' / 'calibre-web-sync'
    base = os.environ.get('XDG_CACHE_HOME')
    # The XDG specification requires an absolute path; ignore relative values.
    root = Path(base) if base and Path(base).is_absolute() else Path.home() / '.cache'
    return root / 'calibre-web-sync'


class DigestProvider:
    """Hash files with optional persistent storage; omit store to disable caching.

    rehash ignores stored entries but permits per-run reuse. force=True bypasses
    both storage and per-run hits. Ebook errors always propagate. The provider
    owns the supplied store and closes it on exit or after a storage failure.
    Create, use, and close the provider and its store on the same thread.
    """

    def __init__(self, store=None, *, rehash=False, warn=None):
        self.store = store
        self.rehash = rehash
        self.warn = warn if warn is not None else LOGGER.warning
        self.stats = CacheStats()
        self._memory = {}
        self._missing_identity = set()
        self._closed = False
        self._owner = threading.current_thread()

    def _check_thread(self):
        if threading.current_thread() is not self._owner:
            raise ThreadConfinementError('Use and close the digest provider on its creating thread.')

    @property
    def persistence_enabled(self):
        return self.store is not None

    def __enter__(self):
        self._check_thread()
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self._check_thread()
        if self.store is not None:
            self.store.close()
            self.store = None
        self._memory.clear()
        self._closed = True

    @contextmanager
    def _storage_errors(self):
        """The only transition from persistent storage to uncached operation."""
        try:
            yield
        except CacheStorageError as error:
            store = self.store
            try:
                store.close()
            except CacheStorageError:
                pass
            self.store = None
            self._memory.clear()
            self.warn(f'{error} at {store.path}; continuing without caching. {error.advice}')

    def _can_persist(self, key):
        if self.store is None:
            return False
        if key.library_uuid is None:
            if key.library_root not in self._missing_identity:
                self._missing_identity.add(key.library_root)
                self.warn(f'Library at {key.library_root} has no usable UUID; '
                          'persistent hash caching is disabled for this library.')
            return False
        return True

    def digest(self, book, force=False):
        self._check_thread()
        if self._closed:
            raise RuntimeError('Digest provider is closed')
        key = CacheKey.from_book(book)
        path = Path(book['path'])
        fingerprint = file_fingerprint(path)
        memory = self._memory.get(key)
        if not force and memory and memory.fingerprint == fingerprint:
            self._ensure_unchanged(path, fingerprint)
            self.stats.ephemeral_hits += 1
            return memory.sha256

        cached = None if force else self._reuse_stored_digest(key, path, fingerprint)
        if cached is not None:
            self._memory[key] = cached
            return cached.sha256

        # File errors are outside the storage recovery boundary.
        result = stable_digest(path)
        self.stats.files_hashed += 1
        self.stats.bytes_hashed += result.fingerprint.size
        if self._can_persist(key):
            with self._storage_errors():
                self.store.save(key, CacheEntry.from_digest(result))
        self._memory[key] = result
        return result.sha256

    def _reuse_stored_digest(self, key, path, fingerprint):
        """Validate a stored entry and record its observation before reusing it."""
        if not self._can_persist(key):
            return None
        self.stats.persistent_misses += 1
        if self.rehash:
            return None

        with self._storage_errors():
            entry = self.store.lookup(key)
            if entry is None or not entry.matches(fingerprint):
                return None
            self._ensure_unchanged(path, fingerprint)
            self.store.save(key, replace(entry, last_seen_ns=time.time_ns()))
            self.stats.persistent_misses -= 1
            self.stats.persistent_hits += 1
            return entry.as_digest()
        return None  # A storage failure was handled; the caller hashes freshly.

    def remember_verified(self, book, result):
        """Seed an already freshly verified digest without another content read."""
        self._check_thread()
        if self._closed:
            raise RuntimeError('Digest provider is closed')
        key = CacheKey.from_book(book)
        self._ensure_unchanged(Path(book['path']), result.fingerprint)
        if self._can_persist(key):
            with self._storage_errors():
                self.store.save(key, CacheEntry.from_digest(result))
        self._memory[key] = result

    def prune_catalog(self, catalog):
        """Prune only an explicitly complete EPUB scan; plain lists prove nothing."""
        self._check_thread()
        if self._closed:
            raise RuntimeError('Digest provider is closed')
        if self.store is None or not isinstance(catalog, LibraryCatalog):
            return 0
        if catalog.library.namespace is None:
            return 0
        keys = {CacheKey.from_book(book) for book in catalog}
        with self._storage_errors():
            return self.store.prune_epubs(catalog.library, keys)
        return 0

    @staticmethod
    def _ensure_unchanged(path, fingerprint):
        if file_fingerprint(path) != fingerprint:
            raise RuntimeError(f'File changed while checking its hash: {path}. Retry when idle.')
