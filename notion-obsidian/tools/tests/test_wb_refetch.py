#!/usr/bin/env python3
"""Tests for tools/wb_refetch.py (module H: targeted refetch, IMPL_CONTRACT §5.12, R-130/R-131).

Everything runs against tools/tests/fake_notion.py on 127.0.0.1 (no real Notion, no real token); snapshots,
blobs, rate-gate state, exports and verifier reports live in tempfile.mkdtemp() dirs that are removed.
Base and "fresh" snapshots are built with the REAL crawler code of tools/snapshot.py (detect_kinds +
BlobInventory) through a ReadOnlyClient, exactly like snapshot.py's main().

Run (from the sandbox root):
  python3 -I -B -m unittest discover -s tools/tests -p 'test_wb_refetch.py' -v
"""
import contextlib
import datetime
import hashlib
import http.server
import inspect
import io
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
for _p in (HERE, TOOLS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import export_obsidian as exp  # noqa: E402
import snapshot as snapmod  # noqa: E402
import verify_vault as vv  # noqa: E402
import wb_refetch  # noqa: E402
from fake_notion import FakeNotion  # noqa: E402
from rate_gate import RateGate  # noqa: E402
from readonly_client import ReadOnlyClient, make_read_client  # noqa: E402
from wb_refetch import RefetchError, RefetchIncomplete, refetch  # noqa: E402
from writeback_client import WritebackClient  # noqa: E402

QUIET = ("nmig.client", "nmig.inventory", "writeback_client", "wb_refetch", "snapshot")
_saved = {}
SPEC_TS = list(vv.SPEC_TEAMSPACES)
IMG = b"\x89PNG\r\n\x1a\n" + b"fake image bytes " * 40


def setUpModule():
    for name in QUIET:
        lg = logging.getLogger(name)
        _saved[name] = lg.level
        lg.setLevel(logging.CRITICAL)


def tearDownModule():
    for name, lvl in _saved.items():
        logging.getLogger(name).setLevel(lvl)


# ----------------------------------------------------------------------------------------------- fixtures
def para(text):
    return {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def image(url):
    return {"type": "image", "image": {"type": "file", "file": {"url": url, "expiry_time": "2026-11-17T23:00:00.000Z"},
                                       "caption": []}}


class FileServer:
    """Tiny local HTTP server for Notion-hosted file URLs (blob downloads)."""

    def __init__(self, files):
        files = dict(files)

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = files.get(self.path.split("?")[0])
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.th = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.th.start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.th.join(5)


def roots_for(home, other):
    return [{"id": home, "title": "Home", "teamspace": "General", "teamspace_order": 1, "root_order": 1},
            {"id": other, "title": "Other", "teamspace": "x_to_notion", "teamspace_order": 3, "root_order": 1}]


def crawl(client, roots_in, path, workers=1):
    """snapshot.py main(), non-incremental, on the fake: detect_kinds -> roots.json -> BlobInventory crawl."""
    roots = snapmod.detect_kinds(client, roots_in)
    with open(path + ".roots.json", "w", encoding="utf-8") as fh:
        json.dump(roots, fh, ensure_ascii=False, indent=1)
    store = snapmod.Store(path)
    me = client.me()
    store.x("INSERT OR REPLACE INTO meta VALUES('workspace_id', ?)", (me["bot"]["workspace_id"],))
    store.x("INSERT OR REPLACE INTO meta VALUES('notion_version', ?)", ("2026-03-11",))
    inv = snapmod.BlobInventory(client, store, workers=workers, hash_files=True, comments=False)
    inv.seed([r for r in roots if not r.get("in_trash")])
    inv.run()
    inv.http.close()
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.db.close()
    for s in ("-wal", "-shm"):
        if os.path.exists(path + s) and os.path.getsize(path + s) == 0:
            os.unlink(path + s)
    return roots


def strip_rid(d):
    if isinstance(d, dict):
        d = dict(d)
        d.pop("request_id", None)
    return d


def dump(path):
    """Snapshot content for comparisons."""
    c = wb_refetch.open_immutable(path)
    try:
        objs = {(i, k): {"parent": p, "root": r, "depth": d, "data": strip_rid(json.loads(data)), "fetched_at": fa,
                         "raw": data}
                for i, k, p, r, d, data, fa in c.execute("SELECT id,kind,parent_id,root_id,depth,data,fetched_at FROM objects")}
        blocks = {r[0]: tuple(r[1:]) for r in c.execute("SELECT id,page_id,parent_id,ord,type,has_children,data FROM blocks")}
        files = {r[0]: tuple(r) for r in c.execute("SELECT * FROM files")}
        work = {(r[0], r[1]): tuple(r[2:]) for r in c.execute("SELECT id,kind,parent_id,root_id,depth,status FROM work")}
        meta = dict(c.execute("SELECT key, value FROM meta"))
        comments = c.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        c.close()
    return {"objects": objs, "blocks": blocks, "files": files, "work": work, "meta": meta, "comments": comments,
            "journal_mode": mode}


def sha_file(p):
    with open(p, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def tree_files(root):
    out = {}
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = sha_file(p)
    return out


def export(snap, sidebar, out_dir, work_dir):
    saved = exp.WORK
    exp.WORK = work_dir                     # blobs/ of the test
    try:
        ex = exp.Exporter(snap, sidebar, "wbtest", out_dir + ".state.db", out_dir, immutable=True)
        with contextlib.redirect_stdout(io.StringIO()):
            ex.run()
    finally:
        exp.WORK = saved
    return os.path.join(out_dir, "vault")


def run_verify(snap, sidebar, vault, out, ids=None):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        ok, rep, _ = vv.verify(snap, sidebar, vault, out, roots_kinds=snap + ".roots.json", ids=ids,
                               snapshot_immutable=True)
    return ok, rep


class Env:
    """Fake Notion + temp dirs + clients for one test (class)."""

    def __init__(self, prefix):
        self.tmp = tempfile.mkdtemp(prefix=prefix)
        self.work = os.path.join(self.tmp, "work")
        self.blobs = os.path.join(self.work, "blobs")
        os.makedirs(os.path.join(self.blobs, "tmp"))
        self.snaps = os.path.join(self.tmp, "snapshots")
        os.makedirs(os.path.join(self.snaps, "wb"))
        self._saved_blobs = snapmod.BLOBS
        snapmod.BLOBS = self.blobs          # snapshot.py's BlobInventory writes here, never to <WORK>/blobs
        self.files = FileServer({"/a.png": IMG, "/k.png": IMG[::-1]})
        self.fake = FakeNotion(tokens={
            "ro": {"caps": {"read", "user_info"}, "roots": {"*"}, "secret": "tok-ro-7f3a9c"},
            "wb": {"caps": {"read", "update", "insert"}, "roots": {"*"}, "secret": "tok-wb-91c2d4"}})
        self.base_url = self.fake.start()
        self.gate = RateGate(os.path.join(self.tmp, "rate_gate.json"), total_rps=1000, burst=100)

    def ro(self, cls=ReadOnlyClient):
        c = make_read_client(base_url=self.base_url, token=self.fake.secret("ro"), rate=None, gate=self.gate,
                             sleep=lambda s: None)
        if cls is not ReadOnlyClient:
            c.__class__ = cls
        return c

    def wb(self):
        return WritebackClient(self.fake.secret("wb"), base_url=self.base_url, silent=False, rate=1000,
                               allow_block_trash=True)

    def close(self):
        snapmod.BLOBS = self._saved_blobs
        self.fake.stop()
        self.files.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


def seed(fake, file_base):
    ids = {}
    home = ids["home"] = fake.add_page("Home")
    a = ids["a"] = fake.add_page("Notes", parent=home, children=[
        para("first"), para("second"),
        {"type": "toggle", "toggle": {"rich_text": [{"text": {"content": "fold"}}], "children": [para("inside")]}},
        image(file_base + "/a.png")])
    ids["s"] = fake.add_page("Sub", parent=a, children=[para("sub body")])
    ids["p"] = fake.add_page("Archive", parent=home, children=[para("archive body")])
    ids["c"] = fake.add_page("Old", parent=ids["p"], children=[para("old body")])
    ids["c2"] = fake.add_page("Older", parent=ids["c"], children=[para("older body")])
    ids["d"] = fake.add_page("Doomed", parent=home, children=[para("doomed body")])
    ids["db"], ids["ds"] = fake.add_database(home, "Tasks", {"Name": "title", "Label": {"type": "select", "options": ["a"]}})
    ids["r1"] = fake.add_row(ids["ds"], "Task 1", {"Label": {"select": {"name": "a"}}}, children=[para("r1 body")])
    ids["r2"] = fake.add_row(ids["ds"], "Task 2", {"Label": {"select": {"name": "a"}}})
    ids["t1"] = fake.add_template(ids["ds"], "Template", children=[para("template body")])
    ids["k"] = fake.add_page("Keep", parent=home, children=[para("keep me"), image(file_base + "/k.png")])
    other = ids["other"] = fake.add_page("Other")
    ids["e"] = fake.add_page("Elsewhere", parent=other, children=[para("elsewhere body")])
    return ids


def write_sidebar(path, roots):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"teamspace_order_required": SPEC_TS, "roots": roots}, fh, ensure_ascii=False)


# ------------------------------------------------------------------------------------- the main scenario
class RefetchScenarioTests(unittest.TestCase):
    """Base snapshot -> Notion changes (edit, append, new child page, trash a page, trash a parent whose child
    must read in_trash, new row, edited row, deleted file block) -> refetch(written pages + parents, data
    source) -> compare with a full fresh crawl, export, verify."""

    @classmethod
    def setUpClass(cls):
        cls.env = env = Env("test_wb_refetch_")
        cls.ids = ids = seed(env.fake, env.files.base)
        cls.roots = roots_for(ids["home"], ids["other"])
        cls.sidebar = os.path.join(env.tmp, "sidebar.json")
        write_sidebar(cls.sidebar, cls.roots)
        cls.base = os.path.join(env.snaps, "snap_base.db")
        crawl(env.ro(), cls.roots, cls.base)
        cls.base_sha = sha_file(cls.base)
        cls.base_roots_sha = sha_file(cls.base + ".roots.json")
        cls.base_dump = dump(cls.base)
        # --- Notion changes (through the write-back client, like an apply) ---
        wb = env.wb()
        kids = env.fake.child_ids(ids["a"])
        first, img = kids[0], kids[3]
        cls.first_block = first
        r = wb.update_block(first, {"paragraph": {"rich_text": [{"type": "text", "text": {"content": "first EDITED"}}]}})
        assert r.ok, r
        r = wb.append_children(ids["a"], [para("appended line")])
        assert r.ok, r
        cls.appended = r.committed_ids[0]
        r = wb.trash_block(img)
        assert r.ok, r
        cls.img_block = img
        r = wb.create_page({"parent": {"page_id": ids["a"]},
                            "properties": {"title": {"title": [{"text": {"content": "New Child"}}]}},
                            "children": [para("new child body")]})
        assert r.ok, r
        cls.new_child = r.committed_ids[0]
        assert wb.update_page(ids["d"], {"in_trash": True}).ok
        assert wb.update_page(ids["p"], {"in_trash": True}).ok
        r = wb.create_page({"parent": {"data_source_id": ids["ds"]},
                            "properties": {"Name": {"title": [{"text": {"content": "Task 3"}}]},
                                           "Label": {"select": {"name": "b"}}},
                            "children": [para("r3 body")]})
        assert r.ok, r
        cls.r3 = r.committed_ids[0]
        assert wb.update_page(ids["r1"], {"properties": {"Name": {"title": [{"text": {"content": "Task 1 renamed"}}]}}}).ok
        wb.session.close()
        # --- targeted refetch: written pages (A, R1) + parents of created/trashed pages (A, Home) + data source ---
        env.fake.clear_requests()
        cls.out = os.path.join(env.snaps, "wb", "snap_refetch.db")
        cls.client = env.ro()
        cls.stats = refetch(cls.base, cls.out, [ids["a"], ids["home"], ids["r1"]], [ids["ds"]], cls.client,
                            blobs_dir=env.blobs, workers=3)
        cls.requests = list(env.fake.requests)
        cls.out_dump = dump(cls.out)
        # --- a full fresh crawl of the same Notion state ---
        cls.fresh = os.path.join(env.snaps, "snap_fresh.db")
        crawl(env.ro(), cls.roots, cls.fresh)
        cls.fresh_dump = dump(cls.fresh)
        # --- exports (B) and verification (D) ---
        cls.vault = export(cls.out, cls.sidebar, os.path.join(env.tmp, "stage_refetch"), env.work)
        cls.vault_fresh = export(cls.fresh, cls.sidebar, os.path.join(env.tmp, "stage_fresh"), env.work)

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    # -- helpers --
    def trashed_ids(self):
        return {t["id"] for t in self.stats["trashed"]}

    # -- tests --
    def test_base_never_written(self):
        self.assertEqual(sha_file(self.base), self.base_sha)
        self.assertEqual(sha_file(self.base + ".roots.json"), self.base_roots_sha)
        for s in ("-wal", "-shm", "-journal"):
            self.assertFalse(os.path.exists(self.base + s), s)

    def test_output_is_one_finished_file(self):
        self.assertTrue(os.path.isfile(self.out))
        self.assertEqual(self.out_dump["journal_mode"], "delete")
        for s in ("-wal", "-shm", "-journal"):
            self.assertFalse(os.path.exists(self.out + s), s)
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.out))),
                         ["snap_refetch.db", "snap_refetch.db.roots.json"])   # no .part leftovers
        with open(self.out + ".roots.json", encoding="utf-8") as fh, open(self.base + ".roots.json", encoding="utf-8") as fb:
            self.assertEqual(json.load(fh), json.load(fb))

    def test_meta(self):
        m = self.out_dump["meta"]
        self.assertTrue(m["finished_at"])
        self.assertGreaterEqual(m["finished_at"], self.base_dump["meta"]["finished_at"])
        self.assertEqual(m["refetch_base"], os.path.abspath(self.base))
        self.assertEqual(m["refetch_base_finished_at"], self.base_dump["meta"]["finished_at"])
        self.assertEqual(json.loads(m["refetch_page_ids"]), [self.ids["a"], self.ids["home"], self.ids["r1"]])
        self.assertEqual(json.loads(m["refetch_data_source_ids"]), [self.ids["ds"]])
        summ = json.loads(m["targeted_refetch"])
        self.assertIn(self.new_child, summ["new_objects"])
        self.assertEqual(set(summ["trashed"]), {self.ids[k] for k in ("p", "c", "c2", "d")})
        self.assertEqual(json.loads(m["trash_markers"]), sorted(self.ids[k] for k in ("p", "c", "c2", "d")))
        self.assertEqual(self.stats["trash_markers"], 4)
        self.assertEqual({t["was_marker"] for t in self.stats["trashed"]}, {False})
        self.assertEqual(m["workspace_id"], self.base_dump["meta"]["workspace_id"])

    def test_equals_fresh_crawl_except_trash_markers(self):
        """Every object/block/file/work row equals a full fresh crawl, except the trashed objects that the
        refetch keeps as markers (a fresh crawl never reaches them)."""
        tr = self.trashed_ids()
        out, fresh = self.out_dump, self.fresh_dump
        o_out = {k: (v["parent"], v["root"], v["depth"], v["data"]) for k, v in out["objects"].items() if k[0] not in tr}
        o_fresh = {k: (v["parent"], v["root"], v["depth"], v["data"]) for k, v in fresh["objects"].items()}
        self.assertEqual(sorted(o_out), sorted(o_fresh))
        for k in o_fresh:
            self.assertEqual(o_out[k], o_fresh[k], k)
        self.assertEqual(out["blocks"], fresh["blocks"])
        self.assertEqual(out["files"], fresh["files"])
        w_out = {k: v for k, v in out["work"].items() if k[0] not in tr}
        self.assertEqual(w_out, fresh["work"])

    def test_untouched_objects_byte_identical_to_base(self):
        """Objects outside the refetch set keep the base rows byte for byte (incl. fetched_at)."""
        for key in ("k", "e", "other", "t1", "s"):
            oid = self.ids[key]
            self.assertEqual(self.out_dump["objects"][(oid, "page")]["raw"], self.base_dump["objects"][(oid, "page")]["raw"], key)
            self.assertEqual(self.out_dump["objects"][(oid, "page")]["fetched_at"],
                             self.base_dump["objects"][(oid, "page")]["fetched_at"], key)
            b_out = {b: v for b, v in self.out_dump["blocks"].items() if v[0] == oid}
            b_base = {b: v for b, v in self.base_dump["blocks"].items() if v[0] == oid}
            self.assertEqual(b_out, b_base, key)
            self.assertTrue(b_base or key == "s")
        k_files = {f: v for f, v in self.out_dump["files"].items() if f.startswith(self.env.fake.child_ids(self.ids["k"])[1])}
        self.assertEqual(len(k_files), 1)

    def test_only_affected_ids_requested(self):
        """R-130: only the listed objects (and what changed under them) cause API requests; all reads."""
        self.assertTrue(self.requests)
        self.assertEqual({r["method"] for r in self.requests} - {"GET", "POST"}, set())
        for r in self.requests:
            if r["method"] == "POST":
                self.assertRegex(r["path"], r"^data_sources/[0-9a-f-]+/query$")
            self.assertEqual(r["token"], "ro")
        text = json.dumps([[r["path"], r["query"]] for r in self.requests])
        for key in ("k", "e", "other", "t1", "s"):
            self.assertNotIn(self.ids[key], text, key)

    def test_edited_and_appended_blocks(self):
        rows = sorted((v for v in self.out_dump["blocks"].values() if v[1] == self.ids["a"]), key=lambda v: v[2])
        texts = [json.loads(v[5]).get(v[3], {}).get("rich_text", [{}])[0].get("plain_text") if v[3] == "paragraph" else v[3]
                 for v in rows]
        self.assertEqual(texts, ["first EDITED", "second", "toggle", "child_page", "appended line", "child_page"])
        self.assertNotIn(self.img_block, self.out_dump["blocks"])
        self.assertFalse([k for k in self.out_dump["files"] if k.startswith(self.img_block)])  # stale file row removed
        self.assertTrue([k for k in self.base_dump["files"] if k.startswith(self.img_block)])

    def test_new_child_page_and_new_row(self):
        self.assertIn((self.new_child, "page"), self.out_dump["objects"])
        self.assertEqual(self.out_dump["objects"][(self.new_child, "page")]["parent"], self.ids["a"])
        self.assertIn((self.r3, "page"), self.out_dump["objects"])
        ds = self.out_dump["objects"][(self.ids["ds"], "data_source")]["data"]
        self.assertEqual(ds["_row_ids"], [self.ids["r1"], self.ids["r2"], self.r3])
        self.assertEqual(ds["_template_ids"], [self.ids["t1"]])
        self.assertEqual(self.out_dump["work"][(self.r3, "row")][-1], "done")
        new = {o["id"] for o in self.stats["new_objects"]}
        self.assertEqual(new, {self.new_child, self.r3})
        r1 = self.out_dump["objects"][(self.ids["r1"], "page")]["data"]
        self.assertEqual(r1["properties"]["Name"]["title"][0]["plain_text"], "Task 1 renamed")

    def test_trashed_pages_marked_with_children(self):
        objs = self.out_dump["objects"]
        for key in ("p", "c", "c2", "d"):
            oid = self.ids[key]
            self.assertTrue(objs[(oid, "page")]["data"]["in_trash"], key)   # c, c2: inherited from the trashed parent
            self.assertFalse([b for b, v in self.out_dump["blocks"].items() if v[0] == oid], key)
            self.assertNotIn((oid, "page"), self.fresh_dump["objects"])
        via = {t["id"]: t["via"] for t in self.stats["trashed"]}
        self.assertEqual(via[self.ids["p"]], "own")
        self.assertEqual(via[self.ids["d"]], "own")
        self.assertEqual(self.stats["pruned"], [])
        # the parent's listing no longer has them
        home_kids = {b for b, v in self.out_dump["blocks"].items() if v[0] == self.ids["home"] and v[3] == "child_page"}
        self.assertNotIn(self.ids["p"], home_kids)
        self.assertNotIn(self.ids["d"], home_kids)

    def test_export_matches_fresh_export(self):
        """B on the refetched snapshot writes exactly what it writes for a full fresh crawl."""
        a, b = tree_files(self.vault), tree_files(self.vault_fresh)
        self.assertEqual(a, b)
        notes = [p for p in a if p.endswith("Notes.md") or p.endswith(os.sep + "Notes" + os.sep + "Notes.md")]
        self.assertTrue(notes, sorted(a))
        with open(os.path.join(self.vault, notes[0]), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("first EDITED", text)
        self.assertIn("appended line", text)
        self.assertIn("New Child", text)
        self.assertNotIn("\nfirst\n", text)
        names = "\n".join(a)
        self.assertIn("New Child.md", names)
        self.assertIn("Task 3.md", names)
        for gone in ("Archive", "Doomed", "Old.md", "Older.md"):
            self.assertNotIn(gone, names)

    def test_verifier_orphan_check_passes(self):
        out = os.path.join(self.env.tmp, "verify_refetch")
        ok, rep = run_verify(self.out, self.sidebar, self.vault, out)
        c02 = rep["checks"]["C02"]
        self.assertEqual(c02["status"], "PASS", c02["summary"])
        self.assertEqual(c02["details"]["orphans_under_in_scope_ancestor"], [])
        bad = {c: r["status"] for c, r in rep["checks"].items() if c != "C11" and r["status"] != "PASS"}
        self.assertEqual(bad, {}, {c: rep["checks"][c]["summary"] for c in bad})
        self.assertIn("immutable=1", json.dumps(rep))

    def test_verifier_ids_scope_trashed_absent(self):
        """W6 (D's rule): pages trashed by write-back pass --ids only if trashed in the refetched snapshot."""
        ids_file = os.path.join(self.env.tmp, "ids.txt")
        listed = [self.ids["a"], self.new_child, self.r3, self.ids["p"], self.ids["c"], self.ids["d"]]
        with open(ids_file, "w", encoding="utf-8") as fh:
            fh.write("\n".join(listed) + "\n")
        ok, rep = run_verify(self.out, self.sidebar, self.vault, os.path.join(self.env.tmp, "verify_ids"), ids=ids_file)
        scope = rep["scope"]
        self.assertEqual(scope["counts"], {"checked": 3, "trashed_absent": 3, "unverifiable": 0}, scope["ids_unverifiable"])
        self.assertEqual(rep["checks"]["C03"]["status"], "PASS")

    def test_stats(self):
        st = self.stats
        self.assertEqual(set(st["refetched_pages"]) >= {self.ids["a"], self.ids["home"], self.ids["r1"], self.new_child,
                                                         self.r3}, True)
        self.assertEqual(st["relisted_databases"], [self.ids["db"]])
        self.assertTrue(st["rate_gate"])
        self.assertEqual(st["requests"]["errors"], 0)
        self.assertGreater(st["requests"]["requests"], 0)
        self.assertEqual(st["requests"]["requests"], len(self.requests))
        json.dumps(st)   # serialisable


# ------------------------------------------------------------------------------------------- structure
class _Small(unittest.TestCase):
    """Small workspace per test: Home{A{S}, B, P{C}}, Other{E}."""

    def setUp(self):
        self.env = Env("test_wb_refetch_s_")
        f = self.env.fake
        self.home = f.add_page("Home")
        self.a = f.add_page("A", parent=self.home, children=[para("a body")])
        self.s = f.add_page("S", parent=self.a, children=[para("s body")])
        self.s2 = f.add_page("S2", parent=self.s, children=[para("s2 body")])
        self.b = f.add_page("B", parent=self.home, children=[para("b body")])
        self.other = f.add_page("Other")
        self.e = f.add_page("E", parent=self.other, children=[para("e body")])
        self.roots = roots_for(self.home, self.other)
        self.sidebar = os.path.join(self.env.tmp, "sidebar.json")
        write_sidebar(self.sidebar, self.roots)
        self.base = os.path.join(self.env.snaps, "snap_base.db")

    def tearDown(self):
        self.env.close()

    def crawl_base(self):
        crawl(self.env.ro(), self.roots, self.base)

    def move(self, oid, new_parent):
        f = self.env.fake
        with f._lock:
            old = f.up[oid]
            f.kids[old].remove(oid)
            if new_parent is None:
                f.up[oid] = None
            else:
                f._insert_child(new_parent, oid)

    def do_refetch(self, pages, dss=(), client=None, **kw):
        out = os.path.join(self.env.snaps, "wb", "snap_out.db")
        st = refetch(self.base, out, pages, list(dss), client or self.env.ro(), blobs_dir=self.env.blobs, **kw)
        return out, st

    def assert_equals_fresh(self, out, st, tag="fresh"):
        """Objects/blocks/files/work equal a full fresh crawl except the trash markers (meta.trash_markers, which
        must include this run's `trashed`); the export equals the fresh export; the verifier's C02 passes."""
        fresh = os.path.join(self.env.snaps, f"snap_{tag}.db")
        crawl(self.env.ro(), self.roots, fresh)
        a, b = dump(out), dump(fresh)
        tr = set(json.loads(a["meta"]["trash_markers"]))
        self.assertGreaterEqual(tr, {t["id"] for t in st["trashed"]})
        oa = {k: (v["parent"], v["root"], v["depth"], v["data"]) for k, v in a["objects"].items()
              if k[0] not in tr and not (k[1] == "data_source" and v["parent"] in tr)}   # a marker db keeps its sources
        ob = {k: (v["parent"], v["root"], v["depth"], v["data"]) for k, v in b["objects"].items()}
        self.assertEqual(sorted(oa), sorted(ob))
        for k in ob:
            self.assertEqual(oa[k], ob[k], k)
        self.assertEqual(a["blocks"], b["blocks"])
        self.assertEqual(a["files"], b["files"])
        self.assertEqual({k: v for k, v in a["work"].items() if k[0] not in tr}, b["work"])
        vault = export(out, self.sidebar, os.path.join(self.env.tmp, f"st_out_{tag}"), self.env.work)
        vault_f = export(fresh, self.sidebar, os.path.join(self.env.tmp, f"st_{tag}"), self.env.work)
        self.assertEqual(tree_files(vault), tree_files(vault_f))
        ok, rep = run_verify(out, self.sidebar, vault, os.path.join(self.env.tmp, f"ver_{tag}"))
        self.assertEqual(rep["checks"]["C02"]["status"], "PASS", rep["checks"]["C02"]["summary"])
        self.last_verify = rep
        return a


class MoveTests(_Small):
    def test_move_between_listed_parents(self):
        self.crawl_base()
        self.move(self.s, self.b)
        out, st = self.do_refetch([self.a, self.b])
        self.assertEqual([m["id"] for m in st["moved"]], [self.s])
        d = self.assert_equals_fresh(out, st)
        self.assertEqual(d["objects"][(self.s, "page")]["parent"], self.b)

    def test_move_new_parent_not_listed_is_found(self):
        """Only the old parent is listed: the probe finds the page alive under B and re-fetches B too."""
        self.crawl_base()
        self.move(self.s, self.b)
        out, st = self.do_refetch([self.a])
        self.assertIn(self.b, st["refetched_pages"])
        self.assertGreaterEqual(len(st["rounds"]), 2)
        self.assert_equals_fresh(out, st)

    def test_move_to_other_root_fixes_root_and_depth(self):
        self.crawl_base()
        self.move(self.s, self.e)
        out, st = self.do_refetch([self.a, self.e])
        d = self.assert_equals_fresh(out, st)    # compares root_id/depth too
        self.assertEqual(d["objects"][(self.s2, "page")]["root"], self.other)
        self.assertEqual(d["objects"][(self.s2, "page")]["depth"], 3)

    def test_move_out_of_scope_prunes_subtree(self):
        self.crawl_base()
        self.move(self.s, None)                      # now a workspace-level page that is no sidebar root
        out, st = self.do_refetch([self.a])
        pruned = {p["id"] for p in st["pruned"]}
        self.assertEqual(pruned, {self.s, self.s2})
        self.assert_equals_fresh(out, st)

    def test_permanently_deleted_page_is_pruned(self):
        self.crawl_base()
        f = self.env.fake
        with f._lock:                                # no API for permanent deletion: drop it from the fake
            f.kids[self.a].remove(self.s)
            for x in (self.s, self.s2):
                f.pages.pop(x)
                f.up.pop(x, None)
        out, st = self.do_refetch([self.a])
        self.assertEqual({p["id"] for p in st["pruned"]}, {self.s, self.s2})
        self.assertTrue(st["pruned"][0]["reason"].startswith(("gone", "under")))
        self.assert_equals_fresh(out, st)

    def test_without_prune_the_verifier_fails(self):
        """Negative control: put a pruned object back -> the verifier's orphan check (C02) fails."""
        self.crawl_base()
        self.move(self.s, None)
        out, st = self.do_refetch([self.a])
        vault = export(out, self.sidebar, os.path.join(self.env.tmp, "st_neg"), self.env.work)
        broken = os.path.join(self.env.tmp, "broken.db")
        shutil.copyfile(out, broken)
        shutil.copyfile(out + ".roots.json", broken + ".roots.json")
        src = wb_refetch.open_immutable(self.base)
        row = src.execute("SELECT * FROM objects WHERE id=? AND kind='page'", (self.s,)).fetchone()
        src.close()
        c = sqlite3.connect(broken)                  # a temp copy of the refetch output
        c.execute("INSERT INTO objects VALUES(?,?,?,?,?,?,?)", row)
        c.commit()
        c.close()
        ok, rep = run_verify(broken, self.sidebar, vault, os.path.join(self.env.tmp, "ver_neg"))
        self.assertEqual(rep["checks"]["C02"]["status"], "FAIL")
        self.assertEqual([o["id"] for o in rep["checks"]["C02"]["details"]["orphans_under_in_scope_ancestor"]], [self.s])
        ok, rep = run_verify(out, self.sidebar, vault, os.path.join(self.env.tmp, "ver_pos"))
        self.assertEqual(rep["checks"]["C02"]["status"], "PASS")

    def test_listed_page_trashed_itself_refetches_its_parent(self):
        """The trashed page itself is listed (not its parent): the parent is re-fetched as well."""
        self.crawl_base()
        wb = self.env.wb()
        self.assertTrue(wb.update_page(self.a, {"in_trash": True}).ok)
        wb.session.close()
        out, st = self.do_refetch([self.a])
        self.assertIn(self.home, st["refetched_pages"])
        self.assertEqual({t["id"] for t in st["trashed"]}, {self.a, self.s, self.s2})
        self.assert_equals_fresh(out, st)

    def test_trashed_root(self):
        self.crawl_base()
        wb = self.env.wb()
        self.assertTrue(wb.update_page(self.other, {"in_trash": True}).ok)
        wb.session.close()
        out, st = self.do_refetch([self.other])
        with open(out + ".roots.json", encoding="utf-8") as fh:
            roots = {r["id"]: r for r in json.load(fh)}
        self.assertTrue(roots[self.other]["in_trash"])
        self.assertFalse(roots[self.home]["in_trash"])
        self.assertEqual({t["id"] for t in st["trashed"]}, {self.other, self.e})
        fresh = os.path.join(self.env.snaps, "snap_fresh.db")
        crawl(self.env.ro(), self.roots, fresh)       # snapshot.py does not seed a trashed root
        vault = export(out, self.sidebar, os.path.join(self.env.tmp, "st"), self.env.work)
        vault_f = export(fresh, self.sidebar, os.path.join(self.env.tmp, "stf"), self.env.work)
        self.assertEqual(tree_files(vault), tree_files(vault_f))

    def test_no_ids_is_a_plain_copy(self):
        self.crawl_base()
        out, st = self.do_refetch([])
        a, b = dump(out), dump(self.base)
        self.assertEqual({k: v["raw"] for k, v in a["objects"].items()}, {k: v["raw"] for k, v in b["objects"].items()})
        self.assertEqual(a["blocks"], b["blocks"])
        self.assertEqual(st["refetched_pages"], [])
        self.assertEqual(st["requests"]["requests"], 1)          # users/me (workspace guard)


class SyncedBlockTests(_Small):
    """A synced reference lists the SOURCE block's children (same block ids). The fake has no synced
    semantics, so a ReadOnlyClient subclass maps reference -> source for list_children."""

    def make_client(self, mapping):
        class SyncedClient(ReadOnlyClient):
            def list_children(self, block_id):
                return super().list_children(mapping.get(block_id, block_id))
        return self.env.ro(SyncedClient)

    def seed_synced(self, ref_owns):
        """The crawl walks pages by depth, and the page walked LAST owns the shared rows: source page at depth
        2 (under B) with the reference in A (depth 1) -> the source owns; source page at depth 1 with the
        reference in S (depth 2) -> the reference page owns."""
        f = self.env.fake
        self.src_page = f.add_page("SrcPage", parent=self.home if ref_owns else self.b)
        self.src = f.add_blocks(self.src_page, [{"type": "synced_block", "synced_block": {"synced_from": None},
                                                 "children": [para("shared text")]}])[0]
        self.src_kid = f.child_ids(self.src)[0]
        self.ref_page = self.s if ref_owns else self.a
        self.ref = f.add_blocks(self.ref_page, [{"type": "synced_block",
                                                 "synced_block": {"synced_from": {"type": "block_id", "block_id": self.src}},
                                                 "children": [para("placeholder")]}])[0]
        return {self.ref: self.src}

    def owners(self, path):
        d = dump(path)
        return d["blocks"][self.src_kid][:2]

    def test_source_page_keeps_its_rows(self):
        mapping = self.seed_synced(ref_owns=False)
        crawl(self.make_client(mapping), self.roots, self.base, workers=1)
        self.assertEqual(self.owners(self.base), (self.src_page, self.src))   # source owns its children in the base
        wb = self.env.wb()
        assert wb.append_children(self.a, [para("a2")]).ok
        wb.session.close()
        out, st = self.do_refetch([self.a], client=self.make_client(mapping))
        self.assertEqual(self.owners(out), (self.src_page, self.src))         # not stolen by A's reference
        self.assertEqual(st["synced_skipped"], 1)
        self.assertEqual(st["synced_repaired"], 0)

    def test_reference_owned_listing_stays_with_reference(self):
        mapping = self.seed_synced(ref_owns=True)
        crawl(self.make_client(mapping), self.roots, self.base, workers=1)
        self.assertEqual(self.owners(self.base), (self.s, self.ref))
        out, st = self.do_refetch([self.s], client=self.make_client(mapping))
        self.assertEqual(self.owners(out), (self.s, self.ref))
        self.assertEqual(st["synced_skipped"], 0)

    def test_repair_when_source_disappears(self):
        mapping = self.seed_synced(ref_owns=False)
        crawl(self.make_client(mapping), self.roots, self.base, workers=1)
        f = self.env.fake
        with f._lock:                                    # the source block is deleted from its page
            f.kids[self.src_page].remove(self.src)
        out, st = self.do_refetch([self.a, self.src_page], client=self.make_client(mapping))
        self.assertEqual(st["synced_repaired"], 1)
        self.assertEqual(self.owners(out), (self.a, self.ref))


# --------------------------------------------------------------------------------------- failure modes
class FailureTests(_Small):
    def test_persistent_error_publishes_nothing(self):
        self.crawl_base()
        self.env.fake.inject("GET", rf"blocks/{self.a}/children", 500, times=None)
        out = os.path.join(self.env.snaps, "wb", "snap_x.db")
        with self.assertRaises(RefetchIncomplete):
            refetch(self.base, out, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)
        self.assertEqual(os.listdir(os.path.join(self.env.snaps, "wb")), [])

    def test_unexplained_structure_is_incomplete(self):
        """A child page missing from its parent's listing while its API parent still names that page."""
        self.crawl_base()
        f = self.env.fake
        with f._lock:
            f.kids[self.a].remove(self.s)     # hidden from the listing, but up[S] is still A
        out = os.path.join(self.env.snaps, "wb", "snap_x.db")
        with self.assertRaises(RefetchIncomplete):
            refetch(self.base, out, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)
        self.assertEqual(os.listdir(os.path.join(self.env.snaps, "wb")), [])

    def test_row_that_failed_in_base_is_tolerated_unless_forced(self):
        f = self.env.fake
        db, ds = f.add_database(self.b, "T", {"Name": "title"})
        r1 = f.add_row(ds, "r1", children=[para("x")])
        f.add_row(ds, "r2")
        f.inject("GET", rf"blocks/{r1}/children", 404, times=None)    # the base crawl cannot read r1's body
        self.crawl_base()
        self.assertEqual(dump(self.base)["work"][(r1, "row")][-1], "denied")
        out, st = self.do_refetch([], [ds])                            # re-listed: r1 fails again -> tolerated
        self.assertEqual([t["id"] for t in st["tolerated_failures"]], [r1])
        self.assertEqual(dump(out)["work"][(r1, "row")][-1], "denied")
        with self.assertRaises(RefetchIncomplete):                     # but a listed (written) page must succeed
            refetch(self.base, os.path.join(self.env.snaps, "wb", "snap_y.db"), [r1], [], self.env.ro(),
                    blobs_dir=self.env.blobs)

    def test_max_rounds(self):
        self.crawl_base()
        self.move(self.s, self.b)
        out = os.path.join(self.env.snaps, "wb", "snap_x.db")
        with self.assertRaises(RefetchIncomplete):
            refetch(self.base, out, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs, max_rounds=1)
        self.assertEqual(os.listdir(os.path.join(self.env.snaps, "wb")), [])


class InputTests(_Small):
    def setUp(self):
        super().setUp()
        self.crawl_base()
        self.out = os.path.join(self.env.snaps, "wb", "snap_out.db")

    def test_requires_read_only_client(self):
        wb = self.env.wb()
        with self.assertRaises(RefetchError):
            refetch(self.base, self.out, [self.a], [], wb, blobs_dir=self.env.blobs)
        wb.session.close()
        self.assertFalse(os.path.exists(self.out))

    def test_refuses_existing_out_and_base_as_out(self):
        open(self.out, "w").close()
        with self.assertRaises(RefetchError):
            refetch(self.base, self.out, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)
        with self.assertRaises(RefetchError):
            refetch(self.base, self.base, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)

    def variant(self, name, *sql):
        """A modified COPY of the test base (made with the backup API from a read-only open)."""
        bad = os.path.join(self.env.tmp, name)
        wb_refetch.copy_snapshot(self.base, bad)
        shutil.copyfile(self.base + ".roots.json", bad + ".roots.json")
        c = sqlite3.connect(bad)
        for q in sql:
            c.execute(q, (self.b,)) if "?" in q else c.execute(q)
        c.commit()
        c.close()
        return bad

    def test_refuses_unfinished_base(self):
        bad = self.variant("nofin.db", "DELETE FROM meta WHERE key='finished_at'")
        with self.assertRaisesRegex(RefetchError, "finished_at"):
            refetch(bad, self.out, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)
        bad = self.variant("pending.db", "UPDATE work SET status='pending' WHERE id=?")
        with self.assertRaisesRegex(RefetchError, "unfinished"):
            refetch(bad, self.out, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)

    def test_refetch_from_partial_sidecars_does_not_modify_the_source(self):
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                base = self.variant("closed + #?%.db" + suffix + ".db")
                with contextlib.closing(sqlite3.connect(base)) as writer:
                    self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
                if suffix == "-shm":
                    # SQLite creates the real WAL index (normally 32 KiB) on this fixture.
                    with contextlib.closing(sqlite3.connect(Path(base).as_uri() + "?mode=ro",
                                                            uri=True)) as reader:
                        reader.execute("SELECT value FROM meta").fetchall()
                    os.unlink(base + "-wal")
                else:
                    open(base + suffix, "xb").close()
                alias = base + ".alias"
                os.symlink(base, alias)
                shutil.copyfile(base + ".roots.json", alias + ".roots.json")
                before = self.sidecar_signature(base)
                out = self.out + suffix + ".db"
                refetch(alias, out, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)
                self.assertTrue(os.path.isfile(out))
                self.assertTrue(dump(out)["meta"]["finished_at"])
                self.assertEqual(self.sidecar_signature(base), before)

    @staticmethod
    def sidecar_signature(base):
        return {suffix: (sha_file(base + suffix), os.stat(base + suffix).st_mtime_ns)
                for suffix in ("", "-wal", "-shm", "-journal") if os.path.exists(base + suffix)}

    def test_refetch_escapes_base_path_once_and_reads_empty_wal(self):
        base = self.variant("base with #?%.db")
        # A stopped WAL database can retain WAL and SHM files after checkpoint.
        with contextlib.closing(sqlite3.connect(base)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO meta VALUES('test_wal', 'committed')")
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.assertEqual(os.stat(base + "-wal").st_size, 0)
            before = (sha_file(base), os.stat(base).st_mtime_ns)
            refetch(base, self.out, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)
            self.assertEqual(before, (sha_file(base), os.stat(base).st_mtime_ns))
            self.assertEqual(os.stat(base + "-wal").st_size, 0)
        self.assertTrue(os.path.isfile(self.out))
        self.assertEqual(dump(self.out)["meta"]["test_wal"], "committed")

    def test_refetch_refuses_first_wal_commit_during_read(self):
        base = self.variant("closed at open.db")
        with contextlib.closing(sqlite3.connect(base)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO meta VALUES('at_checkpoint', 'old')")
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.assertEqual(os.stat(base + "-wal").st_size, 0)
            sync = wb_refetch._fsync_file

            def commit_when_copy_is_ready(path):
                sync(path)
                writer.execute("UPDATE meta SET value='new' WHERE key='at_checkpoint'")
                writer.commit()
                self.assertGreater(os.stat(base + "-wal").st_size, 0)

            with patch.object(wb_refetch, "_fsync_file", commit_when_copy_is_ready):
                with self.assertRaisesRegex(RefetchError, "base snapshot changed"):
                    refetch(base, self.out, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)
        self.assertFalse(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.out + ".roots.json"))

    def test_refuses_non_empty_wal(self):
        bad = self.variant("walbase.db")
        with open(bad + "-wal", "wb") as fh:
            fh.write(b"x" * 64)
        with self.assertRaisesRegex(RefetchError, "not empty"):
            refetch(bad, self.out, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)

    def test_refuses_unknown_ids_and_bad_ids(self):
        self.env.fake.clear_requests()
        with self.assertRaisesRegex(RefetchError, "not pages"):
            refetch(self.base, self.out, ["0" * 32], [], self.env.ro(), blobs_dir=self.env.blobs)
        blk = self.env.fake.child_ids(self.a)[0]
        with self.assertRaisesRegex(RefetchError, "not pages"):
            refetch(self.base, self.out, [blk], [], self.env.ro(), blobs_dir=self.env.blobs)
        with self.assertRaisesRegex(RefetchError, "not a Notion id"):
            refetch(self.base, self.out, ["nope"], [], self.env.ro(), blobs_dir=self.env.blobs)
        self.assertEqual(self.env.fake.requests, [])     # refused before any request
        self.assertFalse(os.path.exists(self.out))

    def test_workspace_guard(self):
        bad = self.variant("ws.db", "UPDATE meta SET value='00000000-0000-0000-0000-000000000000' WHERE key='workspace_id'")
        with self.assertRaisesRegex(RefetchError, "workspace"):
            refetch(bad, self.out, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)

    def test_workspace_guard_cannot_be_skipped(self):
        """Finding 4: no check_workspace switch; a base without meta.workspace_id is refused before any request."""
        self.assertNotIn("check_workspace", inspect.signature(refetch).parameters)
        with self.assertRaises(TypeError):
            refetch(self.base, self.out, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs, check_workspace=False)
        bad = self.variant("nows.db", "DELETE FROM meta WHERE key='workspace_id'")
        self.env.fake.clear_requests()
        with self.assertRaisesRegex(RefetchError, "workspace_id"):
            refetch(bad, self.out, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)
        self.assertEqual(self.env.fake.requests, [])
        self.assertFalse(os.path.exists(self.out))

    def test_trashed_object_of_a_full_snapshot_is_a_marker(self):
        """A page/database whose stored JSON says in_trash counts as a trash marker (re-checked, never reused)
        even without meta.trash_markers; compact and spaced JSON are both recognised."""
        bad = self.variant("trashed_obj.db", "UPDATE objects SET data=json_set(data, '$.in_trash', json('true')) "
                                             "WHERE id=? AND kind='page'")
        self.assertEqual(wb_refetch._inspect_base(bad, [], [])[3], {self.b})
        self.assertEqual(wb_refetch._inspect_base(self.base, [], [])[3], set())

    def test_ungated_client_refused(self):
        """Finding 4: a ReadOnlyClient that is not behind the shared RateGate is refused (0 requests); a
        per-process cap in front of the gate (_CapThenGate) is accepted."""
        self.env.fake.clear_requests()
        bare = make_read_client(base_url=self.env.base_url, token=self.env.fake.secret("ro"), rate=None,
                                sleep=lambda s: None)
        with self.assertRaisesRegex(RefetchError, "RateGate"):
            refetch(self.base, self.out, [self.a], [], bare, blobs_dir=self.env.blobs)
        bare.session.close()
        self.assertEqual(self.env.fake.requests, [])
        self.assertFalse(os.path.exists(self.out))
        capped = make_read_client(base_url=self.env.base_url, token=self.env.fake.secret("ro"), rate=1000,
                                  gate=self.env.gate, sleep=lambda s: None)
        self.assertEqual(type(capped.limiter).__name__, "_CapThenGate")
        st = refetch(self.base, self.out, [self.a], [], capped, blobs_dir=self.env.blobs)
        self.assertTrue(st["rate_gate"])

    def test_undashed_ids_accepted(self):
        out, st = self.do_refetch([self.a.replace("-", "").upper()])
        self.assertEqual(st["requested"]["page_ids"], [self.a])


# ----------------------------------------------------------------------------------------- misc API
class MiscTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_wb_refetch_m_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prune_wb_snapshots(self):
        wb = os.path.join(self.tmp, "snapshots", "wb")
        os.makedirs(wb)
        main = os.path.join(self.tmp, "snapshots", "snap_20261101T0000.db")
        open(main, "w").close()
        now = time.time()
        old, new, keep = (os.path.join(wb, f"snap_{n}.db") for n in ("old", "new", "kept"))
        part = os.path.join(wb, ".snap_x.db.part-1-ab")
        for p in (old, old + ".roots.json", old + "-wal", new, new + ".roots.json", keep, part, os.path.join(wb, "notes.txt")):
            open(p, "w").close()
        for p in (old, keep, part, os.path.join(wb, "notes.txt"), main):
            os.utime(p, (now - 8 * 86400, now - 8 * 86400))
        os.utime(new, (now - 6 * 86400, now - 6 * 86400))
        removed = wb_refetch.prune_wb_snapshots(wb, now=now, keep=[keep], state_dir=self.tmp)
        self.assertEqual(sorted(removed), sorted([old, old + "-wal", old + ".roots.json", part]))
        self.assertEqual(sorted(os.listdir(wb)), sorted(["snap_new.db", "snap_new.db.roots.json", "snap_kept.db", "notes.txt"]))
        self.assertTrue(os.path.exists(main))
        with self.assertRaises(ValueError):
            wb_refetch.prune_wb_snapshots(os.path.dirname(wb), now=now, state_dir=self.tmp)

    def test_prune_keeps_adopted_snapshot_and_removes_orphan_roots(self):
        """Finding 3/2: the snapshot recorded by record_adopted() (next refetch base) is never pruned; an orphan
        snap_*.db.roots.json left by an interrupted publish is; an unreadable record prunes nothing."""
        wb = os.path.join(self.tmp, "snapshots", "wb")
        os.makedirs(wb)
        now = time.time()
        adopted, other = os.path.join(wb, "snap_adopted.db"), os.path.join(wb, "snap_other.db")
        orphan, young_orphan = os.path.join(wb, "snap_gone.db.roots.json"), os.path.join(wb, "snap_new.db.roots.json")
        for p in (adopted, adopted + ".roots.json", other, other + ".roots.json", orphan, young_orphan):
            open(p, "w").close()
        for p in (adopted, adopted + ".roots.json", other, other + ".roots.json", orphan):
            os.utime(p, (now - 9 * 86400, now - 9 * 86400))
        state = os.path.join(self.tmp, "state")
        os.makedirs(state)
        with open(os.path.join(state, wb_refetch.ADOPTED_FILE), "w", encoding="utf-8") as fh:
            fh.write("{broken")
        self.assertEqual(wb_refetch.prune_wb_snapshots(wb, now=now, state_dir=state), [])   # unreadable -> keep all
        with open(os.path.join(state, wb_refetch.ADOPTED_FILE), "w", encoding="utf-8") as fh:
            json.dump({"snapshot": adopted, "at": "2026-11-08T10:00:00+07:00", "at_epoch": now - 9 * 86400}, fh)
        removed = wb_refetch.prune_wb_snapshots(wb, now=now, state_dir=state)
        self.assertEqual(sorted(removed), sorted([other, other + ".roots.json", orphan]))
        self.assertEqual(sorted(os.listdir(wb)), sorted(["snap_adopted.db", "snap_adopted.db.roots.json",
                                                         "snap_new.db.roots.json"]))

    def test_default_out_path(self):
        import datetime
        t = datetime.datetime(2026, 11, 17, 23, 5, 9)
        p, q = wb_refetch.default_out_path(t), wb_refetch.default_out_path(t)
        self.assertRegex(p, r"/snapshots/wb/snap_20261117T230509_[0-9a-f]{8}\.db$")
        self.assertNotEqual(p, q)                                  # two runs in the same second never collide

    def test_make_client_uses_gate(self):
        gate_path = os.path.join(self.tmp, "gate.json")
        c = wb_refetch.make_client(base_url="http://127.0.0.1:9/v1", token="secret-x", gate_path=gate_path,
                                   total_rps=50)
        self.assertIsInstance(c, ReadOnlyClient)
        self.assertEqual(type(c.limiter).__name__, "RateGate")
        self.assertEqual(c.limiter.path, gate_path)
        self.assertEqual(c.limiter.total_rps, 50.0)
        c2 = wb_refetch.make_client(base_url="http://127.0.0.1:9/v1", token="secret-x", gate_path=gate_path, rate=1.0)
        self.assertEqual(type(c2.limiter).__name__, "_CapThenGate")
        self.assertFalse(os.path.exists(gate_path))          # nothing acquired, nothing written

    def test_module_never_imports_write_paths(self):
        with open(os.path.join(TOOLS, "wb_refetch.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("writeback_client", src)
        self.assertNotIn("load_writeback_token", src)
        self.assertNotRegex(src, r"sqlite3\.connect\((?!dst\))[^)]*base")   # base only via open_immutable


# ------------------------------------------------------------------------- chained bases (fix round)
class ChainedBaseTests(_Small):
    """Finding 1: a refetch output used as the base of the next refetch. Its trash markers (content dropped)
    are re-checked: restored objects come back with their whole subtree, never reused from the base.
    Workspace: _Small + P{C{C2}, Inner db{Inner 1}} under Home, created with a clock 2 h in the past so
    snapshot.py's unchanged-page reuse rule WOULD apply to them."""

    def setUp(self):
        super().setUp()
        f = self.env.fake
        real_now = f.now
        f.now = lambda: real_now() - datetime.timedelta(hours=2)
        try:
            self.p = f.add_page("P", parent=self.home, children=[para("p body")])
            self.c = f.add_page("C", parent=self.p, children=[para("c body")])
            self.c2 = f.add_page("C2", parent=self.c, children=[para("c2 body")])
            self.idb, self.ids_ = f.add_database(self.p, "Inner", {"Name": "title"})
            self.ir = f.add_row(self.ids_, "Inner 1", children=[para("inner row body")])
        finally:
            f.now = real_now
        self.subtree = {self.p, self.c, self.c2, self.idb, self.ir}
        self.crawl_base()

    def set_trash(self, oid, flag):
        wb = self.env.wb()
        self.assertTrue(wb.update_page(oid, {"in_trash": flag}).ok)
        wb.session.close()

    def append(self, pid, text):
        wb = self.env.wb()
        self.assertTrue(wb.append_children(pid, [para(text)]).ok)
        wb.session.close()

    def refetch_to(self, name, base, pages, dss=()):
        out = os.path.join(self.env.snaps, "wb", name)
        return out, refetch(base, out, pages, list(dss), self.env.ro(), blobs_dir=self.env.blobs)

    def w1_with_p_trashed(self):
        self.set_trash(self.p, True)
        w1, st = self.refetch_to("w1.db", self.base, [self.home, self.p])
        self.assertEqual(set(json.loads(dump(w1)["meta"]["trash_markers"])), self.subtree)
        self.assertEqual(st["restored"], [])
        return w1

    def assert_restored(self, w2, st):
        d = self.assert_equals_fresh(w2, st)       # nothing left out: no markers, export == fresh, C02 PASS
        self.assertEqual(json.loads(d["meta"]["trash_markers"]), [])
        self.assertEqual({r["id"] for r in st["restored"]}, self.subtree)
        self.assertFalse(d["objects"][(self.ir, "page")]["data"]["in_trash"])
        ir_blocks = [v for v in d["blocks"].values() if v[0] == self.ir]
        self.assertEqual(len(ir_blocks), 1)              # the row's body is back (never reused from the marker)
        bad = {c: r["status"] for c, r in self.last_verify["checks"].items() if r["status"] != "PASS"}
        self.assertEqual(bad, {}, bad)

    def test_restored_page_found_through_listed_parent(self):
        """Reviewer probe3 (a): P restored, only Home (edited) listed, base = the previous refetch output."""
        w1 = self.w1_with_p_trashed()
        self.set_trash(self.p, False)
        self.append(self.home, "later edit")
        w2, st = self.refetch_to("w2.db", w1, [self.home])
        self.assert_restored(w2, st)

    def test_restored_page_listed_itself(self):
        """Reviewer probe1 chain: P restored and listed with its parent."""
        w1 = self.w1_with_p_trashed()
        self.set_trash(self.p, False)
        w2, st = self.refetch_to("w2.db", w1, [self.home, self.p])
        self.assert_restored(w2, st)

    def test_restored_page_found_without_listing_anything_related(self):
        """The marker re-check finds the restore even when neither P nor its parent is listed."""
        w1 = self.w1_with_p_trashed()
        self.set_trash(self.p, False)
        w2, st = self.refetch_to("w2.db", w1, [self.a])
        self.assertEqual(st["markers_rechecked"], 5)
        self.assertIn(self.home, st["refetched_pages"])   # the container, so its listing shows P again
        self.assert_restored(w2, st)

    def test_still_trashed_markers_stay_and_chain_further(self):
        """Still-trashed markers: one GET for the top marker only, carried into the next output; a third
        refetch from that output then restores them."""
        w1 = self.w1_with_p_trashed()
        self.append(self.home, "home2")    # (a root: no parent listing to refresh)
        self.env.fake.clear_requests()
        w2, st = self.refetch_to("w2.db", w1, [self.home])
        self.assertEqual(st["markers_rechecked"], 1)
        text = json.dumps([[r["path"], r["query"]] for r in self.env.fake.requests])
        for x in (self.c, self.c2, self.idb, self.ir):
            self.assertNotIn(x, text)
        d = self.assert_equals_fresh(w2, st, "w2")
        self.assertEqual(set(json.loads(d["meta"]["trash_markers"])), self.subtree)
        self.assertEqual(st["trashed"], [])
        self.assertTrue(d["objects"][(self.p, "page")]["data"]["in_trash"])
        self.set_trash(self.p, False)
        w3, st3 = self.refetch_to("w3.db", w2, [self.a])
        self.assert_restored(w3, st3)

    def test_deleted_marker_is_pruned(self):
        w1 = self.w1_with_p_trashed()
        f = self.env.fake
        with f._lock:                                    # permanently deleted from the trash
            f.kids[self.home].remove(self.p)
            f.pages.pop(self.p)
            f.up.pop(self.p, None)
        w2, st = self.refetch_to("w2.db", w1, [self.a])
        pruned = {p["id"]: p for p in st["pruned"]}
        self.assertGreaterEqual(set(pruned), self.subtree)
        self.assertTrue(all(pruned[x]["was_marker"] for x in self.subtree))
        d = self.assert_equals_fresh(w2, st)
        self.assertEqual(json.loads(d["meta"]["trash_markers"]), [])

    def test_retrashed_listed_marker_keeps_was_marker(self):
        """A still-trashed marker listed explicitly is re-marked; was_marker tells I2 not to archive it again."""
        w1 = self.w1_with_p_trashed()
        w2, st = self.refetch_to("w2.db", w1, [self.p])
        tr = {t["id"]: t for t in st["trashed"]}
        self.assertIn(self.p, tr)
        self.assertTrue(tr[self.p]["was_marker"])
        self.assertEqual(set(json.loads(dump(w2)["meta"]["trash_markers"])), self.subtree)

    def test_restored_root(self):
        self.set_trash(self.other, True)
        w1, st1 = self.refetch_to("w1.db", self.base, [self.other])
        with open(w1 + ".roots.json", encoding="utf-8") as fh:
            self.assertTrue({r["id"]: r for r in json.load(fh)}[self.other]["in_trash"])
        self.set_trash(self.other, False)
        w2, st = self.refetch_to("w2.db", w1, [self.a])
        with open(w2 + ".roots.json", encoding="utf-8") as fh:
            self.assertFalse({r["id"]: r for r in json.load(fh)}[self.other]["in_trash"])
        self.assertEqual({r["id"] for r in st["restored"]}, {self.other, self.e})
        d = self.assert_equals_fresh(w2, st)
        self.assertEqual(json.loads(d["meta"]["trash_markers"]), [])

    def test_markers_are_never_reused_from_the_base(self):
        """Defence in depth: snapshot.py's unchanged-page rule never sees a marker (its blocks were dropped)."""
        w1 = self.w1_with_p_trashed()
        scratch = os.path.join(self.env.tmp, "scratch.db")
        store = snapmod.Store(scratch)
        try:
            inv = wb_refetch._TargetedInventory(self.env.ro(), store, prev_path=w1, blobs_dir=self.env.blobs,
                                                workers=1, markers=self.subtree)
            plain = snapmod.IncrementalInventory(self.env.ro(), store, workers=1, hash_files=True, comments=False,
                                                 prev_path=w1)
            self.assertTrue(self.subtree - {self.idb} <= set(plain.prev_pages))   # what the rule would reuse
            self.assertFalse(self.subtree & set(inv.prev_pages))
            self.assertIn(self.a, inv.prev_pages)
            for i in (inv, plain):
                i.prev.close()
                i.http.close()
        finally:
            store.db.close()

    def test_legacy_wb_base_refused(self):
        w1 = self.w1_with_p_trashed()
        legacy = os.path.join(self.env.tmp, "legacy.db")
        wb_refetch.copy_snapshot(w1, legacy)
        shutil.copyfile(w1 + ".roots.json", legacy + ".roots.json")
        c = sqlite3.connect(legacy)                      # a temp copy
        c.execute("DELETE FROM meta WHERE key='trash_markers'")
        c.commit()
        c.close()
        with self.assertRaisesRegex(RefetchError, "older wb_refetch"):
            self.refetch_to("w2.db", legacy, [self.a])


class ConsecutiveWritebackTests(_Small):
    """Finding 2: two write-backs before the next refresh. choose_base() returns the snapshot the vault was last
    published from, so the second refetch keeps what the first adoption published."""

    def setUp(self):
        super().setUp()
        self.crawl_base()
        self.state = os.path.join(self.env.tmp, "state")
        os.makedirs(self.state)
        self.publish_refresh(self.base, 0)

    def publish_refresh(self, snap, offset_s):
        """What refresh.py's mark_published writes (D-17)."""
        with open(os.path.join(self.state, "last_published_snapshot.txt"), "w", encoding="utf-8") as fh:
            fh.write(snap + "\n")
        at = (datetime.datetime.now().astimezone() + datetime.timedelta(seconds=offset_s)).isoformat(timespec="seconds")
        with open(os.path.join(self.state, "last_published.json"), "w", encoding="utf-8") as fh:
            json.dump({"run_id": "r", "snapshot": snap, "at": at}, fh)

    def note(self, vault, name):
        hits = [p for p in tree_files(vault) if os.path.basename(p) == name]
        self.assertEqual(len(hits), 1, hits)
        with open(os.path.join(vault, hits[0]), encoding="utf-8") as fh:
            return fh.read()

    def test_second_writeback_keeps_first_adoption(self):
        wb = self.env.wb()
        first = self.env.fake.child_ids(self.a)[0]
        self.assertTrue(wb.update_block(first, {"paragraph": {"rich_text": [{"type": "text",
                                                                             "text": {"content": "a EDITED by WB1"}}]}}).ok)
        base1 = wb_refetch.choose_base(state_dir=self.state)
        self.assertEqual(base1, os.path.abspath(self.base))
        w1 = os.path.join(self.env.snaps, "wb", "w1.db")
        refetch(base1, w1, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)
        self.assertIn("a EDITED by WB1", self.note(export(w1, self.sidebar, os.path.join(self.env.tmp, "e1"),
                                                          self.env.work), "A.md"))
        rec = wb_refetch.record_adopted(w1, plan_id="WB1", state_dir=self.state)   # the adoption was published
        self.assertEqual(rec["snapshot"], os.path.abspath(w1))
        # --- write-back 2 before any refresh ---
        self.assertTrue(wb.append_children(self.home, [para("WB2 edit on Home")]).ok)
        wb.session.close()
        base2 = wb_refetch.choose_base(state_dir=self.state)
        self.assertEqual(base2, os.path.abspath(w1))
        w2 = os.path.join(self.env.snaps, "wb", "w2.db")
        st = refetch(base2, w2, [self.home], [], self.env.ro(), blobs_dir=self.env.blobs)
        self.assert_equals_fresh(w2, st)                 # whole export == a fresh crawl's
        v2 = export(w2, self.sidebar, os.path.join(self.env.tmp, "e2"), self.env.work)
        self.assertIn("a EDITED by WB1", self.note(v2, "A.md"))
        self.assertIn("WB2 edit on Home", self.note(v2, "Home.md"))
        # the rule matters: refetching from the refresh snapshot would export WB1's note stale
        bad = os.path.join(self.env.snaps, "wb", "w2_from_refresh.db")
        refetch(self.base, bad, [self.home], [], self.env.ro(), blobs_dir=self.env.blobs)
        self.assertNotIn("a EDITED by WB1", self.note(export(bad, self.sidebar, os.path.join(self.env.tmp, "eb"),
                                                             self.env.work), "A.md"))
        # a later refresh publish wins again
        fresh = os.path.join(self.env.snaps, "snap_later.db")
        crawl(self.env.ro(), self.roots, fresh)
        self.publish_refresh(fresh, 60)
        self.assertEqual(wb_refetch.choose_base(state_dir=self.state), os.path.abspath(fresh))

    def test_choose_base_rules(self):
        st = self.state
        self.assertEqual(wb_refetch.choose_base(state_dir=st), os.path.abspath(self.base))
        # an adoption recorded BEFORE the last refresh publish loses
        w1 = os.path.join(self.env.snaps, "wb", "w1.db")
        refetch(self.base, w1, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)
        wb_refetch.record_adopted(w1, state_dir=st)
        self.assertEqual(wb_refetch.choose_base(state_dir=st), os.path.abspath(w1))
        self.publish_refresh(self.base, 60)
        self.assertEqual(wb_refetch.choose_base(state_dir=st), os.path.abspath(self.base))
        # .txt mtime is the publish time when last_published.json names another snapshot
        os.unlink(os.path.join(st, "last_published.json"))
        os.utime(os.path.join(st, "last_published_snapshot.txt"), (time.time() - 3600, time.time() - 3600))
        self.assertEqual(wb_refetch.choose_base(state_dir=st), os.path.abspath(w1))
        # the newer one missing -> fail closed (never fall back to an older publish)
        os.rename(w1, w1 + ".moved")
        with self.assertRaisesRegex(RefetchError, "missing"):
            wb_refetch.choose_base(state_dir=st)
        os.rename(w1 + ".moved", w1)
        with open(os.path.join(st, wb_refetch.ADOPTED_FILE), "w", encoding="utf-8") as fh:
            fh.write("[]")
        with self.assertRaisesRegex(RefetchError, "malformed"):
            wb_refetch.choose_base(state_dir=st)
        empty = os.path.join(self.env.tmp, "empty_state")
        os.makedirs(empty)
        with self.assertRaisesRegex(RefetchError, "run a refresh"):
            wb_refetch.choose_base(state_dir=empty)

    def test_record_adopted_only_accepts_refetch_outputs(self):
        with self.assertRaisesRegex(RefetchError, "not a finished refetch output"):
            wb_refetch.record_adopted(self.base, state_dir=self.state)      # a refresh snapshot
        with self.assertRaisesRegex(RefetchError, "missing"):
            wb_refetch.record_adopted(os.path.join(self.env.tmp, "nope.db"), state_dir=self.state)
        self.assertFalse(os.path.exists(os.path.join(self.state, wb_refetch.ADOPTED_FILE)))


class PublishRaceTests(_Small):
    """Finding 3: the output is published with hard links; anything that appears at out_path (or its
    .roots.json) while the refetch runs is never overwritten, and nothing of ours is left behind."""

    def setUp(self):
        super().setUp()
        self.crawl_base()
        self.out = os.path.join(self.env.snaps, "wb", "snap_race.db")

    def run_with_hook(self, create_db, create_roots):
        orig = wb_refetch._fsync_file

        def hook(p):
            orig(p)
            if create_db:
                with open(self.out, "wb") as fh:
                    fh.write(b"OTHER PROCESS RESULT")
            if create_roots:
                with open(self.out + ".roots.json", "wb") as fh:
                    fh.write(b"[]")
        wb_refetch._fsync_file = hook
        try:
            with self.assertRaisesRegex(RefetchError, "refusing to overwrite"):
                refetch(self.base, self.out, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)
        finally:
            wb_refetch._fsync_file = orig

    def test_out_and_roots_appearing_during_run(self):
        self.run_with_hook(True, True)
        with open(self.out, "rb") as fh:
            self.assertEqual(fh.read(), b"OTHER PROCESS RESULT")
        with open(self.out + ".roots.json", "rb") as fh:
            self.assertEqual(fh.read(), b"[]")
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.out))), ["snap_race.db", "snap_race.db.roots.json"])

    def test_roots_appearing_during_run(self):
        self.run_with_hook(False, True)
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.out))), ["snap_race.db.roots.json"])

    def test_db_appearing_after_roots_published(self):
        """The db link fails after our .roots.json was linked: our .roots.json is removed again."""
        orig = wb_refetch._link_new
        calls = []

        def link(src, dst):
            calls.append(dst)
            if dst == self.out:
                with open(self.out, "wb") as fh:
                    fh.write(b"OTHER PROCESS RESULT")
            return orig(src, dst)
        wb_refetch._link_new = link
        try:
            with self.assertRaisesRegex(RefetchError, "refusing to overwrite"):
                refetch(self.base, self.out, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)
        finally:
            wb_refetch._link_new = orig
        self.assertEqual(calls, [self.out + ".roots.json", self.out])
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.out))), ["snap_race.db"])
        with open(self.out, "rb") as fh:
            self.assertEqual(fh.read(), b"OTHER PROCESS RESULT")


class BlobDirTests(_Small):
    """Finding 5: the default blob dir (<WORK>/blobs) is refused when it resolves outside <WORK> (the dev
    sandbox's blobs/ is a symlink to production); an explicit blobs_dir is always honoured."""

    def setUp(self):
        super().setUp()
        self.env.fake.add_blocks(self.a, [image(self.env.files.base + "/a.png")])
        self.crawl_base()
        self.work = os.path.join(self.env.tmp, "fakework")
        os.makedirs(self.work)
        self.saved = (wb_refetch.WORK, snapmod.BLOBS)
        wb_refetch.WORK = self.work
        snapmod.BLOBS = os.path.join(self.work, "blobs")

    def tearDown(self):
        wb_refetch.WORK, snapmod.BLOBS = self.saved
        super().tearDown()

    def test_symlinked_default_outside_work_refused(self):
        outside = os.path.join(self.env.tmp, "production_blobs")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.work, "blobs"))
        self.env.fake.clear_requests()
        out = os.path.join(self.env.snaps, "wb", "snap_b.db")
        with self.assertRaisesRegex(RefetchError, "outside"):
            refetch(self.base, out, [self.a], [], self.env.ro())
        self.assertEqual(self.env.fake.requests, [])
        self.assertEqual(os.listdir(outside), [])
        self.assertFalse(os.path.exists(out))
        st = refetch(self.base, out, [self.a], [], self.env.ro(), blobs_dir=self.env.blobs)   # explicit: honoured
        self.assertIn(self.a, st["refetched_pages"])
        self.assertEqual(os.listdir(outside), [])

    def test_default_inside_work_is_used(self):
        os.makedirs(os.path.join(self.work, "blobs", "tmp"))
        out = os.path.join(self.env.snaps, "wb", "snap_b.db")
        refetch(self.base, out, [self.a], [], self.env.ro())
        self.assertIn(hashlib.sha256(IMG).hexdigest(), os.listdir(os.path.join(self.work, "blobs")))


class CliTests(_Small):
    def test_cli_against_fake(self):
        self.crawl_base()
        tok = os.path.join(self.env.tmp, "ro.env")
        with open(tok, "w", encoding="utf-8") as fh:
            fh.write(f"NOTION_TARGET_TOKEN={self.env.fake.secret('ro')}\n")
        out = os.path.join(self.env.snaps, "wb", "snap_cli.db")
        env = dict(os.environ)
        env.pop("NOTION_TARGET_TOKEN", None)
        cmd = [sys.executable, "-I", "-B", os.path.join(TOOLS, "wb_refetch.py"), "--base", self.base, "--out", out,
               "--pages", self.a, "--base-url", self.env.base_url, "--token-file", tok,
               "--gate-state", os.path.join(self.env.tmp, "gate.json"), "--total-rps", "500",
               "--blobs", self.env.blobs, "--workers", "2"]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        st = json.loads(p.stdout)
        self.assertEqual(st["refetched_pages"], [self.a])
        self.assertTrue(os.path.isfile(out))
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)   # out exists now
        self.assertEqual(p.returncode, 2)
        self.assertIn("refusing to overwrite", p.stderr)
        self.assertNotIn(self.env.fake.secret("ro"), p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
