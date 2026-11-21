"""Checks that lead-facing docs match the code (wave-4 fix round 1, owner group "other").

* README-DEV.txt deploy/rollback runbook (finding r1-operability-04). tools/ and state/export.db are deployed and
  rolled back TOGETHER. The test runs the runbook's own export.db commands (backup, restore, drop column) on a
  pre-deploy fixture that the real Publisher.open_rw has migrated, and shows that each one makes the old publisher's
  positional INSERT work again. It also checks that every state file the new tools use is either a pre-deploy file
  or is moved aside in rollback step R4.
* docs/SPEC.md §14 (v1.3) and docs/WRITEBACK_SPEC.md (finding r1-contract-consistency-04). The adoption publish rule
  must match L-16: the whole refetched snapshot is published, with --only-ids only while a paused round exists or as
  the out-of-plan fallback. The sortspec statement must match what sortspec.build_sortspec generates.

Run from the sandbox root: python3 -I -B -m unittest discover -s tools/tests -p test_docs_consistency.py
The docs live next to tools/ only in the dev sandbox, so the tests skip when a doc file is absent.
"""
import ast
import inspect
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
WORK = os.path.dirname(TOOLS)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
import export_obsidian as exp  # noqa: E402
import publish_vault as pv  # noqa: E402
import wb_locks  # noqa: E402
from sortspec import build_sortspec  # noqa: E402

README = os.path.join(WORK, "README-DEV.txt")
SPEC = os.path.join(WORK, "docs", "SPEC.md")
WBSPEC = os.path.join(WORK, "docs", "WRITEBACK_SPEC.md")

# The pre-deploy production publisher, as recorded in understand/pipeline.md §1.1 (pv:89-97 schema, pv:217-222
# _record, pv:308-309 run log), reports/A2.md:173 and test_wb_state.py:869. It uses POSITIONAL 6-value INSERTs.
OLD_PF_COLS = ["rel_path", "sha256", "notion_id", "kind", "run_id", "status"]
OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS published_files(rel_path TEXT PRIMARY KEY, sha256 TEXT, notion_id TEXT, kind TEXT,
  run_id TEXT, status TEXT);
CREATE INDEX IF NOT EXISTS pf_obj ON published_files(notion_id, kind);
"""
OLD_INSERT = "INSERT OR REPLACE INTO published_files VALUES(?,?,?,?,?, 'active')"
OLD_PUBLISHED = "SELECT rel_path, sha256, notion_id, kind FROM published_files WHERE status='active'"
# State files the pre-deploy tools use themselves (understand/pipeline.md §2.1: rf:105-113 refresh.lock,
# rf:121-129 roots_<ts>.json, rf:156 latest_snapshot.txt; export.db). The rollback must keep them in place.
OLD_STATE = {"export.db", "latest_snapshot.txt", "refresh.lock"}
TIMERS = ("notion-obsidian-refresh-incremental.timer", "notion-obsidian-refresh-full.timer")


def read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def between(text, start, end=None):
    i = text.index(start)
    j = text.index(end, i + len(start)) if end else len(text)
    return text[i:j]


def cols(db):
    con = sqlite3.connect(db)
    try:
        return [r[1] for r in con.execute("PRAGMA table_info(published_files)")]
    finally:
        con.close()


def backup_copy(src, dst):
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def state_file_names():
    """Names joined onto a state directory anywhere in tools/*.py (os.path.join(<...state...>, "<name>")), plus
    the wb_locks lock files. Module-level string constants used as the name are resolved."""
    found = {}
    for fn in sorted(os.listdir(TOOLS)):
        if not fn.endswith(".py"):
            continue
        tree = ast.parse(read(os.path.join(TOOLS, fn)))
        consts = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
                  and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
                  for t in n.targets if isinstance(t, ast.Name)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and ast.unparse(node.func) == "os.path.join" and len(node.args) >= 2):
                continue
            if "state" not in ast.unparse(node.args[0]).lower():
                continue
            a = node.args[1]
            val = (a.value if isinstance(a, ast.Constant) and isinstance(a.value, str)
                   else consts.get(a.id) if isinstance(a, ast.Name) else None)
            if val and re.fullmatch(r"[A-Za-z0-9_.-]+", val):
                found.setdefault(val, set()).add(f"{fn}:{node.lineno}")
    for name in wb_locks.LOCK_NAMES:
        found.setdefault(f"{name}.lock", set()).add("wb_locks.py:LOCK_NAMES")
    return found


class DeployRunbookTests(unittest.TestCase):
    """r1-operability-04: README-DEV.txt must not describe deployment as "copy tools/ back" only."""

    def setUp(self):
        if not os.path.isfile(README):
            self.skipTest("README-DEV.txt absent (not the dev sandbox)")
        self.text = read(README)
        self.assertIn("\nDEPLOY\n", self.text)
        self.assertIn("\nROLLBACK", self.text)
        self.deploy = between(self.text, "\nDEPLOY\n", "\nROLLBACK")
        self.rollback = between(self.text, "\nROLLBACK")
        self.tmp = tempfile.mkdtemp(prefix="docs_runbook_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def cmd(self, section, needle):
        """The single `python3 -I -B -c '<code>' args...` line of `section` that contains `needle`, as argv."""
        lines = [ln.strip() for ln in section.splitlines() if ln.strip().startswith("python3 -I -B -c ") and needle in ln]
        self.assertEqual(len(lines), 1, f"expected one runbook command containing {needle!r}, got {lines}")
        argv = shlex.split(lines[0])
        self.assertEqual(argv[:4], ["python3", "-I", "-B", "-c"])
        return argv

    def run_cmd(self, argv, mapping):
        args = [mapping.get(a, a) for a in argv[5:]]
        for a in args:
            self.assertNotIn("$", a, f"unmapped runbook argument {a!r}")
        r = subprocess.run([sys.executable, "-I", "-B", "-c", argv[4]] + args, cwd=self.tmp, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_runbook_covers_state_not_only_tools(self):
        stop = [ln for ln in self.deploy.splitlines() if "systemctl --user stop" in ln]
        self.assertTrue(stop and all(t in stop[0] for t in TIMERS), "D2 must stop both timers")
        self.assertIn("flock -n state/refresh.lock", self.deploy, "D2 must wait for the refresh lock")
        self.assertIn('cp -a tools "$B/tools"', self.deploy, "D3 must back up tools/")
        self.assertIn('state/export.db "$B/export.db"', self.deploy, "D3 must back up state/export.db")
        self.assertIn("config/writeback.json", self.deploy, "D4 must create config/writeback.json")
        self.assertIn("7 columns but 6 values were supplied", self.text, "the tools-only rollback failure is named")
        self.assertIn('cp -a "$B/tools" tools', self.rollback, "R2 must restore the old tools")
        self.assertIn('"$B/export.db" state/export.db', self.rollback, "R3(a) must restore the pre-deploy export.db")
        self.assertIn("DROP COLUMN staged_sha256", self.rollback, "R3(b) must name the column-drop alternative")
        self.assertIn("flock -n state/writeback.lock", self.rollback, "R1 must also wait for the write-back lock")

    def test_documented_export_db_commands_restore_the_old_publisher(self):
        state = os.path.join(self.tmp, "state")
        bdir = os.path.join(self.tmp, "B")
        os.makedirs(state)
        os.makedirs(bdir)
        live = os.path.join(state, "export.db")
        con = sqlite3.connect(live)
        con.executescript(OLD_SCHEMA)
        con.execute(OLD_INSERT, ("General/A.md", "s-a", "nid-a", "page", "R0"))
        con.execute(OLD_INSERT, ("General/B.md", "s-b", "nid-b", "page", "R0"))
        con.commit()
        con.close()
        pre_rows = sqlite3.connect(live).execute(OLD_PUBLISHED + " ORDER BY rel_path").fetchall()

        # D3: the runbook's backup command
        out = self.run_cmd(self.cmd(self.deploy, "s.backup(d)"),
                           {"state/export.db": live, "$B/export.db": os.path.join(bdir, "export.db")})
        self.assertIn("ok", out)
        self.assertIn(repr(OLD_PF_COLS), out)
        self.assertEqual(cols(os.path.join(bdir, "export.db")), OLD_PF_COLS)

        # first applied round of the NEW publisher: the real Publisher.open_rw (PUB_SCHEMA + ensure_export_db)
        pub = pv.Publisher.__new__(pv.Publisher)
        pub.rw, pub.db, pub.state = False, None, live
        pub._persist_intents = lambda: None
        pv.Publisher.open_rw(pub)
        pub.db.execute("INSERT OR REPLACE INTO published_files(rel_path, sha256, notion_id, kind, run_id, status,"
                       " staged_sha256) VALUES(?,?,?,?,?,?,?)", ("General/C.md", "s-c", "nid-c", "page", "R1",
                                                                "active", "s-c"))
        pub.db.commit()
        pub.db.close()
        self.assertEqual(cols(live), OLD_PF_COLS + ["staged_sha256"])

        # tools-only rollback: the old publisher's first INSERT fails (the hazard the runbook names)
        con = sqlite3.connect(live)
        with self.assertRaisesRegex(sqlite3.OperationalError, "7 columns but 6 values were supplied"):
            con.execute(OLD_INSERT, ("General/A.md", "s-a2", "nid-a", "page", "R2"))
        con.close()

        # R3(a): restore the pre-deploy copy with the runbook's command
        db_a = os.path.join(self.tmp, "a", "state", "export.db")
        os.makedirs(os.path.dirname(db_a))
        backup_copy(live, db_a)
        self.run_cmd(self.cmd(self.rollback, '"$B/export.db" state/export.db'),
                     {"$B/export.db": os.path.join(bdir, "export.db"), "state/export.db": db_a})
        self.assertEqual(cols(db_a), OLD_PF_COLS)
        con = sqlite3.connect(db_a)
        self.assertEqual(con.execute(OLD_PUBLISHED + " ORDER BY rel_path").fetchall(), pre_rows)
        con.executescript(OLD_SCHEMA)
        con.execute(OLD_INSERT, ("General/A.md", "s-a2", "nid-a", "page", "R2"))
        con.commit()
        con.close()

        # R3(b): keep the current rows, drop only the new column, with the runbook's command
        db_b = os.path.join(self.tmp, "b", "state", "export.db")
        os.makedirs(os.path.dirname(db_b))
        backup_copy(live, db_b)
        out = self.run_cmd(self.cmd(self.rollback, "DROP COLUMN staged_sha256"), {"state/export.db": db_b})
        self.assertIn(repr(OLD_PF_COLS), out)
        self.assertEqual(cols(db_b), OLD_PF_COLS)
        con = sqlite3.connect(db_b)
        con.executescript(OLD_SCHEMA)
        con.execute(OLD_INSERT, ("General/A.md", "s-a2", "nid-a", "page", "R2"))
        con.commit()
        rows = dict((r[0], r[1]) for r in con.execute(OLD_PUBLISHED))
        con.close()
        self.assertEqual(rows, {"General/A.md": "s-a2", "General/B.md": "s-b", "General/C.md": "s-c"},
                         "R3(b) keeps the rows published after deployment")

    def test_rollback_moves_every_new_state_file(self):
        found = state_file_names()
        self.assertTrue({"writeback.db", "auto_sync_hold.json", "export.db", "writeback.lock"} <= set(found),
                        f"state-file scan is broken: {sorted(found)}")
        r4 = between(self.rollback, " R4.", " R5.")
        loop = [ln for ln in r4.splitlines() if ln.strip().startswith('mkdir "$R/state"; for f in ')]
        self.assertEqual(len(loop), 1, "R4 needs one mv loop")
        moved = set(re.search(r"for f in (.*?); do", loop[0]).group(1).split())
        missing = {n: sorted(found[n]) for n in sorted(set(found) - OLD_STATE - moved)}
        self.assertEqual(missing, {}, "state files used only by the new tools must be moved aside in rollback R4 "
                                      "(README-DEV.txt); add them to the mv loop")
        self.assertFalse(moved & OLD_STATE, "R4 must not move files the old tools need")
        for name in sorted(OLD_STATE) + ["roots_*.json"]:
            self.assertIn(name, r4.split("Keep", 1)[1], f"R4 must say that {name} stays")


class SpecV13ConsistencyTests(unittest.TestCase):
    """r1-contract-consistency-04: SPEC v1.3 §14 and WRITEBACK_SPEC vs L-16 and the sortspec generator."""

    OLD_ADOPT_CLAIM = "写回后的定向发布用 `--adopt-writeback` + `--only-ids`"
    OLD_SORTSPEC_CLAIM = "sortspec 增加 `Notion_Obsidian` 文件夹的配置节"
    NO_SECTIONS = "sortspec 不为 `Notion_Obsidian`"

    def setUp(self):
        if not (os.path.isfile(SPEC) and os.path.isfile(WBSPEC)):
            self.skipTest("docs/SPEC.md or docs/WRITEBACK_SPEC.md absent")
        self.spec14 = between(read(SPEC), "\n## 14.")
        self.wb = read(WBSPEC)

    @staticmethod
    def clauses(text):
        return [c for c in re.split(r"[；。\n]", text) if c.strip()]

    def test_adoption_publish_rule_is_l16(self):
        self.assertNotIn(self.OLD_ADOPT_CLAIM, self.spec14)
        adopt = [c for c in self.clauses(self.spec14) if "--adopt-writeback" in c]
        self.assertTrue(adopt, "SPEC §14 must state how the adoption publish runs")
        self.assertTrue(any("整个" in c and "快照" in c for c in adopt),
                        f"SPEC §14 must say the whole refetched snapshot is published (L-16): {adopt}")
        for c in self.clauses(self.spec14):
            if "--only-ids" in c:
                self.assertTrue("paused_for_review" in c or "计划外" in c,
                                f"--only-ids is only for a paused round or the out-of-plan fallback (L-16): {c}")
        # the binding rule itself (WB §11 step 4) is unchanged
        self.assertIn("存在未放行的 paused_for_review 轮次时，以 `--only-ids` 只采纳计划内笔记", self.wb)

    def stub_exporter(self):
        class Stub:
            pass
        ex = Stub()
        ex.ts_roots = {ts: [] for ts in exp.TEAMSPACES}
        ex.folder_owner, ex.tpl_folder_owner, ex.nodes = {}, {}, {}
        ex.folder_children = lambda folder: exp.Exporter.folder_children(ex, folder)
        return ex

    def test_sortspec_statement_matches_generator(self):
        self.assertEqual(exp.RENDERER_VERSION, "1.2.1",
                         "renderer changed: re-check the sortspec statement in SPEC §14 §9 / WRITEBACK_SPEC 5.1a, "
                         "then update this test")
        # call the generator exactly as the exporter does (the task sections are gated on the renderer version)
        frozen = getattr(exp, "renderer_bytes_frozen", lambda: True)()
        kw = {"task_sections": not frozen} if "task_sections" in inspect.signature(build_sortspec).parameters else {}
        spec = build_sortspec(self.stub_exporter(), **kw)
        targets = [ln.strip()[len("target-folder: "):] for ln in spec.splitlines()
                   if ln.strip().startswith("target-folder: ")]
        self.assertIn("/", targets)
        self.assertIn(f"  /folders {exp.TASK_DIR}", spec, "the root section lists the task folder")
        task_sections = [t for t in targets if t == exp.TASK_DIR or t.startswith(exp.TASK_DIR + "/")]
        task_lines = [ln for ln in self.spec14.splitlines() if "Notion_Obsidian" in ln and "配置节" in ln]
        wb_lines = [ln for ln in self.wb.splitlines() if "Notion_Obsidian" in ln and "配置节" in ln]
        self.assertTrue(task_lines and wb_lines)
        if not task_sections:
            self.assertNotIn(self.OLD_SORTSPEC_CLAIM, self.spec14)
            self.assertIn(self.NO_SECTIONS, self.spec14)
            for ln in wb_lines:
                self.assertTrue(any(m in ln for m in ("~~", "实施说明", "见 5.1a")),
                                f"WRITEBACK_SPEC still promises Notion_Obsidian sortspec sections: {ln[:120]}")
        else:
            self.assertNotIn(self.NO_SECTIONS, self.spec14,
                             f"sortspec now generates {task_sections}: update SPEC §14 §9 and WRITEBACK_SPEC 5.1a")


if __name__ == "__main__":
    unittest.main()
