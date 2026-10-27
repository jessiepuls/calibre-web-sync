"""Controlled process interleavings and cache-file substitution regressions."""
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from calibre_web_sync import cache_store
from calibre_web_sync.cache import DigestProvider
from calibre_web_sync.library import LibraryIdentity

UUID = '326903a0-15a5-4a37-85a7-6c69cd41dc13'


def fixture_book(root):
    return {'library': LibraryIdentity(UUID, root), 'path': root / 'book.epub',
            'relative_path': Path('book.epub'), 'format': 'EPUB'}


def initialize_worker(path, results, ready=None, release=None):
    """Optional barrier pauses a child after building, immediately before publish."""
    class PausedStore(cache_store.SQLiteDigestStore):
        def _publish_database(self, staged):
            if ready is not None:
                ready.set()
                if not release.wait(10):
                    raise RuntimeError('Test barrier timed out')
            super()._publish_database(staged)

    messages = []
    try:
        with DigestProvider(PausedStore(path), warn=messages.append) as provider:
            provider.digest(fixture_book(Path(path).parent.parent))
            result = ('ok', provider.persistence_enabled, messages)
        results.put(result)
    except Exception as error:
        results.put(('error', type(error).__name__, str(error)))


class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'book.epub').write_bytes(b'fixture ebook')
        self.path = self.root / 'cache' / 'hashes.sqlite3'
        self.key = cache_store.CacheKey.from_book(fixture_book(self.root))
        self.context = multiprocessing.get_context('spawn')

    def store(self, path=None):
        store = cache_store.SQLiteDigestStore(path or self.path)
        self.addCleanup(store.close)
        return store

    def start(self, results, ready=None, release=None):
        process = self.context.Process(target=initialize_worker,
                                       args=(str(self.path), results, ready, release))
        process.start()
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process):
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        process.close()

    def result_queue(self):
        queue = self.context.Queue()
        self.addCleanup(queue.close)
        return queue

    def assert_success(self, process, results):
        process.join(timeout=10)
        self.assertFalse(process.is_alive(), 'Initializer did not finish')
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(results.get(timeout=5), ('ok', True, []))

    def test_competing_processes_see_only_fully_initialized_databases(self):
        ready, release = self.context.Event(), self.context.Event()
        results = self.result_queue()
        first = self.start(results, ready, release)
        self.assertTrue(ready.wait(10), 'First initializer did not reach its publish barrier')
        self.assertFalse(self.path.exists())
        second = self.start(results)
        self.assert_success(second, results)
        winner = self.path.stat().st_ino
        release.set()
        self.assert_success(first, results)
        self.assertEqual(self.path.stat().st_ino, winner)
        self.assertIsNotNone(self.store().lookup(self.key))

    def test_interruption_before_publish_does_not_leave_a_broken_cache(self):
        ready, release = self.context.Event(), self.context.Event()
        process = self.start(self.result_queue(), ready, release)
        self.assertTrue(ready.wait(10), 'Initializer did not reach its publish barrier')
        process.terminate()
        process.join(timeout=5)
        self.assertFalse(process.is_alive())
        self.assertFalse(self.path.exists())
        # Abandoned private staging data must not block a later initialization.
        self.assertTrue(list(self.path.parent.glob('.hash-cache-init-*')))
        self.assertIsNone(self.store().lookup(self.key))
        self.assertTrue(self.path.is_file())

    def test_build_failure_is_retryable_without_manual_cache_removal(self):
        store = self.store()
        with patch.object(store, '_build_database', side_effect=sqlite3.OperationalError('failed build')):
            with self.assertRaises(cache_store.CacheStorageError):
                store.lookup(self.key)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob('.hash-cache-init-*')), [])
        self.assertIsNone(self.store().lookup(self.key))

    def test_directory_and_file_writable_by_others_are_rejected(self):
        self.path.parent.mkdir(mode=0o777)
        self.path.parent.chmod(0o777)
        with self.assertRaisesRegex(cache_store.CacheStorageError, 'Unsafe cache directory'):
            self.store().lookup(self.key)
        self.assertFalse(self.path.exists())
        self.path.parent.chmod(0o700)
        self.store().lookup(self.key)
        self.path.chmod(0o666)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(cache_store.CacheStorageError, 'unsafe permissions'):
            self.store().lookup(self.key)
        self.assertEqual(self.path.read_bytes(), before)

    def test_non_regular_file_is_rejected_without_blocking(self):
        self.path.parent.mkdir()
        os.mkfifo(self.path, 0o600)
        with self.assertRaisesRegex(cache_store.CacheStorageError, 'unsafe permissions'):
            self.store().lookup(self.key)
        self.assertTrue(self.path.exists())

    def make_other_database(self):
        other = self.root / 'other.sqlite3'
        store = self.store(other)
        store.lookup(self.key)
        store.close()
        return other

    def test_symlink_substitution_during_sqlite_open_does_not_modify_target(self):
        self.store().lookup(self.key)
        other = self.make_other_database()
        before = other.read_bytes()
        real_connect = sqlite3.connect

        def substitute(database, **kwargs):
            self.path.unlink()
            self.path.symlink_to(other)
            return real_connect(database, **kwargs)

        with patch.object(cache_store.sqlite3, 'connect', side_effect=substitute):
            with self.assertRaisesRegex(cache_store.CacheStorageError, 'Cache file changed'):
                self.store().lookup(self.key)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(other.read_bytes(), before)

    def test_directory_substitution_during_sqlite_open_is_detected(self):
        self.store().lookup(self.key)
        other = self.make_other_database()
        before = other.read_bytes()
        real_connect = sqlite3.connect

        def substitute(database, **kwargs):
            self.path.parent.rename(self.root / 'moved-cache')
            self.path.parent.mkdir(mode=0o700)
            self.path.symlink_to(other)
            return real_connect(database, **kwargs)

        with patch.object(cache_store.sqlite3, 'connect', side_effect=substitute):
            with self.assertRaisesRegex(cache_store.CacheStorageError, 'Cache directory changed'):
                self.store().lookup(self.key)
        self.assertEqual(other.read_bytes(), before)

    def test_substitution_after_open_is_detected_before_next_operation(self):
        store = self.store()
        store.lookup(self.key)
        other = self.make_other_database()
        self.path.unlink()
        self.path.symlink_to(other)
        before = other.read_bytes()
        with self.assertRaisesRegex(cache_store.CacheStorageError, 'Cache file changed'):
            store.lookup(self.key)
        self.assertEqual(other.read_bytes(), before)

    def test_database_not_recreated_if_removed_before_sqlite_connect(self):
        self.store().lookup(self.key)
        real_connect = sqlite3.connect

        def remove(database, **kwargs):
            self.path.unlink()
            return real_connect(database, **kwargs)

        with patch.object(cache_store.sqlite3, 'connect', side_effect=remove):
            with self.assertRaises(cache_store.CacheStorageError):
                self.store().lookup(self.key)
        self.assertFalse(self.path.exists())

    def test_close_releases_file_and_directory_descriptors(self):
        store = self.store()
        store.lookup(self.key)
        descriptors = (store._file_fd, store._directory_fd)
        store.close()
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)


if __name__ == '__main__':
    unittest.main()
