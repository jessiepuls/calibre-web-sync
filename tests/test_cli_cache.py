"""Cache configuration and command-line isolation using disposable libraries."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from calibre_web_sync import cache, cli
from test_planner_cache import FixtureLibrary, LOCAL_UUID, REMOTE_UUID


class CacheOptionsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / 'config.json'

    def parse(self, settings, *options):
        self.config.write_text(json.dumps(settings))
        argv = ['sync.py', '--config', str(self.config), '--remote', '/remote', *options]
        with patch('sys.argv', argv), contextlib.redirect_stderr(io.StringIO()):
            return cli.parse_args()[1]

    def test_enable_disable_precedence(self):
        for configured in (None, False, True):
            settings = {} if configured is None else {'cache': configured}
            for flags, expected in (([], configured is not False), (['--cache'], True), (['--no-cache'], False)):
                with self.subTest(configured=configured, flags=flags):
                    self.assertEqual(self.parse(settings, *flags).cache, expected)

    def test_rehash_requires_enabled_cache(self):
        for settings, flags in (({}, ['--no-cache', '--rehash']), ({'cache': False}, ['--rehash']), ({}, ['--cache', '--no-cache'])):
            with self.subTest(settings=settings, flags=flags), self.assertRaises(SystemExit):
                self.parse(settings, *flags)
        self.assertTrue(self.parse({}, '--rehash').rehash)
        self.assertTrue(self.parse({'cache': False}, '--cache', '--rehash').rehash)

    def test_configuration_types_and_unknown_keys(self):
        for settings in ({'cache': 'false'}, {'cache': 0}, {'cache': None}, {'cache_dir': False}, {'remote': 2}, {'unknown': 'value'}, []):
            with self.subTest(settings=settings), self.assertRaises(SystemExit):
                self.parse(settings)

    def test_cache_directory_override_does_not_enable_cache(self):
        args = self.parse({'cache': False, 'cache_dir': '/configured'}, '--cache-dir', '/override')
        self.assertFalse(args.cache)
        self.assertEqual(args.cache_dir, Path('/override'))

    def test_platform_cache_paths(self):
        with patch.object(Path, 'home', return_value=Path('/home/test')):
            with patch.object(cache.sys, 'platform', 'darwin'), patch.dict(os.environ, {'XDG_CACHE_HOME': '/ignored'}):
                self.assertEqual(cache.default_cache_dir(), Path('/home/test/Library/Caches/calibre-web-sync'))
            with patch.object(cache.sys, 'platform', 'linux'), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(cache.default_cache_dir(), Path('/home/test/.cache/calibre-web-sync'))
                with patch.dict(os.environ, {'XDG_CACHE_HOME': '/custom'}):
                    self.assertEqual(cache.default_cache_dir(), Path('/custom/calibre-web-sync'))
                with patch.dict(os.environ, {'XDG_CACHE_HOME': 'relative'}):
                    self.assertEqual(cache.default_cache_dir(), Path('/home/test/.cache/calibre-web-sync'))

    def run_cli(self, *options):
        argv = ['sync.py', '--config', str(self.config), *options]
        output = io.StringIO()
        with patch('sys.argv', argv), patch.object(cli, 'HERE', self.root), contextlib.redirect_stdout(output):
            cli.main()
        return output.getvalue()

    def libraries(self):
        local = FixtureLibrary(self.root / 'local', LOCAL_UUID)
        remote = FixtureLibrary(self.root / 'remote', REMOTE_UUID)
        self.local, self.remote = local, remote
        local.add(1, 'Book', b'epub')
        remote.add(2, 'Book', b'epub')
        self.config.write_text(json.dumps({'local': str(local.root), 'remote': str(remote.root), 'cache_dir': str(self.root / 'cache')}))
        return self.root / 'cache' / 'hashes.sqlite3'

    def test_dry_run_populates_cache_and_warm_run_avoids_hashing(self):
        database = self.libraries()
        self.run_cli()
        self.assertTrue(database.is_file())
        with patch.object(cache, 'stable_digest', side_effect=AssertionError('Warm cache must avoid hashing')):
            self.run_cli()
        with patch.object(cache, 'stable_digest', wraps=cache.stable_digest) as hashed:
            self.run_cli('--rehash')
        self.assertEqual(hashed.call_count, 2)

    def test_no_cache_neither_creates_nor_changes_storage(self):
        database = self.libraries()
        self.run_cli('--no-cache')
        self.assertFalse(database.parent.exists())
        self.run_cli()
        before = database.read_bytes()
        with patch.object(cli, 'SQLiteDigestStore', side_effect=AssertionError('Disabled storage')):
            self.run_cli('--no-cache')
        self.assertEqual(database.read_bytes(), before)

    def test_check_login_never_constructs_storage(self):
        self.libraries()
        with patch.object(cli, 'SQLiteDigestStore', side_effect=AssertionError('Login-only storage')), patch.object(cli, 'authenticated_client'):
            self.run_cli('--check-login')
        self.assertFalse((self.root / 'cache').exists())

    def test_provider_closes_when_catalog_scan_fails(self):
        self.libraries()
        with patch.object(cli, 'catalog', side_effect=RuntimeError('Scan failed')), patch.object(cli.SQLiteDigestStore, 'close') as close:
            with self.assertRaisesRegex(RuntimeError, 'Scan failed'):
                self.run_cli()
        close.assert_called_once()

    def test_summary_reports_cold_warm_disabled_and_rehash_work(self):
        self.libraries()
        cold = self.run_cli()
        self.assertIn('cache: enabled | persistent hits: 0 | misses: 2', cold)
        self.assertIn('hashed: 2 files, 8 bytes', cold)
        warm = self.run_cli()
        self.assertIn('persistent hits: 2 | misses: 0', warm)
        self.assertIn('hashed: 0 files, 0 bytes', warm)
        disabled = self.run_cli('--no-cache')
        self.assertIn('cache: disabled | persistent hits: 0 | misses: 0', disabled)
        self.assertIn('hashed: 2 files, 8 bytes', disabled)
        refreshed = self.run_cli('--rehash')
        self.assertIn('persistent hits: 0 | misses: 2', refreshed)
        self.assertIn('hashed: 2 files, 8 bytes', refreshed)

    def test_summary_includes_snapshots_in_elapsed_time(self):
        self.libraries()
        events = []
        real_catalog = cli.catalog

        def tick():
            events.append('clock')
            return 10.0 if len(events) == 1 else 13.25

        def catalog(root):
            events.append('catalog')
            return real_catalog(root)

        with patch.object(cli.time, 'perf_counter', side_effect=tick), patch.object(cli, 'catalog', side_effect=catalog):
            output = self.run_cli()
        self.assertEqual(events, ['clock', 'catalog', 'catalog', 'clock'])
        self.assertIn('Comparison: 3.25s', output)

    def test_summary_reports_in_run_reuse(self):
        self.libraries()
        # Both local books compare against the same server candidate.
        self.local.add(3, 'Duplicate', b'epub')
        output = self.run_cli()
        self.assertIn('in-run reuse: 1 | hashed: 3 files, 12 bytes', output)

    def test_cache_failure_summary_and_disabled_retry_preserve_corrupt_file(self):
        database = self.libraries()
        database.parent.mkdir()
        database.write_bytes(b'unknown data')
        with self.assertLogs(cache.LOGGER, level='WARNING') as warnings:
            output = self.run_cli()
        self.assertEqual(len(warnings.output), 1)
        self.assertIn('cache: unavailable', output)
        self.assertIn('hashed: 2 files, 8 bytes', output)
        self.assertIn('cache: disabled', self.run_cli('--no-cache'))
        self.assertEqual(database.read_bytes(), b'unknown data')

    def test_server_deletion_is_visible_in_warm_summary(self):
        self.libraries()
        self.run_cli()
        self.remote.remove(2)
        output = self.run_cli()
        self.assertIn('missing: 1 | present: 0', output)
        self.assertIn('persistent hits: 1 | misses: 0', output)
        self.assertIn('hashed: 0 files, 0 bytes', output)
