"""Read-only snapshot opens in the incremental refresh, using private fixtures only."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import export_obsidian
import refresh
import snapshot
import trash_archive
import verify_vault
import wb_common


class SnapshotConnections(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name, 'snapshot #?%.db')
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE objects(id TEXT, kind TEXT, data TEXT, fetched_at TEXT);
                CREATE TABLE work(id TEXT, kind TEXT, status TEXT);
                CREATE TABLE meta(key TEXT, value TEXT);
                INSERT INTO meta VALUES('finished_at', 'fixture');
            """)

    def test_uri_preserves_filename_and_sidecar_decision(self):
        expected = self.path.as_uri() + '?mode=ro'
        self.assertEqual(wb_common.snapshot_read_uri(self.path), expected + '&immutable=1')
        self.assertEqual(wb_common.snapshot_read_uri(self.path, allow_immutable=False), expected)
        for suffix in ('-wal', '-shm', '-journal'):
            with self.subTest(suffix=suffix):
                sidecar = Path(str(self.path) + suffix)
                sidecar.touch()
                self.assertEqual(wb_common.snapshot_read_uri(self.path), expected + '&immutable=1')
                sidecar.unlink()

    def test_all_refresh_readers_use_read_only_uri_with_lone_shm(self):
        path = str(self.path)
        Path(path + '-shm').touch()
        expected = self.path.as_uri() + '?mode=ro&immutable=1'
        with patch.object(sqlite3, 'connect', wraps=sqlite3.connect) as connect:
            with patch.object(snapshot.BlobInventory, '__init__', return_value=None):
                previous = snapshot.IncrementalInventory(prev_path=path)
                previous.prev.close()
            self.assertEqual(refresh.snapshot_meta(path), {'finished_at': 'fixture'})
            trash_archive.open_snapshot(path).close()
            exporter = export_obsidian.Exporter.__new__(export_obsidian.Exporter)
            exporter._open_snapshot(path, immutable=True)
            exporter.snap.close()
            db, mode = verify_vault.open_ro(path, force_immutable=True)
            db.close()
            self.assertEqual(mode, 'mode=ro&immutable=1')
        self.assertEqual(connect.call_count, 5)
        for call in connect.call_args_list:
            self.assertEqual(call.args[0], expected)
            self.assertIs(call.kwargs['uri'], True)

    def test_wal_with_committed_frames_and_missing_shm_fails_without_creating_it(self):
        # Copy an active SQLite WAL without its index. Run as the ordinary service user.
        directory = self.path.parent / "without shm"
        directory.mkdir()
        target = directory / self.path.name
        with closing(sqlite3.connect(self.path)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO meta VALUES('new_key', 'only in WAL')")
            writer.commit()
            shutil.copy2(self.path, target)
            shutil.copy2(str(self.path) + "-wal", str(target) + "-wal")
        self.assertGreater(os.stat(str(target) + "-wal").st_size, 0)
        for permissions in (0o755, 0o555):
            directory.chmod(permissions)
            try:
                with self.subTest(permissions=oct(permissions)):
                    with self.assertRaisesRegex(OSError, r"-shm") as error:
                        refresh.snapshot_meta(target)
                    self.assertIn(str(target), str(error.exception))
                    self.assertIn("checkpoint", str(error.exception))
                    self.assertFalse(Path(str(target) + "-shm").exists())
            finally:
                directory.chmod(0o755)
                Path(str(target) + "-shm").unlink(missing_ok=True)

    @unittest.skipIf(os.geteuid() == 0, "Unix permissions require a non-root reader")
    def test_unreadable_database_in_readonly_directory_names_file_and_permissions(self):
        directory = self.path.parent / "readable directory"
        directory.mkdir()
        target = directory / self.path.name
        shutil.copy2(self.path, target)
        target.chmod(0)
        directory.chmod(0o555)
        try:
            self.assertFalse(os.access(target, os.R_OK))
            with self.assertRaisesRegex(OSError, "permissions") as error:
                refresh.snapshot_meta(target)
            self.assertIn(str(target), str(error.exception))
        finally:
            directory.chmod(0o755)
            target.chmod(0o644)

    def test_export_refuses_nonempty_rollback_journal_for_finished_snapshot(self):
        Path(str(self.path) + '-journal').write_bytes(b'unfinished rollback')
        exporter = export_obsidian.Exporter.__new__(export_obsidian.Exporter)
        with self.assertRaisesRegex(SystemExit, r"-journal"):
            exporter._open_snapshot(self.path, immutable=True)

    def test_existing_nonempty_wal_guards(self):
        p = str(self.path)
        with closing(sqlite3.connect(p)) as writer:
            writer.execute('pragma journal_mode=WAL')
            writer.execute("INSERT INTO meta VALUES('from_wal','committed')")
            writer.commit()
            self.assertGreater(os.stat(p + '-wal').st_size, 0)
            self.assertEqual(refresh.snapshot_meta(p).get('from_wal'), 'committed')
            with self.assertRaises(trash_archive.SnapshotRefused):
                trash_archive.open_snapshot(p)
            with self.assertRaises(verify_vault.InputError):
                verify_vault.open_ro(p, force_immutable=True)
            exporter = export_obsidian.Exporter.__new__(export_obsidian.Exporter)
            with self.assertRaises(SystemExit):
                exporter._open_snapshot(p, immutable=True)

    def test_existing_nonempty_journal_guards(self):
        p = str(self.path)
        Path(p + '-journal').write_bytes(b'fixture simulating a nonempty journal')
        with self.assertRaises(trash_archive.SnapshotRefused):
            trash_archive.open_snapshot(p)
        with self.assertRaises(verify_vault.InputError):
            verify_vault.open_ro(p, force_immutable=True)

    def test_symlink_checks_sidecars_next_to_database_target(self):
        alias = self.path.with_name('alias.db')
        alias.symlink_to(self.path)
        p = str(alias)
        for suffix in ('-wal', '-journal'):
            with self.subTest(sidecar=suffix):
                sidecar = Path(str(self.path) + suffix)
                sidecar.write_bytes(b'nonempty test sidecar')
                try:
                    self.assertEqual(wb_common.snapshot_read_uri(alias), self.path.as_uri() + '?mode=ro')
                    with self.assertRaises(trash_archive.SnapshotRefused):
                        trash_archive.open_snapshot(p)
                    with self.assertRaises(verify_vault.InputError):
                        verify_vault.open_ro(p, force_immutable=True)
                    if suffix == '-wal':
                        exporter = export_obsidian.Exporter.__new__(export_obsidian.Exporter)
                        with self.assertRaises(SystemExit):
                            exporter._open_snapshot(p, immutable=True)
                        with self.assertRaisesRegex(RuntimeError, 'checkpointed'):
                            export_obsidian.Exporter.for_planning(p, 'unused-export.db')
                    self.assertTrue(verify_vault.snapshot_is_live(p))
                    self.assertIn(str(sidecar), verify_vault.snapshot_sidecars_with_data(p))
                    self.assertEqual(verify_vault.snapshot_signature(p), verify_vault.snapshot_signature(str(self.path)))
                finally:
                    sidecar.unlink()
