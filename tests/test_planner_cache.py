"""Cached planning and pruning against fresh, disposable library catalogs."""
import contextlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from calibre_web_sync import cache, library, planner
from calibre_web_sync.cache_store import CacheKey, SQLiteDigestStore

LOCAL_UUID = '326903a0-15a5-4a37-85a7-6c69cd41dc13'
REMOTE_UUID = '426903a0-15a5-4a37-85a7-6c69cd41dc13'


class FixtureLibrary:
    def __init__(self, root, identity):
        self.root = root
        root.mkdir()
        with contextlib.closing(sqlite3.connect(root / 'metadata.db')) as connection:
            connection.executescript('''
                CREATE TABLE library_id(uuid TEXT);
                CREATE TABLE books(id INTEGER PRIMARY KEY,title TEXT,path TEXT);
                CREATE TABLE authors(id INTEGER PRIMARY KEY,name TEXT);
                CREATE TABLE books_authors_link(id INTEGER PRIMARY KEY,book INTEGER,author INTEGER);
                CREATE TABLE data(book INTEGER,format TEXT,name TEXT);
                INSERT INTO authors VALUES(1,'Test author');
            ''')
            connection.execute('INSERT INTO library_id VALUES(?)', (identity,))
            connection.commit()

    def execute(self, sql, args=()):
        with contextlib.closing(sqlite3.connect(self.root / 'metadata.db')) as connection:
            connection.execute(sql, args)
            connection.commit()

    def add(self, bid, title, content):
        folder = self.root / str(bid)
        folder.mkdir()
        (folder / 'book.epub').write_bytes(content)
        self.execute('INSERT INTO books VALUES(?,?,?)', (bid, title, str(bid)))
        self.execute('INSERT INTO data VALUES(?,?,?)', (bid, 'EPUB', 'book'))
        self.execute('INSERT INTO books_authors_link(book,author) VALUES(?,1)', (bid,))
        return folder / 'book.epub'

    def remove(self, bid):
        self.execute('DELETE FROM books WHERE id=?', (bid,))
        self.execute('DELETE FROM data WHERE book=?', (bid,))
        self.execute('DELETE FROM books_authors_link WHERE book=?', (bid,))
        (self.root / str(bid) / 'book.epub').unlink()

    def catalog(self):
        return library.catalog(self.root)


class PlannerCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.local = FixtureLibrary(root / 'local', LOCAL_UUID)
        self.remote = FixtureLibrary(root / 'remote', REMOTE_UUID)
        self.cache_path = root / 'state' / 'hashes.sqlite3'
        self.messages = []

    def provider(self, *, rehash=False):
        provider = cache.DigestProvider(SQLiteDigestStore(self.cache_path),
                                        rehash=rehash, warn=self.messages.append)
        self.addCleanup(provider.close)
        return provider

    def plan(self, provider=None, *, plain_lists=False, progress=None):
        local, remote = self.local.catalog(), self.remote.catalog()
        if plain_lists:
            local, remote = list(local), list(remote)
        return planner.make_plan(local, remote, progress, digest_provider=provider)

    def cache_rows(self):
        with contextlib.closing(sqlite3.connect(self.cache_path)) as connection:
            return set(connection.execute(
                'SELECT library_uuid,library_root,relative_path,format FROM digests'))

    def add_decision_examples(self):
        self.local.add(1, 'Same', b'identical')
        self.local.add(2, 'New', b'new content')
        self.local.add(3, 'Duplicate', b'new content')
        self.local.add(4, 'Review', b'edition one')
        self.remote.add(10, 'Same', b'identical')
        self.remote.add(20, 'Review', b'edition two')

    def test_default_uncached_path_preserves_decisions_and_progress(self):
        self.add_decision_examples()
        progress = []
        plan = self.plan(progress=lambda done, total: progress.append((done, total)))
        self.assertEqual([book['status'] for book in plan],
                         ['present', 'missing', 'duplicate-local', 'review'])
        self.assertEqual(progress, [(1, 4)])
        self.assertFalse(self.cache_path.exists())

    def test_cached_uncached_and_rehashed_plans_agree(self):
        self.add_decision_examples()
        expected = self.plan()
        cached = self.provider()
        self.assertEqual(self.plan(cached), expected)
        refreshed = self.provider(rehash=True)
        self.assertEqual(self.plan(refreshed), expected)
        self.assertGreater(refreshed.stats.files_hashed, 0)
        self.assertEqual(refreshed.stats.persistent_hits, 0)

    def test_warm_run_reads_no_ebook_contents(self):
        self.add_decision_examples()
        expected = self.plan(self.provider())
        warm = self.provider()
        with patch.object(cache, 'stable_digest', side_effect=AssertionError('Warm file was read')):
            self.assertEqual(self.plan(warm), expected)
        self.assertEqual(warm.stats.files_hashed, 0)
        self.assertGreater(warm.stats.persistent_hits, 0)
        self.assertTrue(warm.persistence_enabled)
        warm.digest(self.local.catalog()[0])  # Borrowed providers remain open.

    def test_changed_file_is_rehashed_without_reading_unchanged_files(self):
        changed = self.local.add(1, 'Same', b'aaaa')
        self.local.add(2, 'Other', b'zzzz')
        self.remote.add(10, 'Same', b'aaaa')
        self.remote.add(20, 'Other', b'zzzz')
        self.plan(self.provider())
        attributes = changed.stat()
        changed.write_bytes(b'bbbb')
        os.utime(changed, ns=(attributes.st_atime_ns, attributes.st_mtime_ns + 1000000000))
        provider = self.provider()
        with patch.object(cache, 'stable_digest', wraps=library.stable_digest) as hashed:
            plan = self.plan(provider)
        self.assertEqual([row['status'] for row in plan], ['review', 'present'])
        self.assertEqual([call.args[0] for call in hashed.call_args_list], [changed.resolve()])
        self.assertEqual(provider.stats.files_hashed, 1)

    def test_metadata_edits_affect_decisions_without_rehashing(self):
        self.local.add(1, 'Review', b'aaaa')
        self.remote.add(10, 'Review', b'bbbb')
        self.assertEqual(self.plan(self.provider())[0]['status'], 'review')
        self.remote.execute("UPDATE books SET title='Other title'")
        self.remote.execute("UPDATE authors SET name='Other author'")
        with patch.object(cache, 'stable_digest', side_effect=AssertionError('Metadata triggered rehash')):
            self.assertEqual(self.plan(self.provider())[0]['status'], 'missing')

    def test_deleted_remote_book_becomes_missing_and_is_pruned(self):
        self.local.add(1, 'Same', b'aaaa')
        self.remote.add(10, 'Same', b'aaaa')
        self.assertEqual(self.plan(self.provider())[0]['status'], 'present')
        self.remote.remove(10)
        with patch.object(cache, 'stable_digest', side_effect=AssertionError('Unchanged source was read')):
            self.assertEqual(self.plan(self.provider())[0]['status'], 'missing')
        self.assertEqual({row[0] for row in self.cache_rows()}, {LOCAL_UUID})

    def test_stale_catalog_cannot_prune_hashes_learned_by_a_newer_comparison(self):
        self.local.add(1, 'Same', b'aaaa')
        with patch.object(library.time, 'time_ns', return_value=100):
            old_local, old_remote = self.local.catalog(), self.remote.catalog()
        self.remote.add(10, 'Same', b'aaaa')
        with patch.object(library.time, 'time_ns', return_value=200):
            self.plan(self.provider())
        with patch.object(library.time, 'time_ns', return_value=300):
            # The provider is created after the newer comparison, but owns old catalogs.
            old_plan = planner.make_plan(old_local, old_remote, digest_provider=self.provider())
        self.assertEqual(old_plan[0]['status'], 'missing')
        self.assertEqual(len(self.cache_rows()), 2)
        with patch.object(library.time, 'time_ns', return_value=400):
            with patch.object(cache, 'stable_digest', side_effect=AssertionError('Newer hashes were pruned')):
                self.assertEqual(self.plan(self.provider())[0]['status'], 'present')
        self.remote.remove(10)
        with patch.object(library.time, 'time_ns', return_value=500):
            self.assertEqual(self.plan(self.provider())[0]['status'], 'missing')
        self.assertEqual({row[0] for row in self.cache_rows()}, {LOCAL_UUID})

    def test_stale_catalog_cannot_prune_an_existing_hash_observed_again(self):
        self.local.add(1, 'Same', b'aaaa')
        self.remote.add(10, 'Same', b'aaaa')
        with patch.object(library.time, 'time_ns', return_value=100):
            self.plan(self.provider())
        # Temporarily absent from the old snapshot, while the EPUB still exists.
        self.remote.execute('DELETE FROM data WHERE book=10')
        with patch.object(library.time, 'time_ns', return_value=200):
            old_local, old_remote = self.local.catalog(), self.remote.catalog()
        self.remote.execute("INSERT INTO data VALUES(10,'EPUB','book')")
        with patch.object(library.time, 'time_ns', return_value=300):
            newer = self.provider()
            self.assertEqual(self.plan(newer)[0]['status'], 'present')
            self.assertEqual(newer.stats.files_hashed, 0)
        with patch.object(library.time, 'time_ns', return_value=400):
            planner.make_plan(old_local, old_remote, digest_provider=self.provider())
        self.assertEqual(len(self.cache_rows()), 2)
        with patch.object(library.time, 'time_ns', return_value=500):
            with patch.object(cache, 'stable_digest', side_effect=AssertionError('Recent hit was pruned')):
                self.assertEqual(self.plan(self.provider())[0]['status'], 'present')

    def test_empty_local_catalog_prunes_its_namespace_only(self):
        self.local.add(1, 'Same', b'aaaa')
        self.remote.add(10, 'Same', b'aaaa')
        self.plan(self.provider())
        self.local.remove(1)
        snapshot = self.local.catalog()
        self.assertEqual(len(snapshot), 0)
        self.assertEqual(snapshot.library.uuid, LOCAL_UUID)
        self.assertEqual(self.plan(self.provider()), [])
        self.assertEqual({row[0] for row in self.cache_rows()}, {REMOTE_UUID})

    def test_server_candidates_are_hashed_lazily(self):
        self.local.add(1, 'Same', b'aaaa')
        matched = self.remote.add(10, 'Same', b'aaaa')
        unused = self.remote.add(20, 'Unneeded', b'different size content')
        provider = self.provider()
        with patch.object(cache, 'stable_digest', wraps=library.stable_digest) as hashed:
            self.assertEqual(self.plan(provider)[0]['status'], 'present')
        paths = {call.args[0] for call in hashed.call_args_list}
        self.assertIn(matched.resolve(), paths)
        self.assertNotIn(unused.resolve(), paths)

    def test_existing_unused_server_cache_entry_is_preserved(self):
        self.local.add(1, 'Same', b'aaaa')
        self.remote.add(10, 'Same', b'aaaa')
        self.remote.add(20, 'Unneeded', b'different size content')
        provider = self.provider()
        provider.digest(self.remote.catalog()[1])
        key = CacheKey.from_book(self.remote.catalog()[1])
        self.plan(provider)
        self.assertIn((key.library_uuid, key.library_root, key.relative_path, key.format), self.cache_rows())

    def test_size_change_hashes_new_candidate_and_preserves_old_unused_candidate(self):
        source = self.local.add(1, 'Same', b'aaaa')
        self.remote.add(10, 'Same', b'aaaa')
        newly_needed = self.remote.add(20, 'Another', b'new sized content')
        self.plan(self.provider())
        source.write_bytes(b'new sized content')
        provider = self.provider()
        with patch.object(cache, 'stable_digest', wraps=library.stable_digest) as hashed:
            result = self.plan(provider)
        self.assertEqual(result[0]['status'], 'present')
        self.assertEqual({call.args[0] for call in hashed.call_args_list},
                         {source.resolve(), newly_needed.resolve()})
        self.assertEqual(sum(row[0] == REMOTE_UUID for row in self.cache_rows()), 2)

    def test_failed_catalog_scan_does_not_prune_either_library(self):
        self.local.add(1, 'Same', b'aaaa')
        missing = self.remote.add(10, 'Same', b'aaaa')
        provider = self.provider()
        self.plan(provider)
        before = self.cache_rows()
        self.local.remove(1)
        missing.unlink()
        with patch.object(provider.store, 'prune_epubs', side_effect=AssertionError('Partial scan pruned')):
            with self.assertRaisesRegex(RuntimeError, 'missing EPUB'):
                self.plan(provider)
        self.assertEqual(self.cache_rows(), before)

    def test_disconnected_library_stops_comparison_without_pruning(self):
        self.local.add(1, 'Same', b'aaaa')
        self.remote.add(10, 'Same', b'aaaa')
        provider = self.provider()
        self.plan(provider)
        before = self.cache_rows()
        self.remote.root = self.remote.root / 'unmounted'
        with self.assertRaisesRegex(RuntimeError, 'database not found'):
            self.plan(provider)
        self.assertEqual(self.cache_rows(), before)

    def test_failure_during_planning_does_not_prune(self):
        self.local.add(1, 'First', b'aaaa')
        self.local.add(2, 'Deleted', b'bbbb')
        provider = self.provider()
        self.plan(provider)
        before = self.cache_rows()
        self.local.remove(2)
        with patch.object(provider, 'digest', side_effect=PermissionError('Unreadable ebook')):
            with self.assertRaises(PermissionError):
                self.plan(provider)
        self.assertEqual(self.cache_rows(), before)

    def test_plain_lists_do_not_authorize_pruning(self):
        self.local.add(1, 'Same', b'aaaa')
        self.remote.add(10, 'Same', b'aaaa')
        provider = self.provider()
        self.plan(provider)
        self.remote.remove(10)
        before = self.cache_rows()
        self.assertEqual(self.plan(provider, plain_lists=True)[0]['status'], 'missing')
        self.assertEqual(self.cache_rows(), before)

    def test_other_namespaces_and_formats_survive_pruning(self):
        self.local.add(1, 'Removed', b'aaaa')
        third = FixtureLibrary(self.local.root.parent / 'third', LOCAL_UUID)
        third.add(1, 'Other library', b'zzzz')
        provider = self.provider()
        provider.digest(third.catalog()[0])
        local_book = self.local.catalog()[0]
        extra = self.local.root / 'extra.mobi'
        extra.write_bytes(b'extra format')
        mobi = dict(local_book, path=extra, relative_path=Path('extra.mobi'), format='MOBI')
        provider.digest(mobi)
        self.plan(provider)
        self.local.remove(1)
        self.assertEqual(self.plan(provider), [])
        rows = self.cache_rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(any(row[1] == str(third.root.resolve()) for row in rows))
        self.assertTrue(any(row[3] == 'MOBI' for row in rows))

    def test_pruning_failure_rolls_back_and_does_not_change_the_plan(self):
        self.local.add(1, 'Keep', b'aaaa')
        self.local.add(2, 'Removed', b'bbbb')
        provider = self.provider()
        self.plan(provider)
        self.local.remove(2)
        with contextlib.closing(sqlite3.connect(self.cache_path)) as connection:
            connection.execute("""CREATE TRIGGER fail_prune BEFORE DELETE ON digests
                               BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
            connection.commit()
        before = self.cache_rows()
        self.assertEqual(self.plan(provider)[0]['status'], 'missing')
        self.assertEqual(self.cache_rows(), before)
        self.assertFalse(provider.persistence_enabled)
        self.assertEqual(len(self.messages), 1)

    def test_timestamp_preserving_edit_requires_rehash(self):
        source = self.local.add(1, 'Same', b'aaaa')
        self.remote.add(10, 'Same', b'aaaa')
        self.plan(self.provider())
        attributes = source.stat()
        source.write_bytes(b'bbbb')
        os.utime(source, ns=(attributes.st_atime_ns, attributes.st_mtime_ns))
        self.assertEqual(self.plan(self.provider())[0]['status'], 'present')
        self.assertEqual(self.plan(self.provider(rehash=True))[0]['status'], 'review')
        self.assertEqual(self.plan()[0]['status'], 'review')

    @staticmethod
    def stop_planning(done, total):
        raise RuntimeError('stop planning')

    def test_owned_provider_is_closed_after_success(self):
        self.local.add(1, 'New', b'aaaa')
        provider = cache.DigestProvider()
        self.addCleanup(provider.close)
        with patch.object(planner, 'DigestProvider', return_value=provider):
            self.plan()
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            provider.digest(self.local.catalog()[0])

    def test_owned_provider_is_closed_after_error(self):
        self.local.add(1, 'New', b'aaaa')
        provider = cache.DigestProvider()
        self.addCleanup(provider.close)
        with patch.object(planner, 'DigestProvider', return_value=provider):
            with self.assertRaisesRegex(RuntimeError, 'stop planning'):
                self.plan(progress=self.stop_planning)
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            provider.digest(self.local.catalog()[0])

    def test_borrowed_provider_remains_open_after_success(self):
        self.local.add(1, 'New', b'aaaa')
        provider = self.provider()
        with patch.object(provider, 'close', wraps=provider.close) as closed:
            self.plan(provider)
        closed.assert_not_called()
        self.assertTrue(provider.persistence_enabled)
        book = self.local.catalog()[0]
        self.assertEqual(provider.digest(book), library.digest(book['path']))

    def test_borrowed_provider_remains_open_after_error(self):
        self.local.add(1, 'New', b'aaaa')
        provider = self.provider()
        with patch.object(provider, 'close', wraps=provider.close) as closed:
            with self.assertRaisesRegex(RuntimeError, 'stop planning'):
                self.plan(provider, progress=self.stop_planning)
        closed.assert_not_called()
        self.assertTrue(provider.persistence_enabled)
        book = self.local.catalog()[0]
        self.assertEqual(provider.digest(book), library.digest(book['path']))

    def test_missing_identity_catalogs_cannot_open_storage_or_prune_other_entries(self):
        self.local.add(1, 'Keep cached', b'aaaa')
        self.plan(self.provider())
        before = self.cache_rows()
        self.local.execute('DELETE FROM library_id')
        self.remote.execute('DELETE FROM library_id')
        for populated in (True, False):
            provider = self.provider()
            if not populated:
                self.local.remove(1)
            with self.subTest(populated=populated), \
                    patch.object(provider.store, '_open', side_effect=AssertionError('Storage opened')), \
                    patch.object(provider.store, 'prune_epubs', side_effect=AssertionError('Storage pruned')):
                result = self.plan(provider)
            self.assertEqual(len(result), int(populated))
            self.assertEqual(self.cache_rows(), before)


if __name__ == '__main__':
    unittest.main()
