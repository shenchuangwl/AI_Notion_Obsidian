#!/usr/bin/env python3
"""W3: end-to-end approval rehearsal of the write-back (Obsidian -> Notion) on the FAKE Notion.

What runs for real (nothing is stubbed):
* tools/tests/fake_notion.py on 127.0.0.1 with fake tokens (never a real token, never the real API);
* snapshot crawl (snapshot.py code through a ReadOnlyClient) -> REAL refresh rounds (refresh.run_round with
  --use-snapshot: trash_archive, wb_scan, export_obsidian.py, verify_vault.py staging + post verify, publish_vault.py
  plan + apply) into a temp WORK (a copy of tools/*.py under WORK/tools, as refresh runs its tools from there) and a
  temp vault;
* the REAL CLI tools/writeback.py: read-only commands in-process (wb_plan.obsidian_status pinned to "running"
  because no Obsidian runs for the temp vault) and in subprocesses; every gated command (reject / unreject /
  discard / restore-deleted / resolve / unblock / rebase / ack-* / hold / reconcile / audit-scope / apply /
  apply --resume / rollback) in a subprocess on a pseudo-terminal (pexpect). The child environment is scrubbed of
  CLAUDECODE / AI_AGENT / CI / GITHUB_ACTIONS / CLAUDE_CODE_* ONLY for that child (allowed by the wave-3 brief:
  tests against the fake server with fake tokens); the gate itself is unchanged and is also shown to REFUSE with
  those variables, without a TTY and with piped stdin. The challenge phrase is read from the terminal output and
  typed back, exactly as a person does;
* the executor's adoption pipeline (wb_refetch -> export -> verify -> publish --adopt-writeback -> verify --ids).

Write-back token: a temp file (0600 in a 0700 dir); ~/.config is never read. Every WORK / vault / state lives in
tempfile.mkdtemp() dirs that are removed afterwards; the sandbox state/ is never touched.

Steps (W3 brief): 1 seed + N->O publish; 2 local edits, status / plan / show; 3 reject + apply a subset through
the pty challenge (exact write log, W1-W10, adoption, re-plan); 4 unapproved-never-written invariant (R-223) for
every CLI command + fault injection (409 / 503 after commit / connection lost after commit / kill -9 between
journal steps + resume / rollback with a fault); 5 rollback drill; 6 deletion + rename policy.

Run (from the sandbox root):
  python3 -I -B -m unittest discover -s tools/tests -p 'test_e2e_writeback.py' -v
"""
import argparse
import contextlib
import copy
import io
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
for _p in (HERE, TOOLS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import refresh  # noqa: E402
import snapshot as snapmod  # noqa: E402
import verify_vault as vv  # noqa: E402
import wb_common  # noqa: E402
import wb_plan  # noqa: E402
import wb_state  # noqa: E402
import writeback  # noqa: E402
from fake_notion import FakeNotion  # noqa: E402
from rate_gate import RateGate  # noqa: E402
from readonly_client import make_read_client  # noqa: E402

try:
    import pexpect  # noqa: E402
except ImportError:  # pragma: no cover
    pexpect = None

WB = os.path.join(TOOLS, "writeback.py")
AGENT_VARS = ("CLAUDECODE", "AI_AGENT", "CI", "GITHUB_ACTIONS")
AGENT_PREFIX = "CLAUDE_CODE_"
OBSIDIAN_RUNNING = {"running": True, "started_at": "2026-01-01T00:00:00+07:00"}
QUIET = ("nmig.client", "nmig.inventory", "snapshot", "writeback_client", "wb_apply", "wb_refetch")
_saved_levels = {}

MAIN = "General/写回测试页/写回测试页.md"
CHILD = "General/写回测试页/子页面.md"
CHILD_NEW = "General/写回测试页/子页面改名.md"
DOOMED = "General/写回测试页/待删页面.md"
NEW_NOTE = "General/写回测试页/新笔记.md"


def setUpModule():
    for n in QUIET:
        lg = logging.getLogger(n)
        _saved_levels[n] = lg.level
        lg.setLevel(logging.CRITICAL)
    # the fake server's threads live in this process; pexpect's forkpty child execs Python immediately
    warnings.filterwarnings("ignore", r"This process .* is multi-threaded, use of forkpty", DeprecationWarning)


def tearDownModule():
    for n, lv in _saved_levels.items():
        logging.getLogger(n).setLevel(lv)


def clean_env(**extra):
    """The CHILD's environment: the agent / CI markers removed (only for that child), everything else kept."""
    env = {k: v for k, v in os.environ.items() if k not in AGENT_VARS and not k.startswith(AGENT_PREFIX)}
    env.update(extra)
    return env


# ============================================================================================ fake content
def rt(t, **ann):
    r = {"type": "text", "text": {"content": t}}
    if ann:
        r["annotations"] = ann
    return r


def blk(typ, *rich, **extra):
    d = {"rich_text": list(rich)}
    d.update(extra)
    return {"type": typ, typ: d}


def plain(b):
    return "".join(x.get("plain_text", "") for x in (b.get(b["type"]) or {}).get("rich_text") or [])


TEST_PAGE_BLOCKS = [
    blk("heading_1", rt("测试标题")),
    blk("paragraph", rt("第一段：普通文字。")),
    blk("paragraph", rt("第二段：将被修改。")),
    blk("heading_2", rt("列表")),
    blk("bulleted_list_item", rt("项目甲")),
    blk("bulleted_list_item", rt("项目乙")),
    blk("numbered_list_item", rt("第一步")),
    blk("numbered_list_item", rt("第二步")),
    blk("to_do", rt("待办事项"), checked=False),
    blk("paragraph", rt("将被删除的一行")),
    blk("code", rt("print('hello')"), language="python"),
    blk("quote", rt("引用文字")),
    blk("callout", rt("提示内容"), icon={"type": "emoji", "emoji": "💡"}),
    blk("toggle", rt("折叠标题"), children=[blk("paragraph", rt("折叠内容"))]),
    blk("paragraph", rt("红色"), rt("文字", color="red")),          # X: coloured segment
    blk("paragraph", rt("最后一段。")),
]


# ============================================================================================ the world
class World:
    """Fake Notion + temp WORK (tools copied to WORK/tools) + temp vault, initial N->O by a REAL refresh round.

    Pages: 写回测试页 (top level, General; every block type of the brief + a mention paragraph) with the child pages
    子页面 and 待删页面. All three are whitelisted (mode full: the test page is a folder note, so stage-2 leaf rules
    would make it read-only; mode full is the mode in which a folder note is writable)."""

    def __init__(self, tc, name="w"):
        self.tmp = tempfile.mkdtemp(prefix=f"wbe2e-{name}-")
        tc.addCleanup(self.close)
        self.fake = None
        self._children = []
        self.work = os.path.join(self.tmp, "work")
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(os.path.join(self.work, "tools"))
        for f in os.listdir(TOOLS):                   # refresh runs WORK/tools/*.py (WORK = their parent)
            if f.endswith(".py"):
                shutil.copy2(os.path.join(TOOLS, f), os.path.join(self.work, "tools", f))
        self.P = refresh.Paths(self.work, self.vault)
        for d in (self.P.state, self.P.reports, self.P.staging, self.P.snapshots, self.P.logs, self.P.evidence,
                  self.P.approvals, os.path.dirname(self.P.config), os.path.join(self.work, "backup"),
                  os.path.join(self.work, "blobs", "tmp")):
            os.makedirs(d, exist_ok=True)
        os.makedirs(self.vault)
        for ts in vv.SPEC_TEAMSPACES:                 # the real vault has every teamspace folder (C01)
            os.makedirs(os.path.join(self.vault, ts))
        with open(os.path.join(self.P.approvals, refresh.STANDING_APPROVAL), "w", encoding="utf-8") as fh:
            fh.write("# standing approval (e2e test)\n")
        self.blobs = os.path.join(self.work, "blobs")
        self.fake = FakeNotion(tokens={
            "ro": {"caps": {"read", "user_info"}, "roots": {"*"}, "secret": "tok-ro-e2e-w3"},
            "wb": {"caps": {"read", "update", "insert"}, "roots": set(), "secret": "tok-wb-e2e-w3"}})
        self.base = self.fake.start()
        self.fake.add_user("Owner")
        self.page = self.fake.add_page("写回测试页", children=copy.deepcopy(TEST_PAGE_BLOCKS))
        self.child = self.fake.add_page("子页面", parent=self.page, children=[blk("paragraph", rt("子页面正文"))])
        self.doomed = self.fake.add_page("待删页面", parent=self.page, children=[blk("paragraph", rt("待删页面正文"))])
        self.fake.add_blocks(self.page, [{"type": "paragraph", "paragraph": {"rich_text": [
            rt("参见 "), {"type": "mention", "mention": {"type": "page", "page": {"id": self.child}}}]}}])  # X
        self.fake.grant("wb", self.page)              # page-level sharing: the subtree is reachable
        self.gate = RateGate(os.path.join(self.tmp, "gate.json"), total_rps=1000, burst=1000)
        self.rc = make_read_client(base_url=self.base, token=self.fake.secret("ro"), rate=None, gate=self.gate,
                                   sleep=lambda s: None)
        self.roots_in = [{"id": self.page, "title": "写回测试页", "teamspace": "General", "teamspace_order": 1,
                          "root_order": 1}]
        self.sidebar = os.path.join(self.tmp, "sidebar.json")
        with open(self.sidebar, "w", encoding="utf-8") as fh:
            json.dump({"teamspace_order_required": list(vv.SPEC_TEAMSPACES), "roots": self.roots_in,
                       "order_match": True}, fh)
        self.cfg = {"mode": "full", "read_integration_readonly_confirmed_at": "2026-11-17T18:30+07:00",
                    "whitelist_page_ids": [self.page, self.child, self.doomed], "allow_block_trash": True,
                    "rate_limit_total_rps": 3.0,
                    # 3 pages: one changed page is 33 % (>= the production 10 %) -> every N->O round after a
                    # write-back would pause as "source_mass_change"; the rehearsal raises the ratio to 1.0
                    "source_change_pause_ratio": 1.0}
        self.write_cfg()
        # tokens: read token file (plain), write-back token file 0600 in a 0700 dir (never ~/.config)
        self.read_token_file = os.path.join(self.tmp, "ro.env")
        with open(self.read_token_file, "w", encoding="utf-8") as fh:
            fh.write("NOTION_TARGET_TOKEN=" + self.fake.secret("ro") + "\n")
        tokdir = os.path.join(self.tmp, "tok")
        os.makedirs(tokdir, mode=0o700)
        os.chmod(tokdir, 0o700)
        self.token_file = os.path.join(tokdir, "writeback.env")
        with open(self.token_file, "w", encoding="utf-8") as fh:
            fh.write("NOTION_WRITEBACK_TOKEN=" + self.fake.secret("wb") + "\n")
        os.chmod(self.token_file, 0o600)
        self.argv = ["--work", self.work, "--vault", self.vault, "--base-url", self.base,
                     "--read-token-file", self.read_token_file, "--token-file", self.token_file]
        self._snap_n = 0
        self.snap = self.crawl()
        rc, st = self.refresh("R1")
        assert st.get("result") == "published", st
        self.seed_texts = self.tree(self.page)

    # ---------------------------------------------------------------- config / files
    def write_cfg(self, **kw):
        self.cfg.update(kw)
        with open(self.P.config, "w", encoding="utf-8") as fh:
            json.dump(self.cfg, fh, ensure_ascii=False)

    def path(self, rel):
        return os.path.join(self.vault, *rel.split("/"))

    def read(self, rel):
        with open(self.path(rel), encoding="utf-8") as fh:
            return fh.read()

    def write(self, rel, text):
        os.makedirs(os.path.dirname(self.path(rel)), exist_ok=True)
        with open(self.path(rel), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def edit(self, rel, old, new):
        t = self.read(rel)
        assert old in t, (rel, old, t)
        self.write(rel, t.replace(old, new, 1))

    def sha(self, rel):
        return wb_common.sha256_file(self.path(rel))

    # ---------------------------------------------------------------- Notion side (fake)
    def crawl(self):
        """A finished snapshot of the fake (snapshot.py code through the read client), as a refresh would take."""
        self._snap_n += 1
        path = os.path.join(self.P.snapshots, f"snap_20261118T{self._snap_n:04d}.db")
        saved = snapmod.BLOBS
        snapmod.BLOBS = self.blobs
        try:
            roots = snapmod.detect_kinds(self.rc, self.roots_in)
            with open(path + ".roots.json", "w", encoding="utf-8") as fh:
                json.dump(roots, fh, ensure_ascii=False, indent=1)
            store = snapmod.Store(path)
            me = self.rc.me()
            store.x("INSERT OR REPLACE INTO meta VALUES('workspace_id', ?)", (me["bot"]["workspace_id"],))
            store.x("INSERT OR REPLACE INTO meta VALUES('notion_version', ?)", ("2026-03-11",))
            inv = snapmod.BlobInventory(self.rc, store, workers=1, hash_files=True, comments=False)
            inv.seed([r for r in roots if not r.get("in_trash")])
            inv.run()
            inv.http.close()
            store.x("INSERT OR REPLACE INTO meta VALUES('finished_at', ?)", (wb_common.now_iso(),))
            store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            store.db.close()
        finally:
            snapmod.BLOBS = saved
        for s in ("-wal", "-shm"):
            if os.path.exists(path + s) and os.path.getsize(path + s) == 0:
                os.unlink(path + s)
        return path

    def refresh(self, run_id, snap=None):
        """One REAL refresh round (refresh.run_round, real tools from WORK/tools) on a fresh crawl of the fake."""
        snap = snap or self.crawl()
        opts = argparse.Namespace(mode="incremental", dry_run=False, use_snapshot=snap, sidebar=self.sidebar,
                                  run_id=run_id)
        with contextlib.redirect_stdout(io.StringIO()):
            rc, st = refresh.run_round(self.P, opts, client_factory=lambda P: self.rc)
        return rc, st

    def tree(self, pid):
        """[(type, plain text, [children...])] of the live (fake) page, trashed blocks excluded."""
        out = []
        for b in self.fake.children(pid):
            kids = self.tree(b["id"]) if b.get("has_children") and b["type"] not in ("child_page",
                                                                                      "child_database") else []
            out.append((b["type"], plain(b) if b["type"] != "child_page" else b["child_page"]["title"], kids))
        return out

    def title(self, pid):
        p = self.fake.page(pid)
        return "".join(x.get("plain_text", "") for x in p["properties"]["title"]["title"])

    def writes(self):
        """Every Notion WRITE request the fake received (search / query POSTs are reads)."""
        return self.fake.writes()

    # ---------------------------------------------------------------- state
    def db(self):
        return wb_state.connect(self.P.wb_db)

    def q(self, sql, args=()):
        con = sqlite3.connect(f"file:{self.P.wb_db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, args)]
        finally:
            con.close()

    def plan_status(self, pid):
        return self.q("SELECT status FROM plans WHERE plan_id=?", (pid,))[0]["status"]

    def decision(self, pid, item_id):
        return self.q("SELECT decision FROM plan_items WHERE plan_id=? AND item_id=?", (pid, item_id))[0]["decision"]

    def plan_obj(self, pid):
        db = self.db()
        try:
            return wb_plan.load_plan(pid, work=wb_plan.Paths(self.work, self.vault), db=db)
        finally:
            db.close()

    def published(self, rel):
        con = sqlite3.connect(f"file:{self.P.export_db}?mode=ro", uri=True)
        try:
            return con.execute("SELECT sha256, notion_id, status FROM published_files WHERE rel_path=?",
                               (rel,)).fetchone()
        finally:
            con.close()

    # ---------------------------------------------------------------- CLI
    def main(self, *args):
        """writeback.main in-process (read-only commands; Obsidian pinned to running/settled)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(wb_plan, "obsidian_status", return_value=OBSIDIAN_RUNNING), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = writeback.main(self.argv + list(args))
        return rc, out.getvalue(), err.getvalue()

    def make_plan(self, *args):
        rc, out, err = self.main("plan", *args)
        if rc != 0:
            raise AssertionError(out + err)
        pid = re.search(r"plan_id：(\S+)", out).group(1)
        sha12 = re.search(r"前 12 位）：([0-9a-f]{12})", out).group(1)
        return pid, sha12, out

    def run(self, *args, env=None, stdin=subprocess.DEVNULL):
        """writeback.py in a subprocess WITHOUT a terminal (pipes)."""
        return subprocess.run([sys.executable, "-I", "-B", WB] + self.argv + list(args), capture_output=True,
                              text=True, env=env or clean_env(), stdin=stdin, timeout=300)

    def spawn(self, *args, env=None, timeout=300):
        child = pexpect.spawn(sys.executable, ["-I", "-B", WB] + self.argv + list(args), env=env or clean_env(),
                              encoding="utf-8", timeout=timeout, dimensions=(60, 300))
        self._children.append(child)
        return child

    def yes_no(self, *args, answer="y"):
        """A decision command on the pty: answer its y/N question. Returns (exit status, transcript)."""
        child = self.spawn(*args)
        idx = child.expect([r"\[y/N\]", pexpect.EOF])
        if idx == 0:
            child.sendline(answer)
            child.expect(pexpect.EOF)
        child.close()
        return child.exitstatus, child.before

    def challenge(self, child, mutate=None):
        """Read the exact list + challenge phrase from the terminal and type it back (optionally altered)."""
        child.expect("请原样输入下面这一行以确认")
        shown = child.before
        child.expect(r"\n> ")
        tail = child.before
        shown += tail
        m = re.search(r"\n  (确认 [^\r\n]*?[0-9A-F]{6})\s*$", tail)
        if m is None:
            raise AssertionError("no challenge phrase in: " + shown)
        phrase = m.group(1)
        child.sendline(mutate(phrase) if mutate else phrase)
        return phrase, shown

    def gated(self, *args, mutate=None, timeout=300):
        """apply / rollback / resume / resolve --take obsidian on the pty with the challenge typed back.
        Returns (exit status, transcript, phrase, shown list)."""
        child = self.spawn(*args, timeout=timeout)
        phrase, shown = self.challenge(child, mutate)
        child.expect(pexpect.EOF, timeout=timeout)
        child.close()
        return child.exitstatus, child.before, phrase, shown

    def apply_args(self, pid, sha12, *sel):
        return ["apply", "--plan", pid, "--sha", sha12, *sel, "--blobs", self.blobs, "--sidebar", self.sidebar]

    def result(self, pid, name="result.json"):
        with open(os.path.join(self.work, "reports", "writeback", pid, name), encoding="utf-8") as fh:
            return json.load(fh)

    def close(self):
        for c in self._children:
            try:
                if c.isalive():
                    c.terminate(force=True)
            except Exception:  # noqa: BLE001
                pass
        if self.fake is not None:
            self.fake.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class _ClassCleanup:
    """World(tc) registers its cleanup on a TestCase; a class-level world registers it on the class."""

    def __init__(self, cls):
        self.addCleanup = cls.addClassCleanup


def _items(plan, cat=None):
    return [it for it in plan["items"] if cat is None or it["category"] == cat]


def _op_requests(item):
    """(method, path) of every request the executor may send for one plan item (WB §11.3 op -> endpoint)."""
    out = set()
    for o in item.get("ops") or []:
        op = o.get("op")
        if op == "UPDATE":
            out.add(("PATCH", f"blocks/{o['target_id']}"))
        elif op == "APPEND":
            out.add(("PATCH", f"blocks/{o['parent_id']}/children"))
        elif op == "TRASH_BLOCK":
            out.add(("DELETE", f"blocks/{o['target_id']}"))
        elif op in ("UPDATE_PROPERTIES", "RENAME", "TRASH_PAGE"):
            out.add(("PATCH", f"pages/{o.get('page_id') or o.get('target_id') or item['notion_id']}"))
        elif op == "CREATE_PAGE":
            out.add(("POST", "pages"))
    return out


# ============================================================================================ steps 1, 2, 3, 5, 6
@unittest.skipIf(pexpect is None, "pexpect not installed")
class TestApprovalRehearsal(unittest.TestCase):
    """One world, the steps in order (each step needs the previous one; a failed step skips the rest)."""

    S = {}

    @classmethod
    def setUpClass(cls):
        cls.w = World(_ClassCleanup(cls), "main")
        cls.S = {"done": set()}

    def need(self, *steps):
        missing = [s for s in steps if s not in self.S["done"]]
        if missing:
            self.skipTest(f"previous step(s) {missing} did not pass")

    # ------------------------------------------------------------------------------------------ step 1
    def test_s1_seed_and_n2o_publish(self):
        """Seeded test page (every block type of the brief, a mention and a coloured segment) + child pages,
        published N->O by a real refresh round; the first `plan` on the clean vault enables write-back."""
        w = self.w
        text = w.read(MAIN)
        for s in ("# 测试标题", "第二段：将被修改。", "## 列表", "- 项目甲\n- 项目乙", "1. 第一步\n2. 第二步",
                  "- [ ] 待办事项", "```python\nprint('hello')\n```", "> 引用文字", "> [!note] 💡\n> 提示内容",
                  "> [!toggle]- 折叠标题", "红色文字", f"[[{CHILD[:-3]}|子页面]]", f"参见 [[{CHILD[:-3]}|子页面]]"):
            self.assertIn(s, text)
        for rel, nid in ((MAIN, w.page), (CHILD, w.child), (DOOMED, w.doomed)):
            self.assertEqual(w.published(rel)[1:], (nid, "active"), rel)
            b = w.q("SELECT origin, sha256 FROM base_history WHERE notion_id=? ORDER BY seq DESC LIMIT 1", (nid,))
            self.assertEqual(b[0]["sha256"], w.sha(rel), rel)
        self.assertTrue(w.q("SELECT count(*) n FROM block_lines WHERE notion_id=?", (w.page,))[0]["n"] >= 17)
        rc, out, err = w.main("plan")                  # enabling: the first plan sees a clean vault
        self.assertEqual(rc, 0, out + err)
        self.assertIn("可执行 0", out)
        self.assertEqual(w.q("SELECT count(*) n FROM plans")[0]["n"], 1)
        self.assertEqual(w.writes(), [])
        self.S["done"].add(1)

    # ------------------------------------------------------------------------------------------ step 2
    def test_s2_edits_status_plan_show(self):
        self.need(1)
        w = self.w
        # (a) rename the child note in Obsidian (links updated by Obsidian) and delete another note; then the
        #     6-hourly refresh runs (it registers the rename as 命名差异 and holds the local delete, mode >= plan)
        os.rename(w.path(CHILD), w.path(CHILD_NEW))
        w.write(MAIN, w.read(MAIN).replace(f"[[{CHILD[:-3]}|", f"[[{CHILD_NEW[:-3]}|"))
        os.unlink(w.path(DOOMED))
        rc, st = w.refresh("R2")
        self.assertIn(st.get("result"), ("published", "published_with_holds"), st)
        ov = w.q("SELECT old_rel, local_rel, status FROM name_overrides WHERE notion_id=?", (w.child,))
        self.assertEqual([(o["old_rel"], o["local_rel"], o["status"]) for o in ov], [(CHILD, CHILD_NEW, "open")])
        self.assertEqual(w.title(w.child), "子页面", "a local rename never changes the Notion title by itself")
        self.assertFalse(os.path.exists(w.path(DOOMED)), "a held local delete is not restored")
        self.assertEqual([h["status"] for h in w.q("SELECT status FROM pending_deletes WHERE notion_id=?",
                                                   (w.doomed,))], ["hold"])
        self.assertFalse(w.fake.page(w.doomed)["in_trash"], "a local delete never trashes by itself")
        # (b) body edits of the test page, an edit inside two X blocks, a new note in the folder-note folder
        w.edit(MAIN, "第二段：将被修改。", "第二段：已在 Obsidian 中修改。")
        w.edit(MAIN, "- 项目乙", "- 项目乙（改）")
        w.edit(MAIN, "将被删除的一行\n\n", "")
        w.write(MAIN, w.read(MAIN).rstrip("\n") + "\n\n新增段落一。\n\n新增段落二。\n")
        with_x = w.read(MAIN)
        w.edit(MAIN, "红色文字", "红色文字（改）")                       # X: coloured segment
        w.edit(MAIN, "参见 [[", "另见 [[")                               # X: mention
        w.write(NEW_NOTE, "新笔记正文。\n")
        rc, out, err = w.main("status")
        self.assertEqual(rc, 0, err)
        for s in ("待确认的本地改动", MAIN, NEW_NOTE, f"{CHILD} → {CHILD_NEW}", f"删除 hold：{DOOMED}",
                  "-- 已拒绝 rejected --"):
            self.assertIn(s, out)
        pid_a, _sha_a, out_a = w.make_plan()
        plan_a, _ = w.plan_obj(pid_a)
        self.assertEqual(sorted(it["category"] for it in plan_a["items"]), ["D", "N", "T"], out_a)
        ref = [r for r in plan_a["refused"] if r["rel_path"] == MAIN]
        self.assertEqual(len(ref), 1, plan_a["refused"])
        self.assertEqual(ref[0]["class"], "UNSUPPORTED_REGION")
        self.assertRegex(ref[0]["reason"], r"X:(color|mention)")
        self.assertTrue(ref[0]["hint"])
        self.assertIn("只读", ref[0]["hint"] + ref[0]["reason"])
        prev_a = w.read(f"Notion_Obsidian/写回预览/{pid_a}.md")
        self.assertIn("拒绝写回 / 只报告", prev_a)
        self.assertIn(MAIN, prev_a)
        # (c) the user follows the hint: X blocks are edited in Notion only -> local X edits reverted, re-plan
        w.write(MAIN, with_x)
        pid, sha12, out = w.make_plan()
        plan, sha = w.plan_obj(pid)
        got = {it["item_id"]: (it["category"], it.get("rel_path"), [o["op"] for o in it["ops"]], it.get("executable"))
               for it in plan["items"]}
        self.assertEqual(got, {"N-001": ("N", NEW_NOTE, ["CREATE_PAGE"], True),
                               "M-001": ("M", MAIN, ["UPDATE", "UPDATE", "APPEND"], True),
                               "B-001": ("B", MAIN, ["TRASH_BLOCK"], True),
                               "D-001": ("D", DOOMED, ["TRASH_PAGE"], True),
                               "T-001": ("T", CHILD_NEW, ["RENAME"], True)}, out)
        self.assertEqual(_items(plan, "M")[0]["binds"], ["B-001"])
        self.assertEqual(plan["refused"], [])
        self.assertEqual(w.plan_status(pid_a), "superseded")
        rc, show, err = w.main("show", "--plan", pid, "--item", "M-001")
        self.assertEqual(rc, 0, err)
        for s in ("@@ ", "-第二段：将被修改。", "+第二段：已在 Obsidian 中修改。", "+- 项目乙（改）", "-将被删除的一行",
                  "+新增段落一。", '"op": "UPDATE"', '"op": "APPEND"', "绑定块删除：B-001"):
            self.assertIn(s, show)
        r = w.run("status", "--no-scan")               # read-only commands need no terminal
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(w.writes(), [], "status / plan / show send no write")
        self.S.update(pid=pid, sha12=sha12, sha=sha, plan=plan)
        self.S["done"].add(2)

    # ------------------------------------------------------------------------------------------ step 3
    def test_s3_reject_and_apply_a_subset(self):
        self.need(2)
        w, pid, sha12, plan = self.w, self.S["pid"], self.S["sha12"], self.S["plan"]
        rc, out = w.yes_no("reject", "--plan", pid, "--items", "T-001", "--reason", "e2e: keep the Notion title")
        self.assertEqual(rc, 0, out)
        self.assertEqual(w.decision(pid, "T-001"), "rejected")
        pre_tree, pre_pages = w.tree(w.page), len(w.fake.pages)
        w.fake.clear_requests()
        rc, out, phrase, shown = w.gated(*w.apply_args(pid, sha12, "--items", "M-001", "--delete-blocks", "B-001"))
        self.assertRegex(phrase, r"^确认 \S{4} 修改1 新增0 删除0 删块1\[B-001\] 改名0 [0-9A-F]{6}$")
        listed = re.findall(r"^\s+([A-Z]+-\d{3})  ", shown, re.M)
        self.assertEqual(listed, ["M-001", "B-001"], shown)
        self.assertEqual(rc, 0, out)
        self.assertIn("完成，可验收（applied）", out)
        # the fake write log holds exactly the ops of the selected + confirmed items, each sent once
        items = {it["item_id"]: it for it in plan["items"]}
        expect = _op_requests(items["M-001"]) | _op_requests(items["B-001"])
        got = [(r["method"], r["path"]) for r in w.writes()]
        self.assertEqual(sorted(got), sorted(expect), "exactly the confirmed ops, nothing else, nothing twice")
        self.assertEqual(len(got), 4)
        self.assertTrue(all(r["token"] == "wb" for r in w.writes()))
        self.assertEqual(len(w.fake.pages), pre_pages, "N-001 was not selected: no page created")
        self.assertEqual(w.title(w.child), "子页面", "T-001 rejected: title unchanged")
        self.assertFalse(w.fake.page(w.doomed)["in_trash"], "D-001 not selected: not trashed")
        conf = w.q("SELECT item_id, plan_sha256, phrase_scope, challenge_ok FROM confirmations WHERE plan_id=?", (pid,))
        self.assertEqual(sorted((c["item_id"], c["plan_sha256"], c["phrase_scope"], c["challenge_ok"]) for c in conf),
                         [("B-001", self.S["sha"], "apply", 1), ("M-001", self.S["sha"], "apply", 1)])
        res = w.result(pid)
        self.assertEqual(res["status"], "applied")
        self.assertEqual({k: v["status"] for k, v in res["w_checks"].items()},
                         {"W1": "pass", "W2": "pass", "W3": "pass", "W4": "n/a", "W5": "n/a", "W6": "pass",
                          "W7": "pass", "W8": "pass", "W9": "pass", "W10": "pass"})
        # Notion now holds the edits
        tree = w.tree(w.page)
        texts = [t for _ty, t, _k in tree]
        for s in ("第二段：已在 Obsidian 中修改。", "项目乙（改）", "新增段落一。", "新增段落二。"):
            self.assertIn(s, texts)
        self.assertNotIn("将被删除的一行", texts)
        self.assertNotEqual(tree, pre_tree)
        # the vault note is adopted: file sha = staged/published sha, base_history origin WRITEBACK
        fsha = w.sha(MAIN)
        with open(os.path.join(w.P.staging, pid, "manifest.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)[MAIN]["sha256"], fsha, "vault file = the adoption's staged file")
        self.assertEqual(w.published(MAIN)[0], fsha)
        b = w.q("SELECT origin, sha256 FROM base_history WHERE notion_id=? ORDER BY seq DESC LIMIT 1", (w.page,))[0]
        self.assertEqual((b["origin"], b["sha256"]), ("WRITEBACK", fsha))
        self.assertIn("第二段：已在 Obsidian 中修改。", w.read(MAIN))
        # re-plan: nothing left for the written note (W7); the rejected rename listed under 已拒绝; N still proposed
        w.fake.clear_requests()
        pid2, _s, out2 = w.make_plan()
        plan2, _ = w.plan_obj(pid2)
        self.assertEqual([it for it in plan2["items"] if it.get("notion_id") == w.page], [])
        self.assertEqual(sorted((it["item_id"], it["category"]) for it in plan2["items"]),
                         [("D-001", "D"), ("N-001", "N")])
        self.assertEqual([(r["rel_path"], r["item_category"]) for r in plan2["rejected"]], [(CHILD_NEW, "T")])
        prev = w.read(f"Notion_Obsidian/写回预览/{pid2}.md")
        sec = prev.split("## 已拒绝", 1)[1].split("\n## ", 1)[0]
        self.assertIn(CHILD_NEW, sec)
        self.assertEqual(w.writes(), [])
        self.S.update(applied=pid, pre_tree=pre_tree)
        self.S["done"].add(3)

    # ------------------------------------------------------------------------------------------ step 5
    def test_s5_rollback_drill(self):
        """Rollback through the pty -> Notion = the pre-apply content (the restored block is reported
        "已恢复但位置可能变化": live Notion puts a restored block at the end, contract §7.6); the next refresh brings the
        vault back. (The second half of the drill -- a block edited in Notion after the write-back is skipped by
        the rollback -- runs in TestUnapprovedNeverWritten.test_r4, together with a rollback fault.)"""
        self.need(3)
        w, pid, pre = self.w, self.S["applied"], self.S["pre_tree"]
        jrows = w.q("SELECT seq, item_id, op, target_id FROM journal WHERE plan_id=? AND state='done' ORDER BY seq",
                    (pid,))
        trashed = next(r["target_id"] for r in jrows if r["op"] == "TRASH_BLOCK")
        appended = json.loads(w.q("SELECT committed_ids FROM journal WHERE plan_id=? AND op='APPEND'",
                                  (pid,))[0]["committed_ids"])
        appended = appended["ids"] if isinstance(appended, dict) else appended
        self.assertEqual(len(appended), 2)
        w.fake.clear_requests()
        rc, out, phrase, shown = w.gated("rollback", "--plan", pid)
        self.assertRegex(phrase, r"^确认 \S{4} 回退2\[(M-001,B-001|B-001,M-001)\] [0-9A-F]{6}$")
        self.assertIn("回退预览", shown)
        self.assertIn("恢复为写入前内容", shown)
        self.assertEqual(rc, 0, out)
        tree = w.tree(w.page)
        self.assertEqual(tree[-1], ("paragraph", "将被删除的一行", []), "restored, at the end (live behaviour)")
        self.assertEqual(tree[:-1], [t for t in pre if t[1] != "将被删除的一行"], "everything else = pre-apply")
        self.assertEqual(sorted(map(repr, tree)), sorted(map(repr, pre)), "same content as before the apply")
        rb = [r for r in w.q("SELECT * FROM journal WHERE plan_id=? AND op LIKE 'RB_%' ORDER BY seq", (pid,))]
        self.assertTrue(rb and all(r["state"] == "done" for r in rb), rb)
        got = sorted((r["method"], r["path"]) for r in w.writes())
        expect = sorted([("PATCH", f"blocks/{r['target_id']}") for r in jrows if r["op"] == "UPDATE"] +
                        [("DELETE", f"blocks/{b}") for b in appended] + [("PATCH", f"blocks/{trashed}")])
        self.assertEqual(got, expect, "rollback writes = the reverse of the journal, each once")
        with open(os.path.join(w.work, "reports", "writeback", pid, "rollback.md"), encoding="utf-8") as fh:
            self.assertIn("已恢复但位置可能变化", fh.read())
        # the next refresh round brings the vault note back to Notion's (rolled back) version
        rc, st = w.refresh("R3")
        self.assertIn(st.get("result"), ("published", "published_with_holds"), st)
        text = w.read(MAIN)
        self.assertIn("第二段：将被修改。", text)
        self.assertIn("- 项目乙\n", text)
        self.assertNotIn("新增段落一", text)
        self.assertTrue(text.rstrip("\n").endswith("将被删除的一行"), text[-300:])
        self.S["done"].add(5)

    # ------------------------------------------------------------------------------------------ step 6
    def test_s6_delete_and_rename_policy(self):
        """A deleted vault note is never trashed in Notion unless its D item is selected with --delete and
        confirmed; a renamed note never changes the Notion title unless --rename selects its T item. (Steps 2-5
        already ran with the local delete held and the rename present: no apply / rollback touched either.)"""
        self.need(5)
        w = self.w
        self.assertFalse(w.fake.page(w.doomed)["in_trash"])
        self.assertEqual(w.title(w.child), "子页面")
        rc, out = w.yes_no("unreject", "--path", CHILD_NEW)
        self.assertEqual(rc, 0, out)
        pid, sha12, out = w.make_plan()
        plan, sha = w.plan_obj(pid)
        cats = {it["item_id"]: (it["category"], it.get("notion_id"), it.get("executable")) for it in plan["items"]}
        self.assertEqual(cats, {"N-001": ("N", None, True), "D-001": ("D", w.doomed, True),
                                "T-001": ("T", w.child, True)}, out)
        w.fake.clear_requests()
        # D / T need their own flags: --items refuses them before any challenge
        for argv, msg in ((["--items", "D-001"], "--delete"), (["--items", "T-001"], "--rename")):
            child = w.spawn(*w.apply_args(pid, sha12, *argv))
            child.expect(pexpect.EOF)
            child.close()
            self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, child.before)
            self.assertIn(msg, child.before)
            self.assertNotIn("请原样输入", child.before)
        # --all-safe = M + N only: the exact list and the phrase leave the deletion and the rename out
        rc, out, phrase, shown = w.gated(*w.apply_args(pid, sha12, "--all-safe"), mutate=lambda p: "取消")
        self.assertRegex(phrase, r"^确认 \S{4} 修改0 新增1 删除0 改名0 [0-9A-F]{6}$")
        self.assertEqual(re.findall(r"^\s+([A-Z]+-\d{3})  ", shown, re.M), ["N-001"])
        self.assertEqual(rc, writeback.EXIT_FAIL, out)
        # a wrong phrase for the deletion + rename: nothing is written
        rc, out, phrase, _s = w.gated(*w.apply_args(pid, sha12, "--delete", "D-001", "--rename", "T-001"),
                                      mutate=lambda p: p.replace("删除1", "删除2"))
        self.assertEqual(rc, writeback.EXIT_FAIL, out)
        self.assertEqual(w.writes(), [])
        self.assertFalse(w.fake.page(w.doomed)["in_trash"])
        self.assertEqual(w.title(w.child), "子页面")
        # explicitly selected and confirmed: exactly the page trash and the title change, nothing else
        rc, out, phrase, shown = w.gated(*w.apply_args(pid, sha12, "--delete", "D-001", "--rename", "T-001"))
        self.assertRegex(phrase, r"^确认 \S{4} 修改0 新增0 删除1\[D-001\]（含子页面0个） 改名1\[T-001\] [0-9A-F]{6}$")
        self.assertIn("移入回收站：页面 待删页面", shown)
        self.assertIn("Notion 标题：子页面 → 子页面改名", shown)
        self.assertEqual(rc, 0, out)
        writes = w.writes()
        self.assertEqual(sorted((r["method"], r["path"]) for r in writes),
                         sorted([("PATCH", f"pages/{w.doomed}"), ("PATCH", f"pages/{w.child}")]))
        self.assertEqual(next(r["body"] for r in writes if r["path"] == f"pages/{w.doomed}").get("in_trash"), True)
        assert_writes_confirmed(self, w, pid, writes)
        self.assertTrue(w.fake.page(w.doomed)["in_trash"])
        self.assertEqual(w.title(w.child), "子页面改名")
        self.assertEqual(len([p for p in w.fake.pages.values() if not p.get("in_trash")]), 2, "N-001 not created")
        res = w.result(pid)
        self.assertEqual(res["status"], "applied", json.dumps(res["w_checks"], ensure_ascii=False))
        self.assertEqual((res["w_checks"]["W4"]["status"], res["w_checks"]["W5"]["status"]), ("pass", "pass"))
        self.assertEqual(w.q("SELECT status FROM name_overrides WHERE notion_id=? ORDER BY rowid DESC LIMIT 1",
                             (w.child,))[0]["status"], "closed", "the rename is adopted: 命名差异 closed")
        self.S["done"].add(6)


# ============================================================================================ step 4 (R-223)
_SENT_STATES = ("sent", "done", "unknown", "failed", "reconciled")


def _journal_match(j, method, path):
    parts = path.split("/")
    if method == "POST" and path == "pages":
        return j["op"] == "CREATE_PAGE"
    if len(parts) < 2 or j["target_id"] != parts[1]:
        return False
    if parts[0] == "blocks" and len(parts) == 3 and parts[2] == "children":
        return method == "PATCH" and j["op"] == "APPEND"
    if parts[0] == "blocks" and len(parts) == 2:
        return (method == "PATCH" and j["op"] in ("UPDATE", "RB_UPDATE", "RB_RESTORE_BLOCK")) or \
            (method == "DELETE" and j["op"] in ("TRASH_BLOCK", "RB_TRASH_BLOCK"))
    if parts[0] == "pages" and len(parts) == 2:
        return method == "PATCH" and j["op"] in ("UPDATE_PROPERTIES", "RENAME", "TRASH_PAGE", "RB_PROPERTIES",
                                                 "RB_RENAME", "RB_RESTORE_PAGE", "RB_TRASH_PAGE")
    return False


def assert_writes_confirmed(tc, w, pid, writes):
    """R-223: every write request maps to exactly one journal row (sent at most once) of an item that has a
    challenge-confirmed `confirmations` row for the plan's FULL sha256 in the matching scope (forward ops: apply /
    resume; RB_* ops: rollback); the write token sent it."""
    sha = w.q("SELECT plan_sha256 FROM plans WHERE plan_id=?", (pid,))[0]["plan_sha256"]
    conf = {(c["item_id"], c["phrase_scope"]) for c in w.q(
        "SELECT item_id, phrase_scope FROM confirmations WHERE plan_id=? AND plan_sha256=? AND challenge_ok=1",
        (pid, sha))}
    rows = [j for j in w.q("SELECT seq, item_id, op, target_id, state FROM journal WHERE plan_id=? ORDER BY seq",
                           (pid,)) if j["state"] in _SENT_STATES]
    used = set()
    for r in writes:
        tc.assertEqual(r["token"], "wb", r)
        j = next((j for j in rows if j["seq"] not in used and _journal_match(j, r["method"], r["path"])), None)
        tc.assertIsNotNone(j, f"write {r['method']} {r['path']} has no journal row of plan {pid} (resent?)")
        used.add(j["seq"])
        scopes = ("rollback",) if j["op"].startswith("RB_") else ("apply", "resume")
        tc.assertTrue(any((j["item_id"], sc) in conf for sc in scopes),
                      f"journal #{j['seq']} {j['op']} belongs to an item without a {scopes} confirmation")
    keys = [(r["method"], r["path"], json.dumps(r["body"], sort_keys=True)) for r in writes]
    tc.assertEqual(len(keys), len(set(keys)), "a request was sent twice")


@unittest.skipIf(pexpect is None, "pexpect not installed")
class TestUnapprovedNeverWritten(unittest.TestCase):
    """R-223: no CLI command writes to Notion without the confirmed challenge; with faults, every write maps to a
    confirmed item of the confirmed plan sha and nothing is resent."""

    S = {}

    @classmethod
    def setUpClass(cls):
        cls.w = World(_ClassCleanup(cls), "inv")
        rc, out, err = cls.w.main("plan")          # enabling on the clean vault
        assert rc == 0, out + err
        cls.S = {"done": set()}

    def need(self, *steps):
        missing = [s for s in steps if s not in self.S["done"]]
        if missing:
            self.skipTest(f"previous step(s) {missing} did not pass")

    def zero(self, what):
        self.assertEqual(self.w.writes(), [], f"{what}: a Notion write was sent")

    def para_id(self, page, text):
        return next(b["id"] for b in self.w.fake.children(page) if plain(b) == text)

    def remote_text(self, bid, text):
        rich = self.w.fake.blocks[bid][self.w.fake.blocks[bid]["type"]]["rich_text"]
        rich[:] = [{"type": "text", "text": {"content": text, "link": None}, "plain_text": text, "href": None,
                    "annotations": {"bold": False, "italic": False, "strikethrough": False, "underline": False,
                                    "code": False, "color": "default"}}]

    # ------------------------------------------------------------------------------------------ view + decisions
    def test_r1_view_and_decision_commands_send_nothing(self):
        w = self.w
        # a refresh round paused as local_mass_delete (max_local_deletes 0, one note deleted locally): neither
        # restored nor held (WB 6.0(c)); `hold --run` turns that deletion into a pending-delete hold (D item)
        os.unlink(w.path(DOOMED))
        w.write_cfg(max_local_deletes=0)
        rc, st = w.refresh("R2")
        w.write_cfg(max_local_deletes=5)
        self.assertEqual(st.get("result"), "paused_for_review", st)
        self.assertTrue(any(r.startswith("local_mass_delete") for r in st["pause_reasons"]), st["pause_reasons"])
        self.assertEqual(w.q("SELECT count(*) n FROM pending_deletes")[0]["n"], 0)
        self.remote_text(self.para_id(w.child, "子页面正文"), "子页面正文（Notion）")
        w.edit(MAIN, "第二段：将被修改。", "第二段：R223 修改。")
        w.edit(CHILD, "子页面正文", "子页面正文（本地）")             # local + Notion changed -> C
        w.fake.clear_requests()
        rc, out = w.yes_no("hold", "--run", "R2")
        self.assertEqual(rc, 0, out)
        self.zero("hold")
        self.assertEqual([h["status"] for h in w.q("SELECT status FROM pending_deletes WHERE notion_id=?",
                                                   (w.doomed,))], ["hold"])
        for argv in (["status"], ["whitelist-check"], ["plan"]):
            r = w.run(*argv)
            self.assertEqual(r.returncode, 0, (argv, r.stdout[-500:], r.stderr[-500:]))
            self.zero(argv[0])
        psub = re.search(r"plan_id：(\S+)", r.stdout).group(1)
        r = w.run("show", "--plan", psub)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.zero("show")
        pid, sha12, out = w.make_plan()
        plan, _ = w.plan_obj(pid)
        cats = {it["category"]: it["item_id"] for it in plan["items"]}
        self.assertEqual(sorted(cats), ["C", "D", "M"], out)
        steps = [
            (["reject", "--plan", pid, "--items", cats["M"]], 0),
            (["unreject", "--path", MAIN], 0),
            (["restore-deleted", "--plan", pid, "--item", cats["D"]], 0),
            (["resolve", "--plan", pid, "--item", cats["C"], "--take", "notion"], 0),
        ]
        for argv, code in steps:
            rc, out = w.yes_no(*argv)
            self.assertEqual(rc, code, (argv, out))
            self.zero(argv[0])
        self.assertEqual(w.q("SELECT status FROM pending_deletes WHERE notion_id=?", (w.doomed,))[0]["status"],
                         "expired")
        self.assertIn("子页面正文\n", w.read(CHILD), "take notion: the local edit is backed up and dropped")
        # resolve --take obsidian: human + phrase; creates a NEW plan; not a single Notion request (not even a read)
        w.edit(CHILD, "子页面正文", "子页面正文（本地2）")
        pid2, _s2, _ = w.make_plan()
        c2 = _items(w.plan_obj(pid2)[0], "C")[0]["item_id"]
        w.fake.clear_requests()
        rc, out, phrase, _shown = w.gated("resolve", "--plan", pid2, "--item", c2, "--take", "obsidian")
        self.assertRegex(phrase, rf"^确认 \S{{4}} 以本地为准 {c2} [0-9A-F]{{6}}$")
        self.assertEqual(rc, 0, out)
        self.assertEqual(w.fake.requests, [], "resolve --take obsidian sends no Notion request at all")
        new_plan = re.search(r"新计划 (\S+)（", out).group(1)
        self.assertEqual(w.plan_status(new_plan), "draft")
        # unblock (a page left BLOCKED by an earlier run), ack-backlog, audit-scope, hold again (R2 is done)
        db = w.db()
        with wb_state.transaction(db):
            db.execute("INSERT OR REPLACE INTO page_state(notion_id, state, reason, since) VALUES(?,?,?,?)",
                       (w.page, "BLOCKED", "e2e: earlier unknown outcome", wb_common.now_iso()))
        db.close()
        for argv, code in ((["unblock", "--page", w.page], 0), (["ack-backlog", "--path", MAIN], 0),
                           (["audit-scope"], 0), (["hold", "--run", "R2"], writeback.EXIT_REFUSED)):  # R2: done
            rc, out = w.yes_no(*argv)
            self.assertEqual(rc, code, (argv, out))
            self.zero(argv[0])
        self.assertEqual(w.q("SELECT state FROM page_state WHERE notion_id=?", (w.page,))[0]["state"], "normal")
        # ack-anomaly: a plan that trips the breaker (max_ops_per_page 1; the test page has 2 ops)
        w.edit(MAIN, "最后一段。", "最后一段（R223）。")
        w.write_cfg(max_ops_per_page=1)
        pid3, _s3, _ = w.make_plan()
        self.assertTrue(w.plan_obj(pid3)[0]["breaker"]["tripped"])
        rc, out = w.yes_no("ack-anomaly", "--run", pid3)
        self.assertEqual(rc, 0, out)
        self.zero("ack-anomaly")
        w.write_cfg(max_ops_per_page=50)
        # discard that draft; rebase --from-notion (mode plan): local edits backed up, published bytes restored
        pid4 = pid3
        rc, out = w.yes_no("discard", "--plan", pid4)
        self.assertEqual(rc, 0, out)
        self.assertEqual(w.plan_status(pid4), "superseded")
        w.write_cfg(mode="plan")
        rc, out = w.yes_no("rebase", "--from-notion")
        self.assertEqual(rc, 0, out)
        w.write_cfg(mode="full")
        self.assertIn("第二段：将被修改。", w.read(MAIN))
        self.zero("discard / rebase")
        self.assertEqual(w.q("SELECT count(*) n FROM journal")[0]["n"], 0, "no journal row: nothing was ever sent")
        self.S.update(discarded=pid4)
        self.S["done"].add(1)

    # ------------------------------------------------------------------------------------------ apply refusals
    def test_r2_apply_refusals_send_nothing(self):
        self.need(1)
        w = self.w
        w.edit(MAIN, "第二段：将被修改。", "第二段：拒绝测试。")
        pid, sha12, _ = w.make_plan()
        w.fake.clear_requests()
        bad = ("0" if sha12[0] != "0" else "1") + sha12[1:]
        child = w.spawn(*w.apply_args(pid, bad, "--items", "M-001"))
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, child.before)
        self.assertNotIn("请原样输入", child.before)
        rc, out, _p, _s = w.gated(*w.apply_args(pid, sha12, "--items", "M-001"),
                                  mutate=lambda p: p.replace("修改1", "修改2"))
        self.assertEqual(rc, writeback.EXIT_FAIL, out)
        self.assertIn("短语不符", out)
        r = w.run(*w.apply_args(pid, sha12, "--items", "M-001"))                    # no terminal
        self.assertEqual(r.returncode, writeback.EXIT_GATE, r.stderr)
        for var in AGENT_VARS + ("CLAUDE_CODE_ENTRYPOINT",):
            child = w.spawn(*w.apply_args(pid, sha12, "--items", "M-001"), env=clean_env(**{var: "1"}))
            child.expect(pexpect.EOF)
            child.close()
            self.assertEqual(child.exitstatus, writeback.EXIT_GATE, (var, child.before))
            self.assertIn(var, child.before)
        shell = "echo 确认 | " + " ".join([sys.executable, "-I", "-B", WB] + w.argv +
                                         w.apply_args(pid, sha12, "--items", "M-001"))
        child = pexpect.spawn("/bin/sh", ["-c", shell], env=clean_env(), encoding="utf-8", timeout=60)
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, writeback.EXIT_GATE, child.before)
        w.write_cfg(mode="plan")
        child = w.spawn(*w.apply_args(pid, sha12, "--items", "M-001"))
        child.expect(pexpect.EOF)
        child.close()
        w.write_cfg(mode="full")
        self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, child.before)
        self.assertIn("mode=plan 只允许生成预览", child.before)
        with open(os.path.join(w.P.state, "WRITEBACK_STOP"), "w") as fh:
            fh.write("stop\n")
        child = w.spawn(*w.apply_args(pid, sha12, "--items", "M-001"))
        child.expect(pexpect.EOF)
        child.close()
        os.unlink(os.path.join(w.P.state, "WRITEBACK_STOP"))
        self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, child.before)
        self.assertIn("WRITEBACK_STOP", child.before)
        # superseded (a newer plan), expired (time passed), discarded (step r1)
        pid_new, sha_new, _ = w.make_plan()
        self.assertEqual(w.plan_status(pid), "superseded")
        db = w.db()
        with wb_state.transaction(db):
            db.execute("UPDATE plans SET expires_at=? WHERE plan_id=?", ("2026-01-01T00:00:00+07:00", pid_new))
        db.close()
        discarded = self.S["discarded"]
        dsha = w.q("SELECT plan_sha256 FROM plans WHERE plan_id=?", (discarded,))[0]["plan_sha256"][:12]
        for p, s, msg in ((pid, sha12, "superseded"), (pid_new, sha_new, "expired"), (discarded, dsha, "superseded")):
            child = w.spawn(*w.apply_args(p, s, "--all-safe"))
            child.expect(pexpect.EOF)
            child.close()
            self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, (msg, child.before))
            self.assertIn(msg, child.before)
            self.assertNotIn("请原样输入", child.before)
        self.zero("apply refusals")
        self.assertEqual(w.q("SELECT count(*) n FROM confirmations WHERE plan_id IN (?,?,?)",
                             (pid, pid_new, discarded))[0]["n"], 0)
        self.assertEqual(w.q("SELECT count(*) n FROM journal")[0]["n"], 0)
        self.S["done"].add(2)

    # ------------------------------------------------------------------------------------------ faults: apply
    def test_r3_apply_with_faults_never_resends(self):
        """409 (rejected), 503 after commit (committed ids in the guidance) and a connection lost after commit
        (what a client timeout after commit looks like to the client: sent, no answer): one request per op, all
        confirmed, none resent; the page whose lost UPDATE is GET-verified as applied continues with its APPEND."""
        self.need(2)
        w = self.w
        rc, st = w.refresh("R3")                     # restores the held note (restore-deleted) and syncs the child
        self.assertIn(st.get("result"), ("published", "published_with_holds"), st)
        self.assertTrue(os.path.exists(w.path(DOOMED)))
        w.write(MAIN, w.read(MAIN).rstrip("\n") + "\n\n故障测试追加段。\n")
        w.edit(CHILD, "子页面正文（Notion）", "子页面正文（故障 409）")
        pid, sha12, out = w.make_plan()
        plan, _ = w.plan_obj(pid)
        by_rel = {it["rel_path"]: it for it in _items(plan, "M")}
        self.assertEqual(sorted(by_rel), sorted([MAIN, CHILD]), out)
        self.assertEqual([o["op"] for o in by_rel[MAIN]["ops"]], ["UPDATE", "APPEND"])
        main_para = by_rel[MAIN]["ops"][0]["target_id"]
        child_para = self.para_id(w.child, "子页面正文（Notion）")
        w.fake.inject("PATCH", rf"^blocks/{main_para}$", after_commit=True, drop=True)
        w.fake.inject("PATCH", rf"^blocks/{w.page}/children$", status=503, after_commit=True)
        w.fake.inject("PATCH", rf"^blocks/{child_para}$", status=409)
        w.fake.clear_requests()
        ids = ",".join(it["item_id"] for it in by_rel.values())
        rc, out, phrase, _s = w.gated(*w.apply_args(pid, sha12, "--items", ids))
        w.fake.clear_faults()
        self.assertRegex(phrase, r" 修改2 新增0 删除0 改名0 ")
        self.assertEqual(rc, writeback.EXIT_FAIL, out)
        res = w.result(pid)
        self.assertEqual(res["status"], "partially_applied")
        writes = w.writes()
        self.assertEqual(sorted((r["method"], r["path"]) for r in writes), sorted(
            [("PATCH", f"blocks/{main_para}"), ("PATCH", f"blocks/{w.page}/children"),
             ("PATCH", f"blocks/{child_para}")]))
        assert_writes_confirmed(self, w, pid, writes)
        self.assertEqual({r["path"]: r["status"] for r in writes if r["fault"]},
                         {f"blocks/{main_para}": "dropped", f"blocks/{w.page}/children": 503,
                          f"blocks/{child_para}": 409}, "every injected fault fired")
        texts = [t for _ty, t, _k in w.tree(w.page)]
        self.assertIn("第二段：拒绝测试。", texts, "committed before the connection was lost")
        self.assertEqual(texts.count("故障测试追加段。"), 1, "the 503-committed append is verified, not resent")
        self.assertEqual(plain(w.fake.block(child_para)), "子页面正文（Notion）", "409: not applied, not retried")
        states = {(j["op"], j["target_id"]): j["state"] for j in w.q("SELECT op, target_id, state FROM journal "
                                                                     "WHERE plan_id=?", (pid,))}
        self.assertEqual(states, {("UPDATE", main_para): "done", ("APPEND", w.page): "done",
                                  ("UPDATE", child_para): "failed"})
        appended = json.loads(w.q("SELECT committed_ids FROM journal WHERE plan_id=? AND op='APPEND'",
                                  (pid,))[0]["committed_ids"])
        self.S.update(fault_plan=pid, main_para=main_para,
                      appended=(appended["ids"] if isinstance(appended, dict) else appended))
        self.S["done"].add(3)

    # ------------------------------------------------------------------------------------------ faults: rollback
    def test_r4_rollback_preview_skip_and_rollback_with_a_fault(self):
        """Rollback preview without confirming sends nothing; then someone edits a written block in Notion and
        the rollback skips it (step 5, second half), while a 409 on another rollback write is not retried."""
        self.need(3)
        w, pid, main_para, (appended,) = self.w, self.S["fault_plan"], self.S["main_para"], self.S["appended"]
        w.fake.clear_requests()
        rc, out, _phrase, shown = w.gated("rollback", "--plan", pid, mutate=lambda p: p[:-1] + "X")
        self.assertEqual(rc, writeback.EXIT_FAIL, out)
        self.assertIn("回退预览", shown)
        self.zero("rollback preview without confirming")
        self.remote_text(main_para, "第二段：Notion 中的人工修改。")
        w.fake.inject("DELETE", rf"^blocks/{appended}$", status=409)
        rc, out, phrase, shown = w.gated("rollback", "--plan", pid)
        w.fake.clear_faults()
        self.assertRegex(shown, r"块在写入后又被编辑（与 after_json 不同）：跳过")
        self.assertEqual(rc, writeback.EXIT_FAIL, out)
        writes = w.writes()
        assert_writes_confirmed(self, w, pid, writes)
        self.assertEqual([(r["method"], r["path"], r["status"]) for r in writes],
                         [("DELETE", f"blocks/{appended}", 409)],
                         "the edited block is skipped; the rejected rollback write is sent once, not retried")
        texts = [t for _ty, t, _k in w.tree(w.page)]
        self.assertIn("第二段：Notion 中的人工修改。", texts, "the human edit in Notion is kept")
        self.assertEqual(texts.count("故障测试追加段。"), 1, "409: the appended block is still there")
        self.S["done"].add(4)

    # ------------------------------------------------------------------------------------------ crash + resume
    def crash_on_next_write(self, argv, path_regex):
        """Run a gated write command on the pty, type the challenge, and kill -9 the CLI while the next matching
        write has been COMMITTED by the fake but its response is still delayed (journal row `sent`)."""
        w = self.w
        fault = w.fake.inject("PATCH", path_regex, after_commit=True, delay=60, times=1)
        n0 = len(w.fake.requests)
        child = w.spawn(*argv)
        phrase, _shown = w.challenge(child)
        deadline = time.time() + 60
        while time.time() < deadline and not any(r["fault"] and r["committed"] for r in w.fake.requests[n0:]):
            time.sleep(0.05)
        self.assertEqual(fault.hits, 1)
        os.kill(child.pid, signal.SIGKILL)
        child.close(force=True)
        w.fake.clear_faults()
        return phrase

    def test_r5_crash_between_journal_steps_resume_reconcile(self):
        """kill -9 of the CLI while a committed write waits for its response (journal row `sent`), then
        `apply --resume` through the pty: the sent request is never resent (unknown -> reconcile) and the page that
        had only `intent` rows is written once; that resume is killed the same way, and the last `apply --resume`
        is the closing run (no intent rows left: 0 write requests). reconcile / unblock send nothing."""
        self.need(4)
        w = self.w
        rc, st = w.refresh("R4")
        self.assertIn(st.get("result"), ("published", "published_with_holds"), st)
        w.edit(MAIN, "第一段：普通文字。", "第一段：崩溃测试。")
        pid, sha12, out = w.make_plan()
        plan, _ = w.plan_obj(pid)
        ms = {it["rel_path"]: it for it in _items(plan, "M")}
        self.assertEqual(sorted(ms), sorted([MAIN, CHILD]), out)
        self.assertTrue(all([o["op"] for o in it["ops"]] == ["UPDATE"] for it in ms.values()), out)
        w.fake.clear_requests()
        # 1. apply, killed after its first write was committed
        self.crash_on_next_write(w.apply_args(pid, sha12, "--items", ",".join(it["item_id"] for it in ms.values())),
                                 r"^blocks/[0-9a-f-]{36}$")
        first = w.writes()
        self.assertEqual(len(first), 1, first)
        self.assertTrue(first[0]["fault"] and first[0]["committed"], "the request was committed, then the CLI died")
        crashed = first[0]["path"].split("/")[1]
        j = {r["target_id"]: r for r in w.q("SELECT seq, item_id, target_id, state FROM journal WHERE plan_id=?",
                                            (pid,))}
        self.assertEqual(sorted(r["state"] for r in j.values()), ["intent", "sent"])
        self.assertEqual(j[crashed]["state"], "sent")
        self.assertEqual(w.plan_status(pid), "applying")
        other = next(t for t, r in j.items() if r["state"] == "intent")
        # 2. apply --resume: only the intent row is sent (once); killed the same way after that commit
        phrase = self.crash_on_next_write(["apply", "--resume", pid, "--blobs", w.blobs, "--sidebar", w.sidebar],
                                          rf"^blocks/{other}$")
        self.assertRegex(phrase, rf"^确认 \S{{4}} 续执行1\[{j[other]['item_id']}\] [0-9A-F]{{6}}$")
        writes = w.writes()
        self.assertEqual(sorted(r["path"] for r in writes), sorted([f"blocks/{crashed}", f"blocks/{other}"]),
                         "the crashed request is never resent; the intent one is sent once")
        st2 = {r["target_id"]: r["state"] for r in w.q("SELECT target_id, state FROM journal WHERE plan_id=?", (pid,))}
        self.assertEqual(st2, {crashed: "unknown", other: "sent"})
        # 3. closing run: no intent row left -> 0 write requests; sent -> unknown, plan closed
        rc, out, phrase, shown = w.gated("apply", "--resume", pid, "--blobs", w.blobs, "--sidebar", w.sidebar)
        self.assertRegex(phrase, r"^确认 \S{4} 续执行0\[\] [0-9A-F]{6}$")
        self.assertIn("本次不会发送任何写入请求（0 个）", shown)
        self.assertEqual(w.writes(), writes, "the closing run sends nothing")
        assert_writes_confirmed(self, w, pid, writes)
        self.assertNotEqual(w.plan_status(pid), "applying", out)
        rows = w.q("SELECT seq, target_id, state FROM journal WHERE plan_id=? ORDER BY seq", (pid,))
        self.assertEqual(sorted(r["state"] for r in rows), ["unknown", "unknown"])
        self.assertEqual(w.q("SELECT count(*) n FROM confirmations WHERE plan_id=? AND phrase_scope='resume'",
                             (pid,))[0]["n"], 1, "one resume confirmation per resumed item (the closing run: none)")
        texts = {plain(b) for b in w.fake.children(w.page)} | {plain(b) for b in w.fake.children(w.child)}
        self.assertIn("第一段：崩溃测试。", texts)
        self.assertIn("子页面正文（故障 409）", texts)
        # 4. reconcile (both committed: verified by a live GET) and unblock: nothing is written
        blocked = sorted(r["notion_id"] for r in w.q("SELECT notion_id FROM page_state WHERE state='BLOCKED'"))
        self.assertEqual(blocked, sorted([w.page, w.child]))
        w.fake.clear_requests()
        for r in rows:
            rc, out = w.yes_no("reconcile", "--journal", str(r["seq"]), "--decision", "committed")
            self.assertEqual(rc, 0, out)
        self.assertEqual([r["state"] for r in w.q("SELECT state FROM journal WHERE plan_id=?", (pid,))],
                         ["reconciled", "reconciled"])
        for nid in blocked:
            rc, out = w.yes_no("unblock", "--page", nid)
            self.assertEqual(rc, 0, out)
        self.zero("reconcile / unblock")
        self.S["done"].add(5)


# ============================================================================================ glue fixes (W3)
@unittest.skipIf(pexpect is None, "pexpect not installed")
class TestGlueFixes(unittest.TestCase):
    def test_hold_never_registers_a_deletion_twice(self):
        """writeback.py hold (W3 fix): a round paused for another reason (source_mass_change) has already put
        its new local deletion on hold (refresh runs update_pending before the pause decision). `hold --run` used
        to INSERT a second hold row for the same page (-> two D items for one deletion); now it reports the
        deletion as already held and changes nothing."""
        w = World(self, "glue")
        rc, out, err = w.main("plan")
        self.assertEqual(rc, 0, out + err)
        b = next(x["id"] for x in w.fake.children(w.child) if plain(x) == "子页面正文")
        w.fake.blocks[b]["paragraph"]["rich_text"][0]["text"]["content"] = "子页面正文（Notion）"
        w.fake.blocks[b]["paragraph"]["rich_text"][0]["plain_text"] = "子页面正文（Notion）"
        os.unlink(w.path(DOOMED))
        w.write_cfg(source_change_pause_ratio=0.10)
        rc, st = w.refresh("R2")
        w.write_cfg(source_change_pause_ratio=1.0)
        self.assertEqual(st.get("result"), "paused_for_review", st)
        self.assertTrue(any(r.startswith("source_mass_change") for r in st["pause_reasons"]), st["pause_reasons"])
        self.assertEqual([r["status"] for r in w.q("SELECT status FROM pending_deletes WHERE notion_id=?",
                                                   (w.doomed,))], ["hold"])
        before = refresh.load_paused(w.P)
        w.fake.clear_requests()
        rc, out = w.yes_no("hold", "--run", "R2")
        self.assertEqual(rc, writeback.EXIT_REFUSED, out)
        self.assertIn("已在 hold 中", out)
        self.assertEqual([r["status"] for r in w.q("SELECT status FROM pending_deletes WHERE notion_id=?",
                                                   (w.doomed,))], ["hold"], "still exactly one hold row")
        self.assertEqual(refresh.load_paused(w.P), before, "the paused round is left as it was")
        self.assertEqual(w.writes(), [])
        pid, _sha, out = w.make_plan()
        self.assertEqual([it["category"] for it in w.plan_obj(pid)[0]["items"] if it["category"] == "D"], ["D"])


# ============================================================================================ L-17 (wave 3.5)
@unittest.skipIf(pexpect is None, "pexpect not installed")
class TestUnregisteredRenameL17(unittest.TestCase):
    """L-17 end to end (W3 F1): while the scan shows a local rename / move that no refresh has registered yet,
    `status` shows the notice, every item of `plan` is non-executable (UNREGISTERED_RENAME + hint) and `apply` is
    refused at the selection with 0 writes. After the next refresh round registered it (same-folder rename: the
    round published the local name; cross-folder move: object hold) the same edit is executable and the apply
    passes W1-W10 (without L-17 these applies ended partially_applied, W6 C04/C05: W3 probe F1(a)).
    Fix loop 1: any hold of the object with its subtree registers it (a rejected same-folder rename, a manual
    hold), and items under a held folder note are refused at plan time. Fix loop 2: an open name override of a
    round that paused (published nothing) does not register the rename; a hold of an ancestor with its subtree
    does."""

    H = wb_plan.UNREGISTERED_RENAME_HINT
    MOVED = "General/待删页面.md"

    def blocked_plan(self, w, kind):
        rc, out, err = w.main("status")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"注意 UNREGISTERED_RENAME：{self.H}", out)
        pid, sha12, out = w.make_plan()
        self.assertIn(self.H, out)
        plan, _ = w.plan_obj(pid)
        self.assertTrue(plan["items"])
        self.assertTrue(all(wb_plan.UNREGISTERED_RENAME in it["blocked_by"] and not it["executable"]
                            for it in plan["items"]), [(it["item_id"], it["blocked_by"]) for it in plan["items"]])
        self.assertEqual([e["kind"] for e in plan["unregistered_renames"]], [kind])
        self.assertIn(self.H, w.read(f"Notion_Obsidian/写回预览/{pid}.md"))
        return pid, sha12, plan

    def refused_apply(self, w, pid, sha12, *sel):
        w.fake.clear_requests()                              # earlier steps' confirmed writes are not this call's
        child = w.spawn(*w.apply_args(pid, sha12, *sel))
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, child.before)
        self.assertIn("UNREGISTERED_RENAME", child.before)
        self.assertIn(self.H, child.before)
        self.assertNotIn("请原样输入", child.before)
        self.assertEqual(w.writes(), [], "refused at the selection: no write")
        self.assertEqual(w.q("SELECT count(*) n FROM confirmations WHERE plan_id=?", (pid,))[0]["n"], 0)
        self.assertEqual(w.plan_status(pid), "draft")

    def executable_plan(self, w, *rels):
        rc, out, err = w.main("status")
        self.assertNotIn("UNREGISTERED_RENAME", out)
        pid, sha12, out = w.make_plan()
        self.assertNotIn(self.H, out)
        plan, _ = w.plan_obj(pid)
        self.assertEqual(plan["unregistered_renames"], [])
        mine = [it for it in plan["items"] if it.get("rel_path") in rels]
        self.assertTrue(mine and all(it["executable"] for it in mine), [(it["item_id"], it["blocked_by"]) for it in mine])
        return pid, sha12, mine

    def applied(self, w, pid, sha12, items):
        sel = []
        for it in items:
            sel += [{"M": "--items", "T": "--rename"}[it["category"]], it["item_id"]]
        w.fake.clear_requests()
        rc, out, phrase, shown = w.gated(*w.apply_args(pid, sha12, *sel))
        self.assertEqual(rc, 0, out)
        res = w.result(pid)
        st = {k: v["status"] for k, v in res["w_checks"].items()}
        self.assertEqual(res["status"], "applied", json.dumps(res["w_checks"], ensure_ascii=False))
        self.assertEqual(sorted(st), [f"W{i}" for i in (1, 10, 2, 3, 4, 5, 6, 7, 8, 9)])
        self.assertFalse([k for k, v in st.items() if v not in ("pass", "n/a")], st)
        for k in ("W1", "W2", "W3", "W6", "W7", "W8", "W10"):
            self.assertEqual(st[k], "pass", (k, st))
        writes = w.writes()
        self.assertTrue(writes)
        assert_writes_confirmed(self, w, pid, writes)
        return res

    def test_rename_and_move_blocked_until_a_refresh_registers_them(self):
        w = World(self, "l17")
        rc, out, err = w.main("plan")                       # enabling on the clean vault
        self.assertEqual(rc, 0, out + err)
        # 1. same-folder rename in Obsidian (links updated by Obsidian), an unrelated note edited; no refresh yet
        os.rename(w.path(CHILD), w.path(CHILD_NEW))
        w.write(MAIN, w.read(MAIN).replace(f"[[{CHILD[:-3]}|", f"[[{CHILD_NEW[:-3]}|"))
        w.edit(DOOMED, "待删页面正文", "待删页面正文（改名之后编辑）")
        pid, sha12, plan = self.blocked_plan(w, "renamed_same_folder")
        m = next(it["item_id"] for it in plan["items"] if it["category"] == "M" and it["rel_path"] == DOOMED)
        self.refused_apply(w, pid, sha12, "--items", m)
        self.refused_apply(w, pid, sha12, "--all-safe")
        self.assertEqual(w.q("SELECT count(*) n FROM journal")[0]["n"], 0)
        # 2. the refresh round registers the rename (命名差异) -> the edit and the rename are executable -> apply of
        #    both passes W1-W10
        rc, st = w.refresh("R2")
        self.assertIn(st.get("result"), ("published", "published_with_holds"), st)
        self.assertEqual([o["status"] for o in w.q("SELECT status FROM name_overrides WHERE notion_id=?",
                                                    (w.child,))], ["open"])
        pid, sha12, mine = self.executable_plan(w, DOOMED, CHILD_NEW)
        self.assertEqual(sorted((it["category"], it["rel_path"]) for it in mine), [("M", DOOMED), ("T", CHILD_NEW)])
        res = self.applied(w, pid, sha12, mine)
        self.assertEqual((res["w_checks"]["W4"]["status"], res["w_checks"]["W5"]["status"]), ("n/a", "pass"))
        self.assertIn("待删页面正文（改名之后编辑）", [plain(b) for b in w.fake.children(w.doomed)])
        self.assertEqual(w.title(w.child), "子页面改名")
        self.assertEqual(w.q("SELECT status FROM name_overrides WHERE notion_id=? ORDER BY rowid DESC LIMIT 1",
                             (w.child,))[0]["status"], "closed")
        # 3. a cross-folder move of that note (no refresh yet), the renamed note edited -> blocked again
        os.rename(w.path(DOOMED), w.path(self.MOVED))
        w.edit(CHILD_NEW, "子页面正文", "子页面正文（移动之后编辑）")
        pid, sha12, plan = self.blocked_plan(w, "moved_other_folder")
        m = next(it["item_id"] for it in plan["items"] if it["category"] == "M" and it["rel_path"] == CHILD_NEW)
        self.refused_apply(w, pid, sha12, "--items", m)
        # 4. the refresh holds the moved object with its new path (registered) -> apply passes W1-W10
        rc, st = w.refresh("R3")
        self.assertIn(st.get("result"), ("published", "published_with_holds"), st)
        hold = refresh.load_holds(w.P).get(w.doomed) or {}
        self.assertIn(self.MOVED, hold.get("paths") or [], hold)
        pid, sha12, mine = self.executable_plan(w, CHILD_NEW)
        self.assertEqual([it["category"] for it in mine], ["M"])
        self.applied(w, pid, sha12, mine)
        self.assertIn("子页面正文（移动之后编辑）", [plain(b) for b in w.fake.children(w.child)])
        self.assertFalse(w.fake.page(w.doomed)["in_trash"], "a local move never trashes or moves the page")
        self.assertEqual((w.fake.page(w.doomed)["parent"].get("page_id") or "").replace("-", ""),
                         w.page.replace("-", ""), "nor moves it in Notion")

    def test_rejected_rename_registered_and_children_of_a_held_folder_note_refused_at_plan(self):
        """Fix loop 1 (W3.5 L17 review). (1) A same-folder rename the exporter rejects ('%' -> unsafe_name): the
        refresh opens no override and holds the object (override_rejected, paths []); that hold registers it, so
        the unrelated edit is executable after the round and every later round (before: blocked for good, with a
        'wait for the sync' hint that never came true) and its apply passes W1-W10; the renamed note says to
        rename it again. (2) A folder-note rename held by the refresh with its subtree: the child's edit is
        refused AUTO_SYNC_HOLD at plan time (before: listed executable, apply aborted W10 fail, 0 writes)."""
        w = World(self, "l17rej")
        rc, out, err = w.main("plan")
        self.assertEqual(rc, 0, out + err)
        bad = "General/写回测试页/待删页面 50%.md"
        os.rename(w.path(DOOMED), w.path(bad))
        w.write(MAIN, w.read(MAIN).replace(f"[[{DOOMED[:-3]}|", f"[[{bad[:-3]}|"))
        w.edit(CHILD, "子页面正文", "子页面正文（拒绝改名之后）")
        self.blocked_plan(w, "renamed_same_folder")
        for rnd in ("R2", "R3"):
            rc, st = w.refresh(rnd)
            self.assertEqual([r.get("reason") for r in (st.get("overrides") or {}).get("rejected") or []],
                             ["unsafe_name"], st.get("overrides"))
            self.assertEqual(w.q("SELECT count(*) n FROM name_overrides WHERE status='open'")[0]["n"], 0)
            hold = refresh.load_holds(w.P).get(w.doomed) or {}
            self.assertEqual((hold.get("reason"), hold.get("source"), hold.get("paths")),
                             ("override_rejected:unsafe_name", "export", []), hold)
            pid, sha12, mine = self.executable_plan(w, CHILD)
            rc, out, err = w.main("status")
            self.assertIn(f"（{wb_plan.OVERRIDE_REJECTED_HINT}）", out)   # what to do instead of waiting
        plan, _ = w.plan_obj(pid)
        ref = [r for r in plan["refused"] if r["rel_path"] == bad]
        self.assertEqual([r["class"] for r in ref], ["AUTO_SYNC_HOLD"], plan["refused"])
        self.assertEqual(ref[0]["hint"], wb_plan.OVERRIDE_REJECTED_HINT)
        self.assertEqual([it["category"] for it in mine], ["M"])
        self.applied(w, pid, sha12, mine)
        self.assertIn("子页面正文（拒绝改名之后）", [plain(b) for b in w.fake.children(w.child)])
        self.assertEqual(w.title(w.doomed), "待删页面", "a rejected local name never renames the page")
        # (2) the folder note renamed (unsupported), its child edited; the refresh holds the folder note + subtree
        main_new = "General/写回测试页/写回测试页改.md"
        os.rename(w.path(MAIN), w.path(main_new))
        w.edit(CHILD, "子页面正文（拒绝改名之后）", "子页面正文（folder note 改名之后）")
        self.blocked_plan(w, "unsupported_rename")
        rc, st = w.refresh("R4")
        hold = refresh.load_holds(w.P).get(w.page) or {}
        self.assertEqual((hold.get("reason"), hold.get("subtree")), ("unsupported_rename", True), hold)
        rc, out, err = w.main("status")
        self.assertNotIn("UNREGISTERED_RENAME", out)
        pid, sha12, out = w.make_plan()
        plan, _ = w.plan_obj(pid)
        self.assertEqual(plan["unregistered_renames"], [])
        self.assertFalse([it for it in plan["items"] if it.get("rel_path") == CHILD], plan["items"])
        ref = [r for r in plan["refused"] if r["rel_path"] == CHILD]
        self.assertEqual([r["class"] for r in ref], ["AUTO_SYNC_HOLD"], plan["refused"])
        self.assertIn(f"上级对象 {w.page.replace('-', '')[:8]} 的自动同步已暂停（含子树：unsupported_rename）",
                      ref[0]["reason"])
        self.assertEqual(plan["summary"]["executable"], 0)
        w.fake.clear_requests()
        child = w.spawn(*w.apply_args(pid, sha12, "--all-safe"))
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, child.before)
        self.assertNotIn("请原样输入", child.before)
        self.assertEqual(w.writes(), [])

    def test_move_of_a_note_with_a_manual_hold_is_registered(self):
        """Fix loop 1 (W3.5 L17 review): a note with a manual-release hold (refresh.py record_post_verify shape:
        source post_verify, auto_release False, paths []) moved to another folder. The refresh keeps that hold and
        never adds the move's paths (refresh.py skips the scan hold), so 'the hold must list the new path' blocked
        every plan until someone released the hold. The hold (with its subtree) registers the move: the unrelated
        edit is executable at once and after a refresh round, and its apply passes W1-W10."""
        w = World(self, "l17man")
        rc, out, err = w.main("plan")
        self.assertEqual(rc, 0, out + err)
        holds = refresh.load_holds(w.P)
        holds[w.doomed] = {"reason": "post_verify_failed:C04", "run_id": "R1", "since": wb_common.now_iso(),
                           "subtree": True, "source": "post_verify", "auto_release": False, "paths": []}
        refresh.save_holds(w.P, holds)
        os.rename(w.path(DOOMED), w.path(self.MOVED))
        w.edit(CHILD, "子页面正文", "子页面正文（人工暂停后移动）")
        pid, sha12, mine = self.executable_plan(w, CHILD)
        self.applied(w, pid, sha12, mine)
        self.assertIn("子页面正文（人工暂停后移动）", [plain(b) for b in w.fake.children(w.child)])
        self.assertFalse(w.fake.page(w.doomed)["in_trash"])
        rc, st = w.refresh("R2")
        hold = refresh.load_holds(w.P).get(w.doomed) or {}
        self.assertEqual((hold.get("source"), hold.get("paths")), ("post_verify", []), hold)
        w.edit(CHILD, "子页面正文（人工暂停后移动）", "子页面正文（刷新之后）")
        self.executable_plan(w, CHILD)



    def test_rename_below_a_held_ancestor_is_registered_fix2(self):
        """Fix loop 2: a grandchild renamed in Obsidian (link rewritten in its parent folder note) below a page held
        WITH its subtree (manual post_verify hold). The publisher leaves the whole held subtree alone, so the scan
        lists that rename after every published round too (open override, published row at the old name): with
        'an open override is not enough' alone, every plan would stay blocked until someone released the hold. The
        ancestor hold registers it — at once and after a round: the unrelated edit is executable and its apply
        passes W1-W10 (the adoption publishes and verifies with that hold)."""
        w = World(self, "l17anc")
        gc = w.fake.add_page("孙页面", parent=w.doomed, children=[blk("paragraph", rt("孙页面正文"))])
        w.write_cfg(whitelist_page_ids=list(w.cfg["whitelist_page_ids"]) + [gc])
        rc, st = w.refresh("R1b")
        self.assertEqual(st.get("result"), "published", st)
        rc, out, err = w.main("plan")
        self.assertEqual(rc, 0, out + err)
        dfolder, gold = "General/写回测试页/待删页面/待删页面.md", "General/写回测试页/待删页面/孙页面.md"
        gnew = "General/写回测试页/待删页面/孙页面改名.md"
        self.assertEqual(w.published(gold)[1], gc)
        holds = refresh.load_holds(w.P)
        holds[w.doomed] = {"reason": "post_verify_failed:C04", "run_id": "R1b", "since": wb_common.now_iso(),
                           "subtree": True, "source": "post_verify", "auto_release": False, "paths": []}
        refresh.save_holds(w.P, holds)
        os.rename(w.path(gold), w.path(gnew))
        w.write(dfolder, w.read(dfolder).replace(f"[[{gold[:-3]}|", f"[[{gnew[:-3]}|"))
        w.edit(CHILD, "子页面正文", "子页面正文（祖先暂停时孙页面改名）")
        pid, sha12, mine = self.executable_plan(w, CHILD)
        plan, _ = w.plan_obj(pid)
        self.assertEqual([r["class"] for r in plan["refused"] if r["rel_path"] == gnew], ["AUTO_SYNC_HOLD"])
        self.applied(w, pid, sha12, mine)
        self.assertIn("子页面正文（祖先暂停时孙页面改名）", [plain(b) for b in w.fake.children(w.child)])
        self.assertEqual(w.title(gc), "孙页面", "a held object is never renamed")
        rc, st = w.refresh("R2")
        self.assertEqual(st.get("result"), "published_with_holds", st.get("pause_reasons"))
        self.assertEqual(w.published(gold)[2], "active", "the publisher left the held subtree alone")
        w.edit(CHILD, "子页面正文（祖先暂停时孙页面改名）", "子页面正文（刷新之后）")
        self.executable_plan(w, CHILD)

    def test_paused_round_does_not_register_the_rename_fix2(self):
        """Fix loop 2 (W3.5 L17 review, requirement lens): the common Obsidian flow 'rename a child note (Obsidian
        rewrites the link in the parent folder note) and edit the parent'. A concurrent remote parent edit gives
        a real two-sided conflict; a rendered link spelling alone must not pause publication. Each refresh opens
        the name override and pauses (conflict_on_page_with_children) for review: it publishes nothing,
        published_files and the sortspec keep the old name. Before fix loop 2 that open override counted as
        registered: status dropped the notice, the unrelated edit was executable and its apply ended
        partially_applied (W6 C05). Now every item stays blocked (UNREGISTERED_RENAME), apply is refused with 0
        writes, round after round; once the paused round is approved (published) the edit is executable and its
        apply passes W1-W10."""
        w = World(self, "l17pause")
        rc, out, err = w.main("plan")
        self.assertEqual(rc, 0, out + err)
        os.rename(w.path(CHILD), w.path(CHILD_NEW))
        w.write(MAIN, w.read(MAIN).replace(f"[[{CHILD[:-3]}|", f"[[{CHILD_NEW[:-3]}|"))
        w.edit(MAIN, "第二段：将被修改。", "第二段：改名之后编辑父页面。")
        w.edit(DOOMED, "待删页面正文", "待删页面正文（父页面暂停时编辑）")
        w.fake.add_blocks(w.page, [blk("paragraph", rt("另一位编辑者同时在 Notion 父页补充了正文。"))])
        for rnd in ("R2", "R3"):
            rc, st = w.refresh(rnd)
            self.assertEqual(st.get("result"), "paused_for_review", st.get("pause_reasons"))
            self.assertTrue([r for r in st.get("pause_reasons") or [] if r.startswith("conflict_on_page_with_children")],
                            st.get("pause_reasons"))
            self.assertEqual([(o["local_rel"], o["status"]) for o in w.q("SELECT local_rel, status FROM name_overrides")],
                             [(CHILD_NEW, "open")])
            self.assertEqual(w.published(CHILD)[2], "active", "nothing published: the old name stays")
            self.assertIsNone(w.published(CHILD_NEW))
            pid, sha12, plan = self.blocked_plan(w, "renamed_same_folder")
            self.assertTrue(plan["unregistered_renames"][0].get("override_open"), plan["unregistered_renames"])
            rc, out, err = w.main("status")
            self.assertIn(wb_plan.OVERRIDE_UNPUBLISHED_TEXT, out)
            m = next(it["item_id"] for it in plan["items"] if it["category"] == "M" and it["rel_path"] == DOOMED)
            self.refused_apply(w, pid, sha12, "--items", m)
            self.refused_apply(w, pid, sha12, "--all-safe")
        self.assertEqual(w.q("SELECT count(*) n FROM journal")[0]["n"], 0)
        self.assertEqual(w.q("SELECT count(*) n FROM confirmations")[0]["n"], 0)
        self.assertNotIn("待删页面正文（父页面暂停时编辑）", [plain(b) for b in w.fake.children(w.doomed)])
        # a person approves the paused round (refresh.py --approve-run R3): it publishes the local name
        import wb_gate
        with mock.patch.object(wb_gate, "require_human", return_value="/dev/pts/t"), \
                mock.patch.object(wb_gate, "confirm_yes_no", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(refresh.approve_run(w.P, "R3"), 0)
        self.assertEqual(w.published(CHILD_NEW)[2], "active")
        pid, sha12, mine = self.executable_plan(w, DOOMED)
        self.assertEqual([it["category"] for it in mine], ["M"])
        self.applied(w, pid, sha12, mine)
        self.assertIn("待删页面正文（父页面暂停时编辑）", [plain(b) for b in w.fake.children(w.doomed)])


@unittest.skipIf(pexpect is None, "pexpect not installed")
class TestHoldParityWave4E2E(unittest.TestCase):
    """Wave 4 prep end to end (real refresh rounds, real CLI on the pty, real executor + adoption on the fake).
    L-20a (i): a local rename plus an Obsidian Sync conflicted copy named after the OLD name. The refresh holds the
    note note-only (sync_conflict_copy) with paths = the old name; that own hold registers the rename, so the
    unrelated edit is executable round after round and its apply passes W1-W10 (before: every plan blocked
    UNREGISTERED_RENAME for as long as the copy existed). L-23 (a): once the copy is deleted the hold stays until the
    next refresh; the renamed note's T item is refused at plan time with the executor's words (before: listed
    executable, the apply skipped it). The next refresh releases the hold, publishes the rename, and T applies."""

    COPY = "General/写回测试页/子页面 (Conflicted copy 2026-11-20 1200).md"
    H = wb_plan.UNREGISTERED_RENAME_HINT          # used by the shared TestUnregisteredRenameL17 helpers

    def test_rename_plus_sync_copy_named_after_the_old_name(self):
        w = World(self, "w4sync")
        rc, out, err = w.main("plan")                       # enabling on the clean vault
        self.assertEqual(rc, 0, out + err)
        child_text = w.read(CHILD)
        os.rename(w.path(CHILD), w.path(CHILD_NEW))         # renamed in Obsidian (links updated by Obsidian) ...
        w.write(MAIN, w.read(MAIN).replace(f"[[{CHILD[:-3]}|", f"[[{CHILD_NEW[:-3]}|"))
        w.write(self.COPY, child_text.replace("子页面正文", "子页面正文（另一台设备）"))   # ... Sync copy, OLD name
        w.edit(DOOMED, "待删页面正文", "待删页面正文（Sync 副本存在时编辑）")
        L17 = TestUnregisteredRenameL17
        L17.blocked_plan(self, w, "renamed_same_folder")    # no refresh round yet: unregistered
        for rnd in ("R2", "R3"):
            rc, st = w.refresh(rnd)
            self.assertIn(st.get("result"), ("published", "published_with_holds"), st.get("pause_reasons"))
            hold = refresh.load_holds(w.P).get(w.child) or {}
            self.assertEqual((hold.get("reason"), hold.get("subtree"), hold.get("paths")),
                             ("sync_conflict_copy", False, [CHILD]), hold)
            self.assertEqual(w.published(CHILD)[2], "active", "the held note's rename is not published")
            pid, sha12, mine = L17.executable_plan(self, w, DOOMED)
            plan, _ = w.plan_obj(pid)
            self.assertFalse([it for it in plan["items"] if it.get("notion_id") == w.child], plan["items"])
            self.assertTrue([r for r in plan["refused"] if r["class"] == "SYNC_CONFLICT_COPY"], plan["refused"])
        self.assertEqual([it["category"] for it in mine], ["M"])
        L17.applied(self, w, pid, sha12, mine)
        self.assertIn("待删页面正文（Sync 副本存在时编辑）", [plain(b) for b in w.fake.children(w.doomed)])
        self.assertEqual(w.title(w.child), "子页面", "a held note is never renamed")
        # L-23 (a): the copy is merged / deleted; the hold stays until the next refresh -> T refused at plan time
        os.remove(w.path(self.COPY))
        w.edit(DOOMED, "待删页面正文（Sync 副本存在时编辑）", "待删页面正文（副本删除之后）")
        pid, sha12, mine = L17.executable_plan(self, w, DOOMED)
        plan, _ = w.plan_obj(pid)
        self.assertEqual([it for it in plan["items"] if it.get("notion_id") == w.child], [], plan["items"])
        ref = [r for r in plan["refused"] if r["rel_path"] == CHILD_NEW]
        self.assertEqual([r["class"] for r in ref], ["AUTO_SYNC_HOLD"], plan["refused"])
        self.assertIn("自动同步已暂停（sync_conflict_copy，scan）", ref[0]["reason"])
        self.assertIn(f"refresh.py --release-hold {w.child}", ref[0]["hint"])
        self.assertIn(f"refresh.py --release-hold {w.child}", w.read(f"Notion_Obsidian/写回预览/{pid}.md"))
        L17.applied(self, w, pid, sha12, mine)
        self.assertEqual(w.title(w.child), "子页面")
        # the next refresh releases the scan hold (copy gone) and publishes the local name; T is executable
        rc, st = w.refresh("R4")
        self.assertIn(st.get("result"), ("published", "published_with_holds"), st.get("pause_reasons"))
        self.assertNotIn(w.child, refresh.load_holds(w.P))
        self.assertEqual(w.published(CHILD_NEW)[2], "active")
        pid, sha12, mine = L17.executable_plan(self, w, CHILD_NEW)
        self.assertEqual([it["category"] for it in mine], ["T"])
        L17.applied(self, w, pid, sha12, mine)
        self.assertEqual(w.title(w.child), "子页面改名")


class TestW4FixRound1E2E(unittest.TestCase):
    """Wave-4 fix round 1 end to end (real refresh rounds, real CLI on the pty, real executor + adoption, fake Notion):
    r1-data-safety-02 (a BOM note keeps its frontmatter), r1-contract-consistency-01 (an unregistered local delete
    does not spoil an unrelated apply), r1-spec-coverage-01 / r1-user-journey-06 (a deletion is verified at the next
    plan after the next refresh), r1-user-journey-01 (the trashed whitelisted page does not block later applies),
    r1-contract-consistency-03 (after a new page is adopted, its parent is plannable before the next refresh)."""

    def applied(self, w, pid, sha12, sel):
        w.fake.clear_requests()
        rc, out, phrase, shown = w.gated(*w.apply_args(pid, sha12, *sel))
        res = w.result(pid)
        st = {k: v["status"] for k, v in res["w_checks"].items()}
        self.assertEqual(res["status"], "applied", json.dumps(res["w_checks"], ensure_ascii=False) + out[-1500:])
        self.assertEqual(rc, 0, out[-1500:])
        self.assertFalse([k for k, v in st.items() if v not in ("pass", "n/a")], st)
        assert_writes_confirmed(self, w, pid, w.writes())
        return res, out

    def plan_items(self, w, *rels):
        pid, sha12, out = w.make_plan()
        plan, _ = w.plan_obj(pid)
        mine = [it for it in plan["items"] if it.get("rel_path") in rels]
        return pid, sha12, out, plan, mine

    def test_bom_new_note_keeps_its_frontmatter(self):
        w = World(self, "w4bom")
        rc, out, err = w.main("plan")
        self.assertEqual(rc, 0, out + err)
        with open(w.path(NEW_NOTE), "wb") as fh:
            fh.write("﻿---\ntags: [project]\naliases: [BN]\n---\n新笔记正文。\n".encode("utf-8"))
        pid, sha12, out, plan, mine = self.plan_items(w, NEW_NOTE)
        self.assertEqual([(it["category"], it["executable"]) for it in mine], [("N", True)], plan["refused"])
        self.applied(w, pid, sha12, ["--items", mine[0]["item_id"]])
        with open(w.path(NEW_NOTE), "rb") as fh:
            raw = fh.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        fm, body, _ = wb_common.split_note(raw.decode("utf-8"))
        meta = wb_common.parse_frontmatter(fm)
        self.assertTrue(meta.get("notion_id"))
        self.assertEqual(meta.get("tags"), ["project"])
        self.assertEqual(meta.get("aliases"), ["BN"])
        self.assertNotIn("tags:", body)

    def test_unregistered_local_delete_does_not_spoil_an_unrelated_apply(self):
        w = World(self, "w4udel")
        rc, out, err = w.main("plan")
        self.assertEqual(rc, 0, out + err)
        os.remove(w.path(DOOMED))                           # no refresh: not registered (no pending_deletes row)
        w.edit(CHILD, "子页面正文", "子页面正文（W4 编辑）")
        pid, sha12, out, plan, mine = self.plan_items(w, CHILD)
        self.assertEqual([(it["category"], it["executable"]) for it in mine], [("M", True)])
        self.applied(w, pid, sha12, ["--items", mine[0]["item_id"]])
        self.assertFalse(os.path.exists(w.path(DOOMED)), "the adoption must not re-add a locally deleted note")
        self.assertEqual(w.q("SELECT * FROM pending_deletes"), [])
        rc, st = w.refresh("R2")                            # the next refresh registers the delete as usual
        self.assertEqual([r["status"] for r in w.q("SELECT status FROM pending_deletes WHERE notion_id=?",
                                                   (w.doomed,))], ["hold"])

    def test_delete_verified_next_round_and_later_applies_not_blocked(self):
        w = World(self, "w4del")
        rc, out, err = w.main("plan")
        self.assertEqual(rc, 0, out + err)
        os.remove(w.path(DOOMED))
        rc, st = w.refresh("R2")
        pid, sha12, out, plan, mine = self.plan_items(w, DOOMED)
        self.assertEqual([(it["category"], it["executable"]) for it in mine], [("D", True)], out)
        self.assertIn(f"--delete {mine[0]['item_id']}", out)
        self.applied(w, pid, sha12, ["--delete", mine[0]["item_id"]])
        self.assertTrue(w.fake.page(w.doomed)["in_trash"])

        def vs():
            return w.q("SELECT verify_status FROM plan_items WHERE plan_id=? AND item_id=?",
                       (pid, mine[0]["item_id"]))[0]["verify_status"]
        self.assertEqual(vs(), "pending_next_round")
        rc, st = w.refresh("R3")
        self.assertIn(st.get("result"), ("published", "published_with_holds"), st)
        w.edit(CHILD, "子页面正文", "子页面正文（删除之后）")
        pid2, sha2, out2, plan2, mine2 = self.plan_items(w, CHILD)
        self.assertEqual(vs(), "verified", out2)
        self.assertIn("已核验（verified）", out2)
        rc, sout, err = w.main("status")
        self.assertIn("已核验（verified）", sout)
        # the whitelisted page this tool trashed is not "unreachable": the next apply is not refused by audit-scope
        self.assertEqual([(it["category"], it["executable"]) for it in mine2], [("M", True)])
        self.applied(w, pid2, sha2, ["--items", mine2[0]["item_id"]])
        self.assertEqual(w.q("SELECT value FROM wb_meta WHERE key='forced_mode' AND value IS NOT NULL"), [])

    def test_new_page_adopted_then_parent_and_new_note_plannable(self):
        w = World(self, "w4new")
        rc, out, err = w.main("plan")
        self.assertEqual(rc, 0, out + err)
        w.write(NEW_NOTE, "新笔记正文。\n")
        pid, sha12, out, plan, mine = self.plan_items(w, NEW_NOTE)
        self.assertEqual([(it["category"], it["executable"]) for it in mine], [("N", True)])
        self.applied(w, pid, sha12, ["--items", mine[0]["item_id"]])
        w.edit(MAIN, "第二段：将被修改。", "第二段：采纳新页面后在父页面修改。")
        w.edit(NEW_NOTE, "新笔记正文。", "新笔记正文（再改）。")
        pid2, sha2, out2, plan2, mine2 = self.plan_items(w, MAIN, NEW_NOTE)
        refused = {r["rel_path"]: r["class"] for r in plan2["refused"]}
        self.assertNotEqual(refused.get(MAIN), "ORACLE_MISMATCH", plan2["refused"])
        self.assertNotEqual(refused.get(NEW_NOTE), "NOT_EXPORTED", plan2["refused"])
        main_items = [it for it in mine2 if it["rel_path"] == MAIN]
        self.assertEqual([(it["category"], it["executable"]) for it in main_items], [("M", True)], plan2["refused"])
