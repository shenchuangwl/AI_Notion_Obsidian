#!/usr/bin/env python3
"""Tests for publish_vault.py (IMPL_CONTRACT §5.10) and rollback.py.

Run from the task root:
  python3 -I -B -m unittest discover -s tools/tests -p 'test_publish_vault.py' -v
Every fixture (staging runs, vault, export.db, writeback.db, backups, reports) lives in tempfile.mkdtemp();
nothing outside it is written. No network.
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import yaml  # noqa: E402

import publish_vault as pv  # noqa: E402
import rollback as rb  # noqa: E402
import wb_state  # noqa: E402

P_ID = "11111111-1111-4111-8111-000000000001"
A_ID = "11111111-1111-4111-8111-00000000000a"
C_ID = "11111111-1111-4111-8111-00000000000c"
D_ID = "11111111-1111-4111-8111-00000000000d"
E_ID = "11111111-1111-4111-8111-00000000000e"
F_ID = "11111111-1111-4111-8111-00000000000f"
G_ID = "11111111-1111-4111-8111-000000000010"
N_ID = "11111111-1111-4111-8111-000000000011"
X_ID = "11111111-1111-4111-8111-000000000020"
R_ID = "11111111-1111-4111-8111-000000000021"

# key: (notion_id, kind, title, parent, default rel)
NOTES = {
    "P": (P_ID, "page", "Proj", None, "General/Proj/Proj.md"),
    "A": (A_ID, "page", "A", P_ID, "General/Proj/A.md"),
    "C": (C_ID, "page", "C", P_ID, "General/Proj/C.md"),
    "D": (D_ID, "page", "D", P_ID, "General/Proj/D.md"),
    "E": (E_ID, "page", "E", None, "General/E.md"),
    "G": (G_ID, "page", "G", None, "General/G.md"),
    "X": (X_ID, "database", "DB", None, "General/DB/DB.md"),
    "R": (R_ID, "row", "Row1", X_ID, "General/DB/Row1.md"),
}
OPTIONAL = {"F": (F_ID, "page", "F", P_ID, "General/Proj/F.md"),
            "N": (N_ID, "page", "New", P_ID, "General/Proj/New.md")}
BASE_REL = "General/DB/DB.base"
ATT_REL = "General/_attachments/pic__abcd1234.png"
GEN_REL = "Notion_Obsidian/sortspec.md"
BASE_TEXT = """filters:
  and:
    - note.notion_kind == "row"
views:
  - type: table
    name: Main
    order:
      - file.name
      - note.Status
    sort:
      - property: note.notion_order
        direction: ASC
"""
BASE_OBSIDIAN = """filters:
  and:
  - note.notion_kind == "row"
views:
- type: table
  name: Main
  order:
  - file.name
  - Status
  sort:
  - property: notion_order
    direction: ASC
"""


def sha(b):
    return hashlib.sha256(b).hexdigest()


def rbytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def stem(rel):
    return rel[:-3] if rel.endswith(".md") else rel


def note_bytes(nid, kind, title, body, parent=None, extra=None):
    fm = {"notion_id": nid, "notion_kind": kind, "notion_title": title,
          "notion_url": f"https://app.notion.com/p/{nid.replace('-', '')}", "notion_parent_id": parent,
          "notion_teamspace": "General", "notion_order": 1, "notion_created_time": "2026-01-01T00:00:00.000Z",
          "notion_last_edited_time": "2026-01-01T00:00:00.000Z"}
    fm.update(extra or {})
    y = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=100000, default_flow_style=False)
    return ("---\n" + y + "---\n" + ("\n" + body + "\n" if body.strip() else "")).encode("utf-8")


def default_bodies(paths):
    a = stem(paths["A"])
    return {"P": "Folder note body", "A": "Alpha text\n\nSecond paragraph",
            "C": f"See [[{a}|A]] here\n\n| col |\n| --- |\n| [[{a}\\|A]] |\n\n`[[not a link]]`",
            "D": "Delta", "E": "Echo", "G": "Golf", "X": f"![[{BASE_REL}]]", "R": "Row body",
            "F": "Foxtrot", "N": "Brand new page body"}


class Fx:
    """A temp world: staging runs, a vault, export.db (with objects_map), writeback.db, reports, backups."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="pubtest_")
        self.vault = os.path.join(self.root, "vault")
        os.makedirs(self.vault)
        self.state = os.path.join(self.root, "state", "export.db")
        os.makedirs(os.path.dirname(self.state))
        self.wb = os.path.join(self.root, "state", "writeback.db")
        self.staging = os.path.join(self.root, "staging")
        self.approval = os.path.join(self.root, "approval.md")
        with open(self.approval, "w", encoding="utf-8") as fh:
            fh.write("# test approval\n")
        con = sqlite3.connect(self.state)
        con.execute("CREATE TABLE objects_map(notion_id TEXT PRIMARY KEY, kind TEXT, rel_path TEXT, name TEXT, parent_id TEXT,"
                    " teamspace TEXT, ord INT, title TEXT, rendered_sha256 TEXT, published_sha256 TEXT, published_path TEXT,"
                    " published_run TEXT, status TEXT, first_run TEXT, last_run TEXT)")
        for key, (nid, kind, title, parent, rel) in list(NOTES.items()) + list(OPTIONAL.items()):
            con.execute("INSERT INTO objects_map(notion_id, kind, rel_path, parent_id, title, status) VALUES(?,?,?,?,?,'active')",
                        (nid, kind, rel, parent, title))
        con.commit()
        con.close()

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------------ staging
    def world(self, bodies=None, paths=None, drop=(), add=(), extra_fm=None, base_text=BASE_TEXT, att=b"\x89PNG-1",
              gen=b"# sortspec v1\n"):
        """{rel: (bytes, kind, nid)} for one staging run."""
        p = {k: v[4] for k, v in list(NOTES.items()) + list(OPTIONAL.items())}
        p.update(paths or {})
        b = default_bodies(p)
        b.update(bodies or {})
        files = {}
        keys = [k for k in NOTES if k not in drop] + list(add)
        for k in keys:
            nid, kind, title, parent, _ = NOTES.get(k) or OPTIONAL[k]
            ex = dict((extra_fm or {}).get(k, {}))
            if kind == "row":
                ex.setdefault("Status", "Done")
            files[p[k]] = (note_bytes(nid, kind, title, b[k], parent, ex), kind, nid)
        if "X" not in drop:
            files[BASE_REL] = (base_text.encode("utf-8"), "base", X_ID)
        files[ATT_REL] = (att, "attachment", None)
        files[GEN_REL] = (gen, "generated", None)
        return files

    def stage(self, run, files, rejected=None, applied=None, page_json=True, block_lines=True):
        d = os.path.join(self.staging, run)
        man = {}
        pj, bl = [], []
        for rel, (data, kind, nid) in sorted(files.items()):
            p = os.path.join(d, "vault", rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fh:
                fh.write(data)
            s = sha(data)
            man[rel] = {"kind": kind, "notion_id": nid, "sha256": s, "size": len(data)}
            if kind in pv.NOTE_KINDS:
                pj.append({"notion_id": nid, "kind": kind, "rel_path": rel, "sha256": s,
                           "page_json_sha": sha((nid + "|pj|" + s).encode()), "renderer_version": "1.2.1"})
                for i, typ in enumerate(("paragraph", "gap")):
                    bl.append({"notion_id": nid, "base_sha": s, "renderer_version": "1.2.1",
                               "block_id": None if typ == "gap" else f"{nid[:8]}-b", "owner_page_id": nid,
                               "parent_block_id": None, "ord": i, "depth": 0, "type": typ, "own_start": 14 + i,
                               "own_end": 14 + i, "line_start": 14 + i, "line_end": 14 + i, "prefix": "", "zero_line": 0,
                               "klass": None if typ == "gap" else "R?", "x_reason": "gap" if typ == "gap" else None,
                               "json_sha": None if typ == "gap" else sha(nid.encode()), "seq": i})
        with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(man, fh, ensure_ascii=False, indent=0, sort_keys=True)
        if page_json:
            with open(os.path.join(d, "page_json.jsonl"), "w", encoding="utf-8") as fh:
                fh.writelines(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in pj)
        if block_lines:
            with open(os.path.join(d, "block_lines.jsonl"), "w", encoding="utf-8") as fh:
                fh.writelines(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in bl)
        rep = {"renderer_version": "1.2.1"}
        if rejected is not None or applied is not None:
            rep["overrides"] = {"given": len(rejected or []) + len(applied or []), "applied": applied or [],
                                "rejected": rejected or []}
        with open(os.path.join(d, "export_report.json"), "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False)
        return man

    # ------------------------------------------------------------ running
    def argv(self, run, *extra, apply=True):
        a = ["--run-id", run, "--vault", self.vault, "--state", self.state, "--allow-other-vault",
             "--report-dir", self.rep(run), "--staging-dir", os.path.join(self.staging, run),
             "--backup-dir", os.path.join(self.root, "backup", run), "--wb-db", self.wb] + list(extra)
        if apply:
            a += ["--apply", "--approval", self.approval]
        return a

    def run(self, run, *extra, apply=True):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = pv.main(self.argv(run, *extra, apply=apply))
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
                err.write(str(exc.code))
        self.last_out, self.last_err = out.getvalue(), err.getvalue()
        return rc

    def publish(self, run, files, *extra, apply=True, **stage_kw):
        self.stage(run, files, **stage_kw)
        return self.run(run, *extra, apply=apply)

    def rep(self, run):
        return os.path.join(self.root, "reports", run)

    def plan(self, run):
        with open(os.path.join(self.rep(run), "publish_plan.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def ops(self, run):
        out = {}
        for a in self.plan(run)["actions"]:
            out.setdefault(a["rel"], []).append(a["op"])
        return out

    def action(self, run, rel, op=None):
        return next(a for a in self.plan(run)["actions"] if a["rel"] == rel and (op is None or a["op"] == op))

    def journal(self, run):
        with open(os.path.join(self.rep(run), "publish_journal.jsonl"), encoding="utf-8") as fh:
            return [json.loads(x) for x in fh if x.strip()]

    def rollback(self, run, *extra, apply=True):
        argv = ["--run-id", run, "--vault", self.vault, "--allow-other-vault",
                "--journal", os.path.join(self.rep(run), "publish_journal.jsonl"),
                "--quarantine", os.path.join(self.root, "quarantine", run), "--log-dir", self.rep(run)] + list(extra)
        argv += ["--apply"] if apply else []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = rb.main(argv)
        self.last_out = out.getvalue()
        return rc

    # ------------------------------------------------------------ vault / state helpers
    def path(self, rel):
        return os.path.join(self.vault, rel)

    def read(self, rel):
        with open(self.path(rel), "rb") as fh:
            return fh.read()

    def write(self, rel, data):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))

    def tree(self, skip_runlogs=False):
        out = {}
        for d, _, files in os.walk(self.vault):
            for f in files:
                p = os.path.join(d, f)
                rel = os.path.relpath(p, self.vault).replace(os.sep, "/")
                if skip_runlogs and rel.startswith("Notion_Obsidian/运行记录/"):
                    continue
                with open(p, "rb") as fh:
                    out[rel] = sha(fh.read())
        return out

    def pf(self):
        con = sqlite3.connect(self.state)
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(published_files)")]
            return {r[0]: dict(zip(cols, r)) for r in con.execute("SELECT * FROM published_files")}
        finally:
            con.close()

    def conflict_rows(self):
        con = sqlite3.connect(self.state)
        try:
            return con.execute("SELECT obj_key, kind, staged_sha, conflict_rel, run_id FROM conflict_copies").fetchall()
        finally:
            con.close()

    def base_rows(self, nid=None):
        if not os.path.exists(self.wb):
            return []
        con = sqlite3.connect(self.wb)
        con.row_factory = sqlite3.Row
        try:
            q = "SELECT * FROM base_history" + (" WHERE notion_id=?" if nid else "") + " ORDER BY notion_id, seq"
            return [dict(r) for r in con.execute(q, (nid,) if nid else ())]
        finally:
            con.close()

    def block_line_count(self, nid, base_sha):
        con = sqlite3.connect(self.wb)
        try:
            return con.execute("SELECT count(*) FROM block_lines WHERE notion_id=? AND base_sha=?", (nid, base_sha)).fetchone()[0]
        finally:
            con.close()

    def jfile(self, name, data):
        p = os.path.join(self.root, name)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        return p


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fx()
        self.addCleanup(self.fx.close)

    def first(self):
        """R1: initial publish of the default world into the empty vault."""
        rc = self.fx.publish("R1", self.fx.world())
        self.assertEqual(rc, 0, self.fx.last_err)
        return self.fx.world()


# ================================================================ basic flow, D-06 / D-07
class TestFirstRunAndIdempotency(Base):
    def test_first_publish_seeds_and_second_run_is_noop(self):
        w = self.first()
        counts = self.fx.plan("R1")["counts"]
        self.assertEqual(counts, {"ADD": len(w)})
        rows = self.fx.pf()
        for rel, (data, kind, nid) in w.items():
            self.assertEqual(rows[rel]["sha256"], sha(data))
            self.assertEqual(rows[rel]["staged_sha256"], sha(data), rel)
        base = self.fx.base_rows()
        notes = [r for r, v in w.items() if v[1] in pv.NOTE_KINDS]
        self.assertEqual(len(base), len(notes))
        self.assertTrue(all(r["seq"] == 1 and r["seed"] == 1 and r["origin"] == "REMOTE" for r in base))
        for rel in notes:
            nid = w[rel][2]
            self.assertEqual(self.fx.block_line_count(nid, sha(w[rel][0])), 2)
            self.assertEqual(wb_state.base_content(sha(w[rel][0]), db=None, base_dir=os.path.join(self.fx.root, "state", "base")),
                             w[rel][0])
        self.assertTrue(os.path.exists(self.fx.path("Notion_Obsidian/运行记录/R1.md")))
        before = self.fx.tree()
        rc = self.fx.publish("R2", self.fx.world())
        self.assertEqual(rc, 0)
        self.assertEqual(set(self.fx.plan("R2")["counts"]), {"UNCHANGED"})
        self.assertEqual(self.fx.tree(), before, "a no-op run writes nothing (no run log either)")
        self.assertEqual(len(self.fx.base_rows()), len(notes), "no new base_history rows / seeds")
        self.assertEqual(self.fx.pf()[pv.TASK_DIR + "/运行记录/R1.md"]["kind"], "runlog")

    def test_plan_only_writes_no_state(self):
        self.fx.stage("R1", self.fx.world())
        os.remove(self.fx.state)
        rc = self.fx.run("R1", apply=False)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.fx.state))
        self.assertFalse(os.path.exists(self.fx.wb))
        self.assertEqual(os.listdir(self.fx.vault), [])
        out = json.loads(self.fx.last_out.strip().splitlines()[-1])
        self.assertEqual(out["plan_sha256"], self.fx.plan("R1")["plan_sha256"])

    def test_plan_only_leaves_export_db_bytes(self):
        self.first()
        self.fx.stage("R2", self.fx.world(bodies={"A": "changed"}))
        h = sha(rbytes(self.fx.state))
        self.assertEqual(self.fx.run("R2", apply=False), 0)
        self.assertEqual(sha(rbytes(self.fx.state)), h)

    def test_replace_appends_base_history_and_block_lines(self):
        self.first()
        w2 = self.fx.world(bodies={"A": "Alpha v2"})
        self.assertEqual(self.fx.publish("R2", w2), 0)
        self.assertEqual(self.fx.ops("R2")["General/Proj/A.md"], ["REPLACE"])
        rows = self.fx.base_rows(A_ID)
        self.assertEqual([(r["seq"], r["seed"], r["run_id"]) for r in rows], [(1, 1, "R1"), (2, 0, "R2")])
        s2 = sha(w2["General/Proj/A.md"][0])
        self.assertEqual(rows[-1]["sha256"], s2)
        self.assertEqual(self.fx.block_line_count(A_ID, s2), 2)
        bk = os.path.join(self.fx.root, "backup", "R2", "General/Proj/A.md")
        self.assertEqual(rbytes(bk), self.fx.world()["General/Proj/A.md"][0])

    def test_unchanged_drift_is_not_recorded(self):
        self.first()
        self.fx.stage("R2", self.fx.world())
        self.assertEqual(self.fx.run("R2", apply=False), 0)
        plan = self.fx.plan("R2")
        self.fx.write("General/E.md", self.fx.read("General/E.md") + b"late edit\n")
        pf = self.fx.jfile("p2.json", plan)
        rc = self.fx.run("R2", "--plan-file", pf, "--plan-sha", plan["plan_sha256"])
        self.assertEqual(rc, 0)
        ops = [j.get("op") for j in self.fx.journal("R2")]
        self.assertIn("SKIP_UNCHANGED_DRIFT", ops)
        self.assertEqual(self.fx.pf()["General/E.md"]["run_id"], "R1")


# ================================================================ pipeline.md §1 scenarios
class TestLocalChanges(Base):
    def test_local_edit_notion_unchanged_is_local_only_without_copy(self):
        self.first()
        edited = self.fx.read("General/Proj/A.md") + b"\nmy local line\n"
        self.fx.write("General/Proj/A.md", edited)
        for run in ("R2", "R3"):
            self.assertEqual(self.fx.publish(run, self.fx.world()), 0)
            self.assertEqual(self.fx.ops(run)["General/Proj/A.md"], ["LOCAL_ONLY"])
            self.assertEqual(self.fx.action(run, "General/Proj/A.md")["variant"], "content")
            self.assertEqual(self.fx.read("General/Proj/A.md"), edited)
            self.assertFalse(os.path.exists(self.fx.path("Notion_Obsidian/conflicts")))
        log = self.fx.read("Notion_Obsidian/运行记录/R2.md").decode()
        self.assertIn("## 待写回的本地修改 (LOCAL_ONLY)", log)
        self.assertIn("`General/Proj/A.md`", log)
        self.assertNotIn("[[", log)
        # L-12: R3's standing items equal R2's -> no new vault run log
        self.assertFalse(os.path.exists(self.fx.path("Notion_Obsidian/运行记录/R3.md")))
        self.assertEqual(len(self.fx.base_rows(A_ID)), 1)

    def test_runlog_standing_items_l12(self):
        """L-12: a standing-only round writes a run log only when the standing set changed or the last log is gone."""
        self.first()
        a = self.fx.read("General/Proj/A.md")
        self.fx.write("General/Proj/A.md", a + b"\nlocal one\n")
        self.assertEqual(self.fx.publish("R2", self.fx.world()), 0)
        self.assertTrue(os.path.exists(self.fx.path("Notion_Obsidian/运行记录/R2.md")))
        self.assertEqual(self.fx.publish("R3", self.fx.world()), 0)
        self.assertFalse(os.path.exists(self.fx.path("Notion_Obsidian/运行记录/R3.md")))
        # the standing set changes (variant stays 'content', but a second note now has a local edit) -> new log
        b_rel = next(r for r, v in self.fx.world().items() if v[1] in pv.NOTE_KINDS and r != "General/Proj/A.md"
                     and not r.startswith("Notion_Obsidian/"))
        self.fx.write(b_rel, self.fx.read(b_rel) + b"\nlocal two\n")
        self.assertEqual(self.fx.publish("R4", self.fx.world()), 0)
        self.assertTrue(os.path.exists(self.fx.path("Notion_Obsidian/运行记录/R4.md")))
        # the last log was removed (e.g. rollback or the user deleted it) -> written again
        os.remove(self.fx.path("Notion_Obsidian/运行记录/R4.md"))
        self.assertEqual(self.fx.publish("R5", self.fx.world()), 0)
        self.assertTrue(os.path.exists(self.fx.path("Notion_Obsidian/运行记录/R5.md")))
        # same standing set as the (present) R5 log -> skipped again (eventful rounds are covered by other tests)
        self.assertEqual(self.fx.publish("R6", self.fx.world()), 0)
        self.assertFalse(os.path.exists(self.fx.path("Notion_Obsidian/运行记录/R6.md")))

    def test_both_changed_one_conflict_copy_across_runs(self):
        self.first()
        edited = self.fx.read("General/Proj/A.md").replace(b"Alpha text", b"Alpha LOCAL")
        self.fx.write("General/Proj/A.md", edited)
        w2 = self.fx.world(bodies={"A": "Alpha NOTION"})
        self.assertEqual(self.fx.publish("R2", w2), 0)
        self.assertEqual(self.fx.ops("R2")["General/Proj/A.md"], ["CONFLICT_BOTH"])
        crel = "Notion_Obsidian/conflicts/R2/General/Proj/A (Notion 版本 R2).md"
        self.assertEqual(self.fx.action("R2", "General/Proj/A.md")["conflict_rel"], crel)
        copy = self.fx.read(crel).decode()
        fm, body, _ = pv.wb_common.split_note(copy)
        fmd = yaml.safe_load(fm)
        self.assertEqual(fmd["notion_kind"], "generated")
        self.assertEqual(fmd["generated_by"], "publish_vault conflict-copy")
        self.assertEqual(fmd["conflict_of_notion_id"], A_ID)
        self.assertEqual(fmd["conflict_run"], "R2")
        self.assertEqual(fmd["conflict_vault_path"], "General/Proj/A.md")
        for k in ("notion_id", "notion_url", "aliases"):
            self.assertNotIn(k, fmd)
        self.assertEqual(body, pv.wb_common.split_note(w2["General/Proj/A.md"][0].decode())[1])
        self.assertEqual(self.fx.read("General/Proj/A.md"), edited)
        self.assertEqual(self.fx.publish("R3", w2), 0)
        a3 = self.fx.action("R3", "General/Proj/A.md")
        self.assertEqual((a3["op"], a3.get("dedup"), a3["conflict_rel"]), ("CONFLICT_BOTH", True, crel))
        self.assertFalse(os.path.exists(self.fx.path("Notion_Obsidian/conflicts/R3")))
        self.assertEqual(len(self.fx.conflict_rows()), 1)
        self.assertEqual(len(self.fx.base_rows(A_ID)), 1, "no base_history row for a conflict")
        self.assertIn("已有冲突副本", self.fx.read("Notion_Obsidian/运行记录/R3.md").decode())

    def test_attachment_conflict_copy_is_renamed_only(self):
        self.first()
        self.fx.write(ATT_REL, b"local image")
        w2 = self.fx.world(att=b"\x89PNG-2")
        self.assertEqual(self.fx.publish("R2", w2), 0)
        a = self.fx.action("R2", ATT_REL)
        self.assertEqual(a["op"], "CONFLICT_BOTH")
        self.assertEqual(a["conflict_rel"], "Notion_Obsidian/conflicts/R2/General/_attachments/pic__abcd1234 (Notion 版本 R2).png")
        self.assertEqual(self.fx.read(a["conflict_rel"]), b"\x89PNG-2")

    def test_local_delete_restored_or_held(self):
        self.first()
        os.remove(self.fx.path("General/Proj/D.md"))
        holds = self.fx.jfile("holds.json", {D_ID: {"reason": "pending_delete", "source": "pending_delete"}})
        self.assertEqual(self.fx.publish("R2", self.fx.world(), "--holds", holds), 0)
        a = self.fx.action("R2", "General/Proj/D.md")
        self.assertEqual(a["op"], "SKIP_HELD")
        self.assertIn("pending_delete", a["reason"])
        self.assertFalse(os.path.exists(self.fx.path("General/Proj/D.md")))
        self.assertIn("## 跳过（暂停/保留）", self.fx.read("Notion_Obsidian/运行记录/R2.md").decode())
        self.assertEqual(self.fx.publish("R3", self.fx.world()), 0)  # mode off: restored (SPEC v1.2 §13)
        a = self.fx.action("R3", "General/Proj/D.md")
        self.assertEqual((a["op"], a["restore"]), ("ADD", True))
        self.assertTrue(os.path.exists(self.fx.path("General/Proj/D.md")))
        self.assertIn("按 Notion 版本恢复", self.fx.read("Notion_Obsidian/运行记录/R3.md").decode())

    def test_hold_covers_subtree(self):
        self.first()
        holds = self.fx.jfile("holds.json", [P_ID])
        w2 = self.fx.world(bodies={"A": "A v2", "C": "C v2", "E": "E v2"})
        self.assertEqual(self.fx.publish("R2", w2, "--holds", holds), 0)
        ops = self.fx.ops("R2")
        for rel in ("General/Proj/Proj.md", "General/Proj/A.md", "General/Proj/C.md", "General/Proj/D.md"):
            self.assertEqual(ops[rel], ["SKIP_HELD"], rel)
        self.assertTrue(self.fx.action("R2", "General/Proj/A.md")["reason"].startswith("hold_subtree:" + P_ID))
        self.assertEqual(ops["General/E.md"], ["REPLACE"])
        self.assertEqual(self.fx.read("General/Proj/A.md"), self.fx.world()["General/Proj/A.md"][0])

    def test_not_a_file_conflict_and_moved_edited_dedupe(self):
        self.first()
        os.remove(self.fx.path("General/E.md"))
        os.symlink(self.fx.path("General/G.md"), self.fx.path("General/E.md"))
        self.fx.write("General/Proj/A.md", self.fx.read("General/Proj/A.md") + b"edit\n")
        w2 = self.fx.world(bodies={"E": "E v2"}, paths={"A": "General/Proj/A moved.md"})
        self.assertEqual(self.fx.publish("R2", w2), 0)
        self.assertEqual(self.fx.ops("R2")["General/E.md"], ["CONFLICT_NOT_FILE"])
        self.assertTrue(os.path.islink(self.fx.path("General/E.md")))
        self.assertTrue(os.path.exists(self.fx.path("Notion_Obsidian/conflicts/R2/General/E (Notion 版本 R2).md")))
        self.assertEqual(self.fx.publish("R3", w2), 0)
        for rel in ("General/E.md", "General/Proj/A moved.md"):
            self.assertTrue(self.fx.action("R3", rel).get("dedup"), rel)
        self.assertFalse(os.path.exists(self.fx.path("Notion_Obsidian/conflicts/R3")))

    def test_hold_without_subtree(self):
        self.first()
        holds = self.fx.jfile("holds.json", {P_ID: {"reason": "auto_sync_hold", "subtree": False}})
        self.assertEqual(self.fx.publish("R2", self.fx.world(bodies={"A": "A v2", "P": "P v2"}), "--holds", holds), 0)
        ops = self.fx.ops("R2")
        self.assertEqual((ops["General/Proj/Proj.md"], ops["General/Proj/A.md"]), (["SKIP_HELD"], ["REPLACE"]))

    def test_rename_with_override_adopt_rename_no_duplicate(self):
        self.first()
        os.rename(self.fx.path("General/Proj/A.md"), self.fx.path("General/Proj/A2.md"))
        ov = self.fx.jfile("ov.json", {A_ID: "General/Proj/A2.md"})
        w2 = self.fx.world(paths={"A": "General/Proj/A2.md"})
        self.assertEqual(self.fx.publish("R2", w2, "--overrides", ov,
                                         applied=[{"notion_id": A_ID, "local_rel": "General/Proj/A2.md",
                                                   "computed_rel": "General/Proj/A.md"}]), 0)
        ops = self.fx.ops("R2")
        self.assertEqual(ops["General/Proj/A2.md"], ["ADOPT_RENAME", "UNCHANGED"])
        self.assertEqual(ops["General/Proj/C.md"], ["REPLACE"])  # link now rendered to local_rel
        self.assertFalse(os.path.exists(self.fx.path("General/Proj/A.md")))
        self.assertEqual(self.fx.plan("R2")["tombstones"], [])
        rows = self.fx.pf()
        self.assertEqual(rows["General/Proj/A.md"]["status"], "renamed")
        self.assertEqual(rows["General/Proj/A2.md"]["status"], "active")
        log = self.fx.read("Notion_Obsidian/运行记录/R2.md").decode()
        self.assertIn("## 命名差异", log)
        self.assertIn("`General/Proj/A.md` → `General/Proj/A2.md`", log)
        self.assertNotIn("双方改名冲突", log)
        self.assertEqual(self.fx.publish("R3", w2, "--overrides", ov), 0)
        self.assertEqual(self.fx.ops("R3")["General/Proj/A2.md"], ["UNCHANGED"])
        self.assertFalse(os.path.exists(self.fx.path("General/Proj/A.md")))
        self.assertEqual([r["rel_path"] for r in self.fx.base_rows(A_ID)], ["General/Proj/A.md", "General/Proj/A2.md"])

    def test_runlog_adopted_then_in_effect_is_same_standing_set_l12(self):
        """L-12: R2 adopts a rename (and keeps a LOCAL_ONLY edit); in R3 the same rename is 'in_effect' -- the standing
        set is unchanged, so R3 writes no vault run log (found in the lead's end-to-end rename rehearsal)."""
        self.first()
        os.rename(self.fx.path("General/Proj/A.md"), self.fx.path("General/Proj/A2.md"))
        self.fx.write("General/E.md", self.fx.read("General/E.md") + b"local edit\n")
        ov = self.fx.jfile("ov.json", {A_ID: "General/Proj/A2.md"})
        w2 = self.fx.world(paths={"A": "General/Proj/A2.md"})
        applied = [{"notion_id": A_ID, "local_rel": "General/Proj/A2.md", "computed_rel": "General/Proj/A.md"}]
        self.assertEqual(self.fx.publish("R2", w2, "--overrides", ov, applied=applied), 0)
        self.assertEqual(self.fx.ops("R2")["General/E.md"], ["LOCAL_ONLY"])
        self.assertTrue(os.path.exists(self.fx.path("Notion_Obsidian/运行记录/R2.md")))
        self.assertEqual(self.fx.publish("R3", w2, "--overrides", ov, applied=applied), 0)
        self.assertEqual(self.fx.ops("R3")["General/E.md"], ["LOCAL_ONLY"])
        self.assertFalse(os.path.exists(self.fx.path("Notion_Obsidian/运行记录/R3.md")))

    def test_override_while_notion_renamed_is_flagged(self):
        self.first()
        os.rename(self.fx.path("General/Proj/A.md"), self.fx.path("General/Proj/A2.md"))
        ov = self.fx.jfile("ov.json", {A_ID: "General/Proj/A2.md"})
        applied = [{"notion_id": A_ID, "local_rel": "General/Proj/A2.md", "computed_rel": "General/Proj/A new.md"}]
        w2 = self.fx.world(paths={"A": "General/Proj/A2.md"})
        self.assertEqual(self.fx.publish("R2", w2, "--overrides", ov, applied=applied), 0)
        self.assertIn("双方改名冲突：Notion 名称现为 `General/Proj/A new.md`",
                      self.fx.read("Notion_Obsidian/运行记录/R2.md").decode())
        self.fx.write("General/E.md", self.fx.read("General/E.md") + b"x\n")  # make R3 eventful (LOCAL_ONLY)
        self.assertEqual(self.fx.publish("R3", w2, "--overrides", ov, applied=applied), 0)
        self.assertIn("命名差异生效中；双方改名冲突", self.fx.read("Notion_Obsidian/运行记录/R3.md").decode())

    def test_obsidian_link_rewrite_is_link_rewrite_only(self):
        self.first()
        os.rename(self.fx.path("General/Proj/A.md"), self.fx.path("General/Proj/A2.md"))
        c = self.fx.read("General/Proj/C.md").replace(b"[[General/Proj/A|A]]", b"[[A2|A]]") \
            .replace(b"[[General/Proj/A\\|A]]", b"[[A2\\|A]]")
        self.assertNotEqual(c, self.fx.read("General/Proj/C.md"))
        self.fx.write("General/Proj/C.md", c)
        ov = self.fx.jfile("ov.json", {A_ID: "General/Proj/A2.md"})
        w2 = self.fx.world(paths={"A": "General/Proj/A2.md"})
        self.assertEqual(self.fx.publish("R2", w2, "--overrides", ov), 0)
        self.assertEqual(self.fx.ops("R2")["General/Proj/C.md"], ["LINK_REWRITE_ONLY"])
        self.assertEqual(self.fx.read("General/Proj/C.md"), w2["General/Proj/C.md"][0])
        self.assertTrue(os.path.exists(os.path.join(self.fx.root, "backup", "R2", "General/Proj/C.md")))
        self.assertFalse(os.path.exists(self.fx.path("Notion_Obsidian/conflicts")))
        self.assertEqual(self.fx.publish("R3", w2, "--overrides", ov), 0)
        self.assertEqual(self.fx.ops("R3")["General/Proj/C.md"], ["UNCHANGED"])

    def test_real_edit_next_to_link_rewrite_is_a_conflict(self):
        self.first()
        os.rename(self.fx.path("General/Proj/A.md"), self.fx.path("General/Proj/A2.md"))
        c = self.fx.read("General/Proj/C.md").replace(b"[[General/Proj/A|A]] here", b"[[A2|A]] there")
        self.fx.write("General/Proj/C.md", c)
        ov = self.fx.jfile("ov.json", {A_ID: "General/Proj/A2.md"})
        self.assertEqual(self.fx.publish("R2", self.fx.world(paths={"A": "General/Proj/A2.md"}), "--overrides", ov), 0)
        self.assertEqual(self.fx.ops("R2")["General/Proj/C.md"], ["CONFLICT_BOTH"])
        self.assertEqual(self.fx.read("General/Proj/C.md"), c)

    def test_override_rejected_is_not_applied(self):
        self.first()
        os.rename(self.fx.path("General/Proj/A.md"), self.fx.path("General/Proj/A2.md"))
        ov = self.fx.jfile("ov.json", {A_ID: "General/Proj/A2.md"})
        rej = [{"notion_id": A_ID, "local_rel": "General/Proj/A2.md", "reason": "name_clash"}]
        self.assertEqual(self.fx.publish("R2", self.fx.world(), "--overrides", ov, rejected=rej), 0)
        a = self.fx.action("R2", "General/Proj/A.md")
        self.assertEqual((a["op"], a["reason"]), ("SKIP_HELD", "override_rejected:name_clash"))
        self.assertFalse(os.path.exists(self.fx.path("General/Proj/A.md")), "never ADD at old_rel")
        self.assertEqual(self.fx.pf()["General/Proj/A.md"]["status"], "active")
        self.assertIn("未生效", self.fx.read("Notion_Obsidian/运行记录/R2.md").decode())

    def test_rename_without_override_no_duplicate(self):
        self.first()
        os.rename(self.fx.path("General/Proj/A.md"), self.fx.path("General/Proj/A2.md"))
        self.fx.write("General/Proj/D (Conflicted copy 2026-11-17 1012).md", self.fx.read("General/Proj/D.md"))
        os.remove(self.fx.path("General/Proj/D.md"))
        self.assertEqual(self.fx.publish("R2", self.fx.world()), 0)
        for rel in ("General/Proj/A.md", "General/Proj/D.md"):
            a = self.fx.action("R2", rel)
            self.assertEqual((a["op"], a["reason"]), ("SKIP_HELD", "duplicate_id_elsewhere"), rel)
            self.assertFalse(os.path.exists(self.fx.path(rel)))

    def test_local_move_other_folder_no_duplicate(self):
        self.first()
        os.makedirs(self.fx.path("General/Elsewhere"))
        os.rename(self.fx.path("General/E.md"), self.fx.path("General/Elsewhere/E.md"))
        self.assertEqual(self.fx.publish("R2", self.fx.world()), 0)
        self.assertEqual(self.fx.action("R2", "General/E.md")["op"], "SKIP_HELD")
        self.assertFalse(os.path.exists(self.fx.path("General/E.md")))

    def test_local_new_file_untouched(self):
        self.first()
        self.fx.write("General/Proj/mine.md", b"# my own note\n")
        self.fx.write("General/photo.jpg", b"jpg")
        before = self.fx.tree()
        self.assertEqual(self.fx.publish("R2", self.fx.world()), 0)
        self.assertEqual(self.fx.tree(), before)
        self.assertNotIn("General/Proj/mine.md", self.fx.ops("R2"))

    def test_local_frontmatter_key_preserved(self):
        self.first()
        orig = self.fx.read("General/E.md").decode()
        fm, body, _ = pv.wb_common.split_note(orig)
        local = "---\n" + fm + "\ntags: [mine, x]\ncssclasses:\n  - wide\n---\n" + body
        self.fx.write("General/E.md", local)
        self.assertEqual(self.fx.publish("R2", self.fx.world()), 0)
        a = self.fx.action("R2", "General/E.md")
        self.assertEqual((a["op"], a["variant"]), ("LOCAL_ONLY", "keys_only"))
        w3 = self.fx.world(bodies={"E": "Echo from Notion v3"})
        self.assertEqual(self.fx.publish("R3", w3), 0)
        a = self.fx.action("R3", "General/E.md")
        self.assertEqual((a["op"], a["keys"]), ("LOCAL_KEYS_ONLY", ["tags", "cssclasses"]))
        now = self.fx.read("General/E.md").decode()
        self.assertIn("Echo from Notion v3", now)
        self.assertIn("tags: [mine, x]\ncssclasses:\n  - wide\n---\n", now, "local keys kept verbatim")
        self.assertTrue(os.path.exists(os.path.join(self.fx.root, "backup", "R3", "General/E.md")))
        row = self.fx.pf()["General/E.md"]
        self.assertEqual(row["staged_sha256"], sha(w3["General/E.md"][0]))
        self.assertEqual(row["sha256"], sha(now.encode()))
        tree = self.fx.tree()
        self.assertEqual(self.fx.publish("R4", w3), 0)
        self.assertEqual(self.fx.ops("R4")["General/E.md"], ["UNCHANGED"])
        self.assertEqual(self.fx.tree(), tree, "after a merge the next run writes nothing (D-07)")
        w5 = self.fx.world(bodies={"E": "Echo v5"})
        self.assertEqual(self.fx.publish("R5", w5), 0)
        self.assertEqual(self.fx.ops("R5")["General/E.md"], ["LOCAL_KEYS_ONLY"])
        self.assertIn("tags: [mine, x]", self.fx.read("General/E.md").decode())
        self.assertIn("Echo v5", self.fx.read("General/E.md").decode())
        self.assertEqual(self.fx.base_rows(E_ID)[-1]["sha256"], sha(w5["General/E.md"][0]),
                         "base_history keeps the staged rendering, not the merged bytes")

    def test_generated_file_overwritten_with_backup(self):
        self.first()
        self.fx.write(GEN_REL, b"# my edits to sortspec\n")
        self.assertEqual(self.fx.publish("R2", self.fx.world(gen=b"# sortspec v2\n")), 0)
        self.assertEqual(self.fx.ops("R2")[GEN_REL], ["GENERATED_OVERWRITE"])
        self.assertEqual(self.fx.read(GEN_REL), b"# sortspec v2\n")
        self.assertEqual(rbytes(os.path.join(self.fx.root, "backup", "R2", GEN_REL)), b"# my edits to sortspec\n")
        self.assertFalse(os.path.exists(self.fx.path("Notion_Obsidian/conflicts")))

    def test_base_reserialised_no_flip_flop(self):
        self.first()
        self.fx.write(BASE_REL, BASE_OBSIDIAN)
        self.assertEqual(self.fx.publish("R2", self.fx.world()), 0)
        self.assertEqual(self.fx.ops("R2")[BASE_REL], ["UNCHANGED_SEMANTIC"])
        self.assertEqual(self.fx.publish("R3", self.fx.world()), 0)
        self.assertEqual(self.fx.ops("R3")[BASE_REL], ["UNCHANGED"])
        self.assertEqual(self.fx.read(BASE_REL), BASE_OBSIDIAN.encode())

    def test_notion_rename_moves_or_conflicts_and_is_no_tombstone(self):
        self.first()
        edited = self.fx.read("General/Proj/A.md") + b"edit\n"
        self.fx.write("General/Proj/A.md", edited)
        w2 = self.fx.world(paths={"A": "General/Proj/A renamed.md", "D": "General/Proj/D renamed.md"})
        self.assertEqual(self.fx.publish("R2", w2), 0)
        ops = self.fx.ops("R2")
        self.assertEqual(ops["General/Proj/A renamed.md"], ["CONFLICT_MOVED_EDITED"])
        self.assertEqual(ops["General/Proj/D renamed.md"], ["MOVE"])
        self.assertEqual(self.fx.plan("R2")["tombstones"], [])
        self.assertEqual(self.fx.read("General/Proj/A.md"), edited)
        self.assertFalse(os.path.exists(self.fx.path("General/Proj/D.md")))
        self.assertEqual(self.fx.read("General/Proj/D renamed.md"), w2["General/Proj/D renamed.md"][0])
        self.assertEqual(self.fx.pf()["General/Proj/D.md"]["status"], "moved")

    def test_notion_move_after_local_delete_supersedes_old_row(self):
        self.first()
        os.remove(self.fx.path("General/Proj/D.md"))
        rows0 = self.fx.pf()
        w2 = self.fx.world(paths={"D": "General/Proj/D renamed.md"})
        self.assertEqual(self.fx.publish("R2", w2), 0)
        a = self.fx.action("R2", "General/Proj/D renamed.md")
        self.assertEqual((a["op"], a["frm"]), ("ADD", "General/Proj/D.md"))
        rows = self.fx.pf()
        self.assertEqual(rows["General/Proj/D.md"]["status"], "moved")
        self.assertEqual([r for r, m in rows.items() if m["notion_id"] == D_ID and m["status"] == "active"],
                         ["General/Proj/D renamed.md"])
        self.assertEqual(self.fx.plan("R2")["tombstones"], [])
        self.assertEqual(self.fx.rollback("R2"), 0)
        self.assertEqual(self.fx.pf()["General/Proj/D.md"], rows0["General/Proj/D.md"])
        self.assertFalse(os.path.exists(self.fx.path("General/Proj/D renamed.md")))

    def test_tombstone_and_trashed_via_writeback(self):
        self.first()
        w2 = self.fx.world(drop=("G", "D"))
        os.remove(self.fx.path("General/G.md"))
        db = wb_state.connect(self.fx.wb)
        db.execute("INSERT INTO pending_deletes(notion_id, rel_path, status) VALUES(?,?,'confirmed')", (G_ID, "General/G.md"))
        db.close()
        self.assertEqual(self.fx.publish("R2", w2), 0)
        plan = self.fx.plan("R2")
        self.assertEqual(plan["tombstones"], ["General/Proj/D.md"])
        self.assertEqual(self.fx.ops("R2")["General/G.md"], ["TRASHED_VIA_WRITEBACK"])
        self.assertEqual(self.fx.pf()["General/G.md"]["status"], "trashed_via_writeback")
        con = sqlite3.connect(self.fx.state)
        self.assertEqual(con.execute("SELECT status FROM objects_map WHERE notion_id=?", (G_ID,)).fetchone()[0],
                         "trashed_via_writeback")
        con.close()
        self.assertTrue(os.path.exists(self.fx.path("General/Proj/D.md")), "tombstoned file kept")
        self.assertEqual(self.fx.publish("R3", w2), 0)
        self.assertEqual(self.fx.plan("R3")["tombstones"], ["General/Proj/D.md"])
        self.assertNotIn("General/G.md", self.fx.ops("R3"))


# ================================================================ adoption
class TestAdopt(Base):
    def test_untracked_note_created_by_writeback_is_adopted(self):
        self.first()
        w2 = self.fx.world(add=("N",))
        staged = w2["General/Proj/New.md"][0]
        fm, body, _ = pv.wb_common.split_note(staged.decode())
        self.fx.write("General/Proj/New.md", f"---\nnotion_id: {N_ID}\nnotion_url: https://x\ntags: [t]\n---\n{body}")
        self.assertEqual(self.fx.publish("R2", w2), 0)
        self.assertEqual(self.fx.ops("R2")["General/Proj/New.md"], ["ADOPT", "LOCAL_KEYS_ONLY"])
        now = self.fx.read("General/Proj/New.md").decode()
        self.assertIn("notion_title: New", now)
        self.assertIn("tags: [t]", now)
        self.assertEqual([r["origin"] for r in self.fx.base_rows(N_ID)], ["WRITEBACK"])
        self.assertEqual(self.fx.publish("R3", w2), 0)
        self.assertEqual(self.fx.ops("R3")["General/Proj/New.md"], ["UNCHANGED"])

    def test_untracked_target_other_content_is_conflict(self):
        self.first()
        w2 = self.fx.world(add=("N",))
        self.fx.write("General/Proj/New.md", "---\nnotion_id: 99999999-1111-4111-8111-000000000099\n---\nmine\n")
        self.assertEqual(self.fx.publish("R2", w2), 0)
        self.assertEqual(self.fx.ops("R2")["General/Proj/New.md"], ["CONFLICT_UNTRACKED"])
        self.assertIn("mine", self.fx.read("General/Proj/New.md").decode())

    def test_adopt_writeback_both_branches(self):
        self.first()
        la = self.fx.read("General/Proj/A.md").replace(b"Alpha text", b"Alpha written back")
        self.fx.write("General/Proj/A.md", la)
        lc = self.fx.read("General/Proj/C.md") + b"written back\n"
        self.fx.write("General/Proj/C.md", lc)
        adopt = {A_ID: {"rel": "General/Proj/A.md", "l_sha": sha(la)},
                 C_ID: {"rel": "General/Proj/C.md", "l_sha": sha(lc)}}
        self.fx.write("General/Proj/C.md", lc + b"edited again after apply\n")
        w2 = self.fx.world(bodies={"A": "Alpha written back (Notion render)", "C": "C as Notion has it"})
        aj = self.fx.jfile("adopt.json", adopt)
        self.assertEqual(self.fx.publish("R2", w2, "--adopt-writeback", aj), 0)
        ops = self.fx.ops("R2")
        self.assertEqual(ops["General/Proj/A.md"], ["ADOPT_WRITEBACK"])
        self.assertEqual(self.fx.read("General/Proj/A.md"), w2["General/Proj/A.md"][0])
        self.assertEqual(rbytes(os.path.join(self.fx.root, "backup", "R2", "General/Proj/A.md")), la)
        self.assertEqual(self.fx.base_rows(A_ID)[-1]["origin"], "WRITEBACK")
        self.assertEqual(ops["General/Proj/C.md"], ["CONFLICT_BOTH"])
        self.assertEqual(self.fx.read("General/Proj/C.md"), lc + b"edited again after apply\n")
        with open(os.path.join(self.fx.rep("R2"), "adopt_needs_manual.json"), encoding="utf-8") as fh:
            nm = json.load(fh)
        self.assertEqual([e["notion_id"] for e in nm], [C_ID])
        self.assertIn("待人工处理", self.fx.read("Notion_Obsidian/运行记录/R2.md").decode())

    def test_adopt_writeback_new_page(self):
        self.first()
        w2 = self.fx.world(add=("N",))
        local = f"---\nnotion_id: {N_ID}\n---\nwhat I typed\n".encode()
        self.fx.write("General/Proj/New.md", local)
        aj = self.fx.jfile("adopt.json", {N_ID: {"rel": "General/Proj/New.md", "l_sha": sha(local)}})
        ids = os.path.join(self.fx.root, "ids.txt")
        with open(ids, "w") as fh:
            fh.write(f"# written pages\n{N_ID}\n")
        self.assertEqual(self.fx.publish("R2", w2, "--adopt-writeback", aj, "--only-ids", ids), 0)
        self.assertEqual(self.fx.ops("R2"), {"General/Proj/New.md": ["ADOPT", "REPLACE"]})
        self.assertEqual(self.fx.read("General/Proj/New.md"), w2["General/Proj/New.md"][0])
        self.assertEqual(self.fx.plan("R2")["tombstones"], [])


# ================================================================ two-step publish, races, --only-ids
class TestPlanFileAndRaces(Base):
    def _plan_r2(self, w2):
        self.fx.stage("R2", w2)
        self.assertEqual(self.fx.run("R2", apply=False), 0)
        return self.fx.plan("R2")

    def test_race_stops_remaining_writes_exit_4(self):
        self.first()
        w2 = self.fx.world(bodies={"A": "A v2", "C": "C v2", "E": "E v2"})
        plan = self._plan_r2(w2)
        self.fx.write("General/Proj/A.md", self.fx.read("General/Proj/A.md") + b"typed meanwhile\n")
        c_before = self.fx.read("General/Proj/C.md")
        pf = self.fx.jfile("plan.json", plan)
        rc = self.fx.run("R2", "--plan-file", pf, "--plan-sha", plan["plan_sha256"])
        self.assertEqual(rc, 4)
        j = self.fx.journal("R2")
        self.assertIn("SKIP_REPLACE_RACE", [x.get("op") for x in j])
        self.assertTrue(any(x.get("event") == "stopped" for x in j))
        self.assertEqual(self.fx.read("General/Proj/C.md"), c_before, "actions after the race are not executed")
        self.assertIn("typed meanwhile", self.fx.read("General/Proj/A.md").decode())
        self.assertIn("提前停止", self.fx.read("Notion_Obsidian/运行记录/R2.md").decode())

    def test_add_race(self):
        self.first()
        w2 = self.fx.world(add=("F",))
        plan = self._plan_r2(w2)
        self.fx.write("General/Proj/F.md", b"user made this\n")
        pf = self.fx.jfile("plan.json", plan)
        self.assertEqual(self.fx.run("R2", "--plan-file", pf, "--plan-sha", plan["plan_sha256"]), 4)
        self.assertEqual(self.fx.read("General/Proj/F.md"), b"user made this\n")

    def test_plan_file_applies_exactly_that_plan(self):
        self.first()
        w2 = self.fx.world(bodies={"A": "A v2"})
        plan = self._plan_r2(w2)
        pf = self.fx.jfile("plan.json", plan)
        with mock.patch.object(pv.Publisher, "plan", side_effect=AssertionError("plan() must not run")):
            rc = self.fx.run("R2", "--plan-file", pf, "--plan-sha", plan["plan_sha256"])
        self.assertEqual(rc, 0, self.fx.last_err)
        self.assertEqual(self.fx.read("General/Proj/A.md"), w2["General/Proj/A.md"][0])

    def test_plan_file_refusals(self):
        self.first()
        w2 = self.fx.world(bodies={"A": "A v2"})
        plan = self._plan_r2(w2)
        tree = self.fx.tree()
        pf = self.fx.jfile("plan.json", plan)
        self.assertEqual(self.fx.run("R2", "--plan-file", pf, "--plan-sha", "0" * 64), 2)
        self.assertIn("--plan-sha mismatch", self.fx.last_err)
        bad = json.loads(json.dumps(plan))
        bad["actions"][0]["op"] = "REPLACE"
        pf2 = self.fx.jfile("plan2.json", bad)
        self.assertEqual(self.fx.run("R2", "--plan-file", pf2, "--plan-sha", plan["plan_sha256"]), 2)
        self.assertIn("modified after planning", self.fx.last_err)
        self.assertEqual(self.fx.run("R2", "--plan-file", pf), 2)  # no --plan-sha
        con = sqlite3.connect(self.fx.state)
        con.execute("UPDATE published_files SET sha256='x' WHERE rel_path='General/E.md'")
        con.commit()
        con.close()
        self.assertEqual(self.fx.run("R2", "--plan-file", pf, "--plan-sha", plan["plan_sha256"]), 2)
        self.assertIn("changed since the plan", self.fx.last_err)
        self.assertEqual(self.fx.tree(), tree)

    def test_staging_drift_stops(self):
        self.first()
        w2 = self.fx.world(bodies={"A": "A v2", "E": "E v2"})
        plan = self._plan_r2(w2)
        with open(os.path.join(self.fx.staging, "R2", "vault", "General/E.md"), "ab") as fh:  # E sorts before A
            fh.write(b"tampered")
        pf = self.fx.jfile("plan.json", plan)
        self.assertEqual(self.fx.run("R2", "--plan-file", pf, "--plan-sha", plan["plan_sha256"]), 4)
        self.assertIn("SKIP_STAGING_RACE", [x.get("op") for x in self.fx.journal("R2")])
        self.assertEqual(self.fx.read("General/E.md"), self.fx.world()["General/E.md"][0])
        self.assertEqual(self.fx.read("General/Proj/A.md"), self.fx.world()["General/Proj/A.md"][0])

    def test_only_ids(self):
        self.first()
        w2 = self.fx.world(bodies={"A": "A v2", "C": "C v2"}, drop=("D",))
        ids = os.path.join(self.fx.root, "ids.txt")
        with open(ids, "w") as fh:
            fh.write(A_ID.replace("-", "") + "\n")
        self.assertEqual(self.fx.publish("R2", w2, "--only-ids", ids), 0)
        self.assertEqual(self.fx.ops("R2"), {"General/Proj/A.md": ["REPLACE"]})
        self.assertEqual(self.fx.plan("R2")["tombstones"], [])
        self.assertEqual(self.fx.read("General/Proj/C.md"), self.fx.world()["General/Proj/C.md"][0])

    def test_bad_inputs_refused(self):
        self.first()
        self.fx.stage("R2", self.fx.world())
        bad = self.fx.jfile("h.json", ["not-an-id"])
        self.assertEqual(self.fx.run("R2", "--holds", bad, apply=False), 2)
        bad = self.fx.jfile("o.json", {A_ID: "/abs/path.md"})
        self.assertEqual(self.fx.run("R2", "--overrides", bad, apply=False), 2)


# ================================================================ rollback
class TestRollback(Base):
    def _busy_run(self):
        """R2 with ADOPT_RENAME, LINK_REWRITE_ONLY, LOCAL_KEYS_ONLY, GENERATED_OVERWRITE, ADOPT+REPLACE, CONFLICT_BOTH,
        ADD, MOVE, TRASHED_VIA_WRITEBACK, REPLACE and a tombstone."""
        self.first()
        fx = self.fx
        os.rename(fx.path("General/Proj/A.md"), fx.path("General/Proj/A2.md"))
        fx.write("General/Proj/C.md", fx.read("General/Proj/C.md").replace(b"[[General/Proj/A|A]]", b"[[A2|A]]")
                 .replace(b"[[General/Proj/A\\|A]]", b"[[A2\\|A]]"))
        e = fx.read("General/E.md").decode()
        fm, body, _ = pv.wb_common.split_note(e)
        fx.write("General/E.md", "---\n" + fm + "\ntags: [k]\n---\n" + body)
        fx.write(GEN_REL, b"# edited\n")
        fx.write("General/Proj/D.md", fx.read("General/Proj/D.md") + b"local d\n")
        os.remove(fx.path("General/G.md"))
        db = wb_state.connect(fx.wb)
        db.execute("INSERT INTO pending_deletes(notion_id, rel_path, status) VALUES(?,?,'confirmed')", (G_ID, "General/G.md"))
        db.close()
        w2 = fx.world(paths={"A": "General/Proj/A2.md", "R": "General/DB/Row One.md"}, add=("F", "N"), drop=("G",),
                      bodies={"E": "Echo v2", "D": "Delta v2", "P": "Proj v2"}, gen=b"# sortspec v2\n")
        n = w2["General/Proj/New.md"][0]
        fx.write("General/Proj/New.md", f"---\nnotion_id: {N_ID}\n---\n" + pv.wb_common.split_note(n.decode())[1])
        fx.write("Notion_Obsidian/conflicts/R2/keep-me.md", b"not created by the run\n")
        ov = fx.jfile("ov.json", {A_ID: "General/Proj/A2.md"})
        return w2, ov

    def test_rollback_undoes_every_new_op_and_replans_identically(self):
        fx = self.fx
        w2, ov = self._busy_run()
        fx.stage("R2", w2)
        self.assertEqual(fx.run("R2", "--overrides", ov, apply=False), 0)
        plan_before = fx.plan("R2")
        ops = {(a["rel"], a["op"]) for a in plan_before["actions"]}
        for want in [("General/Proj/A2.md", "ADOPT_RENAME"), ("General/Proj/C.md", "LINK_REWRITE_ONLY"),
                     ("General/E.md", "LOCAL_KEYS_ONLY"), (GEN_REL, "GENERATED_OVERWRITE"),
                     ("General/Proj/New.md", "ADOPT"), ("General/Proj/New.md", "REPLACE"),
                     ("General/Proj/D.md", "CONFLICT_BOTH"), ("General/Proj/F.md", "ADD"),
                     ("General/DB/Row One.md", "MOVE"), ("General/G.md", "TRASHED_VIA_WRITEBACK"),
                     ("General/Proj/Proj.md", "REPLACE")]:
            self.assertIn(want, ops)
        tree0 = fx.tree()
        rows0 = fx.pf()
        base0 = len(fx.base_rows())
        shutil.rmtree(fx.rep("R2"))
        self.assertEqual(fx.run("R2", "--overrides", ov), 0, fx.last_err)
        self.assertNotEqual(fx.tree(), tree0)
        crel = fx.action("R2", "General/Proj/D.md")["conflict_rel"]
        self.assertTrue(os.path.exists(fx.path(crel)))
        self.assertEqual(fx.rollback("R2", apply=False), 0)
        dry = json.loads(fx.last_out.strip().splitlines()[-1])
        self.assertEqual(dry["n_skipped"], 0, dry)
        self.assertEqual(fx.rollback("R2"), 0)
        self.assertEqual(fx.tree(), tree0, "files exactly as before the run (conflict copy removed, keep-me kept)")
        self.assertTrue(os.path.exists(fx.path("Notion_Obsidian/conflicts/R2/keep-me.md")))
        self.assertFalse(os.path.exists(fx.path(crel)))
        rows = fx.pf()
        for rel in ("General/Proj/A.md", "General/Proj/C.md", "General/E.md", GEN_REL, "General/DB/Row1.md",
                    "General/G.md", "General/Proj/Proj.md"):
            self.assertEqual(rows.get(rel), rows0.get(rel), rel)
        for rel in ("General/Proj/A2.md", "General/Proj/F.md", "General/Proj/New.md", "General/DB/Row One.md",
                    "Notion_Obsidian/运行记录/R2.md"):
            self.assertNotIn(rel, rows, rel)
        self.assertEqual(fx.conflict_rows(), [])
        self.assertEqual(len(fx.base_rows()), base0, "the ADOPT_RENAME re-path row is removed with the rename")
        self.assertEqual(fx.base_rows(A_ID)[-1]["rel_path"], "General/Proj/A.md")
        con = sqlite3.connect(fx.state)
        self.assertEqual(con.execute("SELECT status FROM objects_map WHERE notion_id=?", (G_ID,)).fetchone()[0], "active")
        con.close()
        shutil.move(fx.rep("R2"), fx.rep("R2-applied"))
        self.assertEqual(fx.run("R2", "--overrides", ov, apply=False), 0)
        strip = lambda p: sorted((a["rel"], a["op"], a.get("dedup", False)) for a in p["actions"])  # noqa: E731
        self.assertEqual(strip(fx.plan("R2")), strip(plan_before), "no false conflicts after a rollback")

    def test_rollback_skips_files_changed_after_publish(self):
        self.first()
        w2 = self.fx.world(bodies={"A": "A v2", "E": "E v2"})
        self.assertEqual(self.fx.publish("R2", w2), 0)
        self.fx.write("General/E.md", b"user edit after publish\n")
        self.assertEqual(self.fx.rollback("R2"), 0)
        self.assertEqual(self.fx.read("General/E.md"), b"user edit after publish\n")
        self.assertEqual(self.fx.read("General/Proj/A.md"), self.fx.world()["General/Proj/A.md"][0])
        self.assertEqual(self.fx.pf()["General/E.md"]["run_id"], "R2")

    def test_rollback_adopt_writeback(self):
        self.first()
        la = self.fx.read("General/Proj/A.md") + b"wb\n"
        self.fx.write("General/Proj/A.md", la)
        aj = self.fx.jfile("adopt.json", {A_ID: {"rel": "General/Proj/A.md", "l_sha": sha(la)}})
        rows0 = self.fx.pf()
        self.assertEqual(self.fx.publish("R2", self.fx.world(bodies={"A": "A wb"}), "--adopt-writeback", aj), 0)
        self.assertEqual(self.fx.ops("R2")["General/Proj/A.md"], ["ADOPT_WRITEBACK"])
        self.assertEqual(self.fx.rollback("R2"), 0)
        self.assertEqual(self.fx.read("General/Proj/A.md"), la)
        self.assertEqual(self.fx.pf()["General/Proj/A.md"], rows0["General/Proj/A.md"])
        self.assertEqual(len(self.fx.base_rows(A_ID)), 1)

    def test_legacy_journal_without_db_info(self):
        self.first()
        self.assertEqual(self.fx.publish("R2", self.fx.world(bodies={"A": "A v2"})), 0)
        jp = os.path.join(self.fx.rep("R2"), "publish_journal.jsonl")
        with open(jp, encoding="utf-8") as fh:
            lines = [json.loads(x) for x in fh]
        legacy = []
        for x in lines:
            for k in ("db_before", "backup", "state", "wb_db"):
                x.pop(k, None)
            legacy.append(json.dumps(x, ensure_ascii=False))
        with open(jp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(legacy) + "\n")
        bk_legacy = os.path.join(pv.WORK, "backup", "vault-before", "R2")
        self.assertFalse(os.path.exists(bk_legacy))
        self.assertEqual(self.fx.rollback("R2", apply=False), 0)
        out = json.loads(self.fx.last_out.strip().splitlines()[-1])
        self.assertIsNone(out["db"])
        self.assertEqual(out["n_skipped"], 1, "REPLACE without a backup path cannot be restored")


# ================================================================ pre-D-07 export.db
class TestLegacyExportDb(Base):
    def test_pre_d07_export_db_migrates_and_keeps_working(self):
        fx = self.fx
        w = fx.world()
        fx.stage("R0", w)
        con = sqlite3.connect(fx.state)
        con.executescript("CREATE TABLE published_files(rel_path TEXT PRIMARY KEY, sha256 TEXT, notion_id TEXT, kind TEXT,"
                          " run_id TEXT, status TEXT); CREATE INDEX pf_obj ON published_files(notion_id, kind);")
        for rel, (data, kind, nid) in w.items():
            fx.write(rel, data)
            con.execute("INSERT OR REPLACE INTO published_files VALUES(?,?,?,?,?, 'active')", (rel, sha(data), nid, kind, "R0"))
        con.commit()
        con.close()
        fx.write("General/Proj/A.md", fx.read("General/Proj/A.md") + b"local\n")
        self.assertEqual(fx.publish("R1", w), 0, fx.last_err)
        ops = fx.ops("R1")
        self.assertEqual(ops["General/Proj/A.md"], ["LOCAL_ONLY"], "NULL staged_sha256 = legacy sha256 (no false conflict)")
        others = {r: o for r, o in ops.items() if r != "General/Proj/A.md"}
        self.assertTrue(others and all(o == ["UNCHANGED"] for o in others.values()), others)
        con = sqlite3.connect(fx.state)
        cols = [r[1] for r in con.execute("PRAGMA table_info(published_files)")]
        self.assertIn("staged_sha256", cols)
        self.assertIsNone(con.execute("SELECT staged_sha256 FROM published_files WHERE rel_path='General/Proj/A.md'").fetchone()[0])
        self.assertEqual(con.execute("SELECT staged_sha256 FROM published_files WHERE rel_path='General/E.md'").fetchone()[0],
                         sha(w["General/E.md"][0]))
        con.close()
        self.assertEqual(fx.publish("R2", fx.world(bodies={"E": "E v2"})), 0)
        self.assertEqual(fx.ops("R2")["General/E.md"], ["REPLACE"])
        self.assertEqual(fx.ops("R2")["General/Proj/A.md"], ["LOCAL_ONLY"])


# ================================================================ fix round (adversarial review findings)
_CRASH_CHILD = r"""
import json, os, sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import publish_vault as pv
target, argv = sys.argv[2], json.loads(sys.argv[3])
orig_record, orig_done = pv.Publisher._record, pv.Publisher._intent_done
def rec(self, rel, sha_, a, staged=None):
    if rel == target and a["op"] not in pv.NOOP_OPS:
        os._exit(9)  # hard crash after the vault write, before the DB record / commit
    return orig_record(self, rel, sha_, a, staged)
def done(self, rel):
    if rel.startswith(pv.TASK_DIR + "/conflicts/") and target == "conflict":
        os._exit(9)  # copy written, conflict_copies row not committed
    return orig_done(self, rel)
pv.Publisher._record, pv.Publisher._intent_done = rec, done
pv.main(argv)
"""


class TestReviewFixes(Base):
    def crash_run(self, run, target, *extra):
        r = subprocess.run([sys.executable, "-I", "-B", "-c", _CRASH_CHILD, TOOLS, target,
                            json.dumps(self.fx.argv(run, *extra))], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 9, r.stderr)

    def intents(self):
        con = sqlite3.connect(self.fx.state)
        try:
            return con.execute("SELECT rel_path, op FROM publish_intents").fetchall()
        finally:
            con.close()

    def _move_a_and_hold(self):
        fx = self.fx
        os.makedirs(fx.path("General/Other"))
        os.rename(fx.path("General/Proj/A.md"), fx.path("General/Other/A.md"))
        return fx.jfile("holds.json", {A_ID: {"reason": "unsupported_rename", "source": "scan"}})

    # finding 1: LINK_REWRITE_ONLY must not break a working local link
    def test_link_rewrite_refused_when_it_would_break_a_link(self):
        self.first()
        fx = self.fx
        holds = self._move_a_and_hold()
        c = fx.read("General/Proj/C.md").replace(b"[[General/Proj/A|A]]", b"[[A|A]]") \
            .replace(b"[[General/Proj/A\\|A]]", b"[[A\\|A]]")
        fx.write("General/Proj/C.md", c)
        b = default_bodies({k: v[4] for k, v in list(NOTES.items()) + list(OPTIONAL.items())})
        w2 = fx.world(bodies={"C": b["C"] + "\n\nNotion edit"})
        self.assertEqual(fx.publish("R2", w2, "--holds", holds), 0)
        a = fx.action("R2", "General/Proj/C.md")
        self.assertEqual(a["op"], "CONFLICT_BOTH")
        self.assertIn("would break", a["reason"])
        self.assertEqual(fx.read("General/Proj/C.md"), c, "the user's working links are kept")
        self.assertFalse(os.path.exists(fx.path("General/Proj/A.md")))
        self.assertEqual(fx.ops("R2")["General/Proj/A.md"], ["SKIP_HELD"])

    def test_link_rewrite_refused_for_rejected_override(self):
        self.first()
        fx = self.fx
        os.rename(fx.path("General/Proj/A.md"), fx.path("General/Proj/A2.md"))
        c = fx.read("General/Proj/C.md").replace(b"[[General/Proj/A|A]]", b"[[A2|A]]") \
            .replace(b"[[General/Proj/A\\|A]]", b"[[A2\\|A]]")
        fx.write("General/Proj/C.md", c)
        ov = fx.jfile("ov.json", {A_ID: "General/Proj/A2.md"})
        b = default_bodies({k: v[4] for k, v in list(NOTES.items()) + list(OPTIONAL.items())})
        rej = [{"notion_id": A_ID, "local_rel": "General/Proj/A2.md", "reason": "name_clash"}]
        self.assertEqual(fx.publish("R2", fx.world(bodies={"C": b["C"] + "\n\nmore"}), "--overrides", ov, rejected=rej), 0)
        self.assertEqual(fx.ops("R2")["General/Proj/C.md"], ["CONFLICT_BOTH"])
        self.assertEqual(fx.read("General/Proj/C.md"), c)

    def test_adopt_rewrite_refused_when_it_would_break_a_link(self):
        self.first()
        fx = self.fx
        holds = self._move_a_and_hold()
        w2 = fx.world(add=("N",), bodies={"N": "See [[General/Proj/A|A]]"})
        fx.write("General/Proj/New.md", f"---\nnotion_id: {N_ID}\n---\n\nSee [[A|A]]\n")
        self.assertEqual(fx.publish("R2", w2, "--holds", holds), 0)
        self.assertEqual(fx.ops("R2")["General/Proj/New.md"], ["CONFLICT_UNTRACKED"], "no ADOPT, no REPLACE")
        self.assertNotIn("General/Proj/New.md", fx.pf())

    # finding 2: untracked ADOPT must not overwrite a local property edit
    def test_untracked_adopt_requires_equal_properties(self):
        self.first()
        fx = self.fx
        rel = "General/DB/Row1.md"
        con = sqlite3.connect(fx.state)
        con.execute("DELETE FROM published_files WHERE rel_path=?", (rel,))
        con.commit()
        con.close()
        local = fx.read(rel).replace(b"Status: Done", b"Status: Blocked")
        fx.write(rel, local)
        self.assertEqual(fx.publish("R2", fx.world()), 0)
        a = fx.action("R2", rel)
        self.assertEqual(a["op"], "CONFLICT_UNTRACKED")
        self.assertIn("properties", a["reason"])
        self.assertEqual(fx.read(rel), local)
        fx.write(rel, fx.read(rel).replace(b"Status: Blocked\n", b""))  # an exporter-made note lost a key
        self.assertEqual(fx.publish("R3", fx.world()), 0)
        self.assertEqual(fx.ops("R3")[rel], ["CONFLICT_UNTRACKED"])
        fx.write(rel, fx.world()[rel][0])  # unchanged content: adopted, not a conflict
        self.assertEqual(fx.publish("R4", fx.world()), 0)
        self.assertEqual(fx.ops("R4")[rel], ["UNCHANGED"])

    # finding 3: LOCAL_ONLY notes get a base (first run with a pre-existing local edit; rename + edit)
    def test_first_run_seeds_local_only_notes(self):
        fx = self.fx
        w = fx.world()
        fx.stage("R0", w)
        con = sqlite3.connect(fx.state)
        con.executescript("CREATE TABLE published_files(rel_path TEXT PRIMARY KEY, sha256 TEXT, notion_id TEXT, kind TEXT,"
                          " run_id TEXT, status TEXT);")
        for rel, (data, kind, nid) in w.items():
            fx.write(rel, data)
            con.execute("INSERT INTO published_files VALUES(?,?,?,?,?, 'active')", (rel, sha(data), nid, kind, "R0"))
        con.commit()
        con.close()
        fx.write("General/Proj/A.md", fx.read("General/Proj/A.md") + b"local edit before deploy\n")
        self.assertEqual(fx.publish("R1", w), 0)
        self.assertEqual(fx.ops("R1")["General/Proj/A.md"], ["LOCAL_ONLY"])
        rows = fx.base_rows(A_ID)
        self.assertEqual([(r["seq"], r["seed"], r["sha256"]) for r in rows], [(1, 1, sha(w["General/Proj/A.md"][0]))])
        self.assertEqual(fx.block_line_count(A_ID, rows[0]["sha256"]), 2)
        notes = [r for r, v in w.items() if v[1] in pv.NOTE_KINDS]
        self.assertEqual(len(fx.base_rows()), len(notes), "every note seeded on the first run (D-06)")
        self.assertEqual(fx.publish("R2", w), 0)
        self.assertEqual(len(fx.base_rows()), len(notes), "LOCAL_ONLY adds no row while nothing changed")

    def test_rename_and_edit_repaths_base(self):
        self.first()
        fx = self.fx
        os.rename(fx.path("General/Proj/A.md"), fx.path("General/Proj/A2.md"))
        fx.write("General/Proj/A2.md", fx.read("General/Proj/A2.md") + b"edited after rename\n")
        ov = fx.jfile("ov.json", {A_ID: "General/Proj/A2.md"})
        w2 = fx.world(paths={"A": "General/Proj/A2.md"})
        applied = [{"notion_id": A_ID, "local_rel": "General/Proj/A2.md", "computed_rel": "General/Proj/A.md"}]
        self.assertEqual(fx.publish("R2", w2, "--overrides", ov, applied=applied), 0)
        self.assertEqual(fx.ops("R2")["General/Proj/A2.md"], ["ADOPT_RENAME", "LOCAL_ONLY"])
        rows = fx.base_rows(A_ID)
        self.assertEqual([(r["seq"], r["rel_path"], r["seed"]) for r in rows],
                         [(1, "General/Proj/A.md", 1), (2, "General/Proj/A2.md", 0)])
        self.assertEqual(rows[0]["sha256"], rows[1]["sha256"])
        self.assertEqual(fx.publish("R3", w2, "--overrides", ov), 0)
        self.assertEqual(len(fx.base_rows(A_ID)), 2)
        self.assertEqual(fx.rollback("R2"), 0)  # ADOPT_RENAME undone -> its re-path row goes too
        self.assertEqual([r["rel_path"] for r in fx.base_rows(A_ID)], ["General/Proj/A.md"])

    # finding 4: a user-added `aliases` is a local key
    def test_user_aliases_is_a_local_key(self):
        self.first()
        fx = self.fx
        fm, body, _ = pv.wb_common.split_note(fx.read("General/E.md").decode())
        fx.write("General/E.md", "---\n" + fm + "\naliases: [my alias]\n---\n" + body)
        self.assertEqual(fx.publish("R2", fx.world()), 0)
        a = fx.action("R2", "General/E.md")
        self.assertEqual((a["op"], a["variant"]), ("LOCAL_ONLY", "keys_only"))
        self.assertEqual(fx.publish("R3", fx.world(bodies={"E": "Echo v3"})), 0)
        a = fx.action("R3", "General/E.md")
        self.assertEqual((a["op"], a["keys"]), ("LOCAL_KEYS_ONLY", ["aliases"]))
        now = fx.read("General/E.md").decode()
        self.assertIn("Echo v3", now)
        self.assertIn("aliases: [my alias]", now)
        self.assertFalse(os.path.exists(fx.path("Notion_Obsidian/conflicts")))
        # the exporter starts emitting aliases (Notion title != file name): never a silent overwrite
        w4 = fx.world(bodies={"E": "Echo v4"}, extra_fm={"E": {"aliases": ["E title"]}})
        self.assertEqual(fx.publish("R4", w4), 0)
        a = fx.action("R4", "General/E.md")
        self.assertEqual(a["op"], "CONFLICT_BOTH")
        self.assertIn("aliases", a["reason"])
        self.assertIn("aliases: [my alias]", fx.read("General/E.md").decode())

    # finding 5: an adopt entry that nothing consumed is reported, never silently dropped
    def test_adopt_entry_not_staged_needs_manual(self):
        self.first()
        fx = self.fx
        la = fx.read("General/Proj/A.md") + b"wb\n"
        fx.write("General/Proj/A.md", la)
        aj = fx.jfile("adopt.json", {A_ID: {"rel": "General/Proj/A.md", "l_sha": sha(la)}})
        ids = os.path.join(fx.root, "ids.txt")
        with open(ids, "w") as fh:
            fh.write(A_ID + "\n")
        w2 = {k: v for k, v in fx.world().items() if v[2] != A_ID}
        self.assertEqual(fx.publish("R2", w2, "--adopt-writeback", aj, "--only-ids", ids), 0)
        nm = fx.plan("R2")["adopt_needs_manual"]
        self.assertEqual([(e["notion_id"], e["rel"]) for e in nm], [(A_ID, "General/Proj/A.md")])
        self.assertIn("not in staging", nm[0]["reason"])
        self.assertTrue(os.path.exists(os.path.join(fx.rep("R2"), "adopt_needs_manual.json")))
        self.assertIn("待人工处理", fx.read("Notion_Obsidian/运行记录/R2.md").decode())
        self.assertEqual(fx.read("General/Proj/A.md"), la)
        with open(ids, "w") as fh:
            fh.write(C_ID + "\n")
        self.assertEqual(fx.publish("R3", fx.world(), "--adopt-writeback", aj, "--only-ids", ids), 2)
        self.assertIn("missing from --only-ids", fx.last_err)

    def test_held_adopt_entry_needs_manual(self):
        self.first()
        fx = self.fx
        la = fx.read("General/Proj/A.md") + b"wb\n"
        fx.write("General/Proj/A.md", la)
        aj = fx.jfile("adopt.json", {A_ID: {"rel": "General/Proj/A.md", "l_sha": sha(la)}})
        holds = fx.jfile("holds.json", [A_ID])
        self.assertEqual(fx.publish("R2", fx.world(bodies={"A": "A wb"}), "--adopt-writeback", aj, "--holds", holds), 0)
        self.assertEqual(fx.ops("R2")["General/Proj/A.md"], ["SKIP_HELD"])
        self.assertEqual([e["notion_id"] for e in fx.plan("R2")["adopt_needs_manual"]], [A_ID])

    # finding 6: a renamed database folder does not get its .base re-ADDed at the old path
    def test_folder_rename_does_not_readd_base(self):
        self.first()
        fx = self.fx
        os.rename(fx.path("General/DB"), fx.path("General/DB renamed"))
        self.assertEqual(fx.publish("R2", fx.world()), 0)
        a = fx.action("R2", BASE_REL)
        self.assertEqual((a["op"], a["reason"]), ("SKIP_HELD", "object_skipped:duplicate_id_elsewhere"))
        self.assertFalse(os.path.exists(fx.path("General/DB")))
        os.remove(fx.path(BASE_REL.replace("General/DB/", "General/DB renamed/")))
        os.rename(fx.path("General/DB renamed"), fx.path("General/DB"))
        self.assertEqual(fx.publish("R3", fx.world()), 0)  # DB note back in place: the .base is restored
        self.assertEqual((fx.action("R3", BASE_REL)["op"], fx.action("R3", BASE_REL)["restore"]), ("ADD", True))

    # finding 7: a crash between the vault write and the DB commit is recovered, not misclassified
    def test_crash_after_local_keys_merge_is_recovered(self):
        self.first()
        fx = self.fx
        fm, body, _ = pv.wb_common.split_note(fx.read("General/E.md").decode())
        fx.write("General/E.md", "---\n" + fm + "\ntags: [k]\n---\n" + body)
        w2 = fx.world(bodies={"E": "Echo v2"})
        fx.stage("R2", w2)
        self.crash_run("R2", "General/E.md")
        merged = fx.read("General/E.md")
        self.assertIn(b"Echo v2", merged)
        self.assertEqual(self.intents(), [("General/E.md", "LOCAL_KEYS_ONLY")])
        fx.stage("R3", w2)
        self.assertEqual(fx.run("R3", apply=False), 0)  # plan-only: recovered in memory, export.db untouched
        self.assertEqual(self.intents(), [("General/E.md", "LOCAL_KEYS_ONLY")])
        plan = fx.plan("R3")
        self.assertEqual(fx.action("R3", "General/E.md")["op"], "UNCHANGED")
        self.assertIn({"type": "recovered", "rel": "General/E.md", "op": "LOCAL_KEYS_ONLY", "run_id": "R2",
                       "recorded": True}, plan["notes"])
        pf = fx.jfile("p3.json", plan)
        self.assertEqual(fx.run("R3", "--plan-file", pf, "--plan-sha", plan["plan_sha256"]), 0, fx.last_err)
        self.assertEqual(self.intents(), [])
        row = fx.pf()["General/E.md"]
        self.assertEqual((row["sha256"], row["staged_sha256"]), (sha(merged), sha(w2["General/E.md"][0])))
        self.assertFalse(os.path.exists(fx.path("Notion_Obsidian/conflicts")))
        self.assertTrue(any(j.get("event") == "recovered" for j in fx.journal("R3")))
        self.assertEqual(fx.publish("R4", fx.world(bodies={"E": "Echo v4"})), 0)
        self.assertEqual(fx.ops("R4")["General/E.md"], ["LOCAL_KEYS_ONLY"], "no false conflict later either")

    def test_crash_after_move_leaves_one_active_row(self):
        self.first()
        fx = self.fx
        w2 = fx.world(paths={"D": "General/Proj/D renamed.md"})
        fx.stage("R2", w2)
        self.crash_run("R2", "General/Proj/D renamed.md")
        self.assertFalse(os.path.exists(fx.path("General/Proj/D.md")))
        self.assertEqual(fx.publish("R3", w2), 0)
        self.assertEqual(fx.ops("R3")["General/Proj/D renamed.md"], ["UNCHANGED"])
        rows = fx.pf()
        self.assertEqual(rows["General/Proj/D.md"]["status"], "moved")
        self.assertEqual([r for r, m in rows.items() if m["notion_id"] == D_ID and m["status"] == "active"],
                         ["General/Proj/D renamed.md"])

    def test_crash_after_conflict_copy_is_not_copied_again(self):
        self.first()
        fx = self.fx
        fx.write("General/Proj/A.md", fx.read("General/Proj/A.md").replace(b"Alpha text", b"Alpha LOCAL"))
        w2 = fx.world(bodies={"A": "Alpha NOTION"})
        fx.stage("R2", w2)
        self.crash_run("R2", "conflict")
        crel = "Notion_Obsidian/conflicts/R2/General/Proj/A (Notion 版本 R2).md"
        self.assertTrue(os.path.exists(fx.path(crel)))
        self.assertEqual(fx.publish("R3", w2), 0)
        a = fx.action("R3", "General/Proj/A.md")
        self.assertEqual((a["op"], a.get("dedup"), a["conflict_rel"]), ("CONFLICT_BOTH", True, crel))
        self.assertFalse(os.path.exists(fx.path("Notion_Obsidian/conflicts/R3")))
        self.assertEqual(len(fx.conflict_rows()), 1)

    def test_take_refresh_lock(self):
        import wb_locks
        self.first()
        fx = self.fx
        fx.stage("R2", fx.world(bodies={"A": "A v2"}))
        with wb_locks.lock("refresh", state_dir=os.path.dirname(fx.state)):
            self.assertEqual(fx.run("R2", "--take-refresh-lock"), 2)
            self.assertIn("refresh", fx.last_err)
        self.assertEqual(fx.read("General/Proj/A.md"), fx.world()["General/Proj/A.md"][0])
        shutil.rmtree(fx.rep("R2"))
        self.assertEqual(fx.run("R2", "--take-refresh-lock"), 0)
        self.assertIn(b"A v2", fx.read("General/Proj/A.md"))

    # finding 8: no base row without its block_lines
    def test_no_block_lines_no_base_rows(self):
        fx = self.fx
        w = fx.world()
        self.assertEqual(fx.publish("R1", w, block_lines=False), 0)
        self.assertEqual(fx.base_rows(), [])
        self.assertEqual(json.loads(fx.last_out.strip().splitlines()[-1])["base_history"]["note"],
                         "staging has no block_lines.jsonl: base_history not appended")
        self.assertEqual(fx.publish("R2", w), 0)
        rows = fx.base_rows()
        self.assertTrue(rows and all(r["seed"] == 1 and r["run_id"] == "R2" for r in rows))
        for r in rows:
            self.assertEqual(fx.block_line_count(r["notion_id"], r["sha256"]), 2)


# ================================================================ unit helpers
class TestHelpers(unittest.TestCase):
    def test_merge_local_keys_verbatim_and_fallback(self):
        s = note_bytes(A_ID, "page", "A", "body S")
        fm = pv.wb_common.split_note(s.decode())[0]
        l_ = ("---\n" + fm + "\n# comment\ntags: [a, b]  # mine\nnested:\n  k: v\n---\nbody L\n").encode()
        out = pv.merge_local_keys(s, l_, ["tags", "nested"])
        self.assertTrue(out.decode().endswith("tags: [a, b]  # mine\nnested:\n  k: v\n---\n\nbody S\n"))
        flow = ("---\n{notion_id: x, tags: [a]}\n---\nb\n").encode()
        out = pv.merge_local_keys(s, flow, ["tags"])
        self.assertIn("tags:\n- a\n---\n", out.decode())
        self.assertIsNone(pv.merge_local_keys(s, l_, ["notion_title"]))

    def test_resolver_rules(self):
        keys = {"F/A.md": ("id", "a"), "F/B.md": ("id", "b"), "G/B.md": ("id", "b2"), "F/x.png": ("path", "F/x.png")}
        r = pv.Resolver(keys, lambda rel: keys[rel])
        self.assertEqual(r.resolve("A", "Z/z.md"), ("id", "a"))
        self.assertEqual(r.resolve("F/A", "Z/z.md"), ("id", "a"))
        self.assertEqual(r.resolve("f/a.md", "Z/z.md"), ("id", "a"))
        self.assertIsNone(r.resolve("B", "Z/z.md"), "ambiguous basename is unresolved")
        self.assertEqual(r.resolve("B", "G/q.md"), ("id", "b2"), "same folder wins")
        self.assertEqual(r.resolve("./B", "F/q.md"), ("id", "b"))
        self.assertEqual(r.resolve("x.png", "Q/q.md"), ("path", "F/x.png"))
        self.assertIsNone(r.resolve("missing", "F/q.md"))

    def test_norm_body_keeps_code(self):
        keys = {"F/A.md": ("id", "a"), "F/A2.md": ("id", "a")}
        r = pv.Resolver(keys, lambda rel: keys[rel])
        b1 = "x [[F/A|t]] `[[F/A]]`\n```\n[[F/A]]\n```\n| [[F/A\\|t]] |"
        b2 = "x [[A2|t]] `[[F/A]]`\n```\n[[F/A]]\n```\n| [[A2\\|t]] |"
        self.assertEqual(pv.norm_body(b1, "F/q.md", r), pv.norm_body(b2, "F/q.md", r))
        b3 = "x [[A2|t]] `[[A2]]`\n```\n[[F/A]]\n```\n| [[A2\\|t]] |"
        self.assertNotEqual(pv.norm_body(b1, "F/q.md", r), pv.norm_body(b3, "F/q.md", r))
        self.assertNotEqual(pv.norm_body("[[F/A|t]]", "F/q.md", r), pv.norm_body("[[F/A|other]]", "F/q.md", r))

    def test_inline_code(self):
        self.assertEqual(pv.inline_code("a/b.md"), "`a/b.md`")
        self.assertEqual(pv.inline_code("a`b"), "``a`b``")



# ================================================================ wave-4 fix round 1 (n2o-pipeline)
class TestW4RaceWindow(Base):
    """r1-data-safety-01: a local save that lands AFTER the sha / backup checks (during the base store, the intent
    commit, the journal line or the tmp write) is never overwritten: the final swap inspects what it displaced."""
    USER = b"USER TYPED THIS DURING THE PUBLISH\n"

    def r2(self, **world):
        self.first()
        w2 = self.fx.world(**world)
        self.fx.stage("R2", w2)
        self.assertEqual(self.fx.run("R2", apply=False), 0)
        plan = self.fx.plan("R2")
        return plan, self.fx.jfile("plan.json", plan)

    def run_with_save(self, plan, pf, rel, hook, mode="inplace", intent_rel=None):
        target = self.fx.path(rel)
        intent_rel = intent_rel or rel
        state = {"done": False}

        def save():
            if state["done"]:
                return
            state["done"] = True
            if mode == "inplace":
                with open(target, "ab") as fh:
                    fh.write(self.USER)
            else:  # editor-style save: tmp + rename
                with open(target, "rb") as fh:
                    data = fh.read()
                with open(target + ".tmp", "wb") as fh:
                    fh.write(data + self.USER)
                os.replace(target + ".tmp", target)
        if hook == "intent":
            real = pv.Publisher._intent

            def wrapped(self_, a, r, *args, **kw):
                real(self_, a, r, *args, **kw)
                if r == intent_rel:
                    save()
            ctx = mock.patch.object(pv.Publisher, "_intent", wrapped)
        else:  # right before the final swap of the new bytes
            real_rename2 = pv._rename2
            real_replace = os.replace

            def r2(src, dst, flags):
                if dst == target and flags == pv._RENAME_EXCHANGE:
                    save()
                return real_rename2(src, dst, flags)

            def rep(src, dst, *a, **kw):
                if dst == target and os.path.basename(str(src)).startswith(".nmig-tmp-"):
                    save()
                return real_replace(src, dst, *a, **kw)
            ctx = contextlib.ExitStack()
            ctx.enter_context(mock.patch.object(pv, "_rename2", r2))
            ctx.enter_context(mock.patch.object(pv.os, "replace", rep))
        with ctx:
            rc = self.fx.run("R2", "--plan-file", pf, "--plan-sha", plan["plan_sha256"])
        self.assertTrue(state["done"], "the save was injected")
        return rc

    def assert_user_text_kept(self, rel, rc, race):
        self.assertEqual(rc, 4, self.fx.last_err)
        self.assertIn(self.USER, self.fx.read(rel), "the newer local version stays in the vault")
        j = self.fx.journal("R2")
        self.assertIn(race, [x.get("op") for x in j])
        self.assertFalse([x for x in j if x.get("rel") == rel and x.get("phase") == "done"])
        con = sqlite3.connect(self.fx.state)
        self.assertEqual(con.execute("SELECT count(*) FROM publish_intents").fetchone()[0], 0)
        con.close()
        leftovers = [f for d, _, fs in os.walk(self.fx.vault) for f in fs if f.startswith(".nmig-tmp-")]
        self.assertEqual(leftovers, [])

    def test_replace_save_after_the_intent_commit_is_kept(self):
        plan, pf = self.r2(bodies={"A": "A v2"})
        for mode in ("inplace",):
            rc = self.run_with_save(plan, pf, "General/Proj/A.md", "intent", mode)
            self.assert_user_text_kept("General/Proj/A.md", rc, "SKIP_REPLACE_RACE")

    def test_replace_save_right_before_the_swap_is_kept_inplace_and_by_rename(self):
        for mode in ("inplace", "rename"):
            with self.subTest(mode=mode):
                self.fx.close()
                self.fx = Fx()
                plan, pf = self.r2(bodies={"A": "A v2"})
                rc = self.run_with_save(plan, pf, "General/Proj/A.md", "swap", mode)
                self.assert_user_text_kept("General/Proj/A.md", rc, "SKIP_REPLACE_RACE")
        self.addCleanup(self.fx.close)

    def test_move_with_content_change_save_after_the_intent_is_kept_at_the_old_path(self):
        plan, pf = self.r2(paths={"D": "General/Proj/D renamed.md"}, bodies={"D": "Delta v2"})
        self.assertEqual(self.fx.ops("R2")["General/Proj/D renamed.md"], ["MOVE"])
        rc = self.run_with_save(plan, pf, "General/Proj/D.md", "intent", intent_rel="General/Proj/D renamed.md")
        self.assertEqual(rc, 4)
        self.assertIn(self.USER, self.fx.read("General/Proj/D.md"))
        self.assertFalse(os.path.exists(self.fx.path("General/Proj/D renamed.md")))
        self.assertEqual(self.fx.pf()["General/Proj/D.md"]["status"], "active")

    def test_clean_replace_and_add_still_publish(self):
        plan, pf = self.r2(bodies={"A": "A v2"}, add=("F",))
        self.assertEqual(self.fx.run("R2", "--plan-file", pf, "--plan-sha", plan["plan_sha256"]), 0, self.fx.last_err)
        self.assertIn(b"A v2", self.fx.read("General/Proj/A.md"))
        self.assertIn(b"Foxtrot", self.fx.read("General/Proj/F.md"))

    def test_guarded_add_never_clobbers(self):
        d = os.path.join(self.fx.root, "g")
        os.makedirs(d)
        dst = os.path.join(d, "x.md")
        self.assertTrue(pv.guarded_add(dst, b"one"))
        self.assertFalse(pv.guarded_add(dst, b"two"))
        self.assertEqual(rbytes(dst), b"one")
        self.assertEqual(sorted(os.listdir(d)), ["x.md"])


class TestW4RunlogAndFolders(Base):
    """r1-n2o-completeness-06 / -09, r1-spec-coverage-08 / -12, r1-data-safety-06."""

    def test_tombstone_paths_and_notion_links_are_in_the_vault_run_log(self):
        self.first()
        self.fx.write("General/Proj/A.md", self.fx.read("General/Proj/A.md") + b"\nlocal\n")   # LOCAL_ONLY
        self.assertEqual(self.fx.publish("R2", self.fx.world(drop=("G",))), 0)
        log = self.fx.read("Notion_Obsidian/运行记录/R2.md").decode()
        sec = log.split("## 源端已不存在（tombstone，库内文件保留）", 1)
        self.assertEqual(len(sec), 2, log)
        self.assertIn("`General/G.md`", sec[1])
        self.assertIn(f"https://app.notion.com/p/{G_ID.replace('-', '')}", sec[1])
        lo = log.split("## 待写回的本地修改 (LOCAL_ONLY)", 1)[1].split("##", 1)[0]
        self.assertIn(f"`General/Proj/A.md`（正文或属性修改）（[打开 Notion](https://app.notion.com/p/{A_ID.replace('-', '')})）", lo)
        self.assertNotIn("[[", log)

    def test_runlog_notes_reach_the_vault_and_repeat_only_when_they_change(self):
        self.first()
        notes = [{"key": "new_roots_unshared:abc", "title": "新顶层页面未共享给读连接",
                  "lines": ["`New root`（General，notion_id `abc`）", "处理：共享给读连接"]}]
        nf = self.fx.jfile("notes.json", notes)
        self.assertEqual(self.fx.publish("R2", self.fx.world(), "--runlog-notes", nf), 0, self.fx.last_err)
        log = self.fx.read("Notion_Obsidian/运行记录/R2.md").decode()
        self.assertIn("## 新顶层页面未共享给读连接", log)
        self.assertIn("- `New root`（General，notion_id `abc`）", log)
        self.assertEqual(self.fx.publish("R3", self.fx.world(), "--runlog-notes", nf), 0)   # unchanged: not repeated
        self.assertFalse(os.path.exists(self.fx.path("Notion_Obsidian/运行记录/R3.md")))
        nf2 = self.fx.jfile("notes2.json", notes + [{"key": "warn_block_limit:2", "title": "工作区成员数告警",
                                                       "lines": ["成员数 2"]}])
        self.assertEqual(self.fx.publish("R4", self.fx.world(), "--runlog-notes", nf2), 0)   # changed: written
        self.assertIn("工作区成员数告警", self.fx.read("Notion_Obsidian/运行记录/R4.md").decode())
        bad = self.fx.jfile("bad.json", {"not": "a list"})
        self.fx.stage("R5", self.fx.world())
        self.assertEqual(self.fx.run("R5", "--runlog-notes", bad), 2)

    def test_damaged_standing_digest_never_fails_an_applied_publish(self):
        self.first()
        self.fx.write("General/Proj/A.md", self.fx.read("General/Proj/A.md") + b"\nlocal\n")
        self.assertEqual(self.fx.publish("R2", self.fx.world()), 0)
        digest = os.path.join(os.path.dirname(self.fx.state), "runlog_standing_digest.json")
        for i, junk in enumerate(("[]", "null", '"x"', "5")):
            with open(digest, "w", encoding="utf-8") as fh:
                fh.write(junk)
            run = f"S{i}"
            self.assertEqual(self.fx.publish(run, self.fx.world()), 0, (junk, self.fx.last_err))
            self.assertTrue(os.path.exists(self.fx.path(f"Notion_Obsidian/运行记录/{run}.md")))  # one extra log
            self.assertIsInstance(json.loads(rbytes(digest)), dict)
        with mock.patch.object(pv, "write_runlog", side_effect=RuntimeError("boom")):
            self.assertEqual(self.fx.publish("S9", self.fx.world(bodies={"E": "Echo v9"})), 0)
        self.assertIn("runlog_failed", [x.get("event") for x in self.fx.journal("S9")])
        self.assertIn(b"Echo v9", self.fx.read("General/E.md"))

    def test_moves_leave_no_empty_source_folders(self):
        self.first()
        paths = {"P": "General/Proj2/Proj2.md", "A": "General/Proj2/A.md", "C": "General/Proj2/C.md",
                 "D": "General/Proj2/D.md"}
        self.assertEqual(self.fx.publish("R2", self.fx.world(paths=paths)), 0, self.fx.last_err)
        self.assertFalse(os.path.exists(self.fx.path("General/Proj")))           # folder page renamed in Notion
        self.assertTrue(os.path.isdir(self.fx.path("General")))
        self.assertIn("RMDIR_EMPTY", [x.get("op") for x in self.fx.journal("R2")])
        # folder page turned leaf: children moved up, its folder goes away too
        leaf = {"P": "General/Proj2.md", "A": "General/A.md", "C": "General/C.md", "D": "General/D.md"}
        self.assertEqual(self.fx.publish("R3", self.fx.world(paths=leaf)), 0, self.fx.last_err)
        self.assertFalse(os.path.exists(self.fx.path("General/Proj2")))
        # rollback of R3 re-creates the folder of the moved-back notes
        self.assertEqual(self.fx.rollback("R3"), 0, self.fx.last_out)
        self.assertTrue(os.path.isfile(self.fx.path("General/Proj2/Proj2.md")))
        self.assertTrue(os.path.isfile(self.fx.path("General/Proj2/A.md")))

    def test_a_source_folder_that_still_holds_a_user_file_is_kept(self):
        self.first()
        self.fx.write("General/Proj/mine.txt", b"user file\n")
        paths = {"P": "General/Proj2/Proj2.md", "A": "General/Proj2/A.md", "C": "General/Proj2/C.md",
                 "D": "General/Proj2/D.md"}
        self.assertEqual(self.fx.publish("R2", self.fx.world(paths=paths)), 0, self.fx.last_err)
        self.assertEqual(os.listdir(self.fx.path("General/Proj")), ["mine.txt"])
        self.assertNotIn("RMDIR_EMPTY", [x.get("op") for x in self.fx.journal("R2")])


if __name__ == "__main__":
    unittest.main()
