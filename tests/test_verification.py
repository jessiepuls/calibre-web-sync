"""Targeted, fresh upload verification with disposable server libraries."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from calibre_web_sync import cache, cli, engine, library
from calibre_web_sync.cache_store import SQLiteDigestStore
from test_planner_cache import FixtureLibrary, LOCAL_UUID, REMOTE_UUID


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = FixtureLibrary(self.root / 'remote', REMOTE_UUID)
        self.path = self.remote.add(10, 'Uploaded', b'uploaded epub')
        self.sha = hashlib.sha256(b'uploaded epub').hexdigest()
        self.database = self.root / 'cache' / 'hashes.sqlite3'

    def test_lookup_ignores_unrelated_missing_and_escaping_paths(self):
        other = self.remote.add(20, 'Unrelated', b'other')
        other.unlink()
        self.remote.execute("UPDATE books SET path='../outside' WHERE id=20")
        with patch.object(Path, 'stat', autospec=True, side_effect=Path.stat) as stat:
            book = library.lookup_epub(self.remote.root, 10)
        self.assertEqual(book['id'], 10)
        self.assertEqual(book['library'].uuid, REMOTE_UUID)
        self.assertFalse(any('outside' in str(call.args[0]) for call in stat.call_args_list))
        self.assertIsNone(library.lookup_epub(self.remote.root, 999))

    def test_lookup_does_not_authorize_pruning_and_matches_catalog_record(self):
        self.assertEqual(library.lookup_epub(self.remote.root, 10), self.remote.catalog()[0])
        with cache.DigestProvider(SQLiteDigestStore(self.database)) as provider:
            book = library.lookup_epub(self.remote.root, 10)
            provider.digest(book)
            self.assertEqual(provider.prune_catalog(book), 0)

    def test_lookup_preserves_containment_permission_and_snapshot_errors(self):
        self.remote.execute("UPDATE books SET path='../outside'")
        with self.assertRaisesRegex(RuntimeError, 'escapes library'):
            library.lookup_epub(self.remote.root, 10)
        with patch.object(library, 'snapshot', side_effect=PermissionError('Denied')):
            with self.assertRaises(PermissionError):
                library.lookup_epub(self.remote.root, 10)
        (self.remote.root / 'metadata.db-wal').touch()
        with self.assertRaisesRegex(RuntimeError, 'WAL/journal'):
            library.lookup_epub(self.remote.root, 10)

    def test_verification_reads_fresh_bytes_and_seeds_without_second_hash(self):
        with cache.DigestProvider(SQLiteDigestStore(self.database)) as provider:
            book = library.lookup_epub(self.remote.root, 10)
            provider.digest(book)
            # Even a valid persistent/per-run hit must not bypass verification.
            with patch.object(engine, 'stable_digest', wraps=library.stable_digest) as hashed:
                engine.verify_upload(self.remote.root, 10, self.sha, digest_provider=provider)
            self.assertEqual(hashed.call_count, 1)
        with cache.DigestProvider(SQLiteDigestStore(self.database)) as provider:
            with patch.object(cache, 'stable_digest', side_effect=AssertionError('Seed should be reusable')):
                self.assertEqual(provider.digest(library.lookup_epub(self.remote.root, 10)), self.sha)
            self.assertEqual(provider.stats.persistent_hits, 1)

    def test_delayed_row_and_file_visibility_use_fresh_snapshots(self):
        real_lookup = library.lookup_epub
        calls = []

        def lookup(root, bid):
            calls.append(bid)
            if len(calls) == 1:
                return real_lookup(root, 999)
            if len(calls) == 2:
                self.path.unlink()
                return real_lookup(root, bid)
            self.path.write_bytes(b'uploaded epub')
            return real_lookup(root, bid)

        with patch.object(engine, 'lookup_epub', side_effect=lookup), patch.object(engine.time, 'sleep') as sleep:
            engine.verify_upload(self.remote.root, 10, self.sha)
        self.assertEqual(calls, [10, 10, 10])
        self.assertEqual(sleep.call_count, 2)

    def test_mismatch_and_absent_output_stop_without_seeding(self):
        for bid, sha in ((10, '0' * 64), (999, self.sha)):
            with self.subTest(bid=bid):
                with cache.DigestProvider(SQLiteDigestStore(self.database)) as provider:
                    with patch.object(engine.time, 'sleep') as sleep, patch.object(provider, 'remember_verified') as seed:
                        with self.assertRaisesRegex(RuntimeError, 'could not be verified'):
                            engine.verify_upload(self.remote.root, bid, sha, digest_provider=provider)
                    self.assertEqual(sleep.call_count, 5)
                    seed.assert_not_called()
        self.assertFalse(self.database.exists())

    def test_disabled_provider_does_not_touch_persistent_storage(self):
        with cache.DigestProvider() as provider, patch.object(SQLiteDigestStore, '_open', side_effect=AssertionError('No storage')):
            engine.verify_upload(self.remote.root, 10, self.sha, digest_provider=provider)
        self.assertFalse(self.database.parent.exists())

    def test_cache_save_failure_does_not_invalidate_successful_verification(self):
        # An unknown database is left intact and hashing/verification still succeeds.
        self.database.parent.mkdir()
        self.database.write_bytes(b'not a cache')
        warnings = []
        with cache.DigestProvider(SQLiteDigestStore(self.database), warn=warnings.append) as provider:
            engine.verify_upload(self.remote.root, 10, self.sha, digest_provider=provider)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(self.database.read_bytes(), b'not a cache')

    def test_changed_file_cannot_be_seeded(self):
        book = library.lookup_epub(self.remote.root, 10)
        result = library.stable_digest(book['path'])
        self.path.write_bytes(b'changed content')
        with cache.DigestProvider(SQLiteDigestStore(self.database)) as provider:
            with self.assertRaisesRegex(RuntimeError, 'changed while checking'):
                provider.remember_verified(book, result)
        self.assertFalse(self.database.exists())


class UploadSequenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.local = FixtureLibrary(self.root / 'local', LOCAL_UUID)
        self.remote = FixtureLibrary(self.root / 'remote', REMOTE_UUID)
        self.local.add(1, 'First', b'first')
        self.local.add(2, 'Second', b'second')
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'local': str(self.local.root), 'remote': str(self.remote.root), 'cache_dir': str(self.root / 'cache')}))

    def run_cli(self):
        with patch('sys.argv', ['sync.py', '--config', str(self.config), '--apply']), patch.object(cli, 'HERE', self.root), contextlib.redirect_stdout(io.StringIO()):
            cli.main()

    def test_each_upload_is_verified_before_next_and_summary_does_not_rescan(self):
        events = []

        def upload(path):
            bid = 10 + len(events)
            events.append('upload')
            self.remote.add(bid, 'Uploaded', path.read_bytes())
            return bid

        def verify(*args, **kwargs):
            events.append('verify')
            return engine.verify_upload(*args, **kwargs)

        with patch.object(cli, 'authenticated_client', return_value=Mock(upload=upload)), patch.object(cli, 'verify_upload', side_effect=verify), patch.object(cli, 'catalog', wraps=library.catalog) as catalogs:
            self.run_cli()
        self.assertEqual(events, ['upload', 'verify', 'upload', 'verify'])
        self.assertEqual(catalogs.call_count, 2)  # Initial planning only.
        with cache.DigestProvider(SQLiteDigestStore(self.root / 'cache' / 'hashes.sqlite3')) as provider:
            with patch.object(cache, 'stable_digest', side_effect=AssertionError('Server hashes were seeded')):
                for book in self.remote.catalog():
                    provider.digest(book)

    def test_verification_failure_stops_before_second_upload(self):
        client = Mock()
        client.upload.return_value = 10
        with patch.object(cli, 'authenticated_client', return_value=client), patch.object(engine.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'could not be verified'):
                self.run_cli()
        self.assertEqual(client.upload.call_count, 1)

    def test_source_change_after_planning_prevents_upload(self):
        def login(args):
            (self.local.root / '1' / 'book.epub').write_bytes(b'changed')
            return client

        client = Mock()
        with patch.object(cli, 'authenticated_client', side_effect=login):
            with self.assertRaisesRegex(RuntimeError, 'changed since planning'):
                self.run_cli()
        client.upload.assert_not_called()
