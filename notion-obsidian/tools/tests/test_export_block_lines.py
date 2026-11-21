"""Unit tests for the exporter's write-back hooks (module B): block_lines recording (zero byte change),
side outputs, Exporter.for_planning / render_page / link_index, name overrides, wb_norm (D-10).

Fast: a small synthetic snapshot built in a temp dir (no network, no production paths).
Run: cd /data120/Obsidian/notion-obsidian-dev && python3 -I -B -m unittest tools/tests/test_export_block_lines.py
The full-corpus byte-identity regression is tools/tests/regress_export_bytes.py (run explicitly).
"""
import copy
import hashlib
import inspect
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections import Counter, defaultdict

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
import export_obsidian as exp  # noqa: E402
import wb_norm  # noqa: E402
from sortspec import build_sortspec  # noqa: E402


# ------------------------------------------------------------------ fixture
def jload(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def jdump(obj, p):
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def readtext(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def uid(n):
    return f"{n:08x}-0000-4000-8000-{n:012x}"


ROOT, LEAF, LEAF2, SRC, OTHER, CROSS, DB, DS, ROW = (uid(i) for i in (1, 2, 3, 4, 5, 6, 7, 8, 9))
USER = uid(99)
S1 = uid(500)        # synced original (in SRC)
S1C = uid(501)       # its child paragraph
SREF = uid(123)      # synced reference (in ROOT)
IMG = uid(122)       # hosted image block in ROOT
BLOB = b"\x89PNG\r\n\x1a\n" + b"fixture-image-bytes" * 4
BLOB_SHA = hashlib.sha256(BLOB).hexdigest()
TS = "2026-01-02T03:04:00.000Z"


def ann(**kw):
    a = {"bold": False, "italic": False, "strikethrough": False, "underline": False, "code": False, "color": "default"}
    a.update(kw)
    return a


def txt(s, link=None, **kw):
    return {"type": "text", "text": {"content": s, "link": {"url": link} if link else None}, "annotations": ann(**kw),
            "plain_text": s, "href": link}


def mention_page(pid, label):
    return {"type": "mention", "mention": {"type": "page", "page": {"id": pid}}, "annotations": ann(),
            "plain_text": label, "href": "https://www.notion.so/" + pid.replace("-", "")}


def title_prop(s):
    return {"id": "title", "type": "title", "title": [txt(s)]}


def page_obj(pid, title, parent, extra_props=None, icon=None):
    props = {"title": title_prop(title)}
    props.update(extra_props or {})
    return {"object": "page", "id": pid, "created_time": "2026-01-01T00:00:00.000Z", "last_edited_time": TS,
            "created_by": {"object": "user", "id": USER}, "last_edited_by": {"object": "user", "id": USER},
            "cover": None, "icon": icon, "parent": parent, "archived": False, "in_trash": False,
            "properties": props, "url": "https://www.notion.so/x", "request_id": "req-1"}


class Snap:
    def __init__(self):
        self.objects, self.blocks, self.files = [], [], []
        self.n = 1000

    def nid(self):
        self.n += 1
        return uid(self.n)

    def obj(self, oid, kind, parent, data):
        self.objects.append((oid, kind, parent, data))

    def add(self, page, parent, typ, payload, bid=None, has_children=False):
        bid = bid or self.nid()
        ordv = sum(1 for b in self.blocks if b[2] == parent)
        par = {"type": "page_id", "page_id": parent} if parent == page else {"type": "block_id", "block_id": parent}
        data = {"object": "block", "id": bid, "parent": par, "created_time": TS, "last_edited_time": TS,
                "created_by": {"object": "user", "id": USER}, "last_edited_by": {"object": "user", "id": USER},
                "has_children": has_children, "in_trash": False, "type": typ, typ: payload}
        self.blocks.append((bid, page, parent, ordv, typ, int(has_children), data))
        return bid


def para(*rich, **kw):
    d = {"rich_text": list(rich), "color": kw.get("color", "default")}
    return d


def build_fixture(root_dir):
    """Write snapshot + roots + sidebar + blob store under root_dir; return dict of paths."""
    work = os.path.join(root_dir, "work")
    os.makedirs(os.path.join(work, "blobs"))
    with open(os.path.join(work, "blobs", BLOB_SHA), "wb") as fh:
        fh.write(BLOB)
    s = Snap()
    s.obj(ROOT, "page", None, page_obj(ROOT, "Root Page", {"type": "workspace", "workspace": True}, icon={"type": "emoji", "emoji": "📄"}))
    s.obj(LEAF, "page", ROOT, page_obj(LEAF, "Leaf/One", {"type": "page_id", "page_id": ROOT}))
    s.obj(LEAF2, "page", ROOT, page_obj(LEAF2, "Second", {"type": "page_id", "page_id": ROOT}))
    s.obj(SRC, "page", ROOT, page_obj(SRC, "Src Page", {"type": "page_id", "page_id": ROOT}))
    s.obj(OTHER, "page", None, page_obj(OTHER, "Other Root", {"type": "workspace", "workspace": True}))
    s.obj(CROSS, "page", OTHER, page_obj(CROSS, "Cross", {"type": "page_id", "page_id": OTHER}))
    s.obj(DB, "database", ROOT, {"object": "database", "id": DB, "title": [txt("Tasks")], "description": [],
                                 "parent": {"type": "page_id", "page_id": ROOT}, "data_sources": [{"id": DS, "name": "Tasks"}],
                                 "created_time": TS, "last_edited_time": TS, "in_trash": False, "icon": None, "cover": None,
                                 "_views": [{"object": "view", "id": uid(77), "name": "Table", "type": "table"}]})
    s.obj(DS, "data_source", DB, {"object": "data_source", "id": DS, "title": [txt("Tasks")],
                                  "properties": {"Name": {"id": "title", "name": "Name", "type": "title", "title": {}},
                                                 "Status": {"id": "s", "name": "Status", "type": "select",
                                                            "select": {"options": [{"name": "Doing"}]}}},
                                  "_row_ids": [ROW], "_template_ids": []})
    s.obj(ROW, "page", DB, page_obj(ROW, "Task 1", {"type": "data_source_id", "data_source_id": DS, "database_id": DB},
                                    extra_props={"Status": {"id": "s", "type": "select", "select": {"name": "Doing"}}}))
    del s.objects[-1][3]["properties"]["title"]
    s.objects[-1][3]["properties"]["Name"] = title_prop("Task 1")
    # ROOT body
    P = ROOT
    s.add(P, P, "paragraph", para())                                     # 0 leading blank (popped)
    s.add(P, P, "paragraph", para())                                     # 1 blank merged
    s.add(P, P, "heading_1", {"rich_text": [txt("Title A")], "is_toggleable": False, "color": "default"})
    s.add(P, P, "paragraph", para(txt("Hello "), txt("world", bold=True)))
    s.add(P, P, "paragraph", para())                                     # own blank line
    s.add(P, P, "paragraph", para())                                     # merged into the previous
    s.add(P, P, "numbered_list_item", {"rich_text": [txt("one")], "color": "default"})
    n2 = s.add(P, P, "numbered_list_item", {"rich_text": [txt("two")], "color": "default"}, has_children=True)
    s.add(P, n2, "bulleted_list_item", {"rich_text": [txt("nested")], "color": "default"})
    s.add(P, P, "numbered_list_item", {"rich_text": [txt("three")], "color": "default"})
    s.add(P, P, "bulleted_list_item", {"rich_text": [txt("b1")], "color": "default"})
    s.add(P, P, "to_do", {"rich_text": [txt("t1")], "checked": False, "color": "default"})
    q = s.add(P, P, "quote", {"rich_text": [txt("quoted")], "color": "default"}, has_children=True)
    s.add(P, q, "paragraph", para(txt("qchild")))
    s.add(P, P, "callout", {"rich_text": [txt("note text")], "icon": {"type": "emoji", "emoji": "💡"}, "color": "gray_background"})
    tg = s.add(P, P, "toggle", {"rich_text": [txt("tog")], "color": "default"}, has_children=True)
    s.add(P, tg, "paragraph", para(txt("inside toggle")))
    s.add(P, P, "code", {"rich_text": [txt("print(1)\nprint(2)")], "language": "python", "caption": [txt("cap")]})
    s.add(P, P, "code", {"rich_text": [txt("x = 1")], "language": "plain text", "caption": []})
    s.add(P, P, "equation", {"expression": "x^2"})
    s.add(P, P, "divider", {})
    t = s.add(P, P, "table", {"table_width": 2, "has_column_header": True, "has_row_header": False}, has_children=True)
    s.add(P, t, "table_row", {"cells": [[txt("h1")], [txt("h2")]]})
    s.add(P, t, "table_row", {"cells": [[txt("a|b")], []]})
    cl = s.add(P, P, "column_list", {}, has_children=True)
    c1 = s.add(P, cl, "column", {}, has_children=True)
    c2 = s.add(P, cl, "column", {}, has_children=True)
    s.add(P, c1, "paragraph", para(txt("left")))
    s.add(P, c2, "paragraph", para(txt("right")))
    s.add(P, P, "paragraph", para(txt("see "), mention_page(LEAF, "Leaf/One")))
    s.add(P, P, "paragraph", para(txt("red", color="red")))
    s.add(P, P, "paragraph", para(txt("link", link="https://www.notion.so/Second-" + LEAF2.replace("-", ""))))
    s.add(P, P, "image", {"type": "file", "file": {"url": "https://s3.example/x.png?sig=1", "expiry_time": TS}, "caption": []}, bid=IMG)
    s.add(P, P, "synced_block", {"synced_from": {"type": "block_id", "block_id": S1}}, bid=SREF, has_children=True)
    s.add(P, P, "child_page", {"title": "Leaf/One"}, bid=LEAF)
    s.add(P, P, "child_page", {"title": "Second"}, bid=LEAF2)
    s.add(P, P, "child_page", {"title": "Src Page"}, bid=SRC)
    s.add(P, P, "child_database", {"title": "Tasks"}, bid=DB)
    pk = s.add(P, P, "paragraph", para(txt("parent para")), has_children=True)
    s.add(P, pk, "paragraph", para(txt("kid para")))
    s.add(P, P, "paragraph", para(txt("multi\nline\n  indented")))
    s.add(P, P, "paragraph", para())                                     # trailing blank (popped)
    # other pages
    s.add(LEAF, LEAF, "paragraph", para(txt("leaf body")))
    s.add(SRC, SRC, "synced_block", {"synced_from": None}, bid=S1, has_children=True)
    s.add(SRC, S1, "paragraph", para(txt("synced text")), bid=S1C)
    s.add(SRC, SRC, "paragraph", para(txt("after")))
    s.add(OTHER, OTHER, "child_page", {"title": "Cross"}, bid=CROSS)
    s.add(CROSS, CROSS, "paragraph", para(txt("cross body")))
    s.add(ROW, ROW, "paragraph", para(txt("row body")))
    # write the snapshot (nmig schema)
    snap = os.path.join(root_dir, "snap.db")
    c = sqlite3.connect(snap)
    c.executescript("""
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE objects(id TEXT, kind TEXT, parent_id TEXT, root_id TEXT, depth INT, data TEXT, fetched_at TEXT, PRIMARY KEY(id, kind));
    CREATE TABLE blocks(id TEXT PRIMARY KEY, page_id TEXT, parent_id TEXT, ord INT, type TEXT, has_children INT, data TEXT);
    CREATE INDEX blocks_page ON blocks(page_id);
    CREATE TABLE files(key TEXT PRIMARY KEY, owner_id TEXT, owner_kind TEXT, field TEXT, name TEXT, size INT, sha256 TEXT, status TEXT, error TEXT);
    """)
    for oid, kind, parent, data in s.objects:
        c.execute("INSERT INTO objects VALUES(?,?,?,?,?,?,?)", (oid, kind, parent, None, 0, json.dumps(data, ensure_ascii=False), TS))
    for bid, page, parent, ordv, typ, hc, data in s.blocks:
        c.execute("INSERT INTO blocks VALUES(?,?,?,?,?,?,?)", (bid, page, parent, ordv, typ, hc, json.dumps(data, ensure_ascii=False)))
    c.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)", (f"{IMG}:image", IMG, "block", "image", "pic.png", len(BLOB), BLOB_SHA, "ok", "image/png"))
    c.commit()
    c.close()
    roots = [{"id": ROOT, "kind": "page", "teamspace": "General", "teamspace_order": 0, "root_order": 1, "in_trash": False, "title": "Root Page"},
             {"id": OTHER, "kind": "page", "teamspace": "x_to_notion", "teamspace_order": 2, "root_order": 1, "in_trash": False, "title": "Other Root"}]
    jdump(roots, snap + ".roots.json")
    sidebar = os.path.join(root_dir, "sidebar.json")
    jdump({"roots": roots}, sidebar)
    return {"work": work, "snap": snap, "sidebar": sidebar, "blocks": s.blocks}


def variant_snapshot(fx, root_dir, name, parent, pid, title):
    """Copy of the fixture snapshot with one NEW child page `pid` (title) appended under page `parent`."""
    dst = os.path.join(root_dir, name)
    shutil.copy2(fx["snap"], dst)
    shutil.copy2(fx["snap"] + ".roots.json", dst + ".roots.json")
    c = sqlite3.connect(dst)
    c.execute("INSERT INTO objects VALUES(?,?,?,?,?,?,?)",
              (pid, "page", parent, None, 0,
               json.dumps(page_obj(pid, title, {"type": "page_id", "page_id": parent}), ensure_ascii=False), TS))
    ordv = c.execute("SELECT COALESCE(MAX(ord), -1) + 1 FROM blocks WHERE parent_id=?", (parent,)).fetchone()[0]
    data = {"object": "block", "id": pid, "parent": {"type": "page_id", "page_id": parent}, "created_time": TS,
            "last_edited_time": TS, "has_children": False, "in_trash": False, "type": "child_page",
            "child_page": {"title": title}}
    c.execute("INSERT INTO blocks VALUES(?,?,?,?,?,?,?)",
              (pid, parent, parent, ordv, "child_page", 0, json.dumps(data, ensure_ascii=False)))
    c.commit()
    c.close()
    return dst


def blk(bid, typ, rich=None, **extra):
    """A minimal block JSON for overlays (bid None = a block the planner would create)."""
    b = {"object": "block", "type": typ, "has_children": False, typ: {"rich_text": list(rich or []), "color": "default"}}
    b[typ].update(extra)
    if bid is not None:
        b["id"] = bid
    return b


def render_alone(ex, b, prefix, own_lines, ts="General"):
    """R-042: the block rendered on its own (children cut) with the recorded prefix and list number."""
    b2 = dict(b)
    b2["has_children"] = False
    saved = (ex.report, ex.lists, ex._sandbox, ex._list_num, ex._rec)
    ex.report, ex.lists, ex._sandbox, ex._rec = defaultdict(Counter), defaultdict(list), True, None
    try:
        if b.get("type") == "numbered_list_item":
            m = re.match(re.escape(prefix) + r"(\d+)\.", own_lines[0] if own_lines else "")
            ex._list_num = int(m.group(1)) if m else 1
        return [str(x) for x in ex.render_block(b2, ts, prefix)]
    finally:
        ex.report, ex.lists, ex._sandbox, ex._list_num, ex._rec = saved


def r042_check(ex, text, rows, blocks_by_id):
    """(mismatching 'R?' rows, own_line_lost rows that DO re-render identically): both must be empty."""
    lines = text.split("\n")
    bad, lost_ok = [], []
    for r in rows:
        if r["type"] in ("gap", "table_row") or r["zero_line"] or r["own_start"] is None or r["block_id"] is None:
            continue
        own = lines[r["own_start"] - 1:r["own_end"]]
        if r["klass"] == "R?":
            if render_alone(ex, blocks_by_id[r["block_id"]], r["prefix"], own) != own:
                bad.append(r["block_id"])
        elif r["x_reason"] == "own_line_lost":
            if render_alone(ex, blocks_by_id[r["block_id"]], r["prefix"], own) == own:
                lost_ok.append(r["block_id"])
    return bad, lost_ok


# sha256 of every file the ORIGINAL renderer (export_obsidian.py as of 2026-11-17 09:49, sha256 3054b4b2…)
# writes for this fixture: computed once with that frozen copy; any change here means output bytes changed.
GOLDEN = {
    "General/Root Page/Leaf／One.md": "3aaf561760638f3c8174618d2ad1a3a1d8641b45e84b11e85ab45f74aef6c342",
    "General/Root Page/Root Page.md": "7c5ab1ab199056af80bb7858581faa8a7df8ce56ea03f03e71d90ed61972ba10",
    "General/Root Page/Second.md": "09fb45daad12162f7985b39e5e04b27281535bab8c2de18db04347de21731bcf",
    "General/Root Page/Src Page.md": "c3b173dfc7bf7fadf64e1f8ddfdeed97c954a2be1421f96804ed4e3ec290e0c3",
    "General/Root Page/Tasks/Task 1.md": "400ee67dfa99c2244d31b9796acb338907fb63461b5dbd9760b472acff1ee651",
    "General/Root Page/Tasks/Tasks.base": "d02563c4bb4eb77cc0135110957f6ca92647414d889e23ed0eb9d00cb5827ca2",
    "General/Root Page/Tasks/Tasks.md": "cc1344c212a8c7f966e98ce29fc8142f65629001f495062a8c67bff312f49be1",
    "General/_attachments/pic__7166ce3b.png": "7166ce3ba8e7082cba985f01f03bc598257a1aea88f68d4f76f822dbde33c17e",
    "Notion_Obsidian/Godcreate 目录索引.md": "7abf80025ec6c4452cc0bcd61739f361e0c62d81cc8913925d7632398d949afb",
    "Notion_Obsidian/README.md": "b26d5e100310bc0a89181441b82d83af178907cf20fd3981765d00f8f2608669",
    "Notion_Obsidian/sortspec.md": "8a467cea47a22cf4e73f9319cc8dd0c842cc5790f64b257111840931d7668eea",
    "Notion_Obsidian/映射清单.md": "ab823967c52a6e3d9ee41aac020c47280e88f8651928ad5921a5b19f2f5a8695",
    "x_to_notion/Other Root/Cross.md": "4873a5c47954feb8e89dfff3e64e4f478d9d88742d9d16e0de81dc9a62427171",
    "x_to_notion/Other Root/Other Root.md": "f37d9217487783f8f5b1c5dda82d1c6f716225d616bd58a7a3194469d4bca20f",
}


def export(fx, root_dir, name, **kw):
    out = os.path.join(root_dir, name)
    state = kw.pop("state", None) or os.path.join(root_dir, name + ".state.db")
    ex = exp.Exporter(fx["snap"], fx["sidebar"], name, state, out, immutable=True, **kw)
    rep = ex.run()
    man = jload(os.path.join(out, "manifest.json"))
    return out, state, ex, rep, man


def read_rows(out):
    rows = {}
    p = os.path.join(out, "block_lines.jsonl")
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            rows.setdefault(r["notion_id"], []).append(r)
    return rows


def body_bounds(text):
    lines = text.split("\n")
    j = lines.index("---", 1)
    return j + 3, len(lines) - 1


def partition_problems(text, rows):
    first, last = body_bounds(text)
    cover = {}
    for r in rows:
        if r["type"] == "gap" or (not r["zero_line"] and r["own_start"] is not None):
            for ln in range(r["own_start"], r["own_end"] + 1):
                cover[ln] = cover.get(ln, 0) + 1
    holes = [ln for ln in range(first, last + 1) if ln not in cover]
    dups = [ln for ln, c in cover.items() if c > 1]
    outside = [ln for ln in cover if ln < first or ln > last]
    return holes, dups, outside


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="test_export_bl_")
        cls.fx = build_fixture(cls.tmp)
        cls._work = exp.WORK
        exp.WORK = cls.fx["work"]           # blobs/ of the fixture

    @classmethod
    def tearDownClass(cls):
        exp.WORK = cls._work
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestBytesAndSideOutputs(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.out, cls.state, cls.ex, cls.rep, cls.man = export(cls.fx, cls.tmp, "run1")
        cls.rows = read_rows(cls.out)

    def test_renderer_version(self):
        self.assertEqual(exp.RENDERER_VERSION, "1.2.1")
        self.assertEqual(self.rep["renderer_version"], "1.2.1")

    def test_bytes_equal_original_renderer(self):
        got = {k: v["sha256"] for k, v in self.man.items()}
        self.assertEqual(got, GOLDEN)

    def test_no_block_lines_flag_same_bytes(self):
        out2, _, _, rep2, man2 = export(self.fx, self.tmp, "run_nobl", record_block_lines=False)
        self.assertEqual(man2, self.man)
        self.assertFalse(os.path.exists(os.path.join(out2, "block_lines.jsonl")))
        self.assertTrue(os.path.exists(os.path.join(out2, "page_json.jsonl")))
        self.assertTrue(os.path.exists(os.path.join(out2, "attachments.json")))
        self.assertNotIn("block_lines.jsonl", rep2["side_outputs"])

    def test_side_outputs_outside_vault_and_manifest(self):
        for f in ("block_lines.jsonl", "page_json.jsonl", "attachments.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.out, f)), f)
            self.assertFalse(any(k.endswith(f) for k in self.man))
        for root, _, files in os.walk(os.path.join(self.out, "vault")):
            self.assertFalse({"block_lines.jsonl", "page_json.jsonl", "attachments.json"} & set(files))
        self.assertFalse([f for f in os.listdir(self.out) if f.endswith(".tmp")])

    def test_page_json(self):
        pj = [json.loads(x) for x in readtext(os.path.join(self.out, "page_json.jsonl")).splitlines()]
        notes = {k: v for k, v in self.man.items() if v["kind"] in ("page", "row", "template", "database")}
        self.assertEqual(len(pj), len(notes))
        for r in pj:
            self.assertEqual(set(r), {"notion_id", "kind", "rel_path", "sha256", "page_json_sha", "renderer_version"})
            m = self.man[r["rel_path"]]
            self.assertEqual((m["sha256"], m["kind"], m["notion_id"]), (r["sha256"], r["kind"], r["notion_id"]))
            self.assertEqual(r["renderer_version"], "1.2.1")
            self.assertRegex(r["page_json_sha"], r"^[0-9a-f]{64}$")
        by = {r["notion_id"]: r for r in pj}
        self.assertEqual(by[DB]["kind"], "database")
        self.assertEqual(by[ROW]["kind"], "row")
        # page_json_sha = wb_norm over the snapshot page + DFS blocks (synced children excluded)
        ex = self.ex
        want = wb_norm.page_json_sha(ex.nodes[ROOT]["data"], wb_norm.page_blocks_dfs(ex.blocks.get, ROOT))
        self.assertEqual(by[ROOT]["page_json_sha"], want)

    def test_attachments_json(self):
        att = jload(os.path.join(self.out, "attachments.json"))
        rel = att[f"{IMG}:image"]
        self.assertTrue(rel.startswith("General/_attachments/pic__") and rel.endswith(".png"))
        self.assertIn(rel, self.man)
        self.assertEqual(self.man[rel]["kind"], "attachment")

    def test_rows_shape_and_base_sha(self):
        notes = {v["notion_id"]: (k, v) for k, v in self.man.items() if v["kind"] in ("page", "row", "template")}
        self.assertEqual(set(self.rows), {n for n in notes if any(b[1] == n for b in self.fx["blocks"])})
        for nid, rows in self.rows.items():
            rel, m = notes[nid]
            self.assertEqual([r["seq"] for r in rows], list(range(len(rows))))
            for r in rows:
                self.assertEqual(tuple(r), exp.BLOCK_LINE_KEYS)
                self.assertEqual(r["base_sha"], m["sha256"])
                self.assertEqual(r["renderer_version"], "1.2.1")

    def test_partition_every_note(self):
        for nid, rows in self.rows.items():
            rel = next(k for k, v in self.man.items() if v["notion_id"] == nid and v["kind"] != "base")
            text = readtext(os.path.join(self.out, "vault", rel))
            self.assertEqual(partition_problems(text, rows), ([], [], []), rel)
            self.assertFalse([r for r in rows if r["x_reason"] == "untagged"])
        bl = self.rep["block_lines"]
        self.assertEqual(bl.get("untagged_lines", 0), 0)
        self.assertEqual(bl.get("contiguity_violations", 0), 0)
        self.assertEqual(bl.get("own_noncontiguous", 0), 0)

    def test_every_snapshot_block_accounted_for(self):
        for page in {b[1] for b in self.fx["blocks"]}:
            want = {b[0] for b in self.fx["blocks"] if b[1] == page}
            got = {r["block_id"] for r in self.rows[page] if r["block_id"]}
            self.assertTrue(want <= got, (page, want - got))

    def _root(self):
        text = readtext(os.path.join(self.out, "vault", self.ex.nodes[ROOT]["note"]))
        lines = text.split("\n")
        by = {}
        for r in self.rows[ROOT]:
            if r["block_id"]:
                by.setdefault(r["block_id"], []).append(r)
        return text, lines, by

    def test_root_layout_details(self):
        text, lines, by = self._root()
        first, last = body_bounds(text)
        blocks = [b for b in self.fx["blocks"] if b[1] == ROOT and b[2] == ROOT]
        lead0, lead1 = blocks[0][0], blocks[1][0]
        for bid in (lead0, lead1):                         # popped / merged leading blank paragraphs
            r = by[bid][0]
            self.assertEqual((r["zero_line"], r["line_start"], r["own_start"]), (1, first - 1, first - 1))
        h1 = by[blocks[2][0]][0]
        self.assertEqual((h1["own_start"], lines[h1["own_start"] - 1]), (first, "# Title A"))
        own_blank, merged_blank = by[blocks[4][0]][0], by[blocks[5][0]][0]
        self.assertEqual(own_blank["zero_line"], 0)
        self.assertEqual(lines[own_blank["own_start"] - 1], "")
        self.assertEqual((merged_blank["zero_line"], merged_blank["line_start"]), (1, own_blank["own_start"]))
        trailing = by[blocks[-1][0]][0]
        self.assertEqual((trailing["zero_line"], trailing["line_start"]), (1, last))
        # numbered item with a nested bullet
        two = by[blocks[7][0]][0]
        nested = [r for r in self.rows[ROOT] if r["block_id"] and r["parent_block_id"] == blocks[7][0]][0]
        self.assertEqual(lines[two["own_start"] - 1], "2. two")
        self.assertEqual((nested["depth"], nested["prefix"], lines[nested["own_start"] - 1]), (1, "    ", "    - nested"))
        self.assertEqual((two["line_start"], two["line_end"]), (two["own_start"], nested["own_end"]))
        # bullet -> to_do switch: gap rows '' and '<!-- -->'
        todo = by[blocks[10][0]][0]
        g = [r for r in self.rows[ROOT] if r["type"] == "gap" and r["line_end"] == todo["own_start"] - 1][0]
        self.assertEqual(lines[g["line_start"] - 1:g["line_end"]], ["", "<!-- -->", ""])
        self.assertEqual(g["x_reason"], "gap")

    def test_containers_tables_columns(self):
        text, lines, by = self._root()
        rows = self.rows[ROOT]
        q = [r for r in rows if r["type"] == "quote"][0]
        qc = [r for r in rows if r["block_id"] and r["parent_block_id"] == q["block_id"]][0]
        self.assertEqual(lines[q["own_start"] - 1], "> quoted")
        self.assertEqual((qc["prefix"], lines[qc["own_start"] - 1]), ("> ", "> qchild"))
        sep = [r for r in rows if r["type"] == "gap" and r["x_reason"] == "sep" and r["parent_block_id"] == q["block_id"]]
        self.assertEqual([lines[r["line_start"] - 1] for r in sep], [">"])
        self.assertEqual((q["line_start"], q["line_end"]), (q["own_start"], qc["own_end"]))
        tbl = [r for r in rows if r["type"] == "table"][0]
        trs = [r for r in rows if r["type"] == "table_row"]
        self.assertEqual(lines[tbl["own_start"] - 1], "| --- | --- |")
        self.assertEqual([lines[r["own_start"] - 1] for r in trs], ["| h1 | h2 |", "| a\\|b |   |"])
        self.assertEqual([r["ord"] for r in trs], [0, 1])
        self.assertTrue(all(r["parent_block_id"] == tbl["block_id"] and r["depth"] == 1 for r in trs))
        self.assertEqual((tbl["line_start"], tbl["line_end"]), (trs[0]["own_start"], trs[1]["own_end"]))
        cl = [r for r in rows if r["type"] == "column_list"][0]
        cols = [r for r in rows if r["type"] == "column"]
        self.assertEqual(len(cols), 2)
        self.assertTrue(all(c["own_start"] is None and c["zero_line"] == 0 and c["depth"] == 1 for c in cols))
        left = [r for r in rows if r["block_id"] and r["parent_block_id"] == cols[0]["block_id"]][0]
        self.assertEqual((left["depth"], lines[left["own_start"] - 1]), (2, "left"))
        csep = [lines[r["line_start"] - 1:r["line_end"]] for r in rows if r["x_reason"] == "sep" and r["parent_block_id"] == cl["block_id"]]
        self.assertEqual(csep, [["", "%% column %%", ""]])
        # paragraph with children: own line, sep, kid, sep, sep
        pp = [r for r in rows if r["type"] == "paragraph" and r["own_start"] and lines[r["own_start"] - 1] == "parent para"][0]
        self.assertEqual(lines[pp["line_start"] - 1:pp["line_end"]], ["parent para", "", "kid para", "", "<!-- -->"])
        self.assertEqual((pp["own_start"], pp["own_end"]), (pp["line_start"], pp["line_start"]))
        tail = [r for r in rows if r["x_reason"] == "sep" and r["parent_block_id"] == pp["block_id"]]
        self.assertEqual([lines[r["line_start"] - 1:r["line_end"]] for r in tail], [[""], ["", "<!-- -->"]])
        multi = [r for r in rows if r["type"] == "paragraph" and r["own_start"] and lines[r["own_start"] - 1] == "multi"][0]
        self.assertEqual(lines[multi["own_start"] - 1:multi["own_end"]], ["multi", "line", "  indented"])

    def test_structural_klass(self):
        rows = self.rows[ROOT]
        kl = {}
        for r in rows:
            if r["block_id"]:
                kl.setdefault(r["type"], set()).add((r["klass"], r["x_reason"]))
        self.assertEqual(kl["heading_1"], {("R?", None)})
        self.assertEqual(kl["table"], {("X", "type:table")})
        self.assertEqual(kl["table_row"], {("R?", None)})
        self.assertEqual(kl["column_list"], {("X", "type:column_list")})
        self.assertEqual(kl["column"], {("X", "type:column")})
        self.assertEqual(kl["toggle"], {("X", "type:toggle")})
        self.assertEqual(kl["image"], {("X", "type:image")})
        self.assertEqual(kl["child_page"], {("X", "type:child_page")})
        self.assertEqual(kl["child_database"], {("X", "type:child_database")})
        self.assertIn(("X", "caption"), kl["code"])
        self.assertIn(("R?", None), kl["code"])
        reasons = {r["x_reason"] for r in rows if r["type"] == "paragraph"}
        self.assertTrue({"mention_page", "color", "notion_link", None} <= reasons)
        # D-11: synced reference and everything rendered from the source are X, owner = source page
        sref = [r for r in rows if r["block_id"] == SREF][0]
        kid = [r for r in rows if r["block_id"] == S1C][0]
        self.assertEqual((sref["klass"], sref["x_reason"]), ("X", "synced"))
        self.assertEqual((kid["klass"], kid["x_reason"], kid["owner_page_id"], kid["parent_block_id"]), ("X", "synced", SRC, S1))
        self.assertEqual(kid["depth"], sref["depth"] + 1)
        src_rows = {r["block_id"]: r for r in self.rows[SRC] if r["block_id"]}
        self.assertEqual((src_rows[S1]["klass"], src_rows[S1C]["klass"]), ("X", "X"))
        self.assertEqual(src_rows[S1C]["owner_page_id"], SRC)
        # json_sha = wb_norm.block_json_sha of the snapshot JSON
        b = next(b for b in self.fx["blocks"] if b[0] == S1C)
        self.assertEqual(kid["json_sha"], wb_norm.block_json_sha(b[6]))
        self.assertTrue(all(r["json_sha"] is None and r["klass"] is None for r in rows if r["type"] == "gap"))


class TestPlanningApi(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.out, cls.state, cls.ex, cls.rep, cls.man = export(cls.fx, cls.tmp, "run1")
        cls.rows = read_rows(cls.out)
        cls.att = os.path.join(cls.out, "attachments.json")

    def test_signatures_stable(self):
        self.assertEqual(list(inspect.signature(exp.Exporter.rt).parameters), ["self", "rich", "ts", "table"])
        self.assertEqual(list(inspect.signature(exp.Exporter.render_block).parameters), ["self", "b", "ts", "prefix"])
        self.assertEqual(list(inspect.signature(exp.Exporter.render_blocks).parameters), ["self", "parent_id", "ts", "prefix"])
        self.assertEqual(list(inspect.signature(exp.Exporter.render_page).parameters)[:5],
                         ["self", "node_or_id", "blocks", "record", "sandbox"])
        self.assertEqual(list(inspect.signature(exp.Exporter.for_planning).parameters)[:4],
                         ["snapshot", "export_db", "overrides", "attachments_json"])

    def _hash(self, p):
        with open(p, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    def test_for_planning_read_only_and_reproduces_files(self):
        before = {p: (self._hash(p), os.path.getmtime(p)) for p in (self.state, self.fx["snap"])}
        listing = sorted(os.listdir(self.tmp))
        ex = exp.Exporter.for_planning(self.fx["snap"], self.state, attachments_json=self.att)
        for oid in ex.run_order():
            n = ex.nodes[oid]
            text, rows = ex.render_page(oid, record=True, sandbox=True)
            self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), self.man[n["note"]]["sha256"], n["note"])
            if n["kind"] != "database":
                self.assertEqual(rows or [], self.rows.get(oid, []))
            else:
                self.assertIsNone(rows)
        self.assertEqual(before, {p: (self._hash(p), os.path.getmtime(p)) for p in before})
        self.assertEqual(listing, sorted(os.listdir(self.tmp)))
        self.assertFalse([f for f in os.listdir(os.path.dirname(self.fx["snap"])) if f.endswith(("-wal", "-shm", "-journal"))])
        with self.assertRaises(RuntimeError):
            ex.run()
        # without attachments.json the map is rebuilt by an in-memory pass
        ex2 = exp.Exporter.for_planning(self.fx["snap"], self.state)
        self.assertEqual(ex2.att_map, jload(self.att))
        li = ex.link_index()
        self.assertEqual(li[DB], {"note": ex.nodes[DB]["note"], "title": "Tasks", "base": ex.nodes[DB]["base"]})
        self.assertEqual(li[LEAF]["note"], "General/Root Page/Leaf／One.md")
        self.assertIsNone(li[LEAF]["base"])

    def test_for_planning_refuses_bad_inputs(self):
        with self.assertRaises(FileNotFoundError):
            exp.Exporter.for_planning(self.fx["snap"], os.path.join(self.tmp, "missing.db"))
        empty = os.path.join(self.tmp, "empty_state.db")
        sqlite3.connect(empty).close()
        with self.assertRaises(ValueError):
            exp.Exporter.for_planning(self.fx["snap"], empty)

    def test_sandbox_no_side_effects_and_refusal(self):
        ex = exp.Exporter.for_planning(self.fx["snap"], self.state, attachments_json=self.att)
        snap_state = lambda: (json.dumps({k: dict(v) for k, v in ex.report.items()}, sort_keys=True),  # noqa: E731
                              json.dumps(ex.lists, sort_keys=True, default=str), json.dumps(ex.att_map, sort_keys=True),
                              dict(ex.att_by_sha), dict(ex.attachments))
        s0 = snap_state()
        ex.render_page(ROOT, record=True, sandbox=True)
        self.assertEqual(s0, snap_state())
        top = copy.deepcopy(ex.blocks[ROOT])
        newimg = copy.deepcopy(next(b for b in top if b["id"] == IMG))
        newimg["id"] = uid(4242)
        top.append(newimg)
        with self.assertRaises(exp.RenderRefused):
            ex.render_page(ROOT, blocks={ROOT: top}, sandbox=True)
        self.assertEqual(s0, snap_state())
        self.assertIs(ex._rec, None)
        self.assertFalse(ex._sandbox)
        self.assertNotIsInstance(ex.blocks, exp.ChainMap)

    def test_overlay_edit(self):
        ex = exp.Exporter.for_planning(self.fx["snap"], self.state, attachments_json=self.att)
        before, rows_b = ex.render_page(ROOT, record=True, sandbox=True)
        top = copy.deepcopy(ex.blocks[ROOT])
        hello = next(b for b in top if b["type"] == "paragraph" and exp.plain(b["paragraph"]["rich_text"]) == "Hello world")
        hello["paragraph"]["rich_text"] = [txt("EDITED [x]", bold=True)]
        i = [b["type"] for b in top].index("numbered_list_item")
        new = copy.deepcopy(top[i])
        new["id"] = uid(4243)
        new["numbered_list_item"]["rich_text"] = [txt("inserted")]
        top.insert(i + 1, new)
        after, rows_a = ex.render_page(ROOT, blocks={ROOT: top}, record=True, sandbox=True)
        al = after.split("\n")
        self.assertIn("<strong>EDITED \\[x\\]</strong>", al)
        k = al.index("1. one")
        self.assertEqual(al[k:k + 4], ["1. one", "2. inserted", "3. two", "    - nested"])
        self.assertIn("4. three", al)
        self.assertEqual(partition_problems(after, rows_a), ([], [], []))
        ra = {r["block_id"]: r for r in rows_a if r["block_id"]}
        rb = {r["block_id"]: r for r in rows_b if r["block_id"]}
        self.assertEqual(al[ra[uid(4243)]["own_start"] - 1], "2. inserted")
        self.assertEqual(ra[uid(4243)]["owner_page_id"], ROOT)
        self.assertEqual(al[ra[hello["id"]]["own_start"] - 1], "<strong>EDITED \\[x\\]</strong>")
        self.assertNotEqual(ra[hello["id"]]["json_sha"], rb[hello["id"]]["json_sha"])
        self.assertTrue(all(ra[b]["json_sha"] == rb[b]["json_sha"] for b in rb if b != hello["id"]))
        self.assertEqual(ra[hello["id"]]["base_sha"], hashlib.sha256(after.encode()).hexdigest())
        self.assertEqual(ex.render_page(ROOT, sandbox=True)[0], before)

    def _planner(self):
        return exp.Exporter.for_planning(self.fx["snap"], self.state, attachments_json=self.att)

    def test_overlay_idless_blocks_recorded(self):
        """Blocks to APPEND have no id yet: record=True must place them (block_id null), not crash."""
        ex = self._planner()
        top = copy.deepcopy(ex.blocks[ROOT])
        h = [b["type"] for b in top].index("heading_1")
        top[h + 1:h + 1] = [blk(None, "paragraph", [txt("brand new")]), blk(None, "paragraph", [txt("second new")])]
        q = next(b for b in top if b["type"] == "quote")
        qkids = copy.deepcopy(ex.blocks[q["id"]]) + [blk(None, "bulleted_list_item", [txt("new in quote")])]
        overlay = {ROOT: top, q["id"]: qkids}
        plain_text, _ = ex.render_page(ROOT, blocks=overlay, sandbox=True)
        text, rows = ex.render_page(ROOT, blocks=overlay, record=True, sandbox=True)
        self.assertEqual(text, plain_text)
        self.assertEqual(partition_problems(text, rows), ([], [], []))
        lines = text.split("\n")
        new = [r for r in rows if r["type"] != "gap" and r["block_id"] is None]
        self.assertEqual([lines[r["own_start"] - 1] for r in new], ["brand new", "second new", "> - new in quote"])
        self.assertEqual([(r["ord"], r["parent_block_id"], r["depth"], r["prefix"]) for r in new],
                         [(h + 1, None, 0, ""), (h + 2, None, 0, ""), (len(qkids) - 1, q["id"], 1, "> ")])
        self.assertTrue(all(r["owner_page_id"] == ROOT and r["klass"] == "R?" and r["zero_line"] == 0 for r in new))
        self.assertEqual(new[0]["json_sha"], wb_norm.block_json_sha(top[h + 1]))
        quote_row = next(r for r in rows if r["block_id"] == q["id"])
        self.assertTrue(quote_row["line_start"] <= new[2]["own_start"] <= quote_row["line_end"])
        got = {r["block_id"] for r in rows if r["block_id"]}
        self.assertTrue({b[0] for b in self.fx["blocks"] if b[1] == ROOT} <= got)
        # blocks the renderer cannot place without an id are refused (not a KeyError), state restored
        for bad in (dict(blk(None, "paragraph", [txt("p")]), has_children=True),
                    {"object": "block", "type": "image", "has_children": False, "image": {"type": "external",
                                                                                         "external": {"url": "https://e.com/a.png"}}},
                    "not a block"):
            with self.assertRaises(exp.RenderRefused):
                ex.render_page(LEAF, blocks={LEAF: [bad]}, record=True, sandbox=True)
        self.assertIs(ex._rec, None)
        self.assertNotIsInstance(ex.blocks, exp.ChainMap)

    def test_lost_own_line_is_x(self):
        """A block that lost one of its own lines (leading blank merged into the previous blank line, or trimmed
        at the note's start/end) is X own_line_lost; every remaining R? block re-renders alone to its own lines."""
        ex = self._planner()
        ov = [blk(uid(700), "paragraph", [txt("first")]),
              blk(uid(701), "paragraph", []),
              blk(uid(702), "paragraph", [txt("\nafter blank")]),        # leading "" merged into the blank above
              blk(uid(705), "paragraph", []),
              blk(uid(704), "quote", [txt("\nquoted nl")]),              # leading ">" merged into the blank above
              blk(uid(706), "paragraph", [txt("mid\n")]),                # own trailing "" survives (next is text)
              blk(uid(707), "paragraph", [txt("x")]),
              blk(uid(703), "paragraph", [txt("tail\n")])]               # trailing "" trimmed at the note's end
        text, rows = ex.render_page(LEAF, blocks={LEAF: ov}, record=True, sandbox=True)
        lines = text.split("\n")
        first, last = body_bounds(text)
        self.assertEqual(lines[first - 1:last], ["first", "", "after blank", "", "> quoted nl", "", "mid", "", "x",
                                                 "", "tail"])
        by = {r["block_id"]: r for r in rows if r["block_id"]}
        self.assertEqual({b: (by[b]["klass"], by[b]["x_reason"]) for b in by},
                         {uid(700): ("R?", None), uid(701): ("R?", None), uid(702): ("X", "own_line_lost"),
                          uid(705): ("R?", None), uid(704): ("X", "own_line_lost"), uid(706): ("R?", None),
                          uid(707): ("R?", None), uid(703): ("X", "own_line_lost")})
        self.assertEqual(lines[by[uid(702)]["own_start"] - 1:by[uid(702)]["own_end"]], ["after blank"])
        self.assertEqual(lines[by[uid(706)]["own_start"] - 1:by[uid(706)]["own_end"]], ["mid", ""])
        self.assertEqual(partition_problems(text, rows), ([], [], []))
        self.assertEqual(r042_check(ex, text, rows, {b["id"]: b for b in ov}), ([], []))
        # leading line trimmed at the start of the body
        ov2 = [blk(uid(710), "paragraph", [txt("\nstarts nl")]), blk(uid(711), "paragraph", [txt("y")])]
        text2, rows2 = ex.render_page(LEAF, blocks={LEAF: ov2}, record=True, sandbox=True)
        r = next(r for r in rows2 if r["block_id"] == uid(710))
        self.assertEqual((r["klass"], r["x_reason"], r["zero_line"]), ("X", "own_line_lost", 0))
        self.assertEqual(r042_check(ex, text2, rows2, {b["id"]: b for b in ov2}), ([], []))
        # a structural reason keeps priority over the layout rule
        ov3 = [blk(uid(720), "paragraph", []), blk(uid(721), "paragraph", [txt("\nred", color="red")])]
        _, rows3 = ex.render_page(LEAF, blocks={LEAF: ov3}, record=True, sandbox=True)
        r = next(r for r in rows3 if r["block_id"] == uid(721))
        self.assertEqual((r["klass"], r["x_reason"]), ("X", "color"))

    def test_r042_fixture_rows(self):
        """R-042 over every note of the fixture export: each R? block re-renders alone to its recorded lines."""
        ex = self._planner()
        by_id = {b["id"]: b for lst in ex.blocks.values() for b in lst}
        checked = 0
        for nid, rows in self.rows.items():
            text = readtext(os.path.join(self.out, "vault", ex.nodes[nid]["note"]))
            self.assertEqual(r042_check(ex, text, rows, by_id), ([], []), nid)
            checked += sum(1 for r in rows if r["klass"] == "R?" and not r["zero_line"])
        self.assertGreater(checked, 20)

    def test_anchor_links_are_x(self):
        ex = self._planner()
        kl = lambda b: ex.structural_klass(b, LEAF)  # noqa: E731
        link = lambda url: blk(uid(800), "paragraph", [txt("go "), txt("there", link=url)])  # noqa: E731
        self.assertEqual(kl(link("#Title A")), ("X", "anchor_link"))
        self.assertEqual(kl(link(" #x")), ("X", "anchor_link"))
        bullet = blk(uid(801), "bulleted_list_item", [txt("部署", link="#部署结果速览")])
        self.assertEqual(kl(bullet), ("X", "anchor_link"))
        notion = "https://www.notion.so/Second-" + LEAF2.replace("-", "") + "#" + uid(5).replace("-", "")
        self.assertEqual(kl(link(notion)), ("X", "notion_link"))
        self.assertEqual(kl(link("https://example.com/a#frag")), ("R?", None))
        self.assertEqual(kl(blk(uid(802), "table_row", None) | {"table_row": {"cells": [[txt("c", link="#h")]]}}),
                         ("X", "anchor_link"))
        text, rows = ex.render_page(LEAF, blocks={LEAF: [bullet, link("https://example.com/a#frag")]}, record=True,
                                    sandbox=True)
        by = {r["block_id"]: r for r in rows if r["block_id"]}
        self.assertEqual((by[uid(801)]["klass"], by[uid(801)]["x_reason"]), ("X", "anchor_link"))
        self.assertEqual(by[uid(800)]["klass"], "R?")
        self.assertIn("- [部署](#部署结果速览)", text.split("\n"))

    def test_empty_body_anchor_exists(self):
        """A note whose body is empty after layout: zero-line anchors name the closing frontmatter fence."""
        ex = self._planner()
        text, rows = ex.render_page(LEAF, blocks={LEAF: [blk(uid(900), "paragraph", []), blk(uid(901), "paragraph", [])]},
                                    record=True, sandbox=True)
        lines = text.split("\n")[:-1]                      # text ends with "\n"
        fence = lines.index("---", 1) + 1                  # 1-based closing fence = last line of the file
        self.assertEqual(len(lines), fence)
        self.assertEqual([(r["zero_line"], r["line_start"], r["line_end"], r["own_start"], r["own_end"]) for r in rows],
                         [(1, fence, fence, fence, fence)] * 2)
        self.assertEqual(lines[rows[0]["line_start"] - 1], "---")
        # with a body, a popped leading blank still anchors on the blank line before the body
        text2, rows2 = ex.render_page(LEAF, blocks={LEAF: [blk(uid(902), "paragraph", []),
                                                           blk(uid(903), "paragraph", [txt("body")])]},
                                      record=True, sandbox=True)
        first, _ = body_bounds(text2)
        r = next(r for r in rows2 if r["block_id"] == uid(902))
        self.assertEqual((r["zero_line"], r["line_start"]), (1, first - 1))
        self.assertEqual(text2.split("\n")[first - 2], "")

    def test_render_page_accepts_id_forms(self):
        ex = exp.Exporter.for_planning(self.fx["snap"], self.state, attachments_json=self.att)
        a = ex.render_page(LEAF, sandbox=True)[0]
        self.assertEqual(a, ex.render_page(LEAF.replace("-", ""), sandbox=True)[0])
        self.assertEqual(a, ex.render_page(ex.nodes[LEAF], sandbox=True)[0])


class TestOverrides(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.out0, cls.state0, cls.ex0, _, cls.man0 = export(cls.fx, cls.tmp, "base")
        cls.ovr = {LEAF: "General/Root Page/Leaf renamed.md",
                   CROSS: "General/elsewhere.md",                       # different folder
                   ROOT: "General/Root Renamed.md",                     # folder note
                   LEAF2: "General/Root Page/Src Page.md",              # clashes with a sibling
                   ROW: "General/Root Page/Tasks/bad|name.md",          # unsafe
                   "zzz": "General/x.md",                               # bad id
                   uid(31337): "General/Root Page/ghost.md"}            # not in tree
        p = os.path.join(cls.tmp, "overrides.json")
        jdump(cls.ovr, p)
        # as in production, overrides exist only for published objects: the state already holds the stable names
        st = os.path.join(cls.tmp, "ovr.state.db")
        shutil.copy2(cls.state0, st)
        cls.out, cls.state, cls.ex, cls.rep, cls.man = export(cls.fx, cls.tmp, "ovr", state=st, overrides=jload(p))

    def test_file_lands_at_local_rel_same_bytes(self):
        old = "General/Root Page/Leaf／One.md"
        new = "General/Root Page/Leaf renamed.md"
        self.assertIn(old, self.man0)
        self.assertNotIn(old, self.man)
        self.assertEqual(self.man[new]["sha256"], self.man0[old]["sha256"])   # aliases unchanged
        text = readtext(os.path.join(self.out, "vault", new))
        self.assertIn("aliases:\n- Leaf/One\n", text)

    def test_links_index_mapping_sortspec(self):
        root_rel = self.ex.nodes[ROOT]["note"]
        t = readtext(os.path.join(self.out, "vault", root_rel))
        self.assertIn("[[General/Root Page/Leaf renamed|Leaf/One]]", t)
        self.assertNotIn("Leaf／One]]", t)
        self.assertNotIn("[[General/Root Page/Leaf／One|", t)
        gi = readtext(os.path.join(self.out, "vault", "Notion_Obsidian", "Godcreate 目录索引.md"))
        gm = readtext(os.path.join(self.out, "vault", "Notion_Obsidian", "映射清单.md"))
        ss = readtext(os.path.join(self.out, "vault", "Notion_Obsidian", "sortspec.md"))
        self.assertIn("[[General/Root Page/Leaf renamed|Leaf/One]]", gi)
        self.assertIn("`General/Root Page/Leaf renamed.md`", gm)
        self.assertNotIn("Leaf／One.md`", gm)
        self.assertIn("  /:files. Leaf renamed.md\n", ss)
        self.assertNotIn("/:files. Leaf／One.md", ss)
        self.assertIn("  /:files. Second.md\n", ss)                         # clash -> normal name kept
        changed = sorted(k for k in set(self.man) & set(self.man0) if self.man[k]["sha256"] != self.man0[k]["sha256"])
        self.assertEqual(changed, sorted([root_rel, "Notion_Obsidian/Godcreate 目录索引.md", "Notion_Obsidian/映射清单.md",
                                          "Notion_Obsidian/sortspec.md"]))

    def test_link_targets_redirect_generated_links_without_moving_the_exported_note(self):
        kept = "General/Other/Leaf kept.md"
        out, _state, ex, _rep, _man = export(self.fx, self.tmp, "link_target", link_targets={LEAF: kept})
        root_rel = ex.nodes[ROOT]["note"]
        root = readtext(os.path.join(out, "vault", root_rel))
        self.assertIn("[[General/Other/Leaf kept|Leaf/One]]", root)
        self.assertTrue(os.path.exists(os.path.join(out, "vault", ex.nodes[LEAF]["note"])))
        self.assertIn("[[General/Other/Leaf kept|Leaf/One]]",
                      readtext(os.path.join(out, "vault", "Notion_Obsidian", "Godcreate 目录索引.md")))
        self.assertIn("`General/Other/Leaf kept.md`",
                      readtext(os.path.join(out, "vault", "Notion_Obsidian", "映射清单.md")))
        self.assertIn("target-folder: General/Other",
                      readtext(os.path.join(out, "vault", "Notion_Obsidian", "sortspec.md")))

    def test_rejections_reported(self):
        o = self.rep["overrides"]
        self.assertEqual(o["given"], 7)
        self.assertEqual(o["applied"], [{"notion_id": LEAF, "local_rel": "General/Root Page/Leaf renamed.md",
                                         "computed_rel": "General/Root Page/Leaf／One.md"}])
        rej = {r["notion_id"]: r["reason"] for r in o["rejected"]}
        self.assertEqual(rej, {CROSS: "different_folder", ROOT: "folder_note", LEAF2: "name_clash", ROW: "unsafe_name",
                               "zzz": "bad_id", uid(31337): "not_in_tree"})
        self.assertEqual(self.ex.nodes[CROSS]["note"], self.ex0.nodes[CROSS]["note"])
        self.assertEqual(self.ex.nodes[LEAF2]["note"], self.ex0.nodes[LEAF2]["note"])

    def test_objects_map_and_side_outputs(self):
        db = sqlite3.connect(self.state)
        rel, name = db.execute("SELECT rel_path, name FROM objects_map WHERE notion_id=?", (LEAF,)).fetchone()
        db.close()
        self.assertEqual((rel, name), ("General/Root Page/Leaf renamed.md", "Leaf／One"))   # D-22
        pj = {json.loads(x)["notion_id"]: json.loads(x) for x in readtext(os.path.join(self.out, "page_json.jsonl")).splitlines()}
        self.assertEqual(pj[LEAF]["rel_path"], "General/Root Page/Leaf renamed.md")
        # a second run with the same overrides is stable (objects_map.name stays the natural name)
        out2, _, ex2, _, man2 = export(self.fx, self.tmp, "ovr2", state=self.state, overrides=self.ovr)
        self.assertEqual(man2, self.man)
        # and without overrides the note returns to the title-derived name
        out3, _, _, _, man3 = export(self.fx, self.tmp, "ovr3", state=self.state)
        self.assertEqual(man3, self.man0)

    def test_new_sibling_yields_to_established_override(self):
        """R-046 / WB §7.4.3: the override name takes part in same-folder dedupe.  A NEW Notion page whose title
        equals the user's local name gets the dedupe suffix; the override stays applied (no revert, no path
        collision with the user's renamed file)."""
        newp = uid(600)
        snap2 = variant_snapshot(self.fx, self.tmp, "snap_newsib.db", ROOT, newp, "Leaf renamed")
        fx2 = dict(self.fx, snap=snap2)
        st = os.path.join(self.tmp, "newsib.state.db")
        shutil.copy2(self.state, st)                      # state after the override run (override established)
        out, _, ex, rep, man = export(fx2, self.tmp, "newsib", state=st, overrides=self.ovr)
        mine = "General/Root Page/Leaf renamed.md"
        suffixed = f"General/Root Page/Leaf renamed ({newp.replace('-', '')[:8]}).md"
        self.assertEqual(ex.nodes[LEAF]["note"], mine)
        self.assertEqual(ex.nodes[newp]["note"], suffixed)
        self.assertEqual((man[mine]["notion_id"], man[suffixed]["notion_id"]), (LEAF, newp))
        self.assertEqual(man[mine]["sha256"], self.man[mine]["sha256"])
        self.assertIn({"notion_id": LEAF, "local_rel": mine, "computed_rel": "General/Root Page/Leaf／One.md"},
                      rep["overrides"]["applied"])
        self.assertNotIn(LEAF, {r["notion_id"] for r in rep["overrides"]["rejected"]})
        self.assertEqual(rep["names"]["dedup_suffix"], 1)
        # every other object keeps its path
        self.assertEqual({o: n["note"] for o, n in ex.nodes.items() if o != newp},
                         {o: n["note"] for o, n in self.ex.nodes.items()})
        # the suffixed name is stable on the next run (pass 1), and planning agrees
        _, _, _, _, man2 = export(fx2, self.tmp, "newsib2", state=st, overrides=self.ovr)
        self.assertEqual(man2, man)
        exm = exp.Exporter.for_planning(snap2, st, overrides=self.ovr)
        self.assertEqual((exm.nodes[LEAF]["note"], exm.nodes[newp]["note"]), (mine, suffixed))
        # control: without the override the new page simply takes the plain name
        st0 = os.path.join(self.tmp, "newsib0.state.db")
        shutil.copy2(self.state0, st0)
        _, _, ex0, _, _ = export(fx2, self.tmp, "newsib0", state=st0)
        self.assertEqual(ex0.nodes[newp]["note"], mine)
        # a sibling's stable (pass-1) name still wins over a NEW override (that override is void and reported)
        _, _, ex3, rep3, _ = export(fx2, self.tmp, "newsib3", state=os.path.join(self.tmp, "newsib0.state.db"),
                                    overrides={LEAF: mine})
        self.assertEqual(ex3.nodes[LEAF]["note"], "General/Root Page/Leaf／One.md")
        self.assertEqual(rep3["overrides"]["rejected"], [{"notion_id": LEAF, "local_rel": mine, "reason": "name_clash"}])

    def test_planning_with_overrides(self):
        ex = exp.Exporter.for_planning(self.fx["snap"], self.state, overrides=self.ovr,
                                       attachments_json=os.path.join(self.out, "attachments.json"))
        self.assertEqual(ex.link_index()[LEAF]["note"], "General/Root Page/Leaf renamed.md")
        self.assertIn("  /:files. Leaf renamed.md\n", build_sortspec(ex))
        for oid in ex.run_order():
            n = ex.nodes[oid]
            self.assertEqual(hashlib.sha256(ex.render_page(oid, sandbox=True)[0].encode()).hexdigest(),
                             self.man[n["note"]]["sha256"])


class TestStateRules(_Base):
    def test_trashed_via_writeback_not_reactivated(self):
        out, state, ex, rep, man = export(self.fx, self.tmp, "t1")
        db = sqlite3.connect(state)
        db.execute("UPDATE objects_map SET status='trashed_via_writeback' WHERE notion_id=?", (LEAF2,))
        db.commit()
        db.close()
        out2, _, ex2, rep2, _ = export(self.fx, self.tmp, "t2", state=state)
        db = sqlite3.connect(state)
        st = dict(db.execute("SELECT notion_id, status FROM objects_map").fetchall())
        db.close()
        self.assertEqual(st[LEAF2], "trashed_via_writeback")
        self.assertEqual(st[LEAF], "active")
        self.assertEqual(rep2["lists"]["trashed_via_writeback_reappeared"], [LEAF2])


class TestCli(_Base):
    def _run(self, name, *args):
        cmd = [sys.executable, "-I", "-B", os.path.join(TOOLS, "export_obsidian.py"), "--snapshot", self.fx["snap"],
               "--sidebar", self.fx["sidebar"], "--immutable", "--run-id", name, "--state", os.path.join(self.tmp, name + ".db"),
               "--out", os.path.join(self.tmp, name), *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=300), os.path.join(self.tmp, name)

    def test_cli_overrides_and_no_block_lines(self):
        ovr = os.path.join(self.tmp, "cli_ovr.json")
        jdump({LEAF: "General/Root Page/Leaf cli.md", CROSS: "General/x.md"}, ovr)
        p, out = self._run("cli1", "--overrides", ovr, "--no-block-lines")
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        summary = json.loads(p.stdout)
        self.assertEqual(summary["renderer_version"], "1.2.1")
        self.assertEqual(summary["overrides"], {"applied": 1, "rejected": 1})
        self.assertFalse(os.path.exists(os.path.join(out, "block_lines.jsonl")))
        self.assertIn("General/Root Page/Leaf cli.md", jload(os.path.join(out, "manifest.json")))
        p, out = self._run("cli2")
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertTrue(os.path.exists(os.path.join(out, "block_lines.jsonl")))

    def test_cli_rejects_malformed_overrides(self):
        bad = os.path.join(self.tmp, "bad_ovr.json")
        jdump([LEAF, "x.md"], bad)
        p, out = self._run("cli3", "--overrides", bad)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("must be a JSON object", p.stderr)
        self.assertFalse(os.path.exists(os.path.join(out, "vault")))


class TestWbNorm(unittest.TestCase):
    def test_json_norm_rules(self):
        blk = {"object": "block", "id": "b", "created_time": "t", "last_edited_time": "t", "created_by": {"id": "u"},
               "last_edited_by": {"id": "u"}, "request_id": "r", "_x": 1, "has_children": False, "type": "image",
               "image": {"type": "file", "file": {"url": "https://s3/x?sig=1", "expiry_time": "e"}, "caption": []}}
        self.assertEqual(wb_norm.json_norm(blk), {"object": "block", "id": "b", "has_children": False, "type": "image",
                                                  "image": {"type": "file", "file": {}, "caption": []}})
        seg = {"type": "mention", "mention": {"type": "page", "page": {"id": "p"}}, "plain_text": "T", "href": "h",
               "annotations": {"bold": False}}
        self.assertEqual(wb_norm.json_norm(seg), {"type": "mention", "mention": {"type": "page", "page": {"id": "p"}},
                                                  "annotations": {"bold": False}})
        t = txt("a", link="https://e.com")
        self.assertEqual(wb_norm.json_norm(t), t)                         # text segments keep plain_text/href
        ext = {"type": "external", "external": {"url": "https://e.com/a.png"}}
        self.assertEqual(wb_norm.json_norm(ext), ext)

    def test_properties(self):
        page = {"properties": {"Rel": {"type": "relation", "relation": [{"id": "a"}], "has_more": False},
                               "created_by_me": {"type": "rich_text", "rich_text": [txt("x")]},
                               "_draft": {"type": "checkbox", "checkbox": True},
                               "Created": {"type": "created_time", "created_time": "t"},
                               "Files": {"type": "files", "files": [{"name": "f", "type": "file",
                                                                     "file": {"url": "u", "expiry_time": "e"}}]}},
                "icon": {"type": "file", "file": {"url": "u2", "expiry_time": "e2"}}}
        n = wb_norm.json_norm(page)
        self.assertEqual(set(n["properties"]), {"Rel", "created_by_me", "_draft", "Created", "Files"})
        self.assertNotIn("has_more", n["properties"]["Rel"])
        self.assertEqual(n["properties"]["Created"], {"type": "created_time"})
        self.assertEqual(n["properties"]["Files"]["files"][0]["file"], {})
        self.assertEqual(n["icon"], {"type": "file", "file": {}})

    def test_snapshot_vs_live_shape_same_sha(self):
        live = page_obj(ROOT, "T", {"type": "workspace", "workspace": True})
        live["properties"]["Rel"] = {"id": "r", "type": "relation", "relation": [{"id": "a"}, {"id": "b"}], "has_more": True}
        live["icon"] = {"type": "file", "file": {"url": "https://s3/a?sig=NEW", "expiry_time": "2026-11-18T00:00:00Z"}}
        live["request_id"] = "live-req"
        snap = copy.deepcopy(live)
        snap["properties"]["Rel"]["has_more"] = False
        snap["properties"]["Rel"]["_completed_via_property_item"] = 2
        snap["icon"]["file"] = {"url": "https://s3/a?sig=OLD", "expiry_time": "2026-11-17T00:00:00Z"}
        snap["request_id"] = "snap-req"
        snap["last_edited_time"] = "2026-11-17T01:00:00.000Z"
        b_live = [{"id": "x", "type": "paragraph", "parent": {"type": "page_id", "page_id": ROOT}, "has_children": False,
                   "paragraph": {"rich_text": [mention_page(LEAF, "new title")]}, "last_edited_time": "a"}]
        b_snap = copy.deepcopy(b_live)
        b_snap[0]["paragraph"]["rich_text"][0]["plain_text"] = "old title"
        b_snap[0]["last_edited_time"] = "b"
        self.assertEqual(wb_norm.page_json_sha(live, b_live), wb_norm.page_json_sha(snap, b_snap))
        self.assertEqual(wb_norm.block_json_sha(b_live[0]), wb_norm.block_json_sha(b_snap[0]))
        b_snap[0]["paragraph"]["rich_text"][0]["annotations"]["bold"] = True
        self.assertNotEqual(wb_norm.page_json_sha(live, b_live), wb_norm.page_json_sha(snap, b_snap))
        snap2 = copy.deepcopy(live)
        snap2["properties"]["title"]["title"][0]["text"]["content"] = "changed"
        self.assertNotEqual(wb_norm.page_json_sha(live, b_live), wb_norm.page_json_sha(snap2, b_live))

    def test_synced_children_excluded_and_dfs(self):
        P = "p"
        blocks = {P: [{"id": "s", "type": "synced_block", "has_children": True, "parent": {"type": "page_id", "page_id": P}},
                      {"id": "t", "type": "toggle", "has_children": True, "parent": {"type": "page_id", "page_id": P}},
                      {"id": "c", "type": "child_page", "has_children": True, "parent": {"type": "page_id", "page_id": P}}],
                  "s": [{"id": "s1", "type": "paragraph", "has_children": False, "parent": {"type": "block_id", "block_id": "s"}}],
                  "t": [{"id": "t1", "type": "paragraph", "has_children": False, "parent": {"type": "block_id", "block_id": "t"}}],
                  "c": [{"id": "c1", "type": "paragraph", "has_children": False, "parent": {"type": "page_id", "page_id": "c"}}]}
        dfs = wb_norm.page_blocks_dfs(blocks.get, P)
        self.assertEqual([b["id"] for b in dfs], ["s", "t", "t1", "c"])
        page = {"properties": {}}
        with_synced_kid = dfs[:1] + blocks["s"] + dfs[1:]
        self.assertEqual(wb_norm.page_json_sha(page, dfs), wb_norm.page_json_sha(page, with_synced_kid))
        swapped = [dfs[1], dfs[2], dfs[0], dfs[3]]
        self.assertNotEqual(wb_norm.page_json_sha(page, dfs), wb_norm.page_json_sha(page, swapped))

    def test_page_json_sha_equals_reference_definition(self):
        page = page_obj(ROOT, "Tïtle ✓", {"type": "workspace", "workspace": True}, icon={"type": "emoji", "emoji": "📄"})
        page["is_locked"] = False
        blocks = [{"id": "a", "type": "paragraph", "parent": {"type": "page_id", "page_id": ROOT}, "has_children": True,
                   "paragraph": {"rich_text": [txt("é \"q\" \\ \n"), mention_page(LEAF, "x")], "color": "default"}},
                  {"id": "b", "type": "equation", "parent": {"type": "block_id", "block_id": "a"}, "has_children": False,
                   "equation": {"expression": "1.5e3 < x"}, "n": 1.25}]
        ref = hashlib.sha256(wb_norm.canon_json({"page": wb_norm.json_norm({k: page.get(k) for k in wb_norm.PAGE_KEYS}),
                                                 "blocks": [wb_norm.json_norm(b) for b in blocks]}).encode("utf-8")).hexdigest()
        self.assertEqual(wb_norm.page_json_sha(page, blocks), ref)
        self.assertEqual(wb_norm.page_json_sha_canon(page, [wb_norm.canon_block(b) for b in blocks]), ref)
        empty = hashlib.sha256(wb_norm.canon_json({"page": wb_norm.json_norm({k: None for k in wb_norm.PAGE_KEYS}),
                                                   "blocks": []}).encode("utf-8")).hexdigest()
        self.assertEqual(wb_norm.page_json_sha(None, []), empty)

    def test_canonical(self):
        self.assertEqual(wb_norm.canon_json({"b": 1, "a": "é"}), '{"a":"é","b":1}')


# the sections sortspec.TASK_SECTIONS appends (WB 5.1a / R-122), exactly as written into sortspec.md
TASK_SECTIONS_TEXT = ("\n\n  target-folder: Notion_Obsidian\n  /:files. README.md\n  /:files. Godcreate 目录索引.md\n"
                      "  /:files. 映射清单.md\n  /:files. sortspec.md\n  /folders 运行记录\n  /folders 写回预览\n"
                      "  /folders conflicts\n  %\n\n  target-folder: Notion_Obsidian/运行记录\n  > a-z\n  %\n"
                      "\n  target-folder: Notion_Obsidian/写回预览\n  > a-z\n  %")


class TestNextRendererGeneratedFiles(_Base):
    """R-048 (README: the deployed schedule, write-back, the one operations document) and R-122 / WB 5.1a (sortspec
    sections for Notion_Obsidian, 写回预览, 运行记录, newest first). Both change generated bytes, so they are tied to
    the next RENDERER_VERSION: 1.2.1 keeps the production bytes (GOLDEN), any later version writes the new content
    (r1-spec-coverage-06 / -10; fails on the old exporter, which ignores the version)."""

    NEXT = "1.3.0"

    def export_as(self, version, name):
        saved = exp.RENDERER_VERSION
        exp.RENDERER_VERSION = version
        try:
            return export(self.fx, self.tmp, name)
        finally:
            exp.RENDERER_VERSION = saved

    def test_frozen_version_keeps_the_production_bytes(self):
        self.assertEqual(exp.RENDERER_VERSION, "1.2.1")
        self.assertTrue(exp.renderer_bytes_frozen())
        self.assertFalse(exp.renderer_bytes_frozen(self.NEXT))
        self.assertIs(exp.readme_lines(), exp.README_LINES)
        _out, _s, ex, _r, man = self.export_as("1.2.1", "frozen")
        self.assertEqual({k: v["sha256"] for k, v in man.items()}, GOLDEN)
        self.assertEqual(build_sortspec(ex), build_sortspec(ex, task_sections=False))
        self.assertNotIn("target-folder: Notion_Obsidian", build_sortspec(ex))

    def test_next_version_writes_the_new_readme_and_sortspec_sections(self):
        out, _s, ex, rep, man = self.export_as(self.NEXT, "next")
        self.assertEqual(rep["renderer_version"], self.NEXT)
        got = {k: v["sha256"] for k, v in man.items()}
        self.assertEqual(set(got), set(GOLDEN))
        self.assertEqual(sorted(k for k in GOLDEN if got[k] != GOLDEN[k]),
                         ["Notion_Obsidian/README.md", "Notion_Obsidian/sortspec.md"])  # nothing else changes
        readme = readtext(os.path.join(out, "vault", "Notion_Obsidian", "README.md"))
        self.assertEqual(exp.OPS_DOC, "/data120/Obsidian/notion_obsidian+obsidian_notion 同步相通执行文档.md")
        for s in ("每 6 小时", "00:30、06:30、12:30、18:30", "05:30", exp.OPS_DOC, "Notion_Obsidian/写回预览/",
                  "Notion_Obsidian/运行记录/", "人工确认", "Notion_Obsidian/conflicts/<运行编号>/", "notion_kind: generated"):
            self.assertIn(s, readme)
        for s in ("04:30", "03:30", "每日", "单向导出", "执行方案文档"):
            self.assertNotIn(s, readme)
        self.assertTrue(readme.endswith("请勿手改。\n"))
        spec = readtext(os.path.join(out, "vault", "Notion_Obsidian", "sortspec.md"))
        frozen = build_sortspec(ex)
        cut = frozen.rindex("\n---\n")
        self.assertEqual(spec, frozen[:cut] + TASK_SECTIONS_TEXT + frozen[cut:])
        self.assertEqual(spec, build_sortspec(ex, task_sections=True))

    def test_next_version_output_passes_the_verifier(self):
        """verify_vault accepts the new sections (C05: syntax, no duplicate target-folder) with the generated
        records they order present, and the README; every check PASSes."""
        import verify_vault as vv
        out, *_ = self.export_as(self.NEXT, "next_vv")
        vault = os.path.join(out, "vault")
        rec = {"Notion_Obsidian/写回预览/写回预览-wb-20261121T1012-ab12.md": "writeback.py",
               "Notion_Obsidian/运行记录/写回-wb-20261121T1012-ab12.md": "writeback.py",
               "Notion_Obsidian/运行记录/20261121T0630-T1-已暂停.md": "refresh.py",
               "Notion_Obsidian/运行记录/20261121T1230-T1-未完成.md": "refresh.py"}
        for rel, by in rec.items():
            p = os.path.join(vault, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(f'---\nnotion_kind: generated\ngenerated_by: {by}\nplan_id: "wb-20261121T1012-ab12"\n---\n\n'
                         "# 记录\n\n- 本地文件 `General/Root Page/Second.md`\n")
        ok, r, _ = vv.verify(self.fx["snap"], self.fx["sidebar"], vault, os.path.join(self.tmp, "vv_next"),
                             self.fx["snap"] + ".roots.json", snapshot_immutable=True)
        self.assertTrue(ok, {c: x["summary"] for c, x in r["checks"].items() if x["status"] != "PASS"})
        c05 = r["checks"]["C05"]
        self.assertEqual(c05["details"]["problems"], [])
        self.assertEqual(c05["summary"].split()[0], "sections=12")  # the 9 Notion sections + 3
        self.assertEqual([(w["folder"], w["issue"]) for w in c05["details"]["warnings"]],
                         [("Notion_Obsidian/运行记录", "order_attribute_not_validated"),
                          ("Notion_Obsidian/写回预览", "order_attribute_not_validated")])
        self.assertEqual(r["checks"]["C08"]["details"]["generated_files_checked(WRITEBACK_SPEC_5.1a)"], 4)


if __name__ == "__main__":
    unittest.main()
