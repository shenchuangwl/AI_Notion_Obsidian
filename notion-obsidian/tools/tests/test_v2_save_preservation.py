"""Exercise local rollback, saved-note recovery and adoption with real files, SQLite and worker processes."""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import publish_vault
import rollback
import wb_apply
import wb_config
import wb_state


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save_from_another_process(path, data):
    # Model an editor saving via a new file and atomic rename. The actual hash/swap/undo below still runs.
    code = """import os
from pathlib import Path
import sys
import tempfile
p = Path(sys.argv[1])
fd, tmp = tempfile.mkstemp(dir=p.parent)
with os.fdopen(fd, "wb") as out:
    out.write(sys.stdin.buffer.read())
os.replace(tmp, p)
"""
    subprocess.run([sys.executable, "-I", "-B", "-c", code, str(path)], input=data, check=True, timeout=8)


@contextlib.contextmanager
def at_function_return(schedule):
    # Wait for actual calls to finish before scheduling an independent process. No filesystem primitive is mocked.
    def trace(frame, event, value):
        if event == "return":
            schedule(frame, value)
        return trace
    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        yield
    finally:
        sys.settrace(previous)


class PreservingSaves(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="save-regression-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.note = self.vault / "Note.md"
        self.note.write_bytes(b"published")
        self.bk = self.root / "backup.md"
        self.bk.write_bytes(b"original")
        self.journal = self.root / "publish.jsonl"

    def rollback_args(self, op):
        e = {"phase": "done", "op": op, "rel": self.note.name, "before": sha(b"original"),
             "after": sha(b"published"), "backup": str(self.bk)}
        post = {"rel_path": self.note.name, "sha256": e["after"], "run_id": "test-local", "status": "active"}
        old = dict(post, sha256=e["before"], run_id="earlier")
        e["db_before"] = {self.note.name: old}
        with contextlib.closing(sqlite3.connect(self.state / "export.db")) as db:
            db.execute("CREATE TABLE published_files(rel_path TEXT PRIMARY KEY, sha256 TEXT, run_id TEXT, status TEXT)")
            db.execute("INSERT INTO published_files VALUES(?,?,?,?)", tuple(post.values()))
            db.commit()
        if op == "MOVE":
            e["frm"] = "Older.md"
        self.journal.write_text(json.dumps(e) + "\n", encoding="utf-8")
        return ["--apply", "--run-id", "test-local", "--allow-other-vault", "--vault", str(self.vault),
                "--journal", str(self.journal), "--quarantine", str(self.root / "quarantine"),
                "--log-dir", str(self.root / "log"), "--state", str(self.state / "export.db")]

    def rollback_during_save(self, op):
        argv = self.rollback_args(op)
        saved = []

        def schedule(frame, result):
            if (not saved and frame.f_code is rollback.sha_file.__code__ and result == sha(b"published")
                    and frame.f_back.f_code is rollback.main.__code__):
                save_from_another_process(self.note, b"newest user note")
                saved.append(True)
        with contextlib.redirect_stdout(io.StringIO()) as out, at_function_return(schedule):
            self.assertEqual(rollback.main(argv), 0)
        self.assertTrue(saved)
        with contextlib.closing(sqlite3.connect(self.state / "export.db")) as db:
            self.assertEqual(db.execute("SELECT run_id FROM published_files").fetchone()[0], "test-local")
        self.assertTrue(self.note.is_file(), "new save disappeared from its working path")
        self.assertEqual(self.note.read_bytes(), b"newest user note")
        self.assertFalse((self.vault / "Older.md").exists())
        result = json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual(result["rolled_back"], 0)
        self.assertEqual(result["skipped_at_apply"][0]["rel"], self.note.name)

    def test_restore_keeps_user_save_after_last_hash(self):
        self.rollback_during_save("REPLACE")

    def test_move_back_keeps_user_save_after_last_hash(self):
        self.rollback_during_save("MOVE")

    def test_move_back_restores_backup_to_old_name(self):
        argv = self.rollback_args("MOVE")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rollback.main(argv), 0)
        self.assertFalse(self.note.exists())
        self.assertEqual((self.vault / "Older.md").read_bytes(), b"original")

    def test_move_back_keeps_both_files_when_user_saves_to_both_names(self):
        argv = self.rollback_args("MOVE")
        old = self.vault / "Older.md"
        saved = []

        def schedule(frame, result):
            if (not saved and frame.f_code is publish_vault._rename2.__code__ and result
                    and frame.f_locals["flags"] == publish_vault._RENAME_NOREPLACE):
                save_from_another_process(old, b"save at old name")
                save_from_another_process(self.note, b"save at new name")
                saved.append(True)
        with contextlib.redirect_stdout(io.StringIO()) as out, at_function_return(schedule):
            self.assertEqual(rollback.main(argv), 0)
        self.assertTrue(saved)
        self.assertEqual(old.read_bytes(), b"save at old name")
        self.assertEqual(self.note.read_bytes(), b"save at new name")
        skip = json.loads(out.getvalue().splitlines()[-1])["skipped_at_apply"][0]
        self.assertEqual(set(skip["paths_to_check"]), {old.name, self.note.name})

    def test_local_rollback_obeys_lock_of_other_process(self):
        argv = self.rollback_args("REPLACE")
        code = """import sys
sys.path.insert(0, sys.argv[1])
import wb_locks
with wb_locks.lock("refresh", state_dir=sys.argv[2]):
    print("held", flush=True)
    sys.stdin.buffer.readline()
"""
        with subprocess.Popen([sys.executable, "-I", "-B", "-c", code, str(TOOLS), str(self.state)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE) as owner:
            try:
                self.assertEqual(owner.stdout.readline(), b"held\n")
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(rollback.main(argv), 2)
                self.assertEqual(self.note.read_bytes(), b"published")
            finally:
                owner.communicate(b"release\n", timeout=8)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rollback.main(argv), 0)
        self.assertEqual(self.note.read_bytes(), b"original")

    def local_context(self):
        paths = wb_apply.Paths(str(self.root), str(self.vault))
        db = wb_state.connect(str(self.state / "writeback.db"))
        self.addCleanup(db.close)
        return SimpleNamespace(paths=paths, db=db, notes=[], fm_skipped=set(), plan_id="local-case", cfg=wb_config.defaults(),
                               plan_sha="0" * 64, label="apply", order=[], items={}, item_res={})

    def check_frontmatter(self, double):
        ctx = self.local_context()
        seen = []
        item = {"item_id": "N-001", "rel_path": self.note.name, "l_sha": sha(self.note.read_bytes())}

        def schedule(frame, result):
            if frame.f_code is publish_vault._write_tmp.__code__ and not seen:
                save_from_another_process(self.note, b"user save one")
                seen.append(1)
            if (double and seen == [1] and frame.f_code is publish_vault._rename2.__code__ and result
                    and frame.f_locals["flags"] == publish_vault._RENAME_EXCHANGE):
                save_from_another_process(self.note, b"user save two")
                seen.append(2)
        with at_function_return(schedule):
            out = wb_apply._frontmatter_writeback(ctx, item, "147311cc-5891-428a-92b3-7ad8af64d125")
        self.assertIsNone(out)
        row = ctx.db.execute("SELECT state,committed_ids FROM journal WHERE op='VAULT_FRONTMATTER'").fetchone()
        self.assertEqual((row["state"], json.loads(row["committed_ids"])["outcome"]), ("failed", "sha_changed"))
        self.assertEqual(self.note.read_bytes(), b"user save one")
        if double:
            self.assertEqual(seen, [1, 2])
            kept = Path(json.loads(row["committed_ids"])["race"]["kept"])
            self.assertEqual(kept.read_bytes(), b"user save two")
            report = "\n".join(wb_apply._summary_lines(ctx, {"status": "partially_applied", "notes": ctx.notes}))
            self.assertIn(str(kept), report)
            self.assertIn("另存", report)
            self.assertIn("合并", report)

    def test_frontmatter_save_during_replacement(self):
        self.check_frontmatter(False)

    def test_double_save_recovery_is_in_user_result(self):
        self.check_frontmatter(True)

    def test_undecodable_filename_survives_real_adoption_scan(self):
        ctx = self.local_context()
        raw = os.fsencode(self.vault) + b"/user-\xff.md"
        with open(raw, "wb") as out:
            out.write(b"local private note")
        dev_path = wb_apply._vault_deviations(ctx, str(self.root), "local-scan")
        dev = json.loads(Path(dev_path).read_text())
        self.assertIn(os.fsdecode(b"user-\xff.md"), dev["local_new"])
        self.assertFalse(any("扫描失败" in n for n in ctx.notes))
        with open(raw, "rb") as fh:
            self.assertEqual(fh.read(), b"local private note")
