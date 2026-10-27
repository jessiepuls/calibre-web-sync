"""Library identity, current file attributes, and stable content reads."""
import contextlib
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from calibre_web_sync import library, planner


UUID = '326903a0-15a5-4a37-85a7-6c69cd41dc13'
OTHER_UUID = '426903a0-15a5-4a37-85a7-6c69cd41dc13'


def make_library(root, identity=UUID):
    root.mkdir()
    with contextlib.closing(sqlite3.connect(root / 'metadata.db')) as c:
        c.executescript('''
            CREATE TABLE library_id(uuid TEXT);
            CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT, path TEXT);
            CREATE TABLE authors(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books_authors_link(id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
            CREATE TABLE data(book INTEGER, format TEXT, name TEXT, uncompressed_size INTEGER);
            INSERT INTO books VALUES(1, 'Test book', 'book');
            INSERT INTO authors VALUES(1, 'Test author');
            INSERT INTO books_authors_link VALUES(1, 1, 1);
            INSERT INTO data VALUES(1, 'EPUB', 'book', 999999);
        ''')
        c.execute('INSERT INTO library_id VALUES(?)', (identity,))
        c.commit()
    (root / 'book').mkdir()
    path = root / 'book' / 'book.epub'
    path.write_bytes(b'original ebook')
    return path


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'library'
        self.path = make_library(self.root)

    def edit_database(self, sql, args=()):
        with contextlib.closing(sqlite3.connect(self.root / 'metadata.db')) as c:
            c.execute(sql, args)
            c.commit()

    def test_catalog_exposes_identity_and_live_file_attributes(self):
        book = library.catalog(self.root)[0]
        self.assertEqual(book['library'], library.LibraryIdentity(UUID, self.root.resolve()))
        self.assertEqual(book['library'].namespace, (UUID, str(self.root.resolve())))
        self.assertEqual(book['relative_path'], Path('book/book.epub'))
        self.assertEqual(book['format'], 'EPUB')
        self.assertEqual(book['size'], len(b'original ebook'))
        self.assertEqual(book['mtime_ns'], self.path.stat().st_mtime_ns)
        self.assertEqual(library.file_fingerprint(self.path),
                         library.FileFingerprint(book['size'], book['mtime_ns']))
        self.path.write_bytes(b'changed size')
        self.assertEqual(library.catalog(self.root)[0]['size'], len(b'changed size'))

    def test_scan_cutoff_is_captured_before_snapshot_even_for_empty_catalogs(self):
        snapshot = library.snapshot

        def later_snapshot(root, destination):
            clock.return_value = 200
            snapshot(root, destination)

        for populated in (True, False):
            if not populated:
                self.edit_database('DELETE FROM data')
            with self.subTest(populated=populated), \
                    patch.object(library.time, 'time_ns', return_value=100) as clock, \
                    patch.object(library, 'snapshot', side_effect=later_snapshot):
                result = library.catalog(self.root)
            self.assertEqual(result.scan_started_ns, 100)
            self.assertEqual(len(result), int(populated))

    def test_copied_uuid_at_another_root_is_a_different_namespace(self):
        copied = self.root.parent / 'copy'
        shutil.copytree(self.root, copied)
        original = library.catalog(self.root)[0]['library']
        other = library.catalog(copied)[0]['library']
        self.assertEqual(original.uuid, other.uuid)
        self.assertNotEqual(original.namespace, other.namespace)

    def test_changed_uuid_invalidates_namespace(self):
        before = library.catalog(self.root)[0]['library'].namespace
        self.edit_database('UPDATE library_id SET uuid=?', (OTHER_UUID,))
        self.assertNotEqual(before, library.catalog(self.root)[0]['library'].namespace)

    def test_missing_or_unusable_uuid_disables_namespace_without_breaking_plans(self):
        for value in (None, '', 'not-a-uuid'):
            with self.subTest(value=value):
                self.edit_database('UPDATE library_id SET uuid=?', (value,))
                books = library.catalog(self.root)
                self.assertIsNone(books[0]['library'].namespace)
                self.assertEqual(planner.make_plan(books, books)[0]['status'], 'present')
        self.edit_database('DELETE FROM library_id')
        self.assertIsNone(library.catalog(self.root)[0]['library'].namespace)
        self.edit_database('DROP TABLE library_id')
        self.assertIsNone(library.catalog(self.root)[0]['library'].namespace)

    def test_ambiguous_uuid_is_not_guessed(self):
        self.edit_database('INSERT INTO library_id VALUES(?)', (OTHER_UUID,))
        self.assertIsNone(library.catalog(self.root)[0]['library'].namespace)

    def test_metadata_edit_preserves_identity_and_fingerprint(self):
        before = library.catalog(self.root)[0]
        self.edit_database("UPDATE books SET title='New title'")
        self.edit_database("UPDATE authors SET name='New author'")
        after = library.catalog(self.root)[0]
        self.assertEqual(before['library'], after['library'])
        self.assertEqual((before['size'], before['mtime_ns']),
                         (after['size'], after['mtime_ns']))
        self.assertNotEqual(before['key'], after['key'])

    def test_stable_digest_preserves_string_digest_api(self):
        expected = hashlib.sha256(self.path.read_bytes()).hexdigest()
        result = library.stable_digest(self.path)
        self.assertEqual(result.sha256, expected)
        self.assertEqual(result.fingerprint, library.file_fingerprint(self.path))
        self.assertEqual(library.digest(self.path), expected)

    def hash_during(self, action):
        real_hash = hashlib.sha256()

        class ChangingHash:
            changed = False

            def update(self, chunk):
                real_hash.update(chunk)
                if not self.changed:
                    self.changed = True
                    action()

            def hexdigest(self):
                return real_hash.hexdigest()

        with patch.object(library.hashlib, 'sha256', return_value=ChangingHash()):
            with self.assertRaisesRegex(RuntimeError, 'changed while hashing.*retry'):
                library.stable_digest(self.path)

    def test_size_change_during_read_has_no_digest(self):
        self.hash_during(lambda: self.path.write_bytes(b'larger changed ebook content'))

    def test_same_size_edit_during_read_has_no_digest(self):
        before = self.path.stat()

        def change():
            self.path.write_bytes(b'changed! ebook')
            os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))

        self.assertEqual(len(b'original ebook'), len(b'changed! ebook'))
        self.hash_during(change)

    def test_replacement_with_matching_size_and_mtime_is_detected(self):
        before = self.path.stat()

        def replace():
            replacement = self.path.with_name('replacement.epub')
            replacement.write_bytes(b'changed! ebook')
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            replacement.replace(self.path)

        self.hash_during(replace)

    def test_deletion_during_read_is_detected(self):
        self.hash_during(self.path.unlink)

    def test_missing_file_and_disconnected_library_are_errors(self):
        self.path.unlink()
        with self.assertRaisesRegex(RuntimeError, 'missing EPUB') as missing:
            library.catalog(self.root)
        self.assertIn(str(self.path.resolve()), str(missing.exception))
        with self.assertRaisesRegex(RuntimeError, 'database not found'):
            library.catalog(self.root.parent / 'disconnected')

    def test_unreadable_epub_stops_planning(self):
        books = library.catalog(self.root)
        with patch.object(Path, 'open', side_effect=PermissionError('unreadable EPUB')):
            with self.assertRaises(PermissionError):
                planner.make_plan(books, [])

    def test_symlink_outside_library_is_rejected(self):
        outside = self.root.parent / 'outside.epub'
        self.path.rename(outside)
        self.path.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, 'escapes library'):
            library.catalog(self.root)


if __name__ == '__main__':
    unittest.main()
