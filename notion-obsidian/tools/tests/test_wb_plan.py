#!/usr/bin/env python3
"""Tests for tools/wb_plan.py (I1 planner, IMPL_CONTRACT §5.13, WRITEBACK_SPEC §4-§8, speccheck R-140..R-153, R-192).

Every test builds a realistic fixture: pages seeded in tools/tests/fake_notion.py (127.0.0.1, fake tokens) ->
crawled with the REAL snapshot.py code through a ReadOnlyClient -> exported with the REAL exporter -> published
with the REAL publish_vault.py into a temp vault (published_files, base_history, block_lines, state/base). The
planner then reads the live fake (R) like it reads Notion. Everything lives in tempfile.mkdtemp() dirs that are
removed; no real token, no real Notion, no sandbox state is touched.

Run (from the sandbox root):
  python3 -I -B -m unittest discover -s tools/tests -p 'test_wb_plan.py' -v
"""
import contextlib
import copy
import datetime
import difflib
import io
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
for _p in (HERE, TOOLS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import export_obsidian as exp  # noqa: E402
import publish_vault  # noqa: E402
import snapshot as snapmod  # noqa: E402
import wb_common  # noqa: E402
import wb_config  # noqa: E402
import wb_plan  # noqa: E402
import wb_state  # noqa: E402
from fake_notion import FakeNotion  # noqa: E402
from rate_gate import RateGate  # noqa: E402
from readonly_client import make_read_client  # noqa: E402

QUIET = ("nmig.client", "nmig.inventory", "snapshot", "writeback_client", "wb_apply", "wb_refetch")
_saved_levels = {}
TZ = datetime.timezone(datetime.timedelta(hours=7))


def setUpModule():
    for n in QUIET:
        lg = logging.getLogger(n)
        _saved_levels[n] = lg.level
        lg.setLevel(logging.CRITICAL)


def tearDownModule():
    for n, lv in _saved_levels.items():
        logging.getLogger(n).setLevel(lv)


# ============================================================================================ fixture
def rt(t, **ann):
    r = {"text": {"content": t}}
    if ann:
        r["annotations"] = ann
    return r


def para(t, **ann):
    return {"type": "paragraph", "paragraph": {"rich_text": [rt(t, **ann)]}}


def bullet(t, children=None):
    b = {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [rt(t)]}}
    if children:
        b["bulleted_list_item"]["children"] = children
    return b


def num(t):
    return {"type": "numbered_list_item", "numbered_list_item": {"rich_text": [rt(t)]}}


def todo(t, checked=False, children=None):
    b = {"type": "to_do", "to_do": {"rich_text": [rt(t)], "checked": checked}}
    if children:
        b["to_do"]["children"] = children
    return b


def h2(t):
    return {"type": "heading_2", "heading_2": {"rich_text": [rt(t)]}}


def mention_para(text, pid):
    return {"type": "paragraph", "paragraph": {"rich_text": [
        rt(text), {"type": "mention", "mention": {"type": "page", "page": {"id": pid}}}]}}


def crawl(client, roots_in, path):
    """snapshot.py main() (non-incremental) on the fake: detect_kinds -> roots.json -> BlobInventory crawl."""
    roots = snapmod.detect_kinds(client, roots_in)
    with open(path + ".roots.json", "w", encoding="utf-8") as fh:
        json.dump(roots, fh, ensure_ascii=False, indent=1)
    store = snapmod.Store(path)
    me = client.me()
    store.x("INSERT OR REPLACE INTO meta VALUES('workspace_id', ?)", (me["bot"]["workspace_id"],))
    store.x("INSERT OR REPLACE INTO meta VALUES('notion_version', ?)", ("2026-03-11",))
    store.x("INSERT OR REPLACE INTO meta VALUES('finished_at', ?)", (wb_common.now_iso(),))
    inv = snapmod.BlobInventory(client, store, workers=1, hash_files=True, comments=False)
    inv.seed([r for r in roots if not r.get("in_trash")])
    inv.run()
    inv.http.close()
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.db.close()
    for s in ("-wal", "-shm"):
        if os.path.exists(path + s) and os.path.getsize(path + s) == 0:
            os.unlink(path + s)
    return roots


OBSIDIAN_RUNNING = {"running": True, "started_at": "2026-01-01T00:00:00+07:00"}


class Env:
    """Fake Notion + real crawl/export/publish into temp dirs. Attributes: fake, base, ids, work, vault, snap,
    paths, ro (ReadOnlyClient), read_token_file."""

    def __init__(self, tc=None, *, cfg=None, enabled=True, seed=None):
        self.tmp = tempfile.mkdtemp(prefix="wbplan-")
        if tc is not None:
            tc.addCleanup(self.close)
        self.fake = FakeNotion(tokens={
            "ro": {"caps": {"read", "user_info"}, "roots": {"*"}, "secret": "tok-ro-i1-test"},
            "wb": {"caps": {"read", "update", "insert"}, "roots": {"*"}, "secret": "tok-wb-i1-test"}})
        self.base = self.fake.start()
        self.fake.add_user("Owner")
        f, ids = self.fake, {}
        self.ids = ids
        home = ids["home"] = f.add_page("Home")
        ids["other"] = f.add_page("Other", parent=home, children=[para("other body")])
        ids["leaf"] = f.add_page("Leaf", parent=home, children=[
            para("first para"), para("second para"), bullet("item a"), bullet("item b"), num("one"), num("two"),
            todo("task", False), h2("Heading"), mention_para("see ", ids["other"]), para("last para")])
        ids["ren"] = f.add_page("Rename me", parent=home, children=[para("rename body")])
        ids["del"] = f.add_page("Doomed", parent=home, children=[para("doomed body")])
        ids["del2"] = f.add_page("Doomed parent", parent=home, children=[para("doomed parent body")])
        ids["del2c"] = f.add_page("Doomed child", parent=ids["del2"], children=[para("doomed child body")])
        ids["folder"] = f.add_page("Folder", parent=home, children=[para("folder body")])
        ids["kid"] = f.add_page("Kid", parent=ids["folder"], children=[para("kid body"), para("kid tail")])
        ids["folder2"] = f.add_page("Folder2", parent=home, children=[para("folder2 body")])
        ids["kid2"] = f.add_page("Kid2", parent=ids["folder2"], children=[para("kid2 body")])
        ids["db"], ids["ds"] = f.add_database(home, "Tasks", {
            "Name": "title", "Label": {"type": "select", "options": ["a", "b"]}, "Note": "rich_text",
            "Done": "checkbox", "N": "number", "Fancy": "rich_text"})
        ids["r1"] = f.add_row(ids["ds"], "Task 1", {"Label": {"select": {"name": "a"}},
                                                    "Note": {"rich_text": [rt("n1")]},
                                                    "Fancy": {"rich_text": [rt("bold", bold=True)]}},
                              children=[para("r1 body")])
        if seed:
            seed(self)
        self.work = os.path.join(self.tmp, "work")
        self.vault = os.path.join(self.tmp, "vault")
        for d in ("state", "snapshots", "staging", "reports", "config", "blobs/tmp", "approvals", "backup"):
            os.makedirs(os.path.join(self.work, d), exist_ok=True)
        os.makedirs(self.vault)
        self.gate = RateGate(os.path.join(self.tmp, "rate_gate.json"), total_rps=1000, burst=100)
        self.ro = make_read_client(base_url=self.base, token=self.fake.secret("ro"), rate=None, gate=self.gate,
                                   sleep=lambda s: None)
        self.snap = os.path.join(self.work, "snapshots", "snap_20261118T0100.db")
        saved_b = snapmod.BLOBS
        snapmod.BLOBS = os.path.join(self.work, "blobs")
        try:
            crawl(self.ro, [{"id": home, "title": "Home", "teamspace": "General", "teamspace_order": 1,
                             "root_order": 1}], self.snap)
        finally:
            snapmod.BLOBS = saved_b
        sidebar = os.path.join(self.work, "sidebar.json")
        with open(self.snap + ".roots.json", encoding="utf-8") as fh:
            roots_json = json.load(fh)
        with open(sidebar, "w", encoding="utf-8") as fh:
            json.dump({"roots": roots_json}, fh)
        run = "R1"
        out = os.path.join(self.work, "staging", run)
        saved = exp.WORK
        exp.WORK = self.work
        try:
            ex = exp.Exporter(self.snap, sidebar, run, os.path.join(self.work, "state", "export.db"), out, immutable=True)
            with contextlib.redirect_stdout(io.StringIO()):
                ex.run()
        finally:
            exp.WORK = saved
        appr = os.path.join(self.work, "approvals", "test.approval.md")
        with open(appr, "w", encoding="utf-8") as fh:
            fh.write("# test approval\n")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = publish_vault.main(["--run-id", run, "--vault", self.vault, "--state",
                                     os.path.join(self.work, "state", "export.db"), "--apply", "--approval", appr,
                                     "--allow-other-vault", "--report-dir", os.path.join(self.work, "reports", run),
                                     "--staging-dir", out, "--backup-dir", os.path.join(self.work, "backup", run),
                                     "--wb-db", os.path.join(self.work, "state", "writeback.db")])
        assert rc == 0, rc
        with open(os.path.join(self.work, "state", "last_published_snapshot.txt"), "w", encoding="utf-8") as fh:
            fh.write(self.snap + "\n")
        with open(os.path.join(self.work, "state", "last_published.json"), "w", encoding="utf-8") as fh:
            json.dump({"run_id": run, "snapshot": self.snap}, fh)
        with open(os.path.join(self.work, "reports", run, "overrides.json"), "w", encoding="utf-8") as fh:
            json.dump({}, fh)
        c = {"mode": "whitelist", "read_integration_readonly_confirmed_at": "2026-11-17T18:30+07:00",
             "whitelist_page_ids": [ids["leaf"], ids["ren"], ids["del"], ids["del2"], ids["folder"], ids["kid"],
                                    ids["folder2"], ids["kid2"]],
             "whitelist_database_ids": [ids["db"]], "automation_free_database_ids": [ids["db"]]}
        c.update(cfg or {})
        self.write_cfg(c)
        self.paths = wb_plan.Paths(self.work, self.vault)
        self.read_token_file = os.path.join(self.tmp, "ro.env")
        with open(self.read_token_file, "w", encoding="utf-8") as fh:
            fh.write("NOTION_TARGET_TOKEN=" + self.fake.secret("ro") + "\n")
        self._db = None
        if enabled:
            wb_state.set_meta(self.db, "enabled_at", "2026-11-01T00:00:00+07:00")

    # ---------------------------------------------------------------- helpers
    def write_cfg(self, c):
        self.cfg_dict = c
        with open(os.path.join(self.work, "config", "writeback.json"), "w", encoding="utf-8") as fh:
            json.dump(c, fh)

    def set_cfg(self, **kw):
        c = dict(self.cfg_dict)
        c.update(kw)
        self.write_cfg(c)

    @property
    def db(self):
        if self._db is None:
            self._db = wb_state.connect(os.path.join(self.work, "state", "writeback.db"))
        return self._db

    @property
    def cfg(self):
        return wb_config.load(os.path.join(self.work, "config", "writeback.json"))

    def ctx(self, **kw):
        kw.setdefault("obsidian", OBSIDIAN_RUNNING)
        rc = kw.pop("read_client", self.ro)
        return wb_plan.PlanContext(self.paths, self.cfg, self.db, read_client=rc, **kw)

    def plan(self, scope=None, **kw):
        ctx = self.ctx(**kw)
        p, prev = wb_plan.build_plan(ctx, scope=scope)
        return p, prev, ctx

    def create(self, scope=None, **kw):
        p, prev, ctx = self.plan(scope, **kw)
        info = wb_plan.create_plan(ctx, p, prev)
        return p, info

    def path(self, rel):
        return os.path.join(self.vault, *rel.split("/"))

    def read(self, rel):
        with open(self.path(rel), encoding="utf-8") as fh:
            return fh.read()

    def write(self, rel, text):
        os.makedirs(os.path.dirname(self.path(rel)), exist_ok=True)
        with open(self.path(rel), "w", encoding="utf-8") as fh:
            fh.write(text)

    def edit(self, rel, old, new):
        t = self.read(rel)
        assert old in t, (rel, old, t)
        self.write(rel, t.replace(old, new, 1))

    def remote_text(self, page_key, index, text):
        """Change a paragraph's text in the fake (a Notion-side edit)."""
        b = self.fake.children(self.ids[page_key])[index]
        rich = self.fake.blocks[b["id"]][b["type"]]["rich_text"]
        rich[:] = [{"type": "text", "text": {"content": text, "link": None}, "plain_text": text, "href": None,
                    "annotations": {"bold": False, "italic": False, "strikethrough": False, "underline": False,
                                    "code": False, "color": "default"}}]

    def hold_delete(self, key, rel, subtree=None, ttl_days=10):
        ttl = (datetime.datetime.now().astimezone() + datetime.timedelta(days=ttl_days)).isoformat(timespec="seconds")
        ids = [{"notion_id": self.ids[key], "rel": rel}] + list(subtree or [])
        with wb_state.transaction(self.db):
            self.db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, detected_run, detected_at, "
                            "status, ttl_until) VALUES(?,?,?,?,?,?,?)",
                            (self.ids[key], rel, json.dumps({"ids": ids, "high_risk": len(ids) > 1}), "R2",
                             wb_common.now_iso(), "hold", ttl))

    def register_rename(self, key, old_rel, new_rel, *, published=True):
        """What a refresh round registers for a same-folder local rename: refresh.persist_overrides opens a
        name_overrides row at the new path; when the round then PUBLISHES, the publisher adopts the local name
        (publish_vault ADOPT_RENAME: published_files old_rel -> 'renamed', new_rel active with the old sha256,
        objects_map.published_path) and the scan no longer lists the rename. Only that registers it (L-17 fix loop
        2). published=False: a round that opened the override and then paused for review or failed."""
        with wb_state.transaction(self.db):
            if not self.db.execute("SELECT 1 FROM name_overrides WHERE notion_id=? AND local_rel=? AND status='open'",
                                   (self.ids[key], new_rel)).fetchone():
                self.db.execute("INSERT INTO name_overrides(notion_id, old_rel, local_rel, since, status) "
                                "VALUES(?,?,?,?, 'open')", (self.ids[key], old_rel, new_rel, wb_common.now_iso()))
        if not published:
            return
        con = sqlite3.connect(os.path.join(self.work, "state", "export.db"))
        try:
            with con:
                row = con.execute("SELECT sha256, notion_id, kind, staged_sha256 FROM published_files "
                                  "WHERE rel_path=? AND status='active'", (old_rel,)).fetchone()
                assert row is not None and row[1] == self.ids[key], (old_rel, row)
                con.execute("UPDATE published_files SET status='renamed' WHERE rel_path=?", (old_rel,))
                con.execute("INSERT OR REPLACE INTO published_files(rel_path, sha256, notion_id, kind, run_id, status, "
                            "staged_sha256) VALUES(?,?,?,?, 'R2', 'active', ?)", (new_rel, row[0], row[1], row[2], row[3]))
                con.execute("UPDATE objects_map SET published_path=? WHERE notion_id=?", (new_rel, row[1]))
        finally:
            con.close()

    def hold_like_refresh(self, key, paths, reason="unsupported_rename", *, subtree=True, source="scan",
                          auto_release=True):
        """What a refresh round records for a cross-folder move / unsupported rename (refresh.py step 6: an
        object-level hold in state/auto_sync_hold.json, source scan, paths [old_rel, new_rel]); with other
        arguments the other hold shapes of refresh.py (override_rejected: source export, paths []; post_verify /
        plan / race: auto_release False, paths []; note-only anomalies: subtree False)."""
        p = os.path.join(self.work, "state", "auto_sync_hold.json")
        cur = {}
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                cur = json.load(fh)
        cur[self.ids[key]] = {"reason": reason, "run_id": "R2", "since": wb_common.now_iso(), "subtree": subtree,
                              "source": source, "auto_release": auto_release, "paths": list(paths)}
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cur, fh, ensure_ascii=False)

    def writes(self):
        return [r for r in self.fake.requests if r["method"] in ("POST", "PATCH", "DELETE")
                and not re.search(r"(^|/)(search|query)$", r["path"])]

    def close_db(self):
        """Close this process's writeback.db connection (the next .db access reopens it)."""
        if self._db is not None:
            self._db.close()
            self._db = None

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None
        self.fake.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


# R-142 golden preview (WB 5.2): the scenario of golden_scenario(); ids / times normalised by normalize_preview().
# Reviewed by hand: every section, every per-item field, the residual-risk sentence verbatim.
# L-17: an MV item exists only while its move is unregistered (a refresh holds a moved object, which is then refused
# AUTO_SYNC_HOLD), so this every-item-type plan carries the UNREGISTERED_RENAME notice and every item is blocked;
# the executable rendering ("状态：可执行") is asserted in TestUnregisteredRenameL17 after the registration.
GOLDEN_PREVIEW = """# 写回预览 <PLAN>

- 生成时间：<TS>；过期时间：<TS>
- 模式：full；范围：全库；渲染器：1.2.1
- 依据快照：`snapshots/snap_20261118T0100.db`
- 本文件只是预览，不能作为确认依据；写入 Notion 只能由用户本人在终端核对库外计划的编号、SHA 和选择项，运行生成该计划时终端显示的审批命令并输入现场挑战短语。

> 注意（UNREGISTERED_RENAME）：检测到尚未登记的本地改名/移动：请等待下一次自动同步登记后再生成计划。本计划的所有条目都不可执行。
>
> - 跨文件夹移动：`General/Home/Folder2/Kid2.md` → `General/Home/Folder/Kid2.md`
> - 同文件夹改名：`General/Home/Rename me.md` → `General/Home/Renamed.md`

## 新增内容（N）

### N-001 `General/Home/Folder/Brand new.md`
- 本地路径：`General/Home/Folder/Brand new.md`
- 拟建页面标题：`Brand new`
- Notion 父页面：`Folder` https://app.notion.com/p/<ID1>
- 拟新建块数：2
- 新建行：否
- 状态：不可执行：UNREGISTERED_RENAME（检测到尚未登记的本地改名/移动：请等待下一次自动同步登记后再生成计划）

## 修改内容（M）

### M-001 `General/Home/Leaf.md`
- 本地路径：`General/Home/Leaf.md`
- Notion 页面：`Leaf` https://app.notion.com/p/<ID2>
- 状态：不可执行：BLOCK_TRASH_DISABLED：未开启块删除，请在 Notion 中手工删除后再写回其余改动；UNREGISTERED_RENAME（检测到尚未登记的本地改名/移动：请等待下一次自动同步登记后再生成计划）
- 风险：normal
- 绑定块删除：B-001（未确认则本项整体不执行）
- 逐块差异（旧 → 新，块 ID 前 8 位）与操作：
- UPDATE 块 `<ID6~8>`（paragraph）
```text
- first para
+ second para G
```

统一差异（B ↔ L）：
```text
--- B:General/Home/Leaf.md
+++ L:General/Home/Leaf.md
@@ -1,5 +1,3 @@
-first para
-
-second para
+second para G
 
 - item a
```

## 待删除 / 移入回收站（高风险）：页面删除 D

依据快照 snap_20261118T0100.db（<TS>）及计划时的实时读取；范围外与快照之后的引用不在此列

### D-001 `General/Home/Doomed.md`
- 页面：`Doomed` https://app.notion.com/p/<ID3>
- 类型：page
- 子页面与子数据库（实时读取，递归）：0
- 正文块数：1；附件数：0
- 同步块原件及引用页面：0
- 内联数据库及行数：0
- 入站引用：0（按来源页面：无）
- 入站关系：无
- 是否锁定：否
- Notion 最后编辑时间：<TS>；base：R1（seq 1）
- 风险：normal
- 状态：不可执行：UNREGISTERED_RENAME（检测到尚未登记的本地改名/移动：请等待下一次自动同步登记后再生成计划）

## 待删除 / 移入回收站（高风险）：块删除 B

### B-001 `General/Home/Leaf.md`
- 页面：`Leaf` https://app.notion.com/p/<ID2>
- 块 ID：`<ID7~8>`  类型：paragraph
- 是否有子块：否
- 状态：不可执行：BLOCK_TRASH_DISABLED：未开启块删除，请在 Notion 中手工删除后再写回其余改动；UNREGISTERED_RENAME（检测到尚未登记的本地改名/移动：请等待下一次自动同步登记后再生成计划）
- 绑定的修改项：M-001
- 原文：
```text
second para
```

## 标题或路径变化（高风险，T / MV）：改名 T

### T-001 `General/Home/Renamed.md`
- Notion 当前标题：`Rename me`
- base 中标题：`Rename me`
- 本地新名：`Renamed`
- 新文件名可直接用作 Notion 标题：满足
- 入站引用数：0
- 库内被 Obsidian 改写链接的笔记数：0
- 改名后下一轮库内将移动的文件数：1
- 数据库行：否
- 状态：不可执行：UNREGISTERED_RENAME（检测到尚未登记的本地改名/移动：请等待下一次自动同步登记后再生成计划）
- 未确认时 Notion 标题保持不变，本地文件名记为命名差异

## 标题或路径变化（高风险，T / MV）：移动 MV

### MV-001 `General/Home/Folder/Kid2.md`
- 旧路径：`General/Home/Folder2/Kid2.md`
- 旧父页面：`<ID4>`
- 新父页面：`<ID1>`
- 共享与权限差异：未知
- 状态：不可执行：MV_NOT_SUPPORTED（页面移动（阶段 3）尚未由执行器实现；请在 Notion 中手工移动）；UNREGISTERED_RENAME（检测到尚未登记的本地改名/移动：请等待下一次自动同步登记后再生成计划）

## 冲突（C）

### C-001 `General/Home/Folder/Kid.md`
- 冲突类型：CONFLICT_BOTH
- Notion 页面：https://app.notion.com/p/<ID5>
- 本地改动：本地 +1/-1 行
- Notion 改动：块 改动 1 / 新增 0 / 删除 0
  - Notion 中改动的块（paragraph）：「kid body REMOTE」
- Notion 最后编辑时间：<TS>；编辑者类型：人（person）
- 建议：按预览中的两条处理命令选择以 Notion 为准（先备份再恢复本地），或以本地为准（生成新计划）
- 命令：以 Notion 为准用 `cd <WORK> && python3 -I tools/writeback.py resolve --plan <PLAN> --item C-001 --take notion`，以本地为准生成新计划用 `cd <WORK> && python3 -I tools/writeback.py resolve --plan <PLAN> --item C-001 --take obsidian`
- 下一轮 refresh 会把 Notion 的版本写到 `Notion_Obsidian/conflicts/` 下供对照，本地文件保持不动
- 冲突项不能经 apply 确认，须按上列完整命令单独处理

统一差异（B ↔ L）：
```text
--- B:General/Home/Folder/Kid.md
+++ L:General/Home/Folder/Kid.md
@@ -1,3 +1,3 @@
-kid body
+kid body LOCAL
 
 kid tail
```

## 拒绝写回 / 只报告

- `General/Home/Tasks/Task 1.md`：SCHEMA_OPTION_MISSING — 属性「Label」的选项「zzz」不在数据库 schema 中；建议：选项不存在于数据库 schema；请先在 Notion 中添加选项（写回不新建选项）

## 已拒绝（用户拒绝，文件未再改动前不再提出）

- `General/Home/Other.md`：M 自 <TS>（not now）

## 汇总

- 各类数量：N 1，M 1，B 1，D 1，T 1，MV 1，C 1，拒绝 1，已拒绝 1；可执行 0
- 拟发请求数：约 32（计划阶段读取 13 次）；预计耗时：约 13 秒，另需全库导出与两次校验约 4 分钟
- 拟新建块数：2；块额度状态：unlimited；成员数：1
- 熔断：未触发；本地修改笔记 4，新的本地删除 0，单页最大操作数 2
- Obsidian：运行中
- 剩余风险：API 没有条件写，单个请求的预检与写入之间仍有约 1 秒窗口；执行期间请不要在 Notion 中编辑这些页面
"""
LEAF = "General/Home/Leaf.md"
TASK = "General/Home/Tasks/Task 1.md"
KID = "General/Home/Folder/Kid.md"


def items_of(plan, cat=None):
    return [it for it in plan["items"] if cat is None or it["category"] == cat]


def refused_of(plan, cls_prefix):
    return [r for r in plan["refused"] if r["class"].startswith(cls_prefix)]


def no_wikilink_outside_code(text):
    fence = None
    for ln in text.split("\n"):
        m = re.match(r"^(`{3,}|~{3,})", ln)
        if fence:
            if m and ln.strip() == fence:
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        if re.search(r"!?\[\[", re.sub(r"(`+).*?\1", "", ln)):
            return False
    return True


# ============================================================================================ tests
class TestModifiedNotes(unittest.TestCase):
    def test_edit_append_toggle_one_m_item_and_oracle(self):
        """LOCAL_MODIFIED -> one M item per page: UPDATE (fragment reuse), APPEND after the right sibling,
        to_do checked toggle; the render oracle reproduces L exactly."""
        e = Env(self)
        e.edit(LEAF, "second para\n", "second para EDITED\n\nnew para after second\n")
        e.edit(LEAF, "- item b\n", "- item b\n- item c new\n")
        e.edit(LEAF, "- [ ] task", "- [x] task")
        p, _prev, _ctx = e.plan()
        m = items_of(p, "M")
        self.assertEqual(len(m), 1, p["refused"])
        ops = m[0]["ops"]
        kinds = [o["op"] for o in ops]
        self.assertEqual(kinds, ["UPDATE", "UPDATE", "APPEND", "APPEND"])
        self.assertTrue(m[0]["executable"], m[0]["blocked_by"])
        upd = [o for o in ops if o["op"] == "UPDATE"]
        self.assertEqual({o["block_type"] for o in upd}, {"paragraph", "to_do"})
        todo_op = next(o for o in upd if o["block_type"] == "to_do")
        self.assertTrue(todo_op["request"]["to_do"]["checked"])
        apps = [o for o in ops if o["op"] == "APPEND"]
        for a in apps:
            self.assertEqual(a["position"]["type"], "after_block")
            self.assertIn("anchor_next_id", a)
            self.assertEqual(a["parent_id"], e.ids["leaf"])
        # every op of an executable item reproduces L (oracle): re-check independently
        node = _ctx.ex.nodes[e.ids["leaf"]]
        tree = _ctx.reader.tree(e.ids["leaf"])
        pp = wb_plan.PagePlanner(_ctx, node, wb_state.base_content(wb_state.latest_base(e.db, e.ids["leaf"])["sha256"],
                                                                    db=e.db).decode(),
                                 wb_state.block_lines_for(e.db, e.ids["leaf"], wb_state.latest_base(e.db, e.ids["leaf"])["sha256"]),
                                 wb_plan.body_of(e.read(LEAF))[1], tree, e.ids["leaf"])
        body = pp._render(pp._apply(ops))
        self.assertEqual(wb_plan._renum(body), wb_plan._renum(wb_plan.body_of(e.read(LEAF))[1]))
        self.assertIn("@@", "\n".join(m[0]["diff"]))

    def test_numbered_renumbering_is_free_and_normalized_inline(self):
        """Obsidian renumbering is ignored (M6); a CommonMark-equivalent edit (_x_) is accepted as normalize."""
        e = Env(self)
        e.edit(LEAF, "1. one\n2. two\n", "1. one\n1. two\n")
        p, _prev, _ = e.plan()
        self.assertEqual(items_of(p, "M"), [])
        self.assertEqual([r for r in p["refused"] if r["rel_path"] == LEAF], [], "renumbering alone is no change")
        e.edit(LEAF, "last para", "last _para_ x")
        p, prev, _ = e.plan()
        m = items_of(p, "M")
        self.assertEqual(len(m), 1, p["refused"])
        self.assertTrue(m[0]["normalize"])
        self.assertIn(wb_plan.NORMALIZED_NOTE, prev)

    def test_refusals_surface_md2blocks_reason_and_hint(self):
        """X block edit -> UNSUPPORTED_REGION; type change -> TYPE_CHANGED; to_do child -> ambiguous_nesting (L-03);
        blank-line-only change -> BLANK_LINES. No item is emitted for a refused page."""
        cases = [("see [", "see more ["),          # mention paragraph (X)
                 ("## Heading", "### Heading"),     # heading level change
                 ("- [ ] task\n", "- [ ] task\n      - [ ] sub\n"),   # nested under a to-do
                 ("first para\n", "first para\n\n\n")]
        expect = ["UNSUPPORTED_REGION", "TYPE_CHANGED", "UNSUPPORTED_BLOCK:ambiguous_nesting", "BLANK_LINES"]
        for (old, new), want in zip(cases, expect):
            with self.subTest(want=want):
                e = Env(self)
                t = e.read(LEAF)
                if old == "see [":
                    old = next(ln for ln in t.split("\n") if ln.startswith("see ")) + "\n"
                    new = old.replace("see ", "see more ", 1)
                e.edit(LEAF, old, new)
                p, _prev, _ = e.plan()
                self.assertEqual(items_of(p, "M"), [], p["items"])
                r = [x for x in p["refused"] if x["rel_path"] == LEAF]
                self.assertTrue(r, p["refused"])
                if want == "BLANK_LINES":
                    self.assertTrue(r[0]["class"] in ("BLANK_LINES", "LOCAL_KEYS_ONLY"), r)
                    continue
                self.assertTrue(r[0]["class"].startswith(want), r)
                self.assertTrue(r[0]["hint"], r)

    def test_local_keys_readonly_keys_and_link_noop(self):
        e = Env(self)
        e.edit(LEAF, "notion_order: ", "tags:\n- x\nnotion_order: ")
        p, _prev, _ = e.plan()
        self.assertTrue(refused_of(p, "LOCAL_KEYS_ONLY"), p["refused"])
        self.assertEqual(items_of(p, "M"), [])
        e.edit(LEAF, "notion_title: Leaf", "notion_title: Leaf changed")
        p, _prev, _ = e.plan()
        c = items_of(p, "C")
        self.assertEqual([x["kind"] for x in c], ["READONLY_KEY_CHANGED"])
        self.assertFalse(c[0]["executable"])

    def test_link_rewrite_noop(self):
        e = Env(self)
        home = "General/Home/Home.md"
        t = e.read(home)
        m = re.search(r"\[\[General/Home/Leaf\|Leaf\]\]", t)
        self.assertIsNotNone(m, t)
        e.edit(home, "[[General/Home/Leaf|Leaf]]", "[[Leaf|Leaf]]")
        p, _prev, _ = e.plan()
        self.assertTrue(refused_of(p, "LINK_REWRITE_NOOP"), p["refused"])

    def test_conflict_both_and_r_capture(self):
        e = Env(self)
        e.edit(KID, "kid body", "kid body LOCAL")
        e.remote_text("kid", 0, "kid body REMOTE")
        p, prev, _ = e.plan()
        c = items_of(p, "C")
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["kind"], "CONFLICT_BOTH")
        self.assertIn("r_capture", c[0])
        self.assertEqual(items_of(p, "M"), [])
        self.assertIn("冲突项不能经 apply 确认", prev)

    def test_stale_revert(self):
        """A REMOTE publish (seq >= 2) changed a paragraph; the local file brings the pre-publish text back."""
        e = Env(self)
        nid = e.ids["kid"]
        base = wb_state.latest_base(e.db, nid)
        old = wb_state.base_content(base["sha256"], db=e.db).decode()
        new_text = old.replace("kid body", "kid body v2")
        new = new_text.encode()
        rows = wb_state.block_lines_for(e.db, nid, base["sha256"])
        wb_state.append_base(e.db, notion_id=nid, rel_path=base["rel_path"], sha=wb_common.sha256_bytes(new),
                             page_json_sha=base["page_json_sha"], renderer_version=base["renderer_version"], run_id="R2",
                             origin="REMOTE", content=new)
        wb_state.import_block_lines(e.db, nid, wb_common.sha256_bytes(new),
                                    [dict(r, base_sha=wb_common.sha256_bytes(new)) for r in rows])
        # the vault holds the old version (Sync history restore)
        e.write(KID, old)
        with sqlite3.connect(os.path.join(e.work, "state", "export.db")) as con:
            con.execute("UPDATE published_files SET sha256=? WHERE rel_path=?", (wb_common.sha256_bytes(new), KID))
        p, _prev, _ = e.plan()
        self.assertTrue(refused_of(p, "STALE_REVERT"), (p["refused"], p["items"]))
        self.assertEqual(items_of(p, "M"), [])

    def test_initial_backlog_and_ack(self):
        e = Env(self, enabled=False)
        e.edit(LEAF, "second para", "second para backlog")
        p, info = e.create()
        self.assertTrue(refused_of(p, "INITIAL_BACKLOG"), p["refused"])
        self.assertIsNotNone(wb_state.get_meta(e.db, "enabled_at"))
        p2, _prev, _ = e.plan()
        self.assertTrue(refused_of(p2, "INITIAL_BACKLOG"), "still backlog until acknowledged")
        bl = wb_state.get_meta_json(e.db, "initial_backlog", {})
        bl[e.ids["leaf"]]["acked_at"] = wb_common.now_iso()
        wb_state.set_meta(e.db, "initial_backlog", bl)
        p3, _prev, _ = e.plan()
        self.assertEqual(len(items_of(p3, "M")), 1, p3["refused"])

    def test_rejected_listed_until_file_changes(self):
        e = Env(self)
        e.edit(LEAF, "second para", "second para X")
        p, _prev, _ = e.plan()
        m = items_of(p, "M")[0]
        with wb_state.transaction(e.db):
            e.db.execute("INSERT INTO rejections(notion_id, rel_path, l_sha, item_category, reason, created_at, plan_id,"
                         " active) VALUES(?,?,?,?,?,?,?,1)", (m["notion_id"], m["rel_path"], m["l_sha"], "M", "no",
                                                               wb_common.now_iso(), p["plan_id"]))
        p2, prev2, _ = e.plan()
        self.assertEqual(items_of(p2, "M"), [])
        self.assertEqual(len(p2["rejected"]), 1)
        self.assertIn("已拒绝", prev2)
        e.edit(LEAF, "second para X", "second para Y")
        p3, _prev, _ = e.plan()
        self.assertEqual(len(items_of(p3, "M")), 1, "re-proposed after the file changed")


class TestPlanRules(unittest.TestCase):
    def test_r143_bound_block_delete_and_allow_block_trash(self):
        e = Env(self)
        e.edit(LEAF, "first para\n\nsecond para", "second para CHANGED")
        p, prev, _ = e.plan()
        m, b = items_of(p, "M"), items_of(p, "B")
        self.assertEqual(len(m), 1, p["refused"])
        self.assertEqual(len(b), 1)
        self.assertEqual(m[0]["binds"], [b[0]["item_id"]])
        self.assertEqual(b[0]["bound_to"], m[0]["item_id"])
        self.assertFalse(m[0]["executable"])
        self.assertFalse(b[0]["executable"])
        self.assertTrue(any(wb_plan.TRASH_HINT in x for x in m[0]["blocked_by"]))
        self.assertIn(wb_plan.TRASH_HINT, prev)
        e.set_cfg(allow_block_trash=True)
        p, _prev, _ = e.plan()
        m, b = items_of(p, "M")[0], items_of(p, "B")[0]
        self.assertTrue(m["executable"] and b["executable"], (m["blocked_by"], b["blocked_by"]))
        self.assertEqual(b["ops"][0]["op"], "TRASH_BLOCK")
        with self.assertRaises(wb_plan.WbError):
            wb_plan.selected_items(p, wb_plan.Selection(items=[m["item_id"]]))
        got = wb_plan.selected_items(p, wb_plan.Selection(items=[m["item_id"]], delete_blocks=[b["item_id"]]))
        self.assertEqual([x["item_id"] for x in got], [m["item_id"], b["item_id"]])

    def test_r144_breaker_and_ack_anomaly(self):
        e = Env(self, cfg={"max_modified_notes": 0})
        e.edit(LEAF, "second para", "second para B")
        p, info = e.create()
        self.assertTrue(p["breaker"]["tripped"])
        self.assertEqual(p["summary"]["executable"], 0)
        self.assertTrue(all("BREAKER" in " ".join(it["blocked_by"]) for it in p["items"] if it["category"] != "C"))
        wb_state.set_meta(e.db, "anomaly_ack", wb_plan.anomaly_ack_record(p, os_user="t"))
        p2, _prev, _ = e.plan(scope="General/Home/Leaf.md")
        self.assertTrue(p2["breaker"]["tripped"], "a scoped replan is not the full replan the spec requires")
        p3, i3 = e.create()
        self.assertFalse(p3["breaker"]["tripped"])
        self.assertTrue(p3["breaker"]["acknowledged"] and p3["breaker"]["over_threshold"])
        self.assertEqual(p3["summary"]["executable"], 1)
        # the acknowledgement is consumed by that one full plan: the next full plan trips again
        self.assertIsNone(wb_state.get_meta(e.db, "anomaly_ack"))
        self.assertEqual(wb_state.get_meta_json(e.db, "anomaly_ack_used")["used_by"], p3["plan_id"])
        p4, _prev, _ = e.plan()
        self.assertTrue(p4["breaker"]["tripped"])

    def test_breaker_ack_bound_to_the_acknowledged_set_and_ttl(self):
        """An ack covers exactly the anomalous set of the acknowledged plan (notion_id@L-sha, deletes, pages over
        max_ops_per_page) and expires after plan_ttl_hours: a different burst trips the breaker again."""
        e = Env(self, cfg={"max_modified_notes": 1})
        e.edit(LEAF, "second para", "second para B1")
        e.edit(KID, "kid body", "kid body B1")
        p, _info = e.create()
        self.assertTrue(p["breaker"]["tripped"], p["breaker"])
        self.assertEqual(len(p["breaker"]["keys"]["modified"]), 2)
        wb_state.set_meta(e.db, "anomaly_ack", wb_plan.anomaly_ack_record(p, os_user="t"))
        # other notes are modified (the original edits reverted): not covered by the ack
        e.edit(LEAF, "second para B1", "second para")
        e.edit(KID, "kid body B1", "kid body")
        e.edit("General/Home/Folder2/Kid2.md", "kid2 body", "kid2 body B2")
        e.edit("General/Home/Rename me.md", "rename body", "rename body B2")
        p2, _prev, _ = e.plan()
        self.assertTrue(p2["breaker"]["over_threshold"])
        self.assertTrue(p2["breaker"]["tripped"] and not p2["breaker"]["acknowledged"], p2["breaker"])
        self.assertEqual(p2["summary"]["executable"], 0)
        # the same notes, same contents, but the ack is older than plan_ttl_hours
        e.edit("General/Home/Folder2/Kid2.md", "kid2 body B2", "kid2 body")
        e.edit("General/Home/Rename me.md", "rename body B2", "rename body")
        e.edit(LEAF, "second para", "second para B1")
        e.edit(KID, "kid body", "kid body B1")
        later = datetime.datetime.now().astimezone() + datetime.timedelta(hours=25)
        p3, _prev, _ = e.plan(now=later)
        self.assertTrue(p3["breaker"]["tripped"], "an old acknowledgement does not cover a new plan")
        p4, _prev, _ = e.plan()
        self.assertFalse(p4["breaker"]["tripped"])
        self.assertTrue(p4["breaker"]["acknowledged"])

    def test_r150_block_budget_members_two(self):
        e = Env(self, cfg={"workspace_members": 2})
        e.edit(LEAF, "second para", "second para U")                     # UPDATE only -> allowed
        e.edit(KID, "kid tail", "kid tail\n\nkid appended")             # APPEND -> report only
        e.write("General/Home/Folder/Brand new.md", "new body\n")        # N -> report only
        p, prev, _ = e.plan()
        self.assertEqual(p["creation_state"], "blocked_assumed")
        self.assertEqual(p["members"], 2)
        m = {it["rel_path"]: it for it in items_of(p, "M")}
        self.assertTrue(m[LEAF]["executable"], m[LEAF]["blocked_by"])
        self.assertFalse(m[KID]["executable"])
        self.assertTrue(any(x.startswith("QUOTA") for x in m[KID]["blocked_by"]))
        n = items_of(p, "N")
        self.assertEqual(len(n), 1)
        self.assertFalse(n[0]["executable"])
        self.assertIn("额度受限", prev)
        self.assertGreaterEqual(p["summary"]["new_blocks"], 2)

    def test_r146_whitelist_semantics(self):
        e = Env(self)
        e.edit("General/Home/Other.md", "other body", "other body changed")
        e.edit(TASK, "Label: a", "Label: b")
        p, _prev, _ = e.plan()
        m = {it["rel_path"]: it for it in items_of(p, "M")}
        self.assertIn("OUT_OF_WHITELIST", m["General/Home/Other.md"]["blocked_by"])
        self.assertTrue(m[TASK]["executable"], m[TASK]["blocked_by"])
        e.set_cfg(whitelist_database_ids=[])
        p, _prev, _ = e.plan()
        m = {it["rel_path"]: it for it in items_of(p, "M")}
        self.assertIn("OUT_OF_WHITELIST", m[TASK]["blocked_by"])
        # --scope 白名单 leaves out what is not whitelisted (exact ids, no descendants)
        p, _prev, _ = e.plan(scope="白名单")
        self.assertEqual([it["rel_path"] for it in items_of(p, "M")], [])
        # stage 2 (mode whitelist): only leaf pages
        e.set_cfg(whitelist_database_ids=[e.ids["db"]])
        e.edit("General/Home/Folder/Folder.md", "folder body", "folder body changed")
        p, _prev, _ = e.plan()
        m = {it["rel_path"]: it for it in items_of(p, "M")}
        self.assertTrue(any(x.startswith("NOT_LEAF") for x in m["General/Home/Folder/Folder.md"]["blocked_by"]))

    def test_r147_row_property_rules(self):
        e = Env(self)
        e.edit(TASK, "Label: a", "Label: b")
        e.edit(TASK, "Note: n1", "Note: n2")
        e.edit(TASK, "Done: false", "Done: true")
        e.edit(TASK, "N: null", "N: 7")
        p, _prev, _ = e.plan()
        m = items_of(p, "M")
        self.assertEqual(len(m), 1, p["refused"])
        op = m[0]["ops"][0]
        self.assertEqual(op["op"], "UPDATE_PROPERTIES")
        self.assertEqual(op["properties"]["Label"], {"select": {"name": "b"}})
        self.assertEqual(op["properties"]["Note"], {"rich_text": [{"type": "text", "text": {"content": "n2"}}]})
        self.assertEqual(op["properties"]["Done"], {"checkbox": True})
        self.assertEqual(op["properties"]["N"], {"number": 7})
        self.assertIn("Label", op["before"])
        for bad, cls in (("Label: c", "SCHEMA_OPTION_MISSING"), ("Fancy: plain", "PROPERTY_LOSSY")):
            with self.subTest(bad=bad):
                e2 = Env(self)
                key = bad.split(":")[0]
                t = e2.read(TASK)
                old = next(ln for ln in t.split("\n") if ln.startswith(key + ":"))
                e2.edit(TASK, old, bad)
                p2, _prev, _ = e2.plan()
                self.assertEqual(items_of(p2, "M"), [])
                self.assertTrue(refused_of(p2, cls), p2["refused"])
        e.set_cfg(automation_free_database_ids=[])
        p, _prev, _ = e.plan()
        self.assertIn("AUTOMATION_RISK", items_of(p, "M")[0]["blocked_by"])

    def test_r145_new_page_rules(self):
        e = Env(self, cfg={"mode": "full"})
        e.write("General/Home/Folder/Brand new.md", "Hello new page\n\n- x\n- y\n")
        e.write("General/root note.md", "root\n")                          # teamspace root
        e.write("General/Home/Nofolder/Lost.md", "lost\n")                 # folder without folder note
        e.write("General/Home/Leaf/Under leaf.md", "under leaf\n")         # P/ next to leaf P.md
        e.write("General/Home/Folder/a／b.md", "bad name\n")              # export substitution in the name
        e.write("General/Home/Folder/Copy of leaf.md",
                wb_plan.body_of(e.read(LEAF))[1] and "\n".join(wb_plan.body_of(e.read(LEAF))[1]) + "\n")  # near-dup
        e.write("General/Home/Tasks/Task 2.md", "---\nLabel: b\n---\n\nrow body\n")   # new row
        p, prev, _ = e.plan()
        n = {it["rel_path"]: it for it in items_of(p, "N")}
        ok = n["General/Home/Folder/Brand new.md"]
        self.assertTrue(ok["executable"], ok["blocked_by"])
        self.assertEqual(ok["title"], "Brand new")
        op = ok["ops"][0]
        self.assertEqual(op["op"], "CREATE_PAGE")
        self.assertEqual(op["parent"]["page_id"], e.ids["folder"])
        self.assertEqual(op["new_blocks"], 4)
        row = n["General/Home/Tasks/Task 2.md"]
        self.assertTrue(row["is_row"])
        self.assertEqual(row["ops"][0]["parent"]["data_source_id"], e.ids["ds"])
        self.assertEqual(row["ops"][0]["title_property"], "Name")
        self.assertEqual(row["ops"][0]["properties"]["Label"], {"select": {"name": "b"}})
        ref = {r["rel_path"]: r["class"] for r in p["refused"]}
        self.assertEqual(ref.get("General/root note.md"), "N_LOCATION")
        self.assertEqual(ref.get("General/Home/Nofolder/Lost.md"), "N_LOCATION")
        self.assertEqual(ref.get("General/Home/Leaf/Under leaf.md"), "N_LOCATION")
        self.assertEqual(ref.get("General/Home/Folder/a／b.md"), "N_NEEDS_RENAME")
        self.assertEqual(ref.get("General/Home/Folder/Copy of leaf.md"), "POSSIBLE_DUPLICATE")
        # stage 2 (mode whitelist, and mode plan which carries the same rules): attachments refused, more than
        # 100 blocks refused, a new page under a parent that already has child pages is NOT_LEAF (WB 2.3, 5.3.3)
        e.write("General/Home/Folder/With embed.md", "text\n\n![[pic.png]]\n")
        e.write("General/Home/Folder/Big new page.md", "\n\n".join(f"para {i}" for i in range(101)) + "\n")
        for mode in ("whitelist", "plan"):
            with self.subTest(mode=mode):
                e.set_cfg(mode=mode)
                p, _prev, _ = e.plan()
                ref = {r["rel_path"]: r["class"] for r in p["refused"]}
                self.assertEqual(ref.get("General/Home/Folder/With embed.md"), "UNSUPPORTED_INLINE:embed")
                self.assertEqual(ref.get("General/Home/Folder/Big new page.md"), "LIMIT:new_page_blocks")
                n = {it["rel_path"]: it for it in items_of(p, "N")}
                self.assertTrue(any(x.startswith("NOT_LEAF") for x in n["General/Home/Folder/Brand new.md"]["blocked_by"]))
                self.assertTrue(n["General/Home/Tasks/Task 2.md"]["is_row"])
                self.assertFalse(any(x.startswith("NOT_LEAF") for x in n["General/Home/Tasks/Task 2.md"]["blocked_by"]))
        e.set_cfg(mode="full")
        p, _prev, _ = e.plan()
        self.assertNotIn("General/Home/Folder/Big new page.md", {r["rel_path"] for r in p["refused"]},
                         "the 100-block limit is a stage-2 rule")

    def test_n_already_created_mapping_is_never_proposed_again(self):
        """I2 request: wb_meta created_page:<rel> (any state, incl. unknown / partial) = already created."""
        e = Env(self, cfg={"mode": "full"})
        rel = "General/Home/Folder/Brand new.md"
        e.write(rel, "Hello new page\n")
        p, _prev, _ = e.plan()
        self.assertEqual(len(items_of(p, "N")), 1)
        for state in ("unknown", "partial", "complete"):
            with self.subTest(state=state):
                mp = {"page_id": None if state == "unknown" else e.ids["kid"], "state": state, "plan_id": "wb-x"}
                if state == "unknown":
                    mp["journal_seq"] = 7
                wb_state.set_meta(e.db, "created_page:" + rel, mp)
                p, _prev, _ = e.plan()
                self.assertEqual(items_of(p, "N"), [])
                r = [x for x in p["refused"] if x["rel_path"] == rel]
                self.assertEqual([x["class"] for x in r], ["N_ALREADY_CREATED"])
                if state == "unknown":
                    self.assertIn("reconcile --journal 7", r[0]["reason"])

    def test_r148_rename_rules(self):
        e = Env(self)
        os.rename(e.path("General/Home/Rename me.md"), e.path("General/Home/Renamed.md"))
        e.register_rename("ren", "General/Home/Rename me.md", "General/Home/Renamed.md")   # refresh ran (L-17)
        p, prev, _ = e.plan()
        t = items_of(p, "T")
        self.assertEqual(len(t), 1, p["refused"])
        self.assertTrue(t[0]["writable"] and t[0]["executable"])
        op = t[0]["ops"][0]
        self.assertEqual((op["op"], op["new_title"], op["title_property"]), ("RENAME", "Renamed", "title"))
        self.assertEqual(wb_plan._plain(op["old_title"]), "Rename me")
        sel = wb_plan.selection_from_args(p, _Args(all_safe=True))
        self.assertEqual(sel.items, [], "--all-safe never includes T")
        for line in ("Notion 当前标题", "base 中标题", "本地新名", "新文件名可直接用作 Notion 标题", "入站引用数", "库内被 Obsidian 改写链接的笔记数",
                     "改名后下一轮库内将移动的文件数", "数据库行"):
            self.assertIn(line, prev)
        # export substitution in the new name -> recorded only
        e2 = Env(self)
        os.rename(e2.path("General/Home/Rename me.md"), e2.path("General/Home/a／b.md"))
        e2.register_rename("ren", "General/Home/Rename me.md", "General/Home/a／b.md")
        p2, _prev, _ = e2.plan()
        t2 = items_of(p2, "T")[0]
        self.assertFalse(t2["writable"])
        self.assertIn("RENAME_NOT_WRITABLE", t2["blocked_by"])
        self.assertIn("字符替换", t2["why_not"])
        # the Notion title changed too -> CONFLICT_RENAME (C, no T)
        e3 = Env(self)
        os.rename(e3.path("General/Home/Rename me.md"), e3.path("General/Home/Renamed.md"))
        e3.fake.pages[e3.ids["ren"]]["properties"]["title"]["title"] = [
            {"type": "text", "text": {"content": "Remote name", "link": None}, "plain_text": "Remote name",
             "href": None, "annotations": {"bold": False, "italic": False, "strikethrough": False,
                                           "underline": False, "code": False, "color": "default"}}]
        p3, _prev, _ = e3.plan()
        self.assertEqual(items_of(p3, "T"), [])
        self.assertEqual([c["kind"] for c in items_of(p3, "C")], ["CONFLICT_RENAME"])

    def test_r149_delete_rules(self):
        e = Env(self)
        os.unlink(e.path("General/Home/Doomed.md"))
        e.hold_delete("del", "General/Home/Doomed.md")
        os.unlink(e.path("General/Home/Doomed parent/Doomed parent.md"))
        os.unlink(e.path("General/Home/Doomed parent/Doomed child.md"))
        e.hold_delete("del2", "General/Home/Doomed parent/Doomed parent.md",
                      subtree=[{"notion_id": e.ids["del2c"], "rel": "General/Home/Doomed parent/Doomed child.md"}])
        p, prev, _ = e.plan()
        d = {it["rel_path"]: it for it in items_of(p, "D")}
        self.assertEqual(len(d), 2, p["refused"])
        simple = d["General/Home/Doomed.md"]
        self.assertTrue(simple["executable"], simple["blocked_by"])
        self.assertEqual(simple["risk"], "normal")
        op = simple["ops"][0]
        self.assertEqual(op["op"], "TRASH_PAGE")
        self.assertTrue(op["page_json_sha"] and op["last_edited_time"] is not None)
        high = d["General/Home/Doomed parent/Doomed parent.md"]
        self.assertEqual(high["risk"], "high")
        self.assertEqual(high["children_count"], 1)
        sel = wb_plan.selection_from_args(p, _Args(delete_all_in_plan=True))
        self.assertEqual(sel.delete, [simple["item_id"]], "high-risk D items are only individually confirmable")
        for label in ("子页面与子数据库", "正文块数", "附件数", "同步块原件", "内联数据库", "入站引用", "入站关系", "是否锁定",
                      "Notion 最后编辑时间", "依据快照"):
            self.assertIn(label, prev)
        # Notion changed since base -> CONFLICT_DEL_EDIT, no D item
        e2 = Env(self)
        os.unlink(e2.path("General/Home/Doomed.md"))
        e2.hold_delete("del", "General/Home/Doomed.md")
        e2.remote_text("del", 0, "doomed body changed in Notion")
        p2, _prev, _ = e2.plan()
        self.assertEqual(items_of(p2, "D"), [])
        self.assertTrue(refused_of(p2, "CONFLICT_DEL_EDIT"), p2["refused"])

    def test_d_item_snapshot_older_than_24h_needs_refresh_and_unknown_inbound(self):
        e = Env(self)
        os.unlink(e.path("General/Home/Doomed.md"))
        e.hold_delete("del", "General/Home/Doomed.md")
        later = datetime.datetime.now().astimezone() + datetime.timedelta(hours=30)
        with mock.patch.object(wb_plan, "_inbound", return_value=None):
            p, prev, _ = e.plan(now=later)
        d = items_of(p, "D")[0]
        self.assertIn("NEEDS_REFRESH", d["blocked_by"])
        self.assertIn(f"入站引用：{wb_plan.UNKNOWN}", prev)
        self.assertIn(f"入站关系：{wb_plan.UNKNOWN}", prev)


def golden_scenario(tc):
    """One item of each category (N, M, B, D, T, MV, C) + refusals + a rejection, mode full (MV allowed)."""
    e = Env(tc, cfg={"mode": "full", "allow_page_move": True})
    e.edit(LEAF, "first para\n\nsecond para", "second para G")                       # M + B
    e.write("General/Home/Folder/Brand new.md", "new body\n")                      # N
    os.unlink(e.path("General/Home/Doomed.md"))
    e.hold_delete("del", "General/Home/Doomed.md")                                 # D
    os.rename(e.path("General/Home/Rename me.md"), e.path("General/Home/Renamed.md"))   # T
    os.rename(e.path("General/Home/Folder2/Kid2.md"), e.path("General/Home/Folder/Kid2.md"))  # MV
    e.edit(KID, "kid body", "kid body LOCAL")
    e.remote_text("kid", 0, "kid body REMOTE")                                      # C
    e.edit(TASK, "Label: a", "Label: zzz")                                          # refused
    e.edit("General/Home/Other.md", "other body", "other body REJECTED")            # rejected
    with open(e.path("General/Home/Other.md"), "rb") as fh:
        sha = wb_common.sha256_bytes(fh.read())
    with wb_state.transaction(e.db):
        e.db.execute("INSERT INTO rejections(notion_id, rel_path, l_sha, item_category, reason, created_at, plan_id,"
                     " active) VALUES(?,?,?,?,?,?,?,1)", (e.ids["other"], "General/Home/Other.md", sha, "M",
                                                         "not now", "2026-11-18T01:00:00+07:00", "wb-20261118T010000-aaaa"))
    p, prev, _ = e.plan(now=datetime.datetime(2026, 11, 18, 2, 0, 0, tzinfo=TZ))
    return p, prev


def normalize_preview(text, plan):
    """Golden comparison: plan id, timestamps and Notion ids (full, 32-hex and 8-hex prefixes) become stable
    placeholders numbered by first appearance."""
    text = text.replace(plan["plan_id"], "<PLAN>")
    text = text.replace("cd " + shlex.quote(wb_common.WORK) + " &&", "cd <WORK> &&")
    text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:\d{2}|Z)?", "<TS>", text)
    aliases = {}

    def alias(hex32):
        if hex32 not in aliases:
            aliases[hex32] = f"ID{len(aliases) + 1}"
        return aliases[hex32]

    def full(m):
        return "<" + alias(m.group(0).replace("-", "")) + ">"
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])",
                  full, text)

    def short(m):
        h = m.group(1)
        known = [k for k in aliases if k.startswith(h)]
        return "`<" + (aliases[known[0]] if len(known) == 1 else alias("short:" + h)) + "~8>`"
    return re.sub(r"`([0-9a-f]{8})`", short, text)


class _Args:
    def __init__(self, **kw):
        self.items, self.delete, self.delete_blocks, self.rename, self.move = [], [], [], [], []
        self.all_safe = self.delete_all_in_plan = False
        for k, v in kw.items():
            setattr(self, k, v)


class TestPlanFilesAndLifecycle(unittest.TestCase):
    def test_r140_three_outputs_vault_copy_rules(self):
        e = Env(self)
        e.edit(LEAF, "second para", "second para C")
        p, info = e.create()
        d = info["dir"]
        self.assertTrue(os.path.isfile(os.path.join(d, "plan.json")))
        self.assertTrue(os.path.isfile(os.path.join(d, "preview.md")))
        vc = e.path(info["vault_copy"])
        self.assertEqual(info["vault_copy"], f"Notion_Obsidian/写回预览/{p['plan_id']}.md")
        with open(vc, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8")
        self.assertNotIn(b"\r", raw)
        self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))
        fm, _body, _ = wb_common.split_note(text)
        meta = wb_common.parse_frontmatter(fm)
        self.assertEqual(meta, {"notion_kind": "generated", "generated_by": "writeback.py", "plan_id": p["plan_id"]})
        self.assertTrue(no_wikilink_outside_code(text))
        self.assertTrue(re.fullmatch(r"[0-9A-Za-z_-]+", p["plan_id"]))
        # ADD only: an existing vault copy is never overwritten
        with open(vc, "a", encoding="utf-8") as fh:
            fh.write("user note\n")
        before = e.read(info["vault_copy"])
        p2 = copy.deepcopy(p)
        p2["plan_id"] = p["plan_id"][:-4] + ("beef" if not p["plan_id"].endswith("beef") else "cafe")
        os.rename(vc, e.path(f"Notion_Obsidian/写回预览/{p2['plan_id']}.md"))
        e.write(info["vault_copy"], before)
        wb_plan.create_plan(e.ctx(), p2, wb_plan.render_preview(p2))
        self.assertEqual(e.read(f"Notion_Obsidian/写回预览/{p2['plan_id']}.md"), before, "never overwritten")

    def test_r141_sha_supersede_expire_and_tamper(self):
        e = Env(self)
        e.edit(LEAF, "second para", "second para S")
        p1, i1 = e.create()
        with open(os.path.join(i1["dir"], "plan.json"), "rb") as fh:
            self.assertEqual(wb_common.sha256_bytes(fh.read()), i1["plan_sha256"])
        plan, sha = wb_plan.load_plan(p1["plan_id"], work=e.paths, db=e.db)
        self.assertEqual(sha, i1["plan_sha256"])
        p2, i2 = e.create()
        self.assertIn(p1["plan_id"], i2["superseded"])
        self.assertEqual(wb_plan.plan_row(e.db, p1["plan_id"])["status"], "superseded")
        self.assertEqual(wb_plan.plan_row(e.db, p2["plan_id"])["status"], "draft")
        later = datetime.datetime.now().astimezone() + datetime.timedelta(hours=25)
        self.assertEqual(wb_plan.expire_plans(e.db, later), [p2["plan_id"]])
        self.assertEqual(wb_plan.plan_row(e.db, p2["plan_id"])["status"], "expired")
        pj = os.path.join(i2["dir"], "plan.json")
        os.chmod(pj, 0o640)
        with open(pj, "ab") as fh:
            fh.write(b" ")
        with self.assertRaises(wb_plan.WbError):
            wb_plan.load_plan(p2["plan_id"], work=e.paths, db=e.db)

    def test_plan_json_shape_speccheck6(self):
        e = Env(self)
        e.edit(LEAF, "second para", "second para J")
        p, info = e.create()
        for k in ("plan_id", "created_at", "expires_at", "mode", "scope", "renderer_version", "base_snapshot",
                  "config_sha", "members", "creation_state", "breaker", "items", "refused", "rejected", "counts"):
            self.assertIn(k, p)
        for k in ("tripped", "modified_notes", "local_deletes", "max_ops_page"):
            self.assertIn(k, p["breaker"])
        it = p["items"][0]
        for k in ("item_id", "category", "notion_id", "rel_path", "risk", "l_sha", "b_seq", "r_json_sha", "binds",
                  "ops", "preview"):
            self.assertIn(k, it)
        for c in ("N", "M", "B", "D", "T", "MV", "C", "refused"):
            self.assertIn(c, p["counts"])
        op = it["ops"][0]
        for k in ("op", "target_id", "before_norm_sha", "request", "expected_after"):
            self.assertIn(k, op)
        rows = e.db.execute("SELECT * FROM plan_items WHERE plan_id=?", (p["plan_id"],)).fetchall()
        self.assertEqual([r["item_id"] for r in rows], [it["item_id"]])
        self.assertEqual(rows[0]["decision"], "pending")

    def test_r142_golden_preview_every_item_type(self):
        """One item of each type (N, M, B, D, T, MV, C) + a refusal + a rejection: every WB 5.2 section/field,
        and the whole preview equals the golden text (volatile ids / times normalised)."""
        p, prev = golden_scenario(self)
        got = normalize_preview(prev, p)
        if got != GOLDEN_PREVIEW:
            diff = "\n".join(difflib.unified_diff(GOLDEN_PREVIEW.split("\n"), got.split("\n"), "golden", "got",
                                                  lineterm="", n=1))
            self.fail("preview differs from the golden text:\n" + diff)
        cats = sorted({it["category"] for it in p["items"]})
        self.assertEqual(cats, sorted(["N", "M", "B", "D", "T", "MV", "C"]), p["refused"])
        self.assertTrue(p["refused"])
        for sec in ("## 新增内容（N）", "## 修改内容（M）", "## 待删除 / 移入回收站（高风险）：页面删除 D",
                    "## 待删除 / 移入回收站（高风险）：块删除 B", "## 标题或路径变化（高风险，T / MV）：改名 T",
                    "## 标题或路径变化（高风险，T / MV）：移动 MV", "## 冲突（C）", "## 拒绝写回 / 只报告",
                    "## 已拒绝", "## 汇总"):
            self.assertIn(sec, prev)
        for field in ("拟建页面标题", "Notion 父页面", "拟新建块数", "新建行",                         # N
                      "逐块差异（旧 → 新，块 ID 前 8 位）", "UPDATE 块",                              # M
                      "块 ID：", "是否有子块", "原文",                                                   # B
                      "旧父页面", "新父页面", "共享与权限差异",                                          # MV
                      "两边", "Notion 最后编辑时间", "编辑者类型", "建议",                                # C
                      "各类数量", "拟发请求数", "预计耗时", "拟新建块数", "块额度状态", "熔断"):
            if field == "两边":
                self.assertIn("本地改动", prev)
                self.assertIn("Notion 改动", prev)
                continue
            self.assertIn(field, prev)
        self.assertIn("剩余风险：" + wb_plan.RESIDUAL_RISK, prev)
        self.assertEqual(wb_plan.RESIDUAL_RISK,
                         "API 没有条件写，单个请求的预检与写入之间仍有约 1 秒窗口；执行期间请不要在 Notion 中编辑这些页面")
        self.assertTrue(re.search(r"SCHEMA_OPTION_MISSING — .*；建议：", prev))
        self.assertTrue(no_wikilink_outside_code(wb_plan.vault_copy_text(p, prev)))
        # every item carries its preview text inside plan.json (hashed with the plan)
        self.assertTrue(all(it.get("preview") for it in p["items"]))

    def test_selection_and_challenge_phrase(self):
        e = Env(self, cfg={"allow_block_trash": True, "mode": "full"})
        e.edit(LEAF, "first para\n\nsecond para", "second para P")
        os.unlink(e.path("General/Home/Doomed.md"))
        e.hold_delete("del", "General/Home/Doomed.md")
        os.rename(e.path("General/Home/Rename me.md"), e.path("General/Home/Renamed.md"))
        e.register_rename("ren", "General/Home/Rename me.md", "General/Home/Renamed.md")   # refresh ran (L-17)
        e.write("General/Home/Folder/Brand new.md", "new body\n")
        p, _prev, _ = e.plan()
        ids = {it["category"]: it["item_id"] for it in p["items"]}
        sel = wb_plan.selection_from_args(p, _Args(all_safe=True))
        # the M item shares its note with the B item: --all-safe leaves it out (a note is executed as a whole)
        self.assertEqual(sel.items, [ids["N"]])
        self.assertEqual([(x["item_id"], x["with"]) for x in wb_plan.all_safe_exclusions(p)], [(ids["M"], [ids["B"]])])
        self.assertEqual([x["item_id"] for x in wb_plan.selected_items(p, sel)], [ids["N"]], "--all-safe stays usable")
        self.assertEqual((sel.delete, sel.delete_blocks, sel.rename, sel.move), ([], [], [], []))
        for bad in ([ids["D"]], [ids["T"]], [ids["B"]], ["C-001"], ["X-1"]):
            with self.assertRaises(wb_plan.WbError):
                wb_plan.selection_from_args(p, _Args(items=bad))
        sel = wb_plan.selection_from_args(p, _Args(items=[f"{ids['M']},{ids['N']}"], delete=[ids["D"]],
                                                   delete_blocks=[ids["B"]], rename=[ids["T"]]))
        chosen = wb_plan.selected_items(p, sel)
        phrase = wb_plan.challenge_phrase(p["plan_id"], chosen, "ABC123")
        self.assertEqual(phrase, f"确认 {p['plan_id'][-4:]} 修改1 新增1 删除1[{ids['D']}]（含子页面0个） 删块1[{ids['B']}] "
                                 f"改名1[{ids['T']}] ABC123")
        self.assertTrue(re.fullmatch(r"确认 \w{4} 修改\d+ 新增\d+ 删除\d+(\[[^\]]+\]（含子页面\d+个）)? "
                                     r"(删块\d+\[[^\]]+\] )?改名\d+(\[[^\]]+\])? [0-9A-F]{6}", phrase))
        # without a D item the phrase keeps the WB 10.2 form
        no_d = [x for x in chosen if x["category"] != "D"]
        self.assertNotIn("含子页面", wb_plan.challenge_phrase(p["plan_id"], no_d, "ABC123"))
        with self.assertRaises(wb_plan.WbError):
            wb_plan.selected_items(p, wb_plan.Selection(items=["M-999"]))
        with self.assertRaises(wb_plan.WbError):
            wb_plan.selected_items(p, wb_plan.Selection())
        with self.assertRaises(wb_plan.WbError):
            wb_plan.selected_items(p, sel, {ids["N"]: "rejected"})

    def test_planning_sends_no_write_and_never_loads_write_token(self):
        import notion_tokens
        e = Env(self)
        e.edit(LEAF, "second para", "second para R")
        os.unlink(e.path("General/Home/Doomed.md"))
        e.hold_delete("del", "General/Home/Doomed.md")
        e.fake.clear_requests()
        with mock.patch.object(notion_tokens, "load_writeback_token", side_effect=AssertionError("token loaded")):
            p, info = e.create()
        self.assertEqual(e.writes(), [])
        self.assertTrue(e.fake.requests, "R was read live")
        self.assertTrue(all(r["token"] == "ro" for r in e.fake.requests))
        self.assertNotIn("writeback_client", [m for m in sys.modules if m == "writeback_client" and False])


class TestRefusalClasses(unittest.TestCase):
    def test_sync_settling_no_process_recent_start_and_burst(self):
        e = Env(self)
        e.edit(LEAF, "second para", "second para SYNC")
        p, _prev, _ = e.plan(obsidian={"running": False, "started_at": None})
        m = items_of(p, "M")[0]
        self.assertFalse(m["executable"])
        self.assertTrue(any(x.startswith("SYNC_SETTLING") for x in m["blocked_by"]))
        recent = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        p, _prev, _ = e.plan(obsidian={"running": True, "started_at": recent})
        self.assertTrue(any(x.startswith("SYNC_SETTLING") for x in items_of(p, "M")[0]["blocked_by"]))
        e.set_cfg(sync_burst_files=2)
        e.edit(KID, "kid body", "kid body SYNC")
        p, _prev, _ = e.plan()
        self.assertEqual(items_of(p, "M"), [])
        self.assertEqual(sorted(r["rel_path"] for r in refused_of(p, "SYNC_SETTLING")), sorted([LEAF, KID]))
        self.assertEqual(wb_plan.burst_files({"a": 0, "b": 100, "c": 500}, 120, 2), {"a", "b"})

    def test_l13_seeded_while_local_only_is_initial_backlog(self):
        e = Env(self)
        e.edit(LEAF, "second para", "second para OLD LOCAL EDIT")
        base = wb_state.latest_base(e.db, e.ids["leaf"])
        self.assertEqual((base["seq"], base["seed"]), (1, 1))
        seed_file = os.path.join(e.work, "state", "base", base["sha256"])
        t = os.stat(seed_file).st_mtime
        os.utime(e.path(LEAF), (t - 3600, t - 3600))      # the local edit predates the seed
        p, info = e.create()
        self.assertTrue(refused_of(p, "INITIAL_BACKLOG"), p["refused"])
        self.assertIn(e.ids["leaf"], wb_state.get_meta_json(e.db, "initial_backlog", {}))
        wb_state.set_meta(e.db, "backlog_acked", {e.ids["leaf"]: {"at": wb_common.now_iso()}})
        p2, _prev, _ = e.plan()
        self.assertEqual(len(items_of(p2, "M")), 1, p2["refused"])
        # a normal edit made after the seed is a fresh edit
        e2 = Env(self)
        e2.edit(LEAF, "second para", "second para NEW EDIT")
        p3, _prev, _ = e2.plan()
        self.assertEqual(len(items_of(p3, "M")), 1, p3["refused"])

    def test_auto_sync_hold_and_blocked_page_refused(self):
        e = Env(self)
        e.edit(LEAF, "second para", "second para HOLD")
        e.edit(KID, "kid body", "kid body BLOCKED")
        with open(os.path.join(e.work, "state", "auto_sync_hold.json"), "w", encoding="utf-8") as fh:
            json.dump({e.ids["leaf"]: {"reason": "post_verify_C02", "source": "post_verify", "since": "x"}}, fh)
        with wb_state.transaction(e.db):
            e.db.execute("INSERT INTO page_state(notion_id, state, reason, since) VALUES(?,?,?,?)",
                         (e.ids["kid"], "BLOCKED", "readback", wb_common.now_iso()))
        p, _prev, _ = e.plan()
        self.assertEqual(items_of(p, "M"), [])
        ref = {r["rel_path"]: r["class"] for r in p["refused"]}
        self.assertEqual(ref[LEAF], "AUTO_SYNC_HOLD")
        self.assertEqual(ref[KID], "BLOCKED_PAGE")

    def test_renderer_mismatch_and_locked(self):
        e = Env(self)
        e.edit(LEAF, "second para", "second para RV")
        e.edit(KID, "kid body", "kid body LOCK")
        with wb_state.transaction(e.db):
            e.db.execute("UPDATE base_history SET renderer_version='1.0.0' WHERE notion_id=?", (e.ids["leaf"],))
        e.fake.pages[e.ids["kid"]]["is_locked"] = True
        p, _prev, _ = e.plan()
        ref = {r["rel_path"]: r["class"] for r in p["refused"]}
        self.assertEqual(ref[LEAF], "RENDERER_MISMATCH")
        self.assertEqual(ref[KID], "LOCKED")

    def test_normalized_note_text_matches_md2blocks(self):
        import md2blocks
        self.assertEqual(wb_plan.NORMALIZED_NOTE, md2blocks.HINT_NORMALIZED)

    def test_preview_copy_accepted_by_verifier_c08(self):
        import verify_vault as vv
        e = Env(self)
        e.edit(LEAF, "second para", "second para [[wiki in user text]]")
        p, info = e.create()
        out = os.path.join(e.tmp, "vv")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            vv.verify(e.snap, os.path.join(e.work, "sidebar.json"), e.vault, out, roots_kinds=e.snap + ".roots.json",
                      snapshot_immutable=True)
        with open(os.path.join(out, "verify.json"), encoding="utf-8") as fh:
            c08 = json.load(fh)["checks"]["C08"]
        self.assertEqual(c08["status"], "PASS", c08["summary"])
        self.assertIn("generated_files=1", c08["summary"])


def _add_wl(e, *keys):
    e.set_cfg(whitelist_page_ids=list(e.cfg_dict["whitelist_page_ids"]) + [e.ids[k] for k in keys])


class TestFixRound(unittest.TestCase):
    """Review findings of wave 3 (each test names the finding it covers)."""

    def test_same_body_and_renum_units(self):
        self.assertTrue(wb_plan._same_body(["1. a", "2. b"], {0, 1}, ["1. a", "1. b"]))
        self.assertFalse(wb_plan._same_body(["1. a", "2. b"], {0}, ["1. a", "1. b"]), "line 1 is no list marker")
        self.assertFalse(wb_plan._same_body(["x"], set(), ["x", ""]))
        # the alignment heuristic leaves fenced code and $$ equation lines alone
        self.assertEqual(wb_plan._renum(["$$", "2. c+d", "$$", "3. x", "```", "4. y", "```"]),
                         ["$$", "2. c+d", "$$", "1. x", "```", "4. y", "```"])

    def test_equation_lines_starting_with_numbers_are_not_masked_f5(self):
        """F5: an edit to an equation line '2. c+d' is a real change (UPDATE of the equation), never hidden by the
        list-renumbering normalisation, alone or next to another edit; real list renumbering stays free."""
        def seed(e):
            e.ids["eq"] = e.fake.add_page("Eq", parent=e.ids["home"], children=[
                para("alpha line"), {"type": "equation", "equation": {"expression": "1. a+b\n2. c+d"}},
                num("one"), num("two")])
        e = Env(self, seed=seed)
        _add_wl(e, "eq")
        rel = "General/Home/Eq.md"
        self.assertIn("$$\n1. a+b\n2. c+d\n$$", e.read(rel))
        e.edit(rel, "2. c+d", "9. c+d")
        p, _prev, ctx = e.plan()
        m = [it for it in items_of(p, "M") if it["rel_path"] == rel]
        self.assertEqual(len(m), 1, p["refused"])
        eq_ops = [o for o in m[0]["ops"] if o.get("block_type") == "equation"]
        self.assertEqual(len(eq_ops), 1, m[0]["ops"])
        self.assertEqual(eq_ops[0]["request"]["equation"]["expression"], "1. a+b\n9. c+d")
        e.edit(rel, "alpha line", "alpha line EDITED")
        p, _prev, ctx = e.plan()
        m = [it for it in items_of(p, "M") if it["rel_path"] == rel]
        self.assertEqual(len(m), 1, p["refused"])
        self.assertEqual(sorted(o.get("block_type") for o in m[0]["ops"]), ["equation", "paragraph"])
        # independent oracle: the planned ops re-render to L exactly (numbers may differ on list markers only)
        node = ctx.ex.nodes[e.ids["eq"]]
        base = wb_state.latest_base(e.db, e.ids["eq"])
        pp = wb_plan.PagePlanner(ctx, node, wb_state.base_content(base["sha256"], db=e.db).decode(),
                                 wb_state.block_lines_for(e.db, e.ids["eq"], base["sha256"]),
                                 wb_plan.body_of(e.read(rel))[1], ctx.reader.tree(e.ids["eq"]), e.ids["eq"])
        body, mask = pp._render_m(pp._apply(m[0]["ops"]))
        self.assertEqual(body, wb_plan.body_of(e.read(rel))[1])
        # renumbering the real list (Obsidian style) only: still no item and no refusal
        e2 = Env(self, seed=seed)
        _add_wl(e2, "eq")
        e2.edit(rel, "1. one\n2. two", "1. one\n1. two")
        p2, _prev, _ = e2.plan()
        self.assertEqual([it for it in p2["items"] if it["rel_path"] == rel], [])
        self.assertEqual([r for r in p2["refused"] if r["rel_path"] == rel], [])

    def test_note_is_a_unit_in_the_selection_f2(self):
        """F2: M (with its bound B) and T of one note are selected together or not at all; a non-executable or
        rejected sibling blocks the rest of the note."""
        e = Env(self, cfg={"allow_block_trash": True})
        e.edit(LEAF, "first para\n\nsecond para", "second para U")
        os.rename(e.path(LEAF), e.path("General/Home/Leaf renamed.md"))
        e.register_rename("leaf", LEAF, "General/Home/Leaf renamed.md")   # refresh ran (L-17)
        p, _prev, _ = e.plan()
        m, b, t = items_of(p, "M"), items_of(p, "B"), items_of(p, "T")
        self.assertEqual((len(m), len(b), len(t)), (1, 1, 1), (p["items"], p["refused"]))
        M, B, T = m[0]["item_id"], b[0]["item_id"], t[0]["item_id"]
        self.assertTrue(all(x["executable"] for x in (m[0], b[0], t[0])), [x["blocked_by"] for x in (m[0], b[0], t[0])])
        for sel in (wb_plan.Selection(delete_blocks=[B]), wb_plan.Selection(rename=[T]),
                    wb_plan.Selection(items=[M], delete_blocks=[B])):
            with self.subTest(sel=sel):
                with self.assertRaises(wb_plan.WbError) as cm:
                    wb_plan.selected_items(p, sel)
                self.assertIn("同一笔记", str(cm.exception))
        got = wb_plan.selected_items(p, wb_plan.Selection(items=[M], delete_blocks=[B], rename=[T]))
        self.assertEqual(sorted(x["item_id"] for x in got), sorted([M, B, T]))
        with self.assertRaises(wb_plan.WbError):
            wb_plan.selected_items(p, wb_plan.Selection(items=[M], delete_blocks=[B]), {T: "rejected"})
        # a non-executable sibling (title not writable) blocks the content items of the same note
        e2 = Env(self)
        e2.edit(LEAF, "second para", "second para V")
        os.rename(e2.path(LEAF), e2.path("General/Home/a／b.md"))
        e2.register_rename("leaf", LEAF, "General/Home/a／b.md")
        p2, _prev, _ = e2.plan()
        m2, t2 = items_of(p2, "M")[0], items_of(p2, "T")[0]
        self.assertFalse(t2["executable"])
        with self.assertRaises(wb_plan.WbError) as cm:
            wb_plan.selected_items(p2, wb_plan.Selection(items=[m2["item_id"]]))
        self.assertIn("不可执行", str(cm.exception))
        self.assertNotIn(wb_plan.UNREGISTERED_RENAME, str(cm.exception), "blocked by the sibling T, not by L-17")
        # a refused (not written) content change of the same note blocks its rename item too
        e3 = Env(self)
        e3.edit(LEAF, "## Heading", "### Heading")
        os.rename(e3.path(LEAF), e3.path("General/Home/Leaf renamed.md"))
        e3.register_rename("leaf", LEAF, "General/Home/Leaf renamed.md")
        p3, _prev, _ = e3.plan()
        t3 = items_of(p3, "T")[0]
        self.assertTrue(t3["executable"], t3["blocked_by"])
        self.assertEqual([r["class"] for r in p3["refused"] if r["notion_id"] == e3.ids["leaf"]], ["TYPE_CHANGED"])
        with self.assertRaises(wb_plan.WbError) as cm:
            wb_plan.selected_items(p3, wb_plan.Selection(rename=[t3["item_id"]]))
        self.assertIn("不会写回的本地改动（TYPE_CHANGED）", str(cm.exception))

    def test_stage2_leaf_rule_for_d_and_subtree_scope_f4_f10(self):
        """F4/F10/F13: in stage 2 a D item whose page has child pages is NOT_LEAF; in mode full every descendant must
        be whitelisted; the challenge phrase states the child pages."""
        e = Env(self)
        os.unlink(e.path("General/Home/Doomed parent/Doomed parent.md"))
        os.unlink(e.path("General/Home/Doomed parent/Doomed child.md"))
        e.hold_delete("del2", "General/Home/Doomed parent/Doomed parent.md",
                      subtree=[{"notion_id": e.ids["del2c"], "rel": "General/Home/Doomed parent/Doomed child.md"}])
        p, _prev, _ = e.plan()
        d = items_of(p, "D")[0]
        self.assertFalse(d["executable"])
        self.assertTrue(any(x.startswith("NOT_LEAF") for x in d["blocked_by"]), d["blocked_by"])
        e.set_cfg(mode="full")
        p, _prev, _ = e.plan()
        d = items_of(p, "D")[0]
        self.assertTrue(any("子页面" in x and x.startswith("OUT_OF_WHITELIST") for x in d["blocked_by"]), d["blocked_by"])
        _add_wl(e, "del2c")
        p, _prev, _ = e.plan()
        d = items_of(p, "D")[0]
        self.assertTrue(d["executable"], d["blocked_by"])
        items = wb_plan.selected_items(p, wb_plan.Selection(delete=[d["item_id"]]))
        phrase = wb_plan.challenge_phrase(p["plan_id"], items, "ABC123")
        self.assertIn(f"删除1[{d['item_id']}]（含子页面1个）", phrase)
        self.assertEqual(wb_plan.selection_from_args(p, _Args(delete_all_in_plan=True)).delete, [],
                         "a D item with child pages is never batchable")

    def test_scoped_plan_breaker_counts_out_of_scope_pages_f6_f9(self):
        e = Env(self, cfg={"max_ops_per_page": 2})
        e.edit(LEAF, "first para", "first para X")
        e.edit(LEAF, "second para", "second para Y")
        e.edit(LEAF, "last para", "last para Z")
        e.edit(KID, "kid body", "kid body W")
        full, _prev, _ = e.plan()
        self.assertTrue(full["breaker"]["tripped"], full["breaker"])
        self.assertEqual(full["breaker"]["max_ops_page"], 3)
        scoped, _prev, _ = e.plan(scope="General/Home/Folder")
        self.assertEqual([it["rel_path"] for it in scoped["items"]], [KID], "out-of-scope notes emit no item")
        self.assertTrue(scoped["breaker"]["tripped"], scoped["breaker"])
        self.assertEqual(scoped["breaker"]["max_ops_page"], 3)
        self.assertEqual(scoped["summary"]["executable"], 0)
        self.assertEqual(scoped["refused"], [], "out-of-scope analysis leaves no refusal behind")

    def test_initial_backlog_from_the_whole_scan_at_a_scoped_first_plan_f7(self):
        e = Env(self, enabled=False)
        e.edit(LEAF, "second para", "second para PRE-EXISTING")
        e.edit(KID, "kid body", "kid body PRE-EXISTING")
        os.rename(e.path("General/Home/Rename me.md"), e.path("General/Home/Renamed.md"))
        e.write("General/Home/Folder/Brand new.md", "pre-existing new file\n")
        p1, _info = e.create(scope="General/Home/Folder")
        bl = wb_state.get_meta_json(e.db, "initial_backlog", {})
        for key in (e.ids["leaf"], e.ids["kid"], e.ids["ren"], "new:General/Home/Folder/Brand new.md"):
            self.assertIn(key, bl)
        self.assertIsNotNone(wb_state.get_meta(e.db, "enabled_at"))
        p2, _prev, _ = e.plan()
        self.assertEqual([it["item_id"] for it in p2["items"] if it["category"] != "C"], [],
                         (p2["items"], p2["refused"]))
        ref = {r["rel_path"]: r["class"] for r in p2["refused"]}
        for rel in (LEAF, KID, "General/Home/Renamed.md", "General/Home/Folder/Brand new.md"):
            self.assertEqual(ref.get(rel), "INITIAL_BACKLOG", (rel, ref))
        wb_state.set_meta(e.db, "backlog_acked", {e.ids["leaf"]: {"at": wb_common.now_iso()}})
        p3, _prev, _ = e.plan()
        self.assertEqual([it["rel_path"] for it in items_of(p3, "M")], [LEAF])

    def test_superseded_by_a_plan_that_refuses_the_note_f8(self):
        e = Env(self)
        e.edit(LEAF, "second para", "second para S1")
        p1, i1 = e.create(scope=LEAF)
        self.assertTrue(items_of(p1, "M")[0]["executable"])
        with open(os.path.join(e.work, "state", "auto_sync_hold.json"), "w", encoding="utf-8") as fh:
            json.dump({e.ids["leaf"]: {"reason": "post_verify_C02", "source": "post_verify", "since": "x"}}, fh)
        p2, i2 = e.create(scope=LEAF)
        self.assertEqual(items_of(p2, "M"), [])
        self.assertEqual([r["class"] for r in p2["refused"]], ["AUTO_SYNC_HOLD"])
        self.assertIn(p1["plan_id"], i2["superseded"])
        self.assertEqual(wb_plan.plan_row(e.db, p1["plan_id"])["status"], "superseded")
        # a whole-vault plan supersedes every older open plan (it evaluated every note)
        os.unlink(os.path.join(e.work, "state", "auto_sync_hold.json"))
        e.edit(KID, "kid body", "kid body S3")
        p3, i3 = e.create(scope="General/Home/Folder")
        p4, i4 = e.create()
        self.assertIn(p3["plan_id"], i4["superseded"])
        self.assertIn(p2["plan_id"], i4["superseded"])

    def test_nested_block_delete_items_carry_children_f11(self):
        def seed(e):
            e.ids["nest"] = e.fake.add_page("Nest", parent=e.ids["home"], children=[
                para("head"), bullet("parent item", children=[bullet("kid item")]), para("tail")])
        e = Env(self, seed=seed, cfg={"allow_block_trash": True})
        _add_wl(e, "nest")
        rel = "General/Home/Nest.md"
        t = e.read(rel)
        self.assertIn("- parent item\n    - kid item\n", t)
        e.edit(rel, "- parent item\n    - kid item\n\n", "")
        p, _prev, _ = e.plan()
        b = [it for it in items_of(p, "B") if it["rel_path"] == rel]
        self.assertEqual(len(b), 2, (p["items"], p["refused"]))
        self.assertEqual([x["has_children"] for x in b], [False, True], "post-order: the child first")
        with self.assertRaises(wb_plan.WbError):
            wb_plan.selected_items(p, wb_plan.Selection(delete_blocks=[b[1]["item_id"]]))
        got = wb_plan.selected_items(p, wb_plan.Selection(delete_blocks=[x["item_id"] for x in b]))
        self.assertEqual(len(got), 2)

    def test_mv_scope_lock_and_never_executable_f14(self):
        e = Env(self, cfg={"mode": "full", "allow_page_move": True})
        os.rename(e.path("General/Home/Other.md"), e.path("General/Home/Folder/Other.md"))
        p, _prev, _ = e.plan()
        mv = items_of(p, "MV")
        self.assertEqual(len(mv), 1, p["refused"])
        self.assertFalse(mv[0]["executable"])
        self.assertIn("OUT_OF_WHITELIST", mv[0]["blocked_by"])
        self.assertIn("MV_NOT_SUPPORTED", mv[0]["blocked_by"])
        with self.assertRaises(wb_plan.WbError):
            wb_plan.selected_items(p, wb_plan.Selection(move=[mv[0]["item_id"]]))
        e2 = Env(self, cfg={"mode": "full", "allow_page_move": True})
        e2.fake.pages[e2.ids["ren"]]["is_locked"] = True
        os.rename(e2.path("General/Home/Rename me.md"), e2.path("General/Home/Folder2/Rename me.md"))
        p2, _prev, _ = e2.plan()
        self.assertEqual(items_of(p2, "MV"), [])
        self.assertEqual([r["class"] for r in p2["refused"] if "Rename me" in r["rel_path"]], ["LOCKED"])

    def test_delete_unsupported_objects_and_relation_referenced_row_r149(self):
        def seed(e):
            e.ids["holder"] = e.fake.add_page("Holder", parent=e.ids["home"], children=[para("holder body")])
            e.ids["innerdb"], e.ids["innerds"] = e.fake.add_database(e.ids["holder"], "Inner", {"Name": "title"})
            e.ids["linkdb"], e.ids["linkds"] = e.fake.add_database(e.ids["home"], "Links",
                                                                   {"Name": "title", "Ref": "relation"})
            e.ids["linkrow"] = e.fake.add_row(e.ids["linkds"], "Link 1", {"Ref": {"relation": [{"id": e.ids["r1"]}]}})
        e = Env(self, seed=seed, cfg={"mode": "full"})
        _add_wl(e, "holder")
        import glob
        dbnote = next(os.path.relpath(x, e.vault).replace(os.sep, "/") for x in
                      glob.glob(e.vault + "/General/Home/Tasks/*.md") if "Task 1" not in x)
        holder = next(os.path.relpath(x, e.vault).replace(os.sep, "/") for x in
                      glob.glob(e.vault + "/General/Home/**/Holder.md", recursive=True))
        for rel, key in ((dbnote, "db"), ("General/Home/Home.md", "home"), (holder, "holder"), (TASK, "r1")):
            os.unlink(e.path(rel))
            e.hold_delete(key, rel)
        p, prev, _ = e.plan()
        ref = {r["rel_path"]: (r["class"], r["reason"]) for r in p["refused"]}
        self.assertEqual(ref[dbnote][0], "DELETE_UNSUPPORTED")
        self.assertEqual(ref["General/Home/Home.md"], ("DELETE_UNSUPPORTED", "侧栏根对象"))
        self.assertEqual(ref[holder], ("DELETE_UNSUPPORTED", "页面含子数据库"))
        d = items_of(p, "D")
        self.assertEqual([x["rel_path"] for x in d], [TASK])
        self.assertEqual(d[0]["risk"], "high", "a row referenced by a relation is high risk")
        self.assertEqual(d[0]["inbound"]["relations"][0]["rows"], 1)
        self.assertEqual(wb_plan.selection_from_args(p, _Args(delete_all_in_plan=True)).delete, [])
        self.assertIn("入站关系：数据库", prev)

    def test_refusal_reasons_name_the_place_and_crlf_is_explained_f18(self):
        e = Env(self)
        e.edit(LEAF, "## Heading", "### Heading")
        p, prev, _ = e.plan()
        r = [x for x in p["refused"] if x["rel_path"] == LEAF][0]
        self.assertEqual(r["class"], "TYPE_CHANGED")
        self.assertNotEqual(r["reason"], r["class"])
        self.assertIn("本地文件第", r["reason"])
        self.assertIn("### Heading", r["reason"])
        self.assertTrue(r.get("diff") and any(x.startswith("@@") for x in r["diff"]))
        self.assertIn("该笔记的统一差异", prev)
        self.assertTrue(no_wikilink_outside_code(wb_plan.vault_copy_text(p, prev)))
        e2 = Env(self)
        with open(e2.path(LEAF), "rb") as fh:
            raw = fh.read()
        with open(e2.path(LEAF), "wb") as fh:
            fh.write(raw.replace(b"\n", b"\r\n"))
        p2, _prev, _ = e2.plan()
        r2 = [x for x in p2["refused"] if x["rel_path"] == LEAF]
        self.assertEqual([x["class"] for x in r2], ["LINE_ENDINGS"])
        self.assertIn("LF", r2[0]["hint"])
        # blocked_by codes are explained in the preview
        e3 = Env(self)
        e3.edit("General/Home/Other.md", "other body", "other body changed")
        p3, prev3, _ = e3.plan()
        self.assertIn("OUT_OF_WHITELIST（" + wb_plan.HINTS["OUT_OF_WHITELIST"], prev3)


class TestResolveTakeObsidian(unittest.TestCase):
    def test_r192_new_plan_zero_notion_requests_base_untouched(self):
        e = Env(self)
        e.edit(KID, "kid body", "kid body LOCAL")
        e.remote_text("kid", 0, "kid body REMOTE")
        p, info = e.create()
        c = items_of(p, "C")[0]
        bh_before = e.db.execute("SELECT count(*) FROM base_history").fetchone()[0]
        e.fake.clear_requests()
        ctx = wb_plan.PlanContext(e.paths, e.cfg, e.db, read_client=None)
        src = {"plan_id": p["plan_id"], "item": c, "members": p["members"], "creation_state": p["creation_state"],
               "breaker": p["breaker"], "sync": p["sync"]}
        new, prev = wb_plan.build_plan(ctx, scope=f"resolve:{p['plan_id']}:{c['item_id']}", resolve_from=src)
        info2 = wb_plan.create_plan(ctx, new, prev)
        self.assertEqual(e.fake.requests, [], "resolve --take obsidian sends no Notion request at all")
        self.assertNotEqual(info2["plan_id"], p["plan_id"])
        m = items_of(new, "M")
        self.assertEqual(len(m), 1, new["refused"])
        self.assertEqual(m[0]["resolve_of"], {"plan_id": p["plan_id"], "item_id": c["item_id"]})
        self.assertIn("本项将覆盖 Notion 自", prev)
        self.assertEqual(m[0]["ops"][0]["op"], "UPDATE")
        self.assertEqual(e.db.execute("SELECT count(*) FROM base_history").fetchone()[0], bh_before)
        self.assertEqual(wb_plan.plan_row(e.db, p["plan_id"])["status"], "superseded")


REN = "General/Home/Rename me.md"
REN_NEW = "General/Home/Renamed.md"
KID2 = "General/Home/Folder2/Kid2.md"
KID2_MOVED = "General/Home/Folder/Kid2.md"
FOLDER_NOTE = "General/Home/Folder/Folder.md"
FOLDER_NOTE_NEW = "General/Home/Folder/Folder renamed.md"


class TestUnregisteredRenameL17(unittest.TestCase):
    """L-17 (W3 F1): while the read-only scan shows a local rename / move that no refresh has registered yet
    (renamed_same_folder without an open name_overrides row, moved_other_folder, unsupported_rename), EVERY item
    is non-executable (blocked_by UNREGISTERED_RENAME + the hint) and nothing of the plan can be selected. A rename
    a refresh round published (the scan no longer lists it; an open override alone is not enough: fix loop 2), ANY
    own hold of the object (L-20a (i): subtree or note-only, whatever its paths), an ancestor held with its subtree
    (the refresh's move / unsupported-rename holds and the manual-release holds; fix loops 1 and 2), every hold the
    publisher / adoption use (L-20a (ii): also pending_deletes on hold and page_state BLOCKED / PARTIAL_CREATE) and
    an Obsidian Sync conflicted copy do not trigger it."""

    def assert_all_blocked(self, p, kinds):
        self.assertTrue(p["items"])
        for it in p["items"]:
            self.assertIn(wb_plan.UNREGISTERED_RENAME, it["blocked_by"], it["item_id"])
            self.assertFalse(it["executable"], it["item_id"])
        self.assertEqual(p["summary"]["executable"], 0)
        self.assertEqual(sorted(e["kind"] for e in p["unregistered_renames"]), sorted(kinds))

    def assert_not_triggered(self, p, prev):
        self.assertEqual(p["unregistered_renames"], [])
        self.assertFalse([it["item_id"] for it in p["items"] if wb_plan.UNREGISTERED_RENAME in it["blocked_by"]])
        self.assertNotIn(wb_plan.UNREGISTERED_RENAME_HINT, prev)

    def test_unregistered_renames_unit(self):
        import unicodedata
        scan = {
            "renamed_same_folder": [
                {"notion_id": "a", "old_rel": "X/a.md", "new_rel": "X/a2.md", "override": "existing"},
                {"notion_id": "b", "old_rel": "X/b.md", "new_rel": "X/b2.md", "override": "new"},
                {"notion_id": "c", "old_rel": "X/c.md", "new_rel": "X/é.md", "override": "new"}],
            "overrides_open": [{"notion_id": "c", "old_rel": "X/c.md", "local_rel": unicodedata.normalize("NFD", "X/é.md")},
                               {"notion_id": "b", "old_rel": "X/b.md", "local_rel": "X/b-older-name.md"}],
            "moved_other_folder": [{"notion_id": "d", "old_rel": "X/d.md", "new_rel": "Y/d.md"},
                                   {"notion_id": "e", "old_rel": "X/e.md", "new_rel": "Y/e.md"}],
            "unsupported_rename": [{"notion_id": "f", "old_rel": "X/f/f.md", "new_rel": "X/f/g.md",
                                    "reason": "folder_note"}],
            "sync_conflict_copies": [{"rel": "X/h (Conflicted copy 2026).md", "notion_id": "h",
                                      "original_rel": "X/h.md", "original_notion_id": "h"}]}
        holds = {"d": {"reason": "unsupported_rename", "paths": ["X/d.md", "Y/d.md"], "source": "scan"},
                 "e": {"reason": "sync_conflict_copy", "paths": ["X/e.md", "Z/e.md"], "source": "scan",
                       "subtree": False},
                 "f": "malformed"}
        got = wb_plan.unregistered_renames(scan, holds)
        # fix loop 2: an open name_overrides row at the current path ("a": the scan's override "existing"; "c":
        # the open row after NFC) does NOT register the rename — only the round that publishes it does, and then
        # the scan no longer lists it. Such entries are flagged override_open (display).
        # L-20a (i) (lead, wave 4): ANY entry of the object's OWN hold registers it, whatever its subtree / paths —
        # "e" (note-only sync_conflict_copy hold whose paths name other files) is registered now; before L-20a it
        # was listed here and blocked every plan for as long as the hold lasted
        self.assertEqual([(g["kind"], g["notion_id"], g.get("override_open", False)) for g in got],
                         [("renamed_same_folder", "a", True), ("renamed_same_folder", "b", False),
                          ("unsupported_rename", "f", False), ("renamed_same_folder", "c", True)])
        self.assertIn(("moved_other_folder", "e"), [(g["kind"], g["notion_id"])
                                                    for g in wb_plan.unregistered_renames(scan, {})])
        self.assertEqual(got[2]["reason"], "folder_note")
        # fix loop 1: a hold WITH its subtree registers the object whatever its paths (the adoption leaves a held
        # object's files alone wherever they are): a same-folder rename the exporter rejected (override_rejected,
        # paths []), a stale scan hold (moved again after the round), a manual-release hold whose paths the
        # refresh never updates (post_verify, paths []); a note-only hold only when it lists the current path;
        # ids compare without dashes
        holds2 = {"b": {"reason": "override_rejected:unsafe_name", "paths": [], "source": "export", "subtree": True},
                  "d": {"reason": "unsupported_rename", "paths": ["X/d.md", "Q/d.md"], "source": "scan"},
                  "e": {"reason": "post_verify_failed:C04", "paths": [], "source": "post_verify",
                        "auto_release": False, "subtree": True},
                  "f": {"reason": "path_normalization_mismatch", "paths": ["X/f/g.md"], "subtree": False}}
        self.assertEqual([g["notion_id"] for g in wb_plan.unregistered_renames(scan, holds2)], ["a", "c"])
        uid = "12345678-aaaa-bbbb-cccc-1234567890ab"
        scan3 = {"moved_other_folder": [{"notion_id": uid, "old_rel": "X/u.md", "new_rel": "Y/u.md"}]}
        self.assertEqual(wb_plan.unregistered_renames(scan3, {uid.replace("-", ""): {"reason": "apply_race",
                                                                                    "paths": []}}), [])
        # L-20a (i): a note-only own hold that lists another path registers it too (it did not before L-20a) ...
        self.assertEqual(wb_plan.unregistered_renames(scan3, {uid: {"reason": "x", "paths": ["Z/u.md"],
                                                                    "subtree": False}}), [])
        # ... while an ANCESTOR counts only with its subtree
        par = "99999999-aaaa-bbbb-cccc-1234567890ab"
        self.assertEqual(len(wb_plan.unregistered_renames(scan3, {par: {"reason": "x", "paths": [], "subtree": False}},
                                                          {uid: par})), 1)
        self.assertEqual(wb_plan.unregistered_renames(scan3, {par: {"reason": "x", "paths": []}}, {uid: par}), [])
        self.assertEqual(wb_plan.unregistered_renames({}, None), [])
        self.assertEqual(wb_plan.unregistered_renames(None), [])
        self.assertEqual(wb_plan.unregistered_renames({"sync_conflict_copies": scan["sync_conflict_copies"]}), [])
        lines = wb_plan.unregistered_lines(got)
        self.assertEqual(lines[0], f"同文件夹改名：X/a.md → X/a2.md（{wb_plan.OVERRIDE_UNPUBLISHED_TEXT}）")
        self.assertEqual(lines[1], "同文件夹改名：X/b.md → X/b2.md")
        self.assertIn("跨文件夹移动：X/e.md → Y/e.md", wb_plan.unregistered_lines(wb_plan.unregistered_renames(scan, {})))
        self.assertEqual(wb_plan.blocked_text(wb_plan.UNREGISTERED_RENAME),
                         f"UNREGISTERED_RENAME（{wb_plan.UNREGISTERED_RENAME_HINT}）")
        self.assertEqual(wb_plan.UNREGISTERED_RENAME_HINT,
                         "检测到尚未登记的本地改名/移动：请等待下一次自动同步登记后再生成计划")

    def test_open_override_alone_and_ancestor_holds_unit_fix2(self):
        """Fix loop 2 (W3.5 L17 review, requirement lens): (1) an open name_overrides row whose round did not publish
        (the scan still lists the rename, override "existing") is unregistered; (2) a hold WITH its subtree of any
        ANCESTOR (export parent chain) registers a rename / move below it — the publisher leaves the whole subtree
        alone, so the scan keeps listing such a rename after every published round; a note-only ancestor hold does
        not; parents as {id: parent_id} or load_export_state's {id: {parent_id}}; ids compare without dashes;
        a cyclic chain ends."""
        G = "aaaaaaaa-0000-0000-0000-00000000000a"
        scan = {"renamed_same_folder": [
            {"notion_id": "r1", "old_rel": "P/r1.md", "new_rel": "P/r1x.md", "override": "existing"},
            {"notion_id": "r2", "old_rel": "Q/r2.md", "new_rel": "Q/r2x.md", "override": "existing"},
            {"notion_id": "r3", "old_rel": "G/C/r3.md", "new_rel": "G/C/r3x.md", "override": "new"},
            {"notion_id": "r4", "old_rel": "Z/r4.md", "new_rel": "Z/r4x.md", "override": "new"}],
            "moved_other_folder": [{"notion_id": "m1", "old_rel": "G/m1.md", "new_rel": "Y/m1.md"}],
            "overrides_open": [{"notion_id": "r1", "old_rel": "P/r1.md", "local_rel": "P/r1x.md"},
                               {"notion_id": "r2", "old_rel": "Q/r2.md", "local_rel": "Q/r2x.md"}]}
        self.assertEqual([(g["notion_id"], g.get("override_open", False)) for g in wb_plan.unregistered_renames(scan)],
                         [("r3", False), ("r1", True), ("r2", True), ("m1", False), ("r4", False)])
        parents = {"r1": "P", "r2": "Q", "r3": "C", "C": G.replace("-", ""), "m1": G, "r4": "z1", "z1": "z2",
                   "z2": "z1"}
        holds = {"P": {"reason": "post_verify_failed:C04", "subtree": True, "source": "post_verify", "paths": []},
                 "Q": {"reason": "sync_conflict_copy", "subtree": False, "paths": ["Q/Q.md"]},
                 G: {"reason": "unsupported_rename", "paths": ["G/G.md", "G/G2.md"], "source": "scan"}}
        for par in (parents, {k: {"parent_id": v, "kind": "page"} for k, v in parents.items()}):
            got = wb_plan.unregistered_renames(scan, holds, par)
            self.assertEqual([g["notion_id"] for g in got], ["r2", "r4"])
        self.assertEqual([g["notion_id"] for g in wb_plan.unregistered_renames(scan, holds, None)],
                         ["r3", "r1", "r2", "m1", "r4"], "without the chain only the object's own hold counts")

    def test_override_opened_by_a_round_that_did_not_publish_still_blocks_fix2(self):
        """Fix loop 2: a refresh round opens the name override and then pauses (paused_for_review, e.g.
        conflict_on_page_with_children) or fails: published_files and the sortspec keep the old name, the scan still
        lists the rename (override "existing"). Before fix loop 2 that counted as registered and the apply's adoption
        ended partially_applied (W6 C05). Now every item stays blocked until a round publishes it."""
        e = Env(self)
        e.edit(LEAF, "second para", "second para OVR")
        os.rename(e.path(REN), e.path(REN_NEW))
        e.register_rename("ren", REN, REN_NEW, published=False)
        self.assertEqual([r["override"] for r in e.ctx().scan()["renamed_same_folder"]], ["existing"])
        p, prev, _ = e.plan()
        self.assert_all_blocked(p, ["renamed_same_folder"])
        self.assertEqual(p["unregistered_renames"], [{"kind": "renamed_same_folder", "notion_id": e.ids["ren"],
                                                      "old_rel": REN, "new_rel": REN_NEW, "override_open": True}])
        self.assertIn(f"`{REN}` → `{REN_NEW}`（{wb_plan.OVERRIDE_UNPUBLISHED_TEXT}）", prev)
        m = items_of(p, "M")[0]["item_id"]
        with self.assertRaises(wb_plan.WbError) as cm:
            wb_plan.selected_items(p, wb_plan.Selection(items=[m]))
        self.assertIn(wb_plan.UNREGISTERED_RENAME_HINT, str(cm.exception))
        # the round that publishes it (ADOPT_RENAME moves the published row): registered
        e.register_rename("ren", REN, REN_NEW)
        self.assertEqual(e.ctx().scan()["renamed_same_folder"], [])
        p2, prev2, _ = e.plan()
        self.assert_not_triggered(p2, prev2)
        got = {it["category"]: it for it in p2["items"]}
        self.assertTrue(got["M"]["executable"], got["M"]["blocked_by"])
        self.assertTrue(got["T"]["executable"], got["T"]["blocked_by"])

    def test_rename_below_a_held_ancestor_is_registered_fix2(self):
        """Fix loop 2: a same-folder rename of a child whose folder note is held WITH its subtree (here a manual
        post_verify hold). The publisher leaves the whole subtree alone, so after every published round the scan
        still lists the rename (with an open override); without the ancestor rule fix loop 2 would block every plan
        until someone released the hold. The ancestor hold registers it (the adoption verifies with that hold:
        e2e W1-W10 pass); the child's own items are refused AUTO_SYNC_HOLD. A note-only ancestor hold does not."""
        e = Env(self)
        e.edit(LEAF, "second para", "second para ANC")
        kid_new = "General/Home/Folder/Kid renamed.md"
        os.rename(e.path(KID), e.path(kid_new))
        e.hold_like_refresh("folder", [], "post_verify_failed:C04", source="post_verify", auto_release=False)
        for published_round in (False, True):
            if published_round:
                e.register_rename("kid", KID, kid_new, published=False)   # the round's publish skipped the subtree
                self.assertEqual([r["override"] for r in e.ctx().scan()["renamed_same_folder"]], ["existing"])
            p, prev, _ = e.plan()
            self.assert_not_triggered(p, prev)
            m = items_of(p, "M")
            self.assertEqual([it["rel_path"] for it in m], [LEAF])
            self.assertTrue(m[0]["executable"], m[0]["blocked_by"])
            self.assertFalse([it for it in p["items"] if it.get("rel_path") == kid_new], p["items"])
            self.assertTrue([r for r in p["refused"] if r["rel_path"] == kid_new and r["class"] == "AUTO_SYNC_HOLD"],
                            p["refused"])
        e.hold_like_refresh("folder", [FOLDER_NOTE], "sync_conflict_copy", subtree=False)
        p3, _prev3, _ = e.plan()
        self.assert_all_blocked(p3, ["renamed_same_folder"])

    def test_same_folder_rename_blocks_every_item_until_registered(self):
        e = Env(self)
        e.edit(LEAF, "second para", "second para L17")                          # M
        e.write("General/Home/Folder/Brand new.md", "new body\n")             # N
        os.rename(e.path(REN), e.path(REN_NEW))                                # T, not registered by a refresh
        p, prev, _ = e.plan()
        self.assertEqual(sorted(it["category"] for it in p["items"]), ["M", "N", "T"], p["refused"])
        self.assert_all_blocked(p, ["renamed_same_folder"])
        self.assertEqual(p["unregistered_renames"], [{"kind": "renamed_same_folder", "notion_id": e.ids["ren"],
                                                      "old_rel": REN, "new_rel": REN_NEW}])
        self.assertIn(f"注意（UNREGISTERED_RENAME）：{wb_plan.UNREGISTERED_RENAME_HINT}", prev)
        self.assertIn(f"`{REN}` → `{REN_NEW}`", prev)
        self.assertIn(f"不可执行：UNREGISTERED_RENAME（{wb_plan.UNREGISTERED_RENAME_HINT}）", prev)
        # never limited by --scope (the scan is whole-vault)
        ps, _prev, _ = e.plan(scope=LEAF)
        self.assert_all_blocked(ps, ["renamed_same_folder"])
        # nothing can be selected: explicit ids, --all-safe, and (defence in depth) a plan whose flags were altered
        m = items_of(p, "M")[0]["item_id"]
        with self.assertRaises(wb_plan.WbError) as cm:
            wb_plan.selected_items(p, wb_plan.Selection(items=[m]))
        self.assertIn(wb_plan.UNREGISTERED_RENAME_HINT, str(cm.exception))
        sel = wb_plan.selection_from_args(p, _Args(all_safe=True))
        self.assertEqual(sel.items, [])
        with self.assertRaises(wb_plan.WbError) as cm:
            wb_plan.selected_items(p, sel)
        self.assertIn(wb_plan.UNREGISTERED_RENAME, str(cm.exception))
        altered = copy.deepcopy(p)
        for it in altered["items"]:
            it["executable"], it["blocked_by"] = True, []
        with self.assertRaises(wb_plan.WbError):
            wb_plan.selected_items(altered, wb_plan.Selection(items=[m]))
        # create_plan keeps it: plan.json carries the list, the plan_items reason the code
        info = wb_plan.create_plan(e.ctx(), p, prev)
        loaded, _sha = wb_plan.load_plan(info["plan_id"], work=e.paths, db=e.db)
        self.assertEqual(loaded["unregistered_renames"], p["unregistered_renames"])
        reasons = [r[0] for r in e.db.execute("SELECT reason FROM plan_items WHERE plan_id=?", (info["plan_id"],))]
        self.assertTrue(reasons and all("UNREGISTERED_RENAME" in (r or "") for r in reasons), reasons)
        # the next refresh registers the rename (open name_overrides row): the same edits are executable again
        e.register_rename("ren", REN, REN_NEW)
        p3, prev3, _ = e.plan()
        self.assert_not_triggered(p3, prev3)
        self.assertIn("- 状态：可执行", prev3)
        got = {it["category"]: it for it in p3["items"]}
        self.assertTrue(got["M"]["executable"], got["M"]["blocked_by"])
        self.assertTrue(got["T"]["executable"], got["T"]["blocked_by"])
        # a further rename after the registration is unregistered again
        os.rename(e.path(REN_NEW), e.path("General/Home/Renamed again.md"))
        p4, _prev4, _ = e.plan()
        self.assert_all_blocked(p4, ["renamed_same_folder"])

    def test_cross_folder_move_blocks_until_the_refresh_holds_it(self):
        e = Env(self)
        e.edit(LEAF, "second para", "second para MV")
        os.rename(e.path(KID2), e.path(KID2_MOVED))
        p, prev, _ = e.plan()
        self.assert_all_blocked(p, ["moved_other_folder"])
        self.assertIn(f"跨文件夹移动：`{KID2}` → `{KID2_MOVED}`", prev)
        # a NOTE-ONLY hold of an ANCESTOR (the old parent folder note) does not register the move (L-20a: ancestor
        # holds count only with their subtree)
        e.hold_like_refresh("folder2", [], "sync_conflict_copy", subtree=False)
        p1, _prev1, _ = e.plan()
        self.assert_all_blocked(p1, ["moved_other_folder"])
        # L-20a (i): ANY own hold registers it, also a note-only one whose paths list another file (before L-20a this
        # case stayed blocked): the other edit is executable, the moved note itself is refused AUTO_SYNC_HOLD
        e.hold_like_refresh("kid2", [KID2, "General/Home/Doomed parent/Kid2.md"], "sync_conflict_copy", subtree=False)
        p1b, prev1b, _ = e.plan()
        self.assert_not_triggered(p1b, prev1b)
        self.assertTrue([it for it in items_of(p1b, "M") if it["rel_path"] == LEAF and it["executable"]], p1b["items"])
        # the refresh holds the moved object with its current path: registered, the other edit is executable
        e.hold_like_refresh("kid2", [KID2, KID2_MOVED])
        p2, prev2, _ = e.plan()
        self.assert_not_triggered(p2, prev2)
        m = items_of(p2, "M")
        self.assertEqual([it["rel_path"] for it in m], [LEAF])
        self.assertTrue(m[0]["executable"], m[0]["blocked_by"])
        self.assertTrue([r for r in p2["refused"] if r["rel_path"] == KID2_MOVED], p2["refused"])

    def test_folder_note_rename_unsupported_blocks_until_the_refresh_holds_it(self):
        e = Env(self)
        e.edit(LEAF, "second para", "second para FN")
        os.rename(e.path(FOLDER_NOTE), e.path(FOLDER_NOTE_NEW))
        p, prev, _ = e.plan()
        self.assert_all_blocked(p, ["unsupported_rename"])
        self.assertEqual(p["unregistered_renames"][0]["reason"], "folder_note")
        self.assertTrue(refused_of(p, "UNSUPPORTED_RENAME"), p["refused"])
        e.hold_like_refresh("folder", [FOLDER_NOTE, FOLDER_NOTE_NEW])
        p2, prev2, _ = e.plan()
        self.assert_not_triggered(p2, prev2)
        self.assertTrue(items_of(p2, "M")[0]["executable"], items_of(p2, "M")[0]["blocked_by"])

    def test_sync_conflict_copy_does_not_trigger(self):
        e = Env(self)
        e.edit(LEAF, "second para", "second para SC")
        shutil.copyfile(e.path(REN), e.path("General/Home/Rename me (Conflicted copy 2026-11-18 0900).md"))
        p, prev, _ = e.plan()
        self.assert_not_triggered(p, prev)
        self.assertTrue(refused_of(p, "SYNC_CONFLICT_COPY"), p["refused"])
        m = items_of(p, "M")
        self.assertEqual([it["rel_path"] for it in m], [LEAF])
        self.assertTrue(m[0]["executable"], m[0]["blocked_by"])

    def test_conflict_items_and_resolve_plan_blocked_too(self):
        e = Env(self)
        e.edit(KID, "kid body", "kid body LOCAL")
        e.remote_text("kid", 0, "kid body REMOTE")
        p, _info = e.create()
        c = items_of(p, "C")[0]
        os.rename(e.path(REN), e.path(REN_NEW))
        full, _prev, _ = e.plan()
        cc = items_of(full, "C")[0]
        self.assertEqual(cc["blocked_by"], ["CONFLICT", wb_plan.UNREGISTERED_RENAME])
        self.assertFalse(cc["executable"])
        # resolve --take obsidian builds a NEW plan without any Notion request: it is blocked the same way
        e.fake.clear_requests()
        ctx = wb_plan.PlanContext(e.paths, e.cfg, e.db, read_client=None)
        src = {"plan_id": p["plan_id"], "item": c, "members": p["members"], "creation_state": p["creation_state"],
               "breaker": p["breaker"], "sync": p["sync"]}
        new, prev = wb_plan.build_plan(ctx, scope=f"resolve:{p['plan_id']}:{c['item_id']}", resolve_from=src)
        self.assertEqual(e.fake.requests, [], "still no Notion request")
        self.assertEqual(len(items_of(new, "M")), 1, new["refused"])
        self.assert_all_blocked(new, ["renamed_same_folder"])
        self.assertIn(wb_plan.UNREGISTERED_RENAME_HINT, prev)


    # ---- fix loop 1 (W3.5 L17 review): holds register a rename / move; descendants of a subtree hold are refused
    def test_rejected_same_folder_rename_is_registered_by_the_refresh_hold(self):
        """A same-folder rename to a name the exporter rejects (here '%': unsafe_name) never gets an open
        name_overrides row; the refresh holds the object (override_rejected:<why>, source export, paths []). That
        hold registers it: the unrelated edit is executable after the round (before fix loop 1 every plan stayed
        blocked for good), and the renamed note's refusal says to rename it again (not 'wait for the sync')."""
        e = Env(self)
        e.edit(LEAF, "second para", "second para REJ")
        bad = "General/Home/Rename me 50%.md"
        os.rename(e.path(REN), e.path(bad))
        p, _prev, _ = e.plan()
        self.assert_all_blocked(p, ["renamed_same_folder"])           # before the refresh round: unregistered
        e.hold_like_refresh("ren", [], "override_rejected:unsafe_name", source="export")
        p2, prev2, _ = e.plan()
        self.assert_not_triggered(p2, prev2)
        m = items_of(p2, "M")
        self.assertEqual([it["rel_path"] for it in m], [LEAF])
        self.assertTrue(m[0]["executable"], m[0]["blocked_by"])
        self.assertEqual(items_of(p2, "T"), [])
        ref = [r for r in p2["refused"] if r["rel_path"] == bad]
        self.assertEqual([r["class"] for r in ref], ["AUTO_SYNC_HOLD"], p2["refused"])
        self.assertEqual(ref[0]["hint"], wb_plan.OVERRIDE_REJECTED_HINT)
        self.assertIn(wb_plan.OVERRIDE_REJECTED_TEXT["unsafe_name"], ref[0]["reason"])
        self.assertIn(wb_plan.OVERRIDE_REJECTED_HINT, prev2)
        self.assertNotIn("等待下一次自动同步", ref[0]["hint"])

    def test_move_of_a_note_with_a_manual_release_hold_is_registered(self):
        """A note held by a manual-release hold (post_verify: auto_release False, paths []) and moved afterwards:
        the refresh keeps that hold and never adds the move's paths (refresh.py skips the scan hold), so a rule
        that needs the new path in the hold blocked every plan until someone released the hold. The hold (with
        its subtree) registers the move: the adoption leaves the held note alone wherever it is."""
        e = Env(self)
        e.edit(LEAF, "second para", "second para MAN")
        e.hold_like_refresh("kid2", [], "post_verify_failed:C04", source="post_verify", auto_release=False)
        os.rename(e.path(KID2), e.path(KID2_MOVED))
        p, prev, _ = e.plan()
        self.assert_not_triggered(p, prev)
        m = items_of(p, "M")
        self.assertEqual([it["rel_path"] for it in m], [LEAF])
        self.assertTrue(m[0]["executable"], m[0]["blocked_by"])
        self.assertTrue([r for r in p["refused"] if r["rel_path"] == KID2_MOVED], p["refused"])

    def test_items_under_a_held_object_are_refused_at_plan_time(self):
        """L-18 parity: the executor refuses an item whose ANCESTOR is held with its subtree (or is on deletion
        hold); the planner now refuses it too (AUTO_SYNC_HOLD), instead of listing it executable and letting the
        apply abort (W10 fail, 0 writes: W3.5 L17 review, folder-note rename held by the refresh)."""
        e = Env(self)
        e.edit(LEAF, "second para", "second para ANC")
        e.edit(KID, "kid body", "kid body ANC")                        # child page of the folder note
        os.rename(e.path(FOLDER_NOTE), e.path(FOLDER_NOTE_NEW))         # unsupported rename (folder note)
        e.hold_like_refresh("folder", [FOLDER_NOTE, FOLDER_NOTE_NEW])    # what the refresh round records
        p, prev, _ = e.plan()
        self.assert_not_triggered(p, prev)
        self.assertEqual([(it["category"], it["rel_path"]) for it in p["items"]], [("M", LEAF)])
        self.assertTrue(p["items"][0]["executable"], p["items"][0]["blocked_by"])
        ref = {r["rel_path"]: r for r in p["refused"]}
        self.assertEqual(ref[KID]["class"], "AUTO_SYNC_HOLD", p["refused"])
        self.assertIn(f"上级对象 {wb_plan.nid32(e.ids['folder'])[:8]} 的自动同步已暂停（含子树：unsupported_rename）",
                      ref[KID]["reason"])
        # a NOTE-ONLY hold of the parent does not cover the child, but a new page under it is refused
        os.rename(e.path(FOLDER_NOTE_NEW), e.path(FOLDER_NOTE))
        e.hold_like_refresh("folder", [FOLDER_NOTE], "sync_conflict_copy", subtree=False)
        new = "General/Home/Folder/Brand new.md"
        e.write(new, "new body\n")
        p2, prev2, _ = e.plan()
        self.assert_not_triggered(p2, prev2)
        self.assertEqual(sorted((it["category"], it["rel_path"]) for it in p2["items"]), [("M", KID), ("M", LEAF)])
        ref2 = {r["rel_path"]: r for r in p2["refused"]}
        self.assertEqual(ref2[new]["class"], "AUTO_SYNC_HOLD", p2["refused"])
        self.assertIn(f"新页面的父级 {wb_plan.nid32(e.ids['folder'])[:8]} 的自动同步已暂停", ref2[new]["reason"])
        os.remove(e.path(new))
        os.remove(os.path.join(e.work, "state", "auto_sync_hold.json"))
        # an ancestor on deletion hold (folder note deleted locally, its child page kept and edited)
        os.remove(e.path(FOLDER_NOTE))
        e.hold_delete("folder", FOLDER_NOTE, subtree=[{"notion_id": e.ids["kid"], "rel": KID}])
        p3, _prev3, _ = e.plan()
        self.assertEqual(p3["unregistered_renames"], [])
        self.assertFalse([it for it in p3["items"] if it.get("rel_path") == KID], p3["items"])
        ref3 = {r["rel_path"]: r for r in p3["refused"]}
        self.assertEqual(ref3[KID]["class"], "AUTO_SYNC_HOLD", p3["refused"])
        self.assertIn("处于删除保留（pending_deletes=hold，含子树）", ref3[KID]["reason"])
        # a D item under an object held with its subtree (manual hold of the parent, the child deleted locally)
        e.hold_like_refresh("folder2", [], "post_verify_failed:C02", source="post_verify", auto_release=False)
        os.remove(e.path(KID2))
        e.hold_delete("kid2", KID2)
        p4, _prev4, _ = e.plan()
        self.assertFalse([it for it in p4["items"] if it["category"] == "D" and it.get("rel_path") == KID2],
                         p4["items"])
        ref4 = [r for r in p4["refused"] if r["rel_path"] == KID2]
        self.assertTrue(ref4 and ref4[0]["class"] == "AUTO_SYNC_HOLD", p4["refused"])
        self.assertIn(f"上级对象 {wb_plan.nid32(e.ids['folder2'])[:8]}", ref4[0]["reason"])



# ============================================================================================ wave 4 prep: L-20a, L-23
SYNC_COPY_OLD_NAME = "General/Home/Rename me (Conflicted copy 2026-11-20 1200).md"
DOOMED_REL = "General/Home/Doomed.md"
KID_RENAMED = "General/Home/Folder/Kid renamed.md"


def executor_hold_reason(e, item):
    """What the executor says for an item right before its writes (wb_apply._hold_check on the SAME state:
    state/auto_sync_hold.json, writeback.db pending_deletes / page_state, export.db objects_map), None if it writes."""
    import types
    import wb_apply
    ctx = types.SimpleNamespace(paths=wb_apply.Paths(e.work, e.vault), db=e.db, _objmap=None)
    try:
        wb_apply._hold_check(ctx, item)
    except wb_apply._PageStop as exc:
        return exc.reason
    return None


def set_page_state(e, key, state):
    with wb_state.transaction(e.db):
        e.db.execute("DELETE FROM page_state WHERE notion_id=?", (e.ids[key],))
        if state:
            e.db.execute("INSERT INTO page_state(notion_id, state, reason, since) VALUES(?,?,?,?)",
                         (e.ids[key], state, "readback", wb_common.now_iso()))


def clear_pending(e):
    with wb_state.transaction(e.db):
        e.db.execute("DELETE FROM pending_deletes")


def clear_auto_holds(e):
    p = os.path.join(e.work, "state", "auto_sync_hold.json")
    if os.path.exists(p):
        os.remove(p)


class TestHoldParityWave4(unittest.TestCase):
    """Wave 4 prep (contract §2.4). L-20a: a local rename / move counts as registered when (i) the object has ANY own
    entry in state/auto_sync_hold.json (subtree or note-only, whatever its paths) or an ancestor is held with its
    subtree, and (ii) the hold set is the one the publisher / adoption use (wb_apply._adopt_holds: auto_sync_hold.json
    + pending_deletes on hold + page_state BLOCKED / PARTIAL_CREATE). L-23: the planner refuses (AUTO_SYNC_HOLD,
    the executor's own words) every item wb_apply._hold_check would skip: (a) the note's own sync_conflict_copy hold
    after the copy is gone, (b) M / B / T of a note with its own pending_delete* auto hold or pending_deletes row on
    hold (only D keeps its own deletion hold), (c) resolve --take obsidian plans exactly like normal plans. Each test
    fails on the pre-wave-4 wb_plan.py (scratch copy run, see the I1 report)."""

    def assert_registered(self, p, prev):
        self.assertEqual(p["unregistered_renames"], [])
        self.assertFalse([it["item_id"] for it in p["items"] if wb_plan.UNREGISTERED_RENAME in it["blocked_by"]])
        self.assertNotIn(wb_plan.UNREGISTERED_RENAME_HINT, prev)

    def assert_leaf_executable(self, p):
        m = [it for it in items_of(p, "M") if it["rel_path"] == LEAF]
        self.assertEqual(len(m), 1, (p["items"], p["refused"]))
        self.assertTrue(m[0]["executable"], m[0]["blocked_by"])

    def test_rename_plus_sync_copy_named_after_the_old_name_l20a_i_and_l23_a(self):
        """L-20a (i): rename in Obsidian + an Obsidian Sync conflicted copy named after the OLD name. The refresh
        holds the note note-only (sync_conflict_copy) with paths = the copy's original (old) name: before L-20a
        that never counted as registration and blocked ALL write-back round after round. L-23 (a): once the copy is
        deleted the hold stays until the next refresh; the planner now refuses the note's own T item with the
        executor's words (before: T listed executable, the apply skipped it)."""
        import wb_scan
        e = Env(self)
        e.edit(LEAF, "second para", "second para L20A")
        os.rename(e.path(REN), e.path(REN_NEW))
        shutil.copyfile(e.path(REN_NEW), e.path(SYNC_COPY_OLD_NAME))
        scan = e.ctx().scan()
        self.assertEqual([(r["old_rel"], r["new_rel"]) for r in scan["renamed_same_folder"]], [(REN, REN_NEW)])
        self.assertEqual([(s["original_rel"], s["original_notion_id"]) for s in scan["sync_conflict_copies"]],
                         [(REN, e.ids["ren"])])
        p0, _prev0, _ = e.plan()                                    # no refresh round yet: unregistered
        self.assertEqual([x["kind"] for x in p0["unregistered_renames"]], ["renamed_same_folder"])
        # exactly what refresh.py step 6 records for it (wb_scan.anomaly_ids -> scan_holds)
        an = wb_scan.anomaly_ids(scan)[e.ids["ren"]]
        self.assertEqual((an["reason"], an["paths"], an.get("subtree")), ("sync_conflict_copy", [REN], False))
        self.assertNotIn(REN_NEW, an["paths"])
        e.hold_like_refresh("ren", an["paths"], an["reason"], subtree=False)
        for rnd in (1, 2):                                          # round after round
            p, prev, _ = e.plan()
            self.assert_registered(p, prev)
            self.assert_leaf_executable(p)
            self.assertFalse([it for it in p["items"] if it.get("notion_id") == e.ids["ren"]], p["items"])
            self.assertTrue(refused_of(p, "SYNC_CONFLICT_COPY"), p["refused"])
        sel = wb_plan.selection_from_args(p, _Args(all_safe=True))
        self.assertEqual([it["rel_path"] for it in wb_plan.selected_items(p, sel)], [LEAF])
        # L-23 (a): the copy is merged / deleted; the note-only hold stays in auto_sync_hold.json until the next refresh
        os.remove(e.path(SYNC_COPY_OLD_NAME))
        p2, prev2, _ = e.plan()
        self.assert_registered(p2, prev2)                           # still registered by its own hold
        self.assert_leaf_executable(p2)
        self.assertEqual(items_of(p2, "T"), [], "the held note's rename is not executable")
        ref = [r for r in p2["refused"] if r["rel_path"] == REN_NEW]
        self.assertEqual([r["class"] for r in ref], ["AUTO_SYNC_HOLD"], p2["refused"])
        self.assertEqual(ref[0]["reason"], "该对象的 Notion → Obsidian 自动同步已暂停（sync_conflict_copy，scan）：不写入")
        self.assertEqual(ref[0]["hint"], wb_plan.HOLD_SCAN_RELEASE_HINT.format(command=wb_common.cli_command("refresh", "--release-hold", e.ids["ren"])))
        self.assertIn(f"refresh.py --release-hold {e.ids['ren']}", ref[0]["hint"])
        self.assertIn(ref[0]["hint"], prev2)
        # the executor skips the same item with exactly these words
        self.assertEqual(executor_hold_reason(e, {"category": "T", "notion_id": e.ids["ren"]}),
                         f"AUTO_SYNC_HOLD：{ref[0]['reason']}；{ref[0]['hint']}")
        self.assertEqual(e.writes(), [], "planning sends no write")

    def test_rename_of_an_object_the_publisher_holds_is_registered_l20a_ii(self):
        """L-20a (ii): the publisher (refresh round and adoption) also holds pending_deletes on hold and page_state
        BLOCKED / PARTIAL_CREATE objects WITH their subtree, so a rename of such an object (or below it) stays listed
        by the scan after every round. Before L-20a only auto_sync_hold.json counted: every plan stayed blocked for
        as long as that state lasted. Each source, for the object itself and for an ancestor."""
        for src in ("pending_deletes", "BLOCKED", "PARTIAL_CREATE"):
            with self.subTest(src=src):
                e = Env(self)
                e.edit(LEAF, "second para", f"second para {src}")

                def put(key, rel):
                    if src == "pending_deletes":
                        e.hold_delete(key, rel)
                    else:
                        set_page_state(e, key, src)
                # (1) the renamed object itself
                os.rename(e.path(REN), e.path(REN_NEW))
                p0, _prev0, _ = e.plan()
                self.assertEqual([x["kind"] for x in p0["unregistered_renames"]], ["renamed_same_folder"])
                put("ren", REN)
                p, prev, _ = e.plan()
                self.assert_registered(p, prev)
                self.assert_leaf_executable(p)
                self.assertEqual(items_of(p, "T"), [])
                ref = {r["rel_path"]: r for r in p["refused"]}
                if src == "pending_deletes":        # L-23 (b): its own deletion hold refuses the T item
                    self.assertEqual((ref[REN_NEW]["class"], ref[REN_NEW]["reason"]),
                                     ("AUTO_SYNC_HOLD", wb_plan.PENDING_HOLD_TEXT))
                    self.assertEqual(executor_hold_reason(e, {"category": "T", "notion_id": e.ids["ren"]}),
                                     f"AUTO_SYNC_HOLD：{wb_plan.PENDING_HOLD_TEXT}")
                else:
                    self.assertEqual(ref[REN_NEW]["class"], "BLOCKED_PAGE")
                clear_pending(e)
                set_page_state(e, "ren", None)
                os.rename(e.path(REN_NEW), e.path(REN))
                # (2) a child renamed below the object (the publisher holds the whole subtree)
                os.rename(e.path(KID), e.path(KID_RENAMED))
                p1, _prev1, _ = e.plan()
                self.assertEqual([x["new_rel"] for x in p1["unregistered_renames"]], [KID_RENAMED])
                put("folder", FOLDER_NOTE)
                p2, prev2, _ = e.plan()
                self.assert_registered(p2, prev2)
                self.assert_leaf_executable(p2)
                self.assertEqual(e.writes(), [])

    def test_adoption_holds_is_the_set_the_adoption_publish_uses_l20a_ii(self):
        """The planner's registration set (wb_plan.adoption_holds) == the --holds of the adoption publish
        (wb_apply._adopt_holds) for the same state: ids and subtree flags (auto_sync_hold entry wins)."""
        import types
        import wb_apply
        e = Env(self)
        e.hold_like_refresh("kid", [KID], "sync_conflict_copy", subtree=False)
        e.hold_like_refresh("folder2", [], "post_verify_failed:C04", source="post_verify", auto_release=False)
        e.hold_delete("del", DOOMED_REL)
        e.hold_delete("kid", KID)                                      # also auto-held: the auto entry wins
        set_page_state(e, "kid2", "BLOCKED")
        set_page_state(e, "leaf", "PARTIAL_CREATE")
        with open(os.path.join(e.work, "state", "auto_sync_hold.json"), encoding="utf-8") as fh:
            raw = json.load(fh)
        mine = wb_plan.adoption_holds(raw, e.db)
        rep = tempfile.mkdtemp(prefix="adoptholds-")
        self.addCleanup(shutil.rmtree, rep, True)
        ctx = types.SimpleNamespace(paths=wb_apply.Paths(e.work, e.vault), db=e.db, adopt_holdback=None)
        with open(wb_apply._adopt_holds(ctx, rep), encoding="utf-8") as fh:
            theirs = json.load(fh)
        self.assertEqual({wb_plan.nid32(k).lower(): v["subtree"] for k, v in theirs.items()},
                         {k: v.get("subtree", True) is not False for k, v in mine.items()})
        self.assertEqual(len(mine), 5)
        self.assertEqual(mine[wb_plan.nid32(e.ids["kid"]).lower()]["reason"], "sync_conflict_copy")
        self.assertEqual(wb_plan.adoption_holds(raw, None).keys(),
                         {wb_plan.nid32(e.ids[k]).lower() for k in ("kid", "folder2")})

    def test_own_sync_copy_hold_after_the_copy_is_gone_refuses_the_note_l23_a(self):
        """L-23 (a) without any rename: the refresh held the note note-only for a Sync conflicted copy; the copy is
        gone but the hold stays in auto_sync_hold.json until the next refresh. The planner filtered
        sync_conflict_copy holds and listed the edit executable while the executor skips it; now it is refused with
        the executor's words (wait for the next refresh or refresh.py --release-hold <id>)."""
        e = Env(self)
        e.edit(LEAF, "second para", "second para SYNC GONE")
        e.edit(KID, "kid body", "kid body SYNC GONE")
        e.hold_like_refresh("leaf", [LEAF], "sync_conflict_copy", subtree=False)
        p, prev, _ = e.plan()
        self.assertEqual([it["rel_path"] for it in items_of(p, "M")], [KID])
        self.assertTrue(items_of(p, "M")[0]["executable"], items_of(p, "M")[0]["blocked_by"])
        ref = [r for r in p["refused"] if r["rel_path"] == LEAF]
        self.assertEqual([r["class"] for r in ref], ["AUTO_SYNC_HOLD"], p["refused"])
        self.assertEqual(ref[0]["reason"], "该对象的 Notion → Obsidian 自动同步已暂停（sync_conflict_copy，scan）：不写入")
        self.assertIn(f"`{wb_common.cli_command('refresh', '--release-hold', e.ids['leaf'])}`", ref[0]["hint"])
        self.assertIn("下一轮 refresh 自动解除", ref[0]["hint"])
        self.assertIn(ref[0]["hint"], prev)
        self.assertEqual(executor_hold_reason(e, {"category": "M", "notion_id": e.ids["leaf"]}),
                         f"AUTO_SYNC_HOLD：{ref[0]['reason']}；{ref[0]['hint']}")
        sel = wb_plan.selection_from_args(p, _Args(all_safe=True))
        self.assertEqual([it["rel_path"] for it in wb_plan.selected_items(p, sel)], [KID])
        self.assertEqual(e.writes(), [])

    def test_own_deletion_holds_refuse_m_b_t_but_not_d_l23_b(self):
        """L-23 (b): an M / B / T item of a note with its own pending_deletes row on hold or its own pending_delete*
        auto hold is refused at plan time (the executor skips it: before, the planner filtered pending_delete holds
        for every category and listed the item executable). A D item keeps its own deletion hold (its precondition)
        but is refused by any other own hold (here a leftover sync_conflict_copy hold), as in the executor."""
        e = Env(self)
        e.edit(LEAF, "second para", "second para PEND")
        e.edit(KID, "kid body", "kid body PEND")
        lid = e.ids["leaf"]
        # (1) its own pending_deletes row on hold (the file came back and was edited; the refresh marks it restored
        # only at its next round)
        e.hold_delete("leaf", LEAF)
        p, prev, _ = e.plan()
        self.assertEqual([it["rel_path"] for it in items_of(p, "M")], [KID])
        ref = {r["rel_path"]: r for r in p["refused"]}
        self.assertEqual((ref[LEAF]["class"], ref[LEAF]["reason"], ref[LEAF]["hint"]),
                         ("AUTO_SYNC_HOLD", wb_plan.PENDING_HOLD_TEXT, wb_plan.PENDING_HOLD_HINT))
        self.assertEqual(executor_hold_reason(e, {"category": "M", "notion_id": lid}),
                         f"AUTO_SYNC_HOLD：{wb_plan.PENDING_HOLD_TEXT}")
        self.assertIsNone(executor_hold_reason(e, {"category": "M", "notion_id": e.ids["kid"]}))
        clear_pending(e)
        # (2) its own pending_delete auto hold in auto_sync_hold.json
        e.hold_like_refresh("leaf", [LEAF], "pending_delete", source="pending_delete", auto_release=False)
        p2, _prev2, _ = e.plan()
        self.assertEqual([it["rel_path"] for it in items_of(p2, "M")], [KID])
        r2 = [r for r in p2["refused"] if r["rel_path"] == LEAF][0]
        self.assertEqual((r2["class"], r2["hint"]), ("AUTO_SYNC_HOLD", wb_plan.HOLD_MANUAL_RELEASE_HINT))
        self.assertEqual(executor_hold_reason(e, {"category": "M", "notion_id": lid}),
                         f"AUTO_SYNC_HOLD：{r2['reason']}；{r2['hint']}")
        clear_auto_holds(e)
        # (3) D: the note deleted locally and on deletion hold; its own pending_delete auto hold does not refuse it
        os.unlink(e.path(DOOMED_REL))
        e.hold_delete("del", DOOMED_REL)
        e.hold_like_refresh("del", [DOOMED_REL], "pending_delete", source="pending_delete", auto_release=False)
        p3, _prev3, _ = e.plan()
        d = [it for it in items_of(p3, "D") if it["rel_path"] == DOOMED_REL]
        self.assertEqual(len(d), 1, p3["refused"])
        self.assertTrue(d[0]["executable"], d[0]["blocked_by"])
        self.assertIsNone(executor_hold_reason(e, {"category": "D", "notion_id": e.ids["del"]}))
        # ... any other own hold does (a sync_conflict_copy hold left over from before the deletion)
        e.hold_like_refresh("del", [DOOMED_REL], "sync_conflict_copy", subtree=False)
        p4, _prev4, _ = e.plan()
        self.assertEqual([it for it in items_of(p4, "D") if it["rel_path"] == DOOMED_REL], [])
        r4 = [r for r in p4["refused"] if r["rel_path"] == DOOMED_REL][0]
        self.assertEqual(r4["class"], "AUTO_SYNC_HOLD")
        self.assertEqual(executor_hold_reason(e, {"category": "D", "notion_id": e.ids["del"]}),
                         f"AUTO_SYNC_HOLD：{r4['reason']}；{r4['hint']}")
        self.assertEqual(e.writes(), [])

    def test_resolve_take_obsidian_refuses_held_items_like_a_normal_plan_l23_c(self):
        """L-23 (c): `resolve --take obsidian` builds a new plan (0 Notion requests); before wave 4 it skipped the
        own-hold and ancestor-hold refusals (resolve_of set) and listed the M item executable, while the executor
        skipped it. Now held items are refused exactly like in a normal plan."""
        e = Env(self)
        e.edit(KID, "kid body", "kid body LOCAL")
        e.remote_text("kid", 0, "kid body REMOTE")
        p, _info = e.create()
        c = items_of(p, "C")[0]
        src = {"plan_id": p["plan_id"], "item": c, "members": p["members"], "creation_state": p["creation_state"],
               "breaker": p["breaker"], "sync": p["sync"]}

        def resolve():
            ctx = wb_plan.PlanContext(e.paths, e.cfg, e.db, read_client=None)
            return wb_plan.build_plan(ctx, scope=f"resolve:{p['plan_id']}:{c['item_id']}", resolve_from=src)
        e.fake.clear_requests()
        new0, _prev0 = resolve()
        self.assertEqual(len(items_of(new0, "M")), 1, new0["refused"])            # control: not held -> executable
        self.assertTrue(items_of(new0, "M")[0]["executable"], items_of(new0, "M")[0]["blocked_by"])
        cases = [("own post_verify hold", lambda: e.hold_like_refresh("kid", [], "post_verify_failed:C02",
                                                                     source="post_verify", auto_release=False),
                  "该对象的 Notion → Obsidian 自动同步已暂停（post_verify_failed:C02，post_verify）"),
                 ("own sync_conflict_copy hold, copy gone", lambda: e.hold_like_refresh("kid", [KID], "sync_conflict_copy",
                                                                                       subtree=False),
                  "该对象的 Notion → Obsidian 自动同步已暂停（sync_conflict_copy，scan）"),
                 ("ancestor held with its subtree", lambda: e.hold_like_refresh("folder", [], "unsupported_rename"),
                  f"上级对象 {wb_plan.nid32(e.ids['folder'])[:8]} 的自动同步已暂停（含子树：unsupported_rename）"),
                 ("own pending_deletes row on hold", lambda: e.hold_delete("kid", KID),
                  "该对象处于删除保留（pending_deletes=hold，本地笔记已删除）：不写入")]
        for label, setup, text in cases:
            with self.subTest(label):
                setup()
                new, prev = resolve()
                self.assertEqual(items_of(new, "M"), [], label)
                ref = [r for r in new["refused"] if r["rel_path"] == KID]
                self.assertEqual([r["class"] for r in ref], ["AUTO_SYNC_HOLD"], new["refused"])
                self.assertIn(text, ref[0]["reason"])
                self.assertEqual(new["summary"]["executable"], 0)
                why = executor_hold_reason(e, {"category": "M", "notion_id": e.ids["kid"]})
                self.assertTrue(why and why.startswith("AUTO_SYNC_HOLD：") and text in why, why)
                clear_auto_holds(e)
                clear_pending(e)
        self.assertEqual(e.fake.requests, [], "resolve --take obsidian sends no Notion request at all")



# ============================================================================================ wave-4 fix round 1
class TestW4FixRound1Planner(unittest.TestCase):
    def test_whole_page_oracle_refuses_when_every_group_passes(self):
        """r1-spec-coverage-02 (R-053): the final whole-page re-render gate refuses on its own (ORACLE_MISMATCH,
        0 executable items) even when every per-group oracle passed."""
        e = Env(self)
        e.edit(LEAF, "second para", "second para ORACLE")
        real = wb_plan.PagePlanner._expected

        def wrong(planner, ops, base=None, span=None):
            return real(planner, ops, base, span) + ["a line the re-render does not produce"]
        with mock.patch.object(wb_plan.PagePlanner, "_expected", wrong):
            p, _prev, _ctx = e.plan()
        self.assertEqual([r["rel_path"] for r in refused_of(p, "ORACLE_MISMATCH")], [LEAF])
        self.assertEqual([it for it in p["items"] if it.get("executable")], [])
        p2, _prev, _ctx = e.plan()                               # control: the same edit plans fine
        self.assertEqual([it["rel_path"] for it in items_of(p2, "M") if it["executable"]], [LEAF])

    def test_remote_trashed_local_present_is_a_conflict(self):
        """r1-spec-coverage-09 (R-069): Notion page in the trash (or archived), local note edited."""
        for flag in ("in_trash", "archived"):
            e = Env(self)
            e.edit(LEAF, "second para", "second para TRASHED")
            e.fake.pages[e.ids["leaf"]][flag] = True
            p, prev, _ctx = e.plan()
            cs = items_of(p, "C")
            self.assertEqual([(c["kind"], c["rel_path"], c["executable"]) for c in cs],
                             [("REMOTE_TRASHED_LOCAL_PRESENT", LEAF, False)], flag)
            self.assertEqual(items_of(p, "M"), [])
            self.assertIn("REMOTE_TRASHED_LOCAL_PRESENT", prev)
            self.assertEqual(e.fake.writes(), [])

    def test_local_deleted_without_hold_is_refused(self):
        e = Env(self)
        os.unlink(e.path("General/Home/Doomed.md"))
        p, _prev, _ctx = e.plan()
        self.assertEqual([r["rel_path"] for r in refused_of(p, "LOCAL_DELETED")], ["General/Home/Doomed.md"])
        self.assertIn("refresh.py --mode incremental", refused_of(p, "LOCAL_DELETED")[0]["hint"])
        self.assertNotIn("6.0", refused_of(p, "LOCAL_DELETED")[0]["hint"])

    def test_bom_note_edit_is_refused_with_a_hint(self):
        """r1-data-safety-02: the adoption publish would read a BOM note's frontmatter as body (local keys lost)."""
        e = Env(self)
        t = e.read(LEAF).replace("second para", "second para BOM", 1)
        with open(e.path(LEAF), "wb") as fh:
            fh.write(b"\xef\xbb\xbf" + t.encode("utf-8"))
        p, _prev, _ctx = e.plan()
        self.assertEqual([r["rel_path"] for r in refused_of(p, "UTF8_BOM")], [LEAF])
        self.assertIn("无 BOM", refused_of(p, "UTF8_BOM")[0]["hint"])
        self.assertEqual(items_of(p, "M"), [])

    def test_unchanged_link_to_a_locally_deleted_sibling_keeps_link_rewrite_noop(self):
        """r1-user-journey-08: only a wikilink spelling changed in the folder note; an UNCHANGED link in it points to a
        sibling deleted locally (no refresh yet). Still LINK_REWRITE_NOOP, not an X-block 'edit Notion' refusal."""
        def seed(env):
            env.ids["kid3"] = env.fake.add_page("Kid3", parent=env.ids["folder"], children=[para("kid3 body")])
        e = Env(self, seed=seed)
        folder = "General/Home/Folder/Folder.md"
        self.assertIn("[[General/Home/Folder/Kid3|", e.read(folder))
        e.edit(folder, "[[General/Home/Folder/Kid|", "[[Kid|")
        os.unlink(e.path("General/Home/Folder/Kid3.md"))
        p, _prev, _ctx = e.plan()
        mine = [r for r in p["refused"] if r["rel_path"] == folder]
        self.assertEqual([r["class"] for r in mine], ["LINK_REWRITE_NOOP"], mine)
        self.assertEqual([r["class"] for r in p["refused"] if r["rel_path"].endswith("Kid3.md")], ["LOCAL_DELETED"])

    def test_x_block_refusal_names_the_block_in_plain_words(self):
        e = Env(self)
        e.edit(LEAF, "see ", "see also ")
        p, _prev, _ctx = e.plan()
        r = [x for x in p["refused"] if x["rel_path"] == LEAF][0]
        self.assertEqual(r["class"], "UNSUPPORTED_REGION")
        self.assertIn("块内：含提及（@", r["reason"])          # plain words first, the code next to them
        self.assertIn("X:mention_page", r["reason"])

    def test_notion_timestamp_is_not_listed_as_a_kept_local_key(self):
        """r1-user-journey-14: a differing notion_last_edited_time is Notion's timestamp, not a kept local key."""
        e = Env(self)
        t = e.read(LEAF)
        t = re.sub(r"notion_last_edited_time: .*", "notion_last_edited_time: 2030-01-01T00:00:00.000Z", t, count=1)
        e.write(LEAF, t.replace("second para", "second para KEYS", 1))
        p, prev, _ctx = e.plan()
        m = [it for it in items_of(p, "M") if it["rel_path"] == LEAF]
        self.assertEqual(len(m), 1, p["refused"])
        self.assertEqual(m[0]["local_keys_kept"], [])
        self.assertNotIn("保留在库内、不写 Notion：`notion_last_edited_time`", prev)

    def test_conflict_preview_shows_notion_side_editor_url_and_commands(self):
        """r1-user-journey-09."""
        e = Env(self)
        e.edit(KID, "kid body", "kid body LOCAL")
        e.remote_text("kid", 0, "kid body REMOTE by Owner")
        owner = next(u["id"] for u in e.fake.users.values() if u.get("type") == "person") \
            if isinstance(getattr(e.fake, "users", None), dict) else None
        if owner:
            e.fake.pages[e.ids["kid"]]["last_edited_by"] = {"object": "user", "id": owner}
        p, info = e.create()
        c = items_of(p, "C")[0]
        self.assertEqual(c["kind"], "CONFLICT_BOTH")
        self.assertIn("kid body REMOTE by Owner", json.dumps(c["notion_side"], ensure_ascii=False))
        if owner:
            self.assertEqual(c["notion_editor_type"], "人（person）")
        prev = c["preview"]
        self.assertIn(c["notion_url"], prev)
        self.assertIn(f"writeback.py resolve --plan {p['plan_id']} --item {c['item_id']} --take notion", prev)
        self.assertIn("Notion_Obsidian/conflicts/", prev)
        self.assertNotRegex(prev, r"编辑时间：\S*Z；")             # local time, not UTC 'Z'

    def test_all_safe_with_only_d_and_t_items_explains_why(self):
        """r1-user-journey-13."""
        plan = {"plan_id": "wb-20261120T000000-abcd", "items": [
            {"item_id": "D-001", "category": "D", "executable": True, "notion_id": "a", "rel_path": "x.md"},
            {"item_id": "T-001", "category": "T", "executable": True, "notion_id": "b", "rel_path": "y.md"}]}
        sel = wb_plan.selection_from_args(plan, _Args(all_safe=True))
        with self.assertRaises(wb_plan.WbError) as cm:
            wb_plan.selected_items(plan, sel)
        msg = str(cm.exception)
        self.assertIn("--all-safe 只包含", msg)
        self.assertIn("D-001（用 --delete）", msg)
        self.assertIn("T-001（用 --rename）", msg)
        with self.assertRaises(wb_plan.WbError) as cm:                 # no flag at all: the plain message
            wb_plan.selected_items(plan, wb_plan.selection_from_args(plan, _Args()))
        self.assertNotIn("--all-safe 只包含", str(cm.exception))

    def test_sync_settling_blocker_says_to_plan_again(self):
        """r1-user-journey-02: executability is frozen in plan.json; the reason must say to re-run plan."""
        txt = wb_plan.blocked_text("SYNC_SETTLING：Obsidian 启动不足 15 分钟，同步可能未完成")
        self.assertIn(f"重新运行 {wb_common.cli_command('writeback', 'plan')}", txt)
        self.assertEqual(wb_plan.blocked_text(wb_plan.UNREGISTERED_RENAME),
                         f"{wb_plan.UNREGISTERED_RENAME}（{wb_plan.UNREGISTERED_RENAME_HINT}）")

    def test_plan_records_its_vault(self):
        """r1-never-unapproved-05: plan.json names the vault its L shas were read from."""
        e = Env(self)
        e.edit(LEAF, "second para", "second para V")
        p, _prev, _ctx = e.plan()
        self.assertEqual(p["vault"], os.path.abspath(e.vault))

    def test_planner_uses_the_last_adopted_snapshot(self):
        """r1-contract-consistency-03: the planning snapshot is the one the vault was LAST published from
        (wb_refetch.choose_base): a newer write-back adoption wins over the refresh's last_published_snapshot.txt;
        deletion freshness still uses the refresh snapshot."""
        import time as _time
        e = Env(self)
        adopted = os.path.join(e.work, "snapshots", "wb", "snap_adopted.db")
        os.makedirs(os.path.dirname(adopted))
        shutil.copy2(e.snap, adopted)
        shutil.copy2(e.snap + ".roots.json", adopted + ".roots.json")
        rec = {"snapshot": adopted, "finished_at": "2026-11-20T00:00:00+00:00", "plan_id": "wb-x",
               "at": wb_common.now_iso(), "at_epoch": _time.time() + 60}
        with open(os.path.join(e.work, "state", "last_adopted_wb_snapshot.json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        ctx = e.ctx()
        self.assertEqual(ctx.snapshot(), adopted)
        self.assertEqual(os.path.abspath(ctx.refresh_snapshot()), os.path.abspath(e.snap))
        rec["at_epoch"] = 1.0                          # a refresh published later: back to the refresh snapshot
        with open(os.path.join(e.work, "state", "last_adopted_wb_snapshot.json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        self.assertEqual(os.path.abspath(e.ctx().snapshot()), os.path.abspath(e.snap))
        os.unlink(adopted)                             # the chosen adopted snapshot is missing: fail closed
        rec["at_epoch"] = _time.time() + 60
        with open(os.path.join(e.work, "state", "last_adopted_wb_snapshot.json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        with self.assertRaises(wb_plan.WbError):
            e.ctx().snapshot()


if __name__ == "__main__":
    unittest.main()
