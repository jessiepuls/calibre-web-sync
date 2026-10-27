"""Diagnostics, thread ownership, and validation of cache hits."""
from concurrent.futures import ThreadPoolExecutor
import errno
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import warnings

from calibre_web_sync import cache, cache_store
from calibre_web_sync.library import LibraryIdentity, file_fingerprint


class CacheSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'book.epub'
        self.path.write_bytes(b'original ebook')
        self.cache_path = self.root / 'cache' / 'hashes.sqlite3'
        self.book = {
            'library': LibraryIdentity('326903a0-15a5-4a37-85a7-6c69cd41dc13', self.root),
            'path': self.path, 'relative_path': Path('book.epub'), 'format': 'EPUB',
        }

    def provider(self):
        store = cache_store.SQLiteDigestStore(self.cache_path)
        provider = cache.DigestProvider(store)
        self.addCleanup(provider.close)
        return provider

    @staticmethod
    def on_other_thread(operation):
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(operation).result(timeout=5)

    def test_default_fallback_works_when_warnings_are_errors(self):
        self.cache_path.parent.mkdir()
        self.cache_path.write_bytes(b'not a database')
        provider = self.provider()
        with warnings.catch_warnings():
            warnings.simplefilter('error', RuntimeWarning)
            with self.assertLogs(cache.LOGGER, level='WARNING') as messages:
                result = provider.digest(self.book)
                self.assertEqual(provider.digest(self.book, force=True), result)
        self.assertFalse(provider.persistence_enabled)
        self.assertEqual(len(messages.output), 1)
        self.assertIn('corrupt or invalid', messages.output[0])

    def test_missing_identity_uses_logging_not_python_warnings(self):
        self.book['library'] = LibraryIdentity(None, self.root)
        provider = self.provider()
        with warnings.catch_warnings():
            warnings.simplefilter('error', RuntimeWarning)
            with self.assertLogs(cache.LOGGER, level='WARNING') as messages:
                provider.digest(self.book)
                provider.digest(self.book, force=True)
        self.assertEqual(len(messages.output), 1)
        self.assertIn('no usable UUID', messages.output[0])
        self.assertFalse(self.cache_path.exists())

    def test_failure_categories_have_relevant_advice_without_raw_error_contents(self):
        cases = [
            (PermissionError(errno.EACCES, 'private filesystem details'), 'permission', 'permissions'),
            (sqlite3.OperationalError('database is locked'), 'locked', 'other cache operation'),
            (sqlite3.DatabaseError('file is not a database'), 'corrupt', 'disposable cache'),
            (OSError(errno.ENOSPC, 'private disk details'), 'full', 'Free space'),
            (OSError('private path details'), 'filesystem', 'cache location'),
            (sqlite3.OperationalError('private SQL details'), 'database operation', 'cache location'),
        ]
        for error, category, advice in cases:
            with self.subTest(category=category):
                diagnosed = cache_store.CacheStorageError.from_exception(error)
                self.assertIn(category, str(diagnosed))
                self.assertIn(advice, diagnosed.advice)
                self.assertNotIn('private', str(diagnosed) + diagnosed.advice)

    def test_extended_sqlite_codes_are_classified_by_base_code(self):
        error = sqlite3.OperationalError('private SQL details')
        error.sqlite_errorcode = 8 | (3 << 8)  # SQLITE_READONLY extended error.
        diagnosed = cache_store.CacheStorageError.from_exception(error)
        self.assertIn('permission', str(diagnosed))
        self.assertNotIn('private', str(diagnosed))

    def assert_provider_thread_confinement(self, provider):
        expected = provider.digest(self.book)
        store = provider.store
        for operation in (lambda: provider.digest(self.book), provider.close):
            with self.subTest(operation=operation):
                with self.assertRaises(cache_store.ThreadConfinementError):
                    self.on_other_thread(operation)
        self.assertIs(provider.store, store)
        self.assertEqual(provider.digest(self.book, force=True), expected)

    def test_cached_provider_rejects_other_threads_without_dropping_connection(self):
        provider = self.provider()
        self.assert_provider_thread_confinement(provider)
        self.assertTrue(provider.persistence_enabled)
        self.assertIsNotNone(provider.store._connection)

    def test_uncached_provider_enforces_the_same_thread_contract(self):
        provider = cache.DigestProvider()
        self.addCleanup(provider.close)
        self.assert_provider_thread_confinement(provider)
        self.assertFalse(provider.persistence_enabled)

    def test_store_rejects_cross_thread_reads_and_close_without_losing_connection(self):
        provider = self.provider()
        provider.digest(self.book)
        store = provider.store
        key = cache_store.CacheKey.from_book(self.book)
        connection = store._connection
        for operation in (lambda: store.lookup(key), store.close):
            with self.subTest(operation=operation):
                with self.assertRaises(cache_store.ThreadConfinementError):
                    self.on_other_thread(operation)
        self.assertIs(store._connection, connection)
        self.assertIsNotNone(store.lookup(key))
        store.close()
        self.assertIsNone(store._connection)

    def assert_hit_race_detected(self, mode, delete):
        self.path.write_bytes(b'original ebook')
        provider = self.provider()
        provider.digest(self.book)
        if mode == 'persistent':
            provider.close()
            provider = self.provider()
        initial_hashes = provider.stats.files_hashed
        calls = 0

        def changed_after_first_stat(path):
            nonlocal calls
            calls += 1
            fingerprint = file_fingerprint(path)
            if calls == 1:
                if delete:
                    self.path.unlink()
                else:
                    self.path.write_bytes(b'changed and longer ebook')
            return fingerprint

        expected = FileNotFoundError if delete else RuntimeError
        with patch.object(cache, 'file_fingerprint', side_effect=changed_after_first_stat):
            with self.assertRaises(expected):
                provider.digest(self.book)
        self.assertTrue(provider.persistence_enabled)
        self.assertEqual(provider.stats.files_hashed, initial_hashes)

    def test_changes_during_ephemeral_and_persistent_hit_checks_are_errors(self):
        for mode in ('memory', 'persistent'):
            with self.subTest(mode=mode):
                self.assert_hit_race_detected(mode, delete=False)

    def test_deletion_during_ephemeral_and_persistent_hit_checks_is_an_error(self):
        for mode in ('memory', 'persistent'):
            with self.subTest(mode=mode):
                self.assert_hit_race_detected(mode, delete=True)


if __name__ == '__main__':
    unittest.main()
