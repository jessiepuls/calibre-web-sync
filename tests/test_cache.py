"""Persistent hash storage without real libraries, credentials, or network I/O."""
import contextlib
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from calibre_web_sync import cache, cache_store
from calibre_web_sync.library import LibraryIdentity, file_fingerprint

UUID = '326903a0-15a5-4a37-85a7-6c69cd41dc13'
OTHER_UUID = '426903a0-15a5-4a37-85a7-6c69cd41dc13'


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'cache' / 'hashes.sqlite3'
        self.messages = []
        self.book = self.make_book('local')

    def make_book(self, folder, content=b'original ebook', identity=UUID):
        root = self.root / folder
        root.mkdir()
        path = root / 'book.epub'
        path.write_bytes(content)
        # Stale catalog sizes deliberately do not match real attributes.
        return {'id': 1, 'library': LibraryIdentity(identity, root.resolve()),
                'path': path, 'relative_path': Path('book.epub'), 'format': 'EPUB',
                'size': 999999, 'mtime_ns': 1}

    def provider(self, **kwargs):
        timeout = kwargs.pop('timeout', 0.1)
        store = cache_store.SQLiteDigestStore(self.path, timeout=timeout)
        provider = cache.DigestProvider(store, warn=self.messages.append, **kwargs)
        self.addCleanup(provider.close)
        return provider

    def query(self, sql, args=()):
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            result = c.execute(sql, args).fetchall()
            c.commit()
            return result

    def populate(self):
        with self.provider() as provider:
            return provider.digest(self.book)

    def fake_store(self):
        store = Mock(spec=['path', 'lookup', 'save', 'close'])
        store.path = self.path
        store.lookup.return_value = None
        return store

    def test_provider_can_use_a_store_without_sqlite(self):
        store = self.fake_store()
        entry = cache_store.CacheEntry(
            fingerprint=file_fingerprint(self.book['path']),
            sha256=hashlib.sha256(b'original ebook').hexdigest(),
            hashed_at_ns=100, last_seen_ns=100,
        )
        store.lookup.return_value = entry
        with cache.DigestProvider(store) as provider:
            with patch.object(cache, 'stable_digest', side_effect=AssertionError('Cache hit')):
                with patch.object(cache.time, 'time_ns', return_value=200):
                    self.assertEqual(provider.digest(self.book), entry.sha256)
            key = cache_store.CacheKey.from_book(self.book)
            store.lookup.assert_called_once_with(key)
            saved_key, saved_entry = store.save.call_args.args
            self.assertEqual(saved_key, key)
            self.assertEqual(saved_entry.hashed_at_ns, 100)
            self.assertEqual(saved_entry.last_seen_ns, 200)
            self.assertEqual(provider.stats.persistent_hits, 1)
        store.close.assert_called_once()
        self.assertFalse(self.path.parent.exists())

    def test_provider_saves_named_entry_on_miss(self):
        store = self.fake_store()
        with cache.DigestProvider(store) as provider:
            expected = provider.digest(self.book)
            key, entry = store.save.call_args.args
            self.assertEqual(key.library_uuid, UUID)
            self.assertEqual(key.relative_path, 'book.epub')
            self.assertEqual(entry.sha256, expected)
            self.assertEqual(entry.fingerprint, file_fingerprint(self.book['path']))
            self.assertEqual(provider.stats.files_hashed, 1)

    def test_provider_handles_storage_failure_in_one_place(self):
        store = self.fake_store()
        store.save.side_effect = cache_store.CacheStorageError('simulated storage failure')
        with cache.DigestProvider(store, warn=self.messages.append) as provider:
            expected = provider.digest(self.book)
            self.assertFalse(provider.persistence_enabled)
            self.assertEqual(provider.digest(self.book, force=True), expected)
            store.lookup.assert_called_once()
            store.save.assert_called_once()
            store.close.assert_called_once()
            self.assertEqual(len(self.messages), 1)

    def test_storage_is_lazy_and_files_are_private(self):
        provider = self.provider()
        self.assertFalse(self.path.parent.exists())
        provider.close()
        self.assertFalse(self.path.parent.exists())
        provider = self.provider()
        expected = provider.digest(self.book)
        self.assertEqual(expected, hashlib.sha256(b'original ebook').hexdigest())
        self.assertEqual(provider.stats.persistent_misses, 1)
        self.assertEqual(provider.stats.files_hashed, 1)
        self.assertEqual(provider.stats.bytes_hashed, len(b'original ebook'))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.query('PRAGMA application_id')[0][0], cache_store.APPLICATION_ID)
        self.assertEqual(self.query('PRAGMA user_version')[0][0], cache_store.SCHEMA_VERSION)

    def test_persistence_across_instances_and_current_attributes(self):
        expected = self.populate()
        provider = self.provider()
        with patch.object(cache, 'stable_digest', side_effect=AssertionError('Should not hash')):
            self.assertEqual(provider.digest(self.book), expected)
        self.assertEqual(provider.stats.persistent_hits, 1)
        self.assertEqual(provider.stats.files_hashed, 0)
        self.assertEqual(provider.stats.persistent_misses, 0)

    def test_persistence_in_a_separate_process(self):
        expected = self.populate()
        code = '''
import sys
from pathlib import Path
from calibre_web_sync.cache import DigestProvider
from calibre_web_sync.cache_store import SQLiteDigestStore
from calibre_web_sync.library import LibraryIdentity, file_fingerprint
from unittest.mock import Mock, patch
book={'library':LibraryIdentity(sys.argv[3],Path(sys.argv[2])),
      'path':Path(sys.argv[2])/'book.epub','relative_path':Path('book.epub'),'format':'EPUB'}
with DigestProvider(SQLiteDigestStore(sys.argv[1])) as provider:
    with patch('calibre_web_sync.cache.stable_digest',side_effect=AssertionError('unexpected read')):
        print(provider.digest(book))
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.path),
                                 str(self.book['library'].root), UUID],
                                capture_output=True, text=True, check=True, timeout=30)
        self.assertEqual(result.stdout.strip(), expected)

    def test_ephemeral_reuse_and_fresh_current_stats(self):
        provider = self.provider()
        expected = provider.digest(self.book)
        with patch.object(cache, 'stable_digest', side_effect=AssertionError('Should not hash')):
            self.assertEqual(provider.digest(dict(self.book, title='New metadata')), expected)
        self.assertEqual(provider.stats.ephemeral_hits, 1)
        self.assertEqual(provider.stats.persistent_misses, 1)
        self.assertEqual(provider.stats.files_hashed, 1)

    def test_size_and_mtime_changes_invalidate_even_within_one_provider(self):
        provider = self.provider()
        original = provider.digest(self.book)
        self.book['path'].write_bytes(b'different and longer ebook')
        second = provider.digest(self.book)
        self.assertNotEqual(original, second)
        attributes = self.book['path'].stat()
        os.utime(self.book['path'], ns=(attributes.st_atime_ns, attributes.st_mtime_ns + 1000000000))
        self.assertEqual(provider.digest(self.book), second)
        self.assertEqual(provider.stats.files_hashed, 3)

    def test_namespace_path_and_format_isolation(self):
        self.populate()
        copied_root = self.root / 'copy'
        shutil.copytree(self.book['library'].root, copied_root)
        copied = dict(self.book, library=LibraryIdentity(UUID, copied_root), path=copied_root/'book.epub')
        other_uuid = dict(self.book, library=LibraryIdentity(OTHER_UUID, self.book['library'].root))
        renamed = self.book['path'].with_name('renamed.epub')
        shutil.copy2(self.book['path'], renamed)
        other_path = dict(self.book, path=renamed, relative_path=Path('renamed.epub'))
        other_format = dict(self.book, format='MOBI')
        provider = self.provider()
        for book in (copied, other_uuid, other_path, other_format):
            provider.digest(book)
        self.assertEqual(provider.stats.files_hashed, 4)
        self.assertEqual(self.query('SELECT count(*) FROM digests')[0][0], 5)

    def test_same_numeric_book_id_does_not_share_content_across_libraries(self):
        other = self.make_book('remote', content=b'another ebook!')
        provider = self.provider()
        self.assertEqual(self.book['id'], other['id'])
        self.assertNotEqual(provider.digest(self.book), provider.digest(other))
        self.assertEqual(provider.stats.files_hashed, 2)

    def test_missing_uuid_explains_bypass_once_and_does_not_open_cache(self):
        book = dict(self.book, library=LibraryIdentity(None, self.book['library'].root))
        provider = self.provider()
        provider.digest(book)
        provider.digest(book, force=True)
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(len(self.messages), 1)
        self.assertIn('no usable UUID', self.messages[0])
        provider.digest(self.book)
        self.assertTrue(self.path.is_file())

    def test_uncached_provider_never_opens_sqlite_or_creates_storage(self):
        with cache.DigestProvider() as provider:
            with patch.object(cache_store.sqlite3, 'connect', side_effect=AssertionError('No database access')):
                expected = provider.digest(self.book)
                self.assertEqual(provider.digest(self.book), expected)
            self.assertEqual(provider.stats.files_hashed, 1)
            self.assertEqual(provider.stats.ephemeral_hits, 1)
        self.assertFalse(self.path.parent.exists())

    def test_uncached_provider_leaves_an_existing_database_untouched(self):
        self.populate()
        before = self.path.read_bytes()
        with cache.DigestProvider() as provider:
            provider.digest(self.book)
        self.assertEqual(self.path.read_bytes(), before)

    def test_force_rehash_bypasses_memory_and_refreshes_persistence(self):
        provider = self.provider()
        original = provider.digest(self.book)
        attributes = self.book['path'].stat()
        self.book['path'].write_bytes(b'changed! ebook')
        os.utime(self.book['path'], ns=(attributes.st_atime_ns, attributes.st_mtime_ns))
        # Deliberate demonstration of the accepted size/mtime trust limitation.
        self.assertEqual(provider.digest(self.book), original)
        refreshed = provider.digest(self.book, force=True)
        self.assertNotEqual(refreshed, original)
        self.assertEqual(self.provider().digest(self.book), refreshed)

    def test_rehash_mode_bypasses_persistence_but_reuses_this_runs_results(self):
        self.populate()
        provider = self.provider(rehash=True)
        provider.digest(self.book)
        provider.digest(self.book)
        self.assertEqual(provider.stats.files_hashed, 1)
        self.assertEqual(provider.stats.ephemeral_hits, 1)
        self.assertEqual(provider.stats.persistent_hits, 0)

    def test_algorithm_and_digest_version_invalidate_entries(self):
        self.populate()
        for column, value in [('algorithm', 'sha1'), ('digest_version', 99)]:
            with self.subTest(column=column):
                self.query('UPDATE digests SET ' + column + '=?', (value,))
                provider = self.provider()
                provider.digest(self.book)
                self.assertEqual(provider.stats.files_hashed, 1)

    def test_calculation_time_is_preserved_and_last_seen_updates_on_hit(self):
        with patch.object(cache.time, 'time_ns', return_value=100):
            self.populate()
        with patch.object(cache.time, 'time_ns', return_value=200):
            self.provider().digest(self.book)
        self.assertEqual(self.query('SELECT hashed_at_ns,last_seen_ns FROM digests'), [(100, 200)])

    def test_corrupt_file_is_not_overwritten_and_warning_occurs_once(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b'not a SQLite database')
        original = self.path.read_bytes()
        provider = self.provider()
        expected = provider.digest(self.book)
        self.assertEqual(expected, hashlib.sha256(b'original ebook').hexdigest())
        provider.digest(self.book, force=True)
        self.assertFalse(provider.persistence_enabled)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.path.read_bytes(), original)

    def test_unrecognized_database_is_not_overwritten(self):
        self.path.parent.mkdir()
        self.query('CREATE TABLE important_data(value TEXT)')
        self.query("INSERT INTO important_data VALUES('keep me')")
        original = self.path.read_bytes()
        provider = self.provider()
        provider.digest(self.book)
        self.assertFalse(provider.persistence_enabled)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.query('SELECT * FROM important_data'), [('keep me',)])

    def test_unsupported_schema_version_is_not_overwritten(self):
        self.populate()
        for version in (0, cache_store.SCHEMA_VERSION + 1, 999):
            with self.subTest(version=version):
                self.query(f'PRAGMA user_version={version}')
                before = self.path.read_bytes()
                provider = self.provider()
                provider.digest(self.book)
                self.assertFalse(provider.persistence_enabled)
                self.assertEqual(self.path.read_bytes(), before)

    def test_permission_failure_falls_back_once(self):
        provider = self.provider()
        with patch.object(Path, 'mkdir', side_effect=PermissionError('denied')):
            provider.digest(self.book)
        provider.digest(self.book, force=True)
        self.assertFalse(provider.persistence_enabled)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(provider.stats.files_hashed, 2)

    def test_read_lock_contention_falls_back(self):
        self.populate()
        with contextlib.closing(sqlite3.connect(self.path)) as lock:
            lock.execute('BEGIN EXCLUSIVE')
            provider = self.provider(timeout=0.01)
            provider.digest(self.book)
            self.assertFalse(provider.persistence_enabled)
            self.assertEqual(len(self.messages), 1)
            lock.rollback()

    def test_write_lock_contention_falls_back_without_losing_digest(self):
        provider = self.provider(timeout=0.01)
        expected = provider.digest(self.book)
        with contextlib.closing(sqlite3.connect(self.path)) as lock:
            lock.execute('BEGIN IMMEDIATE')
            self.assertEqual(provider.digest(self.book, force=True), expected)
            self.assertFalse(provider.persistence_enabled)
            lock.rollback()
        self.assertEqual(self.query('SELECT count(*) FROM digests'), [(1,)])

    def test_existing_empty_or_symlinked_files_are_not_adopted(self):
        self.path.parent.mkdir()
        self.path.touch()
        provider = self.provider()
        provider.digest(self.book)
        self.assertFalse(provider.persistence_enabled)
        self.assertEqual(self.path.read_bytes(), b'')
        self.path.unlink()
        target = self.root / 'unrelated.db'
        target.write_bytes(b'keep this file')
        self.path.symlink_to(target)
        provider = self.provider()
        provider.digest(self.book)
        self.assertFalse(provider.persistence_enabled)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_bytes(), b'keep this file')

    def test_invalid_cached_hash_disables_persistence_without_replacing_database(self):
        self.populate()
        self.query("UPDATE digests SET sha256='broken'")
        before = self.path.read_bytes()
        provider = self.provider()
        provider.digest(self.book)
        self.assertFalse(provider.persistence_enabled)
        self.assertEqual(provider.stats.files_hashed, 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_ebook_errors_are_not_handled_as_cache_failures(self):
        provider = self.provider()
        for error in (PermissionError('ebook unreadable'), RuntimeError('ebook changed')):
            with self.subTest(error=error), patch.object(cache, 'stable_digest', side_effect=error):
                with self.assertRaises(type(error)):
                    provider.digest(self.book)
        self.assertTrue(provider.persistence_enabled)
        self.assertEqual(self.query('SELECT count(*) FROM digests'), [(0,)])
        self.assertEqual(self.messages, [])

    def test_cache_fallback_does_not_hide_an_ebook_read_error(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b'broken cache')
        with patch.object(cache, 'stable_digest', side_effect=PermissionError('ebook unreadable')):
            with self.assertRaises(PermissionError):
                self.provider().digest(self.book)
        self.assertEqual(len(self.messages), 1)

    def test_deletion_after_caching_still_fails(self):
        provider = self.provider()
        provider.digest(self.book)
        self.book['path'].unlink()
        with self.assertRaises(FileNotFoundError):
            provider.digest(self.book)

    def test_closed_provider_cannot_reopen_storage(self):
        provider = self.provider()
        provider.close()
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            provider.digest(self.book)
        self.assertFalse(self.path.parent.exists())


if __name__ == '__main__':
    unittest.main()
