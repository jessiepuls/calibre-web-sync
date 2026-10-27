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
from calibre_web_sync.library import FileFingerprint


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

    def test_parameterized_keys_allow_sql_punctuation(self):
        key = replace(self.key, relative_path="Robert'); DROP TABLE digests;--.epub")
        store = self.store()
        store.save(key, self.entry)
        self.assertEqual(store.lookup(key), self.entry)
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

    def test_failed_initialization_rolls_back_schema(self):
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
        self.assertEqual(self.query('PRAGMA application_id'), [(0,)])
        self.assertEqual(self.query('PRAGMA user_version'), [(0,)])
        self.assertEqual(self.query("SELECT name FROM sqlite_master WHERE type='table'"), [])

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


if __name__ == '__main__':
    unittest.main()
