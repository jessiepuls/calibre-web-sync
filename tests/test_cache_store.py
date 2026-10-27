"""Storage behavior tested independently of digest-provider fallback policy."""
import contextlib
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from calibre_web_sync.cache_store import (
    APPLICATION_ID, SCHEMA_VERSION, CacheEntry, CacheKey, CacheStorageError,
    SQLiteDigestStore,
)
from calibre_web_sync.library import FileFingerprint, LibraryIdentity


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'cache' / 'hashes.sqlite3'
        self.key = CacheKey('326903a0-15a5-4a37-85a7-6c69cd41dc13',
                            str(self.root / 'library'), 'book.epub', 'EPUB')
        self.entry = CacheEntry(FileFingerprint(14, 1000000000),
                                hashlib.sha256(b'original ebook').hexdigest(), 100, 200)

    def store(self):
        store = SQLiteDigestStore(self.path)
        self.addCleanup(store.close)
        return store

    def query(self, sql, args=()):
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            rows = c.execute(sql, args).fetchall()
            c.commit()
            return rows

    def test_lookup_and_save_round_trip_named_records(self):
        store = self.store()
        self.assertFalse(self.path.parent.exists())
        self.assertIsNone(store.lookup(self.key))
        store.save(self.key, self.entry)
        self.assertEqual(store.lookup(self.key), self.entry)
        updated = replace(self.entry, last_seen_ns=300)
        store.save(self.key, updated)
        self.assertEqual(self.store().lookup(self.key), updated)

    def test_lookup_does_not_update_stored_timestamps(self):
        store = self.store()
        store.save(self.key, self.entry)
        before = self.query('SELECT hashed_at_ns,last_seen_ns FROM digests')
        store.lookup(self.key)
        self.assertEqual(self.query('SELECT hashed_at_ns,last_seen_ns FROM digests'), before)

    def test_touch_only_advances_observation_time(self):
        store = self.store()
        store.save(self.key, self.entry)
        store.touch(self.key, self.entry, 400)
        self.assertEqual(store.lookup(self.key), replace(self.entry, last_seen_ns=400))
        # A delayed observer must not move time backward, even with an old entry.
        self.store().touch(self.key, self.entry, 300)
        self.assertEqual(store.lookup(self.key), replace(self.entry, last_seen_ns=400))

    def test_stale_touch_preserves_replacements_from_another_connection(self):
        reader, writer = self.store(), self.store()
        replacements = [
            replace(self.entry, sha256='f' * 64),
            replace(self.entry, fingerprint=FileFingerprint(15, 1000000000)),
            replace(self.entry, fingerprint=FileFingerprint(14, 2000000000)),
            replace(self.entry, algorithm='sha512'),
            replace(self.entry, digest_version=2),
            replace(self.entry, hashed_at_ns=500),
        ]
        for replacement in replacements:
            with self.subTest(replacement=replacement):
                reader.save(self.key, self.entry)
                observed = reader.lookup(self.key)
                writer.save(self.key, replacement)
                reader.touch(self.key, observed, 600)
                self.assertEqual(writer.lookup(self.key), replacement)

    def test_touch_does_not_recreate_a_deleted_entry(self):
        reader, writer = self.store(), self.store()
        reader.save(self.key, self.entry)
        observed = reader.lookup(self.key)
        library = LibraryIdentity(self.key.library_uuid, Path(self.key.library_root))
        writer.prune_epubs(library, set())
        reader.touch(self.key, observed, 600)
        self.assertIsNone(writer.lookup(self.key))

    def test_touch_is_scoped_to_the_full_key(self):
        store = self.store()
        others = [replace(self.key, library_uuid='other-uuid'),
                  replace(self.key, library_root=str(self.root / 'other-library')),
                  replace(self.key, relative_path='other.epub'),
                  replace(self.key, format='MOBI')]
        for key in [self.key] + others:
            store.save(key, self.entry)
        store.touch(self.key, self.entry, 600)
        self.assertEqual(store.lookup(self.key), replace(self.entry, last_seen_ns=600))
        for key in others:
            self.assertEqual(store.lookup(key), self.entry)

    def test_failed_touch_rolls_back_and_store_can_retry(self):
        store = self.store()
        store.save(self.key, self.entry)
        self.query("""CREATE TRIGGER simulate_failed_touch AFTER UPDATE ON digests
                   BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
        with self.assertRaises(CacheStorageError):
            store.touch(self.key, self.entry, 600)
        self.assertEqual(store.lookup(self.key), self.entry)
        self.query('DROP TRIGGER simulate_failed_touch')
        store.touch(self.key, self.entry, 600)
        self.assertEqual(store.lookup(self.key), replace(self.entry, last_seen_ns=600))

    def test_parameterized_keys_allow_sql_punctuation(self):
        key = replace(self.key, relative_path="Robert'); DROP TABLE digests;--.epub")
        store = self.store()
        store.save(key, self.entry)
        store.touch(key, self.entry, 300)
        self.assertEqual(store.lookup(key), replace(self.entry, last_seen_ns=300))
        self.assertEqual(self.query('SELECT count(*) FROM digests'), [(1,)])

    def test_old_schema_is_rejected_without_modifying_it(self):
        self.path.parent.mkdir()
        # Independent DDL, not generated from current implementation text.
        self.query('''CREATE TABLE digests (
            library_uuid TEXT, library_root TEXT, relative_path TEXT, format TEXT,
            size INTEGER, mtime_ns INTEGER, algorithm TEXT, digest_version INTEGER,
            sha256 TEXT,
            PRIMARY KEY (library_uuid,library_root,relative_path,format)
        )''')
        self.query(f'PRAGMA application_id={APPLICATION_ID}')
        self.query('PRAGMA user_version=0')
        before = self.path.read_bytes()
        with self.assertRaises(CacheStorageError):
            self.store().lookup(self.key)
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_initialization_does_not_publish_partial_schema(self):
        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith('PRAGMA user_version='):
                    raise sqlite3.OperationalError('simulated initialization failure')
                return super().execute(sql, *args)
        real_connect = sqlite3.connect

        def failing_connect(*args, **kwargs):
            return real_connect(*args, factory=FailingConnection, **kwargs)

        with patch('calibre_web_sync.cache_store.sqlite3.connect', side_effect=failing_connect):
            with self.assertRaises(CacheStorageError):
                self.store().lookup(self.key)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob('.hash-cache-init-*')), [])
        self.assertIsNone(self.store().lookup(self.key))

    def test_interrupted_transaction_preserves_completed_entry(self):
        store = self.store()
        store.save(self.key, self.entry)
        store._connection.execute('BEGIN IMMEDIATE')
        store._connection.execute('DELETE FROM digests')
        store.close()
        self.assertEqual(self.store().lookup(self.key), self.entry)

    def test_failed_insert_rolls_back_preserving_prior_entries(self):
        store = self.store()
        store.save(self.key, self.entry)
        self.query("""CREATE TRIGGER simulate_failed_write AFTER INSERT ON digests
                   WHEN new.relative_path='second.epub'
                   BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
        with self.assertRaises(CacheStorageError):
            store.save(replace(self.key, relative_path='second.epub'), self.entry)
        self.assertEqual(store.lookup(self.key), self.entry)
        self.assertEqual(self.query('SELECT relative_path FROM digests'), [('book.epub',)])

    def test_invalid_digest_reports_error_without_choosing_fallback(self):
        store = self.store()
        store.save(self.key, self.entry)
        self.query("UPDATE digests SET sha256='invalid'")
        with self.assertRaisesRegex(CacheStorageError, 'Invalid cached digest'):
            store.lookup(self.key)
        # Error policy belongs to the provider: storage remains open until closed.
        self.assertIsNotNone(store._connection)

    def test_missing_identity_cannot_be_persisted(self):
        with self.assertRaisesRegex(ValueError, 'UUID'):
            self.store().save(replace(self.key, library_uuid=None), self.entry)
        self.assertFalse(self.path.parent.exists())

    def test_closed_store_does_not_reopen_database(self):
        store = self.store()
        store.close()
        with self.assertRaisesRegex(CacheStorageError, 'closed'):
            store.lookup(self.key)
        self.assertFalse(self.path.parent.exists())

    def test_failed_open_cannot_bypass_schema_validation_on_retry(self):
        self.store().save(self.key, self.entry)
        self.query('PRAGMA user_version=0')
        store = self.store()
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                with self.assertRaises(CacheStorageError):
                    store.lookup(self.key)
                self.assertIsNone(store._connection)

    def test_schema_metadata_identifies_new_store(self):
        self.store().save(self.key, self.entry)
        self.assertEqual(self.query('PRAGMA application_id'), [(APPLICATION_ID,)])
        self.assertEqual(self.query('PRAGMA user_version'), [(SCHEMA_VERSION,)])

    def test_pruning_is_limited_to_the_observed_library_and_epubs(self):
        store = self.store()
        keep = replace(self.key, relative_path='kept.epub')
        other_root = replace(self.key, library_root=str(self.root / 'other-library'))
        other_uuid = replace(self.key, library_uuid='426903a0-15a5-4a37-85a7-6c69cd41dc13')
        other_format = replace(self.key, format='MOBI')
        for key in (self.key, keep, other_root, other_uuid, other_format):
            store.save(key, self.entry)
        identity = LibraryIdentity(self.key.library_uuid, Path(self.key.library_root))
        self.assertEqual(store.prune_epubs(identity, {keep}), 1)
        self.assertIsNone(store.lookup(self.key))
        for key in (keep, other_root, other_uuid, other_format):
            self.assertEqual(store.lookup(key), self.entry)

    def test_pruning_large_catalogs_avoids_parameter_limits(self):
        store = self.store()
        obsolete = replace(self.key, relative_path='obsolete.epub')
        store.save(self.key, self.entry)
        store.save(obsolete, self.entry)
        if hasattr(store._connection, 'setlimit'):
            store._connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 10)
        observed = {replace(self.key, relative_path=f'book-{number}.epub') for number in range(2000)}
        observed.add(self.key)
        identity = LibraryIdentity(self.key.library_uuid, Path(self.key.library_root))
        self.assertEqual(store.prune_epubs(identity, observed), 1)
        self.assertEqual(store.lookup(self.key), self.entry)
        self.assertIsNone(store.lookup(obsolete))

    def test_failed_pruning_rolls_back_and_can_be_retried(self):
        store = self.store()
        store.save(self.key, self.entry)
        other = replace(self.key, relative_path='second.epub')
        store.save(other, self.entry)
        identity = LibraryIdentity(self.key.library_uuid, Path(self.key.library_root))
        self.query("""CREATE TRIGGER fail_delete BEFORE DELETE ON digests
                   BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
        with self.assertRaises(CacheStorageError):
            store.prune_epubs(identity, set())
        self.assertEqual(store.lookup(self.key), self.entry)
        self.assertEqual(store.lookup(other), self.entry)
        self.query('DROP TRIGGER fail_delete')
        self.assertEqual(store.prune_epubs(identity, set()), 2)
        self.assertEqual(store.prune_epubs(identity, set()), 0)

    def test_pruning_rejects_mixed_namespaces_or_formats_before_opening_storage(self):
        identity = LibraryIdentity(self.key.library_uuid, Path(self.key.library_root))
        keys = [replace(self.key, library_uuid='other'),
                replace(self.key, library_root='other'), replace(self.key, format='MOBI')]
        for key in keys:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    self.store().prune_epubs(identity, {key})
        self.assertFalse(self.path.exists())

    def test_missing_identity_cannot_prune_or_open_storage(self):
        identity = LibraryIdentity(None, Path(self.key.library_root))
        self.assertEqual(self.store().prune_epubs(identity, set()), 0)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
