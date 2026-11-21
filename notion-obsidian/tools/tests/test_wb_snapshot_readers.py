"""Read-only snapshot consumers must honor SQLite sidecars, without modifying closed inputs."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wb_apply
import wb_plan
import wb_refetch


class WritebackSnapshotReaders(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "finished.db"
        with closing(sqlite3.connect(self.base)) as writer:
            writer.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO meta VALUES ('finished_at', 'first');
                CREATE TABLE objects(id TEXT, kind TEXT, parent_id TEXT);
                INSERT INTO objects VALUES ('one', 'page', NULL);
            """)
        self.ctx = wb_plan.PlanContext(None, {}, None, mode="plan", snapshot=str(self.base))

    def test_quiescent_and_empty_sidecars(self):
        for suffix in (None, "-wal", "-shm", "-journal"):
            with self.subTest(sidecar=suffix):
                side = Path(str(self.base) + suffix) if suffix else None
                if side:
                    side.touch()
                try:
                    with mock.patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connect:
                        self.assertEqual(self.ctx.snapshot_time(), "first")
                        self.assertEqual(wb_apply._snapshot_ids(self.base), {"one": ("page", None)})
                        with closing(wb_refetch.open_immutable(str(self.base))) as refetch_reader:
                            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                                refetch_reader.execute("DELETE FROM objects")
                        expected = {"mode": ["ro"], "immutable": ["1"]}
                        self.assertEqual(connect.call_count, 3)
                        for call in connect.call_args_list:
                            self.assertEqual(parse_qs(urlsplit(call.args[0]).query), expected)
                            self.assertTrue(call.kwargs["uri"])
                finally:
                    if side:
                        side.unlink(missing_ok=True)

    def test_uri_sensitive_characters_in_snapshot_filename(self):
        renamed = self.base.with_name("finished +#?%.db")
        self.base.rename(renamed)
        ctx = wb_plan.PlanContext(None, {}, None, mode="plan", snapshot=str(renamed))
        self.assertEqual(ctx.snapshot_time(), "first")
        self.assertIn("one", wb_apply._snapshot_ids(renamed))
        with closing(wb_refetch.open_immutable(str(renamed))) as reader:
            self.assertEqual(reader.execute("SELECT count(*) FROM objects").fetchone()[0], 1)

    def test_plan_and_object_lookup_see_committed_wal_and_refetch_refuses_it(self):
        with closing(sqlite3.connect(self.base)) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            writer.execute("UPDATE meta SET value='second'")
            writer.execute("INSERT INTO objects VALUES ('two', 'page', 'one')")
            writer.commit()
            self.assertGreater(os.stat(str(self.base) + "-wal").st_size, 0)
            self.assertEqual(self.ctx.snapshot_time(), "second")
            self.assertIn("two", wb_apply._snapshot_ids(self.base))
            alias = self.base.with_name("alias.db")
            alias.symlink_to(self.base)
            for path in (self.base, alias):
                with self.subTest(path=path), self.assertRaisesRegex(wb_refetch.RefetchError, "not empty"):
                    with closing(wb_refetch.open_immutable(path)):
                        pass

    def test_refetch_rejects_nonempty_journal_before_creating_a_copy(self):
        Path(str(self.base) + "-journal").write_bytes(b"unfinished rollback")
        output = self.base.parent / "new.db"
        with self.assertRaisesRegex(wb_refetch.RefetchError, "not empty"):
            wb_refetch.copy_snapshot(str(self.base), str(output))
        self.assertFalse(output.exists())
