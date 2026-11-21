#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for tools/verify_vault.py (SPEC v1.2).

Builds a tiny nmig-format snapshot, a sidebar JSON, a roots.json and a small
vault written by hand to SPEC v1.2 (incl. section 13: database templates under
`<db folder>/_templates/`, a 300+ text-block page for the strict C10 rule), then
checks that every check PASSes on the fixture and that each injected fault makes
the intended check FAIL (C11: WARN).

Run:  python3 -I -m unittest discover -s /data120/Obsidian/notion-obsidian/tools/tests -v
(temporary files go to $TMPDIR)
"""
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
import unittest

sys.dont_write_bytecode = True  # do not leave __pycache__ next to the tools
HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("verify_vault", os.path.join(HERE, "..", "verify_vault.py"))
vv = importlib.util.module_from_spec(_spec)
sys.modules["verify_vault"] = vv
_spec.loader.exec_module(vv)

TEAMSPACES = ["General", "Quantitative Investing", "x_to_notion", "Godcreate Business", "Godcreate AI工作空间"]
CT, LT = "2026-01-01T00:00:00.000Z", "2026-01-02T00:00:00.000Z"
PNG = b"\x89PNG\r\n\x1a\nfixture-image-bytes"
PNG_SHA = hashlib.sha256(PNG).hexdigest()
ATT_REL = f"General/_attachments/pic__{PNG_SHA[:8]}.png"


def uid(n):
    return f"{n:08x}-0000-4000-8000-{n:012x}"


D1, DS1, R1, R2, S1 = uid(1), uid(2), uid(3), uid(4), uid(5)
P1, P2, P3, P4, D2, DS2, T1 = uid(10), uid(11), uid(12), uid(13), uid(14), uid(15), uid(16)
DS3, R3, R4 = uid(17), uid(18), uid(19)
Q1, X1, B1, A1 = uid(20), uid(30), uid(40), uid(50)
Z1, W1, R5 = uid(60), uid(61), uid(62)  # used by the reference tests only
# SPEC v1.2 section 13: templates of database Tasks (D1 / DS1); TW has a child page AG
TW, TB, AG = uid(70), uid(71), uid(72)
# Server (Q1) is the large page: 320 log lines + one paragraph that occurs twice (C10 strict rule)
SERVER_LINES = [f"Line {k:03d} of the server log: rotated keys on host alpha-{k:03d}" for k in range(320)]
REPEATED = "Repeated paragraph about nightly backups"
SERVER_TEXTS = SERVER_LINES[:100] + [REPEATED] + SERVER_LINES[100:200] + [REPEATED] + SERVER_LINES[200:]


def rt(text, bold=False, mention=None):
    if mention:
        return {"type": "mention", "mention": mention, "plain_text": text, "href": None,
                "annotations": {"bold": False}}
    return {"type": "text", "text": {"content": text, "link": None}, "plain_text": text, "href": None,
            "annotations": {"bold": bold}}


def page_obj(i, title, parent, trashed=False, icon=None):
    return {"object": "page", "id": i, "created_time": CT, "last_edited_time": LT, "icon": icon, "cover": None,
            "parent": parent, "in_trash": trashed, "is_archived": False,
            "properties": {"title": {"id": "title", "type": "title", "title": [rt(title)]}},
            "url": "https://app.notion.com/p/" + i.replace("-", "")}


def row_obj(i, title, ds, db, status):
    return {"object": "page", "id": i, "created_time": CT, "last_edited_time": LT, "icon": None, "cover": None,
            "parent": {"type": "data_source_id", "data_source_id": ds, "database_id": db},
            "in_trash": False, "is_archived": False,
            "properties": {"Name": {"id": "title", "type": "title", "title": [rt(title)]},
                           "Status": {"id": "s", "type": "select", "select": {"name": status}}}}


def db_obj(i, title, ds_list, parent):
    return {"object": "database", "id": i, "title": [rt(title)],
            "data_sources": [{"id": ds, "name": title} for ds in ds_list],
            "parent": parent, "in_trash": False, "created_time": CT, "last_edited_time": LT,
            "icon": {"type": "emoji", "emoji": "📋"}, "cover": None}


def ds_obj(i, db, rows, templates=None):
    d = {"object": "data_source", "id": i, "_row_ids": rows, "in_trash": False,
         "parent": {"type": "database_id", "database_id": db}}
    if templates is not None:  # SPEC v1.2: `_template_ids` (a missing key means "no templates")
        d["_template_ids"] = list(templates)
        d["_templates"] = [{"id": t, "name": "", "is_default": False} for t in templates]
    return d


def build_snapshot(path, extra_blocks=(), extra_objs=(), ds_rows=None, wal=False, ds_templates=None,
                   template_objs=True):
    """Returns the open writer connection when wal=True (the caller closes it), else None.
    ds_templates: {ds: [template ids] or None (= key absent)} overriding the default
    (DS1: [TW, TB]; DS2: key absent; DS3: []). template_objs=False leaves the template pages out."""
    con = sqlite3.connect(path)
    if wal:
        con.execute("pragma journal_mode=wal")
        con.execute("pragma wal_autocheckpoint=0")
    con.executescript("""
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE work(id TEXT, kind TEXT, parent_id TEXT, root_id TEXT, depth INT,
        status TEXT, attempts INT DEFAULT 0, error TEXT, updated TEXT, PRIMARY KEY(id, kind));
    CREATE TABLE objects(id TEXT, kind TEXT, parent_id TEXT, root_id TEXT, depth INT,
        data TEXT, fetched_at TEXT, PRIMARY KEY(id, kind));
    CREATE TABLE blocks(id TEXT PRIMARY KEY, page_id TEXT, parent_id TEXT, ord INT,
        type TEXT, has_children INT, data TEXT);
    CREATE INDEX blocks_page ON blocks(page_id);
    CREATE TABLE comments(id TEXT PRIMARY KEY, page_id TEXT, data TEXT);
    CREATE TABLE files(key TEXT PRIMARY KEY, owner_id TEXT, owner_kind TEXT, field TEXT,
        name TEXT, size INT, sha256 TEXT, status TEXT, error TEXT);
    CREATE TABLE events(ts TEXT, level TEXT, obj TEXT, msg TEXT);
    """)
    ws = {"type": "workspace", "workspace": True}
    rows = {DS1: [R1, R2], DS2: [R3], DS3: [R4]}
    rows.update(ds_rows or {})
    tmpl = {DS1: [TW, TB], DS2: None, DS3: []}
    tmpl.update(ds_templates or {})
    objs = [
        (D1, "database", None, D1, 0, db_obj(D1, "Tasks", [DS1], ws)),
        (DS1, "data_source", D1, D1, 0, ds_obj(DS1, D1, rows[DS1], tmpl[DS1])),
        (R1, "page", D1, D1, 1, row_obj(R1, "Row A", DS1, D1, "Todo")),
        (R2, "page", D1, D1, 1, row_obj(R2, "Row B", DS1, D1, "Done")),
        (S1, "page", R2, D1, 2, page_obj(S1, "Sub Page", {"type": "page_id", "page_id": R2})),
        (P1, "page", None, P1, 0, page_obj(P1, "Home", ws, icon={"type": "emoji", "emoji": "🏠"})),
        (P2, "page", P1, P1, 1, page_obj(P2, "Child One", {"type": "page_id", "page_id": P1})),
        (P3, "page", P1, P1, 1, page_obj(P3, "Child Two", {"type": "block_id", "block_id": uid(102)})),
        (P4, "page", P3, P1, 2, page_obj(P4, "Grand: Child", {"type": "page_id", "page_id": P3})),
        # multi-data-source database: it lists DS3 BEFORE DS2 (rowid order is the opposite)
        (D2, "database", P1, P1, 1, db_obj(D2, "Inline DB", [DS3, DS2], {"type": "page_id", "page_id": P1})),
        (DS2, "data_source", D2, P1, 1, ds_obj(DS2, D2, rows[DS2], tmpl[DS2])),
        (DS3, "data_source", D2, P1, 1, ds_obj(DS3, D2, rows[DS3], tmpl[DS3])),
        (R3, "page", D2, P1, 2, row_obj(R3, "Row C", DS2, D2, "Todo")),
        (R4, "page", D2, P1, 2, row_obj(R4, "Row D", DS3, D2, "Done")),
        (T1, "page", P1, P1, 1, page_obj(T1, "Old Page", {"type": "page_id", "page_id": P1}, trashed=True)),
        (Q1, "page", None, Q1, 0, page_obj(Q1, "Server", ws)),
        (X1, "page", None, X1, 0, page_obj(X1, "Teamspace Home", ws)),
        (B1, "page", None, B1, 0, page_obj(B1, "Home", ws)),
        (A1, "page", None, A1, 0, page_obj(A1, "Teamspace Home", ws)),
    ]
    if template_objs:
        # template pages: API parent = the data source (like rows); rowid order (TB first) differs from
        # the `_template_ids` order (TW first)
        objs += [
            (TB, "page", D1, D1, 1, row_obj(TB, "Bug Report", DS1, D1, "Todo")),
            (TW, "page", D1, D1, 1, row_obj(TW, "Weekly Plan", DS1, D1, "Todo")),
            (AG, "page", TW, D1, 2, page_obj(AG, "Agenda", {"type": "page_id", "page_id": TW})),
        ]
    objs += list(extra_objs)
    for i, kind, par, root, depth, data in objs:
        con.execute("insert into objects values (?,?,?,?,?,?,?)",
                    (i, kind, par, root, depth, json.dumps(data, ensure_ascii=False), LT))
        con.execute("insert into work(id, kind, parent_id, root_id, depth, status) values (?,?,?,?,?,?)",
                    (i, kind, par, root, depth, "done"))

    def blk(i, page, parent, ordv, typ, payload, has_children=0):
        data = {"object": "block", "id": i, "type": typ, typ: payload, "has_children": bool(has_children),
                "in_trash": False}
        con.execute("insert into blocks values (?,?,?,?,?,?,?)",
                    (i, page, parent, ordv, typ, has_children, json.dumps(data, ensure_ascii=False)))

    def para(text):
        return {"rich_text": [rt(text)]}

    # Home (P1)
    blk(uid(100), P1, P1, 0, "paragraph",
        {"rich_text": [rt("Welcome to the "), rt("home page", bold=True), rt(" of the team")]})
    blk(uid(101), P1, P1, 1, "heading_2", para("Section"))
    # leftover child_page block pointing at a ROOT whose API parent is the workspace (like the real
    # import-log page): a reference, rendered as a link, not a child (same ord as the heading: the
    # tie is broken by insertion order, so it renders right after "## Section")
    blk(B1, P1, P1, 1, "child_page", {"title": "Home"}, 0)
    blk(P2, P1, P1, 2, "child_page", {"title": "Child One"}, 1)
    blk(uid(102), P1, P1, 3, "toggle", para("Toggle title"), 1)
    blk(P3, P1, uid(102), 0, "child_page", {"title": "Child Two"}, 1)
    blk(uid(103), P1, P1, 4, "image", {"caption": [], "type": "file", "file": {"url": "https://example.invalid/x"}})
    blk(D2, P1, P1, 5, "child_database", {"title": "Inline DB"})
    blk(uid(104), P1, P1, 6, "table", {"table_width": 2, "has_column_header": True}, 1)
    blk(uid(105), P1, uid(104), 0, "table_row", {"cells": [[rt("a")], [rt("b")]]})
    blk(uid(106), P1, uid(104), 1, "table_row", {"cells": [[rt("c | pipe")], [rt("d")]]})
    blk(uid(107), P1, P1, 7, "code",
        {"rich_text": [rt('x = "[[not a link]]"  # a long code line here')], "language": "python"})
    blk(T1, P1, P1, 8, "child_page", {"title": "Old Page"}, 0)
    blk(uid(108), P1, P1, 9, "paragraph",
        {"rich_text": [rt("Child One", mention={"type": "page", "page": {"id": P2}}), rt(" is linked")]})
    # Child One (P2)
    blk(uid(110), P2, P2, 0, "paragraph", para("Some *special* text with a [bracket] and an escape \\ char"))
    # Child Two (P3)
    blk(P4, P3, P3, 0, "child_page", {"title": "Grand: Child"}, 1)
    blk(uid(111), P3, P3, 1, "paragraph", para("After the grandchild"))
    # Grand: Child (P4)
    blk(uid(112), P4, P4, 0, "paragraph", para("Grandchild text"))
    blk(uid(119), P4, P4, 1, "file", {"caption": [], "type": "file", "file": {"url": "https://example.invalid/y"},
                                      "name": "lost.pdf"})
    # Row B (R2) and Sub Page (S1)
    blk(uid(113), R2, R2, 0, "paragraph", para("Row B body text"))
    blk(S1, R2, R2, 1, "child_page", {"title": "Sub Page"}, 1)
    blk(uid(114), S1, S1, 0, "bulleted_list_item", para("Sub page bullet"))
    # other roots
    blk(uid(115), Q1, Q1, 0, "heading_1", para("Server notes"))
    blk(uid(116), Q1, Q1, 1, "to_do", {"rich_text": [rt("Check backups")], "checked": True})
    blk(uid(117), X1, X1, 0, "quote", para("Quote in x"))
    blk(uid(118), A1, A1, 0, "callout", {"rich_text": [rt("Callout text")], "icon": {"type": "emoji", "emoji": "💡"}})
    # the large page (C10 strict rule)
    for k, text in enumerate(SERVER_TEXTS):
        blk(uid(1000 + k), Q1, Q1, 2 + k, "paragraph", para(text))
    if template_objs:
        blk(uid(130), TW, TW, 0, "paragraph", para("Weekly template intro"))
        blk(AG, TW, TW, 1, "child_page", {"title": "Agenda"}, 1)
        blk(uid(131), AG, AG, 0, "paragraph", para("Agenda item"))
        blk(uid(132), TB, TB, 0, "paragraph", para("Steps to reproduce"))
    for b in extra_blocks:
        blk(*b)
    con.execute("insert into files values (?,?,?,?,?,?,?,?,?)",
                (uid(103) + ":image", uid(103), "block", "image", "pic.png", len(PNG), PNG_SHA, "ok", "image/png"))
    con.execute("insert into files values (?,?,?,?,?,?,?,?,?)",
                (uid(119) + ":file", uid(119), "block", "file", "lost.pdf", 0, None, "error", "http 403"))
    con.execute("insert into meta values ('notion_version', '2026-03-11')")
    con.commit()
    if wal:
        return con
    con.close()
    return None


ROOTS = [  # id, title, teamspace, root_order, kind
    (D1, "Tasks", "General", 1, "database"),
    (P1, "Home", "General", 2, "page"),
    (Q1, "Server", "Quantitative Investing", 1, "page"),
    (X1, "Teamspace Home", "x_to_notion", 1, "page"),
    (B1, "Home", "Godcreate Business", 1, "page"),
    (A1, "Teamspace Home", "Godcreate AI工作空间", 1, "page"),
]


def build_sidebar(path, roots_path):
    roots = [{"id": i, "title": t, "teamspace": ts, "teamspace_order": TEAMSPACES.index(ts) + 1, "root_order": o}
             for i, t, ts, o, _k in ROOTS]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"teamspace_order_required": TEAMSPACES, "teamspace_order_observed": TEAMSPACES,
                   "roots": roots}, fh, ensure_ascii=False)
    with open(roots_path, "w", encoding="utf-8") as fh:
        json.dump([dict(r, kind=k, in_trash=False) for r, (_i, _t, _ts, _o, k) in zip(roots, ROOTS)], fh,
                  ensure_ascii=False)


def fm(**fields):
    lines = ["---"]
    for k, v in fields.items():
        if v is None:
            lines.append(f"{k}: null")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, int):
            lines.append(f"{k}: {v}")
        elif isinstance(v, list):
            lines.append(f"{k}:")
            lines += [f"  - {json.dumps(x, ensure_ascii=False)}" for x in v]
        else:
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def nfm(i, kind, title, parent, ts, order, **extra):
    d = dict(notion_id=i, notion_kind=kind, notion_url="https://app.notion.com/p/" + i.replace("-", ""),
             notion_title=title, notion_parent_id=parent, notion_teamspace=ts, notion_order=order,
             notion_created_time=CT, notion_last_edited_time=LT)
    d.update(extra)
    return fm(**d)


def base_yaml(folder, db):
    return (f"filters:\n  and:\n    - file.inFolder(\"{folder}\")\n    - 'notion_kind == \"row\"'\n"
            f"    - 'notion_parent_id == \"{db}\"'\nviews:\n  - type: table\n    name: Table\n    order:\n"
            f"      - file.name\n      - Status\n    sort:\n      - property: notion_order\n        direction: ASC\n")


def base_yaml_legacy(folder, db):
    """Pre-v1.2 exporter layout: `note.`-prefixed ids in order / sort, sequences NOT indented."""
    return (f"filters:\n  and:\n  - file.inFolder(\"{folder}\")\n  - file.ext == \"md\"\n"
            f"  - note.notion_kind == \"row\"\n  - note.notion_parent_id == \"{db}\"\n"
            "properties:\n  file.name:\n    displayName: 名称\n  note.notion_order:\n    displayName: Notion 顺序\n"
            "views:\n- type: table\n  name: Notion 顺序\n  order:\n  - file.name\n  - note.Status\n"
            "  - note.notion_order\n  sort:\n  - property: note.notion_order\n    direction: ASC\n")


def base_yaml_canonical(folder, db, direction="ASC"):
    """SPEC v1.2 section 13 canonical layout (Obsidian 1.15.1 write-back): bare ids in order / sort,
    sequences indented 2 spaces under their key, `properties` keys keep file.name / note.<name>."""
    return (f"filters:\n  and:\n    - file.inFolder(\"{folder}\")\n    - file.ext == \"md\"\n"
            f"    - note.notion_kind == \"row\"\n    - note.notion_parent_id == \"{db}\"\n"
            "properties:\n  file.name:\n    displayName: 名称\n  note.notion_order:\n    displayName: Notion 顺序\n"
            "views:\n  - type: table\n    name: Notion 顺序\n    order:\n      - file.name\n      - Status\n"
            f"      - notion_order\n    sort:\n      - property: notion_order\n        direction: {direction}\n")


SORTSPEC = """---
notion_kind: "generated"
sorting-spec: |
  // generated from Notion order; custom-sort 3.2.0 syntax
  target-folder: /
  /folders General
  /folders Quantitative Investing
  /folders x_to_notion
  /folders Godcreate Business
  /folders Godcreate AI工作空间
  /folders Notion_Obsidian
  %

  target-folder: General
  /folders Tasks
  /folders Home
  %
  /folders _attachments

  target-folder: Quantitative Investing
  /:files. Server.md
  %
  /folders _attachments

  target-folder: x_to_notion
  /:files. Teamspace Home.md
  %
  /folders _attachments

  target-folder: Godcreate Business
  /:files. Home.md
  %
  /folders _attachments

  target-folder: Godcreate AI工作空间
  /:files. Teamspace Home.md
  %
  /folders _attachments

  target-folder: General/Tasks
  /:files. Tasks.md
  /:files. Tasks.base
  /:files. Row A.md
  /folders Row B
  %
  /folders _templates

  target-folder: General/Tasks/_templates
  /folders Weekly Plan
  /:files. Bug Report.md
  %

  target-folder: General/Tasks/_templates/Weekly Plan
  /:files. Weekly Plan.md
  /:files. Agenda.md
  %

  target-folder: General/Tasks/Row B
  /:files. Row B.md
  /:files. Sub Page.md
  %

  target-folder: General/Home
  /:files. Home.md
  /:files. Child One.md
  /folders Child Two
  /folders Inline DB
  %

  target-folder: General/Home/Child Two
  /:files. Child Two.md
  /:files. Grand： Child.md
  %

  target-folder: General/Home/Inline DB
  /:files. Inline DB.md
  /:files. Inline DB.base
  /:files. Row D.md
  /:files. Row C.md
  %
---
"""

INDEX_LINKS = [
    ("General/Tasks/Tasks", "Tasks"), ("General/Tasks/Row A", "Row A"), ("General/Tasks/Row B/Row B", "Row B"),
    ("General/Tasks/Row B/Sub Page", "Sub Page"), ("General/Home/Home", "Home"),
    ("General/Home/Child One", "Child One"), ("General/Home/Child Two/Child Two", "Child Two"),
    ("General/Home/Child Two/Grand： Child", "Grand: Child"), ("General/Home/Inline DB/Inline DB", "Inline DB"),
    ("General/Home/Inline DB/Row D", "Row D"), ("General/Home/Inline DB/Row C", "Row C"),
    ("Quantitative Investing/Server", "Server"), ("x_to_notion/Teamspace Home", "Teamspace Home"),
    ("Godcreate Business/Home", "Home"), ("Godcreate AI工作空间/Teamspace Home", "Teamspace Home"),
]


def vault_files():
    g = "General"
    return {
        "General/Tasks/Tasks.md": nfm(D1, "database", "Tasks", None, g, 1, notion_icon="📋") +
        "| 属性 | 类型 | 选项 |\n| --- | --- | --- |\n| Name | title |  |\n| Status | select | Todo, Done |\n\n"
        "- [[General/Tasks/Row A|Row A]]\n- [[General/Tasks/Row B/Row B|Row B]]\n\n![[General/Tasks/Tasks.base]]\n\n"
        "## 模板\n\n- [[General/Tasks/_templates/Weekly Plan/Weekly Plan|Weekly Plan]]\n"
        "- [[General/Tasks/_templates/Bug Report|Bug Report]]\n",
        "General/Tasks/_templates/Weekly Plan/Weekly Plan.md": nfm(TW, "template", "Weekly Plan", D1, g, 1) +
        "Weekly template intro\n\n[[General/Tasks/_templates/Weekly Plan/Agenda|Agenda]]\n",
        "General/Tasks/_templates/Weekly Plan/Agenda.md": nfm(AG, "page", "Agenda", TW, g, 1) + "Agenda item\n",
        "General/Tasks/_templates/Bug Report.md": nfm(TB, "template", "Bug Report", D1, g, 2) + "Steps to reproduce\n",
        "General/Tasks/Tasks.base": base_yaml("General/Tasks", D1),
        "General/Tasks/Row A.md": nfm(R1, "row", "Row A", D1, g, 1, Status="Todo"),
        "General/Tasks/Row B/Row B.md": nfm(R2, "row", "Row B", D1, g, 2, Status="Done") +
        "Row B body text\n\n[[General/Tasks/Row B/Sub Page|Sub Page]]\n",
        "General/Tasks/Row B/Sub Page.md": nfm(S1, "page", "Sub Page", R2, g, 1) + "- Sub page bullet\n",
        "General/Home/Home.md": nfm(P1, "page", "Home", None, g, 2, notion_icon="🏠") +
        "Welcome to the **home page** of the team\n\n## Section\n\n"
        "[[Godcreate Business/Home|Home]]\n\n"  # SPEC 3 v1.1 reference -> link to the real location
        "[[General/Home/Child One|Child One]]\n\n"
        "> [!toggle]- Toggle title\n> [[General/Home/Child Two/Child Two|Child Two]]\n\n"
        f"![[{ATT_REL}]]\n\n"
        "![[General/Home/Inline DB/Inline DB.base]]\n[[General/Home/Inline DB/Inline DB|Inline DB]]\n\n"
        "| a | b |\n| --- | --- |\n| c \\| pipe | d |\n\n"
        "```python\nx = \"[[not a link]]\"  # a long code line here\n```\n\n"
        "[[General/Home/Child One|Child One]] is linked\n",
        "General/Home/Child One.md": nfm(P2, "page", "Child One", P1, g, 1) +
        "Some \\*special\\* text with a \\[bracket\\] and an escape \\\\ char\n",
        "General/Home/Child Two/Child Two.md": nfm(P3, "page", "Child Two", P1, g, 2) +
        "[[General/Home/Child Two/Grand： Child|Grand: Child]]\n\nAfter the grandchild\n",
        "General/Home/Child Two/Grand： Child.md": nfm(P4, "page", "Grand: Child", P3, g, 1,
                                                      aliases=["Grand: Child"]) +
        "Grandchild text\n\n%% notion: file lost.pdf not downloaded (status=error) %%\n",
        "General/Home/Inline DB/Inline DB.md": nfm(D2, "database", "Inline DB", P1, g, 3, notion_icon="📋") +
        "| 属性 | 类型 | 选项 |\n| --- | --- | --- |\n| Name | title |  |\n| Status | select | Todo, Done |\n\n"
        "- [[General/Home/Inline DB/Row D|Row D]]\n- [[General/Home/Inline DB/Row C|Row C]]\n\n"
        "![[General/Home/Inline DB/Inline DB.base]]\n",
        "General/Home/Inline DB/Inline DB.base": base_yaml("General/Home/Inline DB", D2),
        "General/Home/Inline DB/Row D.md": nfm(R4, "row", "Row D", D2, g, 1, Status="Done"),
        "General/Home/Inline DB/Row C.md": nfm(R3, "row", "Row C", D2, g, 2, Status="Todo"),
        "Quantitative Investing/Server.md": nfm(Q1, "page", "Server", None, "Quantitative Investing", 1) +
        "# Server notes\n\n- [x] Check backups\n\n" + "\n\n".join(SERVER_TEXTS) + "\n",
        "x_to_notion/Teamspace Home.md": nfm(X1, "page", "Teamspace Home", None, "x_to_notion", 1) +
        "> Quote in x\n",
        "Godcreate Business/Home.md": nfm(B1, "page", "Home", None, "Godcreate Business", 1),
        "Godcreate AI工作空间/Teamspace Home.md": nfm(A1, "page", "Teamspace Home", None, "Godcreate AI工作空间", 1) +
        "> [!note] 💡\n> Callout text\n",
        "Notion_Obsidian/README.md": "# README\n\nGenerated by the exporter.\n",
        "Notion_Obsidian/sortspec.md": SORTSPEC,
        "Notion_Obsidian/Godcreate 目录索引.md": fm(notion_kind="generated") +
        "".join(f"- [[{p}|{t}]]\n" for p, t in INDEX_LINKS),
        ".obsidian/plugins/custom-sort/data.json": "{}\n",
    }


def write_vault(root):
    for rel, text in vault_files().items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    p = os.path.join(root, *ATT_REL.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(PNG)


def tree_hash(root):
    h = {}
    for dp, _dns, fns in os.walk(root):
        for f in fns:
            full = os.path.join(dp, f)
            with open(full, "rb") as fh:
                h[os.path.relpath(full, root)] = hashlib.sha256(fh.read()).hexdigest()
    return h


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="verify_vault_test_")
        self.snap = os.path.join(self.tmp, "snap", "snap.db")
        os.makedirs(os.path.dirname(self.snap))
        self.sidebar = os.path.join(self.tmp, "sidebar.json")
        self.roots = os.path.join(self.tmp, "snap.db.roots.json")
        self.vault = os.path.join(self.tmp, "vault")
        build_snapshot(self.snap)
        build_sidebar(self.sidebar, self.roots)
        write_vault(self.vault)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_verify(self, vault=None, out_name="out", negative=False, snap=None):
        out = os.path.join(self.tmp, out_name)
        return vv.verify(snap or self.snap, self.sidebar, vault or self.vault, out, self.roots, negative)

    def statuses(self, rep):
        return {k: r["status"] for k, r in rep["checks"].items()}

    def mutate_copy(self, name):
        dst = os.path.join(self.tmp, name)
        shutil.copytree(self.vault, dst, symlinks=True)
        return dst

    def edit(self, root, rel, old, new):
        p = os.path.join(root, *rel.split("/"))
        with open(p, encoding="utf-8") as fh:
            t = fh.read()
        self.assertIn(old, t)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(t.replace(old, new, 1))

    def snap2(self, name, **kw):
        p = os.path.join(self.tmp, name, "snap.db")
        os.makedirs(os.path.dirname(p))
        build_snapshot(p, **kw)
        return p

    def assert_fails(self, vault, check, also_pass=()):
        ok, rep, _ = self.run_verify(vault=vault, out_name="out_" + os.path.basename(vault))
        st = self.statuses(rep)
        self.assertFalse(ok)
        self.assertEqual(st[check], "FAIL", rep["checks"][check]["summary"])
        for c in also_pass:
            self.assertEqual(st[c], "PASS", (c, rep["checks"][c]["summary"]))
        return rep


class TestAllPass(Fixture):
    def test_fixture_passes_every_check(self):
        ok, rep, _ = self.run_verify()
        bad = {k: (r["summary"], r["details"]) for k, r in rep["checks"].items() if r["status"] != "PASS"}
        self.assertEqual(bad, {}, json.dumps(bad, ensure_ascii=False, indent=1, default=str)[:4000])
        self.assertTrue(ok)
        self.assertEqual(rep["overall"], "PASS")
        self.assertEqual(set(rep["status_by_check"]), set(vv.CHECK_TITLES))
        e = rep["expectation"]
        self.assertEqual(e["sidebar_roots"], 6)
        self.assertEqual(e["expected_by_kind"], {"database": 2, "page": 10, "row": 4, "template": 2})
        self.assertEqual(e["databases_with_templates"], 1)
        self.assertEqual(e["trashed_excluded"], 1)
        self.assertEqual(e["snapshot_gaps_reachable_but_not_in_snapshot"], 0)
        self.assertEqual(e["references_to_roots"], 1)
        self.assertEqual(e["snapshot_open_mode"], "mode=ro&immutable=1")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "out", "verify.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "out", "verify.md")))

    def test_expected_order_and_parents(self):
        exp = vv.Expectation(self.snap, self.sidebar, self.roots)
        # trashed T1 excluded, toggle-nested P3 (parent block_id inside P1) found in body order,
        # the leftover child_page block for root B1 is a reference, not a child
        self.assertEqual(exp.objs[P1].children, [P2, P3, D2])
        self.assertEqual(exp.objs[D1].children, [R1, R2])
        self.assertEqual(exp.objs[R1].kind, "row")
        self.assertEqual(exp.objs[P3].order, 2)
        self.assertEqual(exp.objs[D2].order, 3)
        self.assertEqual(exp.objs[P4].parent, P3)
        self.assertEqual(exp.objs[S1].teamspace, "General")
        self.assertEqual(exp.objs[A1].teamspace, "Godcreate AI工作空间")

    def test_multi_data_source_rows_follow_database_order(self):
        exp = vv.Expectation(self.snap, self.sidebar, self.roots)
        # the database lists DS3 before DS2: rows of DS3 first, regardless of snapshot rowid order
        self.assertEqual(exp.objs[D2].children, [R4, R3])
        self.assertEqual([exp.objs[R4].order, exp.objs[R3].order], [1, 2])

    def test_root_referenced_by_child_block_stays_root(self):
        exp = vv.Expectation(self.snap, self.sidebar, self.roots)
        self.assertTrue(exp.objs[B1].is_root)
        self.assertIsNone(exp.objs[B1].parent)
        self.assertEqual(exp.objs[B1].order, 1)
        self.assertEqual(exp.objs[B1].teamspace, "Godcreate Business")
        refs = [(m["id"], m["from"], m["target_is_root"], m["api_parent"]["type"]) for m in exp.references]
        self.assertEqual(refs, [(B1, P1, True, "workspace")])
        self.assertEqual(exp.multi_ref, [])

    def test_inputs_untouched(self):
        before_v, before_s = tree_hash(self.vault), tree_hash(os.path.dirname(self.snap))
        self.run_verify(out_name="out_untouched", negative=True)
        self.assertEqual(tree_hash(self.vault), before_v)
        self.assertEqual(tree_hash(os.path.dirname(self.snap)), before_s)

    def test_out_inside_vault_refused(self):
        with self.assertRaises(vv.InputError):
            vv.verify(self.snap, self.sidebar, self.vault, os.path.join(self.vault, "rep"), self.roots)


class TestCanonicalParent(Fixture):
    """SPEC 3 v1.1: the object's own API parent decides; other child blocks / rows are references."""

    def test_child_page_block_of_object_parented_elsewhere_is_reference(self):
        # Z1's API parent is Q1 (page) and W1's is a block of Q1, yet both child_page blocks sit in P1
        # (before P2). Neither is a child of P1; P1's child sequence and notion_order are unchanged.
        snap = self.snap2("s_ref", extra_objs=[
            (Z1, "page", Q1, Q1, 1, page_obj(Z1, "Moved Page", {"type": "page_id", "page_id": Q1})),
            (W1, "page", Q1, Q1, 1, page_obj(W1, "Block Child", {"type": "block_id", "block_id": uid(115)})),
        ], extra_blocks=[(Z1, P1, P1, 2, "child_page", {"title": "Moved Page"}, 0),
                         (W1, P1, P1, 2, "child_page", {"title": "Block Child"}, 0)])
        exp = vv.Expectation(snap, self.sidebar, self.roots)
        self.assertEqual(exp.objs[P1].children, [P2, P3, D2])
        self.assertEqual([exp.objs[c].order for c in exp.objs[P1].children], [1, 2, 3])
        self.assertNotIn(Z1, exp.objs)
        self.assertNotIn(W1, exp.objs)
        by_id = {m["id"]: m for m in exp.references}
        self.assertEqual(by_id[Z1]["api_parent"], {"type": "page_id", "id": Q1})
        self.assertEqual(by_id[W1]["api_parent"]["type"], "block_id")
        self.assertFalse(by_id[Z1]["target_in_scope"])
        unreach = {u["id"]: u for u in exp.unreachable}
        self.assertEqual(unreach[Z1]["referenced_from"], [P1])

    def test_block_id_parent_inside_current_page_is_child(self):
        exp = vv.Expectation(self.snap, self.sidebar, self.roots)
        self.assertIn(P3, exp.objs[P1].children)  # parent block_id = toggle uid(102) inside P1
        self.assertTrue(exp.api_parent_ok(P1, {"parent": {"type": "block_id", "block_id": uid(102)}}, False)[0])
        self.assertFalse(exp.api_parent_ok(P1, {"parent": {"type": "block_id", "block_id": uid(115)}}, False)[0])
        self.assertFalse(exp.api_parent_ok(P1, {"parent": {"type": "workspace", "workspace": True}}, False)[0])

    def test_row_of_other_data_source_is_reference(self):
        # R5's parent is DS1 (database Tasks) but DS2 (database Inline DB) lists it: not a row of D2
        snap = self.snap2("s_rowref", extra_objs=[
            (R5, "page", D1, D1, 1, row_obj(R5, "Row E", DS1, D1, "Todo"))], ds_rows={DS2: [R5, R3]})
        exp = vv.Expectation(snap, self.sidebar, self.roots)
        self.assertEqual(exp.objs[D2].children, [R4, R3])
        self.assertEqual(exp.objs[R3].order, 2)
        self.assertEqual([(m["id"], m["via"]) for m in exp.references if m["id"] == R5], [(R5, "data_source")])
        self.assertTrue(exp.api_parent_ok(D2, {"parent": {"type": "database_id", "database_id": D2}}, True)[0])

    def test_reference_link_in_parent_body_is_not_an_order_error(self):
        # the base fixture's Home note links root B1 (a reference) ABOVE its children: C04 must PASS
        ok, rep, _ = self.run_verify(out_name="out_refc04")
        self.assertEqual(rep["checks"]["C04"]["status"], "PASS", rep["checks"]["C04"]["summary"])
        # ...but if the exporter treated the reference as a child (folder + notion_order), it is caught
        v = self.mutate_copy("v_refchild")
        self.edit(v, "General/Home/Child One.md", "notion_order: 1", "notion_order: 2")
        rep = self.assert_fails(v, "C04")
        self.assertTrue(rep["checks"]["C04"]["details"]["notion_order_mismatch"])


class TestSnapshotOpenMode(Fixture):
    def test_live_wal_snapshot_opened_mode_ro_and_sees_wal_rows(self):
        p = os.path.join(self.tmp, "live", "snap.db")
        os.makedirs(os.path.dirname(p))
        writer = build_snapshot(p, wal=True)  # writer keeps the DB open: all rows still in -wal
        try:
            self.assertTrue(os.path.getsize(p + "-wal") > 0)
            exp = vv.Expectation(p, self.sidebar, self.roots)
            self.assertEqual(exp.open_mode, "mode=ro")
            self.assertEqual(len(exp.objs), 18)
            self.assertTrue(any("non-empty" in w and "mode=ro" in w for w in exp.warnings))
            ok, rep, _ = self.run_verify(out_name="out_live", snap=p)
            self.assertEqual(rep["checks"]["C10"]["status"], "PASS", rep["checks"]["C10"]["summary"])
            self.assertTrue(ok, {k: r["summary"] for k, r in rep["checks"].items() if r["status"] != "PASS"})
        finally:
            writer.close()

    def test_quiescent_snapshot_uses_immutable(self):
        self.assertFalse(vv.snapshot_is_live(self.snap))
        con, mode = vv.open_ro(self.snap)
        con.close()
        self.assertEqual(mode, "mode=ro&immutable=1")


class TestNegativeMode(Fixture):
    def test_negative_tests_all_detected(self):
        ok, rep, neg = self.run_verify(negative=True)
        self.assertTrue(ok, json.dumps(neg, ensure_ascii=False, indent=1, default=str)[:3000])
        self.assertEqual(len(vv.NEG_CASES), 9)  # 9 fault classes (SPEC v1.2 adds the C10 single-block case)
        self.assertEqual(len(neg["cases"]), len(vv.NEG_CASES))
        for c in neg["cases"]:
            self.assertIsNone(c.get("skipped"), c)
            self.assertEqual(c["target_status"], "FAIL", c)
            self.assertTrue(c["conclusive"], c)
        self.assertEqual({c["target_check"] for c in neg["cases"]},
                         {"C02", "C03", "C04", "C05", "C06", "C07", "C10"})
        c09 = [c for c in neg["cases"] if c["case"].startswith("09_")][0]
        self.assertEqual(c09["target_check"], "C10")
        # one block of the 324-block page; coverage stays above the old 99% threshold, still FAIL
        self.assertEqual(c09["mutation"]["page"], Q1)
        self.assertGreaterEqual(c09["mutation"]["page_text_blocks"], 300)
        self.assertEqual(c09["statuses"]["C02"], "PASS")
        self.assertEqual(c09["statuses"]["C04"], "PASS")
        self.assertEqual(c09["statuses"]["C06"], "PASS")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "out", "negative_tests.json")))
        ok2, rep2, _ = self.run_verify(out_name="out2")  # the original vault is still clean
        self.assertTrue(ok2)


class TestCli(Fixture):
    def test_main_exit_codes_and_cleanup(self):
        out = os.path.join(self.tmp, "cli_out")
        args = ["--snapshot", self.snap, "--sidebar", self.sidebar, "--roots-kinds", self.roots,
                "--vault", self.vault, "--out", out]
        self.assertEqual(vv.main(args + ["--negative-tests", "--neg-cleanup"]), 0)
        with open(os.path.join(out, "negative_tests.json"), encoding="utf-8") as fh:
            neg = json.load(fh)
        self.assertTrue(neg["all_detected"])
        for c in neg["cases"]:
            self.assertFalse(os.path.exists(os.path.join(out, "neg", c["case"], "vault")))
            self.assertTrue(os.path.isfile(os.path.join(out, "neg", c["case"], "verify.json")))
        v = self.mutate_copy("v_cli")
        os.remove(os.path.join(v, "Quantitative Investing", "Server.md"))
        self.assertEqual(vv.main(args[:-4] + ["--vault", v, "--out", os.path.join(self.tmp, "cli_out2")]), 1)
        self.assertEqual(vv.main(args[:-4] + ["--vault", os.path.join(self.tmp, "nope"),
                                              "--out", os.path.join(self.tmp, "cli_out3")]), 2)

    def test_as_of_classifies_missing_objects(self):
        v = self.mutate_copy("v_asof")
        os.remove(os.path.join(v, "General", "Home", "Child One.md"))
        out = os.path.join(self.tmp, "out_asof")
        ok, rep, _ = vv.verify(self.snap, self.sidebar, v, out, self.roots, as_of="2025-12-31T00:00:00Z")
        miss = rep["checks"]["C02"]["details"]["missing"]
        self.assertEqual([(m["id"], m["cause_hint"]) for m in miss],
                         [(P2, "fetched_after_as_of(snapshot_newer_than_export)")])
        self.assertEqual(rep["checks"]["C02"]["status"], "FAIL")  # classification never changes the status


class TestMiniYamlBackend(Fixture):
    """Same fixture, but with the built-in frontmatter parser instead of PyYAML."""

    def setUp(self):
        super().setUp()
        self._saved = vv._yaml
        vv._yaml = None

    def tearDown(self):
        vv._yaml = self._saved
        super().tearDown()

    def test_fixture_passes_and_faults_detected_without_pyyaml(self):
        ok, rep, neg = self.run_verify(negative=True)
        bad = {k: r["summary"] for k, r in rep["checks"].items() if r["status"] != "PASS"}
        self.assertEqual(bad, {})
        self.assertEqual(rep["tool"]["yaml_backend"], "mini")
        self.assertTrue(neg["all_detected"] and neg["all_conclusive"])


class TestTargetedFaults(Fixture):
    def test_c01_extra_top_level(self):
        v = self.mutate_copy("v_c01")
        os.makedirs(os.path.join(v, "Extra Folder"))
        rep = self.assert_fails(v, "C01")
        self.assertEqual(rep["checks"]["C01"]["details"]["extra"], ["Extra Folder"])

    def test_c01_hidden_items_ignored(self):
        v = self.mutate_copy("v_c01h")
        os.makedirs(os.path.join(v, ".trash"))
        ok, rep, _ = self.run_verify(vault=v, out_name="out_c01h")
        self.assertEqual(rep["checks"]["C01"]["status"], "PASS")

    def test_c02_snapshot_gap_reported(self):
        snap2 = self.snap2("s_gap", extra_blocks=[(uid(999), Q1, Q1, 5, "child_page", {"title": "Ghost"}, 0)])
        ok, rep, _ = self.run_verify(out_name="out_gap", snap=snap2)
        c02 = rep["checks"]["C02"]
        self.assertEqual(c02["status"], "FAIL")
        self.assertEqual([g["id"] for g in c02["details"]["missing_not_in_snapshot"]], [uid(999)])

    def test_c02_extra_unknown_id(self):
        v = self.mutate_copy("v_c02x")
        with open(os.path.join(v, "General", "Stray.md"), "w", encoding="utf-8") as fh:
            fh.write(nfm(uid(777), "page", "Stray", None, "General", 9))
        rep = self.assert_fails(v, "C02")
        self.assertEqual(rep["checks"]["C02"]["details"]["extra"][0]["reason"], "unknown_to_snapshot")

    def test_c02_generated_note_outside_task_dir(self):
        v = self.mutate_copy("v_c02g")
        self.edit(v, "General/Home/Child One.md", 'notion_kind: "page"', 'notion_kind: "generated"')
        rep = self.assert_fails(v, "C02")
        d = rep["checks"]["C02"]["details"]
        self.assertEqual(d["generated_outside_task_dir"], ["General/Home/Child One.md"])
        self.assertEqual([m["id"] for m in d["missing"]], [P2])

    def test_c02_md_in_non_canonical_attachments_folder_is_checked(self):
        # only <teamspace>/_attachments/*.md is exempt; a note under General/Home/_attachments/ is not
        v = self.mutate_copy("v_c02a")
        os.makedirs(os.path.join(v, "General", "Home", "_attachments"))
        with open(os.path.join(v, "General", "Home", "_attachments", "stray.md"), "w", encoding="utf-8") as fh:
            fh.write("no frontmatter\n")
        rep = self.assert_fails(v, "C02")
        self.assertEqual(rep["checks"]["C02"]["details"]["md_without_notion_id"], ["General/Home/_attachments/stray.md"])
        # ...while an .md attachment in the canonical location is not a note
        v2 = self.mutate_copy("v_c02a2")
        with open(os.path.join(v2, "General", "_attachments", "notes__0123abcd.md"), "w", encoding="utf-8") as fh:
            fh.write("attachment text\n")
        ok, rep2, _ = self.run_verify(vault=v2, out_name="out_c02a2")
        self.assertEqual(rep2["checks"]["C02"]["status"], "PASS", rep2["checks"]["C02"]["summary"])

    def test_c03_parent_id_and_structure(self):
        v = self.mutate_copy("v_c03")
        self.edit(v, "General/Home/Child One.md", f'notion_parent_id: "{P1}"', f'notion_parent_id: "{P3}"')
        rep = self.assert_fails(v, "C03")
        self.assertIn("notion_parent_id_mismatch", rep["checks"]["C03"]["details"]["issue_counts"])
        v2 = self.mutate_copy("v_c03b")  # leaf turned into a folder note
        os.makedirs(os.path.join(v2, "General", "Home", "Child One"))
        os.replace(os.path.join(v2, "General", "Home", "Child One.md"),
                   os.path.join(v2, "General", "Home", "Child One", "Child One.md"))
        rep2 = self.assert_fails(v2, "C03")
        self.assertIn("wrong_location", rep2["checks"]["C03"]["details"]["issue_counts"])

    def test_c03_base_missing_and_base_without_filters(self):
        v = self.mutate_copy("v_c03base")
        os.remove(os.path.join(v, "General", "Home", "Inline DB", "Inline DB.base"))
        rep = self.assert_fails(v, "C03")
        self.assertIn("base_file_missing", rep["checks"]["C03"]["details"]["issue_counts"])
        v2 = self.mutate_copy("v_c03base2")  # the db id only in a comment: shows the whole vault
        with open(os.path.join(v2, "General", "Tasks", "Tasks.base"), "w", encoding="utf-8") as fh:
            fh.write(f"# {D1}\nviews:\n  - type: table\n    name: Table\n")
        rep2 = self.assert_fails(v2, "C03")
        self.assertIn("base_filters_missing_or_invalid", rep2["checks"]["C03"]["details"]["issue_counts"])

    def test_c03_name_rules_v11(self):
        v = self.mutate_copy("v_c03name")  # title "Grand: Child" -> name must use the full-width colon
        os.replace(os.path.join(v, "General", "Home", "Child Two", "Grand： Child.md"),
                   os.path.join(v, "General", "Home", "Child Two", "Grand_ Child.md"))
        for rel in ("General/Home/Child Two/Child Two.md", "Notion_Obsidian/Godcreate 目录索引.md"):
            self.edit(v, rel, "Grand： Child|", "Grand_ Child|")
        self.edit(v, "Notion_Obsidian/sortspec.md", "Grand： Child.md", "Grand_ Child.md")
        rep = self.assert_fails(v, "C03")
        self.assertIn("name_not_derived_from_title", rep["checks"]["C03"]["details"]["issue_counts"])

    def test_c04_body_link_order(self):
        v = self.mutate_copy("v_c04")
        self.edit(v, "General/Home/Home.md", "[[General/Home/Child One|Child One]]\n\n> [!toggle]",
                  "PLACEHOLDER\n\n> [!toggle]")
        self.edit(v, "General/Home/Home.md", "![[General/Home/Inline DB/Inline DB.base]]",
                  "[[General/Home/Child One|Child One]]\n\n![[General/Home/Inline DB/Inline DB.base]]")
        self.edit(v, "General/Home/Home.md", "PLACEHOLDER\n", "")
        rep = self.assert_fails(v, "C04", also_pass=("C02", "C03", "C06"))
        self.assertTrue(rep["checks"]["C04"]["details"]["child_links_out_of_order"])

    def test_c04_missing_child_link(self):
        v = self.mutate_copy("v_c04m")
        self.edit(v, "General/Tasks/Tasks.md", "- [[General/Tasks/Row A|Row A]]\n", "")
        rep = self.assert_fails(v, "C04")
        self.assertEqual(len(rep["checks"]["C04"]["details"]["child_link_missing"]), 1)

    def test_c04_child_link_only_inside_code_or_comment_is_missing(self):
        for name, wrap in (("v_c04code", "```\n{}\n```"), ("v_c04com", "%% {} %%")):
            v = self.mutate_copy(name)
            self.edit(v, "General/Tasks/Tasks.md", "- [[General/Tasks/Row A|Row A]]\n",
                      wrap.format("[[General/Tasks/Row A|Row A]]") + "\n")
            rep = self.assert_fails(v, "C04")
            self.assertEqual(len(rep["checks"]["C04"]["details"]["child_link_missing"]), 1, name)

    def test_c04_multi_data_source_row_order(self):
        v = self.mutate_copy("v_c04ds")  # rows in rowid order instead of the database's data-source order
        self.edit(v, "General/Home/Inline DB/Row D.md", "notion_order: 1", "notion_order: 2")
        self.edit(v, "General/Home/Inline DB/Row C.md", "notion_order: 2", "notion_order: 1")
        rep = self.assert_fails(v, "C04")
        self.assertEqual(len(rep["checks"]["C04"]["details"]["notion_order_mismatch"]), 2)

    def test_c06_links_in_code_ignored_and_ambiguous_detected(self):
        v = self.mutate_copy("v_c06")
        with open(os.path.join(v, "Notion_Obsidian", "Godcreate 目录索引.md"), "a", encoding="utf-8") as fh:
            fh.write("- [[Teamspace Home]]\n")  # two files share this basename
        rep = self.assert_fails(v, "C06")
        d = rep["checks"]["C06"]["details"]
        self.assertEqual(len(d["ambiguous"]), 1)
        self.assertEqual(d["unresolved"], [])

    def test_c06_notion_url_warning_ignores_wikilink_alias(self):
        v = self.mutate_copy("v_c06url")
        url = "https://app.notion.com/p/Child-One-" + P2.replace("-", "")
        with open(os.path.join(v, "Quantitative Investing", "Server.md"), "a", encoding="utf-8") as fh:
            fh.write(f"\n[[General/Home/Child One|{url}]]\n\nplain {url}\n")
        ok, rep, _ = self.run_verify(vault=v, out_name="out_c06url")
        hits = rep["checks"]["C06"]["details"]["in_scope_notion_urls_not_rewritten(warning)"]
        self.assertEqual(len(hits), 1)  # only the bare URL, not the rewritten link's display text
        self.assertEqual(rep["checks"]["C06"]["status"], "PASS")

    def test_c07_sha_mismatch(self):
        v = self.mutate_copy("v_c07")
        bad = "General/_attachments/pic__deadbeef.png"
        os.replace(os.path.join(v, *ATT_REL.split("/")), os.path.join(v, *bad.split("/")))
        self.edit(v, "General/Home/Home.md", ATT_REL, bad)
        rep = self.assert_fails(v, "C07", also_pass=("C06",))
        self.assertEqual(len(rep["checks"]["C07"]["details"]["sha8_mismatch"]), 1)

    def test_c07_unreferenced_snapshot_file(self):
        v = self.mutate_copy("v_c07u")
        self.edit(v, "General/Home/Home.md", f"![[{ATT_REL}]]\n", "")
        rep = self.assert_fails(v, "C07")
        nr = rep["checks"]["C07"]["details"]["snapshot_files_not_referenced"]
        self.assertEqual(nr[0]["reason"], "file_present_but_not_referenced")

    def test_c08_missing_field_and_bad_url(self):
        v = self.mutate_copy("v_c08")
        self.edit(v, "General/Home/Child One.md", f'notion_created_time: "{CT}"\n', "")
        self.edit(v, "Quantitative Investing/Server.md", "https://app.notion.com/p/", "https://notion.so/p/")
        rep = self.assert_fails(v, "C08")
        cnt = rep["checks"]["C08"]["details"]["issue_counts"]
        self.assertIn("missing_field", cnt)
        self.assertIn("bad_notion_url", cnt)

    def test_c08_yaml_error_and_unquoted_time(self):
        v = self.mutate_copy("v_c08y")
        self.edit(v, "x_to_notion/Teamspace Home.md", 'notion_title: "Teamspace Home"', 'notion_title: "unterminated')
        self.edit(v, "General/Home/Child One.md", f'notion_created_time: "{CT}"', "notion_created_time: 2026-01-01T00:00:00Z")
        rep = self.assert_fails(v, "C08")
        cnt = rep["checks"]["C08"]["details"]["issue_counts"]
        self.assertIn("yaml_error", cnt)
        if vv._yaml is not None:
            self.assertIn("time_not_quoted_string", cnt)

    def test_c08_trailing_newline_and_alias(self):
        v = self.mutate_copy("v_c08n")
        self.edit(v, "General/Home/Child Two/Grand： Child.md", '  - "Grand: Child"\n', "")
        self.edit(v, "General/Home/Child Two/Grand： Child.md", "aliases:\n", "")
        p = os.path.join(v, "x_to_notion", "Teamspace Home.md")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("\n")
        rep = self.assert_fails(v, "C08")
        cnt = rep["checks"]["C08"]["details"]["issue_counts"]
        self.assertIn("aliases_missing_original_title", cnt)
        self.assertIn("not_single_trailing_newline", cnt)

    def test_c09_forbidden_and_collision(self):
        v = self.mutate_copy("v_c09")
        shutil.copy(os.path.join(v, "General", "Home", "Child One.md"),
                    os.path.join(v, "General", "Home", "bad:name.md"))
        shutil.copy(os.path.join(v, "Quantitative Investing", "Server.md"),
                    os.path.join(v, "Quantitative Investing", "server.md"))
        rep = self.assert_fails(v, "C09")
        d = rep["checks"]["C09"]["details"]
        self.assertTrue(any(x["issue"] == "forbidden_char" for x in d["name_issues"]))
        self.assertEqual(len(d["nfc_casefold_collisions"]), 1)

    def test_c09_v11_name_rules_nfd_percent_dots(self):
        v = self.mutate_copy("v_c09b")
        d = os.path.join(v, "x_to_notion")
        for n in (unicodedata.normalize("NFD", "café.png"), "50% off.png", "wait....png"):
            with open(os.path.join(d, n), "wb") as fh:
                fh.write(b"x")
        rep = self.assert_fails(v, "C09")
        kinds = {x["issue"] for x in rep["checks"]["C09"]["details"]["name_issues"]}
        self.assertTrue({"not_nfc", "percent_char", "three_dots"} <= kinds, kinds)

    def test_c10_missing_text(self):
        v = self.mutate_copy("v_c10")
        self.edit(v, "General/Home/Child Two/Grand： Child.md", "Grandchild text\n", "")
        rep = self.assert_fails(v, "C10", also_pass=("C02", "C03", "C04", "C06"))
        low = rep["checks"]["C10"]["details"]["pages_with_missing_blocks"]
        self.assertEqual([x["id"] for x in low], [P4])

    def test_c10_accepts_strong_tags_fullwidth_pipe_and_link_mention_title(self):
        # bold rendered as <strong> (CJK punctuation), a page mention whose title contains '｜', and a
        # link_mention shown with its title instead of the URL: all visible-text equal -> C10 PASS
        lm = {"type": "mention", "plain_text": "https://ta-lib.org/", "href": "https://ta-lib.org/",
              "mention": {"type": "link_mention", "link_mention": {"href": "https://ta-lib.org/",
                                                                   "title": "TA-Lib Library"}}}
        snap = self.snap2("s_c10", extra_blocks=[
            (uid(120), Q1, Q1, 2, "paragraph", {"rich_text": [rt("决策：", bold=True), rt("本地只保留 LightGBM")]}),
            (uid(121), Q1, Q1, 3, "paragraph", {"rich_text": [rt("访问 "), lm, rt(" 的 Github 页面")]}),
        ])
        v = self.mutate_copy("v_c10ok")
        with open(os.path.join(v, "Quantitative Investing", "Server.md"), "a", encoding="utf-8") as fh:
            fh.write("\n<strong>决策：</strong> 本地只保留 LightGBM\n\n访问 [TA-Lib Library](https://ta-lib.org/) 的 Github 页面\n")
        ok, rep, _ = self.run_verify(vault=v, out_name="out_c10ok", snap=snap)
        self.assertEqual(rep["checks"]["C10"]["status"], "PASS",
                         rep["checks"]["C10"]["details"]["pages_with_missing_blocks"])
        # ...while the same snapshot against the unchanged vault (text really missing) still fails
        ok2, rep2, _ = self.run_verify(out_name="out_c10bad", snap=snap)
        self.assertEqual(rep2["checks"]["C10"]["status"], "FAIL")

    def test_c11_is_warn_not_fail_and_values_never_printed(self):
        v = self.mutate_copy("v_c11")
        token = "ntn_" + "Ab3" * 15
        with open(os.path.join(v, "General", "Home", "Child One.md"), "a", encoding="utf-8") as fh:
            fh.write(f"leaked {token}\n")
        with open(os.path.join(v, ".obsidian", "plugins", "custom-sort", "data.json"), "w") as fh:
            fh.write("-----BEGIN OPENSSH PRIVATE KEY-----\n")
        ok, rep, _ = self.run_verify(vault=v, out_name="out_v_c11")
        self.assertEqual(rep["checks"]["C11"]["status"], "WARN")
        self.assertEqual(rep["status_by_check"]["C11"], "WARN")
        self.assertEqual(rep["overall"], "PASS")
        self.assertTrue(ok)
        self.assertEqual(rep["checks_warn"], ["C11"])
        hits = rep["checks"]["C11"]["details"]["hits(values_never_printed)"]
        self.assertEqual(sorted(h["kind"] for h in hits), ["notion_token_like", "private_key_block"])
        self.assertTrue(all(set(h) == {"path", "line", "kind", "length"} for h in hits))
        out = os.path.join(self.tmp, "out_v_c11")
        for f in ("verify.json", "verify.md"):
            with open(os.path.join(out, f), encoding="utf-8") as fh:
                self.assertNotIn(token, fh.read())
        # C11 WARN does not mask a real failure elsewhere
        os.remove(os.path.join(v, "Quantitative Investing", "Server.md"))
        ok2, rep2, _ = self.run_verify(vault=v, out_name="out_v_c11b")
        self.assertFalse(ok2)
        self.assertEqual(rep2["overall"], "FAIL")


class TestSortspec(Fixture):
    """C05: custom-sort 3.2.0 syntax and per-folder order (docs/custom-sort.md)."""

    def sortspec_variant(self, name, old, new):
        v = self.mutate_copy(name)
        self.edit(v, "Notion_Obsidian/sortspec.md", old, new)
        return v

    def c05_issues(self, rep):
        return [p["issue"] for p in rep["checks"]["C05"]["details"]["problems"]]

    def test_parser_positive_v11_syntax(self):
        spec = ("// comment line\ntarget-folder: /\n/folders General\n%\n\n"
                "target-folder: General\n/folders Home\n/:files. Description 草稿.md\n/:files. ASCII 速查.md\n"
                "%\n/folders _attachments\n\n"
                "target-folder: General/Home\n/:files. Home.md\n/:files. Home.base\n/folders Child\n%\n")
        secs = vv.parse_sorting_spec(spec)
        self.assertEqual([s["targets"] for s in secs], [[""], ["General"], ["General/Home"]])
        self.assertEqual([s["errors"] for s in secs], [[], [], []])
        g = secs[1]["groups"]
        self.assertEqual([(x["kind"], x["mtype"], x.get("name")) for x in g],
                         [("exact", "folders", "Home"), ("exact", "files_ext", "Description 草稿.md"),
                          ("exact", "files_ext", "ASCII 速查.md"), ("outsiders", "both", None),
                          ("exact", "folders", "_attachments")])
        # prefixed names starting with asc/desc are accepted by the plugin (only noted)
        self.assertEqual(len(secs[1]["notes"]), 2)
        # matching: /:files. compares the full name, /folders only folders, strict code points
        assign = vv.cs_assign(secs[2], [("Home.md", False), ("Home.base", False), ("Child", True),
                                        ("Home", True), ("x.md", False)])
        self.assertEqual([assign[("Home.md", False)], assign[("Home.base", False)], assign[("Child", True)]],
                         [0, 1, 2])
        self.assertEqual(assign[("x.md", False)], 3)  # outsiders
        self.assertEqual(assign[("Home", True)], 3)  # a folder is never matched by /:files.

    def test_parser_fatal_lines(self):
        cases = {
            "bare asc/desc name": "target-folder: X\nDescription 草稿\n",
            "bare ASCII name": "target-folder: X\nascii table\n",
            "two '...' in one line": "target-folder: X\n/:files. a...b...c.md\n",
            "name starts with %": "target-folder: X\n/folders %done\n",
            "'% ' second type prefix": "target-folder: X\n/:files. % off.md\n",
            "indented target-folder": "target-folder: X\n/folders a\n  target-folder: Y\n/folders b\n",
            "target-folder without space": "target-folder:X\n/folders a\n",
            "target-folder without value": "target-folder: \n/folders a\n",
        }
        for label, spec in cases.items():
            errs = [e for s in vv.parse_sorting_spec(spec) for e in s["errors"]]
            self.assertTrue(errs, label)

    def test_c05_fixture_sections_and_folder_notes(self):
        ok, rep, _ = self.run_verify(out_name="out_c05ok")
        d = rep["checks"]["C05"]
        self.assertEqual(d["status"], "PASS", d["summary"])
        self.assertEqual(d["details"]["object_folders_without_section"], 0)

    def test_c05_teamspace_section_order(self):
        v = self.sortspec_variant("v_c05", "  /folders Tasks\n  /folders Home\n", "  /folders Home\n  /folders Tasks\n")
        rep = self.assert_fails(v, "C05")
        self.assertIn("order", self.c05_issues(rep))

    def test_c05_base_before_folder_note_is_order_error(self):
        v = self.sortspec_variant("v_c05b", "  /:files. Tasks.md\n  /:files. Tasks.base\n",
                                  "  /:files. Tasks.base\n  /:files. Tasks.md\n")
        rep = self.assert_fails(v, "C05")
        self.assertIn("order", self.c05_issues(rep))

    def test_c05_untyped_name_does_not_separate_note_and_base(self):
        # bare "Tasks" matches Tasks.md AND Tasks.base (basename): one group, order undefined
        v = self.sortspec_variant("v_c05c", "  /:files. Tasks.md\n  /:files. Tasks.base\n", "  Tasks\n")
        rep = self.assert_fails(v, "C05")
        self.assertIn("order", self.c05_issues(rep))

    def test_c05_fatal_duplicate_target_folder(self):
        v = self.sortspec_variant("v_c05dup", "  target-folder: General/Home/Child Two\n",
                                  "  target-folder: General/Home\n  /:files. Home.md\n  %\n\n"
                                  "  target-folder: General/Home/Child Two\n")
        rep = self.assert_fails(v, "C05")
        self.assertTrue(any(p.get("error") == "duplicate target-folder"
                            for p in rep["checks"]["C05"]["details"]["problems"]))

    def test_c05_fatal_bare_desc_line(self):
        v = self.sortspec_variant("v_c05desc", "  /:files. Row A.md\n", "  Description of Row A\n  /:files. Row A.md\n")
        rep = self.assert_fails(v, "C05")
        self.assertIn("spec_error_disables_plugin", self.c05_issues(rep))

    def test_c05_fatal_two_wildcards(self):
        v = self.sortspec_variant("v_c05dots", "  /:files. Row A.md\n", "  /:files. Row...A....md\n  /:files. Row A.md\n")
        rep = self.assert_fails(v, "C05")
        self.assertIn("spec_error_disables_plugin", self.c05_issues(rep))

    def test_c05_fatal_name_starting_with_percent(self):
        v = self.sortspec_variant("v_c05pct", "  /:files. Row A.md\n", "  /:files. %Row A.md\n  /:files. Row A.md\n")
        rep = self.assert_fails(v, "C05")
        self.assertIn("name_starts_with_percent", self.c05_issues(rep))

    def test_c05_object_folder_without_section(self):
        v = self.sortspec_variant("v_c05miss", "  target-folder: General/Tasks/Row B\n  /:files. Row B.md\n"
                                               "  /:files. Sub Page.md\n  %\n", "")
        rep = self.assert_fails(v, "C05")
        self.assertIn("section_missing", self.c05_issues(rep))
        self.assertEqual(rep["checks"]["C05"]["details"]["object_folders_without_section"], 1)

    def test_c05_target_with_leading_slash_or_nfd_name_does_not_match(self):
        v = self.sortspec_variant("v_c05slash", "  target-folder: General/Home/Child Two\n",
                                  "  target-folder: /General/Home/Child Two\n")
        rep = self.assert_fails(v, "C05")
        self.assertIn("section_missing", self.c05_issues(rep))
        # the plugin compares names with ===: an NFD line never matches the NFC file name on disk
        sec = vv.parse_sorting_spec("target-folder: X\n/:files. " + unicodedata.normalize("NFD", "café") + ".md\n%\n")[0]
        self.assertEqual(vv.cs_assign(sec, [("café.md", False)])[("café.md", False)], 1)  # outsiders

    def test_c05_attachments_must_follow_outsiders(self):
        v = self.sortspec_variant("v_c05att", "  /folders Home\n  %\n  /folders _attachments\n",
                                  "  /folders Home\n  /folders _attachments\n  %\n")
        self.run_verify(vault=v, out_name="out_v_c05att")
        with open(os.path.join(v, "General", "Extra.md"), "w", encoding="utf-8") as fh:
            fh.write(nfm(uid(778), "page", "Extra", None, "General", 3))
        rep = self.assert_fails(v, "C05")
        self.assertIn("_attachments_not_last", self.c05_issues(rep))

    def test_c05_sorting_spec_must_be_string(self):
        v = self.mutate_copy("v_c05list")
        with open(os.path.join(v, "Notion_Obsidian", "sortspec.md"), "w", encoding="utf-8") as fh:
            fh.write('---\nsorting-spec:\n  - "target-folder: /"\n  - "/folders General"\n---\n')
        rep = self.assert_fails(v, "C05")
        self.assertEqual(rep["checks"]["C05"]["details"]["error"], "sorting_spec_not_string")

    def test_c05_sorting_spec_in_scanned_folder_note(self):
        v = self.mutate_copy("v_c05foreign")
        self.edit(v, "General/Tasks/Row B/Row B.md", 'Status: "Done"\n', 'Status: "Done"\nsorting-spec: "x"\n')
        rep = self.assert_fails(v, "C05")
        self.assertIn("foreign_sorting_spec_parsed_by_plugin", self.c05_issues(rep))


class TestTemplates(Fixture):
    """SPEC v1.2 section 13: database page templates."""

    def issues(self, rep, check, key="issues"):
        return [x["issue"] for x in rep["checks"][check]["details"][key]]

    def test_expectation_templates(self):
        exp = vv.Expectation(self.snap, self.sidebar, self.roots)
        d1 = exp.objs[D1]
        self.assertEqual(d1.children, [R1, R2])  # templates are not rows
        self.assertEqual(d1.templates, [TW, TB])  # `_template_ids` order, not snapshot rowid order
        self.assertEqual([(exp.objs[t].kind, exp.objs[t].order, exp.objs[t].parent, exp.objs[t].teamspace)
                          for t in d1.templates], [("template", 1, D1, "General"), ("template", 2, D1, "General")])
        self.assertEqual((exp.objs[AG].kind, exp.objs[AG].parent, exp.objs[AG].order), ("page", TW, 1))
        self.assertTrue(exp.objs[TW].folder_expected)
        self.assertFalse(exp.objs[TB].folder_expected)
        self.assertEqual(exp.objs[D2].templates, [])  # DS2 has no `_template_ids` key, DS3 an empty list
        self.assertEqual(exp.orphans, [])
        self.assertEqual(exp.summary()["snapshot_data_sources_with_template_ids"], 2)

    def test_fixture_counts_templates_in_c02(self):
        ok, rep, _ = self.run_verify(out_name="out_tmplc02")
        self.assertTrue(ok)
        d = rep["checks"]["C02"]["details"]
        self.assertEqual(d["expected_by_kind"]["template"], 2)
        self.assertEqual(d["present_by_kind"]["template"], 2)
        self.assertIn("templates_expected=2 templates_present=2", rep["checks"]["C02"]["summary"])
        self.assertEqual(rep["checks"]["C05"]["details"]["templates_sections_checked"], 1)

    def test_c02_missing_template_note(self):
        v = self.mutate_copy("v_tmpl_del")
        os.remove(os.path.join(v, "General", "Tasks", "_templates", "Bug Report.md"))
        rep = self.assert_fails(v, "C02")
        d = rep["checks"]["C02"]["details"]
        self.assertEqual([(m["id"], m["kind"]) for m in d["missing"]], [(TB, "template")])
        self.assertEqual(d["missing_by_kind"], {"template": 1})

    def test_template_ids_absent_means_no_templates(self):
        # no `_template_ids` and no template pages: the database has no templates, so the vault's
        # _templates folder and its notes are extra
        snap = self.snap2("s_notmpl", ds_templates={DS1: None}, template_objs=False)
        exp = vv.Expectation(snap, self.sidebar, self.roots)
        self.assertEqual(exp.objs[D1].templates, [])
        self.assertNotIn("template", exp.summary()["expected_by_kind"])
        ok, rep, _ = self.run_verify(out_name="out_notmpl", snap=snap)
        self.assertEqual(rep["checks"]["C02"]["status"], "FAIL")
        self.assertEqual(sorted(x["id"] for x in rep["checks"]["C02"]["details"]["extra"]), sorted([TW, TB, AG]))
        self.assertEqual(rep["checks"]["C03"]["status"], "FAIL")
        tf = [x for x in rep["checks"]["C03"]["details"]["issues"] if x["issue"] == "templates_folder_without_templates"]
        self.assertEqual([x["path"] for x in tf], ["General/Tasks/_templates"])

    def test_row_fallback_without_row_ids_excludes_templates(self):
        # DS1 lost `_row_ids`: rows come from pages whose API parent is DS1, but templates share that
        # parent type and must stay templates (not rows)
        snap = self.snap2("s_norowids")
        con = sqlite3.connect(snap)
        con.execute("update objects set data = json_remove(data, '$._row_ids') where id = ?", (DS1,))
        con.commit()
        con.close()
        exp = vv.Expectation(snap, self.sidebar, self.roots)
        self.assertEqual(exp.objs[D1].children, [R1, R2])
        self.assertTrue(exp.objs[D1].order_unknown)
        self.assertEqual(exp.objs[D1].templates, [TW, TB])
        self.assertEqual(exp.objs[TW].kind, "template")

    def test_template_pages_not_listed_are_orphans(self):
        # template pages in the snapshot but `_template_ids` missing: never silently accepted
        snap = self.snap2("s_tmplorph", ds_templates={DS1: None})
        exp = vv.Expectation(snap, self.sidebar, self.roots)
        self.assertEqual(sorted(o["id"] for o in exp.orphans), sorted([TW, TB, AG]))
        ok, rep, _ = self.run_verify(out_name="out_tmplorph", snap=snap)
        self.assertEqual(rep["checks"]["C02"]["status"], "FAIL")

    def test_c03_templates_folder_in_database_without_templates(self):
        v = self.mutate_copy("v_tmpl_emptyfolder")
        os.makedirs(os.path.join(v, "General", "Home", "Inline DB", "_templates"))
        rep = self.assert_fails(v, "C03", also_pass=("C02", "C05", "C09"))
        tf = [x for x in rep["checks"]["C03"]["details"]["issues"] if x["issue"] == "templates_folder_without_templates"]
        self.assertEqual([(x["id"], x["path"]) for x in tf], [(D2, "General/Home/Inline DB/_templates")])

    def test_c03_template_location_kind_parent(self):
        v = self.mutate_copy("v_tmpl_loc")  # template written next to the rows instead of _templates/
        os.replace(os.path.join(v, "General", "Tasks", "_templates", "Bug Report.md"),
                   os.path.join(v, "General", "Tasks", "Bug Report.md"))
        rep = self.assert_fails(v, "C03")
        wrong = [x for x in rep["checks"]["C03"]["details"]["issues"] if x["issue"] == "wrong_location"]
        self.assertEqual([(x["id"], x["expected"]) for x in wrong], [(TB, "General/Tasks/_templates/<name>.md")])
        v2 = self.mutate_copy("v_tmpl_kind")
        rel = "General/Tasks/_templates/Bug Report.md"
        self.edit(v2, rel, 'notion_kind: "template"', 'notion_kind: "row"')
        self.edit(v2, rel, f'notion_parent_id: "{D1}"', f'notion_parent_id: "{DS1}"')
        rep2 = self.assert_fails(v2, "C03")
        cnt = rep2["checks"]["C03"]["details"]["issue_counts"]
        self.assertIn("notion_kind_mismatch", cnt)
        self.assertIn("notion_parent_id_mismatch", cnt)
        # a template folder note flattened to a leaf (it has a child page)
        v3 = self.mutate_copy("v_tmpl_flat")
        os.replace(os.path.join(v3, "General", "Tasks", "_templates", "Weekly Plan", "Weekly Plan.md"),
                   os.path.join(v3, "General", "Tasks", "_templates", "Weekly Plan.md"))
        rep3 = self.assert_fails(v3, "C03")
        wrong3 = [x for x in rep3["checks"]["C03"]["details"]["issues"] if x["issue"] == "wrong_location"]
        self.assertIn(TW, [x["id"] for x in wrong3])

    def test_c08_template_kind_is_valid(self):
        ok, rep, _ = self.run_verify(out_name="out_tmplc08")
        self.assertEqual(rep["checks"]["C08"]["status"], "PASS")
        v = self.mutate_copy("v_tmpl_badkind")
        self.edit(v, "General/Tasks/_templates/Bug Report.md", 'notion_kind: "template"', 'notion_kind: "templat"')
        rep2 = self.assert_fails(v, "C08")
        self.assertIn("bad_notion_kind", rep2["checks"]["C08"]["details"]["issue_counts"])

    def test_c04_template_notion_order(self):
        v = self.mutate_copy("v_tmpl_order")
        self.edit(v, "General/Tasks/_templates/Weekly Plan/Weekly Plan.md", "notion_order: 1", "notion_order: 2")
        self.edit(v, "General/Tasks/_templates/Bug Report.md", "notion_order: 2", "notion_order: 1")
        rep = self.assert_fails(v, "C04", also_pass=("C02", "C03", "C05"))
        mism = rep["checks"]["C04"]["details"]["notion_order_mismatch"]
        self.assertEqual(sorted((m["id"], m["kind"], m["expected"]) for m in mism),
                         sorted([(TW, "template", 1), (TB, "template", 2)]))

    def test_c04_template_section(self):
        links = ("- [[General/Tasks/_templates/Weekly Plan/Weekly Plan|Weekly Plan]]\n",
                 "- [[General/Tasks/_templates/Bug Report|Bug Report]]\n")
        v = self.mutate_copy("v_tmpl_sec_order")
        self.edit(v, "General/Tasks/Tasks.md", links[0] + links[1], links[1] + links[0])
        rep = self.assert_fails(v, "C04", also_pass=("C02", "C03", "C05", "C06"))
        self.assertEqual(self.issues(rep, "C04", "template_section_issues"), ["template_links_out_of_order"])
        v2 = self.mutate_copy("v_tmpl_sec_missing")
        self.edit(v2, "General/Tasks/Tasks.md", "## 模板\n", "## Other\n")
        rep2 = self.assert_fails(v2, "C04")
        self.assertEqual(self.issues(rep2, "C04", "template_section_issues"), ["template_section_missing"])
        v3 = self.mutate_copy("v_tmpl_sec_link")
        self.edit(v3, "General/Tasks/Tasks.md", links[1], "")
        rep3 = self.assert_fails(v3, "C04", also_pass=("C06",))
        self.assertEqual(self.issues(rep3, "C04", "template_section_issues"), ["template_link_missing_in_section"])
        # a template link placed above the section (e.g. among the rows) does not count
        v4 = self.mutate_copy("v_tmpl_sec_outside")
        self.edit(v4, "General/Tasks/Tasks.md", links[1], "")
        self.edit(v4, "General/Tasks/Tasks.md", "\n![[General/Tasks/Tasks.base]]", links[1] + "\n![[General/Tasks/Tasks.base]]")
        rep4 = self.assert_fails(v4, "C04")
        self.assertEqual(self.issues(rep4, "C04", "template_section_issues"), ["template_link_missing_in_section"])

    def test_c05_templates_folder_and_section(self):
        ss = "Notion_Obsidian/sortspec.md"
        cases = [
            ("v_tmpl_ss_before", "  /folders Row B\n  %\n  /folders _templates\n",
             "  /folders Row B\n  /folders _templates\n  %\n", "templates_folder_not_after_outsiders"),
            ("v_tmpl_ss_unlisted", "  %\n  /folders _templates\n", "  %\n", "templates_folder_not_listed"),
            ("v_tmpl_ss_order", "  /folders Weekly Plan\n  /:files. Bug Report.md\n",
             "  /:files. Bug Report.md\n  /folders Weekly Plan\n", "order"),
            ("v_tmpl_ss_nosection", "  target-folder: General/Tasks/_templates\n  /folders Weekly Plan\n"
                                    "  /:files. Bug Report.md\n  %\n\n", "", "section_missing"),
            ("v_tmpl_ss_notlast", "  /:files. Row A.md\n  /folders Row B\n  %\n  /folders _templates\n",
             "  /folders Row B\n  %\n  /folders _templates\n  /:files. Row A.md\n", "templates_folder_not_last"),
        ]
        for name, old, new, want in cases:
            v = self.mutate_copy(name)
            self.edit(v, ss, old, new)
            rep = self.assert_fails(v, "C05")
            probs = rep["checks"]["C05"]["details"]["problems"]
            self.assertIn(want, [p["issue"] for p in probs], (name, probs))
            if want == "section_missing":
                self.assertIn("General/Tasks/_templates", [p["folder"] for p in probs])

    def test_base_filter_must_keep_templates_out(self):
        v = self.mutate_copy("v_tmpl_base")
        self.edit(v, "General/Tasks/Tasks.base", "    - 'notion_kind == \"row\"'\n", "")
        rep = self.assert_fails(v, "C03")
        self.assertIn("base_filter_missing_notion_kind_row", rep["checks"]["C03"]["details"]["issue_counts"])

    def test_c03_row_named__templates_is_reserved(self):
        snap = self.snap2("s_rowtmpl", extra_objs=[(R5, "page", D1, D1, 1, row_obj(R5, "_templates", DS1, D1, "Todo"))],
                          ds_rows={DS1: [R1, R2, R5]})
        v = self.mutate_copy("v_rowtmpl")
        with open(os.path.join(v, "General", "Tasks", "_templates.md"), "w", encoding="utf-8") as fh:
            fh.write(nfm(R5, "row", "_templates", D1, "General", 3, Status="Todo"))
        ok, rep, _ = self.run_verify(vault=v, out_name="out_rowtmpl", snap=snap)
        self.assertEqual(rep["checks"]["C03"]["status"], "FAIL")
        res = [x for x in rep["checks"]["C03"]["details"]["issues"] if x["issue"] == "reserved_name__templates_used_by_row"]
        self.assertEqual([x["id"] for x in res], [R5])
        v2 = self.mutate_copy("v_rowtmpl2")  # the deduplicated name "_templates (<id8>)" is conformant
        with open(os.path.join(v2, "General", "Tasks", f"_templates ({R5.replace('-', '')[:8]}).md"), "w",
                  encoding="utf-8") as fh:
            fh.write(nfm(R5, "row", "_templates", D1, "General", 3, Status="Todo", aliases=["_templates"]))
        ok2, rep2, _ = self.run_verify(vault=v2, out_name="out_rowtmpl2", snap=snap)
        self.assertEqual(rep2["checks"]["C03"]["status"], "PASS", rep2["checks"]["C03"]["details"]["issues"])


class TestBaseFormats(Fixture):
    """SPEC v1.2 section 13: C03 accepts the canonical .base layout and the legacy one (semantic compare)."""

    def test_vault_with_legacy_and_canonical_base_passes_both_backends(self):
        v = self.mutate_copy("v_base_layouts")
        with open(os.path.join(v, "General", "Tasks", "Tasks.base"), "w", encoding="utf-8") as fh:
            fh.write(base_yaml_legacy("General/Tasks", D1))
        with open(os.path.join(v, "General", "Home", "Inline DB", "Inline DB.base"), "w", encoding="utf-8") as fh:
            fh.write(base_yaml_canonical("General/Home/Inline DB", D2))
        saved = vv._yaml
        try:
            for backend in (saved, None):
                vv._yaml = backend
                ok, rep, _ = self.run_verify(vault=v, out_name=f"out_base_{'py' if backend else 'mini'}")
                bad = {k: r["summary"] for k, r in rep["checks"].items() if r["status"] != "PASS"}
                self.assertEqual(bad, {}, backend)
                self.assertEqual(rep["checks"]["C03"]["details"]["base_layouts(info)"],
                                 {"canonical_v1.2": 1, "legacy": 1})
        finally:
            vv._yaml = saved

    def test_semantic_compare_and_issues(self):
        props = {"Name": "title", "Status": "select"}
        legacy, canon = base_yaml_legacy("General/Tasks", D1), base_yaml_canonical("General/Tasks", D1)
        saved = vv._yaml
        try:
            for backend in (saved, None):
                vv._yaml = backend
                self.assertTrue(vv.bases_equivalent(legacy, canon))
                self.assertFalse(vv.bases_equivalent(legacy, base_yaml_canonical("General/Tasks", D1, "DESC")))
                self.assertFalse(vv.bases_equivalent(legacy, canon.replace("      - Status\n", "")))
                self.assertEqual(vv.base_issues(legacy, "General/Tasks", D1, props), [])
                self.assertEqual(vv.base_issues(canon, "General/Tasks", D1, props), [])
                self.assertEqual(vv.base_issues(base_yaml_canonical("General/Tasks", D1, "DESC"), "General/Tasks", D1,
                                                props), ["base_view_not_sorted_by_notion_order_asc"])
                self.assertEqual(vv.base_issues(canon.replace("      - Status\n", ""), "General/Tasks", D1, props),
                                 ["base_view_order_missing_property"])
                for txt, want in ((legacy, "legacy"), (canon, "canonical_v1.2")):
                    self.assertEqual(vv.base_layout(txt, vv.load_yaml(txt)[0]), want)
        finally:
            vv._yaml = saved
        # legacy key `column` and note.-prefixed properties keys normalise too
        a = vv.base_normalize({"views": [{"type": "table", "sort": [{"column": "notion_order", "direction": "ASC"}],
                                          "order": ["file.name", "Status"]}], "properties": {"Status": {}}})
        b = vv.base_normalize({"views": [{"type": "table", "sort": [{"property": "note.notion_order", "direction": "ASC"}],
                                          "order": ["file.name", "note.Status"]}], "properties": {"note.Status": {}}})
        self.assertEqual(a, b)


class TestC10Strict(Fixture):
    """SPEC v1.2 section 13: C10 fails on ANY missing text block (no 99% threshold)."""

    SERVER = "Quantitative Investing/Server.md"

    def c10(self, rep):
        return rep["checks"]["C10"]["details"]

    def test_one_block_of_a_324_block_page_is_detected(self):
        v = self.mutate_copy("v_c10_big")
        self.edit(v, self.SERVER, SERVER_LINES[160] + "\n\n", "")
        rep = self.assert_fails(v, "C10", also_pass=("C02", "C03", "C04", "C05", "C06", "C07", "C08", "C09"))
        d = self.c10(rep)
        self.assertEqual(d["missing_blocks"], 1)
        pages = d["pages_with_missing_blocks"]
        self.assertEqual([(x["id"], x["blocks"], x["covered"]) for x in pages], [(Q1, 324, 323)])
        self.assertGreater(pages[0]["coverage"], 0.99)  # would have passed the v1.1 99% page threshold
        self.assertGreater(d["overall_coverage"], 0.99)
        self.assertEqual([(m["block"], m["reason"]) for m in pages[0]["missing_blocks"]],
                         [(uid(1000 + 161), "not_found")])

    def test_one_of_two_identical_paragraphs_deleted(self):
        v = self.mutate_copy("v_c10_dup")
        self.edit(v, self.SERVER, REPEATED + "\n\n", "")  # removes the first of the two copies
        rep = self.assert_fails(v, "C10")
        pages = self.c10(rep)["pages_with_missing_blocks"]
        self.assertEqual(len(pages), 1)
        miss = pages[0]["missing_blocks"]
        self.assertEqual(len(miss), 1)
        self.assertIn(miss[0]["block"], (uid(1100), uid(1201)))
        self.assertTrue(miss[0]["reason"].startswith("fewer_occurrences_than_blocks"), miss)

    def test_missing_note_counts_its_blocks(self):
        v = self.mutate_copy("v_c10_note")
        os.remove(os.path.join(v, "General", "Home", "Child One.md"))
        rep = self.assert_fails(v, "C10")
        d = self.c10(rep)
        self.assertEqual([(x["id"], x["blocks"]) for x in d["pages_note_missing"]], [(P2, 1)])
        self.assertEqual(d["missing_blocks"], 1)

    def test_c10_missing_unit(self):
        it = lambda b, k, alts=(): (b, "paragraph", k, k, alts)  # noqa: E731
        self.assertEqual(vv.c10_missing([it("a", "abc"), it("b", "abc")], "xabcyabc", ""), [])
        self.assertEqual([(m[0][0], m[1]) for m in vv.c10_missing([it("a", "abc"), it("b", "abc")], "xabcy", "")],
                         [("b", "fewer_occurrences_than_blocks(1<2)")])
        self.assertEqual([m[0][0] for m in vv.c10_missing([it("a", "abc"), it("c", "zzz")], "abc", "")], ["c"])
        # alternative mention display: one block shows the URL, its twin the page title
        self.assertEqual(vv.c10_missing([it("a", "seeurl", ("seetitle",)), it("b", "seeurl", ("seetitle",))],
                                        "seeurl..seetitle", ""), [])
        # greedy counting never under-counts disjoint copies, even with a self-overlapping key
        self.assertEqual(vv.c10_missing([it("a", "aXa"), it("b", "aXa")], "zaXaXaaXa", ""), [])


class TestHelpers(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(vv.sanitize("Grand: Child"), "Grand： Child")
        self.assertEqual(vv.sanitize('a/b\\c*d?e"f<g>h|i#j^k[l]m'), "a／b＼c＊d？e＂f＜g＞h｜i＃j＾k［l］m")
        self.assertEqual(vv.sanitize("  two   spaces\tand\nnewline.. "), "two spaces and newline")
        self.assertEqual(vv.sanitize(".hidden"), "．hidden")
        self.assertEqual(vv.sanitize(""), "Untitled")
        self.assertEqual(vv.sanitize("Godcreate AI工作空间"), "Godcreate AI工作空间")

    def test_sanitize_v11_step1(self):
        # NFC first, then '...' -> '…' and '%' -> '％'
        self.assertEqual(vv.sanitize(unicodedata.normalize("NFD", "café")), "café")
        self.assertEqual(vv.sanitize("Wait... what"), "Wait… what")
        self.assertEqual(vv.sanitize("50% off"), "50％ off")
        self.assertEqual(vv.sanitize("%start"), "％start")
        self.assertEqual(vv.sanitize("a......b"), "a……b")
        self.assertEqual(vv.sanitize("end...."), "end…")  # '…' + '.', trailing dot removed
        for t in ("x...y...z", "a....b", "%%", "....."):
            s = vv.sanitize(t)
            self.assertNotIn("...", s, t)
            self.assertNotIn("%", s, t)

    def test_name_matches(self):
        i = uid(5)
        self.assertTrue(vv.name_matches("Grand： Child", "Grand: Child", i))
        self.assertTrue(vv.name_matches("Home (00000005)", "Home", i))
        self.assertFalse(vv.name_matches("Home (12345678)", "Home", i))
        long_title = "长" * 60  # 180 bytes
        trunc = "长" * 39 + "…" + " (00000005)"
        self.assertTrue(vv.name_matches(trunc, long_title, i))
        self.assertFalse(vv.name_matches("长" * 39 + "…", long_title, i))  # id suffix mandatory
        self.assertFalse(vv.name_matches("长" * 3 + "…" + " (00000005)", long_title, i))  # far too short
        self.assertFalse(vv.name_matches("Other", "Home", i))
        self.assertFalse(vv.name_matches("50% off", "50% off", i))
        self.assertTrue(vv.name_matches("50％ off", "50% off", i))
        self.assertFalse(vv.name_matches("Wait...", "Wait...", i))
        self.assertTrue(vv.name_matches("Wait…", "Wait...", i))

    def test_canon_id(self):
        self.assertEqual(vv.canon_id("1FEF525759CF45F1BBF1729AD8BECEFF"), "1fef5257-59cf-45f1-bbf1-729ad8beceff")
        self.assertIsNone(vv.canon_id("nope"))

    def test_mini_yaml_matches_pyyaml(self):
        text = vault_files()["General/Home/Child Two/Grand： Child.md"].split("---\n")[1]
        text2 = SORTSPEC.split("---\n")[1]
        text3 = base_yaml("General/Home/Inline DB", D2)
        for t in (text, text2, text3):
            mini = vv.mini_yaml_load(t)
            if vv._yaml is not None:
                self.assertEqual(mini, vv._yaml.safe_load(t))
        self.assertEqual(vv.mini_yaml_load('a: "x\\"y"\nb: null\nc: 3\nd: [1, "two"]\n'),
                         {"a": 'x"y', "b": None, "c": 3, "d": [1, "two"]})
        with self.assertRaises(vv.MiniYAMLError):
            vv.mini_yaml_load('a: "unterminated\n')

    def test_resolver(self):
        r = vv.Resolver(["A/x.md", "B/x.md", "A/y.md", "A/sub/z.md", "A/img.png"])
        self.assertEqual(r.resolve("A/x", "A/y.md"), ("ok", "A/x.md"))
        self.assertEqual(r.resolve("x", "A/y.md")[0], "ambiguous")
        self.assertEqual(r.resolve("y", "B/x.md"), ("ok", "A/y.md"))
        self.assertEqual(r.resolve("sub/z", "B/x.md"), ("ok", "A/sub/z.md"))
        self.assertEqual(r.resolve("./sub/z", "A/y.md"), ("ok", "A/sub/z.md"))
        self.assertEqual(r.resolve("img.png", "B/x.md"), ("ok", "A/img.png"))
        self.assertEqual(r.resolve("a/Y", "B/x.md"), ("ok_ci", "A/y.md"))
        self.assertEqual(r.resolve("nothing", "B/x.md"), ("unresolved", None))

    def test_code_and_comment_masking(self):
        # a shorter fence (```) must not close a longer one (````); quoted fences, %%comments%% too
        body = ("a [[l1]]\n````\n[[in code]]\n```\n[[still code]]\n````\n`[[inline]]` [[l2]]\n> ```\n> [[q]]\n> ```\n"
                "%% [[c1]] %% [[l3]]\n%%\n[[c2]]\n%%\n")
        lines = vv.mask_code_lines(body)
        found = [inner for ln in lines for _e, inner, _s, _t in vv.find_wikilinks(ln)]
        self.assertEqual(found, ["l1", "l2", "l3"])

    def test_norm_text(self):
        self.assertEqual(vv.norm_text("a *b* \\[c\\] | d<br>e"), "abcde")
        self.assertEqual(vv.norm_text("<strong>x：</strong> y <em>z</em>"), "x：yz")
        self.assertEqual(vv.norm_text(vv.visible_text("[[p/q|00｜目录]]")), vv.norm_text("00｜目录"))
        self.assertEqual(vv.norm_text(vv.visible_text("x [[p/q|Alias]] [t](<http://u v>) ![[img.png]]")),
                         "xAliast")

    def test_overall_ignores_warn(self):
        self.assertEqual(vv.overall_status({"C01": {"status": "PASS"}, "C11": {"status": "WARN"}}), "PASS")
        self.assertEqual(vv.overall_status({"C01": {"status": "FAIL"}, "C11": {"status": "WARN"}}), "FAIL")


# ============================================================ WRITEBACK_SPEC v1.1 additions (verifier v1.3)
ALL_CHECKS = ("C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "C09", "C10")
CHILD_ONE = "General/Home/Child One.md"
SYNC_COPY = "General/Home/Child One (Conflicted copy 2026-11-18 0101).md"


class WBFixture(Fixture):
    def write_file(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return p

    def dev_file(self, obj, name="expected_deviations.json"):
        return self.write_file(name, json.dumps(obj, ensure_ascii=False))

    def ids_file(self, ids, name="ids.txt"):
        return self.write_file(name, "".join(i + "\n" for i in ids))

    def run_wb(self, vault=None, dev=None, ids=None, out_name=None):
        out = os.path.join(self.tmp, out_name or ("out_wb_" + os.path.basename(vault or self.vault)
                                                   + ("_dev" if dev else "") + ("_ids" if ids else "")))
        return vv.verify(self.snap, self.sidebar, vault or self.vault, out, self.roots,
                         expected_deviations=dev, ids=ids)

    def add_file(self, root, rel, text):
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def remove(self, root, rel):
        p = os.path.join(root, *rel.split("/"))
        if os.path.isdir(p):
            shutil.rmtree(p)
        else:
            os.remove(p)

    def assert_statuses(self, rep, fail=(), ctx=""):
        st = self.statuses(rep)
        want = {c: ("FAIL" if c in fail else "PASS") for c in ALL_CHECKS}
        got = {c: st[c] for c in ALL_CHECKS}
        self.assertEqual(got, want, (ctx, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS if got[c] != want[c]}))
        self.assertEqual(st["C11"], "PASS")

    def reg(self, rep, check):
        return rep["checks"][check]["registered_deviations"]


class TestExpectedDeviations(WBFixture):
    """--expected-deviations (WRITEBACK_SPEC 14.0): listed objects' issues -> INFO, unlisted ones still FAIL."""

    def test_default_run_has_no_new_report_keys(self):
        ok, rep, _ = self.run_verify()
        self.assertTrue(ok)
        self.assertNotIn("registered_deviations", rep)
        self.assertNotIn("scope", rep)
        self.assertNotIn("expected_deviations", rep["inputs"])
        for cid, r in rep["checks"].items():
            self.assertNotIn("registered_deviations", r, cid)
            self.assertNotIn("registered_deviations(info)", r["details"], cid)
            self.assertNotIn("registered_deviations", r["summary"], cid)
            self.assertNotIn("scope=--ids", r["summary"], cid)
        self.assertNotIn("generated_files", rep["checks"]["C08"]["summary"])
        self.assertEqual(rep["tool"]["version"], "1.3")

    def test_empty_file_keeps_every_status(self):
        ok, rep, _ = self.run_wb(dev=self.dev_file({}))
        self.assertTrue(ok)
        self.assert_statuses(rep)
        d = rep["registered_deviations"]
        self.assertEqual(d["total"], 0)
        self.assertEqual(set(d["by_check"]), set(ALL_CHECKS))
        self.assertEqual(d["unmatched_entries(info)"], [])
        for c in ALL_CHECKS:
            self.assertEqual(self.reg(rep, c), 0)
            self.assertIn("registered_deviations(info)=0", rep["checks"][c]["summary"])
        self.assertNotIn("registered_deviations", rep["checks"]["C11"])
        self.assertEqual(rep["inputs"]["expected_deviations"]["sha256"],
                         hashlib.sha256(b"{}").hexdigest())

    def test_sync_conflict_copy_registered(self):
        """speccheck R-120 acceptance: without the flag C02 FAIL, with the flag PASS + INFO."""
        v = self.mutate_copy("v_sync")
        shutil.copy2(os.path.join(v, *CHILD_ONE.split("/")), os.path.join(v, *SYNC_COPY.split("/")))
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C02", "C08"))
        self.assertEqual(rep0["checks"]["C02"]["details"]["duplicates"], [{"id": P2, "paths": sorted([CHILD_ONE, SYNC_COPY])}])
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"sync_conflict_copies": [SYNC_COPY]}))
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assert_statuses(rep)
        self.assertEqual(self.reg(rep, "C02"), 1)
        info = rep["checks"]["C02"]["details"]["registered_deviations(info)"]["duplicates"]
        self.assertEqual(info[0]["deviation"], "sync_conflict_copy")
        self.assertEqual(self.reg(rep, "C08"), 1)  # aliases_missing_original_title on the copy
        self.assertEqual(rep["registered_deviations"]["by_category"], {"sync_conflict_copy": 2})
        self.assertEqual(rep["registered_deviations"]["unmatched_entries(info)"], [])
        # object form with the right id works too; a WRONG id does not match
        ok2, _r, _ = self.run_wb(vault=v, dev=self.dev_file({"sync_conflict_copies": [{"path": SYNC_COPY,
                                                                                        "notion_id": P2}]}, "d2.json"),
                                 out_name="o2")
        self.assertTrue(ok2)
        _ok3, rep3, _ = self.run_wb(vault=v, dev=self.dev_file({"sync_conflict_copies": [{"path": SYNC_COPY,
                                                                                           "notion_id": P3}]}, "d3.json"),
                                    out_name="o3")
        self.assertEqual(rep3["checks"]["C02"]["status"], "FAIL")
        self.assertEqual(rep3["registered_deviations"]["rejected_entries"],
                         [{"path": SYNC_COPY, "notion_id": P3, "category": "sync_conflict_copy",
                           "reason": "listed_notion_id_differs_from_file"}])

    def test_unlisted_duplicate_still_fails(self):
        v = self.mutate_copy("v_sync_dup")
        src = os.path.join(v, *CHILD_ONE.split("/"))
        shutil.copy2(src, os.path.join(v, *SYNC_COPY.split("/")))
        shutil.copy2(src, os.path.join(v, "General", "Home", "Child One (neg-dup).md"))  # NOT listed
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"sync_conflict_copies": [SYNC_COPY]}))
        self.assertFalse(ok)
        self.assertEqual(rep["checks"]["C02"]["status"], "FAIL")
        self.assertEqual(len(rep["checks"]["C02"]["details"]["duplicates"]), 1)
        self.assertEqual(rep["checks"]["C08"]["status"], "FAIL")  # the unlisted copy's aliases issue
        c08 = rep["checks"]["C08"]
        self.assertEqual([i["path"] for i in c08["details"]["issues"]], ["General/Home/Child One (neg-dup).md"])
        self.assertEqual([i["path"] for i in c08["details"]["registered_deviations(info)"]["issues"]], [SYNC_COPY])

    def test_local_new_files_registered(self):
        v = self.mutate_copy("v_local_new")
        listed = {
            "General/Home/My Draft.md": "just my own text\n",                                  # C02 + C08
            "General/Home/50% done....md": "---\ntags: [x]\n---\nnote\n",                      # + C09 names
            "Inbox/idea.md": "an idea\n",                                                       # C01 folder
            "Pasted image 20261118.png": "not really a png\n",                                  # C01 file
        }
        for rel, text in listed.items():
            self.add_file(v, rel, text)
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C01", "C02", "C08", "C09"))
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"local_new": sorted(listed)}))
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assert_statuses(rep)
        self.assertEqual(self.reg(rep, "C01"), 2)
        self.assertEqual(self.reg(rep, "C02"), 3)  # three .md without notion_id
        self.assertGreaterEqual(self.reg(rep, "C08"), 3)
        self.assertGreaterEqual(self.reg(rep, "C09"), 2)  # percent_char, three_dots, trailing_dot
        self.assertEqual(set(rep["registered_deviations"]["by_category"]), {"local_new"})
        # negative: one more UNLISTED note, an unlisted file in the listed top-level folder, an unlisted root file
        self.add_file(v, "General/Home/Unlisted.md", "not registered\n")
        self.add_file(v, "Inbox/other.txt", "x\n")
        self.add_file(v, "Other.png", "x\n")
        ok2, rep2, _ = self.run_wb(vault=v, dev=self.dev_file({"local_new": sorted(listed)}), out_name="o_neg")
        self.assertFalse(ok2)
        self.assert_statuses(rep2, fail=("C01", "C02", "C08"))
        self.assertEqual(sorted(rep2["checks"]["C01"]["details"]["extra"]), ["Inbox", "Other.png"])
        self.assertEqual(rep2["checks"]["C02"]["details"]["md_without_notion_id"], ["General/Home/Unlisted.md"])
        self.assertEqual({i["path"] for i in rep2["checks"]["C08"]["details"]["issues"]}, {"General/Home/Unlisted.md"})
        self.assertEqual(self.reg(rep2, "C01"), 1)  # only the root image is still fully listed

    def test_hold_leaf_deleted_locally(self):
        """A held leaf (pending delete): C02 missing, C06 links to it, C10 missing note -> INFO."""
        v = self.mutate_copy("v_hold")
        self.remove(v, CHILD_ONE)
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C02", "C06", "C10"))
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [P2]}))
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual(self.reg(rep, "C02"), 1)
        self.assertEqual(self.reg(rep, "C06"), 3)  # Home.md x2 + index
        self.assertEqual(self.reg(rep, "C10"), 1)
        self.assertEqual(rep["registered_deviations"]["by_category"], {"hold": 5})
        # negative: a second, UNLISTED leaf deleted as well -> still FAIL for that one only
        self.remove(v, "General/Home/Child Two/Grand： Child.md")
        ok2, rep2, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [P2]}), out_name="o_neg")
        self.assertFalse(ok2)
        self.assert_statuses(rep2, fail=("C02", "C06", "C10"))
        self.assertEqual([m["id"] for m in rep2["checks"]["C02"]["details"]["missing"]], [P4])
        self.assertEqual({u["target"] for u in rep2["checks"]["C06"]["details"]["unresolved"]},
                         {"General/Home/Child Two/Grand： Child"})
        self.assertEqual([m["id"] for m in rep2["checks"]["C10"]["details"]["pages_note_missing"]], [P4])
        self.assertEqual(self.reg(rep2, "C02"), 1)

    def test_hold_link_match_is_folder_precise(self):
        """An unresolved link counts as 'to the held note' only if it names it in ITS folder (or a listed path)."""
        v = self.mutate_copy("v_hold_precise")
        self.remove(v, CHILD_ONE)
        self.edit(v, "Quantitative Investing/Server.md", "# Server notes\n",
                  "# Server notes\n\n[[General/Tasks/Child One]]\n\n[[Old Name]]\n")
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [P2]}))
        self.assertFalse(ok)
        self.assert_statuses(rep, fail=("C06",))
        self.assertEqual(sorted(u["target"] for u in rep["checks"]["C06"]["details"]["unresolved"]),
                         ["General/Tasks/Child One", "Old Name"])
        self.assertEqual(self.reg(rep, "C06"), 3)
        # an explicitly listed old path of the held note covers a link to that name
        dev = self.dev_file({"hold_ids": [{"notion_id": P2, "paths": ["General/Home/Old Name.md"]}]}, "d2.json")
        _ok2, rep2, _ = self.run_wb(vault=v, dev=dev, out_name="o2")
        self.assertEqual([u["target"] for u in rep2["checks"]["C06"]["details"]["unresolved"]], ["General/Tasks/Child One"])
        self.assertEqual(self.reg(rep2, "C06"), 4)

    def test_hold_covers_subtree_unless_disabled(self):
        v = self.mutate_copy("v_hold_tree")
        self.remove(v, "General/Home/Child Two")
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [P3]}))
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual(self.reg(rep, "C02"), 2)
        self.assertEqual(rep["registered_deviations"]["held_ids_with_subtree"], 2)
        ok2, rep2, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [{"notion_id": P3, "subtree": False}]},
                                                             "d2.json"), out_name="o2")
        self.assertFalse(ok2)
        self.assertEqual([m["id"] for m in rep2["checks"]["C02"]["details"]["missing"]], [P4])
        # paused is the same mechanism (auto_sync_hold), reported as its own category
        ok3, rep3, _ = self.run_wb(vault=v, dev=self.dev_file({"paused": [P3]}, "d3.json"), out_name="o3")
        self.assertTrue(ok3)
        self.assertEqual(set(rep3["registered_deviations"]["by_category"]), {"paused"})

    def test_hold_stale_note_present(self):
        """A held note the publisher did not REPLACE: stale frontmatter / text are INFO for that id only."""
        v = self.mutate_copy("v_hold_stale")
        self.edit(v, CHILD_ONE, f'notion_last_edited_time: "{LT}"', 'notion_last_edited_time: "2025-01-01T00:00:00.000Z"')
        self.edit(v, CHILD_ONE, "Some \\*special\\* text", "Old text")
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C08", "C10"))
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [P2]}))
        self.assertTrue(ok)
        self.assertEqual((self.reg(rep, "C08"), self.reg(rep, "C10")), (1, 1))
        # the same stale state on an UNLISTED id still fails
        ok2, rep2, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [P4]}, "d2.json"), out_name="o2")
        self.assertFalse(ok2)
        self.assert_statuses(rep2, fail=("C08", "C10"))
        self.assertEqual(len(rep2["registered_deviations"]["unmatched_entries(info)"]), 1)

    def test_local_modified_only_its_own_issue_types(self):
        v = self.mutate_copy("v_lm")
        self.edit(v, CHILD_ONE, "Some \\*special\\* text", "My local rewrite [[Nowhere Note]] of the")
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C06", "C10"))
        dev = self.dev_file({"local_modified": [{"notion_id": P2, "path": CHILD_ONE}]})
        ok, rep, _ = self.run_wb(vault=v, dev=dev)
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual((self.reg(rep, "C06"), self.reg(rep, "C10")), (1, 1))
        # a user edit to notion_parent_id (C03) of the same note is never a registered deviation
        self.edit(v, CHILD_ONE, f'notion_parent_id: "{P1}"', f'notion_parent_id: "{P3}"')
        ok2, rep2, _ = self.run_wb(vault=v, dev=dev, out_name="o2")
        self.assertFalse(ok2)
        self.assert_statuses(rep2, fail=("C03",))
        self.assertEqual([i["issue"] for i in rep2["checks"]["C03"]["details"]["issues"]], ["notion_parent_id_mismatch"])

    def test_plain_copy_listed_as_sync_copy_is_rejected(self):
        """Only an Obsidian Sync conflict NAME can excuse a duplicate. A same-basename copy (which also makes
        [[Child One]] ambiguous) listed as a sync copy is rejected: C02 and C06 keep failing."""
        v = self.mutate_copy("v_amb")
        other = "General/Tasks/Child One.md"
        shutil.copy2(os.path.join(v, *CHILD_ONE.split("/")), os.path.join(v, *other.split("/")))
        self.edit(v, "Quantitative Investing/Server.md", "# Server notes\n", "# Server notes\n\nsee [[Child One]]\n")
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C02", "C06"))
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"sync_conflict_copies": [{"path": other, "notion_id": P2}]}))
        self.assertFalse(ok)
        self.assert_statuses(rep, fail=("C02", "C06"))
        self.assertEqual(rep["registered_deviations"]["total"], 0)
        self.assertEqual(rep["registered_deviations"]["rejected_entries"],
                         [{"path": other, "notion_id": P2, "category": "sync_conflict_copy",
                           "reason": "name_is_not_a_sync_conflict_copy"}])
        self.assertEqual(rep["registered_deviations"]["unmatched_entries(info)"], [])

    def test_unmatched_and_ignored_entries_reported(self):
        dev = self.dev_file({"schema": "expected_deviations/1", "run_id": "r1",
                             "local_new": ["General/Nope.md", "Inbox/"], "hold_ids": [P4],
                             "name_overrides": [{"notion_id": P2, "old_rel": CHILD_ONE, "local_rel": "General/Home/My Child.md"}],
                             "sync_conflict_copies": ["General/Nope (Conflicted copy 2026-11-18 0101).md",
                                                      "General/Nope (Conflicted copy).md"], "future_key": [1]})
        ok, rep, _ = self.run_wb(dev=dev)
        self.assertTrue(ok)
        d = rep["registered_deviations"]
        self.assertEqual(d["total"], 0)
        self.assertEqual(sorted(e["category"] for e in d["unmatched_entries(info)"]),
                         ["hold", "local_new", "local_new", "name_override", "sync_conflict_copy"])
        self.assertIn({"path": "Inbox", "category": "local_new"}, d["unmatched_entries(info)"])  # trailing '/' accepted
        # SYNC_CONFLICT_RE needs " (Conflicted copy <something>).md": the second name is rejected, not unmatched
        self.assertEqual(d["rejected_entries"], [{"path": "General/Nope (Conflicted copy).md", "notion_id": None,
                                                  "category": "sync_conflict_copy",
                                                  "reason": "name_is_not_a_sync_conflict_copy"}])
        self.assertEqual(d["ignored_keys"], ["future_key"])
        with open(os.path.join(self.tmp, "out_wb_vault_dev", "verify.md"), encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("已登记的本地差异", md)
        self.assertIn("命名差异", md)
        self.assertIn("Rejected entries (not used: the issues they name keep failing): 1", md)

    def test_malformed_files_are_input_errors(self):
        bad = [("notjson.json", "{oops"), ("list.json", "[]"), ("str.json", json.dumps({"local_new": "x.md"})),
               ("abs.json", json.dumps({"local_new": ["/etc/passwd"]})),
               ("dotdot.json", json.dumps({"local_new": ["General/../x.md"]})),
               ("badid.json", json.dumps({"hold_ids": ["not-an-id"]})),
               ("ovr.json", json.dumps({"name_overrides": [{"notion_id": P2}]})),
               ("sub.json", json.dumps({"hold_ids": [{"notion_id": P2, "subtree": "yes"}]}))]
        for name, text in bad:
            p = self.write_file(name, text)
            with self.assertRaises(vv.InputError, msg=name):
                self.run_wb(dev=p, out_name="o_" + name)
        args = ["--snapshot", self.snap, "--sidebar", self.sidebar, "--roots-kinds", self.roots,
                "--vault", self.vault, "--out", os.path.join(self.tmp, "cli_bad"),
                "--expected-deviations", os.path.join(self.tmp, "notjson.json")]
        self.assertEqual(vv.main(args), 2)
        self.assertEqual(vv.main(args[:-1] + [os.path.join(self.tmp, "missing.json")]), 2)

    def test_negative_tests_refused_with_new_flags(self):
        out = os.path.join(self.tmp, "o_neg_refused")
        with self.assertRaises(vv.InputError):
            vv.verify(self.snap, self.sidebar, self.vault, out, self.roots, True,
                      expected_deviations=self.dev_file({}))
        with self.assertRaises(vv.InputError):
            vv.verify(self.snap, self.sidebar, self.vault, out, self.roots, True, ids=self.ids_file([P2]))

    def test_cli_flags(self):
        v = self.mutate_copy("v_cli_dev")
        shutil.copy2(os.path.join(v, *CHILD_ONE.split("/")), os.path.join(v, *SYNC_COPY.split("/")))
        args = ["--snapshot", self.snap, "--sidebar", self.sidebar, "--roots-kinds", self.roots, "--vault", v]
        self.assertEqual(vv.main(args + ["--out", os.path.join(self.tmp, "c1")]), 1)
        dev = self.dev_file({"sync_conflict_copies": [SYNC_COPY]})
        self.assertEqual(vv.main(args + ["--out", os.path.join(self.tmp, "c2"), "--expected-deviations", dev]), 0)
        with open(os.path.join(self.tmp, "c2", "verify.json"), encoding="utf-8") as fh:
            rep = json.load(fh)
        self.assertEqual(rep["status_by_check"]["C02"], "PASS")
        self.assertEqual(rep["registered_deviations"]["total"], 2)
        self.assertEqual(vv.main(args + ["--out", os.path.join(self.tmp, "c3"), "--expected-deviations", dev,
                                         "--ids", self.ids_file([P2])]), 0)


class TestNameOverrides(WBFixture):
    """WRITEBACK_SPEC 7.4: a local same-folder rename the exporter applied -> C03 name issue is INFO (命名差异)."""

    MY = "General/Home/My Child.md"

    def applied_override_vault(self, name):
        v = self.mutate_copy(name)
        os.replace(os.path.join(v, *CHILD_ONE.split("/")), os.path.join(v, *self.MY.split("/")))
        self.edit(v, self.MY, "notion_created_time", 'aliases:\n  - "Child One"\nnotion_created_time')
        home = "General/Home/Home.md"
        self.edit(v, home, "[[General/Home/Child One|Child One]]\n\n> [!toggle]",
                  "[[General/Home/My Child|Child One]]\n\n> [!toggle]")
        self.edit(v, home, "[[General/Home/Child One|Child One]] is linked", "[[General/Home/My Child|Child One]] is linked")
        self.edit(v, "Notion_Obsidian/Godcreate 目录索引.md", "[[General/Home/Child One|", "[[General/Home/My Child|")
        self.edit(v, "Notion_Obsidian/sortspec.md", "/:files. Child One.md", "/:files. My Child.md")
        return v

    def dev(self, local_rel=None, name="d.json"):
        return self.dev_file({"name_overrides": [{"notion_id": P2, "old_rel": CHILD_ONE,
                                                  "local_rel": local_rel or self.MY}]}, name)

    def test_applied_override_is_info(self):
        v = self.applied_override_vault("v_ovr")
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C03",))
        self.assertEqual([i["issue"] for i in rep0["checks"]["C03"]["details"]["issues"]], ["name_not_derived_from_title"])
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev())
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual(self.reg(rep, "C03"), 1)
        naming = rep["registered_deviations"]["name_overrides"]
        self.assertEqual([(n["id"], n["found"], n["expected"], n["old_rel"]) for n in naming],
                         [(P2, "My Child", "Child One", CHILD_ONE)])

    def test_override_without_alias_is_info_l11(self):
        """L-11: the exporter derives aliases from the title-derived name (WB 7.4.3 / D-22), so an override note
        whose title-derived name equals its title has no alias -> C08 aliases issue is 命名差异 too."""
        v = self.applied_override_vault("v_ovr_noalias")
        self.edit(v, self.MY, 'aliases:\n  - "Child One"\nnotion_created_time', "notion_created_time")
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C03", "C08"))
        self.assertEqual([i["issue"] for i in rep0["checks"]["C08"]["details"]["issues"]],
                         ["aliases_missing_original_title"])
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev())
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual(self.reg(rep, "C03"), 1)
        self.assertEqual(self.reg(rep, "C08"), 1)
        # listed at another path: both issues keep failing
        ok2, rep2, _ = self.run_wb(vault=v, dev=self.dev("General/Home/Other Name.md", name="d2.json"))
        self.assertFalse(ok2)
        self.assert_statuses(rep2, fail=("C03", "C08"))

    def test_override_listed_at_another_path_still_fails(self):
        v = self.applied_override_vault("v_ovr_other")
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev("General/Home/Other Name.md"))
        self.assertFalse(ok)
        self.assert_statuses(rep, fail=("C03",))
        it = rep["checks"]["C03"]["details"]["issues"][0]
        self.assertEqual((it["issue"], it["name_override_listed_local_rel"]),
                         ("name_not_derived_from_title", "General/Home/Other Name.md"))
        self.assertEqual(rep["registered_deviations"]["name_overrides"], [])

    def test_override_never_hides_a_readded_original(self):
        """Publisher re-ADDs old_rel next to the renamed note (forbidden by 7.4.2): C02 duplicates FAIL."""
        v = self.applied_override_vault("v_ovr_dup")
        shutil.copy2(os.path.join(self.vault, *CHILD_ONE.split("/")), os.path.join(v, *CHILD_ONE.split("/")))
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev())
        self.assertFalse(ok)
        self.assertEqual(rep["checks"]["C02"]["status"], "FAIL")
        self.assertEqual(rep["checks"]["C02"]["details"]["duplicates"][0]["paths"], sorted([CHILD_ONE, self.MY]))
        self.assertEqual(self.reg(rep, "C02"), 0)
        self.assertEqual(self.reg(rep, "C03"), 1)  # the override's local_rel stays the canonical note

    def test_unrewritten_links_to_old_rel_still_fail(self):
        """The exporter must rewrite links / sortspec / index to local_rel: a stale old_rel link is a defect."""
        v = self.mutate_copy("v_ovr_raw")
        os.replace(os.path.join(v, *CHILD_ONE.split("/")), os.path.join(v, *self.MY.split("/")))
        self.edit(v, self.MY, "notion_created_time", 'aliases:\n  - "Child One"\nnotion_created_time')
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev())
        self.assertFalse(ok)
        self.assert_statuses(rep, fail=("C04", "C05", "C06"))
        self.assertEqual(self.reg(rep, "C03"), 1)
        self.assertEqual((self.reg(rep, "C05"), self.reg(rep, "C06")), (0, 0))

    GRAND = "General/Home/Child Two/Grand： Child.md"
    MY_GRAND = "General/Home/Child Two/My Grand.md"

    def grand_override_vault(self, name):
        """P4 ("Grand: Child", title-derived name "Grand： Child") renamed locally to "My Grand", applied by the
        exporter: links / index / sortspec use local_rel, aliases keep the title (the name needed sanitising)."""
        v = self.mutate_copy(name)
        os.replace(os.path.join(v, *self.GRAND.split("/")), os.path.join(v, *self.MY_GRAND.split("/")))
        self.edit(v, "General/Home/Child Two/Child Two.md", "[[General/Home/Child Two/Grand： Child|",
                  "[[General/Home/Child Two/My Grand|")
        self.edit(v, "Notion_Obsidian/Godcreate 目录索引.md", "[[General/Home/Child Two/Grand： Child|",
                  "[[General/Home/Child Two/My Grand|")
        self.edit(v, "Notion_Obsidian/sortspec.md", "/:files. Grand： Child.md", "/:files. My Grand.md")
        return v

    def test_override_alias_downgrade_needs_a_title_derived_name_equal_to_the_title_l11(self):
        """L-11 guard (r1-spec-coverage-07): the C08 alias downgrade applies only when the title-derived name equals
        the title. "Grand: Child" sanitises to "Grand： Child", so the exporter writes `aliases: ["Grand: Child"]` on
        the override note as well; a missing alias there is damage and keeps failing C08 (FAILs if the guard is
        dropped: the issue would be hidden as 命名差异)."""
        v = self.grand_override_vault("v_ovr_grand")
        dev = self.dev_file({"name_overrides": [{"notion_id": P4, "old_rel": self.GRAND, "local_rel": self.MY_GRAND}]},
                            "d_grand.json")
        ok, rep, _ = self.run_wb(vault=v, dev=dev)
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual((self.reg(rep, "C03"), self.reg(rep, "C08")), (1, 0))
        self.edit(v, self.MY_GRAND, '  - "Grand: Child"\n', "")
        self.edit(v, self.MY_GRAND, "aliases:\n", "")
        ok2, rep2, _ = self.run_wb(vault=v, dev=dev, out_name="o_grand_noalias")
        self.assertFalse(ok2)
        self.assert_statuses(rep2, fail=("C08",))
        self.assertEqual([(i["issue"], i["path"]) for i in rep2["checks"]["C08"]["details"]["issues"]],
                         [("aliases_missing_original_title", self.MY_GRAND)])
        self.assertEqual((self.reg(rep2, "C03"), self.reg(rep2, "C08")), (1, 0))
        self.assertEqual(rep2["checks"]["C08"]["details"]["registered_deviations(info)"], {})

    def test_held_copy_of_an_override_note_is_info_but_a_readded_original_still_fails(self):
        """r1-n2o-completeness-04 with a name override in effect: "Make a copy" of the renamed note -> the scan holds
        the object with both notes listed -> C02 INFO. A publisher re-ADD of old_rel is not in the hold entry, so the
        duplicate keeps failing (the 'never hides a re-added original' rule still holds)."""
        v = self.applied_override_vault("v_ovr_heldcopy")
        my_copy = "General/Home/My Child 1.md"
        shutil.copy2(os.path.join(v, *self.MY.split("/")), os.path.join(v, *my_copy.split("/")))
        dev = self.dev_file({"name_overrides": [{"notion_id": P2, "old_rel": CHILD_ONE, "local_rel": self.MY}],
                             "paused": [{"notion_id": P2, "subtree": True, "paths": sorted([self.MY, my_copy])}]},
                            "d_heldcopy.json")
        ok, rep, _ = self.run_wb(vault=v, dev=dev)
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        info = rep["checks"]["C02"]["details"]["registered_deviations(info)"]["duplicates"]
        self.assertEqual([(i["id"], i["paths"], i["deviation"]) for i in info], [(P2, sorted([self.MY, my_copy]), "paused")])
        shutil.copy2(os.path.join(self.vault, *CHILD_ONE.split("/")), os.path.join(v, *CHILD_ONE.split("/")))
        ok2, rep2, _ = self.run_wb(vault=v, dev=dev, out_name="o_heldcopy_readd")
        self.assertFalse(ok2)
        self.assertEqual(rep2["checks"]["C02"]["status"], "FAIL")
        self.assertEqual(rep2["checks"]["C02"]["details"]["duplicates"],
                         [{"id": P2, "paths": sorted([CHILD_ONE, self.MY, my_copy])}])
        self.assertEqual(self.reg(rep2, "C02"), 0)


class TestHeldDuplicates(WBFixture):
    """WB 14.3 映射异常 (r1-n2o-completeness-04): Obsidian "Make a copy" keeps the frontmatter, so one notion_id is in
    two notes. The scan holds the object (mapping_duplicate, subtree) and lists EVERY note of it in its own hold
    entry; refresh passes it to the post-publish verify under `paused`. C02 duplicates of that id are then the
    registered object hold (INFO, result published_with_holds), like the copy's C08 staleness. A hold that does not
    describe every copy (no paths, a path missing, only an ancestor's entry) keeps the duplicate a FAIL."""

    COPY = "General/Home/Child One 1.md"
    COPY2 = "General/Home/Child One 2.md"

    def copy_vault(self, name, copies=(COPY,)):
        v = self.mutate_copy(name)
        src = os.path.join(v, *CHILD_ONE.split("/"))
        for rel in copies:
            shutil.copy2(src, os.path.join(v, *rel.split("/")))
        return v

    def test_scan_hold_listing_every_note_turns_the_duplicate_into_info(self):
        v = self.copy_vault("v_hdup")
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C02", "C08"))
        both = sorted([CHILD_ONE, self.COPY])
        self.assertEqual(rep0["checks"]["C02"]["details"]["duplicates"], [{"id": P2, "paths": both}])
        for key, cat in (("paused", "paused"), ("hold_ids", "hold")):
            dev = self.dev_file({key: [{"notion_id": P2, "subtree": True, "paths": both}]}, f"d_{key}.json")
            ok, rep, _ = self.run_wb(vault=v, dev=dev, out_name="o_" + key)
            self.assertTrue(ok, (key, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS}))
            self.assert_statuses(rep, ctx=key)
            info = rep["checks"]["C02"]["details"]["registered_deviations(info)"]["duplicates"]
            self.assertEqual([(i["id"], i["paths"], i["deviation"]) for i in info], [(P2, both, cat)])
            self.assertEqual(self.reg(rep, "C02"), 1)
            self.assertEqual(rep["checks"]["C02"]["details"]["duplicates"], [])
            self.assertEqual(rep["registered_deviations"]["rejected_entries"], [])
            self.assertEqual(rep["registered_deviations"]["unmatched_entries(info)"], [])

    def test_plain_copy_and_sync_copy_together(self):
        """The scan lists Sync conflict copies separately (never in its duplicates): hold paths = the plain notes,
        sync_conflict_copies = the Sync copy; together they describe every note of the id."""
        v = self.copy_vault("v_hdup_sync", copies=(self.COPY, SYNC_COPY))
        dev = self.dev_file({"paused": [{"notion_id": P2, "subtree": True, "paths": sorted([CHILD_ONE, self.COPY])}],
                             "sync_conflict_copies": [{"path": SYNC_COPY, "notion_id": P2}]})
        ok, rep, _ = self.run_wb(vault=v, dev=dev)
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        info = rep["checks"]["C02"]["details"]["registered_deviations(info)"]["duplicates"]
        self.assertEqual([(i["id"], i["deviation"]) for i in info], [(P2, "paused")])
        self.assertEqual(rep["registered_deviations"]["unmatched_entries(info)"], [])
        # without the Sync entry the Sync copy is an undescribed third note -> FAIL
        dev2 = self.dev_file({"paused": [{"notion_id": P2, "subtree": True, "paths": sorted([CHILD_ONE, self.COPY])}]},
                             "d2.json")
        ok2, rep2, _ = self.run_wb(vault=v, dev=dev2, out_name="o2")
        self.assertFalse(ok2)
        self.assertEqual(rep2["checks"]["C02"]["status"], "FAIL")
        self.assertEqual(self.reg(rep2, "C02"), 0)

    def test_hold_that_does_not_describe_every_copy_still_fails(self):
        v = self.copy_vault("v_hdup_neg", copies=(self.COPY, self.COPY2))
        three = sorted([CHILD_ONE, self.COPY, self.COPY2])
        cases = {
            # a hold without paths (post-verify hold, pending delete, tombstone): never excuses a duplicate
            "no_paths": {"hold_ids": [P2]},
            "no_paths_object": {"paused": [{"notion_id": P2, "subtree": True, "paths": []}]},
            # the scan saw two notes; a third one appeared later (or was written by the publisher)
            "one_copy_unlisted": {"paused": [{"notion_id": P2, "subtree": True, "paths": sorted([CHILD_ONE, self.COPY])}]},
            # only an ancestor's subtree entry lists the notes: not the scan's registration of THIS id's duplicate
            "ancestor_entry_only": {"paused": [{"notion_id": P1, "subtree": True, "paths": three}]},
        }
        for name, obj in cases.items():
            ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file(obj, f"d_{name}.json"), out_name="o_" + name)
            self.assertFalse(ok, name)
            c02 = rep["checks"]["C02"]
            self.assertEqual(c02["status"], "FAIL", name)
            self.assertEqual(c02["details"]["duplicates"], [{"id": P2, "paths": three}], name)
            self.assertEqual(self.reg(rep, "C02"), 0, name)
            self.assertEqual(rep["registered_deviations"]["rejected_entries"], [], name)
        # the same entry listing all three notes is the registered hold
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"paused": [{"notion_id": P2, "paths": three}]}, "d_all.json"),
                                 out_name="o_all")
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual(self.reg(rep, "C02"), 1)

    def test_hold_entry_of_another_object_never_excuses_the_duplicate(self):
        """Paths listed under another object's entry are rejected by validation (not notes of a held object)."""
        v = self.copy_vault("v_hdup_other")
        both = sorted([CHILD_ONE, self.COPY])
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"paused": [{"notion_id": P4, "subtree": True, "paths": both}]}))
        self.assertFalse(ok)
        self.assertEqual(rep["checks"]["C02"]["details"]["duplicates"], [{"id": P2, "paths": both}])
        self.assertEqual(self.reg(rep, "C02"), 0)
        self.assertEqual(sorted((e["path"], e["reason"]) for e in rep["registered_deviations"]["rejected_entries"]),
                         [(p, "path_is_not_a_note_of_a_held_object") for p in both])


class TestIdsScope(WBFixture):
    """--ids (WRITEBACK_SPEC W6): C03/C04/C06/C08/C10 per object; C01/C02/C05/C07/C09 still whole vault."""

    GRAND = "General/Home/Child Two/Grand： Child.md"

    def break_grandchild(self, v):
        self.edit(v, self.GRAND, "notion_order: 1", "notion_order: 7")                     # C04
        self.edit(v, self.GRAND, f'notion_parent_id: "{P3}"', f'notion_parent_id: "{P1}"')  # C03
        self.edit(v, self.GRAND, '"Grand: Child"\nnotion_parent_id', '"Grand X"\nnotion_parent_id')  # C08 title
        self.edit(v, self.GRAND, "Grandchild text\n", "[[Broken Target]]\n")                # C06 + C10

    def test_scope_restricts_per_object_checks(self):
        v = self.mutate_copy("v_ids")
        self.break_grandchild(v)
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C03", "C04", "C06", "C08", "C10"))
        ok, rep, _ = self.run_wb(vault=v, ids=self.ids_file([P2]))
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        for c in vv.SCOPED_CHECKS:
            self.assertIn("scope=--ids(1)", rep["checks"][c]["summary"])
        for c in ("C01", "C02", "C05", "C07", "C09", "C11"):
            self.assertNotIn("scope=--ids", rep["checks"][c]["summary"])
        self.assertEqual(rep["scope"]["listed"], [P2])
        self.assertEqual(rep["scope"]["notes_in_scope"], 1)
        self.assertTrue(rep["checks"]["C03"]["summary"].startswith("checked=1 "))
        _ok2, rep2, _ = self.run_wb(vault=v, ids=self.ids_file([P4], "ids2.txt"), out_name="o2")
        self.assert_statuses(rep2, fail=("C03", "C04", "C06", "C08", "C10"))

    def test_global_checks_still_run_with_ids(self):
        v = self.mutate_copy("v_ids_global")
        self.remove(v, "Quantitative Investing/Server.md")         # C02 (and C10 outside the scope)
        self.add_file(v, "Stray.md", "x\n")                          # C01 + C02 md_without_notion_id
        self.edit(v, "Notion_Obsidian/sortspec.md", "  /folders Tasks\n  /folders Home\n",
                  "  /folders Home\n  /folders Tasks\n")             # C05
        self.remove(v, ATT_REL)                                      # C07
        self.add_file(v, "General/Home/bad%name.png", "x\n")         # C09
        ok, rep, _ = self.run_wb(vault=v, ids=self.ids_file([P2]))
        self.assertFalse(ok)
        self.assert_statuses(rep, fail=("C01", "C02", "C05", "C07", "C09"))

    def test_scope_checks_the_parent_body_order(self):
        v = self.mutate_copy("v_ids_parent")
        home = "General/Home/Home.md"
        self.edit(v, home, "[[General/Home/Child One|Child One]]\n\n> [!toggle]- Toggle title\n"
                           "> [[General/Home/Child Two/Child Two|Child Two]]",
                  "[[General/Home/Child Two/Child Two|Child Two]]\n\n> [!toggle]- Toggle title\n"
                  "> [[General/Home/Child One|Child One]]")
        _ok, rep, _ = self.run_wb(vault=v, ids=self.ids_file([P2]))
        self.assertEqual(rep["checks"]["C04"]["status"], "FAIL", rep["checks"]["C04"]["summary"])
        _ok2, rep2, _ = self.run_wb(vault=v, ids=self.ids_file([Q1], "q.txt"), out_name="o2")
        self.assertEqual(rep2["checks"]["C04"]["status"], "PASS", rep2["checks"]["C04"]["summary"])

    def test_inbound_subpath_into_scoped_note_is_checked(self):
        v = self.mutate_copy("v_ids_inbound")
        self.edit(v, "Quantitative Investing/Server.md", "# Server notes\n",
                  "# Server notes\n\n![[General/Home/Child One#No Such Heading]]\n")
        _ok, rep, _ = self.run_wb(vault=v, ids=self.ids_file([P2]))
        self.assertEqual(rep["checks"]["C06"]["status"], "FAIL")
        self.assertEqual(len(rep["checks"]["C06"]["details"]["embed_subpath_unresolved"]), 1)
        _ok2, rep2, _ = self.run_wb(vault=v, ids=self.ids_file([P4], "p4.txt"), out_name="o2")
        self.assertEqual(rep2["checks"]["C06"]["status"], "PASS")

    def test_ids_file_rules(self):
        p = self.write_file("ids_c.txt", f"# written objects\n{P2.replace('-', '')}  # Child One\n\n{P2}\n")
        ok, rep, _ = self.run_wb(ids=p)
        self.assertTrue(ok)
        self.assertEqual(rep["scope"]["ids"], 1)
        unknown = uid(999)
        ok2, rep2, _ = self.run_wb(ids=self.ids_file([unknown], "u.txt"), out_name="o2")
        self.assertFalse(ok2)  # fails closed: an id that cannot be checked never passes vacuously
        self.assert_statuses(rep2, fail=("C03",))
        self.assertEqual(rep2["scope"]["ids_not_in_expectation"], [unknown])
        self.assertEqual([u["id"] for u in rep2["scope"]["ids_unverifiable"]], [unknown])
        for name, text in (("empty.txt", "# nothing\n\n"), ("bad.txt", f"{P2}\nGeneral/Home/Child One.md\n")):
            with self.assertRaises(vv.InputError, msg=name):
                self.run_wb(ids=self.write_file(name, text), out_name="o_" + name)

    def test_w6_ids_with_deviations(self):
        """W6: --ids for the written object plus the round's deviations, in one run."""
        v = self.mutate_copy("v_w6")
        self.remove(v, "General/Home/Child Two/Grand： Child.md")
        self.add_file(v, "General/Home/My Draft.md", "draft\n")
        dev = self.dev_file({"hold_ids": [P4], "local_new": ["General/Home/My Draft.md"]})
        ok, rep, _ = self.run_wb(vault=v, dev=dev, ids=self.ids_file([P2]))
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual(self.reg(rep, "C02"), 2)
        ok2, rep2, _ = self.run_wb(vault=v, ids=self.ids_file([P2], "i2.txt"), out_name="o2")
        self.assertFalse(ok2)
        self.assertEqual(rep2["checks"]["C02"]["status"], "FAIL")  # without deviations the global C02 fails


class TestGeneratedFiles(WBFixture):
    """WRITEBACK_SPEC 5.1a generated notes under Notion_Obsidian/ and their custom-sort sections."""

    PREVIEW = "Notion_Obsidian/写回预览/写回预览-wb-20261118T1012-ab12.md"
    RUNLOG = "Notion_Obsidian/运行记录/写回-wb-20261118T1012-ab12.md"
    PAUSED = "Notion_Obsidian/运行记录/20261118T0400-T1-已暂停.md"
    UNFINISHED = "Notion_Obsidian/运行记录/20261118T0400-T1-未完成.md"
    SECTIONS = ("\n  target-folder: Notion_Obsidian\n  /:files. README.md\n  /:files. Godcreate 目录索引.md\n"
                "  /:files. sortspec.md\n  /folders 写回预览\n  /folders 运行记录\n  %\n"
                "\n  target-folder: Notion_Obsidian/写回预览\n  > a-z\n  %\n"
                "\n  target-folder: Notion_Obsidian/运行记录\n  > a-z\n  %\n")

    def gen(self, by="writeback.py", extra="", body=""):
        return ("---\nnotion_kind: generated\n" + (f"generated_by: {by}\n" if by else "") + 'plan_id: "wb-20261118T1012-ab12"\n'
                + extra + "---\n\n# 写回预览\n\n- 本地文件 `General/Home/Child One.md`（Notion：https://app.notion.com/p/"
                + P2.replace("-", "") + "）\n" + body)

    def generated_vault(self, name):
        v = self.mutate_copy(name)
        for rel in (self.PREVIEW, self.RUNLOG):
            self.add_file(v, rel, self.gen())
        for rel in (self.PAUSED, self.UNFINISHED):
            self.add_file(v, rel, self.gen(by="refresh.py"))
        self.edit(v, "Notion_Obsidian/sortspec.md", "  /:files. Row C.md\n  %\n", "  /:files. Row C.md\n  %\n" + self.SECTIONS)
        return v

    def test_generated_files_and_new_sections_pass(self):
        v = self.generated_vault("v_gen")
        ok, rep, _ = self.run_wb(vault=v)
        self.assertTrue(ok, {c: (rep["checks"][c]["summary"], rep["checks"][c]["details"].get("problems"))
                             for c in ALL_CHECKS if rep["checks"][c]["status"] != "PASS"})
        self.assertIn("generated_files=4", rep["checks"]["C08"]["summary"])
        self.assertEqual(rep["checks"]["C08"]["details"]["generated_files_checked(WRITEBACK_SPEC_5.1a)"], 4)
        self.assertEqual(rep["checks"]["C08"]["details"]["warnings"], [])
        self.assertEqual(rep["checks"]["C05"]["summary"].split()[0], "sections=16")  # 13 + 3
        self.assertEqual({w["issue"] for w in rep["checks"]["C05"]["details"]["warnings"]}, {"order_attribute_not_validated"})
        self.assertEqual(vv.generated_file_kind(self.PREVIEW), "writeback_preview")
        self.assertEqual(vv.generated_file_kind(self.PAUSED), "paused_runlog")
        self.assertIsNone(vv.generated_file_kind("Notion_Obsidian/运行记录/20261117T1859-T1.md"))
        self.assertIsNone(vv.generated_file_kind("General/写回预览/x.md"))

    def test_generated_file_rules_fail_c08(self):
        v = self.generated_vault("v_gen_bad")
        self.add_file(v, self.PREVIEW, self.gen(body="- [[General/Home/Child One]]\n"))          # wikilink (resolves)
        self.add_file(v, self.RUNLOG, "# 写回运行记录\n")                                         # no frontmatter
        self.add_file(v, self.PAUSED, self.gen(by=None, extra=f'notion_id: "{P2}"\n'))           # notion_id, no generated_by
        ok, rep, _ = self.run_wb(vault=v)
        self.assertFalse(ok)
        self.assert_statuses(rep, fail=("C08",))
        got = sorted((i["path"], i["issue"]) for i in rep["checks"]["C08"]["details"]["issues"])
        self.assertEqual(got, sorted([(self.PREVIEW, "generated_file_contains_wikilink"),
                                      (self.RUNLOG, "generated_file_not_kind_generated"),
                                      (self.PAUSED, "generated_file_has_notion_id")]))
        self.assertEqual(sorted((w["path"], w["issue"]) for w in rep["checks"]["C08"]["details"]["warnings"]),
                         sorted([(self.PAUSED, "generated_file_without_generated_by"),
                                 (self.RUNLOG, "generated_file_without_generated_by")]))
        # a registered deviation never excuses a generated-file rule
        ok2, rep2, _ = self.run_wb(vault=v, dev=self.dev_file({"local_new": [self.PREVIEW, self.RUNLOG]}), out_name="o2")
        self.assertFalse(ok2)
        self.assertEqual(rep2["checks"]["C08"]["status"], "FAIL")

    def test_generated_looking_files_in_wrong_places_still_fail(self):
        v = self.generated_vault("v_gen_wrong")
        # a second sortspec.md (the plugin parses it: duplicate target-folder risk) -> C05, even if "listed"
        self.add_file(v, "Notion_Obsidian/写回预览/sortspec.md",
                      '---\nnotion_kind: generated\nsorting-spec: |\n  target-folder: Notion_Obsidian/写回预览\n  %\n---\n')
        # a preview folder at the vault root -> C01 extra (unlisted)
        self.add_file(v, "写回预览/写回预览-wb-x.md", self.gen())
        dev = self.dev_file({"local_new": ["Notion_Obsidian/写回预览/sortspec.md"]})
        ok, rep, _ = self.run_wb(vault=v, dev=dev)
        self.assertFalse(ok)
        self.assertEqual(rep["checks"]["C01"]["details"]["extra"], ["写回预览"])
        self.assertEqual([p["issue"] for p in rep["checks"]["C05"]["details"]["problems"]],
                         ["foreign_sorting_spec_parsed_by_plugin"])
        self.assertIn("C01", [c for c in ALL_CHECKS if rep["status_by_check"][c] == "FAIL"])
        self.assertEqual(rep["status_by_check"]["C05"], "FAIL")

    def test_task_dir_section_syntax_errors_fail_c05(self):
        v = self.mutate_copy("v_gen_syntax")
        self.edit(v, "Notion_Obsidian/sortspec.md", "  /:files. Row C.md\n  %\n",
                  "  /:files. Row C.md\n  %\n\n  target-folder: Notion_Obsidian/写回预览\n"
                  "  target-folder: Notion_Obsidian/运行记录\n  %\n")
        rep = self.assert_fails(v, "C05")
        self.assertEqual([p["issue"] for p in rep["checks"]["C05"]["details"]["problems"]], ["targets_share_one_section"])
        v2 = self.mutate_copy("v_gen_syntax2")
        self.edit(v2, "Notion_Obsidian/sortspec.md", "  /:files. Row C.md\n  %\n",
                  "  /:files. Row C.md\n  %\n\n  target-folder: Notion_Obsidian/写回预览\n  写回预览-latest.md\n  "
                  "Descending previews\n  %\n")
        rep2 = self.assert_fails(v2, "C05")
        self.assertEqual({p["issue"] for p in rep2["checks"]["C05"]["details"]["problems"]}, {"spec_error_disables_plugin"})


# ============================================================ review round 1 (verifier v1.3 fixes)
import contextlib  # noqa: E402 (kept here: the original test section stays byte-identical)
import io  # noqa: E402
from unittest import mock  # noqa: E402

SERVER = "Quantitative Investing/Server.md"
GRAND = "General/Home/Child Two/Grand： Child.md"
INDEX = "Notion_Obsidian/Godcreate 目录索引.md"


class TestIdsFailClosed(WBFixture):
    """--ids never passes vacuously: every listed id is checked, or verified as trashed-and-absent, or FAILs C03."""

    GRAND = GRAND

    def test_unverifiable_ids_fail_c03(self):
        v = self.mutate_copy("v_ids_closed")
        TestIdsScope.break_grandchild(self, v)  # faults outside the scope: they must not matter, nor pass vacuously
        cases = [(uid(4242), "unknown_to_snapshot"), (DS1, "data_source_id"), (uid(110), "block_id")]
        for n, (i, why) in enumerate(cases):
            ok, rep, _ = self.run_wb(vault=v, ids=self.ids_file([i], f"u{n}.txt"), out_name=f"o{n}")
            self.assertFalse(ok, why)
            self.assert_statuses(rep, fail=("C03",), ctx=why)
            un = rep["checks"]["C03"]["details"]["scope_ids_unverifiable"]
            self.assertEqual([(u["id"], u["reason"].split("(")[0]) for u in un], [(i, why)])
            self.assertEqual(rep["scope"]["counts"], {"checked": 0, "trashed_absent": 0, "unverifiable": 1})
            self.assertIn("scope_ids_unverifiable=1", rep["checks"]["C03"]["summary"])
        # one checkable id does not excuse an unverifiable one
        ok, rep, _ = self.run_wb(vault=v, ids=self.ids_file([P2, uid(4242)], "mix.txt"), out_name="o_mix")
        self.assertFalse(ok)
        self.assert_statuses(rep, fail=("C03",))
        self.assertEqual(rep["scope"]["counts"], {"checked": 1, "trashed_absent": 0, "unverifiable": 1})
        self.assertEqual(rep["scope"]["ids_checked"], [P2])
        # stdout shows the scope counts; exit 1
        buf = io.StringIO()
        args = ["--snapshot", self.snap, "--sidebar", self.sidebar, "--roots-kinds", self.roots, "--vault", v,
                "--out", os.path.join(self.tmp, "cli_scope"), "--ids", self.ids_file([uid(4242)], "cli.txt")]
        with contextlib.redirect_stdout(buf):
            rc = vv.main(args)
        self.assertEqual(rc, 1)
        self.assertIn("SCOPE(--ids) listed=1 checked=0 trashed_absent(info)=0 unverifiable=1 notes_in_scope=0",
                      buf.getvalue())
        with open(os.path.join(self.tmp, "cli_scope", "verify.md"), encoding="utf-8") as fh:
            self.assertIn("unverifiable (C03 FAIL): 1", fh.read())

    def test_trashed_page_verified_absent(self):
        """A page trashed by write-back: trashed in the snapshot AND no note -> info; a leftover note FAILs."""
        ok, rep, _ = self.run_wb(ids=self.ids_file([T1]))
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual(rep["scope"]["ids_trashed_in_snapshot_and_absent(info)"], [T1])
        self.assertEqual(rep["scope"]["counts"], {"checked": 0, "trashed_absent": 1, "unverifiable": 0})
        v = self.mutate_copy("v_trash_left")
        self.add_file(v, "General/Home/Old Page.md", nfm(T1, "page", "Old Page", P1, "General", 5) + "old\n")
        ok2, rep2, _ = self.run_wb(vault=v, ids=self.ids_file([T1], "t2.txt"), out_name="o2")
        self.assertFalse(ok2)
        self.assertEqual(rep2["status_by_check"]["C03"], "FAIL")
        self.assertEqual(rep2["status_by_check"]["C02"], "FAIL")
        self.assertEqual([u["reason"] for u in rep2["checks"]["C03"]["details"]["scope_ids_unverifiable"]],
                         ["note_in_vault_for_trashed_object"])

    def test_written_object_without_note_fails_even_if_held(self):
        v = self.mutate_copy("v_ids_nonote")
        self.remove(v, CHILD_ONE)
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [P2]}), ids=self.ids_file([P2]))
        self.assertFalse(ok)
        self.assert_statuses(rep, fail=("C03",))  # C02 missing / C10 are INFO through the hold, the scope is not
        self.assertEqual([u["reason"] for u in rep["checks"]["C03"]["details"]["scope_ids_unverifiable"]],
                         ["expected_object_has_no_note_in_vault"])
        self.assertGreaterEqual(self.reg(rep, "C02"), 1)


class TestDeviationEntryValidation(WBFixture):
    """An entry that does not describe what it names is rejected (never used): its issues keep failing."""

    def rejected(self, rep):
        return [(e["category"], e.get("path"), e["reason"].split(":")[0])
                for e in rep["registered_deviations"]["rejected_entries"]]

    def test_local_new_naming_an_exported_note_is_rejected(self):
        v = self.mutate_copy("v_ln_exported")
        self.edit(v, CHILD_ONE, f'notion_last_edited_time: "{LT}"', 'notion_last_edited_time: "2025-01-01T00:00:00.000Z"')
        self.edit(v, CHILD_ONE, "an escape \\\\ char\n", "an escape \\\\ char\n\n[[Nowhere At All]]\n")
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C06", "C08"))
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"local_new": [CHILD_ONE]}))
        self.assertFalse(ok)
        self.assert_statuses(rep, fail=("C06", "C08"))
        self.assertEqual(rep["registered_deviations"]["total"], 0)
        self.assertEqual(self.rejected(rep), [("local_new", CHILD_ONE, "has_notion_id")])
        self.assertEqual(rep["registered_deviations"]["unmatched_entries(info)"], [])

    def test_local_new_naming_tool_folder_or_exporter_files_is_rejected(self):
        v = self.mutate_copy("v_ln_tool")
        self.edit(v, INDEX, "[[General/Home/Child One|", "[[General/Home/Child Onx|")  # exporter defect (C05/C06)
        with open(os.path.join(v, *ATT_REL.split("/")), "ab") as fh:                 # damaged attachment (C07)
            fh.write(b"corrupted")
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C05", "C06", "C07"))
        listed = [INDEX, ATT_REL, "General/Home", "General", "General/Tasks/Tasks.base"]
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"local_new": listed}))
        self.assertFalse(ok)
        self.assert_statuses(rep, fail=("C05", "C06", "C07"))
        self.assertEqual(rep["registered_deviations"]["total"], 0)
        self.assertEqual(self.rejected(rep), [
            ("local_new", INDEX, "tool_file_in_task_dir"),
            ("local_new", ATT_REL, "exporter_attachment_name"),
            ("local_new", "General/Home", "folder_holds_has_notion_id"),
            ("local_new", "General", "teamspace_folder"),
            ("local_new", "General/Tasks/Tasks.base", "database_base_file")])
        # a user folder holding only user files is still accepted
        self.add_file(v, "General/Home/Drafts/a.md", "a\n")
        ok2, rep2, _ = self.run_wb(vault=v, dev=self.dev_file({"local_new": ["General/Home/Drafts",
                                                                             "General/Home/Drafts/a.md"]}, "d2.json"),
                                   out_name="o2")
        self.assertEqual(rep2["registered_deviations"]["rejected_entries"], [])
        self.assertEqual(rep2["registered_deviations"]["unmatched_entries(info)"], [{"path": "General/Home/Drafts",
                                                                                    "category": "local_new"}])

    def test_hold_paths_must_be_absent_or_a_held_note(self):
        v = self.mutate_copy("v_hold_paths")
        self.edit(v, SERVER, "# Server notes\n", "# Server notes\n\n[[Broken Target Q]]\n\n"
                                                 "[[General/Home/Child Two/Old Grand]]\n")
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C06",))
        # another object's note as a hold path (review probe C): rejected, its broken link keeps failing
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [{"notion_id": P4, "paths": [SERVER]}]}))
        self.assertFalse(ok)
        self.assert_statuses(rep, fail=("C06",))
        self.assertEqual(self.reg(rep, "C06"), 0)
        self.assertEqual(self.rejected(rep), [("hold", SERVER, "path_is_not_a_note_of_a_held_object")])
        # the held note itself and an absent old name are accepted; an existing folder is rejected
        dev = self.dev_file({"hold_ids": [{"notion_id": P4, "paths": [GRAND, "General/Home/Child Two/Old Grand.md",
                                                                      "General/Home"]}]}, "d2.json")
        ok2, rep2, _ = self.run_wb(vault=v, dev=dev, out_name="o2")
        self.assertFalse(ok2)
        self.assertEqual([u["target"] for u in rep2["checks"]["C06"]["details"]["unresolved"]], ["Broken Target Q"])
        self.assertEqual(self.reg(rep2, "C06"), 1)  # the link to the held note's old name
        self.assertEqual(self.rejected(rep2), [("hold", "General/Home", "path_is_an_existing_folder")])

    def test_sync_copy_needs_a_conflict_name(self):
        """Review probe G: a plain duplicate listed as a sync copy -> rejected, C02 / C08 keep failing."""
        v = self.mutate_copy("v_sync_plain")
        dup = "General/Home/Child One 2.md"
        shutil.copy2(os.path.join(v, *CHILD_ONE.split("/")), os.path.join(v, *dup.split("/")))
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"sync_conflict_copies": [dup]}))
        self.assertFalse(ok)
        self.assert_statuses(rep, fail=("C02", "C08"))
        self.assertEqual(rep["registered_deviations"]["total"], 0)
        self.assertEqual(self.rejected(rep), [("sync_conflict_copy", dup, "name_is_not_a_sync_conflict_copy")])
        self.assertIsNone(vv.SYNC_CONFLICT_RE.search("Child One 2.md"))
        self.assertIsNotNone(vv.SYNC_CONFLICT_RE.search(vv.bname(SYNC_COPY)))

    def test_local_modified_path_must_belong_to_its_id(self):
        v = self.mutate_copy("v_lm_wrong")
        self.edit(v, SERVER, "# Server notes\n", "# Server notes\n\n[[Broken Target Q]]\n")
        draft = "General/Home/My Draft.md"
        self.add_file(v, draft, "draft [[Nowhere Draft]]\n")
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C02", "C06", "C08"))
        dev = self.dev_file({"local_modified": [{"notion_id": P2, "path": SERVER}, {"path": draft}]})
        ok, rep, _ = self.run_wb(vault=v, dev=dev)
        self.assertFalse(ok)
        self.assert_statuses(rep, fail=("C02", "C06", "C08"))
        self.assertEqual(rep["registered_deviations"]["total"], 0)
        self.assertEqual(self.rejected(rep), [("local_modified", SERVER, "path_belongs_to_another_notion_id"),
                                              ("local_modified", draft, "path_has_no_notion_id")])
        # the right entries: Server as local_modified of Q1, the draft as local_new
        dev2 = self.dev_file({"local_modified": [{"notion_id": Q1, "path": SERVER}], "local_new": [draft]}, "d2.json")
        ok2, rep2, _ = self.run_wb(vault=v, dev=dev2, out_name="o2")
        self.assertTrue(ok2, {c: rep2["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual(rep2["registered_deviations"]["rejected_entries"], [])
        self.assertEqual(self.reg(rep2, "C06"), 2)


class TestDeviationRuleCoverage(WBFixture):
    """One fixture per downgrade rule (review round 1): the listed object's issue is INFO, the same issue on an
    unlisted object still FAILs."""

    def test_hold_c07_snapshot_file_not_referenced(self):
        v = self.mutate_copy("v_hold_c07")
        self.edit(v, "General/Home/Home.md", f"![[{ATT_REL}]]\n\n", "")
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C07",))
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [{"notion_id": P1, "subtree": False}]}))
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        info = rep["checks"]["C07"]["details"]["registered_deviations(info)"]["snapshot_files_not_referenced"]
        self.assertEqual([(x["page"], x["deviation"]) for x in info], [(P1, "hold")])
        ok2, rep2, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [Q1]}, "d2.json"), out_name="o2")
        self.assertFalse(ok2)
        self.assert_statuses(rep2, fail=("C07",))

    def test_hold_c04_child_links(self):
        # child_link_missing: the parent body lost the link to a HELD child
        v = self.mutate_copy("v_hold_c04_missing")
        self.edit(v, "General/Home/Child Two/Child Two.md", "[[General/Home/Child Two/Grand： Child|Grand: Child]]\n\n", "")
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C04",))
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [P4]}))
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual(list(rep["checks"]["C04"]["details"]["registered_deviations(info)"]), ["child_link_missing"])
        _ok2, rep2, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [P2]}, "d2.json"), out_name="o2")
        self.assert_statuses(rep2, fail=("C04",))
        # child_links_out_of_order: a HELD (stale) parent body
        v2 = self.mutate_copy("v_hold_c04_order")
        self.edit(v2, "General/Home/Home.md", "[[General/Home/Child One|Child One]]\n\n> [!toggle]- Toggle title\n"
                                              "> [[General/Home/Child Two/Child Two|Child Two]]",
                  "[[General/Home/Child Two/Child Two|Child Two]]\n\n> [!toggle]- Toggle title\n"
                  "> [[General/Home/Child One|Child One]]")
        _ok, rep3, _ = self.run_wb(vault=v2, out_name="o3")
        self.assert_statuses(rep3, fail=("C04",))
        ok4, rep4, _ = self.run_wb(vault=v2, dev=self.dev_file({"hold_ids": [{"notion_id": P1, "subtree": False}]},
                                                              "d4.json"), out_name="o4")
        self.assertTrue(ok4, {c: rep4["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertIn("child_links_out_of_order", rep4["checks"]["C04"]["details"]["registered_deviations(info)"])
        _ok5, rep5, _ = self.run_wb(vault=v2, dev=self.dev_file({"hold_ids": [Q1]}, "d5.json"), out_name="o5")
        self.assert_statuses(rep5, fail=("C04",))

    def test_hold_c05_index(self):
        v = self.mutate_copy("v_hold_idx_missing")
        self.edit(v, INDEX, "- [[General/Home/Child One|Child One]]\n", "")
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C05",))
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [P2]}))
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual([p["issue"] for p in rep["checks"]["C05"]["details"]["registered_deviations(info)"]["problems"]],
                         ["index_missing_object"])
        _ok2, rep2, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [P4]}, "d2.json"), out_name="o2")
        self.assert_statuses(rep2, fail=("C05",))
        v2 = self.mutate_copy("v_hold_idx_order")
        self.edit(v2, INDEX, "- [[General/Home/Child One|Child One]]\n- [[General/Home/Child Two/Child Two|Child Two]]\n",
                  "- [[General/Home/Child Two/Child Two|Child Two]]\n- [[General/Home/Child One|Child One]]\n")
        _ok, rep3, _ = self.run_wb(vault=v2, out_name="o3")
        self.assert_statuses(rep3, fail=("C05",))
        ok4, rep4, _ = self.run_wb(vault=v2, dev=self.dev_file({"hold_ids": [P2]}, "d4.json"), out_name="o4")
        self.assertTrue(ok4, {c: rep4["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual([p["issue"] for p in rep4["checks"]["C05"]["details"]["registered_deviations(info)"]["problems"]],
                         ["index_order"])
        _ok5, rep5, _ = self.run_wb(vault=v2, dev=self.dev_file({"hold_ids": [Q1]}, "d5.json"), out_name="o5")
        self.assert_statuses(rep5, fail=("C05",))

    def test_paused_stale_note_with_old_name(self):
        """A paused note still at its old file name: C03 / C04 / C05 (sortspec name_key) / C06 / C08 are INFO."""
        v = self.mutate_copy("v_paused_old_name")
        os.replace(os.path.join(v, *CHILD_ONE.split("/")), os.path.join(v, "General", "Home", "Child Old.md"))
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C03", "C04", "C05", "C06", "C08"))
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"paused": [P2]}))
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual({c: self.reg(rep, c) for c in ("C03", "C04", "C05", "C06", "C08")},
                         {"C03": 1, "C04": 1, "C05": 2, "C06": 3, "C08": 1})
        self.assertIn("not_listed", {p["issue"] for p in
                                     rep["checks"]["C05"]["details"]["registered_deviations(info)"]["problems"]})
        self.assertEqual([i["issue"] for i in rep["checks"]["C08"]["details"]["registered_deviations(info)"]["issues"]],
                         ["aliases_missing_original_title"])
        self.assertEqual(set(rep["registered_deviations"]["by_category"]), {"paused"})
        _ok2, rep2, _ = self.run_wb(vault=v, dev=self.dev_file({"paused": [P4]}, "d2.json"), out_name="o2")
        self.assert_statuses(rep2, fail=("C03", "C04", "C05", "C06", "C08"))

    def test_hold_c08_only_staleness(self):
        """A held note's stale title is INFO; CRLF damage of the same note still FAILs C08."""
        v = self.mutate_copy("v_hold_c08")
        self.edit(v, GRAND, 'notion_title: "Grand: Child"', 'notion_title: "Grand Old"')
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C08",))
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [P4]}))
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual(self.reg(rep, "C08"), 1)
        p = os.path.join(v, *GRAND.split("/"))
        with open(p, "rb") as fh:
            raw = fh.read()
        with open(p, "wb") as fh:
            fh.write(raw.replace(b"\n", b"\r\n"))
        ok2, rep2, _ = self.run_wb(vault=v, dev=self.dev_file({"hold_ids": [P4]}, "d2.json"), out_name="o2")
        self.assertFalse(ok2)
        self.assertEqual(rep2["status_by_check"]["C08"], "FAIL")
        self.assertEqual(sorted(i["issue"] for i in rep2["checks"]["C08"]["details"]["issues"]),
                         ["crlf_line_endings"])
        self.assertEqual([i["issue"] for i in rep2["checks"]["C08"]["details"]["registered_deviations(info)"]["issues"]],
                         ["notion_title_mismatch"])

    def test_local_modified_c04_c07_c08(self):
        v = self.mutate_copy("v_lm_more")
        self.edit(v, CHILD_ONE, "notion_order: 1", "notion_order: 5")                          # C04
        self.edit(v, CHILD_ONE, "an escape \\\\ char\n",
                  "an escape \\\\ char\n\n![[General/_attachments/gone__deadbeef.png]]\n")    # C06 + C07 ref
        self.edit(v, "General/Home/Home.md", 'notion_icon: "🏠"', 'notion_icon: "🏡"')           # C08 icon (P1)
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C04", "C06", "C07", "C08"))
        lm = [{"notion_id": P1, "path": "General/Home/Home.md"}, {"notion_id": P2, "path": CHILD_ONE}]
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"local_modified": lm}))
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual({c: self.reg(rep, c) for c in ("C04", "C06", "C07", "C08")},
                         {"C04": 1, "C06": 1, "C07": 1, "C08": 1})
        self.assertEqual(list(rep["checks"]["C04"]["details"]["registered_deviations(info)"]), ["notion_order_mismatch"])
        self.assertEqual(list(rep["checks"]["C07"]["details"]["registered_deviations(info)"]), ["missing_references"])
        self.assertEqual([i["issue"] for i in rep["checks"]["C08"]["details"]["registered_deviations(info)"]["issues"]],
                         ["notion_icon_emoji_mismatch"])
        # P1 not listed: its icon still FAILs
        ok2, rep2, _ = self.run_wb(vault=v, dev=self.dev_file({"local_modified": lm[1:]}, "d2.json"), out_name="o2")
        self.assertFalse(ok2)
        self.assert_statuses(rep2, fail=("C08",))
        # a format defect of a listed local_modified note still FAILs
        self.edit(v, CHILD_ONE, "gone__deadbeef.png]]\n", "gone__deadbeef.png]]\n\n")
        ok3, rep3, _ = self.run_wb(vault=v, dev=self.dev_file({"local_modified": lm}, "d3.json"), out_name="o3")
        self.assertFalse(ok3)
        self.assert_statuses(rep3, fail=("C08",))
        self.assertEqual([i["issue"] for i in rep3["checks"]["C08"]["details"]["issues"]], ["not_single_trailing_newline"])

    def test_local_new_c09_collision_and_c07_duplicate_content(self):
        v = self.mutate_copy("v_ln_c09")
        mine = "General/Home/child one.md"                    # differs from the exported "Child One.md" by case
        att = "General/_attachments/my copy.png"            # same bytes as the exported attachment
        self.add_file(v, mine, "my own note\n")
        with open(os.path.join(v, *att.split("/")), "wb") as fh:
            fh.write(PNG)
        _ok, rep0, _ = self.run_wb(vault=v)
        self.assert_statuses(rep0, fail=("C02", "C07", "C08", "C09"))
        ok, rep, _ = self.run_wb(vault=v, dev=self.dev_file({"local_new": [mine, att]}))
        self.assertTrue(ok, {c: rep["checks"][c]["summary"] for c in ALL_CHECKS})
        self.assertEqual(list(rep["checks"]["C09"]["details"]["registered_deviations(info)"]), ["nfc_casefold_collisions"])
        self.assertEqual(sorted(rep["checks"]["C07"]["details"]["registered_deviations(info)"]),
                         ["bad_name", "duplicate_content_in_teamspace"])
        # only the image listed: the unlisted note's collision, C02 and C08 still FAIL
        ok2, rep2, _ = self.run_wb(vault=v, dev=self.dev_file({"local_new": [att]}, "d2.json"), out_name="o2")
        self.assertFalse(ok2)
        self.assert_statuses(rep2, fail=("C02", "C08", "C09"))


class TestSnapshotImmutableFlag(Fixture):
    """Read finished snapshots without changing their SQLite files. WAL data and races must be detected."""

    def quiescent_wal_copy(self):
        src = os.path.join(self.tmp, "walsrc", "snap.db")
        os.makedirs(os.path.dirname(src))
        writer = build_snapshot(src, wal=True)
        try:
            writer.execute("pragma wal_checkpoint(TRUNCATE)")
            self.assertEqual(os.path.getsize(src + "-wal"), 0)
            dst = os.path.join(self.tmp, "walq", "snap.db")
            os.makedirs(os.path.dirname(dst))
            for suf in ("", "-wal", "-shm"):  # like a production snapshot: 0-byte -wal and a -shm left behind
                shutil.copy2(src + suf, dst + suf)
        finally:
            writer.close()
        return dst

    def snapshot_state(self, p):
        out = {}
        for suf in ("", "-wal", "-shm", "-journal"):
            if os.path.exists(p + suf):
                st = os.stat(p + suf)
                with open(p + suf, "rb") as fh:
                    out[suf] = (st.st_size, st.st_mtime_ns, hashlib.sha256(fh.read()).hexdigest())
        return out

    def test_empty_wal_uses_immutable_with_and_without_flag(self):
        p = self.quiescent_wal_copy()
        before = self.snapshot_state(p)
        self.assertEqual(vv.Expectation(p, self.sidebar, self.roots).open_mode, "mode=ro&immutable=1")
        exp = vv.Expectation(p, self.sidebar, self.roots, snapshot_immutable=True)
        self.assertEqual(exp.open_mode, "mode=ro&immutable=1")
        self.assertEqual(len(exp.objs), 18)
        self.assertFalse(any("live" in w for w in exp.warnings))
        self.assertEqual(self.snapshot_state(p), before)
        out = os.path.join(self.tmp, "out_imm")
        ok, rep, _ = vv.verify(p, self.sidebar, self.vault, out, self.roots, snapshot_immutable=True)
        self.assertTrue(ok, {k: r["summary"] for k, r in rep["checks"].items() if r["status"] != "PASS"})
        self.assertEqual(rep["inputs"]["snapshot_open_mode"], "mode=ro&immutable=1")
        self.assertEqual(self.snapshot_state(p), before)
        self.assertEqual(sorted(os.listdir(os.path.dirname(p))), ["snap.db", "snap.db-shm", "snap.db-wal"])
        args = ["--snapshot", p, "--sidebar", self.sidebar, "--roots-kinds", self.roots, "--vault", self.vault,
                "--out", os.path.join(self.tmp, "out_imm_cli"), "--snapshot-immutable"]
        self.assertEqual(vv.main(args), 0)

    def test_verify_partial_sidecars_in_readonly_directory(self):
        src = self.quiescent_wal_copy()
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                folder = os.path.join(self.tmp, suffix)
                os.mkdir(folder)
                path = os.path.join(folder, "closed + #?%.db")
                shutil.copy2(src, path)
                if suffix == "-journal":
                    open(path + suffix, "xb").close()
                else:
                    shutil.copy2(src + suffix, path + suffix)
                link = os.path.join(self.tmp, "alias" + suffix)
                os.symlink(path, link)
                before = self.snapshot_state(path)
                os.chmod(folder, 0o555)
                try:
                    ok, report, _ = vv.verify(link, self.sidebar, self.vault, os.path.join(self.tmp, "out" + suffix),
                                              self.roots, snapshot_immutable=True)
                finally:
                    os.chmod(folder, 0o755)
                self.assertTrue(ok, report["checks"])
                self.assertEqual(report["inputs"]["snapshot_open_mode"], "mode=ro&immutable=1")
                self.assertFalse(any("snapshot is live" in w for w in report.get("warnings", [])))
                self.assertEqual(self.snapshot_state(path), before)

    def test_wal_receives_first_commit_during_automatic_immutable_read(self):
        path = self.quiescent_wal_copy()
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        self.assertEqual(os.stat(path + "-wal").st_size, 0)
        load = vv.Expectation._load_snapshot

        def load_then_commit(exp):
            load(exp)
            writer.execute("CREATE TABLE raced(value)")
            writer.commit()
            self.assertGreater(os.stat(path + "-wal").st_size, 0)

        with mock.patch.object(vv.Expectation, "_load_snapshot", load_then_commit):
            with self.assertRaisesRegex(vv.InputError, "changed while it was being read"):
                vv.Expectation(path, self.sidebar, self.roots)

    def test_export_refuses_wal_created_during_automatic_immutable_read(self):
        import export_obsidian

        path = self.quiescent_wal_copy()
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        self.assertEqual(os.stat(path + "-wal").st_size, 0)
        load = export_obsidian.Exporter.load

        def load_then_commit(exporter):
            load(exporter)
            writer.execute("CREATE TABLE raced(value)")
            writer.commit()
            self.assertGreater(os.stat(path + "-wal").st_size, 0)

        with mock.patch.object(export_obsidian.Exporter, "load", load_then_commit):
            with self.assertRaisesRegex(SystemExit, "changed while it was being read"):
                export_obsidian.Exporter(path, self.sidebar, "sample", os.path.join(self.tmp, "export.db"),
                                         os.path.join(self.tmp, "export"), roots_kinds=self.roots)

    def test_wal_with_data_or_change_during_read_is_refused(self):
        p = os.path.join(self.tmp, "live", "snap.db")
        os.makedirs(os.path.dirname(p))
        writer = build_snapshot(p, wal=True)  # rows still only in the -wal
        try:
            self.assertGreater(os.path.getsize(p + "-wal"), 0)
            with self.assertRaises(vv.InputError):
                vv.Expectation(p, self.sidebar, self.roots, snapshot_immutable=True)
            args = ["--snapshot", p, "--sidebar", self.sidebar, "--roots-kinds", self.roots, "--vault", self.vault,
                    "--out", os.path.join(self.tmp, "out_live_imm"), "--snapshot-immutable"]
            self.assertEqual(vv.main(args), 2)
        finally:
            writer.close()
        sigs = iter([(("", 1, 1),), (("", 2, 2),)])
        with mock.patch.object(vv, "snapshot_signature", lambda _p: next(sigs)):
            with self.assertRaises(vv.InputError) as cm:
                vv.Expectation(self.snap, self.sidebar, self.roots, snapshot_immutable=True)
        self.assertIn("changed while it was being read", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
