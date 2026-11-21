#!/usr/bin/env python3
"""Tests for tools/wb_apply.py (I2 executor; WRITEBACK_SPEC §11–§13, IMPL_CONTRACT §5.13.1).

Every test runs against tools/tests/fake_notion.py on 127.0.0.1 with fake tokens; WORK, vault, writeback.db,
rate-gate state and token files live in tempfile.mkdtemp() dirs that are removed. The confirmed path is driven
through the internal entry points with an injected confirmation record (contract rule 3); every test asserts the
fake's write log (requests made with the write-back token).

Run (from the sandbox root):
  python3 -I -B -m unittest discover -s tools/tests -p 'test_wb_apply.py' -v
"""
import contextlib
import datetime
import io
import json
import logging
import os
import posixpath
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
for _p in (HERE, TOOLS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import md2blocks  # noqa: E402
import wb_apply  # noqa: E402
import wb_common  # noqa: E402
import wb_config  # noqa: E402
import wb_norm  # noqa: E402
import wb_state  # noqa: E402
from fake_notion import FakeNotion  # noqa: E402
from rate_gate import RateGate  # noqa: E402
from readonly_client import make_read_client  # noqa: E402
from writeback_client import WritebackClient  # noqa: E402

QUIET = ("nmig.client", "writeback_client", "wb_refetch", "nmig.inventory", "snapshot")
_saved = {}


def setUpModule():
    for name in QUIET:
        lg = logging.getLogger(name)
        _saved[name] = lg.level
        lg.setLevel(logging.CRITICAL)


def tearDownModule():
    for name, lvl in _saved.items():
        logging.getLogger(name).setLevel(lvl)


class _Crash(BaseException):
    """Simulated process death (not an Exception: nothing in wb_apply may swallow it)."""


def rt(text):
    return [{"type": "text", "text": {"content": text}}]


def para(text):
    return {"type": "paragraph", "paragraph": {"rich_text": rt(text)}}


def req_para(text):
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rt(text)}}


def plain(b):
    t = b["type"]
    return "".join(x.get("plain_text", "") for x in b[t].get("rich_text", []))


def ok_adopter(ctx, written, **kw):
    return {"ok": True, "needs_manual": [], "W6": {"status": "pass", "detail": "stub"},
            "W7": {"status": "pass", "detail": "stub"}, "W8": {"status": "pass", "detail": "stub"}}


# =============================================================================================== world
class World:
    """Fake Notion with N whitelisted test pages + temp WORK / vault / writeback.db + clients."""

    def __init__(self, n_pages=1, allow_block_trash=False, timeout=(2.0, 2.0), cfg=None, blocks_per_page=3):
        self.tmp = tempfile.mkdtemp(prefix="wbapply-")
        self.work = os.path.join(self.tmp, "work")
        self.state = os.path.join(self.work, "state")
        os.makedirs(self.state)
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(os.path.join(self.vault, "General"))
        self.fake = FakeNotion(tokens={
            "ro": {"caps": {"read", "user_info"}, "roots": {"*"}, "secret": "secret-ro-5d1e"},
            "wb": {"caps": {"read", "update", "insert"}, "roots": set(), "secret": "secret-wb-8c2f"},
            "admin": {"caps": {"all"}, "roots": {"*"}, "secret": "secret-admin-77aa"}})
        self.fake.add_user("Alice")
        self.base = self.fake.start()
        self.pages, self.rels = [], []
        for i in range(n_pages):
            pid = self.fake.add_page(f"Page {i}", children=[para(f"p{i} line {k}") for k in range(blocks_per_page)])
            self.fake.grant("wb", pid)
            self.pages.append(pid)
            rel = f"General/Page {i}.md"
            self.rels.append(rel)
            self.write_note(rel, f"---\nnotion_id: {pid}\nnotion_kind: page\n---\n# Page {i}\n\nbody {i}\n")
        self.cfg = wb_config.defaults()
        self.cfg.update(mode="whitelist", read_integration_readonly_confirmed_at="2026-11-17T18:30+07:00",
                        whitelist_page_ids=list(self.pages), allow_block_trash=allow_block_trash,
                        rate_limit_total_rps=1000)
        self.cfg.update(cfg or {})
        self.db = wb_state.connect(os.path.join(self.state, "writeback.db"))
        self.gate = RateGate(os.path.join(self.tmp, "rate_gate.json"), total_rps=1000, burst=1000)
        self.wb = WritebackClient(self.fake.secret("wb"), base_url=self.base, gate=self.gate, timeout=timeout,
                                  silent=True, allow_block_trash=allow_block_trash)
        self.rc = make_read_client(base_url=self.base, token=self.fake.secret("ro"), rate=None, gate=self.gate,
                                   sleep=lambda s: None, max_retries=2)
        self.admin = WritebackClient(self.fake.secret("admin"), base_url=self.base, rate=1000, silent=False,
                                     allow_block_trash=True)

    def close(self):
        try:
            self.db.close()
        finally:
            self.fake.stop()
            shutil.rmtree(self.tmp, ignore_errors=True)

    # -------------------------------------------------- vault / notion helpers
    def write_note(self, rel, text):
        p = os.path.join(self.vault, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(text.encode("utf-8"))
        return wb_common.sha256_bytes(text.encode("utf-8"))

    def vsha(self, rel):
        return wb_common.sha256_file(os.path.join(self.vault, *rel.split("/")))

    def kids(self, pid):
        return self.rc.list_children(pid)

    def wb_writes(self):
        return [r for r in self.fake.writes() if r["token"] == "wb"]

    def wb_requests(self):
        return [r for r in self.fake.requests if r["token"] == "wb"]

    # -------------------------------------------------- plan items
    def op_update(self, pid, idx, text):
        b = self.kids(pid)[idx]
        return {"op": "UPDATE", "target_id": b["id"], "before_norm_sha": wb_norm.block_json_sha(b),
                "request": {"paragraph": {"rich_text": rt(text)}}}

    def op_append(self, pid, after_idx, texts, parent=None):
        kids = self.kids(parent or pid)
        pos = {"type": "after_block", "after_block": {"id": kids[after_idx]["id"]}} if after_idx is not None else \
            {"type": "start"}
        nxt = kids[after_idx + 1]["id"] if after_idx is not None and after_idx + 1 < len(kids) else \
            (kids[0]["id"] if after_idx is None and kids else None)
        return {"op": "APPEND", "parent_id": parent or pid, "position": pos, "children": [req_para(t) for t in texts],
                "anchor_next_id": nxt, "new_blocks": len(texts)}

    def op_trash_block(self, pid, idx):
        b = self.kids(pid)[idx]
        return {"op": "TRASH_BLOCK", "target_id": b["id"], "before_norm_sha": wb_norm.block_json_sha(b)}

    def item(self, iid, page_idx, ops, cat="M", binds=None, **kw):
        it = {"item_id": iid, "category": cat, "notion_id": self.pages[page_idx], "rel_path": self.rels[page_idx],
              "l_sha": self.vsha(self.rels[page_idx]), "risk": "normal", "binds": binds or [], "ops": ops}
        it.update(kw)
        return it

    def plan(self, items, *, select=None, confirm=None, status="confirmed", expires_h=24, extra=None, plan_id=None,
             scope="apply"):
        plan_id = plan_id or f"wb-t-{os.urandom(3).hex()}"
        now = datetime.datetime.now().astimezone()
        plan = {"plan_id": plan_id, "created_at": (now - datetime.timedelta(minutes=2)).isoformat(timespec="seconds"),
                "expires_at": (now + datetime.timedelta(hours=expires_h)).isoformat(timespec="seconds"),
                "mode": "whitelist", "scope": "白名单", "members": 1, "creation_state": "unlimited",
                "breaker": {"tripped": False}, "items": items, "refused": [], "rejected": [],
                "counts": {c: sum(1 for i in items if i["category"] == c) for c in ("N", "M", "B", "D", "T")}}
        plan.update(extra or {})
        d = os.path.join(self.work, "reports", "writeback", plan_id)
        os.makedirs(d, exist_ok=True)
        raw = json.dumps(plan, ensure_ascii=False, indent=1).encode("utf-8")
        with open(os.path.join(d, "plan.json"), "wb") as fh:
            fh.write(raw)
        sha = wb_common.sha256_bytes(raw)
        with wb_state.transaction(self.db):
            self.db.execute("INSERT INTO plans(plan_id, created_at, expires_at, plan_sha256, status, scope, counts_json) "
                            "VALUES(?,?,?,?,?,?,?)", (plan_id, plan["created_at"], plan["expires_at"], sha, status,
                                                       "白名单", json.dumps(plan["counts"])))
            for it in items:
                self.db.execute("INSERT INTO plan_items(plan_id, item_id, notion_id, rel_path, category, ops_json, risk, "
                                "decision, l_sha, binds) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (plan_id, it["item_id"], it.get("notion_id"), it.get("rel_path"), it["category"],
                                 json.dumps(it["ops"]), "normal", "pending", it.get("l_sha"), json.dumps(it.get("binds"))))
        ids = [i["item_id"] for i in items] if select is None else select
        sel = self.selection(ids)
        self.confirm(plan_id, sha, ids if confirm is None else confirm, scope)
        rec = {"plan_id": plan_id, "plan_sha256": sha, "os_user": "tester", "tty": "/dev/pts/9",
               "confirmed_at": wb_common.now_iso(), "selection": sel, "challenge_ok": True, "phrase_scope": scope}
        return plan_id, sha, sel, rec

    @staticmethod
    def selection(ids):
        sel = {"items": [], "delete": [], "delete_blocks": [], "rename": [], "move": [], "resolve": []}
        for i in ids:
            pre = i.split("-")[0]
            sel[{"M": "items", "N": "items", "D": "delete", "B": "delete_blocks", "T": "rename", "MV": "move",
                 "C": "resolve"}[pre]].append(i)
        return sel

    def confirm(self, plan_id, sha, ids, scope="apply"):
        with wb_state.transaction(self.db):
            for i in ids:
                self.db.execute("INSERT INTO confirmations(plan_id, item_id, confirmed_at, os_user, tty, challenge_ok, "
                                "phrase_scope, plan_sha256) VALUES(?,?,?,?,?,?,?,?)",
                                (plan_id, i, wb_common.now_iso(), "tester", "/dev/pts/9", 1, scope, sha))

    def record(self, plan_id, sha, scope, sel=None):
        return {"plan_id": plan_id, "plan_sha256": sha, "os_user": "tester", "tty": "/dev/pts/9",
                "confirmed_at": wb_common.now_iso(), "selection": sel, "challenge_ok": True, "phrase_scope": scope}

    def run(self, plan_id, sel, rec, **kw):
        kw.setdefault("adopter", ok_adopter)
        return wb_apply.execute_confirmed_plan(plan_id, sel, rec, client=self.wb, read_client=self.rc, cfg=self.cfg,
                                               db=self.db, work=self.work, vault=self.vault, **kw)

    def resume(self, plan_id, rec, **kw):
        kw.setdefault("adopter", ok_adopter)
        return wb_apply.resume_plan(plan_id, rec, client=self.wb, read_client=self.rc, cfg=self.cfg, db=self.db,
                                    work=self.work, vault=self.vault, **kw)

    def plan_status(self, plan_id):
        return self.db.execute("SELECT status FROM plans WHERE plan_id=?", (plan_id,)).fetchone()[0]

    def journal(self, plan_id):
        return [dict(r) for r in self.db.execute("SELECT * FROM journal WHERE plan_id=? ORDER BY seq", (plan_id,))]

    def page_state(self, pid):
        r = self.db.execute("SELECT state FROM page_state WHERE notion_id=?", (pid,)).fetchone()
        return r[0] if r else None


class WorldCase(unittest.TestCase):
    N_PAGES = 1
    ALLOW_TRASH = False

    def setUp(self):
        self.w = World(self.N_PAGES, allow_block_trash=self.ALLOW_TRASH)

    def tearDown(self):
        self.w.close()

    def assertNoWrites(self):
        self.assertEqual(self.w.wb_writes(), [], [(r["method"], r["path"]) for r in self.w.wb_writes()])


# =============================================================================================== happy path
class HappyPathTests(WorldCase):
    def test_update_and_append_applied_with_journal_and_outputs(self):
        w = self.w
        pid = w.pages[0]
        it = w.item("M-001", 0, [w.op_update(pid, 1, "p0 line 1 EDITED"), w.op_append(pid, 1, ["new A", "new B"])])
        P, sha, sel, rec = w.plan([it])
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res)
        self.assertEqual(res["executed"], ["M-001"])
        writes = w.wb_writes()
        self.assertEqual([(r["method"], r["path"].split("/")[0]) for r in writes], [("PATCH", "blocks"), ("PATCH", "blocks")])
        self.assertTrue(writes[1]["path"].endswith("/children"))
        texts = [plain(b) for b in w.kids(pid)]
        self.assertEqual(texts, ["p0 line 0", "p0 line 1 EDITED", "new A", "new B", "p0 line 2"])
        rows = w.journal(P)
        self.assertEqual([r["state"] for r in rows], ["done", "done"])
        self.assertTrue(all(r["after_json"] for r in rows))
        for k, v in res["w_checks"].items():
            self.assertIn(v["status"], ("pass", "n/a"), (k, v))
        self.assertEqual(w.plan_status(P), "applied")
        # outputs
        self.assertTrue(os.path.isfile(res["result_md"]))
        vr = os.path.join(w.vault, res["vault_record"])
        with open(vr, encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nnotion_kind: generated\ngenerated_by: writeback.py\nplan_id: "))
        self.assertNotIn("notion_id:", text)
        self.assertNotIn("[[", text)
        self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))
        self.assertNotIn("\r", text)
        # block ledger: 2 new blocks
        self.assertEqual(w.db.execute("SELECT SUM(created_blocks) FROM block_budget WHERE plan_id=?", (P,)).fetchone()[0], 2)
        # every write was preceded by an intent row and a 'sent' mark
        self.assertEqual(res["requests"], 2)

    def test_unselected_item_never_sent(self):
        w = self.w
        pid = w.pages[0]
        a = w.item("M-001", 0, [w.op_update(pid, 0, "A")])
        b = w.item("M-002", 0, [w.op_update(pid, 2, "B")])
        P, sha, sel, rec = w.plan([a, b], select=["M-001"])
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(res["executed"], ["M-001"])
        self.assertEqual(len(w.wb_writes()), 1)
        self.assertEqual([plain(b) for b in w.kids(pid)], ["A", "p0 line 1", "p0 line 2"])
        self.assertTrue(all(r["item_id"] == "M-001" for r in w.journal(P)))

    def test_rename_closes_override(self):
        w = self.w
        pid = w.pages[0]
        with wb_state.transaction(w.db):
            w.db.execute("INSERT INTO name_overrides(notion_id, old_rel, local_rel, since, status) VALUES(?,?,?,?,?)",
                         (pid, "General/Page 0.md", "General/Page 0.md", "x", "open"))
        it = w.item("T-001", 0, [{"op": "RENAME", "old_title": rt("Page 0"), "new_title": "Page Zero"}], cat="T")
        P, sha, sel, rec = w.plan([it])
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res)
        self.assertEqual(w.fake.page(pid)["properties"]["title"]["title"][0]["plain_text"], "Page Zero")
        self.assertEqual([(r["method"], r["path"]) for r in w.wb_writes()], [("PATCH", f"pages/{pid}")])
        self.assertIsNone(w.db.execute("SELECT 1 FROM name_overrides WHERE status='open'").fetchone())
        self.assertEqual(res["w_checks"]["W5"]["status"], "pass")

    def test_rename_not_selected_never_changes_title(self):
        w = self.w
        pid = w.pages[0]
        m = w.item("M-001", 0, [w.op_update(pid, 0, "x")])
        t = w.item("T-001", 0, [{"op": "RENAME", "old_title": rt("Page 0"), "new_title": "Page Zero"}], cat="T")
        P, sha, sel, rec = w.plan([m, t], select=["M-001"])
        w.fake.clear_requests()
        w.run(P, sel, rec)
        self.assertEqual(w.fake.page(pid)["properties"]["title"]["title"][0]["plain_text"], "Page 0")
        self.assertFalse(any(r["path"].startswith("pages/") for r in w.wb_writes()))


class DeleteTests(WorldCase):
    N_PAGES = 1

    def _child(self):
        w = self.w
        child = w.fake.add_page("Doomed", parent=w.pages[0], children=[para("doomed body")])
        w.fake.grant("wb", child)
        w.cfg["whitelist_page_ids"] = list(w.pages) + [child]
        with wb_state.transaction(w.db):
            w.db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, detected_run, detected_at, "
                         "status, ttl_until) VALUES(?,?,?,?,?,?,?)", (child, "General/Page 0/Doomed.md", "[]", "r",
                                                                      "x", "hold", "2099-01-01"))
        return child

    def test_confirmed_delete_trashes_page_archives_and_confirms(self):
        w = self.w
        child = self._child()
        it = {"item_id": "D-001", "category": "D", "notion_id": child, "rel_path": "General/Page 0/Doomed.md",
              "l_sha": None, "ops": [{"op": "TRASH_PAGE", "subtree_ids": []}], "binds": []}
        P, sha, sel, rec = w.plan([it])
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res)
        self.assertTrue(w.fake.page(child)["in_trash"])
        self.assertEqual([(r["method"], r["path"]) for r in w.wb_writes()], [("PATCH", f"pages/{child}")])
        self.assertEqual(w.db.execute("SELECT status FROM pending_deletes WHERE notion_id=?", (child,)).fetchone()[0],
                         "confirmed")
        self.assertIsNotNone(w.db.execute("SELECT 1 FROM trash_archive WHERE notion_id=?", (child,)).fetchone())
        self.assertEqual(w.db.execute("SELECT verify_status FROM plan_items WHERE plan_id=? AND item_id='D-001'",
                                      (P,)).fetchone()[0], "pending_next_round")
        self.assertEqual(res["w_checks"]["W4"]["status"], "pass")

    def test_delete_refused_when_note_reappeared_or_not_on_hold(self):
        w = self.w
        child = self._child()
        w.write_note("General/Page 0/Doomed.md", f"---\nnotion_id: {child}\n---\nback\n")
        it = {"item_id": "D-001", "category": "D", "notion_id": child, "rel_path": "General/Page 0/Doomed.md",
              "l_sha": None, "ops": [{"op": "TRASH_PAGE"}], "binds": []}
        P, sha, sel, rec = w.plan([it])
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "aborted")
        self.assertNoWrites()
        self.assertFalse(w.fake.page(child)["in_trash"])

    def test_delete_not_selected_is_never_sent(self):
        w = self.w
        child = self._child()
        m = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "x")])
        d = {"item_id": "D-001", "category": "D", "notion_id": child, "rel_path": "General/Page 0/Doomed.md",
             "l_sha": None, "ops": [{"op": "TRASH_PAGE"}], "binds": []}
        P, sha, sel, rec = w.plan([m, d], select=["M-001"])
        w.fake.clear_requests()
        w.run(P, sel, rec)
        self.assertFalse(w.fake.page(child)["in_trash"])
        self.assertFalse(any(r["path"] == f"pages/{child}" for r in w.wb_writes()))


# =============================================================================================== refusals (0 writes)
class RefusalTests(WorldCase):
    def _plan(self, **kw):
        w = self.w
        it = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "changed")])
        return w.plan([it], **kw)

    def _refused(self, res, status_after=None, P=None):
        self.assertEqual(res["status"], "refused", res)
        self.assertNoWrites()
        self.assertEqual([plain(b) for b in self.w.kids(self.w.pages[0])][0], "p0 line 0")
        if status_after:
            self.assertEqual(self.w.plan_status(P), status_after)

    def test_missing_confirmations_row(self):
        P, sha, sel, rec = self._plan(confirm=[])
        self.w.fake.clear_requests()
        self._refused(self.w.run(P, sel, rec), "confirmed", P)

    def test_wrong_sha_in_record(self):
        P, sha, sel, rec = self._plan()
        rec["plan_sha256"] = "0" * 64
        self.w.fake.clear_requests()
        self._refused(self.w.run(P, sel, rec), "confirmed", P)

    def test_confirmation_rows_for_other_sha(self):
        w = self.w
        P, sha, sel, rec = self._plan(confirm=[])
        w.confirm(P, "f" * 64, ["M-001"])
        w.fake.clear_requests()
        self._refused(w.run(P, sel, rec))

    def test_tampered_plan_file(self):
        w = self.w
        P, sha, sel, rec = self._plan()
        p = os.path.join(w.work, "reports", "writeback", P, "plan.json")
        with open(p, "ab") as fh:
            fh.write(b" ")
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self._refused(res)
        self.assertIn("sha256", res["reason"])

    def test_unselected_item_in_selection_without_confirmation(self):
        w = self.w
        pid = w.pages[0]
        a = w.item("M-001", 0, [w.op_update(pid, 0, "A")])
        b = w.item("M-002", 0, [w.op_update(pid, 1, "B")])
        P, sha, sel, rec = w.plan([a, b], select=["M-001", "M-002"], confirm=["M-001"])
        w.fake.clear_requests()
        self._refused(w.run(P, sel, rec))

    def test_selection_differs_from_record(self):
        w = self.w
        pid = w.pages[0]
        a = w.item("M-001", 0, [w.op_update(pid, 0, "A")])
        b = w.item("M-002", 0, [w.op_update(pid, 1, "B")])
        P, sha, sel, rec = w.plan([a, b], select=["M-001"], confirm=["M-001", "M-002"])
        w.fake.clear_requests()
        self._refused(w.run(P, w.selection(["M-001", "M-002"]), rec))

    def test_rejected_item(self):
        w = self.w
        P, sha, sel, rec = self._plan()
        with wb_state.transaction(w.db):
            w.db.execute("UPDATE plan_items SET decision='rejected' WHERE plan_id=?", (P,))
        w.fake.clear_requests()
        self._refused(w.run(P, sel, rec))

    def test_rejection_row_for_same_l_sha(self):
        w = self.w
        P, sha, sel, rec = self._plan()
        with wb_state.transaction(w.db):
            w.db.execute("INSERT INTO rejections(notion_id, rel_path, l_sha, item_category, reason, created_at, plan_id, "
                         "active) VALUES(?,?,?,?,?,?,?,1)", (w.pages[0], w.rels[0], w.vsha(w.rels[0]), "M", "no", "x", "old"))
        w.fake.clear_requests()
        self._refused(w.run(P, sel, rec))

    def test_expired(self):
        P, sha, sel, rec = self._plan(expires_h=-1)
        self.w.fake.clear_requests()
        self._refused(self.w.run(P, sel, rec), "expired", P)

    def test_superseded_and_draft_and_applied(self):
        for st in ("superseded", "draft", "applied", "aborted"):
            P, sha, sel, rec = self._plan(status=st)
            self.w.fake.clear_requests()
            self._refused(self.w.run(P, sel, rec), st, P)

    def test_mode_plan(self):
        self.w.cfg["mode"] = "plan"
        P, sha, sel, rec = self._plan()
        self.w.fake.clear_requests()
        self._refused(self.w.run(P, sel, rec), "aborted", P)

    def test_read_integration_not_confirmed_caps_mode(self):
        self.w.cfg["read_integration_readonly_confirmed_at"] = None
        P, sha, sel, rec = self._plan()
        self.w.fake.clear_requests()
        self._refused(self.w.run(P, sel, rec), "aborted", P)

    def test_forced_mode_downgrade(self):
        wb_config.record_downgrade(self.w.db, "plan", "test")
        P, sha, sel, rec = self._plan()
        self.w.fake.clear_requests()
        self._refused(self.w.run(P, sel, rec))

    def test_config_file_mode_is_reread(self):
        w = self.w
        os.makedirs(os.path.join(w.work, "config"))
        cfg = dict(w.cfg, mode="plan", rate_limit_total_rps=2.5)
        with open(os.path.join(w.work, "config", "writeback.json"), "w") as fh:
            json.dump(cfg, fh)
        P, sha, sel, rec = self._plan()
        w.fake.clear_requests()
        self._refused(w.run(P, sel, rec))

    def test_stop_file(self):
        w = self.w
        P, sha, sel, rec = self._plan()
        open(os.path.join(w.state, wb_apply.STOP_FILE), "w").close()
        w.fake.clear_requests()
        self._refused(w.run(P, sel, rec), "aborted", P)

    def test_breaker_tripped(self):
        P, sha, sel, rec = self._plan(extra={"breaker": {"tripped": True}})
        self.w.fake.clear_requests()
        self._refused(self.w.run(P, sel, rec), "aborted", P)

    def test_members_changed_since_plan(self):
        w = self.w
        w.fake.add_user("Bob")
        P, sha, sel, rec = self._plan()
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self._refused(res, "aborted", P)
        self.assertIn("额度", res["reason"])

    def test_member_count_unknown_is_multi_member(self):
        w = self.w
        w.fake.set_caps("ro", {"read"})                 # no user_info capability -> members unknown
        P, sha, sel, rec = self._plan()
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self._refused(res, "aborted", P)

    def test_conflict_item_cannot_be_applied(self):
        w = self.w
        c = w.item("C-001", 0, [w.op_update(w.pages[0], 0, "x")], cat="C")
        P, sha, sel, rec = w.plan([c])
        w.fake.clear_requests()
        self._refused(w.run(P, sel, rec))

    def test_item_through_wrong_bucket(self):
        w = self.w
        b = w.item("B-001", 0, [w.op_trash_block(w.pages[0], 0)], cat="B")
        P, sha, sel, rec = w.plan([b])
        bad = {"items": ["B-001"], "delete": [], "delete_blocks": [], "rename": [], "move": [], "resolve": []}
        rec["selection"] = bad
        w.fake.clear_requests()
        self._refused(w.run(P, bad, rec))

    def test_lock_busy(self):
        import wb_locks
        P, sha, sel, rec = self._plan()
        self.w.fake.clear_requests()
        with wb_locks.lock("refresh", state_dir=self.w.state):
            res = self.w.run(P, sel, rec)
        self._refused(res, "confirmed", P)

    def test_read_client_must_be_readonly(self):
        w = self.w
        P, sha, sel, rec = self._plan()
        w.fake.clear_requests()
        res = wb_apply.execute_confirmed_plan(P, sel, rec, client=w.wb, read_client=w.admin, cfg=w.cfg, db=w.db,
                                              work=w.work, vault=w.vault, adopter=ok_adopter)
        self._refused(res)


class AuditScopeTests(WorldCase):
    def test_extra_object_forces_plan_and_zero_writes(self):
        w = self.w
        extra = w.fake.add_page("Not whitelisted")
        w.fake.grant("wb", extra)
        it = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "x")])
        P, sha, sel, rec = w.plan([it])
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "refused")
        self.assertNoWrites()
        self.assertEqual(wb_state.get_meta(w.db, "forced_mode"), "plan")
        self.assertEqual(wb_config.effective_mode(w.cfg, w.db), "plan")
        self.assertEqual(w.plan_status(P), "aborted")

    def test_audit_scope_ok_and_unreachable(self):
        w = self.w
        r = wb_apply.audit_scope(client=w.wb, cfg=w.cfg, db=w.db)
        self.assertTrue(r["ok"], r)
        missing = w.fake.add_page("Whitelisted but not shared")
        w.cfg["whitelist_page_ids"] = list(w.pages) + [missing]
        r = wb_apply.audit_scope(client=w.wb, cfg=w.cfg, db=w.db)
        self.assertFalse(r["ok"])
        self.assertEqual([u["id"] for u in r["unreachable"]], [missing])
        self.assertNoWrites()


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.w = World(1)
        self.tokdir = os.path.join(self.w.tmp, "cfgdir")
        os.mkdir(self.tokdir, 0o700)
        self.tok = os.path.join(self.tokdir, "writeback.env")
        with open(self.tok, "w") as fh:
            fh.write(f"NOTION_WRITEBACK_TOKEN={self.w.fake.secret('wb')}\n")
        os.chmod(self.tok, 0o600)
        self.ro = os.path.join(self.tokdir, "read.env")
        with open(self.ro, "w") as fh:
            fh.write(f"NOTION_TARGET_TOKEN={self.w.fake.secret('ro')}\n")

    def tearDown(self):
        self.w.close()

    def test_make_clients_and_permission_drift_refuses(self):
        w = self.w
        wb, rc = wb_apply.make_clients(w.cfg, token_path=self.tok, base_url=w.base, read_token_path=self.ro,
                                       gate_path=os.path.join(w.tmp, "g.json"), state_dir=w.state)
        self.assertIsInstance(wb, WritebackClient)
        self.assertFalse(wb.allow_block_trash)
        self.assertNotIn(w.fake.secret("wb"), repr(wb))
        it = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "x")])
        P, sha, sel, rec = w.plan([it])
        os.chmod(self.tok, 0o644)
        w.fake.clear_requests()
        res = wb_apply.execute_confirmed_plan(P, sel, rec, client=wb, read_client=rc, cfg=w.cfg, db=w.db,
                                              work=w.work, vault=w.vault, adopter=ok_adopter)
        self.assertEqual(res["status"], "refused")
        self.assertIn("凭据", res["reason"])
        self.assertEqual(w.wb_requests(), [])
        with self.assertRaises(Exception):
            wb_apply.make_clients(w.cfg, token_path=self.tok, base_url=w.base, read_token_path=self.ro,
                                  gate_path=os.path.join(w.tmp, "g.json"))

    def test_rollback_purpose_enables_block_trash_only_there(self):
        w = self.w
        wb, _rc = wb_apply.make_clients(w.cfg, token_path=self.tok, base_url=w.base, read_token_path=self.ro,
                                        gate_path=os.path.join(w.tmp, "g.json"), purpose="rollback")
        self.assertTrue(wb.allow_block_trash)


# =============================================================================================== faults
class FaultTests(WorldCase):
    def _plan_append(self):
        w = self.w
        pid = w.pages[0]
        it = w.item("M-001", 0, [w.op_append(pid, 0, ["new one"])])
        return w.plan([it])

    def test_timeout_after_commit_append_no_resend_blocked(self):
        w = self.w
        pid = w.pages[0]
        P, sha, sel, rec = self._plan_append()
        w.fake.inject("PATCH", rf"blocks/{pid}/children", after_commit=True, delay=3.0)
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        appends = [r for r in w.wb_writes() if r["path"].endswith("/children")]
        self.assertEqual(len(appends), 1)
        self.assertEqual([plain(b) for b in w.kids(pid)].count("new one"), 1)
        rows = w.journal(P)
        self.assertEqual(rows[0]["state"], "reconciled")
        self.assertEqual(json.loads(rows[0]["committed_ids"])["reconciled"], "set_difference_unique")
        self.assertEqual(w.page_state(pid), "BLOCKED")
        self.assertEqual(res["status"], "partially_applied")
        self.assertIn(pid, res["blocked_pages"])

    def test_timeout_after_commit_update_verified_by_get_not_resent(self):
        w = self.w
        pid = w.pages[0]
        it = w.item("M-001", 0, [w.op_update(pid, 0, "slow but done"), w.op_update(pid, 2, "second")])
        P, sha, sel, rec = w.plan([it])
        w.fake.inject("PATCH", rf"blocks/{it['ops'][0]['target_id']}$", after_commit=True, delay=3.0)
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res)
        self.assertEqual(len(w.wb_writes()), 2)                      # one per op, nothing resent
        row = w.journal(P)[0]
        self.assertEqual(row["state"], "done")
        self.assertEqual(json.loads(row["committed_ids"])["reconciled"], "applied_by_get")
        self.assertEqual([plain(b) for b in w.kids(pid)], ["slow but done", "p0 line 1", "second"])

    def test_dropped_connection_on_update_not_applied_no_resend(self):
        w = self.w
        pid = w.pages[0]
        it = w.item("M-001", 0, [w.op_update(pid, 0, "never")])
        P, sha, sel, rec = w.plan([it])
        bid = it["ops"][0]["target_id"]
        w.fake.inject("PATCH", rf"blocks/{bid}$", drop=True)
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(len(w.wb_writes()), 1)
        self.assertEqual(plain(w.kids(pid)[0]), "p0 line 0")
        row = w.journal(P)[0]
        self.assertEqual(row["state"], "reconciled")
        self.assertEqual(json.loads(row["committed_ids"])["reconciled"], "not_applied_by_get")
        self.assertIsNone(w.page_state(pid))           # nothing was written to the page
        self.assertEqual(res["status"], "partially_applied")

    def test_dropped_connection_on_append_unknown_needs_reconcile(self):
        w = self.w
        pid = w.pages[0]
        P, sha, sel, rec = self._plan_append()
        w.fake.inject("PATCH", rf"blocks/{pid}/children", drop=True)
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(len(w.wb_writes()), 1)
        self.assertEqual(w.journal(P)[0]["state"], "unknown")
        self.assertEqual(w.page_state(pid), "BLOCKED")
        self.assertEqual(res["unknown"], ["M-001"])
        with open(res["result_md"], encoding="utf-8") as fh:
            self.assertIn("reconcile --journal", fh.read())

    def test_409_rejected_not_applied(self):
        w = self.w
        pid = w.pages[0]
        it = w.item("M-001", 0, [w.op_update(pid, 0, "x"), w.op_update(pid, 1, "y")])
        P, sha, sel, rec = w.plan([it])
        w.fake.inject("PATCH", rf"blocks/{it['ops'][0]['target_id']}$", status=409)
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(len(w.wb_writes()), 1)       # second op never sent after the failure
        self.assertEqual([r["state"] for r in w.journal(P)], ["failed", "cancelled"])
        self.assertEqual(res["items"]["M-001"]["status"], "failed")
        self.assertIsNone(w.page_state(pid))

    def test_429_retried_by_client_then_applied(self):
        w = self.w
        pid = w.pages[0]
        it = w.item("M-001", 0, [w.op_update(pid, 0, "after 429")])
        P, sha, sel, rec = w.plan([it])
        w.fake.inject("PATCH", rf"blocks/{it['ops'][0]['target_id']}$", status=429, retry_after=1)
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res)
        self.assertEqual([r["status"] for r in w.wb_writes()], [429, 200])
        self.assertEqual(plain(w.kids(pid)[0]), "after 429")

    def test_503_committed_fitting_guidance_verified_not_resent(self):
        w = self.w
        pid = w.pages[0]
        P, sha, sel, rec = self._plan_append()
        w.fake.inject("PATCH", rf"blocks/{pid}/children", status=503, after_commit=True)
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(len(w.wb_writes()), 1)
        self.assertEqual(res["status"], "applied", res)
        row = w.journal(P)[0]
        self.assertEqual(row["state"], "done")
        self.assertTrue(json.loads(row["committed_ids"])["verify_required"])

    def test_503_mismatching_guidance_is_unknown_and_blocked(self):
        w = self.w
        pid = w.pages[0]
        P, sha, sel, rec = self._plan_append()
        w.fake.inject("PATCH", rf"blocks/{pid}/children", status=503, after_commit=True,
                      body=lambda c: {"object": "error", "status": 503, "code": "service_unavailable", "message": "x",
                                      "additional_data": {"retry_guidance": {"committed_resource_id": pid}}})
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(len(w.wb_writes()), 1)
        self.assertEqual(w.journal(P)[0]["state"], "reconciled")
        self.assertEqual(w.page_state(pid), "BLOCKED")
        self.assertEqual(res["status"], "partially_applied")

    def test_quota_403_vs_ordinary_403(self):
        w = self.w
        pid = w.pages[0]
        P, sha, sel, rec = self._plan_append()
        w.fake.set_block_limit(0)
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(len(w.wb_writes()), 1)
        last = w.db.execute("SELECT state, source FROM creation_state ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertEqual(last[0], "blocked_confirmed")
        self.assertEqual(w.page_state(pid), "BLOCKED")
        self.assertEqual(res["w_checks"]["W9"]["status"], "fail")
        # ordinary 403 on a second world: no creation_state change
        w2 = World(1)
        try:
            it = w2.item("M-001", 0, [w2.op_append(w2.pages[0], 0, ["x"])])
            P2, sha2, sel2, rec2 = w2.plan([it])
            w2.fake.inject("PATCH", rf"blocks/{w2.pages[0]}/children", status=403)
            w2.fake.clear_requests()
            res2 = w2.run(P2, sel2, rec2)
            self.assertEqual(len(w2.wb_writes()), 1)
            self.assertIsNone(w2.db.execute("SELECT 1 FROM creation_state WHERE state='blocked_confirmed'").fetchone())
            self.assertEqual(res2["items"]["M-001"]["status"], "blocked")
        finally:
            w2.close()

    def test_blocked_confirmed_is_persistent_and_refuses_next_plan(self):
        w = self.w
        P, sha, sel, rec = self._plan_append()
        w.fake.set_block_limit(0)
        w.run(P, sel, rec)
        w.fake.set_block_limit(None)
        with wb_state.transaction(w.db):
            w.db.execute("DELETE FROM page_state")
        P2, sha2, sel2, rec2 = self._plan_append()
        w.fake.clear_requests()
        res = w.run(P2, sel2, rec2)
        self.assertEqual(res["status"], "refused")       # plan says unlimited, state is blocked_confirmed
        self.assertNoWrites()

    def test_append_failure_stops_page_no_trash_blocked(self):
        w = World(1, allow_block_trash=True)
        try:
            pid = w.pages[0]
            b = w.item("B-001", 0, [w.op_trash_block(pid, 2)], cat="B", binds=["M-001"])
            m = w.item("M-001", 0, [w.op_update(pid, 0, "upd"), w.op_append(pid, 1, ["ins"])], binds=["B-001"])
            P, sha, sel, rec = w.plan([m, b])
            w.fake.inject("PATCH", rf"blocks/{pid}/children", status=500)
            w.fake.clear_requests()
            res = w.run(P, sel, rec)
            methods = [(r["method"], r["path"].endswith("/children")) for r in w.wb_writes()]
            self.assertEqual(methods, [("PATCH", False), ("PATCH", True)])
            self.assertFalse(any(r["method"] == "DELETE" for r in w.wb_writes()))
            self.assertEqual(w.page_state(pid), "BLOCKED")
            self.assertFalse(w.fake.block(it_id := b["ops"][0]["target_id"])["in_trash"], it_id)
            self.assertEqual(res["status"], "partially_applied")
        finally:
            w.close()

    def test_readback_mismatch_blocks_without_retry(self):
        w = self.w
        pid = w.pages[0]
        it = w.item("M-001", 0, [w.op_update(pid, 0, "written text")])
        P, sha, sel, rec = w.plan([it])
        bid = it["ops"][0]["target_id"]
        orig = w.rc.retrieve_block

        def normalised(b_id):            # Notion "normalised" the text differently on read-back
            b = orig(b_id)
            if b_id == bid and plain(b) == "written text":
                b["paragraph"]["rich_text"][0]["text"]["content"] = "written  text"
                b["paragraph"]["rich_text"][0]["plain_text"] = "written  text"
            return b
        w.rc.retrieve_block = normalised
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(len(w.wb_writes()), 1)
        self.assertEqual(w.page_state(pid), "BLOCKED")
        self.assertEqual(res["w_checks"]["W1"]["status"], "fail")
        self.assertEqual(res["status"], "partially_applied")
        self.assertIsNotNone(w.journal(P)[0]["after_json"])

    def test_unplanned_sibling_change_fails_w2(self):
        w = self.w
        pid = w.pages[0]
        it = w.item("M-001", 0, [w.op_update(pid, 0, "mine")])
        P, sha, sel, rec = w.plan([it])
        other = w.kids(pid)[2]["id"]
        orig = w.wb.update_block

        def racing(bid, body):          # someone edits another block during the apply window
            r = orig(bid, body)
            w.admin.update_block(other, {"paragraph": {"rich_text": rt("someone else")}})
            return r
        w.wb.update_block = racing
        res = w.run(P, sel, rec)
        self.assertEqual(res["w_checks"]["W2"]["status"], "fail")
        self.assertEqual(w.page_state(pid), "BLOCKED")


# =============================================================================================== precheck drift
class PrecheckDriftTests(WorldCase):
    """Each WB §11.3.1 condition: drift after the plan -> item skipped (CONFLICT_BOTH) and nothing sent; with one
    page selected the run is aborted by the 20 % rule."""

    def _go(self, items, select=None):
        w = self.w
        P, sha, sel, rec = w.plan(items, select=select)
        return P, sha, sel, rec

    def _assert_skipped(self, res, iid="M-001", contains=None):
        self.assertEqual(res["status"], "aborted", res)
        self.assertNoWrites()
        r = res["items"][iid]
        self.assertEqual(r["status"], "skipped")
        self.assertEqual(r.get("conflict"), "CONFLICT_BOTH")
        if contains:
            self.assertIn(contains, r["reason"])

    def test_block_changed(self):
        w = self.w
        it = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "x")])
        P, sha, sel, rec = self._go([it])
        w.admin.update_block(it["ops"][0]["target_id"], {"paragraph": {"rich_text": rt("remote edit")}})
        w.fake.clear_requests()
        self._assert_skipped(w.run(P, sel, rec), contains="已改变")

    def test_page_locked(self):
        w = self.w
        it = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "x")])
        P, sha, sel, rec = self._go([it])
        w.fake.pages[w.pages[0]]["is_locked"] = True
        w.fake.clear_requests()
        self._assert_skipped(w.run(P, sel, rec), contains="LOCKED")

    def test_page_trashed(self):
        w = self.w
        it = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "x")])
        P, sha, sel, rec = self._go([it])
        w.fake.pages[w.pages[0]]["in_trash"] = True
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertNoWrites()
        self.assertNotEqual(res["status"], "applied")

    def test_out_of_scope(self):
        # defence in depth: even if audit-scope passed, a page outside the declared scope is never written
        w = self.w
        it = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "x")])
        P, sha, sel, rec = self._go([it])
        w.cfg["whitelist_page_ids"] = []
        w.fake.clear_requests()
        with mock.patch.object(wb_apply, "audit_scope", return_value={"ok": True, "extra": [], "unreachable": []}):
            res = w.run(P, sel, rec)
        self._assert_skipped(res, contains="OUT_OF_WHITELIST")

    def test_vault_l_sha_changed(self):
        w = self.w
        it = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "x")])
        P, sha, sel, rec = self._go([it])
        w.write_note(w.rels[0], "edited again\n")
        w.fake.clear_requests()
        self._assert_skipped(w.run(P, sel, rec), contains="sha")

    def test_append_anchor_next_changed(self):
        w = self.w
        pid = w.pages[0]
        it = w.item("M-001", 0, [w.op_append(pid, 0, ["mine"])])
        P, sha, sel, rec = self._go([it])
        w.admin.append_children(pid, [req_para("inserted remotely")],
                                {"type": "after_block", "after_block": {"id": w.kids(pid)[0]["id"]}})
        w.fake.clear_requests()
        self._assert_skipped(w.run(P, sel, rec), contains="锚点")

    def test_append_bot_created_sibling_since_plan(self):
        w = self.w
        pid = w.pages[0]
        op = w.op_append(pid, 0, ["mine"])
        op.pop("anchor_next_id")
        op["parent_child_ids"] = [b["id"] for b in w.kids(pid)]
        it = w.item("M-001", 0, [op])
        P, sha, sel, rec = self._go([it])
        w.wb.append_children(pid, [req_para("left over from an earlier apply")])     # bot block, at the end
        w.fake.clear_requests()
        self._assert_skipped(w.run(P, sel, rec))

    def test_append_without_anchor_facts_is_refused(self):
        w = self.w
        pid = w.pages[0]
        op = w.op_append(pid, 0, ["mine"])
        op.pop("anchor_next_id")
        P, sha, sel, rec = self._go([w.item("M-001", 0, [op])])
        w.fake.clear_requests()
        self._assert_skipped(w.run(P, sel, rec), contains="PLAN_INCOMPLETE")

    def test_title_changed_for_rename(self):
        w = self.w
        pid = w.pages[0]
        it = w.item("T-001", 0, [{"op": "RENAME", "old_title": rt("Page 0"), "new_title": "New"}], cat="T")
        P, sha, sel, rec = self._go([it])
        w.admin.update_page(pid, {"properties": {"title": {"title": rt("Renamed in Notion")}}})
        w.fake.clear_requests()
        self._assert_skipped(w.run(P, sel, rec), iid="T-001", contains="CONFLICT_RENAME")

    def test_row_property_changed(self):
        w = self.w
        pid = w.pages[0]
        db, ds = w.fake.add_database(pid, "Tasks", {"Name": "title", "Tag": {"type": "select", "options": ["a", "b"]}})
        row = w.fake.add_row(ds, "Row 1", {"Tag": {"select": {"name": "a"}}})
        w.cfg["whitelist_database_ids"] = [db]
        w.cfg["automation_free_database_ids"] = [db]
        rel = "General/Tasks/Row 1.md"
        w.write_note(rel, f"---\nnotion_id: {row}\n---\nrow\n")
        live = w.rc.retrieve_page(row)
        it = {"item_id": "M-001", "category": "M", "notion_id": row, "rel_path": rel, "l_sha": w.vsha(rel), "binds": [],
              "ops": [{"op": "UPDATE_PROPERTIES", "properties": {"Tag": {"select": {"name": "b"}}},
                       "before": {"Tag": live["properties"]["Tag"]}}]}
        # positive first: executes
        P, sha, sel, rec = self._go([it])
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res)
        self.assertEqual(w.fake.page(row)["properties"]["Tag"]["select"]["name"], "b")
        # drift: the property changed again in Notion after a new plan
        live = w.rc.retrieve_page(row)
        it["ops"][0]["before"] = {"Tag": live["properties"]["Tag"]}
        it["ops"][0]["properties"] = {"Tag": {"select": {"name": "a"}}}
        P2, sha2, sel2, rec2 = self._go([it])
        w.admin.update_page(row, {"properties": {"Tag": {"select": {"name": "a"}}}})
        w.fake.clear_requests()
        self._assert_skipped(w.run(P2, sel2, rec2), contains="属性")

    def test_row_property_needs_automation_free_db(self):
        w = self.w
        pid = w.pages[0]
        db, ds = w.fake.add_database(pid, "Tasks", {"Name": "title", "Tag": {"type": "select", "options": ["a", "b"]}})
        row = w.fake.add_row(ds, "Row 1", {"Tag": {"select": {"name": "a"}}})
        w.cfg.update(whitelist_database_ids=[db])
        rel = "General/Tasks/Row 1.md"
        w.write_note(rel, f"---\nnotion_id: {row}\n---\nrow\n")
        live = w.rc.retrieve_page(row)
        it = {"item_id": "M-001", "category": "M", "notion_id": row, "rel_path": rel, "l_sha": w.vsha(rel), "binds": [],
              "ops": [{"op": "UPDATE_PROPERTIES", "properties": {"Tag": {"select": {"name": "b"}}},
                       "before": {"Tag": live["properties"]["Tag"]}}]}
        P, sha, sel, rec = self._go([it])
        w.fake.clear_requests()
        self._assert_skipped(w.run(P, sel, rec), contains="AUTOMATION_RISK")

    def test_blocked_page_state(self):
        w = self.w
        it = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "x")])
        P, sha, sel, rec = self._go([it])
        with wb_state.transaction(w.db):
            w.db.execute("INSERT INTO page_state(notion_id, state, reason, since) VALUES(?,?,?,?)",
                         (w.pages[0], "BLOCKED", "earlier", "x"))
        w.fake.clear_requests()
        self._assert_skipped(w.run(P, sel, rec), contains="BLOCKED_PAGE")

    def test_bound_b_not_selected_skips_m(self):
        w = World(1, allow_block_trash=True)
        try:
            pid = w.pages[0]
            b = w.item("B-001", 0, [w.op_trash_block(pid, 2)], cat="B", binds=["M-001"])
            m = w.item("M-001", 0, [w.op_append(pid, 1, ["replacement"])], binds=["B-001"])
            P, sha, sel, rec = w.plan([m, b], select=["M-001"])
            w.fake.clear_requests()
            res = w.run(P, sel, rec)
            self.assertEqual(w.wb_writes(), [])
            self.assertIn("B-001", res["items"]["M-001"]["reason"])
        finally:
            w.close()

    def test_block_trash_disabled_skips_b(self):
        w = self.w
        b = w.item("B-001", 0, [w.op_trash_block(w.pages[0], 2)], cat="B")
        P, sha, sel, rec = self._go([b])
        w.fake.clear_requests()
        self._assert_skipped(w.run(P, sel, rec), iid="B-001", contains="allow_block_trash")


class InconsistencyRatioTests(unittest.TestCase):
    def _run(self, n, drift):
        w = World(n)
        try:
            items = [w.item(f"M-{i + 1:03d}", i, [w.op_update(w.pages[i], 0, f"new {i}")]) for i in range(n)]
            P, sha, sel, rec = w.plan(items)
            for i in range(drift):
                w.admin.update_block(items[i]["ops"][0]["target_id"], {"paragraph": {"rich_text": rt("remote")}})
            w.fake.clear_requests()
            res = w.run(P, sel, rec)
            return res, len(w.wb_writes())
        finally:
            w.close()

    def test_one_of_five_inconsistent_continues(self):
        res, writes = self._run(5, 1)
        self.assertEqual(res["status"], "partially_applied")
        self.assertEqual(writes, 4)
        self.assertEqual(len(res["executed"]), 4)

    def test_three_of_ten_inconsistent_aborts_with_zero_writes(self):
        res, writes = self._run(10, 3)
        self.assertEqual(res["status"], "aborted")
        self.assertEqual(writes, 0)
        self.assertIn("20%", res["reason"])


# =============================================================================================== stop / mode during run
class MidRunGuardTests(unittest.TestCase):
    def test_stop_file_mid_run_stops_every_request(self):
        w = World(3)
        try:
            items = [w.item(f"M-{i + 1:03d}", i, [w.op_update(w.pages[i], 0, f"n{i}")]) for i in range(3)]
            P, sha, sel, rec = w.plan(items)
            orig = w.wb.update_block
            stop = os.path.join(w.state, wb_apply.STOP_FILE)

            def then_stop(bid, body):
                r = orig(bid, body)
                open(stop, "w").close()
                return r
            w.wb.update_block = then_stop
            w.fake.clear_requests()
            res = w.run(P, sel, rec)
            self.assertEqual(res["status"], "aborted")
            self.assertEqual(len(w.wb_writes()), 1)
            n = len(w.fake.requests)
            self.assertEqual(w.plan_status(P), "aborted")
            self.assertEqual(len(w.fake.requests), n)
            # the written page: no read-back (STOP), BLOCKED; rows of the other pages cancelled
            self.assertEqual(w.page_state(w.pages[0]), "BLOCKED")
            self.assertEqual(sorted({r["state"] for r in w.journal(P)}), ["cancelled", "done"])
        finally:
            w.close()

    def test_mode_lowered_mid_run(self):
        w = World(2)
        try:
            os.makedirs(os.path.join(w.work, "config"))
            cfgp = os.path.join(w.work, "config", "writeback.json")
            good = {k: v for k, v in w.cfg.items()}
            good["rate_limit_total_rps"] = 2.5
            with open(cfgp, "w") as fh:
                json.dump(good, fh)
            items = [w.item(f"M-{i + 1:03d}", i, [w.op_update(w.pages[i], 0, f"n{i}")]) for i in range(2)]
            P, sha, sel, rec = w.plan(items)
            orig = w.wb.update_block

            def then_lower(bid, body):
                r = orig(bid, body)
                with open(cfgp, "w") as fh:
                    json.dump(dict(good, mode="plan"), fh)
                return r
            w.wb.update_block = then_lower
            w.fake.clear_requests()
            res = w.run(P, sel, rec)
            self.assertEqual(res["status"], "aborted")
            self.assertEqual(len(w.wb_writes()), 1)
            self.assertEqual(plain(w.kids(w.pages[1])[0]), "p1 line 0")
        finally:
            w.close()


# =============================================================================================== new pages
class NewPageTests(WorldCase):
    def _new_item(self, n_children=2, title="Brand New"):
        w = self.w
        rel = f"General/Page 0/{title}.md"
        sha = w.write_note(rel, f"# {title}\n\nnew text\n")
        kids = [req_para(f"c{i}") for i in range(n_children)]
        return {"item_id": "N-001", "category": "N", "notion_id": None, "rel_path": rel, "l_sha": sha, "binds": [],
                "ops": [{"op": "CREATE_PAGE", "parent": {"page_id": w.pages[0]}, "title": title, "children": kids}]}

    def test_create_with_filling_mapping_and_frontmatter(self):
        w = self.w
        it = self._new_item(130)
        P, sha, sel, rec = w.plan([it])
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res)
        new = res["created"]["N-001"]
        writes = w.wb_writes()
        self.assertEqual([r["method"] for r in writes], ["POST", "PATCH"])          # POST (100) + one APPEND (30)
        self.assertEqual(len(w.kids(new)), 130)
        self.assertEqual([plain(b) for b in w.kids(new)][:3], ["c0", "c1", "c2"])
        m = json.loads(wb_state.get_meta(w.db, "created_page:" + it["rel_path"]))
        self.assertEqual((m["page_id"], m["state"]), (new, "complete"))
        with open(os.path.join(w.vault, *it["rel_path"].split("/")), encoding="utf-8") as fh:
            self.assertTrue(fh.read().startswith(f"---\nnotion_id: {new}\n---\n# Brand New"))
        bk = os.path.join(w.work, "backup", "writeback", P, *it["rel_path"].split("/"))
        self.assertTrue(os.path.isfile(bk))
        self.assertEqual(wb_common.sha256_file(bk), it["l_sha"])
        self.assertEqual(w.db.execute("SELECT SUM(created_blocks) FROM block_budget WHERE plan_id=?", (P,)).fetchone()[0], 131)
        self.assertEqual(res["w_checks"]["W9"]["status"], "pass")

    def test_failure_while_filling_is_partial_create_no_retry(self):
        w = self.w
        it = self._new_item(130)
        P, sha, sel, rec = w.plan([it])
        w.fake.inject("PATCH", r"blocks/.*/children", status=500)
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        new = res["created"]["N-001"]
        self.assertEqual([r["method"] for r in w.wb_writes()], ["POST", "PATCH"])
        self.assertEqual(w.page_state(new), "PARTIAL_CREATE")
        m = json.loads(wb_state.get_meta(w.db, "created_page:" + it["rel_path"]))
        self.assertEqual(m["state"], "partial")
        self.assertEqual(res["status"], "partially_applied")

    def test_frontmatter_not_written_when_vault_changed(self):
        w = self.w
        it = self._new_item(1)
        P, sha, sel, rec = w.plan([it])
        orig = w.wb.create_page
        p = os.path.join(w.vault, *it["rel_path"].split("/"))

        def create_then_user_edits(body):
            r = orig(body)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write("user typed more\n")
            return r
        w.wb.create_page = create_then_user_edits
        res = w.run(P, sel, rec)
        with open(p, encoding="utf-8") as fh:
            self.assertNotIn("notion_id", fh.read())
        self.assertTrue(json.loads(wb_state.get_meta(w.db, "created_page:" + it["rel_path"]))["page_id"])
        self.assertTrue(any("frontmatter" in n for n in res["notes"]))

    def test_existing_mapping_or_bot_page_prevents_duplicate(self):
        w = self.w
        it = self._new_item(1)
        left = w.wb.create_page({"parent": {"page_id": w.pages[0]}, "properties": {"title": {"title": rt("Brand New")}}})
        w.cfg["whitelist_page_ids"].append(left.committed_ids[0])     # in scope, but a bot page made after the plan
        P, sha, sel, rec = w.plan([it])
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertNoWrites()
        self.assertIn("同名", res["items"]["N-001"]["reason"])

    def test_create_timeout_after_commit_reconciled_partial(self):
        w = self.w
        it = self._new_item(1)
        P, sha, sel, rec = w.plan([it])
        w.fake.inject("POST", r"^pages$", after_commit=True, delay=3.0)
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(len([r for r in w.wb_writes() if r["method"] == "POST"]), 1)
        titles = [b["child_page"]["title"] for b in w.kids(w.pages[0]) if b["type"] == "child_page"]
        self.assertEqual(titles.count("Brand New"), 1)
        row = w.journal(P)[0]
        self.assertEqual(row["state"], "reconciled")
        m = json.loads(wb_state.get_meta(w.db, "created_page:" + it["rel_path"]))
        self.assertEqual(m["state"], "partial")
        self.assertEqual(res["status"], "partially_applied")


# =============================================================================================== crash + resume
class CrashResumeTests(WorldCase):
    def _plan2(self):
        w = self.w
        pid = w.pages[0]
        it = w.item("M-001", 0, [w.op_update(pid, 0, "U1"), w.op_append(pid, 1, ["A1"])])
        return w.plan([it])

    def _resume_conf(self, P, sha, ids=("M-001",)):
        self.w.confirm(P, sha, list(ids), "resume")
        return self.w.record(P, sha, "resume")

    def test_crash_after_intent_before_send_resume_sends_once(self):
        w = self.w
        P, sha, sel, rec = self._plan2()
        orig = wb_apply._send
        with mock.patch.object(wb_apply, "_send", side_effect=_Crash()):
            with self.assertRaises(_Crash):
                w.run(P, sel, rec)
        self.assertEqual(w.plan_status(P), "applying")
        self.assertEqual([r["state"] for r in w.journal(P)], ["intent", "intent"])
        self.assertNoWrites()
        self.assertIs(wb_apply._send, orig)
        res = w.resume(P, self._resume_conf(P, sha))
        self.assertEqual(res["status"], "applied", res)
        self.assertEqual(len(w.wb_writes()), 2)
        self.assertEqual([plain(b) for b in w.kids(w.pages[0])][:3], ["U1", "p0 line 1", "A1"])

    def test_crash_after_sent_mark_is_unknown_never_resent(self):
        w = self.w
        P, sha, sel, rec = self._plan2()
        orig = wb_apply._j_update

        def crash_on_sent(ctx, seq, **cols):
            orig(ctx, seq, **cols)
            if cols.get("state") == "sent":
                raise _Crash()
        with mock.patch.object(wb_apply, "_j_update", side_effect=crash_on_sent):
            with self.assertRaises(_Crash):
                w.run(P, sel, rec)
        self.assertEqual([r["state"] for r in w.journal(P)], ["sent", "intent"])
        n0 = len(w.wb_writes())
        res = w.resume(P, self._resume_conf(P, sha))
        self.assertEqual(len(w.wb_writes()), n0)           # nothing sent by resume
        self.assertEqual([r["state"] for r in w.journal(P)], ["unknown", "cancelled"])
        self.assertEqual(w.page_state(w.pages[0]), "BLOCKED")
        self.assertEqual(res["status"], "partially_applied")

    def test_crash_after_response_before_done(self):
        w = self.w
        P, sha, sel, rec = self._plan2()
        with mock.patch.object(wb_apply, "_result_info", side_effect=_Crash()):
            with self.assertRaises(_Crash):
                w.run(P, sel, rec)
        self.assertEqual(len(w.wb_writes()), 1)             # the UPDATE reached Notion
        self.assertEqual([r["state"] for r in w.journal(P)], ["sent", "intent"])
        res = w.resume(P, self._resume_conf(P, sha))
        self.assertEqual(len(w.wb_writes()), 1)             # never resent, APPEND not sent either
        self.assertEqual(w.journal(P)[0]["state"], "unknown")
        self.assertEqual(res["status"], "partially_applied")

    def test_crash_mid_page_resume_runs_only_intent_rows(self):
        w = self.w
        P, sha, sel, rec = self._plan2()
        orig = wb_apply._exec_step
        calls = []

        def second_crashes(ctx, g, seq):
            calls.append(seq)
            if len(calls) == 2:
                raise _Crash()
            return orig(ctx, g, seq)
        with mock.patch.object(wb_apply, "_exec_step", side_effect=second_crashes):
            with self.assertRaises(_Crash):
                w.run(P, sel, rec)
        self.assertEqual([r["state"] for r in w.journal(P)], ["done", "intent"])
        res = w.resume(P, self._resume_conf(P, sha))
        self.assertEqual(res["status"], "applied", res)
        self.assertEqual(len(w.wb_writes()), 2)
        self.assertEqual([plain(b) for b in w.kids(w.pages[0])][:3], ["U1", "p0 line 1", "A1"])

    def test_crash_after_done_before_readback(self):
        w = self.w
        P, sha, sel, rec = self._plan2()
        with mock.patch.object(wb_apply, "_readback_group", side_effect=_Crash()):
            with self.assertRaises(_Crash):
                w.run(P, sel, rec)
        self.assertEqual([r["state"] for r in w.journal(P)], ["done", "done"])
        res = w.resume(P, self._resume_conf(P, sha))
        self.assertEqual(res["status"], "applied", res)
        self.assertEqual(len(w.wb_writes()), 2)
        self.assertTrue(all(r["after_json"] for r in w.journal(P)))

    def test_resume_refusals(self):
        w = self.w
        P, sha, sel, rec = self._plan2()
        with mock.patch.object(wb_apply, "_send", side_effect=_Crash()):
            with self.assertRaises(_Crash):
                w.run(P, sel, rec)
        res = w.resume(P, w.record(P, sha, "resume"))          # no resume confirmation rows
        self.assertEqual(res["status"], "refused")
        res = w.resume(P, w.record(P, sha, "apply"))            # wrong scope
        self.assertEqual(res["status"], "refused")
        self.assertNoWrites()
        P2, sha2, sel2, rec2 = self._plan2()
        res = w.resume(P2, self._resume_conf(P2, sha2))        # confirmed, never applying
        self.assertEqual(res["status"], "refused")
        with wb_state.transaction(w.db):
            w.db.execute("UPDATE plans SET status='aborted' WHERE plan_id=?", (P,))
        res = w.resume(P, self._resume_conf(P, sha))
        self.assertEqual(res["status"], "refused")
        self.assertNoWrites()

    def test_resume_of_expired_plan_closes_it(self):
        w = self.w
        P, sha, sel, rec = self._plan2()
        with mock.patch.object(wb_apply, "_send", side_effect=_Crash()):
            with self.assertRaises(_Crash):
                w.run(P, sel, rec)
        with wb_state.transaction(w.db):
            w.db.execute("UPDATE plans SET expires_at=? WHERE plan_id=?", ("2000-01-01T00:00:00+00:00", P))
        res = w.resume(P, self._resume_conf(P, sha))
        self.assertEqual(res["status"], "refused")
        self.assertNoWrites()
        self.assertEqual(w.plan_status(P), "aborted")
        self.assertEqual({r["state"] for r in w.journal(P)}, {"cancelled"})

    def test_resume_precheck_drift_after_crash(self):
        w = self.w
        P, sha, sel, rec = self._plan2()
        with mock.patch.object(wb_apply, "_send", side_effect=_Crash()):
            with self.assertRaises(_Crash):
                w.run(P, sel, rec)
        w.admin.update_block(w.journal(P)[0]["target_id"], {"paragraph": {"rich_text": rt("remote")}})
        res = w.resume(P, self._resume_conf(P, sha))
        self.assertNoWrites()
        self.assertEqual(res["status"], "partially_applied")


# =============================================================================================== reconcile
class ReconcileTests(WorldCase):
    def _unknown_append(self, commit):
        w = self.w
        pid = w.pages[0]
        it = w.item("M-001", 0, [w.op_append(pid, 0, ["maybe"])])
        P, sha, sel, rec = w.plan([it])
        if commit:
            w.fake.inject("PATCH", rf"blocks/{pid}/children", after_commit=True, delay=3.0)
        else:
            w.fake.inject("PATCH", rf"blocks/{pid}/children", drop=True)
        with mock.patch.object(wb_apply, "_append_set_diff", return_value=None):
            w.run(P, sel, rec)
        row = w.journal(P)[0]
        self.assertEqual(row["state"], "unknown")
        return row["seq"]

    def test_reconcile_committed(self):
        w = self.w
        seq = self._unknown_append(True)
        bad = wb_apply.reconcile_journal(seq, "not_committed", read_client=w.rc, cfg=w.cfg, db=w.db, work=w.work)
        self.assertFalse(bad["ok"])                 # live state shows it committed: refused
        r = wb_apply.reconcile_journal(seq, "committed", read_client=w.rc, cfg=w.cfg, db=w.db, work=w.work)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["ids"]), 1)
        row = wb_state.connect(os.path.join(w.state, "writeback.db")).execute(
            "SELECT state FROM journal WHERE seq=?", (seq,)).fetchone()
        self.assertEqual(row[0], "reconciled")
        self.assertEqual(w.page_state(w.pages[0]), "BLOCKED")

    def test_reconcile_not_committed(self):
        w = self.w
        seq = self._unknown_append(False)
        n = len(w.wb_writes())
        bad = wb_apply.reconcile_journal(seq, "committed", read_client=w.rc, cfg=w.cfg, db=w.db, work=w.work)
        self.assertFalse(bad["ok"])
        r = wb_apply.reconcile_journal(seq, "not_committed", read_client=w.rc, cfg=w.cfg, db=w.db, work=w.work)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(w.wb_writes()), n)     # reconcile never writes Notion
        again = wb_apply.reconcile_journal(seq, "not_committed", read_client=w.rc, cfg=w.cfg, db=w.db, work=w.work)
        self.assertFalse(again["ok"])


# =============================================================================================== rollback
class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.w = World(1, allow_block_trash=True)

    def tearDown(self):
        self.w.close()

    def _applied(self):
        w = self.w
        pid = w.pages[0]
        m = w.item("M-001", 0, [w.op_update(pid, 0, "U1"), w.op_append(pid, 1, ["A1", "A2"])])
        b = w.item("B-001", 0, [w.op_trash_block(pid, 2)], cat="B")
        t = w.item("T-001", 0, [{"op": "RENAME", "old_title": rt("Page 0"), "new_title": "Page Zero"}], cat="T")
        P, sha, sel, rec = w.plan([m, b, t])
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res)
        return P, sha

    def _rb(self, P, sha, items=None):
        w = self.w
        ids = items or ["M-001", "B-001", "T-001"]
        w.confirm(P, sha, ids, "rollback")
        return wb_apply.rollback_plan(P, items, w.record(P, sha, "rollback"), client=w.wb, read_client=w.rc,
                                      cfg=w.cfg, db=w.db, work=w.work, vault=w.vault)

    def test_full_rollback_restores_content(self):
        w = self.w
        pid = w.pages[0]
        P, sha = self._applied()
        prev = wb_apply.rollback_preview(P, None, read_client=w.rc, db=w.db, work=w.work)
        self.assertEqual(prev["counts"], {"do": 4, "skip": 0, "manual": 0})
        n = len(w.wb_writes())
        self.assertEqual(len(w.wb_writes()), n)       # preview sent nothing
        w.fake.clear_requests()
        res = self._rb(P, sha)
        self.assertEqual(res["status"], "applied", res)
        live = [plain(b) for b in w.kids(pid)]
        self.assertEqual(sorted(live), sorted(["p0 line 0", "p0 line 1", "p0 line 2"]))
        self.assertEqual(w.fake.page(pid)["properties"]["title"]["title"][0]["plain_text"], "Page 0")
        self.assertTrue(any("位置" in (s.get("note") or "") for s in res["steps"]))
        kinds = sorted((r["method"]) for r in w.wb_writes())
        self.assertEqual(kinds, ["DELETE", "DELETE", "PATCH", "PATCH", "PATCH"])
        self.assertTrue(all(r["state"] == "done" for r in w.journal(P) if r["op"].startswith("RB_")))
        self.assertTrue(os.path.isfile(res["result_md"]))

    def test_rollback_skips_block_edited_since(self):
        w = self.w
        pid = w.pages[0]
        P, sha = self._applied()
        upd_target = w.journal(P)[0]["target_id"]
        w.admin.update_block(upd_target, {"paragraph": {"rich_text": rt("edited after apply")}})
        prev = wb_apply.rollback_preview(P, ["M-001"], read_client=w.rc, db=w.db, work=w.work)
        acts = {s["op"]: s["action"] for s in prev["steps"]}
        self.assertEqual(acts, {"RB_UPDATE": "skip", "RB_TRASH_BLOCK": "do"})
        w.fake.clear_requests()
        res = self._rb(P, sha, ["M-001"])
        self.assertEqual(plain(w.kids(pid)[0]), "edited after apply")
        self.assertEqual([r["method"] for r in w.wb_writes()], ["DELETE", "DELETE"])
        self.assertNotIn("A1", [plain(b) for b in w.kids(pid)])

    def test_rollback_new_page_trashes_only_the_page(self):
        w = self.w
        rel = "General/Page 0/Fresh.md"
        sha = w.write_note(rel, "# Fresh\n")
        it = {"item_id": "N-001", "category": "N", "notion_id": None, "rel_path": rel, "l_sha": sha, "binds": [],
              "ops": [{"op": "CREATE_PAGE", "parent": {"page_id": w.pages[0]}, "title": "Fresh",
                       "children": [req_para(f"c{i}") for i in range(105)]}]}
        P, psha, sel, rec = w.plan([it])
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res)
        new = res["created"]["N-001"]
        prev = wb_apply.rollback_preview(P, None, read_client=w.rc, db=w.db, work=w.work)
        self.assertEqual([s["op"] for s in prev["steps"]], ["RB_TRASH_PAGE"])
        w.fake.clear_requests()
        out = self._rb(P, psha, ["N-001"])
        self.assertEqual(out["status"], "applied", out)
        self.assertEqual([(r["method"], r["path"]) for r in w.wb_writes()], [("PATCH", f"pages/{new}")])
        self.assertTrue(w.fake.page(new)["in_trash"])

    def test_rollback_requires_rollback_confirmation(self):
        w = self.w
        P, sha = self._applied()
        w.fake.clear_requests()
        res = wb_apply.rollback_plan(P, None, w.record(P, sha, "rollback"), client=w.wb, read_client=w.rc, cfg=w.cfg,
                                     db=w.db, work=w.work, vault=w.vault)
        self.assertEqual(res["status"], "refused")
        self.assertEqual(w.wb_writes(), [])


# =============================================================================================== adoption orchestration
class AdoptionPipelineTests(WorldCase):
    """_adopt_pipeline with a stub runner + stub refetch: argument contract (L-05 --snapshot-immutable, --ids,
    adopt ids == --only-ids, record_adopted on exit 0/3/4/5 only, adopt_needs_manual -> needs_manual)."""

    def _base(self):
        w = self.w
        snaps = os.path.join(w.work, "snapshots")
        os.makedirs(snaps, exist_ok=True)
        base = os.path.join(snaps, "snap_base.db")
        con = sqlite3.connect(base)
        con.execute("CREATE TABLE objects(id TEXT, kind TEXT, parent_id TEXT, data TEXT)")
        con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        con.execute("CREATE TABLE blocks(id TEXT, page_id TEXT, parent_id TEXT, ord INT, type TEXT, has_children INT, data TEXT)")
        for pid in w.pages:
            con.execute("INSERT INTO objects VALUES(?,?,?,?)", (pid, "page", None, "{}"))
        con.commit()
        con.close()
        with open(base + ".roots.json", "w") as fh:
            json.dump([], fh)
        with open(os.path.join(w.state, "last_published_snapshot.txt"), "w") as fh:
            fh.write(base + "\n")
        return base

    def _refetch(self, calls):
        def fake_refetch(base, out, page_ids, ds_ids, client, **kw):
            calls.append((base, out, list(page_ids), list(ds_ids), kw))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            con = sqlite3.connect(out)
            con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
            con.executemany("INSERT INTO meta VALUES(?,?)", [("finished_at", "2026-11-18T00:00:00+00:00"),
                                                            ("targeted_refetch", "{}"), ("trash_markers", "[]")])
            con.commit()
            con.close()
            with open(out + ".roots.json", "w") as fh:
                json.dump([], fh)
            return {"out": out, "roots_json": out + ".roots.json", "trashed": [], "pruned": []}
        return fake_refetch

    def _runner(self, calls, publish_apply_rc=0, needs_manual=None, staged_from_vault=True, foreign_in_full=False):
        w = self.w

        def run(script, args, timeout=None):
            calls.append((script, list(args)))
            a = dict(zip(args[::1], args[1::1]))
            if script == "export_obsidian.py":
                stg = a["--out"]
                os.makedirs(os.path.join(stg, "vault", "General"), exist_ok=True)
                man = {}
                for rel in w.rels:
                    src = os.path.join(w.vault, *rel.split("/"))
                    with open(src, "rb") as fh:
                        data = fh.read()
                    with open(os.path.join(stg, "vault", *rel.split("/")), "wb") as fh:
                        fh.write(data)
                    man[rel] = {"kind": "page", "notion_id": w.pages[w.rels.index(rel)], "sha256": wb_common.sha256_bytes(data)}
                with open(os.path.join(stg, "manifest.json"), "w") as fh:
                    json.dump(man, fh)
                return 0
            if script == "verify_vault.py":
                os.makedirs(a["--out"], exist_ok=True)
                with open(os.path.join(a["--out"], "verify.json"), "w") as fh:
                    json.dump({"status_by_check": {f"C{i:02d}": "PASS" for i in range(1, 12)}}, fh)
                return 0
            if script == "publish_vault.py":
                rep = a["--report-dir"]
                if "--apply" not in args:
                    acts = [{"op": "UNCHANGED", "rel": r, "nid": w.pages[i], "sha": w.vsha(r)} for i, r in enumerate(w.rels)]
                    if foreign_in_full and "--only-ids" not in args:     # e.g. a locally deleted, unheld note
                        acts.append({"op": "ADD", "rel": "General/Unrelated.md", "kind": "page",
                                     "nid": "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f", "sha": "cd" * 32})
                    with open(os.path.join(rep, "publish_plan.json"), "w") as fh:
                        json.dump({"actions": acts, "plan_sha256": "ab" * 32}, fh)
                    if needs_manual:
                        with open(os.path.join(rep, "adopt_needs_manual.json"), "w") as fh:
                            json.dump([{"notion_id": n, "reason": "x"} for n in needs_manual], fh)
                    return 0
                return publish_apply_rc
            return 1
        return run

    def _ctx_after_apply(self):
        w = self.w
        it = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "adopt me")])
        P, sha, sel, rec = w.plan([it])
        holder = {}

        def capture(ctx, written, **kw):
            holder["ctx"], holder["written"] = ctx, written
            return ok_adopter(ctx, written)
        w.run(P, sel, rec, adopter=capture)
        return holder["ctx"], holder["written"]

    def test_arguments_and_record_adopted(self):
        w = self.w
        base = self._base()
        ctx, written = self._ctx_after_apply()
        rcalls, calls = [], []
        out = wb_apply._adopt_pipeline(ctx, written, runner=self._runner(calls), refetch=self._refetch(rcalls),
                                       blobs_dir=os.path.join(w.tmp, "blobs"), sidebar=os.path.join(w.tmp, "sb.json"))
        self.assertEqual(rcalls[0][0], base)
        self.assertEqual(rcalls[0][2], [w.pages[0]])
        self.assertTrue(rcalls[0][1].startswith(os.path.join(w.work, "snapshots", "wb")))
        scripts = [c[0] for c in calls]
        self.assertEqual(scripts, ["export_obsidian.py", "verify_vault.py", "publish_vault.py", "publish_vault.py",
                                   "verify_vault.py"])
        for c in calls:
            if c[0] == "verify_vault.py":
                self.assertIn("--snapshot-immutable", c[1])
        self.assertIn("--immutable", calls[0][1])
        post = calls[-1][1]
        ids_file = post[post.index("--ids") + 1]
        with open(ids_file) as fh:
            self.assertEqual(fh.read().split(), [w.pages[0]])
        pub = calls[3][1]
        self.assertIn("--apply", pub)
        with open(pub[pub.index("--adopt-writeback") + 1]) as fh:
            adopt = json.load(fh)
        # WB §11.4: no unreleased paused round -> normal publish (no --only-ids), holds as a refresh round has them
        self.assertNotIn("--only-ids", pub)
        self.assertEqual(out["steps"]["publish_mode"], "full")
        with open(pub[pub.index("--holds") + 1]) as fh:
            self.assertIsInstance(json.load(fh), dict)
        self.assertEqual(adopt[w.pages[0]]["rel"], w.rels[0])
        self.assertIn("--allow-other-vault", pub)
        with open(os.path.join(w.state, "last_adopted_wb_snapshot.json")) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["plan_id"], ctx.plan_id)
        self.assertEqual(out["W6"]["status"], "pass", out)

    def test_publish_refused_does_not_record_adoption(self):
        w = self.w
        self._base()
        ctx, written = self._ctx_after_apply()
        out = wb_apply._adopt_pipeline(ctx, written, runner=self._runner([], publish_apply_rc=2),
                                       refetch=self._refetch([]), blobs_dir=w.tmp, sidebar=os.path.join(w.tmp, "sb.json"))
        self.assertFalse(out["ok"])
        self.assertFalse(os.path.exists(os.path.join(w.state, "last_adopted_wb_snapshot.json")))
        self.assertEqual(out["W6"]["status"], "fail")

    def test_race_exit_still_records_and_needs_manual_reported(self):
        w = self.w
        self._base()
        ctx, written = self._ctx_after_apply()
        out = wb_apply._adopt_pipeline(ctx, written, runner=self._runner([], publish_apply_rc=4, needs_manual=[w.pages[0]]),
                                       refetch=self._refetch([]), blobs_dir=w.tmp, sidebar=os.path.join(w.tmp, "sb.json"))
        self.assertTrue(os.path.exists(os.path.join(w.state, "last_adopted_wb_snapshot.json")))
        self.assertEqual(out["needs_manual"], [w.pages[0]])
        vs = w.db.execute("SELECT verify_status FROM plan_items WHERE notion_id=?", (w.pages[0],)).fetchone()[0]
        self.assertEqual(vs, "needs_manual")
        self.assertEqual(out["W6"]["status"], "fail")

    def test_no_base_snapshot_is_needs_manual(self):
        w = self.w
        ctx, written = self._ctx_after_apply()
        out = wb_apply._adopt_pipeline(ctx, written, runner=self._runner([]), refetch=self._refetch([]))
        self.assertFalse(out["ok"])
        self.assertIn("refresh", out["reason"])
        self.assertEqual(out["needs_manual"], [w.pages[0]])

    def test_failed_adoption_makes_apply_partial(self):
        w = self.w
        it = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "x")])
        P, sha, sel, rec = w.plan([it])
        res = w.run(P, sel, rec, adopter=None, runner=self._runner([]), sidebar=os.path.join(w.tmp, "sb.json"))
        self.assertEqual(res["status"], "partially_applied")     # no base snapshot -> adoption needs manual
        self.assertEqual(res["w_checks"]["W6"]["status"], "fail")

    # ---- fix round: WB §11.4 publish modes, holds, deviations
    def test_paused_round_publishes_only_the_plan_notes(self):
        w = self.w
        self._base()
        with open(os.path.join(w.state, "paused_runs.json"), "w") as fh:
            json.dump({"latest": "R9", "runs": {"R9": {"status": "paused"}, "R8": {"status": "approved"}}}, fh)
        ctx, written = self._ctx_after_apply()
        calls = []
        out = wb_apply._adopt_pipeline(ctx, written, runner=self._runner(calls), refetch=self._refetch([]),
                                       blobs_dir=w.tmp, sidebar=os.path.join(w.tmp, "sb.json"))
        self.assertEqual(out["steps"]["publish_mode"], "only_ids")
        pub = [c[1] for c in calls if c[0] == "publish_vault.py"]
        for a in pub:
            with open(a[a.index("--only-ids") + 1]) as fh:
                self.assertEqual(fh.read().split(), [w.pages[0]])        # exactly the adopted notes
        self.assertTrue(any("暂停" in n for n in ctx.notes))

    def test_foreign_write_in_full_publish_falls_back_to_only_ids(self):
        w = self.w
        self._base()
        ctx, written = self._ctx_after_apply()
        calls = []
        out = wb_apply._adopt_pipeline(ctx, written, runner=self._runner(calls, foreign_in_full=True),
                                       refetch=self._refetch([]), blobs_dir=w.tmp, sidebar=os.path.join(w.tmp, "sb.json"))
        self.assertEqual([c[0] for c in calls], ["export_obsidian.py", "verify_vault.py", "publish_vault.py",
                                                 "publish_vault.py", "publish_vault.py", "verify_vault.py"])
        first, second, apply_ = (c[1] for c in calls[2:5])
        self.assertNotIn("--only-ids", first)                       # the normal publish was planned first ...
        self.assertIn("--only-ids", second)                         # ... wrote an unrelated note: fallback
        self.assertIn("--apply", apply_)
        self.assertEqual(apply_[apply_.index("--report-dir") + 1], second[second.index("--report-dir") + 1])
        self.assertEqual(out["steps"]["publish_mode"], "only_ids_parents")
        self.assertTrue(any("General/Unrelated.md" in n for n in ctx.notes))

    def test_adoption_holds_include_pending_deletes_and_blocked_pages(self):
        w = self.w
        self._base()
        ctx, written = self._ctx_after_apply()
        hold_id, blocked_id, auto_id = (str(__import__("uuid").uuid4()) for _ in range(3))
        with wb_state.transaction(w.db):
            w.db.execute("INSERT INTO pending_deletes(notion_id, rel_path, status) VALUES(?,?,?)",
                         (hold_id, "General/Gone.md", "hold"))
            w.db.execute("INSERT INTO page_state(notion_id, state, reason, since) VALUES(?,?,?,?)",
                         (blocked_id, "BLOCKED", "x", "x"))
        with open(os.path.join(w.state, "auto_sync_hold.json"), "w") as fh:
            json.dump({auto_id: {"reason": "C04", "source": "post_verify", "subtree": False}}, fh)
        calls = []
        wb_apply._adopt_pipeline(ctx, written, runner=self._runner(calls), refetch=self._refetch([]), blobs_dir=w.tmp,
                                 sidebar=os.path.join(w.tmp, "sb.json"))
        pub = next(c[1] for c in calls if c[0] == "publish_vault.py")
        with open(pub[pub.index("--holds") + 1]) as fh:
            holds = json.load(fh)
        self.assertEqual(holds[hold_id]["source"], "pending_delete")    # a locally deleted note is never re-added
        self.assertEqual(holds[blocked_id]["source"], "page_state")
        self.assertEqual(holds[auto_id], {"reason": "C04", "source": "post_verify", "subtree": False})

    def test_vault_deviations_with_dict_entries(self):
        """refresh.build_expected_deviations returns dict entries ({notion_id, path} / {notion_id, subtree, paths});
        the written objects are removed, the others kept (was: TypeError unhashable dict)."""
        import refresh
        import wb_scan
        w = self.w
        ctx, written = self._ctx_after_apply()
        mine, other = w.pages[0], str(__import__("uuid").uuid4())
        dev = {"schema": 1, "run_id": "x", "local_new": [], "name_overrides": [], "sync_conflict_copies": [],
               "local_modified": [{"notion_id": mine, "path": w.rels[0]}, {"notion_id": other, "path": "General/O.md"}],
               "hold_ids": [{"notion_id": other, "subtree": True, "paths": ["General/O.md"]}, mine],
               "paused": [{"notion_id": mine, "subtree": False, "paths": [w.rels[0]]}]}
        undecodable = "General/\udcff.md"
        rep = os.path.join(w.tmp, "rep")
        os.makedirs(rep)
        with mock.patch.object(wb_scan, "scan", return_value={"undecodable_names": [{"rel": undecodable}]}), \
                mock.patch.object(refresh, "build_expected_deviations", return_value=dev):
            p = wb_apply._vault_deviations(ctx, rep, "run-x", exclude={mine})
        with open(p) as fh:
            got = json.load(fh)
        self.assertEqual(got["local_new"], [undecodable])
        self.assertEqual(got["local_modified"], [{"notion_id": other, "path": "General/O.md"}])
        self.assertEqual(got["hold_ids"], [{"notion_id": other, "subtree": True, "paths": ["General/O.md"]}])
        self.assertEqual(got["paused"], [])

    def test_w6_diff_allows_renumbering_and_rename_keys_only(self):
        L = b"---\nnotion_id: x\nnotion_title: Kid\naliases:\n  - Kid\n---\n1. a\n2. new\n2. b\n"
        S = b"---\nnotion_id: x\nnotion_title: Kid2\nnotion_last_edited_time: t\n---\n1. a\n2. new\n3. b\n"
        self.assertEqual(wb_apply._diff_outside_ok_keys(L, S, rename=True), [])
        self.assertTrue(wb_apply._diff_outside_ok_keys(L, S, rename=False))           # title change only for T
        self.assertTrue(wb_apply._diff_outside_ok_keys(L, S.replace(b"3. b", b"3. B"), rename=True))


# =============================================================================================== real adoption (WB §11.4)
class RealAdoptionTests(unittest.TestCase):
    """End to end on temp copies: fake Notion -> real snapshot crawl (base) -> real exporter + publisher (initial
    publish into a temp vault: export.db, writeback.db base_history) -> local edit -> execute_confirmed_plan with
    the REAL adoption pipeline (wb_refetch.choose_base/refetch -> export_obsidian -> verify_vault --snapshot-immutable
    -> publish_vault --adopt-writeback/--only-ids -> verify_vault --ids) -> a second consecutive write-back."""

    @classmethod
    def setUpClass(cls):
        import snapshot as snapmod
        import verify_vault as vv
        cls.snapmod = snapmod
        cls.tmp = tempfile.mkdtemp(prefix="wbapply-e2e-")
        cls.work = os.path.join(cls.tmp, "work")
        for d in ("state", "snapshots", "staging", "reports", "backup"):
            os.makedirs(os.path.join(cls.work, d))
        cls.vault = os.path.join(cls.tmp, "vault")
        os.makedirs(cls.vault)
        cls.blobs = os.path.join(cls.tmp, "blobs")
        os.makedirs(os.path.join(cls.blobs, "tmp"))
        cls._saved_blobs = snapmod.BLOBS
        snapmod.BLOBS = cls.blobs
        cls.fake = FakeNotion(tokens={
            "ro": {"caps": {"read", "user_info"}, "roots": {"*"}, "secret": "secret-ro-e2e1"},
            "wb": {"caps": {"read", "update", "insert"}, "roots": set(), "secret": "secret-wb-e2e2"}})
        cls.fake.add_user("Alice")
        cls.base_url = cls.fake.start()
        cls.home = cls.fake.add_page("Home", children=[para("alpha line"), para("beta line"), para("gamma line")])
        cls.fake.grant("wb", cls.home)
        cls.gate = RateGate(os.path.join(cls.tmp, "gate.json"), total_rps=1000, burst=1000)
        cls.rc = make_read_client(base_url=cls.base_url, token=cls.fake.secret("ro"), rate=None, gate=cls.gate,
                                  sleep=lambda s: None)
        roots = [{"id": cls.home, "title": "Home", "teamspace": "General", "teamspace_order": 1, "root_order": 1}]
        cls.sidebar = os.path.join(cls.tmp, "sidebar.json")
        with open(cls.sidebar, "w", encoding="utf-8") as fh:
            json.dump({"teamspace_order_required": list(vv.SPEC_TEAMSPACES), "roots": roots, "order_match": True}, fh)
        cls.base = os.path.join(cls.work, "snapshots", "snap_base.db")
        cls._crawl(roots, cls.base)
        py = [sys.executable, "-I", "-B"]
        cls.export_db = os.path.join(cls.work, "state", "export.db")
        stg = os.path.join(cls.work, "staging", "R0")
        r = subprocess.run(py + [os.path.join(TOOLS, "export_obsidian.py"), "--snapshot", cls.base, "--sidebar",
                                 cls.sidebar, "--run-id", "R0", "--immutable", "--state", cls.export_db, "--out", stg],
                           capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        appr = os.path.join(cls.tmp, "approval.md")
        with open(appr, "w") as fh:
            fh.write("test approval\n")
        r = subprocess.run(py + [os.path.join(TOOLS, "publish_vault.py"), "--run-id", "R0", "--vault", cls.vault,
                                 "--state", cls.export_db, "--report-dir", os.path.join(cls.work, "reports", "R0"),
                                 "--staging-dir", stg, "--backup-dir", os.path.join(cls.work, "backup", "R0"),
                                 "--wb-db", os.path.join(cls.work, "state", "writeback.db"), "--allow-other-vault",
                                 "--apply", "--approval", appr], capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        for ts in vv.SPEC_TEAMSPACES:          # the real vault has every teamspace folder (C01)
            os.makedirs(os.path.join(cls.vault, ts), exist_ok=True)
        with open(os.path.join(cls.work, "state", "last_published_snapshot.txt"), "w") as fh:
            fh.write(cls.base + "\n")
        with open(os.path.join(cls.work, "state", "last_published.json"), "w") as fh:
            json.dump({"run_id": "R0", "snapshot": cls.base, "at": wb_common.now_iso()}, fh)
        with open(os.path.join(stg, "manifest.json"), encoding="utf-8") as fh:
            man = json.load(fh)
        cls.rel = next(k for k, v in man.items() if v.get("notion_id") == cls.home and k.endswith(".md"))
        cls.db = wb_state.connect(os.path.join(cls.work, "state", "writeback.db"))
        cls.wb = WritebackClient(cls.fake.secret("wb"), base_url=cls.base_url, gate=cls.gate, timeout=(5, 20))
        cls.cfg = wb_config.defaults()
        cls.cfg.update(mode="whitelist", read_integration_readonly_confirmed_at="x", whitelist_page_ids=[cls.home],
                       rate_limit_total_rps=1000)

    @classmethod
    def _crawl(cls, roots_in, path):
        snapmod = cls.snapmod
        roots = snapmod.detect_kinds(cls.rc, roots_in)
        with open(path + ".roots.json", "w", encoding="utf-8") as fh:
            json.dump(roots, fh, ensure_ascii=False, indent=1)
        store = snapmod.Store(path)
        me = cls.rc.me()
        store.x("INSERT OR REPLACE INTO meta VALUES('workspace_id', ?)", (me["bot"]["workspace_id"],))
        store.x("INSERT OR REPLACE INTO meta VALUES('notion_version', ?)", ("2026-03-11",))
        inv = snapmod.BlobInventory(cls.rc, store, workers=1, hash_files=True, comments=False)
        inv.seed([r for r in roots if not r.get("in_trash")])
        inv.run()
        inv.http.close()
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        store.db.close()
        for sfx in ("-wal", "-shm"):
            if os.path.exists(path + sfx) and os.path.getsize(path + sfx) == 0:
                os.unlink(path + sfx)

    @classmethod
    def tearDownClass(cls):
        cls.snapmod.BLOBS = cls._saved_blobs
        cls.db.close()
        cls.fake.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _edit_and_apply(self, old, new, plan_id):
        w_note = os.path.join(self.vault, *self.rel.split("/"))
        with open(w_note, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(old, text)
        text = text.replace(old, new)
        with open(w_note, "w", encoding="utf-8") as fh:
            fh.write(text)
        l_sha = wb_common.sha256_file(w_note)
        kids = self.rc.list_children(self.home)
        b = next(k for k in kids if plain(k) == old)
        item = {"item_id": "M-001", "category": "M", "notion_id": self.home, "rel_path": self.rel, "l_sha": l_sha,
                "binds": [], "ops": [{"op": "UPDATE", "target_id": b["id"], "before_norm_sha": wb_norm.block_json_sha(b),
                                      "request": {"paragraph": {"rich_text": rt(new)}}}]}
        now = datetime.datetime.now().astimezone()
        plan = {"plan_id": plan_id, "created_at": (now - datetime.timedelta(minutes=1)).isoformat(timespec="seconds"),
                "expires_at": (now + datetime.timedelta(hours=1)).isoformat(timespec="seconds"), "members": 1,
                "mode": self.cfg["mode"], "creation_state": "unlimited", "breaker": {"tripped": False},
                "items": [item]}
        d = os.path.join(self.work, "reports", "writeback", plan_id)
        os.makedirs(d)
        raw = json.dumps(plan, ensure_ascii=False).encode()
        with open(os.path.join(d, "plan.json"), "wb") as fh:
            fh.write(raw)
        sha = wb_common.sha256_bytes(raw)
        with wb_state.transaction(self.db):
            self.db.execute("INSERT INTO plans(plan_id, created_at, expires_at, plan_sha256, status) VALUES(?,?,?,?,?)",
                            (plan_id, plan["created_at"], plan["expires_at"], sha, "confirmed"))
            self.db.execute("INSERT INTO plan_items(plan_id, item_id, notion_id, rel_path, category, l_sha) "
                            "VALUES(?,?,?,?,?,?)", (plan_id, "M-001", self.home, self.rel, "M", l_sha))
            self.db.execute("INSERT INTO confirmations(plan_id, item_id, confirmed_at, os_user, tty, challenge_ok, "
                            "phrase_scope, plan_sha256) VALUES(?,?,?,?,?,1,'apply',?)",
                            (plan_id, "M-001", wb_common.now_iso(), "tester", "/dev/pts/9", sha))
        sel = World.selection(["M-001"])
        rec = {"plan_id": plan_id, "plan_sha256": sha, "challenge_ok": True, "phrase_scope": "apply", "selection": sel}
        return wb_apply.execute_confirmed_plan(plan_id, sel, rec, client=self.wb, read_client=self.rc, cfg=self.cfg,
                                               db=self.db, work=self.work, vault=self.vault, blobs_dir=self.blobs,
                                               sidebar=self.sidebar)

    def test_real_planner_plan_is_executed_and_replan_is_empty(self):
        """I1's real planner (wb_plan) builds the plan from the vault edit; the executor applies it; a re-plan right
        after the adoption has 0 items for the note (W7 for real)."""
        try:
            import wb_plan
        except ImportError:
            self.skipTest("wb_plan (I1) not present")
        if wb_state.get_meta(self.db, "enabled_at") is None:
            wb_state.set_meta(self.db, "enabled_at", "2026-11-17T00:00:00+07:00")      # not an INITIAL_BACKLOG edit
        note = os.path.join(self.vault, *self.rel.split("/"))
        with open(note, encoding="utf-8") as fh:
            text = fh.read()
        with open(note, "w", encoding="utf-8") as fh:
            fh.write(text.replace("alpha line", "alpha line via planner"))
        paths = wb_plan.Paths(self.work, self.vault)
        obs = {"running": True, "started_at": "2000-01-01T00:00:00+00:00"}
        ctx = wb_plan.PlanContext(paths, self.cfg, self.db, read_client=self.rc, obsidian=obs)
        plan, preview = wb_plan.build_plan(ctx)
        execs = [it for it in plan["items"] if it.get("executable")]
        self.assertEqual([(it["category"], [o["op"] for o in it["ops"]]) for it in execs], [("M", ["UPDATE"])],
                         json.dumps(plan.get("refused"), ensure_ascii=False))
        info = wb_plan.create_plan(ctx, plan, preview)
        P, sha = info["plan_id"], info["plan_sha256"]
        sel = wb_plan.Selection(items=[execs[0]["item_id"]])
        self.assertTrue(wb_state.cas_plan_status(self.db, P, "draft", "confirmed"))
        with wb_state.transaction(self.db):
            self.db.execute("INSERT INTO confirmations(plan_id, item_id, confirmed_at, os_user, tty, challenge_ok, "
                            "phrase_scope, plan_sha256) VALUES(?,?,?,?,?,1,'apply',?)",
                            (P, execs[0]["item_id"], wb_common.now_iso(), "tester", "/dev/pts/9", sha))
        rec = {"plan_id": P, "plan_sha256": sha, "challenge_ok": True, "phrase_scope": "apply",
               "selection": sel.as_dict()}
        self.fake.clear_requests()
        res = wb_apply.execute_confirmed_plan(P, sel, rec, client=self.wb, read_client=self.rc, cfg=self.cfg, db=self.db,
                                              work=self.work, vault=self.vault, blobs_dir=self.blobs, sidebar=self.sidebar)
        self.assertEqual(res["status"], "applied", json.dumps(res["w_checks"], ensure_ascii=False)[:2000])
        self.assertEqual([r["method"] for r in self.fake.writes() if r["token"] == "wb"], ["PATCH"])
        self.assertEqual(plain(self.rc.list_children(self.home)[0]), "alpha line via planner")
        ctx2 = wb_plan.PlanContext(paths, self.cfg, self.db, read_client=self.rc, obsidian=obs)
        plan2, _ = wb_plan.build_plan(ctx2)
        self.assertEqual([it for it in plan2["items"] if it.get("notion_id") == self.home], [])
        self.assertEqual(plan2.get("refused"), [])

    def test_two_consecutive_writebacks_are_adopted(self):
        import wb_refetch
        expect_base = wb_refetch.choose_base(state_dir=os.path.join(self.work, "state"))
        res = self._edit_and_apply("beta line", "beta line EDITED in Obsidian", "wb-e2e-1")
        self.assertEqual(res["status"], "applied", json.dumps({k: res[k] for k in ("w_checks", "adoption", "notes")},
                                                              ensure_ascii=False, default=str)[:3000])
        note = os.path.join(self.vault, *self.rel.split("/"))
        with open(note, encoding="utf-8") as fh:
            self.assertIn("beta line EDITED in Obsidian", fh.read())
        last = wb_state.latest_base(self.db, self.home)
        self.assertEqual(last["origin"], "WRITEBACK")
        self.assertEqual(last["sha256"], wb_common.sha256_file(note))
        with open(os.path.join(self.work, "state", "last_adopted_wb_snapshot.json")) as fh:
            first_snap = json.load(fh)["snapshot"]
        self.assertTrue(first_snap.startswith(os.path.join(self.work, "snapshots", "wb")))
        steps = res["adoption"]["steps"]
        self.assertEqual(steps["refetch"]["base"], expect_base)
        # a second write-back chains on the adopted snapshot (H: choose_base) and keeps the first edit
        res2 = self._edit_and_apply("gamma line", "gamma line second edit", "wb-e2e-2")
        self.assertEqual(res2["status"], "applied", json.dumps(res2["w_checks"], ensure_ascii=False)[:2000])
        self.assertEqual(res2["adoption"]["steps"]["refetch"]["base"], first_snap)
        with open(note, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("beta line EDITED in Obsidian", text)
        self.assertIn("gamma line second edit", text)
        self.assertEqual([plain(b) for b in self.rc.list_children(self.home)][1:],
                         ["beta line EDITED in Obsidian", "gamma line second edit"])


def _crawl_snapshot(snapmod, rc, roots_in, path):
    """Real snapshot crawl of the fake (the way refresh's snapshot.py does it), for the e2e fixtures."""
    roots = snapmod.detect_kinds(rc, roots_in)
    with open(path + ".roots.json", "w", encoding="utf-8") as fh:
        json.dump(roots, fh, ensure_ascii=False, indent=1)
    store = snapmod.Store(path)
    me = rc.me()
    store.x("INSERT OR REPLACE INTO meta VALUES('workspace_id', ?)", (me["bot"]["workspace_id"],))
    store.x("INSERT OR REPLACE INTO meta VALUES('notion_version', ?)", ("2026-03-11",))
    inv = snapmod.BlobInventory(rc, store, workers=1, hash_files=True, comments=False)
    inv.seed([r for r in roots if not r.get("in_trash")])
    inv.run()
    inv.http.close()
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.db.close()
    for sfx in ("-wal", "-shm"):
        if os.path.exists(path + sfx) and os.path.getsize(path + sfx) == 0:
            os.unlink(path + sfx)


# =============================================================================================== fix round (review)
def _rb_client(w):
    """A write-back client for rollback (trash_block enabled, like make_clients(purpose="rollback"))."""
    return WritebackClient(w.fake.secret("wb"), base_url=w.base, gate=w.gate, timeout=(2.0, 2.0), silent=True,
                           allow_block_trash=True)


def _later_iso(minutes=1):
    return (datetime.datetime.now().astimezone() + datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _api_error(status=503):
    from nmig.client import NotionAPIError
    return NotionAPIError(status, "service_unavailable", "fake outage")


class FixRoundSafetyTests(WorldCase):
    N_PAGES = 2

    def test_reconciled_append_rollback_trashes_only_its_own_blocks(self):
        """Safety finding 1: reconcile(committed) records exactly this request's new blocks; rollback trashes only
        those -- never a block an EARLIER plan appended to the same parent (even with a legacy after_json that
        lists the whole child list)."""
        w = self.w
        pid = w.pages[0]
        it1 = w.item("M-001", 0, [w.op_append(pid, 0, ["P1-block"])])
        P1, _s1, sel1, rec1 = w.plan([it1])
        self.assertEqual(w.run(P1, sel1, rec1)["status"], "applied")
        p1_block = next(b["id"] for b in w.kids(pid) if plain(b) == "P1-block")
        it2 = w.item("M-001", 0, [w.op_append(pid, 3, ["P2-block"])])
        P2, sha2, sel2, rec2 = w.plan([it2], extra={"created_at": _later_iso()})
        w.fake.inject("PATCH", rf"blocks/{pid}/children", after_commit=True, delay=3.0)
        with mock.patch.object(wb_apply, "_append_set_diff", return_value=None):
            w.run(P2, sel2, rec2)
        row = w.journal(P2)[0]
        self.assertEqual(row["state"], "unknown")
        rr = wb_apply.reconcile_journal(row["seq"], "committed", read_client=w.rc, cfg=w.cfg, db=w.db, work=w.work)
        self.assertTrue(rr["ok"], rr)
        p2_block = next(b["id"] for b in w.kids(pid) if plain(b) == "P2-block")
        self.assertEqual(rr["ids"], [p2_block])
        self.assertEqual([b["id"] for b in json.loads(w.journal(P2)[0]["after_json"])], [p2_block])
        # a row written by the old code: after_json = every child of the parent
        legacy = [w.rc.retrieve_block(b["id"]) for b in w.kids(pid)]
        with wb_state.transaction(w.db):
            w.db.execute("UPDATE journal SET after_json=? WHERE seq=?", (json.dumps(legacy), row["seq"]))
        prev = wb_apply.rollback_preview(P2, ["M-001"], read_client=w.rc, db=w.db, work=w.work)
        self.assertEqual([s["action"] for s in prev["steps"]], ["do"])
        self.assertIn(p2_block, prev["steps"][0]["reason"])                 # every block to trash is listed
        self.assertNotIn(p1_block, prev["steps"][0]["reason"])
        w.confirm(P2, sha2, ["M-001"], "rollback")
        w.fake.clear_requests()
        out = wb_apply.rollback_plan(P2, ["M-001"], w.record(P2, sha2, "rollback"), client=_rb_client(w),
                                     read_client=w.rc, cfg=w.cfg, db=w.db, work=w.work, vault=w.vault)
        self.assertEqual(out["status"], "applied", out)
        self.assertEqual([(r["method"], r["path"]) for r in w.wb_writes()], [("DELETE", f"blocks/{p2_block}")])
        self.assertFalse(w.rc.retrieve_block(p1_block)["in_trash"])

    def test_resume_of_delete_repeats_the_delete_rechecks(self):
        """Safety finding 2: apply --resume of a D item re-runs WB §6.4 (note back, pending_deletes not hold, page
        edited) -- each drift alone: 0 writes, the page stays out of the trash."""
        for drift in ("note_back", "pending_restored", "page_edited"):
            with self.subTest(drift=drift):
                w = World(1)
                try:
                    parent = w.pages[0]
                    child = w.fake.add_page("Doomed", parent=parent, children=[para("doomed body")])
                    w.fake.grant("wb", child)
                    w.cfg["whitelist_page_ids"] = [parent, child]
                    rel = "General/Page 0/Doomed.md"
                    with wb_state.transaction(w.db):
                        w.db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, detected_run, "
                                     "detected_at, status, ttl_until) VALUES(?,?,?,?,?,?,?)",
                                     (child, rel, "[]", "r", "x", "hold", "2099-01-01"))
                    page = w.rc.retrieve_page(child)
                    psha = wb_norm.page_json_sha(page, wb_norm.page_blocks_dfs(lambda b: w.rc.list_children(b), child))
                    it = {"item_id": "D-001", "category": "D", "notion_id": child, "rel_path": rel, "l_sha": None,
                          "binds": [], "ops": [{"op": "TRASH_PAGE", "target_id": child, "page_id": child,
                                                "subtree_ids": [], "page_json_sha": psha,
                                                "last_edited_time": page["last_edited_time"]}]}
                    P, sha, sel, rec = w.plan([it])
                    with mock.patch.object(wb_apply, "_send", side_effect=_Crash()):
                        with self.assertRaises(_Crash):
                            w.run(P, sel, rec)
                    self.assertEqual([r["state"] for r in w.journal(P)], ["intent"])
                    if drift == "note_back":
                        w.write_note(rel, f"---\nnotion_id: {child}\n---\nI restored this note\n")
                    elif drift == "pending_restored":
                        with wb_state.transaction(w.db):
                            w.db.execute("UPDATE pending_deletes SET status='restored' WHERE notion_id=?", (child,))
                    else:
                        w.admin.append_children(child, [req_para("new content in Notion after the plan")])
                    w.confirm(P, sha, ["D-001"], "resume")
                    w.fake.clear_requests()
                    res = w.resume(P, w.record(P, sha, "resume"))
                    self.assertEqual(w.wb_writes(), [])
                    self.assertFalse(w.fake.page(child)["in_trash"])
                    self.assertNotEqual(res["status"], "applied")
                    self.assertNotEqual(w.plan_status(P), "applying")
                finally:
                    w.close()

    def test_page_op_target_is_bound_to_the_item_page(self):
        """Safety finding 3: a TRASH_PAGE whose page_id is another page (planner defect / tampered plan) is refused."""
        w = self.w
        parent = w.pages[0]
        child = w.fake.add_page("Doomed", parent=parent, children=[para("doomed body")])
        w.fake.grant("wb", child)
        w.cfg["whitelist_page_ids"] = list(w.pages) + [child]
        with wb_state.transaction(w.db):
            w.db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, detected_run, detected_at, "
                         "status, ttl_until) VALUES(?,?,?,?,?,?,?)",
                         (child, "General/Page 0/Doomed.md", "[]", "r", "x", "hold", "2099-01-01"))
        it = {"item_id": "D-001", "category": "D", "notion_id": child, "rel_path": "General/Page 0/Doomed.md",
              "l_sha": None, "binds": [], "ops": [{"op": "TRASH_PAGE", "page_id": parent, "subtree_ids": []}]}
        P, sha, sel, rec = w.plan([it])
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "aborted")
        self.assertNoWrites()
        self.assertFalse(w.fake.page(parent)["in_trash"])
        self.assertIn("目标", res["items"]["D-001"]["reason"])
        self.assertIsNone(w.db.execute("SELECT 1 FROM trash_archive").fetchone())

    def test_w3_binds_every_request_target_to_its_item(self):
        w = self.w
        p0, p1 = w.pages
        it = w.item("M-001", 0, [w.op_update(p0, 0, "mine")])
        P, sha, sel, rec = w.plan([it])
        holder = {}

        def capture(ctx, written, **kw):
            holder["ctx"] = ctx
            return ok_adopter(ctx, written)
        res = w.run(P, sel, rec, adopter=capture)
        self.assertEqual(res["w_checks"]["W3"]["status"], "pass", res["w_checks"]["W3"])
        ctx, row = holder["ctx"], w.journal(P)[0]
        bid = row["target_id"]
        self.assertIsNone(wb_apply._send_target_problem(ctx, "PATCH", f"blocks/{bid}", row))
        other_on_page = w.kids(p0)[1]["id"]
        self.assertIsNotNone(wb_apply._send_target_problem(ctx, "PATCH", f"blocks/{other_on_page}", row))
        foreign = w.kids(p1)[0]["id"]
        self.assertIsNotNone(wb_apply._send_target_problem(ctx, "PATCH", f"blocks/{foreign}",
                                                           dict(row, target_id=foreign)))
        self.assertIsNotNone(wb_apply._send_target_problem(ctx, "PATCH", f"pages/{p1}",
                                                           dict(row, op="RENAME", target_id=p1)))
        self.assertIsNone(wb_apply._send_target_problem(ctx, "PATCH", f"pages/{p0}",
                                                        dict(row, op="RENAME", target_id=p0)))

    def test_stop_or_mode_drop_during_429_pause_prevents_the_resend(self):
        """Safety finding 5: the client's Retry-After resend is preceded by the STOP / mode check."""
        for variant in ("stop", "mode"):
            with self.subTest(variant=variant):
                w = World(1)
                try:
                    pid = w.pages[0]
                    os.makedirs(os.path.join(w.work, "config"), exist_ok=True)
                    cfgp = os.path.join(w.work, "config", "writeback.json")
                    with open(cfgp, "w") as fh:
                        json.dump(dict(w.cfg, rate_limit_total_rps=2.5), fh)
                    op = w.op_update(pid, 0, "must not be written after the stop")
                    P, sha, sel, rec = w.plan([w.item("M-001", 0, [op])])
                    w.fake.inject("PATCH", rf"blocks/{op['target_id']}$", status=429, retry_after=1)
                    stop = os.path.join(w.state, wb_apply.STOP_FILE)

                    def during_pause(_s):
                        if variant == "stop":
                            open(stop, "w").close()
                        else:
                            with open(cfgp, "w") as fh:
                                json.dump(dict(w.cfg, mode="plan", rate_limit_total_rps=2.5), fh)
                    w.wb.sleep = during_pause
                    w.fake.clear_requests()
                    res = w.run(P, sel, rec)
                    self.assertEqual([(r["method"], r["status"]) for r in w.wb_writes()], [("PATCH", 429)])
                    self.assertEqual(plain(w.kids(pid)[0]), "p0 line 0")
                    self.assertEqual(res["status"], "aborted")
                    row = w.journal(P)[0]
                    self.assertEqual(row["state"], "failed")
                    self.assertEqual(json.loads(row["committed_ids"])["code"], "stopped_before_retry")
                finally:
                    w.close()

    def test_rollback_refuses_a_page_locked_since(self):
        """Safety finding 6: rollback honours page locks like apply (the API ignores is_locked)."""
        w = self.w
        pid = w.pages[0]
        m = w.item("M-001", 0, [w.op_update(pid, 0, "U1")])
        t = w.item("T-001", 0, [{"op": "RENAME", "old_title": rt("Page 0"), "new_title": "Page Zero"}], cat="T")
        P, sha, sel, rec = w.plan([m, t])
        self.assertEqual(w.run(P, sel, rec)["status"], "applied")
        w.fake.pages[pid]["is_locked"] = True
        prev = wb_apply.rollback_preview(P, None, read_client=w.rc, db=w.db, work=w.work)
        self.assertEqual({s["action"] for s in prev["steps"]}, {"manual"})
        self.assertTrue(all("锁定" in s["reason"] for s in prev["steps"]))
        w.confirm(P, sha, ["M-001", "T-001"], "rollback")
        w.fake.clear_requests()
        out = wb_apply.rollback_plan(P, None, w.record(P, sha, "rollback"), client=_rb_client(w), read_client=w.rc,
                                     cfg=w.cfg, db=w.db, work=w.work, vault=w.vault)
        self.assertNoWrites()
        self.assertNotEqual(out["status"], "applied")
        self.assertEqual(plain(w.kids(pid)[0]), "U1")

    def test_m_item_cannot_change_a_title_or_a_read_only_property(self):
        """Safety finding 8a: titles only through T items; read-only property types never."""
        w = self.w
        pid = w.pages[0]
        db, ds = w.fake.add_database(pid, "Tasks", {"Name": "title", "Tag": {"type": "select", "options": ["a", "b"]}})
        row = w.fake.add_row(ds, "Row 1", {"Tag": {"select": {"name": "a"}}})
        w.cfg.update(whitelist_database_ids=[db], automation_free_database_ids=[db])
        rel = "General/Tasks/Row 1.md"
        w.write_note(rel, f"---\nnotion_id: {row}\n---\nrow\n")
        live = w.rc.retrieve_page(row)
        it = {"item_id": "M-001", "category": "M", "notion_id": row, "rel_path": rel, "l_sha": w.vsha(rel), "binds": [],
              "ops": [{"op": "UPDATE_PROPERTIES", "properties": {"Name": {"title": rt("Row renamed by M item")}},
                       "before": {"Name": live["properties"]["Name"]}}]}
        P, sha, sel, rec = w.plan([it])
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertNoWrites()
        self.assertIn("T 项", res["items"]["M-001"]["reason"])
        self.assertEqual(w.fake.page(row)["properties"]["Name"]["title"][0]["plain_text"], "Row 1")

    def test_x_blocks_are_never_updated_or_trashed(self):
        """Safety finding 8b: a block with a mention / coloured segment / link to a Notion page is read-only."""
        w = World(1, allow_block_trash=True)
        try:
            pid = w.pages[0]
            uid = next(iter(w.fake.users))
            variants = {
                "mention": [{"type": "text", "text": {"content": "hi "}},
                            {"type": "mention", "mention": {"type": "user", "user": {"id": uid}}}],
                "colour": [{"type": "text", "text": {"content": "red part"}, "annotations": {"color": "red"}}],
                "notion link": [{"type": "text", "text": {"content": "see",
                                                          "link": {"url": f"/{pid.replace('-', '')}"}}}]}
            for name, rich in variants.items():
                for opname in ("UPDATE", "TRASH_BLOCK"):
                    with self.subTest(x=name, op=opname):
                        bid = w.fake.add_blocks(pid, [{"type": "paragraph", "paragraph": {"rich_text": rich}}])[0]
                        b = w.rc.retrieve_block(bid)
                        op = {"op": opname, "target_id": bid, "before_norm_sha": wb_norm.block_json_sha(b)}
                        if opname == "UPDATE":
                            op["request"] = {"paragraph": {"rich_text": rt("plain overwrite")}}
                        it = w.item("M-001" if opname == "UPDATE" else "B-001", 0, [op],
                                    cat="M" if opname == "UPDATE" else "B")
                        P, sha, sel, rec = w.plan([it])
                        w.fake.clear_requests()
                        res = w.run(P, sel, rec)
                        self.assertEqual(w.wb_writes(), [])
                        self.assertIn("只读", res["items"][it["item_id"]]["reason"])
                        self.assertEqual(wb_norm.json_norm(w.rc.retrieve_block(bid)), wb_norm.json_norm(b))
        finally:
            w.close()

    def test_duplicate_item_ids_in_plan_are_refused(self):
        w = self.w
        pid = w.pages[0]
        m = w.item("M-001", 0, [w.op_update(pid, 0, "x")])
        P, sha, sel, rec = w.plan([m])
        t = w.item("M-001", 0, [{"op": "RENAME", "old_title": rt("Page 0"), "new_title": "Renamed via --items"}], cat="T")
        d = os.path.join(w.work, "reports", "writeback", P)
        with open(os.path.join(d, "plan.json"), encoding="utf-8") as fh:
            plan = json.load(fh)
        plan["items"].append(t)
        raw = json.dumps(plan, ensure_ascii=False, indent=1).encode("utf-8")
        with open(os.path.join(d, "plan.json"), "wb") as fh:
            fh.write(raw)
        sha2 = wb_common.sha256_bytes(raw)
        with wb_state.transaction(w.db):
            w.db.execute("UPDATE plans SET plan_sha256=? WHERE plan_id=?", (sha2, P))
        w.confirm(P, sha2, ["M-001"])
        w.fake.clear_requests()
        res = w.run(P, sel, dict(rec, plan_sha256=sha2))
        self.assertEqual(res["status"], "refused")
        self.assertIn("重复", res["reason"])
        self.assertNoWrites()


class FixRoundNewPageTests(WorldCase):
    N_PAGES = 2

    def _new_item(self, title="Brand New", page_idx=0, n_children=1, iid="N-001"):
        w = self.w
        rel = f"General/Page {page_idx}/{title}.md"
        sha = w.write_note(rel, f"# {title}\n\nnew text\n")
        return {"item_id": iid, "category": "N", "notion_id": None, "rel_path": rel, "l_sha": sha, "binds": [],
                "ops": [{"op": "CREATE_PAGE", "parent": {"page_id": w.pages[page_idx]}, "title": title,
                         "children": [req_para(f"c{i}") for i in range(n_children)]}]}

    def _posts(self):
        return [r for r in self.w.wb_writes() if r["method"] == "POST" and r["path"] == "pages"]

    def test_unidentified_create_is_unknown_closed_and_never_posted_again(self):
        """Safety finding 7 / correctness high (line 1972): a dropped POST pages with nothing committed no longer
        crashes; the plan is closed with a result, the other page still runs, the note is never POSTed again
        until reconcile says not_committed."""
        w = self.w
        n = self._new_item()
        m = w.item("M-001", 1, [w.op_update(w.pages[1], 0, "other page edited")])
        P, sha, sel, rec = w.plan([n, m])
        w.fake.inject("POST", r"^pages$", drop=True)
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "partially_applied", res)
        self.assertEqual(w.plan_status(P), "partially_applied")
        self.assertTrue(os.path.isfile(res["result_md"]))
        self.assertEqual(res["unknown"], ["N-001"])
        self.assertEqual(res["executed"], ["M-001"])
        self.assertEqual(len(self._posts()), 1)
        row = next(r for r in w.journal(P) if r["op"] == "CREATE_PAGE")
        self.assertEqual(row["state"], "unknown")
        m_ = json.loads(wb_state.get_meta(w.db, "created_page:" + n["rel_path"]))
        self.assertEqual((m_["state"], m_["page_id"], m_["journal_seq"]), ("unknown", None, row["seq"]))
        with open(res["result_md"], encoding="utf-8") as fh:
            self.assertIn(f"reconcile --journal {row['seq']}", fh.read())
        # a new plan for the same note: refused (mapping), and still refused by the journal row without it
        for drop_mapping in (False, True):
            if drop_mapping:
                wb_state.set_meta(w.db, "created_page:" + n["rel_path"], None)
            P2, sha2, sel2, rec2 = w.plan([n], plan_id=f"wb-t-again-{int(drop_mapping)}")
            w.fake.clear_requests()
            res2 = w.run(P2, sel2, rec2)
            self.assertEqual(self._posts(), [])
            self.assertEqual(res2["items"]["N-001"]["status"], "skipped")
            if drop_mapping:
                self.assertIn("reconcile", res2["items"]["N-001"]["reason"])
        # reconcile: nothing was created -> not_committed; then the note can be created (once)
        rr = wb_apply.reconcile_journal(row["seq"], "not_committed", read_client=w.rc, cfg=w.cfg, db=w.db, work=w.work)
        self.assertTrue(rr["ok"], rr)
        self.assertIsNone(wb_state.get_meta(w.db, "created_page:" + n["rel_path"]))
        P3, sha3, sel3, rec3 = w.plan([n], plan_id="wb-t-again-3")
        w.fake.clear_requests()
        self.assertEqual(w.run(P3, sel3, rec3)["status"], "applied")
        self.assertEqual(len(self._posts()), 1)

    def test_unknown_create_with_unreadable_parent_is_unknown_not_failed(self):
        w = self.w
        n = self._new_item()
        P, sha, sel, rec = w.plan([n])
        w.fake.inject("POST", r"^pages$", drop=True)
        orig = wb_apply._bot_pages_since
        calls = []

        def flaky(ctx, parent, since, title):
            calls.append(1)
            if len(calls) > 1:                      # the set-difference read after the POST fails
                raise _api_error()
            return orig(ctx, parent, since, title)
        with mock.patch.object(wb_apply, "_bot_pages_since", side_effect=flaky):
            res = w.run(P, sel, rec)
        self.assertEqual(res["items"]["N-001"]["status"], "unknown")
        self.assertEqual(json.loads(wb_state.get_meta(w.db, "created_page:" + n["rel_path"]))["state"], "unknown")
        self.assertNotEqual(w.plan_status(P), "applying")

    def test_crash_after_sent_on_create_then_resume_closes_the_plan(self):
        w = self.w
        n = self._new_item()
        P, sha, sel, rec = w.plan([n])
        orig = wb_apply._j_update

        def crash_on_sent(ctx, seq, **cols):
            orig(ctx, seq, **cols)
            if cols.get("state") == "sent":
                raise _Crash()
        with mock.patch.object(wb_apply, "_j_update", side_effect=crash_on_sent):
            with self.assertRaises(_Crash):
                w.run(P, sel, rec)
        self.assertEqual(w.plan_status(P), "applying")
        n0 = len(self._posts())
        w.confirm(P, sha, ["N-001"], "resume")
        res = w.resume(P, w.record(P, sha, "resume"))
        self.assertEqual(len(self._posts()), n0)                       # never resent
        self.assertEqual(res["status"], "partially_applied", res)
        self.assertEqual(w.plan_status(P), "partially_applied")
        self.assertEqual(res["unknown"], ["N-001"])
        self.assertEqual(json.loads(wb_state.get_meta(w.db, "created_page:" + n["rel_path"]))["state"], "unknown")

    def test_create_timeout_unique_reconcile_is_ledgered_read_back_and_rollbackable(self):
        """Correctness finding (line 1653): the uniquely identified page counts as written (ledger, read-back,
        after_json) and rollback previews trashing THE NEW PAGE (target = its id, not the parent)."""
        w = self.w
        n = self._new_item(n_children=2)
        P, sha, sel, rec = w.plan([n])
        w.fake.inject("POST", r"^pages$", after_commit=True, delay=3.0)
        res = w.run(P, sel, rec)
        row = w.journal(P)[0]
        self.assertEqual(row["state"], "reconciled")
        self.assertIsNotNone(row["after_json"])
        self.assertEqual(res["w_checks"]["W9"]["status"], "pass", res["w_checks"]["W9"])
        self.assertEqual(res["w_checks"]["W1"]["status"], "pass")
        new = json.loads(row["committed_ids"])["ids"][0]
        prev = wb_apply.rollback_preview(P, None, read_client=w.rc, db=w.db, work=w.work)
        self.assertEqual([(s["op"], s["target"], s["action"]) for s in prev["steps"]], [("RB_TRASH_PAGE", new, "do")])
        self.assertIn("Brand New", prev["lines"][0])

    def test_rollback_preview_names_the_new_page_not_its_parent(self):
        w = self.w
        n = self._new_item()
        P, sha, sel, rec = w.plan([n])
        res = w.run(P, sel, rec)
        new = res["created"]["N-001"]
        prev = wb_apply.rollback_preview(P, None, read_client=w.rc, db=w.db, work=w.work)
        self.assertEqual(prev["steps"][0]["target"], new)
        self.assertNotIn(w.pages[0][:8], prev["lines"][0])

    def test_new_child_page_and_append_to_the_same_parent_in_either_order(self):
        """Correctness finding (line 1184): the run's own new child page is not a remote change of the parent."""
        for order in (["M-001", "N-001"], ["N-001", "M-001"]):
            with self.subTest(order=order):
                w = World(1)
                try:
                    pid = w.pages[0]
                    rel = "General/Page 0/Child New.md"
                    sha = w.write_note(rel, "# Child New\n\nbody\n")
                    n = {"item_id": "N-001", "category": "N", "notion_id": None, "rel_path": rel, "l_sha": sha,
                         "binds": [], "ops": [{"op": "CREATE_PAGE", "parent": {"page_id": pid}, "title": "Child New",
                                               "children": [req_para("c0")]}]}
                    m = w.item("M-001", 0, [w.op_append(pid, 2, ["appended at end"])])
                    P, psha, _sel, rec = w.plan([m, n])
                    sel = w.selection(order)
                    w.fake.clear_requests()
                    res = w.run(P, sel, dict(rec, selection=sel))
                    self.assertEqual(res["status"], "applied", res["items"])
                    self.assertEqual(len(w.wb_writes()), 2)
                finally:
                    w.close()

    def test_frontmatter_skipped_makes_item_needs_manual_and_adopt_uses_plan_sha(self):
        """Safety finding 4 / correctness high (line 2411): adoption never takes the live sha of a new note."""
        w = self.w
        n = self._new_item()
        P, sha, sel, rec = w.plan([n])
        p = os.path.join(w.vault, *n["rel_path"].split("/"))
        orig = w.wb.create_page

        def create_then_user_edits(body):
            r = orig(body)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write("user typed more\n")
            return r
        w.wb.create_page = create_then_user_edits
        holder = {}

        def capture(ctx, written, **kw):
            holder["sets"] = wb_apply._refetch_sets(ctx, written)
            return ok_adopter(ctx, written)
        res = w.run(P, sel, rec, adopter=capture)
        adopt = holder["sets"][2]
        self.assertEqual(adopt[res["created"]["N-001"]]["l_sha"], n["l_sha"])          # plan L sha, not the live file
        self.assertNotEqual(adopt[res["created"]["N-001"]]["l_sha"], w.vsha(n["rel_path"]))
        self.assertEqual(res["w_checks"]["W8"]["status"], "fail")
        self.assertEqual(res["status"], "partially_applied")

    def test_adopt_entry_uses_the_sha_the_executor_wrote(self):
        w = self.w
        n = self._new_item()
        P, sha, sel, rec = w.plan([n])
        holder = {}

        def capture(ctx, written, **kw):
            with open(os.path.join(w.vault, *n["rel_path"].split("/")), "a", encoding="utf-8") as fh:
                fh.write("typed after the frontmatter write\n")          # before the adoption publish
            holder["sets"] = wb_apply._refetch_sets(ctx, written)
            return ok_adopter(ctx, written)
        res = w.run(P, sel, rec, adopter=capture)
        mapping = json.loads(wb_state.get_meta(w.db, "created_page:" + n["rel_path"]))
        adopt = holder["sets"][2][res["created"]["N-001"]]
        self.assertEqual(adopt["l_sha"], mapping["vault_sha"])
        self.assertNotEqual(adopt["l_sha"], w.vsha(n["rel_path"]))


class FixRoundResumeReconcileTests(WorldCase):
    def test_resume_keeps_items_skipped_before_the_crash(self):
        """Correctness finding (line 2890): a confirmed item skipped at the crashed apply's precheck stays in W10
        and in the result; the resumed plan is partially_applied, not applied."""
        w = World(5)
        try:
            items = [w.item(f"M-00{i + 1}", i, [w.op_update(w.pages[i], 0, f"edit {i}")]) for i in range(5)]
            P, sha, sel, rec = w.plan(items)
            w.admin.update_block(items[0]["ops"][0]["target_id"], {"paragraph": {"rich_text": rt("remote edit")}})
            orig = wb_apply._exec_step
            calls = []

            def crash_third(ctx, g, seq):
                calls.append(seq)
                if len(calls) == 3:
                    raise _Crash()
                return orig(ctx, g, seq)
            with mock.patch.object(wb_apply, "_exec_step", side_effect=crash_third):
                with self.assertRaises(_Crash):
                    w.run(P, sel, rec)
            w.confirm(P, sha, ["M-004", "M-005"], "resume")
            res = w.resume(P, w.record(P, sha, "resume"))
            self.assertEqual(res["status"], "partially_applied", res)
            self.assertEqual(res["w_checks"]["W10"]["status"], "fail")
            self.assertIn("M-001", res["w_checks"]["W10"]["detail"])
            self.assertEqual([s["item_id"] for s in res["skipped"]], ["M-001"])
            with open(res["result_md"], encoding="utf-8") as fh:
                self.assertIn("M-001", fh.read())
            self.assertEqual(len(w.wb_writes()), 4)
        finally:
            w.close()

    def test_reconcile_the_second_append_to_the_same_parent(self):
        """Correctness finding (line 3136): the child set is re-read right before each APPEND, so an earlier APPEND
        of the run does not make the second one ambiguous."""
        for truth in ("not_committed", "committed"):
            with self.subTest(truth=truth):
                w = World(1)
                try:
                    pid = w.pages[0]
                    it = w.item("M-001", 0, [w.op_append(pid, 0, ["first new"]), w.op_append(pid, 2, ["second new"])])
                    P, sha, sel, rec = w.plan([it])
                    orig = wb_apply._send
                    calls = []

                    def send(ctx, seq, fn, *args):
                        calls.append(seq)
                        if len(calls) == 2:
                            if truth == "not_committed":
                                w.fake.inject("PATCH", rf"blocks/{pid}/children", drop=True)
                            else:
                                w.fake.inject("PATCH", rf"blocks/{pid}/children", after_commit=True, delay=3.0)
                        return orig(ctx, seq, fn, *args)
                    with mock.patch.object(wb_apply, "_send", side_effect=send), \
                            mock.patch.object(wb_apply, "_append_set_diff", return_value=None):
                        w.run(P, sel, rec)
                    rows = w.journal(P)
                    self.assertEqual([(r["op"], r["state"]) for r in rows], [("APPEND", "done"), ("APPEND", "unknown")])
                    wrong = "committed" if truth == "not_committed" else "not_committed"
                    bad = wb_apply.reconcile_journal(rows[1]["seq"], wrong, read_client=w.rc, cfg=w.cfg, db=w.db,
                                                     work=w.work)
                    self.assertFalse(bad["ok"])
                    ok = wb_apply.reconcile_journal(rows[1]["seq"], truth, read_client=w.rc, cfg=w.cfg, db=w.db,
                                                    work=w.work)
                    self.assertTrue(ok["ok"], ok)
                    if truth == "committed":
                        new = next(b["id"] for b in w.kids(pid) if plain(b) == "second new")
                        self.assertEqual(ok["ids"], [new])
                finally:
                    w.close()

    def test_reconcile_committed_create_registers_the_page(self):
        """Correctness finding (line 3083): mapping, PARTIAL_CREATE, plan_items.notion_id and (with a vault) the
        sha-guarded notion_id frontmatter; audit-scope then treats the page as created by this tool."""
        w = self.w
        rel = "General/Page 0/Brand New.md"
        sha = w.write_note(rel, "# Brand New\n\nnew text\n")
        n = {"item_id": "N-001", "category": "N", "notion_id": None, "rel_path": rel, "l_sha": sha, "binds": [],
             "ops": [{"op": "CREATE_PAGE", "parent": {"page_id": w.pages[0]}, "title": "Brand New",
                      "children": [req_para("c0")]}]}
        P, psha, sel, rec = w.plan([n])
        w.fake.inject("POST", r"^pages$", after_commit=True, delay=3.0)
        orig = wb_apply._bot_pages_since
        calls = []

        def lagging_listing(ctx, parent, since, title):
            calls.append(1)
            return [] if len(calls) > 1 else orig(ctx, parent, since, title)
        with mock.patch.object(wb_apply, "_bot_pages_since", side_effect=lagging_listing):
            w.run(P, sel, rec)
        row = w.journal(P)[0]
        self.assertEqual(row["state"], "unknown")
        rr = wb_apply.reconcile_journal(row["seq"], "committed", read_client=w.rc, cfg=w.cfg, db=w.db, work=w.work,
                                        vault=w.vault)
        self.assertTrue(rr["ok"], rr)
        new = rr["ids"][0]
        m = json.loads(wb_state.get_meta(w.db, "created_page:" + rel))
        self.assertEqual((m["page_id"], m["state"]), (new, "partial"))
        self.assertEqual(w.page_state(new), "PARTIAL_CREATE")
        self.assertEqual(w.db.execute("SELECT notion_id FROM plan_items WHERE plan_id=? AND item_id='N-001'",
                                      (P,)).fetchone()[0], new)
        with open(os.path.join(w.vault, *rel.split("/")), encoding="utf-8") as fh:
            self.assertTrue(fh.read().startswith(f"---\nnotion_id: {new}\n---\n"))
        audit = wb_apply.audit_scope(client=w.wb, cfg=w.cfg, db=w.db, record=False)
        self.assertTrue(audit["ok"], audit)


class FixRoundDeleteTests(unittest.TestCase):
    def setUp(self):
        self.w = World(1, allow_block_trash=True)

    def tearDown(self):
        self.w.close()

    def test_b_item_with_vault_drift_is_skipped(self):
        """Correctness finding (line 1078): B items need the note's sha to equal the plan's L sha too."""
        w = self.w
        b = w.item("B-001", 0, [w.op_trash_block(w.pages[0], 2)], cat="B")
        P, sha, sel, rec = w.plan([b])
        w.write_note(w.rels[0], "the paragraph is back\n")
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(w.wb_writes(), [])
        self.assertIn("L sha", res["items"]["B-001"]["reason"])

    def test_trashing_the_only_child_of_a_nested_block_passes_w2(self):
        w = self.w
        pid = w.pages[0]
        par = w.fake.add_blocks(pid, [{"type": "bulleted_list_item", "bulleted_list_item": {
            "rich_text": rt("parent item"), "children": [{"type": "bulleted_list_item",
                                                          "bulleted_list_item": {"rich_text": rt("only child")}}]}}])[0]
        child = w.rc.list_children(par)[0]
        it = w.item("B-001", 0, [{"op": "TRASH_BLOCK", "target_id": child["id"],
                                  "before_norm_sha": wb_norm.block_json_sha(child)}], cat="B")
        P, sha, sel, rec = w.plan([it])
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res["w_checks"])
        self.assertEqual(res["w_checks"]["W2"]["status"], "pass")
        self.assertEqual([r["method"] for r in w.wb_writes()], ["DELETE"])

    def test_nested_list_item_delete_runs_post_order(self):
        """Correctness finding (line 1120): a list item with a child is deleted when its child is deleted in the same
        confirmed selection (child first); the parent alone stays refused."""
        w = self.w
        pid = w.pages[0]
        par = w.fake.add_blocks(pid, [{"type": "bulleted_list_item", "bulleted_list_item": {
            "rich_text": rt("parent item"), "children": [{"type": "bulleted_list_item",
                                                          "bulleted_list_item": {"rich_text": rt("child item")}}]}}])[0]
        kid = w.rc.list_children(par)[0]
        pb = w.rc.retrieve_block(par)
        op_par = {"op": "TRASH_BLOCK", "target_id": par, "before_norm_sha": wb_norm.block_json_sha(pb),
                  "has_children": True}
        op_kid = {"op": "TRASH_BLOCK", "target_id": kid["id"], "before_norm_sha": wb_norm.block_json_sha(kid)}
        # the parent alone: refused, nothing sent
        P0, s0, sel0, rec0 = w.plan([w.item("B-001", 0, [op_par], cat="B")])
        w.fake.clear_requests()
        res0 = w.run(P0, sel0, rec0)
        self.assertEqual(w.wb_writes(), [])
        self.assertIn("子块", res0["items"]["B-001"]["reason"])
        # parent first in the plan, child second: executed child -> parent
        b1 = w.item("B-001", 0, [op_par], cat="B")
        b2 = w.item("B-002", 0, [op_kid], cat="B")
        m = w.item("M-001", 0, [w.op_update(pid, 0, "edited")], binds=["B-001", "B-002"])
        P, sha, sel, rec = w.plan([m, b1, b2])
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res["items"])
        self.assertEqual([(r["method"], r["path"]) for r in w.wb_writes()],
                         [("PATCH", f"blocks/{m['ops'][0]['target_id']}"), ("DELETE", f"blocks/{kid['id']}"),
                          ("DELETE", f"blocks/{par}")])

    def test_rollback_of_a_page_delete_resets_pending_deletes(self):
        """Correctness finding (line 3294): no dangling pending_deletes=confirmed / pending_next_round after restore."""
        w = self.w
        child = w.fake.add_page("Doomed", parent=w.pages[0], children=[para("doomed body")])
        w.fake.grant("wb", child)
        w.cfg["whitelist_page_ids"] = list(w.pages) + [child]
        with wb_state.transaction(w.db):
            w.db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, detected_run, detected_at, "
                         "status, ttl_until) VALUES(?,?,?,?,?,?,?)",
                         (child, "General/Page 0/Doomed.md", "[]", "r", "x", "hold", "2099-01-01"))
        it = {"item_id": "D-001", "category": "D", "notion_id": child, "rel_path": "General/Page 0/Doomed.md",
              "l_sha": None, "ops": [{"op": "TRASH_PAGE", "subtree_ids": []}], "binds": []}
        P, sha, sel, rec = w.plan([it])
        self.assertEqual(w.run(P, sel, rec)["status"], "applied")
        w.confirm(P, sha, ["D-001"], "rollback")
        out = wb_apply.rollback_plan(P, None, w.record(P, sha, "rollback"), client=w.wb, read_client=w.rc, cfg=w.cfg,
                                     db=w.db, work=w.work, vault=w.vault)
        self.assertEqual(out["status"], "applied", out)
        self.assertFalse(w.fake.page(child)["in_trash"])
        self.assertEqual(w.db.execute("SELECT status FROM pending_deletes WHERE notion_id=?", (child,)).fetchone()[0],
                         "expired")
        self.assertEqual(w.db.execute("SELECT verify_status FROM plan_items WHERE plan_id=? AND item_id='D-001'",
                                      (P,)).fetchone()[0], "rolled_back")

    def test_delete_of_a_row_with_more_than_25_relations(self):
        """Correctness finding (line 1217): has_more properties are completed before page_json_sha, like the
        planner (wb_plan.LiveReader + page_sha_live)."""
        try:
            import wb_plan
        except ImportError:
            self.skipTest("wb_plan (I1) not present")
        w = self.w
        other = w.fake.add_page("Other root")
        targets = [w.fake.add_page(f"T{i}", parent=other) for i in range(30)]
        did, ds = w.fake.add_database(w.pages[0], "DB", {"Name": "title", "Rel": "relation"})
        row = w.fake.add_row(ds, "Row X", {"Rel": {"relation": [{"id": t} for t in targets]}},
                             children=[para("row body")])
        w.fake.grant("wb", did)
        w.cfg["whitelist_database_ids"] = [did]
        page = w.rc.retrieve_page(row)
        self.assertTrue(page["properties"]["Rel"].get("has_more"))
        lr = wb_plan.LiveReader(w.rc)
        r_sha = wb_plan.page_sha_live(lr.page(row), lr.tree(row), row)
        rel = "General/Page 0/DB/Row X.md"
        with wb_state.transaction(w.db):
            w.db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, detected_run, detected_at, "
                         "status, ttl_until) VALUES(?,?,?,?,?,?,?)", (row, rel, "[]", "r", "x", "hold", "2099-01-01"))
        it = {"item_id": "D-001", "category": "D", "notion_id": row, "rel_path": rel, "l_sha": None, "binds": [],
              "ops": [{"op": "TRASH_PAGE", "target_id": row, "page_id": row, "request": {"in_trash": True},
                       "page_json_sha": r_sha, "last_edited_time": page.get("last_edited_time"), "subtree_ids": []}]}
        P, sha, sel, rec = w.plan([it])
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res["items"])
        self.assertEqual([(r["method"], r["path"]) for r in w.wb_writes()], [("PATCH", f"pages/{row}")])


class FixRoundOutputTests(WorldCase):
    N_PAGES = 2

    def test_result_lists_a_next_step_and_a_notion_link_per_item(self):
        w = self.w
        items = [w.item(f"M-00{i + 1}", i, [w.op_update(w.pages[i], 0, f"edit {i}")]) for i in range(2)]
        P, sha, sel, rec = w.plan(items)
        w.admin.update_block(items[0]["ops"][0]["target_id"], {"paragraph": {"rich_text": rt("remote edit")}})
        with mock.patch.object(wb_apply, "INCONSISTENT_ABORT_RATIO", 0.6):
            res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "partially_applied")
        with open(res["result_md"], encoding="utf-8") as fh:
            md = fh.read()
        row = next(ln for ln in md.splitlines() if ln.startswith("| M-001 "))
        self.assertIn("writeback.py plan", row)
        self.assertIn(f"https://app.notion.com/p/{w.pages[0].replace('-', '')}", row)
        with open(os.path.join(w.vault, res["vault_record"]), encoding="utf-8") as fh:
            self.assertIn("app.notion.com/p/", fh.read())

    def test_internal_error_never_leaves_the_plan_applying(self):
        w = self.w
        for where, expect in (("_build_steps", "aborted"), ("_readback_group", "partially_applied")):
            with self.subTest(where=where):
                it = w.item("M-001", 0, [w.op_update(w.pages[0], 0, f"edit via {where}")])
                P, sha, sel, rec = w.plan([it])
                with mock.patch.object(wb_apply, where, side_effect=RuntimeError("boom")):
                    res = w.run(P, sel, rec)
                self.assertEqual(res["status"], expect, res)
                self.assertEqual(w.plan_status(P), expect)
                self.assertTrue(os.path.isfile(res["result_md"]))
                self.assertIn("内部错误", res["reason"])
                self.assertNotIn("intent", {r["state"] for r in w.journal(P)})


    def test_defect_in_the_start_checks_refuses_and_closes_the_plan(self):
        w = self.w
        it = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "never")])
        P, sha, sel, rec = w.plan([it])
        w.fake.clear_requests()
        with mock.patch.object(wb_apply, "audit_scope", side_effect=RuntimeError("boom")):
            res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "refused")
        self.assertIn("程序错误", res["reason"])
        self.assertEqual(w.plan_status(P), "aborted")
        self.assertNoWrites()


class E2EWorld:
    """Fake Notion (Home [+ child page Kid]) -> real snapshot crawl -> real export + publish into a temp vault;
    plans are written the way I1's create_plan does (plan.json bytes + plans / plan_items / confirmations rows) and
    run through execute_confirmed_plan with the REAL adoption pipeline (WB §11.4). Temp dirs only."""

    def __init__(self, children, kid=False, mode="whitelist"):
        """mode: the config mode and the plans' mode (L-18: equal). Stage 2 (whitelist) writes leaf pages only, so a
        test that writes the parent Home of the child page Kid runs in mode full."""
        self.mode = mode
        import snapshot as snapmod
        import verify_vault as vv
        self.snapmod = snapmod
        self.tmp = tempfile.mkdtemp(prefix="wbapply-e2e2-")
        self.work = os.path.join(self.tmp, "work")
        for d in ("state", "snapshots", "staging", "reports", "backup"):
            os.makedirs(os.path.join(self.work, d))
        self.state = os.path.join(self.work, "state")
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(self.vault)
        self.blobs = os.path.join(self.tmp, "blobs")
        os.makedirs(os.path.join(self.blobs, "tmp"))
        self._saved_blobs = snapmod.BLOBS
        snapmod.BLOBS = self.blobs
        try:
            self._setup(children, kid, vv)
        except BaseException:
            self.close()
            raise

    def _setup(self, children, kid, vv):
        self.fake = FakeNotion(tokens={
            "ro": {"caps": {"read", "user_info"}, "roots": {"*"}, "secret": "secret-ro-e2e3"},
            "wb": {"caps": {"read", "update", "insert"}, "roots": set(), "secret": "secret-wb-e2e4"}})
        self.fake.add_user("Alice")
        self.base_url = self.fake.start()
        self.home = self.fake.add_page("Home", children=children)
        self.kid = self.fake.add_page("Kid", parent=self.home, children=[para("kid body")]) if kid else None
        self.fake.grant("wb", self.home)
        self.gate = RateGate(os.path.join(self.tmp, "gate.json"), total_rps=1000, burst=1000)
        self.rc = make_read_client(base_url=self.base_url, token=self.fake.secret("ro"), rate=None, gate=self.gate,
                                   sleep=lambda s: None)
        roots = [{"id": self.home, "title": "Home", "teamspace": "General", "teamspace_order": 1, "root_order": 1}]
        self.sidebar = os.path.join(self.tmp, "sidebar.json")
        with open(self.sidebar, "w", encoding="utf-8") as fh:
            json.dump({"teamspace_order_required": list(vv.SPEC_TEAMSPACES), "roots": roots, "order_match": True}, fh)
        base = os.path.join(self.work, "snapshots", "snap_base.db")
        _crawl_snapshot(self.snapmod, self.rc, roots, base)
        py = [sys.executable, "-I", "-B"]
        export_db = os.path.join(self.state, "export.db")
        stg = os.path.join(self.work, "staging", "R0")
        r = subprocess.run(py + [os.path.join(TOOLS, "export_obsidian.py"), "--snapshot", base, "--sidebar",
                                 self.sidebar, "--run-id", "R0", "--immutable", "--state", export_db, "--out", stg],
                           capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        appr = os.path.join(self.tmp, "approval.md")
        with open(appr, "w") as fh:
            fh.write("test approval\n")
        r = subprocess.run(py + [os.path.join(TOOLS, "publish_vault.py"), "--run-id", "R0", "--vault", self.vault,
                                 "--state", export_db, "--report-dir", os.path.join(self.work, "reports", "R0"),
                                 "--staging-dir", stg, "--backup-dir", os.path.join(self.work, "backup", "R0"),
                                 "--wb-db", os.path.join(self.state, "writeback.db"), "--allow-other-vault",
                                 "--apply", "--approval", appr], capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        for ts in vv.SPEC_TEAMSPACES:
            os.makedirs(os.path.join(self.vault, ts), exist_ok=True)
        with open(os.path.join(self.state, "last_published_snapshot.txt"), "w") as fh:
            fh.write(base + "\n")
        with open(os.path.join(self.state, "last_published.json"), "w") as fh:
            json.dump({"run_id": "R0", "snapshot": base, "at": wb_common.now_iso()}, fh)
        with open(os.path.join(stg, "manifest.json"), encoding="utf-8") as fh:
            man = json.load(fh)
        self.rel = {v["notion_id"]: k for k, v in man.items() if v.get("notion_id") and k.endswith(".md")}
        self.db = wb_state.connect(os.path.join(self.state, "writeback.db"))
        self.wb = WritebackClient(self.fake.secret("wb"), base_url=self.base_url, gate=self.gate, timeout=(5, 20))
        self.cfg = wb_config.defaults()
        self.cfg.update(mode=self.mode, read_integration_readonly_confirmed_at="x", rate_limit_total_rps=1000,
                        whitelist_page_ids=[self.home] + ([self.kid] if self.kid else []))
        if self.kid:
            self.fake.grant("wb", self.kid)

    def path(self, rel):
        return os.path.join(self.vault, *rel.split("/"))

    def read(self, rel):
        with open(self.path(rel), encoding="utf-8") as fh:
            return fh.read()

    def write(self, rel, text):
        os.makedirs(os.path.dirname(self.path(rel)), exist_ok=True)
        with open(self.path(rel), "w", encoding="utf-8") as fh:
            fh.write(text)
        return wb_common.sha256_file(self.path(rel))

    def apply(self, items, plan_id, extra=None, **kw):
        now = datetime.datetime.now().astimezone()
        plan = {"plan_id": plan_id, "created_at": (now - datetime.timedelta(minutes=1)).isoformat(timespec="seconds"),
                "expires_at": (now + datetime.timedelta(hours=1)).isoformat(timespec="seconds"), "members": 1,
                "mode": self.cfg["mode"], "creation_state": "unlimited", "breaker": {"tripped": False},
                "items": items}
        plan.update(extra or {})
        d = os.path.join(self.work, "reports", "writeback", plan_id)
        os.makedirs(d)
        raw = json.dumps(plan, ensure_ascii=False).encode()
        with open(os.path.join(d, "plan.json"), "wb") as fh:
            fh.write(raw)
        sha = wb_common.sha256_bytes(raw)
        ids = [it["item_id"] for it in items]
        with wb_state.transaction(self.db):
            self.db.execute("INSERT INTO plans(plan_id, created_at, expires_at, plan_sha256, status) VALUES(?,?,?,?,?)",
                            (plan_id, plan["created_at"], plan["expires_at"], sha, "confirmed"))
            for it in items:
                self.db.execute("INSERT INTO plan_items(plan_id, item_id, notion_id, rel_path, category, l_sha) "
                                "VALUES(?,?,?,?,?,?)", (plan_id, it["item_id"], it.get("notion_id"), it["rel_path"],
                                                        it["category"], it.get("l_sha")))
                self.db.execute("INSERT INTO confirmations(plan_id, item_id, confirmed_at, os_user, tty, challenge_ok, "
                                "phrase_scope, plan_sha256) VALUES(?,?,?,?,?,1,'apply',?)",
                                (plan_id, it["item_id"], wb_common.now_iso(), "tester", "/dev/pts/9", sha))
        sel = World.selection(ids)
        rec = {"plan_id": plan_id, "plan_sha256": sha, "challenge_ok": True, "phrase_scope": "apply", "selection": sel}
        self.fake.clear_requests()
        return wb_apply.execute_confirmed_plan(plan_id, sel, rec, client=self.wb, read_client=self.rc, cfg=self.cfg,
                                               db=self.db, work=self.work, vault=self.vault, blobs_dir=self.blobs,
                                               sidebar=self.sidebar, **kw)

    def wb_writes(self):
        return [r for r in self.fake.writes() if r["token"] == "wb"]

    def published(self, rel):
        con = sqlite3.connect(f"file:{os.path.join(self.state, 'export.db')}?mode=ro", uri=True)
        try:
            r = con.execute("SELECT status, notion_id FROM published_files WHERE rel_path=?", (rel,)).fetchone()
        finally:
            con.close()
        return r

    def close(self):
        self.snapmod.BLOBS = self._saved_blobs
        try:
            if getattr(self, "db", None) is not None:
                self.db.close()
        finally:
            if getattr(self, "fake", None) is not None:
                self.fake.stop()
            shutil.rmtree(self.tmp, ignore_errors=True)


class FixRoundRealAdoptionTests(unittest.TestCase):
    """Review findings on W6 / adoption, end to end with the real exporter / verifier / publisher."""

    def _ok(self, res):
        detail = json.dumps({k: res.get(k) for k in ("status", "reason", "w_checks", "notes", "items")},
                            ensure_ascii=False, default=str)[:3000]
        self.assertEqual(res["status"], "applied", detail)
        for k, v in res["w_checks"].items():
            self.assertIn(v["status"], ("pass", "n/a"), (k, v))

    def test_new_page_is_adopted_with_parent_link_and_index(self):
        """W6 C04/C05 for an N item: the parent note and the generated index are republished (normal publish)."""
        e = E2EWorld([para("首页正文")], kid=True, mode="full")     # Home has a child page: not a leaf
        try:
            home_rel = e.rel[e.home]
            rel = posixpath.join(os.path.dirname(home_rel), "新页面.md")
            l_sha = e.write(rel, "新页面正文\n")
            it = {"item_id": "N-001", "category": "N", "notion_id": None, "rel_path": rel, "l_sha": l_sha, "binds": [],
                  "ops": [{"op": "CREATE_PAGE", "parent": {"page_id": e.home}, "title": "新页面",
                           "children": [req_para("新页面正文")]}]}
            res = e.apply([it], "wb-e2e-new")
            self._ok(res)
            new = res["created"]["N-001"]
            self.assertEqual(res["adoption"]["steps"]["publish_mode"], "full")
            self.assertIn(f"notion_id: {new}", e.read(rel))
            self.assertIn("新页面", e.read(home_rel))                          # the parent links its new child
            self.assertEqual(e.published(rel), ("active", new))
            with open(e.path("Notion_Obsidian/Godcreate 目录索引.md"), encoding="utf-8") as fh:
                self.assertIn("新页面", fh.read())
        finally:
            e.close()

    def test_new_page_edited_during_apply_keeps_the_users_text(self):
        """Safety finding 4 / correctness (line 2411): the user's in-apply paragraph survives; Notion's version goes
        to conflicts/ (待人工处理)."""
        e = E2EWorld([para("首页正文")], kid=True, mode="full")     # Home has a child page: not a leaf
        try:
            rel = posixpath.join(os.path.dirname(e.rel[e.home]), "新页面.md")
            l_sha = e.write(rel, "新页面正文\n")
            it = {"item_id": "N-001", "category": "N", "notion_id": None, "rel_path": rel, "l_sha": l_sha, "binds": [],
                  "ops": [{"op": "CREATE_PAGE", "parent": {"page_id": e.home}, "title": "新页面",
                           "children": [req_para("新页面正文")]}]}
            orig = wb_apply._frontmatter_writeback

            def user_types(ctx, item, pid):
                with open(e.path(rel), "a", encoding="utf-8") as fh:
                    fh.write("\n用户在执行期间新写的一段\n")
                return orig(ctx, item, pid)
            with mock.patch.object(wb_apply, "_frontmatter_writeback", side_effect=user_types):
                res = e.apply([it], "wb-e2e-new-edit")
            self.assertEqual(res["status"], "partially_applied")
            self.assertIn("用户在执行期间新写的一段", e.read(rel))
            with open(os.path.join(e.work, "reports", "writeback", "wb-e2e-new-edit", "adopt", "adopt.json")) as fh:
                self.assertEqual(json.load(fh)[res["created"]["N-001"]]["l_sha"], l_sha)
            conf = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(e.vault, "Notion_Obsidian", "conflicts"))
                    for f in fs]
            self.assertEqual(len(conf), 1, conf)
            self.assertEqual(res["adoption"]["needs_manual"], [res["created"]["N-001"]])
        finally:
            e.close()

    def test_rename_without_a_refresh_in_between_is_adopted(self):
        """Correctness finding (W6 b/d): the T item's new local path goes to the publisher as an override
        (ADOPT_RENAME), and notion_title / aliases may change."""
        e = E2EWorld([para("首页正文")], kid=True)
        try:
            old_rel, home_rel = e.rel[e.kid], e.rel[e.home]
            new_rel = posixpath.join(os.path.dirname(old_rel), "Kid2.md")
            os.rename(e.path(old_rel), e.path(new_rel))
            home = e.read(home_rel)
            e.write(home_rel, home.replace(old_rel[:-3] + "|", new_rel[:-3] + "|"))   # Obsidian's link update
            it = {"item_id": "T-001", "category": "T", "notion_id": e.kid, "rel_path": new_rel, "old_rel": old_rel,
                  "l_sha": wb_common.sha256_file(e.path(new_rel)), "binds": [],
                  "ops": [{"op": "RENAME", "target_id": e.kid, "page_id": e.kid, "title_property": "title",
                           "old_title": rt("Kid"), "new_title": "Kid2"}]}
            res = e.apply([it], "wb-e2e-rename")
            self._ok(res)
            self.assertEqual(e.published(new_rel), ("active", e.kid))
            self.assertNotEqual((e.published(old_rel) or ("",))[0], "active")
            self.assertFalse(os.path.exists(e.path(old_rel)))
            self.assertIn("notion_title: Kid2", e.read(new_rel))
        finally:
            e.close()

    def test_rename_and_body_edit_of_one_note_in_one_run_pass_w6(self):
        """L-18 fix loop 2 (C-L18-reg; also on pre-L18 code): a T and an M item of the same note applied together.
        The adopted file carries the new notion_title for both items; before, only the T item's own W6 diff allowed
        it, the M item failed W6 ('额外差异 notion_title: Kid2') and the plan ended partially_applied although every
        write and the adoption succeeded."""
        e = E2EWorld([para("首页正文")], kid=True)
        try:
            old_rel, home_rel = e.rel[e.kid], e.rel[e.home]
            new_rel = posixpath.join(os.path.dirname(old_rel), "Kid2.md")
            os.rename(e.path(old_rel), e.path(new_rel))
            e.write(home_rel, e.read(home_rel).replace(old_rel[:-3] + "|", new_rel[:-3] + "|"))   # Obsidian's link update
            text = e.read(new_rel)
            self.assertIn("kid body", text)
            self.assertIn("notion_title: Kid\n", text)
            l_sha = e.write(new_rel, text.replace("kid body", "kid body（已修改）"))
            b = e.rc.list_children(e.kid)[0]
            m = {"item_id": "M-001", "category": "M", "notion_id": e.kid, "rel_path": new_rel, "l_sha": l_sha,
                 "binds": [], "ops": [{"op": "UPDATE", "target_id": b["id"], "before_norm_sha": wb_norm.block_json_sha(b),
                                       "request": {"paragraph": {"rich_text": rt("kid body（已修改）")}}}]}
            t = {"item_id": "T-001", "category": "T", "notion_id": e.kid, "rel_path": new_rel, "old_rel": old_rel,
                 "l_sha": l_sha, "binds": [],
                 "ops": [{"op": "RENAME", "target_id": e.kid, "page_id": e.kid, "title_property": "title",
                          "old_title": rt("Kid"), "new_title": "Kid2"}]}
            res = e.apply([m, t], "wb-e2e-rename-edit")
            self._ok(res)
            self.assertEqual(sorted((r["method"], r["path"]) for r in e.wb_writes()),
                             sorted([("PATCH", f"blocks/{b['id']}"), ("PATCH", f"pages/{e.kid}")]))
            self.assertEqual(res["executed"], ["M-001", "T-001"])
            after = e.read(new_rel)
            self.assertIn("notion_title: Kid2", after)
            self.assertIn("kid body（已修改）", after)
            self.assertEqual(e.published(new_rel), ("active", e.kid))
            self.assertFalse(os.path.exists(e.path(old_rel)))
        finally:
            e.close()

    def test_numbered_list_insert_renumbered_by_notion_passes_w6(self):
        e = E2EWorld([{"type": "numbered_list_item", "numbered_list_item": {"rich_text": rt("第一")}},
                      {"type": "numbered_list_item", "numbered_list_item": {"rich_text": rt("第二")}}])
        try:
            rel = e.rel[e.home]
            text = e.read(rel)
            self.assertIn("1. 第一\n2. 第二", text)
            l_sha = e.write(rel, text.replace("1. 第一\n2. 第二", "1. 第一\n2. 新项\n2. 第二"))
            kids = e.rc.list_children(e.home)
            op = {"op": "APPEND", "parent_id": e.home, "position": {"type": "after_block", "after_block": {"id": kids[0]["id"]}},
                  "anchor_next_id": kids[1]["id"], "children": [{"object": "block", "type": "numbered_list_item",
                                                                 "numbered_list_item": {"rich_text": rt("新项")}}]}
            it = {"item_id": "M-001", "category": "M", "notion_id": e.home, "rel_path": rel, "l_sha": l_sha, "binds": [],
                  "ops": [op]}
            res = e.apply([it], "wb-e2e-list")
            self._ok(res)
            self.assertIn("1. 第一\n2. 新项\n3. 第二", e.read(rel))
        finally:
            e.close()

    def test_other_local_edits_and_holds_do_not_break_the_adoption_verify(self):
        """Correctness high (line 2672): pre-existing local_modified / hold entries (dicts) in the expected
        deviations; the held, locally edited note is left alone."""
        e = E2EWorld([para("首页正文")], kid=True, mode="full")     # Home has a child page: not a leaf
        try:
            kid_rel, home_rel = e.rel[e.kid], e.rel[e.home]
            e.write(kid_rel, e.read(kid_rel).replace("kid body", "kid body edited locally, not planned"))
            with open(os.path.join(e.state, "auto_sync_hold.json"), "w") as fh:
                json.dump({e.kid: {"reason": "test hold", "source": "post_verify", "subtree": False}}, fh)
            text = e.read(home_rel)
            l_sha = e.write(home_rel, text.replace("首页正文", "首页正文（已修改）"))
            b = e.rc.list_children(e.home)[0]
            it = {"item_id": "M-001", "category": "M", "notion_id": e.home, "rel_path": home_rel, "l_sha": l_sha,
                  "binds": [], "ops": [{"op": "UPDATE", "target_id": b["id"], "before_norm_sha": wb_norm.block_json_sha(b),
                                        "request": {"paragraph": {"rich_text": rt("首页正文（已修改）")}}}]}
            res = e.apply([it], "wb-e2e-other")
            self._ok(res)
            self.assertIn("kid body edited locally, not planned", e.read(kid_rel))
        finally:
            e.close()


# =============================================================================================== L-18 (wave 3.5)
def _crash_then(w, P, sel, rec):
    """Simulated process death right before the first send: the journal holds only intent rows."""
    with mock.patch.object(wb_apply, "_send", side_effect=_Crash()):
        try:
            w.run(P, sel, rec)
        except _Crash:
            pass
    assert {r["state"] for r in w.journal(P)} == {"intent"}, w.journal(P)
    assert w.plan_status(P) == "applying"


def _write_paths(w):
    return [(r["method"], r["path"]) for r in w.wb_writes()]


def _decision(w, P, iid):
    return w.db.execute("SELECT decision FROM plan_items WHERE plan_id=? AND item_id=?", (P, iid)).fetchone()[0]


def _after_first_update(w, action):
    """Run `action` right after the run's first block PATCH (while the later pages are still to be written)."""
    orig, done = w.wb.update_block, []

    def hook(bid, body):
        r = orig(bid, body)
        if not done:
            done.append(1)
            action()
        return r
    w.wb.update_block = hook


def _resume_after_crash(w, P, sha, sel, rec, ids, during=None):
    """Crash before the first send, then apply --resume; `during` runs right after the resume's first PATCH."""
    _crash_then(w, P, sel, rec)
    w.confirm(P, sha, ids, "resume")
    if during is not None:
        _after_first_update(w, during)
    w.fake.clear_requests()
    return w.resume(P, w.record(P, sha, "resume"))


def _intent_left(w, P, iid):
    return [r["seq"] for r in w.journal(P) if r["item_id"] == iid and r["state"] == "intent"]


class L18LeafTests(unittest.TestCase):
    """L-18 (a): in stage 2 (mode whitelist) apply re-checks LIVE, right before the page's writes, that the target page
    is a leaf (no child_page / child_database at any depth): M / B / T pages, D targets and N parents."""

    def _skipped_not_leaf(self, w, P, res, iid):
        r = res["items"][iid]
        self.assertEqual(r["status"], "skipped", r)
        self.assertTrue(r["reason"].startswith("NOT_LEAF"), r)
        self.assertEqual(r.get("conflict"), "NOT_LEAF")
        self.assertEqual(_decision(w, P, iid), "skipped_precheck")

    def test_page_that_gained_a_child_page_after_planning_is_refused(self):
        for cat in ("M", "B", "T"):
            with self.subTest(cat=cat):
                w = World(2, allow_block_trash=True)
                try:
                    p0, p1 = w.pages
                    ops = {"M": [w.op_update(p0, 0, "must not be written")], "B": [w.op_trash_block(p0, 2)],
                           "T": [{"op": "RENAME", "old_title": rt("Page 0"), "new_title": "Renamed"}]}[cat]
                    target = w.item(f"{cat}-001", 0, ops, cat=cat)
                    other = w.item("M-002", 1, [w.op_update(p1, 0, "fine")])
                    P, sha, sel, rec = w.plan([target, other])
                    sub = w.fake.add_page("Sub page", parent=p0)          # created in Notion after the plan
                    w.cfg["whitelist_page_ids"].append(sub)               # (audit-scope stays clean)
                    w.fake.clear_requests()
                    with mock.patch.object(wb_apply, "INCONSISTENT_ABORT_RATIO", 0.6):
                        res = w.run(P, sel, rec)
                    self.assertEqual(_write_paths(w), [("PATCH", f"blocks/{other['ops'][0]['target_id']}")])
                    self._skipped_not_leaf(w, P, res, f"{cat}-001")
                    self.assertEqual(res["items"]["M-002"]["status"], "executed")
                    self.assertEqual([plain(b) for b in w.kids(p0)][:3], ["p0 line 0", "p0 line 1", "p0 line 2"])
                    self.assertEqual(w.fake.page(p0)["properties"]["title"]["title"][0]["plain_text"], "Page 0")
                finally:
                    w.close()

    def test_child_database_at_depth_counts_and_full_mode_has_no_leaf_rule(self):
        for mode in ("whitelist", "full"):
            with self.subTest(mode=mode):
                w = World(1)
                try:
                    p0 = w.pages[0]
                    w.cfg["mode"] = mode
                    toggle = w.admin.append_children(p0, [{"object": "block", "type": "toggle", "toggle": {
                        "rich_text": rt("folded"), "children": [req_para("inside")]}}]).committed_ids[0]
                    it = w.item("M-001", 0, [w.op_update(p0, 0, "edited")])
                    P, sha, sel, rec = w.plan([it], extra={"mode": mode})
                    did, _ds = w.fake.add_database(toggle, "Nested DB", {"Name": "title"})   # inline DB in the toggle
                    w.cfg["whitelist_database_ids"] = [did]
                    w.fake.clear_requests()
                    res = w.run(P, sel, rec)
                    if mode == "whitelist":
                        self.assertNoWritesIn(w)
                        self._skipped_not_leaf(w, P, res, "M-001")
                        self.assertEqual(plain(w.kids(p0)[0]), "p0 line 0")
                    else:
                        self.assertEqual(res["status"], "applied", res["items"])
                        self.assertEqual(_write_paths(w), [("PATCH", f"blocks/{it['ops'][0]['target_id']}")])
                finally:
                    w.close()

    def assertNoWritesIn(self, w):
        self.assertEqual(w.wb_writes(), [], _write_paths(w))

    def test_delete_target_that_gained_a_child_page_is_refused(self):
        w = World(1)
        try:
            p0 = w.pages[0]
            child = w.fake.add_page("Doomed", parent=p0, children=[para("doomed body")])
            w.fake.grant("wb", child)
            w.cfg["whitelist_page_ids"] = [p0, child]
            rel = "General/Page 0/Doomed.md"
            with wb_state.transaction(w.db):
                w.db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, detected_run, detected_at, "
                             "status, ttl_until) VALUES(?,?,?,?,?,?,?)", (child, rel, "[]", "r", "x", "hold", "2099-01-01"))
            it = {"item_id": "D-001", "category": "D", "notion_id": child, "rel_path": rel, "l_sha": None, "binds": [],
                  "ops": [{"op": "TRASH_PAGE", "target_id": child, "page_id": child, "subtree_ids": []}]}
            P, sha, sel, rec = w.plan([it])
            w.cfg["whitelist_page_ids"].append(w.fake.add_page("Grandchild", parent=child))
            w.fake.clear_requests()
            res = w.run(P, sel, rec)
            self.assertNoWritesIn(w)
            self.assertFalse(w.fake.page(child)["in_trash"])
            self._skipped_not_leaf(w, P, res, "D-001")
            self.assertIsNone(w.db.execute("SELECT 1 FROM trash_archive").fetchone())
        finally:
            w.close()

    def test_new_page_under_a_parent_that_gained_a_child_page_is_not_created(self):
        w = World(1)
        try:
            p0 = w.pages[0]
            rel = "General/Page 0/Brand New.md"
            sha = w.write_note(rel, "# Brand New\n\nnew text\n")
            it = {"item_id": "N-001", "category": "N", "notion_id": None, "rel_path": rel, "l_sha": sha, "binds": [],
                  "ops": [{"op": "CREATE_PAGE", "parent": {"page_id": p0}, "title": "Brand New",
                           "children": [req_para("new text")]}]}
            P, sha_p, sel, rec = w.plan([it])
            w.cfg["whitelist_page_ids"].append(w.fake.add_page("Someone else's page", parent=p0))
            w.fake.clear_requests()
            res = w.run(P, sel, rec)
            self.assertNoWritesIn(w)
            self._skipped_not_leaf(w, P, res, "N-001")
            self.assertIsNone(wb_state.get_meta(w.db, "created_page:" + rel))
        finally:
            w.close()

    def test_resume_rechecks_the_leaf_rule(self):
        w = World(1)
        try:
            p0 = w.pages[0]
            it = w.item("M-001", 0, [w.op_update(p0, 0, "U1")])
            P, sha, sel, rec = w.plan([it])
            _crash_then(w, P, sel, rec)
            w.cfg["whitelist_page_ids"].append(w.fake.add_page("Appeared after the crash", parent=p0))
            w.confirm(P, sha, ["M-001"], "resume")
            w.fake.clear_requests()
            res = w.resume(P, w.record(P, sha, "resume"))
            self.assertNoWritesIn(w)
            self.assertNotEqual(res["status"], "applied")
            self.assertTrue(res["items"]["M-001"]["reason"].startswith("NOT_LEAF"), res["items"])
            self.assertEqual(plain(w.kids(p0)[0]), "p0 line 0")
            self.assertNotEqual(w.plan_status(P), "applying")
        finally:
            w.close()

    def test_resume_rechecks_the_leaf_rule_before_each_later_page(self):
        """Fix loop 1 (L-18 a in resume): a child page that appears under page 1 while page 0 is written by
        apply --resume stops page 1 before its PATCH (the resume's first precheck ran before any write)."""
        for case in ("child_page_appears", "nothing_changes"):
            with self.subTest(case=case):
                w = World(2)
                try:
                    p0, p1 = w.pages
                    a = w.item("M-001", 0, [w.op_update(p0, 0, "r0")], page_kind="page")
                    b = w.item("M-002", 1, [w.op_update(p1, 0, "r1")], page_kind="page")
                    P, sha, sel, rec = w.plan([a, b])

                    def add():
                        w.cfg["whitelist_page_ids"].append(w.fake.add_page("Late child", parent=p1))
                    res = _resume_after_crash(w, P, sha, sel, rec, ["M-001", "M-002"],
                                              add if case == "child_page_appears" else None)
                    pa, pb = ("PATCH", f"blocks/{a['ops'][0]['target_id']}"), ("PATCH", f"blocks/{b['ops'][0]['target_id']}")
                    self.assertEqual(res["items"]["M-001"]["status"], "executed", res["items"])
                    if case == "nothing_changes":       # the per-page re-check does not disturb a clean resume
                        self.assertEqual(_write_paths(w), [pa, pb])
                        self.assertEqual(res["status"], "applied", res["items"])
                        continue
                    self.assertEqual(_write_paths(w), [pa])
                    r = res["items"]["M-002"]
                    self.assertEqual(r["status"], "skipped", r)
                    self.assertTrue(r["reason"].startswith("NOT_LEAF"), r)
                    self.assertEqual(_decision(w, P, "M-002"), "skipped_precheck")
                    self.assertEqual(_intent_left(w, P, "M-002"), [])           # its intent rows are cancelled
                    self.assertNotEqual(res["status"], "applied")
                    self.assertIsNone(w.page_state(p1))                         # nothing sent: not BLOCKED
                    self.assertEqual(plain(w.kids(p1)[0]), "p1 line 0")
                finally:
                    w.close()

    def test_resume_does_not_trash_a_delete_target_that_gained_a_child_during_the_run(self):
        """Fix loop 1, D variant: the page a person adds under the delete target while apply --resume writes the
        earlier page must not go to the trash with it."""
        w = World(2)
        try:
            p0, p1 = w.pages
            child = w.fake.add_page("Doomed", parent=p1, children=[para("doomed body")])
            w.fake.grant("wb", child)
            w.cfg["whitelist_page_ids"] = [p0, p1, child]
            rel = "General/Page 1/Doomed.md"
            with wb_state.transaction(w.db):
                w.db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, detected_run, detected_at, "
                             "status, ttl_until) VALUES(?,?,?,?,?,?,?)", (child, rel, "[]", "r", "x", "hold", "2099-01-01"))
            m = w.item("M-001", 0, [w.op_update(p0, 0, "r0")], page_kind="page")
            d = {"item_id": "D-001", "category": "D", "notion_id": child, "rel_path": rel, "l_sha": None, "binds": [],
                 "ops": [{"op": "TRASH_PAGE", "target_id": child, "page_id": child, "subtree_ids": []}]}
            P, sha, sel, rec = w.plan([m, d])
            added = []

            def add():
                added.append(w.fake.add_page("Human's new child", parent=child))
                w.cfg["whitelist_page_ids"].append(added[-1])
            res = _resume_after_crash(w, P, sha, sel, rec, ["M-001", "D-001"], add)
            self.assertEqual(_write_paths(w), [("PATCH", f"blocks/{m['ops'][0]['target_id']}")])
            self.assertFalse(w.fake.page(child)["in_trash"])
            self.assertFalse(w.fake.page(added[0])["in_trash"])
            r = res["items"]["D-001"]
            self.assertEqual(r["status"], "skipped", r)
            self.assertTrue(r["reason"].startswith("NOT_LEAF"), r)
            self.assertEqual(_intent_left(w, P, "D-001"), [])
            self.assertNotEqual(res["status"], "applied")
            self.assertIsNone(w.db.execute("SELECT 1 FROM trash_archive").fetchone())
            self.assertEqual(w.db.execute("SELECT status FROM pending_deletes WHERE notion_id=?", (child,)).fetchone()[0],
                             "hold")
        finally:
            w.close()


class L18ModeTests(unittest.TestCase):
    """L-18 (b): a plan runs only under the mode it was built in -- refused with 0 writes at the start, and the run
    stops before the next write when the mode changes while it runs."""

    def test_plan_mode_differs_from_the_current_mode(self):
        for plan_mode, cfg_mode in (("whitelist", "full"), ("full", "whitelist"), (None, "whitelist"), ("plan", "full")):
            with self.subTest(plan=plan_mode, current=cfg_mode):
                w = World(1)
                try:
                    w.cfg["mode"] = cfg_mode
                    it = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "never written")])
                    P, sha, sel, rec = w.plan([it], extra={"mode": plan_mode})
                    w.fake.clear_requests()
                    res = w.run(P, sel, rec)
                    self.assertEqual(res["status"], "refused", res)
                    self.assertIn("模式", res["reason"])
                    self.assertIn(cfg_mode, res["reason"])
                    self.assertEqual(w.wb_writes(), [])
                    self.assertEqual(w.journal(P), [])
                    self.assertEqual(w.plan_status(P), "aborted")
                    self.assertEqual(plain(w.kids(w.pages[0])[0]), "p0 line 0")
                finally:
                    w.close()

    def test_resume_refused_while_the_mode_differs_then_allowed_in_the_plan_mode(self):
        w = World(1)
        try:
            p0 = w.pages[0]
            it = w.item("M-001", 0, [w.op_update(p0, 0, "U1")])
            P, sha, sel, rec = w.plan([it])
            _crash_then(w, P, sel, rec)
            w.confirm(P, sha, ["M-001"], "resume")
            w.cfg["mode"] = "full"
            w.fake.clear_requests()
            res = w.resume(P, w.record(P, sha, "resume"))
            self.assertEqual(res["status"], "refused", res)
            self.assertIn("模式", res["reason"])
            self.assertEqual(w.wb_writes(), [])
            self.assertEqual(w.plan_status(P), "applying")
            self.assertEqual({r["state"] for r in w.journal(P)}, {"intent"})
            w.cfg["mode"] = "whitelist"
            res = w.resume(P, w.record(P, sha, "resume"))
            self.assertEqual(res["status"], "applied", res)
            self.assertEqual(_write_paths(w), [("PATCH", f"blocks/{it['ops'][0]['target_id']}")])
        finally:
            w.close()

    def test_mode_change_during_the_run_stops_before_the_next_write(self):
        """whitelist -> full (still a write mode) while page 0 is written: page 1 is never written."""
        w = World(2)
        try:
            os.makedirs(os.path.join(w.work, "config"))
            cfgp = os.path.join(w.work, "config", "writeback.json")
            good = dict(w.cfg, rate_limit_total_rps=2.5)
            with open(cfgp, "w") as fh:
                json.dump(good, fh)
            items = [w.item(f"M-{i + 1:03d}", i, [w.op_update(w.pages[i], 0, f"n{i}")]) for i in range(2)]
            P, sha, sel, rec = w.plan(items)
            orig = w.wb.update_block

            def then_upgrade(bid, body):
                r = orig(bid, body)
                with open(cfgp, "w") as fh:
                    json.dump(dict(good, mode="full"), fh)
                return r
            w.wb.update_block = then_upgrade
            w.fake.clear_requests()
            res = w.run(P, sel, rec)
            self.assertEqual(res["status"], "aborted", res)
            self.assertEqual(len(w.wb_writes()), 1)
            self.assertEqual(plain(w.kids(w.pages[1])[0]), "p1 line 0")
            self.assertIn("模式", res["reason"])
            self.assertNotEqual(res["items"]["M-002"]["status"], "executed")
        finally:
            w.close()


class L18HoldTests(unittest.TestCase):
    """L-18 (c): before each page's writes, auto_sync_hold.json and the open holds of writeback.db are read again;
    an object held since the plan (also through a subtree hold of an ancestor) is skipped."""

    def _hold(self, w, holds):
        with open(os.path.join(w.state, "auto_sync_hold.json"), "w", encoding="utf-8") as fh:
            if isinstance(holds, str):
                fh.write(holds)
            else:
                json.dump(holds, fh)

    def _assert_held(self, w, P, res, iid):
        r = res["items"][iid]
        self.assertEqual(r["status"], "skipped", r)
        self.assertTrue(r["reason"].startswith("AUTO_SYNC_HOLD"), r)
        self.assertEqual(r.get("conflict"), "AUTO_SYNC_HOLD")
        self.assertEqual(_decision(w, P, iid), "skipped_precheck")

    def test_hold_appearing_after_planning_skips_only_that_page(self):
        w = World(2)
        try:
            p0, p1 = w.pages
            a = w.item("M-001", 0, [w.op_update(p0, 0, "held, never written")])
            b = w.item("M-002", 1, [w.op_update(p1, 0, "written")])
            P, sha, sel, rec = w.plan([a, b])
            self._hold(w, {p0: {"reason": "post_verify_failed", "source": "post_verify", "since": "now"}})
            w.fake.clear_requests()
            with mock.patch.object(wb_apply, "INCONSISTENT_ABORT_RATIO", 0.6):
                res = w.run(P, sel, rec)
            self.assertEqual(_write_paths(w), [("PATCH", f"blocks/{b['ops'][0]['target_id']}")])
            self._assert_held(w, P, res, "M-001")
            self.assertIn("post_verify_failed", res["items"]["M-001"]["reason"])
            self.assertEqual(res["items"]["M-002"]["status"], "executed")
            self.assertEqual(plain(w.kids(p0)[0]), "p0 line 0")
        finally:
            w.close()

    def _grand(self, w):
        """Page 0 > Kid > Grand (all whitelisted); returns (kid, grand, M item on Grand)."""
        p0 = w.pages[0]
        kid = w.fake.add_page("Kid", parent=p0, children=[para("kid body")])
        grand = w.fake.add_page("Grand", parent=kid, children=[para("grand body")])
        for x in (kid, grand):
            w.fake.grant("wb", x)
        w.cfg["whitelist_page_ids"] = list(w.pages) + [kid, grand]
        rel = "General/Page 0/Kid/Grand.md"
        l_sha = w.write_note(rel, f"---\nnotion_id: {grand}\n---\ngrand body edited\n")
        it = {"item_id": "M-001", "category": "M", "notion_id": grand, "rel_path": rel, "l_sha": l_sha, "binds": [],
              "page_kind": "page", "ops": [w.op_update(grand, 0, "grand body edited")]}
        return kid, grand, it

    def _objects_map(self, w, rows):
        con = sqlite3.connect(os.path.join(w.state, "export.db"))
        try:
            con.execute("CREATE TABLE objects_map(notion_id TEXT PRIMARY KEY, kind TEXT, rel_path TEXT, parent_id TEXT, "
                        "title TEXT, status TEXT, name TEXT)")
            con.executemany("INSERT INTO objects_map(notion_id, kind, parent_id, status) VALUES(?,?,?,?)",
                            [(n, "page", p, "active") for n, p in rows])
            con.commit()
        finally:
            con.close()

    def test_subtree_hold_of_an_ancestor_through_the_parent_chain(self):
        cases = [("p0_subtree_objects_map", True, True, True), ("p0_note_only", False, True, False),
                 ("kid_subtree_live_parent", True, False, True)]
        for name, subtree, use_map, held in cases:
            with self.subTest(case=name):
                w = World(1)
                try:
                    p0 = w.pages[0]
                    kid, grand, it = self._grand(w)
                    P, sha, sel, rec = w.plan([it])
                    if use_map:
                        self._objects_map(w, [(kid, p0), (grand, kid), (p0, None)])
                    target = p0 if name.startswith("p0") else kid
                    self._hold(w, {target: {"reason": "source_mass_change", "source": "pause", "subtree": subtree}})
                    w.fake.clear_requests()
                    res = w.run(P, sel, rec)
                    if held:
                        self.assertEqual(w.wb_writes(), [], _write_paths(w))
                        self._assert_held(w, P, res, "M-001")
                        self.assertIn(target[:8], res["items"]["M-001"]["reason"])
                        self.assertEqual(plain(w.kids(grand)[0]), "grand body")
                    else:
                        self.assertEqual(res["status"], "applied", res["items"])
                        self.assertEqual(_write_paths(w), [("PATCH", f"blocks/{it['ops'][0]['target_id']}")])
                finally:
                    w.close()

    def test_pending_delete_hold_on_the_object_or_an_ancestor(self):
        for case in ("own_pending", "ancestor_pending"):
            with self.subTest(case=case):
                w = World(1)
                try:
                    p0 = w.pages[0]
                    kid, grand, it = self._grand(w)
                    P, sha, sel, rec = w.plan([it])
                    with wb_state.transaction(w.db):
                        w.db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, detected_run, "
                                     "detected_at, status, ttl_until) VALUES(?,?,?,?,?,?,?)",
                                     (grand if case == "own_pending" else kid, "x.md", "[]", "r", "x", "hold",
                                      "2099-01-01"))
                    w.fake.clear_requests()
                    res = w.run(P, sel, rec)
                    self.assertEqual(w.wb_writes(), [], _write_paths(w))
                    self._assert_held(w, P, res, "M-001")
                    self.assertEqual(plain(w.kids(grand)[0]), "grand body")
                finally:
                    w.close()

    def _doomed(self, w):
        p0 = w.pages[0]
        child = w.fake.add_page("Doomed", parent=p0, children=[para("doomed body")])
        w.fake.grant("wb", child)
        w.cfg["whitelist_page_ids"] = [p0, child]
        rel = "General/Page 0/Doomed.md"
        with wb_state.transaction(w.db):
            w.db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, detected_run, detected_at, "
                         "status, ttl_until) VALUES(?,?,?,?,?,?,?)", (child, rel, "[]", "r", "x", "hold", "2099-01-01"))
        it = {"item_id": "D-001", "category": "D", "notion_id": child, "rel_path": rel, "l_sha": None, "binds": [],
              "ops": [{"op": "TRASH_PAGE", "target_id": child, "page_id": child, "subtree_ids": []}]}
        return child, it

    def test_delete_keeps_its_own_deletion_hold_but_no_other_hold(self):
        for reason, executes in (("pending_delete_local_mass_delete", True), ("post_verify_failed", False)):
            with self.subTest(reason=reason):
                w = World(1)
                try:
                    child, it = self._doomed(w)
                    P, sha, sel, rec = w.plan([it])
                    self._hold(w, {child: {"reason": reason, "source": "pause"}})
                    w.fake.clear_requests()
                    res = w.run(P, sel, rec)
                    if executes:
                        self.assertEqual(res["status"], "applied", res["items"])
                        self.assertEqual(_write_paths(w), [("PATCH", f"pages/{child}")])
                    else:
                        self.assertEqual(w.wb_writes(), [], _write_paths(w))
                        self._assert_held(w, P, res, "D-001")
                        self.assertFalse(w.fake.page(child)["in_trash"])
                finally:
                    w.close()

    def test_new_page_under_a_held_or_blocked_parent_is_not_created(self):
        for case in ("note_only_hold", "pending_delete", "blocked"):
            with self.subTest(case=case):
                w = World(1)
                try:
                    p0 = w.pages[0]
                    rel = "General/Page 0/Brand New.md"
                    sha = w.write_note(rel, "# Brand New\n\nnew text\n")
                    it = {"item_id": "N-001", "category": "N", "notion_id": None, "rel_path": rel, "l_sha": sha,
                          "binds": [], "ops": [{"op": "CREATE_PAGE", "parent": {"page_id": p0}, "title": "Brand New",
                                                "children": [req_para("new text")]}]}
                    P, sha_p, sel, rec = w.plan([it])
                    if case == "note_only_hold":
                        self._hold(w, {p0: {"reason": "sync_conflict_copy", "source": "scan", "subtree": False}})
                    else:
                        with wb_state.transaction(w.db):
                            if case == "blocked":
                                w.db.execute("INSERT INTO page_state(notion_id, state, reason, since) VALUES(?,?,?,?)",
                                             (p0, "BLOCKED", "earlier write-back", "x"))
                            else:
                                w.db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, "
                                             "detected_run, detected_at, status, ttl_until) VALUES(?,?,?,?,?,?,?)",
                                             (p0, w.rels[0], "[]", "r", "x", "hold", "2099-01-01"))
                    w.fake.clear_requests()
                    res = w.run(P, sel, rec)
                    self.assertEqual(w.wb_writes(), [], _write_paths(w))
                    self._assert_held(w, P, res, "N-001")
                    self.assertIsNone(wb_state.get_meta(w.db, "created_page:" + rel))
                finally:
                    w.close()

    def test_blocked_parent_does_not_stop_an_independent_child_page(self):
        """A BLOCKED page (an earlier write's outcome unknown) is refused itself, but its child page is a separate
        page: it is still written (apply --resume after the parent crashed, W3 r5)."""
        w = World(1)
        try:
            kid, grand, it = self._grand(w)
            with wb_state.transaction(w.db):
                w.db.execute("INSERT INTO page_state(notion_id, state, reason, since) VALUES(?,?,?,?)",
                             (kid, "BLOCKED", "earlier write-back", "x"))
            P, sha, sel, rec = w.plan([it])
            w.fake.clear_requests()
            res = w.run(P, sel, rec)
            self.assertEqual(res["items"]["M-001"]["status"], "executed", res["items"])
            self.assertEqual(_write_paths(w), [("PATCH", f"blocks/{it['ops'][0]['target_id']}")])
        finally:
            w.close()

    def test_unreadable_hold_file_writes_nothing(self):
        w = World(1)
        try:
            it = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "never written")])
            P, sha, sel, rec = w.plan([it])
            self._hold(w, '{"truncated": ')
            w.fake.clear_requests()
            res = w.run(P, sel, rec)
            self.assertEqual(w.wb_writes(), [])
            self._assert_held(w, P, res, "M-001")
            self.assertIn("不可读", res["items"]["M-001"]["reason"])
        finally:
            w.close()

    def test_hold_appearing_between_crash_and_resume(self):
        w = World(1)
        try:
            p0 = w.pages[0]
            it = w.item("M-001", 0, [w.op_update(p0, 0, "U1")])
            P, sha, sel, rec = w.plan([it])
            _crash_then(w, P, sel, rec)
            self._hold(w, {p0: {"reason": "post_verify_failed", "source": "post_verify"}})
            w.confirm(P, sha, ["M-001"], "resume")
            w.fake.clear_requests()
            res = w.resume(P, w.record(P, sha, "resume"))
            self.assertEqual(w.wb_writes(), [])
            self.assertNotEqual(res["status"], "applied")
            self.assertTrue(res["items"]["M-001"]["reason"].startswith("AUTO_SYNC_HOLD"), res["items"])
            self.assertEqual(plain(w.kids(p0)[0]), "p0 line 0")
        finally:
            w.close()

    def test_resume_rechecks_holds_before_each_later_page(self):
        """Fix loop 1 (L-18 c in resume): a hold of page 1 that appears while apply --resume writes page 0
        (auto_sync_hold.json, or a deletion hold in writeback.db) stops page 1 before its PATCH."""
        for case in ("auto_hold", "pending_delete_hold"):
            with self.subTest(case=case):
                w = World(2)
                try:
                    p0, p1 = w.pages
                    a = w.item("M-001", 0, [w.op_update(p0, 0, "r0")], page_kind="page")
                    b = w.item("M-002", 1, [w.op_update(p1, 0, "r1")], page_kind="page")
                    P, sha, sel, rec = w.plan([a, b])

                    def add():
                        if case == "auto_hold":
                            self._hold(w, {p1: {"reason": "post_verify_failed", "source": "post_verify"}})
                        else:
                            with wb_state.transaction(w.db):
                                w.db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, detected_run, "
                                             "detected_at, status, ttl_until) VALUES(?,?,?,?,?,?,?)",
                                             (p1, w.rels[1], "[]", "r", "x", "hold", "2099-01-01"))
                    res = _resume_after_crash(w, P, sha, sel, rec, ["M-001", "M-002"], add)
                    self.assertEqual(_write_paths(w), [("PATCH", f"blocks/{a['ops'][0]['target_id']}")])
                    self.assertEqual(res["items"]["M-001"]["status"], "executed", res["items"])
                    r = res["items"]["M-002"]
                    self.assertEqual(r["status"], "skipped", r)
                    self.assertTrue(r["reason"].startswith("AUTO_SYNC_HOLD"), r)
                    self.assertEqual(_decision(w, P, "M-002"), "skipped_precheck")
                    self.assertEqual(_intent_left(w, P, "M-002"), [])
                    self.assertNotEqual(res["status"], "applied")
                    self.assertEqual(plain(w.kids(p1)[0]), "p1 line 0")
                finally:
                    w.close()

    def test_hold_or_leaf_skip_skips_only_its_own_page(self):
        """Lead decision, L-18 fix loop 2 (C-L18-reg over-blocking finding): an AUTO_SYNC_HOLD / NOT_LEAF skip no
        longer counts toward the WB §5 "> 20 % of the selected pages inconsistent -> the round aborts" rule. The
        planner drops the note's own sync_conflict_copy / pending_delete* hold and lists the note as executable;
        before, that one note aborted every plan of up to 4 pages with 0 writes, the clean notes included. Flips
        the fix-loop-1 pin test_hold_skips_count_toward_the_inconsistent_pages_abort (1 held page of 4)."""
        for case in ("stale_scan_hold", "own_pending_delete_hold", "own_pending_deletes_row", "hold_1_of_4",
                     "not_leaf"):
            with self.subTest(case=case):
                n = 4 if case == "hold_1_of_4" else 2
                w = World(n)
                try:
                    held = w.pages[0]
                    if case == "stale_scan_hold":       # already there when the plan was made (planner ignores it)
                        self._hold(w, {held: {"reason": "sync_conflict_copy", "source": "scan", "subtree": False}})
                    elif case == "own_pending_delete_hold":
                        self._hold(w, {held: {"reason": "pending_delete", "source": "pending_delete",
                                              "subtree": True}})
                    items = [w.item(f"M-{i + 1:03d}", i, [w.op_update(w.pages[i], 0, f"n{i}")], page_kind="page")
                             for i in range(n)]
                    P, sha, sel, rec = w.plan(items)
                    if case == "own_pending_deletes_row":
                        with wb_state.transaction(w.db):
                            w.db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, detected_run, "
                                         "detected_at, status, ttl_until) VALUES(?,?,?,?,?,?,?)",
                                         (held, w.rels[0], "[]", "r", "x", "hold", "2099-01-01"))
                    elif case == "hold_1_of_4":         # appears after planning (post_verify)
                        self._hold(w, {held: {"reason": "post_verify_failed", "source": "post_verify"}})
                    elif case == "not_leaf":            # a child page created in Notion after the plan
                        w.cfg["whitelist_page_ids"].append(w.fake.add_page("Sub page", parent=held))
                    w.fake.clear_requests()
                    res = w.run(P, sel, rec)
                    detail = json.dumps({k: res.get(k) for k in ("status", "reason", "items")}, ensure_ascii=False,
                                        default=str)[:2000]
                    self.assertEqual(res["status"], "partially_applied", detail)
                    self.assertEqual(w.plan_status(P), "partially_applied")
                    self.assertNotIn("20%", res.get("reason") or "", detail)
                    self.assertEqual(sorted(_write_paths(w)),
                                     sorted(("PATCH", f"blocks/{it['ops'][0]['target_id']}") for it in items[1:]))
                    if case == "not_leaf":
                        r = res["items"]["M-001"]
                        self.assertEqual((r["status"], r.get("conflict")), ("skipped", "NOT_LEAF"), r)
                        self.assertEqual(_decision(w, P, "M-001"), "skipped_precheck")
                    else:
                        self._assert_held(w, P, res, "M-001")
                    self.assertEqual([res["items"][it["item_id"]]["status"] for it in items[1:]],
                                     ["executed"] * (n - 1), detail)
                    self.assertEqual(plain(w.kids(held)[0]), "p0 line 0")
                    self.assertEqual([plain(w.kids(w.pages[i])[0]) for i in range(1, n)],
                                     [f"n{i}" for i in range(1, n)])
                finally:
                    w.close()

    def test_live_drift_still_counts_toward_the_abort_but_holds_do_not(self):
        """Control for the lead decision: a live drift (CONFLICT_BOTH) still counts, a hold does not.
        5 pages, 1 drift + 1 hold -> 1/5 = 20 % (not more): the 3 clean pages are written (before: 2/5, aborted).
        4 pages, 1 drift + 1 hold -> 1/4 > 20 %: aborted, 0 writes, the message counts only the drift page."""
        for n, want in ((5, "partially_applied"), (4, "aborted")):
            with self.subTest(pages=n):
                w = World(n)
                try:
                    items = [w.item(f"M-{i + 1:03d}", i, [w.op_update(w.pages[i], 0, f"n{i}")], page_kind="page")
                             for i in range(n)]
                    P, sha, sel, rec = w.plan(items)
                    w.admin.update_block(items[0]["ops"][0]["target_id"], {"paragraph": {"rich_text": rt("remote")}})
                    self._hold(w, {w.pages[1]: {"reason": "post_verify_failed", "source": "post_verify"}})
                    w.fake.clear_requests()
                    res = w.run(P, sel, rec)
                    self.assertEqual(res["status"], want, res["items"])
                    self.assertEqual(res["items"]["M-001"].get("conflict"), "CONFLICT_BOTH", res["items"])
                    self._assert_held(w, P, res, "M-002")
                    if want == "aborted":
                        self.assertIn(f"预检不一致的页面 1/{n}", res["reason"])
                        self.assertEqual(w.wb_writes(), [], _write_paths(w))
                        self.assertEqual({res["items"][i]["status"] for i in ("M-003", "M-004")}, {"not_executed"})
                    else:
                        self.assertEqual(sorted(_write_paths(w)), sorted(
                            ("PATCH", f"blocks/{it['ops'][0]['target_id']}") for it in items[2:]))
                        self.assertEqual(res["executed"], ["M-003", "M-004", "M-005"])
                finally:
                    w.close()

    def test_every_selected_page_held_is_aborted_with_zero_writes(self):
        """Nothing left to run after the precheck (every selected page skipped by a hold / leaf skip): aborted, not
        "partially applied" (that status suggests a rollback of writes that never happened)."""
        w = World(2)
        try:
            items = [w.item(f"M-{i + 1:03d}", i, [w.op_update(w.pages[i], 0, f"n{i}")], page_kind="page")
                     for i in range(2)]
            P, sha, sel, rec = w.plan(items)
            self._hold(w, {w.pages[0]: {"reason": "sync_conflict_copy", "source": "scan", "subtree": False},
                           w.pages[1]: {"reason": "post_verify_failed", "source": "post_verify"}})
            w.fake.clear_requests()
            res = w.run(P, sel, rec)
            self.assertEqual(res["status"], "aborted", res["items"])
            self.assertEqual(w.plan_status(P), "aborted")
            self.assertIn("所选的 2 个页面都未通过预检", res["reason"])
            self.assertEqual(w.wb_writes(), [], _write_paths(w))
            self.assertEqual(w.journal(P), [])
            for iid in ("M-001", "M-002"):
                self._assert_held(w, P, res, iid)
        finally:
            w.close()

    def test_stale_scan_hold_of_the_note_names_how_it_is_released(self):
        """Fix loop 1 (finding 2): the planner ignores the note's own sync_conflict_copy hold (the copy is gone),
        but auto_sync_hold.json keeps it until the next refresh and the adoption publish would SKIP_HELD the note:
        the executor still writes nothing and tells how the hold is released. Other holds keep the old text."""
        for src, reason in (("scan", "sync_conflict_copy"), ("post_verify", "post_verify_failed")):
            with self.subTest(source=src):
                w = World(1)
                try:
                    p0 = w.pages[0]
                    it = w.item("M-001", 0, [w.op_update(p0, 0, "never written")], page_kind="page")
                    P, sha, sel, rec = w.plan([it])
                    self._hold(w, {p0: {"reason": reason, "source": src, "subtree": False}})
                    w.fake.clear_requests()
                    res = w.run(P, sel, rec)
                    self.assertEqual(w.wb_writes(), [], _write_paths(w))
                    self._assert_held(w, P, res, "M-001")
                    why = res["items"]["M-001"]["reason"]
                    self.assertIn(f"（{reason}，{src}）", why)
                    if src == "scan":
                        self.assertIn(f"refresh.py --release-hold {wb_apply._nid(p0)}", why)
                        self.assertIn("下一轮 refresh 自动解除", why)
                    else:
                        self.assertNotIn("--release-hold", why)
                        self.assertIn("人工处理并解除暂停后重新生成计划", why)
                    self.assertEqual(plain(w.kids(p0)[0]), "p0 line 0")
                finally:
                    w.close()


class L18AdoptionHoldbackTests(unittest.TestCase):
    """L-18 (d): a note that still has an unselected / rejected / skipped / failed item of the same plan (or a local
    change the plan does not write) is never adopted: the Notion writes of its executed items stand, the vault file
    stays local (LOCAL_ONLY) and the note is listed as 待人工处理 with the reason."""

    def _adopter(self, calls):
        def adopter(ctx, written, **kw):
            calls.append(list(written))
            return ok_adopter(ctx, written, **kw)
        return adopter

    def _assert_held_back(self, w, P, res, iid, nid, contains):
        hb = res["adoption"]["held_back"]
        h = next(x for x in hb if x["notion_id"] == nid)
        self.assertIn(iid, h["items"])
        self.assertIn(contains, h["reason"])
        self.assertIn(nid, res["adoption"]["needs_manual"])
        self.assertEqual(res["status"], "partially_applied")
        for k in ("W6", "W7", "W8"):
            self.assertEqual(res["w_checks"][k]["status"], "fail", k)
            self.assertIn("待人工处理", res["w_checks"][k]["detail"])
        self.assertEqual(res["items"][iid]["status"], "executed")
        self.assertEqual(w.db.execute("SELECT verify_status FROM plan_items WHERE plan_id=? AND item_id=?",
                                      (P, iid)).fetchone()[0], "needs_manual")
        with open(res["result_md"], encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("待人工处理（未采纳", md)
        self.assertIn(contains, md)
        row = next(ln for ln in md.splitlines() if ln.startswith(f"| {iid} "))
        self.assertIn("未采纳", row)

    def test_note_with_an_unselected_item_is_not_adopted(self):
        w = World(2)
        try:
            p0, p1 = w.pages
            m1 = w.item("M-001", 0, [w.op_update(p0, 0, "written to Notion")])
            t1 = w.item("T-001", 0, [{"op": "RENAME", "old_title": rt("Page 0"), "new_title": "Renamed"}], cat="T")
            m2 = w.item("M-002", 1, [w.op_update(p1, 0, "written and adopted")])
            P, sha, sel, rec = w.plan([m1, t1, m2], select=["M-001", "M-002"])
            calls = []
            w.fake.clear_requests()
            res = w.run(P, sel, rec, adopter=self._adopter(calls))
            self.assertEqual(_write_paths(w), [("PATCH", f"blocks/{m1['ops'][0]['target_id']}"),
                                               ("PATCH", f"blocks/{m2['ops'][0]['target_id']}")])
            self.assertEqual(calls, [["M-002"]])                     # page 1 adopted, page 0 never handed over
            self._assert_held_back(w, P, res, "M-001", p0, "T-001")
            self.assertEqual([x["notion_id"] for x in res["adoption"]["held_back"]], [p0])
            self.assertEqual(w.fake.page(p0)["properties"]["title"]["title"][0]["plain_text"], "Page 0")
        finally:
            w.close()

    def test_rejected_skipped_refused_or_plan_rejected_sibling_blocks_the_adoption(self):
        for case in ("rejected_item", "skipped_item", "refused_entry", "plan_rejected_entry"):
            with self.subTest(case=case):
                w = World(2)
                try:
                    p0, p1 = w.pages
                    m1 = w.item("M-001", 0, [w.op_update(p0, 0, "written to Notion")])
                    t1 = w.item("T-001", 0, [{"op": "RENAME", "old_title": rt("Page 0"), "new_title": "Renamed"}],
                                cat="T")
                    items, select, extra, contains = [m1], ["M-001"], {}, None
                    if case == "rejected_item":
                        items, contains = [m1, t1], "已被拒绝"
                    elif case == "skipped_item":
                        items, select, contains = [m1, t1], ["M-001", "T-001"], "已跳过"
                    elif case == "refused_entry":
                        extra = {"refused": [{"rel_path": w.rels[0], "notion_id": p0, "class": "UNSUPPORTED_INLINE:wikilink",
                                              "reason": "新的 [[链接]]", "hint": ""}]}
                        contains = "UNSUPPORTED_INLINE:wikilink"
                    else:
                        extra = {"rejected": [{"notion_id": p0, "rel_path": w.rels[0], "l_sha": "0" * 64,
                                               "item_category": "B", "since": "x"}]}
                        contains = "已拒绝 B"
                    other = w.item("M-002", 1, [w.op_update(p1, 0, "fine")])
                    P, sha, sel, rec = w.plan(items + [other], select=select + ["M-002"], extra=extra)
                    if case == "rejected_item":
                        with wb_state.transaction(w.db):
                            w.db.execute("UPDATE plan_items SET decision='rejected' WHERE plan_id=? AND item_id='T-001'",
                                         (P,))
                    if case == "skipped_item":
                        w.admin.update_page(p0, {"properties": {"title": {"title": rt("Renamed in Notion")}}})
                    calls = []
                    w.fake.clear_requests()
                    with mock.patch.object(wb_apply, "INCONSISTENT_ABORT_RATIO", 0.6):
                        res = w.run(P, sel, rec, adopter=self._adopter(calls))
                    self.assertIn(("PATCH", f"blocks/{m1['ops'][0]['target_id']}"), _write_paths(w))
                    self.assertNotIn(("PATCH", f"pages/{p0}"), _write_paths(w))
                    self.assertEqual(calls, [["M-002"]])
                    self._assert_held_back(w, P, res, "M-001", p0, contains)
                finally:
                    w.close()

    def test_only_held_notes_written_means_no_adoption_run(self):
        w = World(1)
        try:
            p0 = w.pages[0]
            m1 = w.item("M-001", 0, [w.op_update(p0, 0, "written to Notion")])
            t1 = w.item("T-001", 0, [{"op": "RENAME", "old_title": rt("Page 0"), "new_title": "Renamed"}], cat="T")
            P, sha, sel, rec = w.plan([m1, t1], select=["M-001"])
            calls = []
            w.fake.clear_requests()
            res = w.run(P, sel, rec, adopter=self._adopter(calls))
            self.assertEqual(_write_paths(w), [("PATCH", f"blocks/{m1['ops'][0]['target_id']}")])
            self.assertEqual(calls, [])
            self.assertFalse(res["adoption"]["ok"])
            self._assert_held_back(w, P, res, "M-001", p0, "T-001")
        finally:
            w.close()

    def test_holdback_check_failure_adopts_nothing(self):
        w = World(1)
        try:
            m1 = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "written to Notion")])
            P, sha, sel, rec = w.plan([m1])
            calls = []
            with mock.patch.object(wb_apply, "_adoption_holdback", side_effect=sqlite3.OperationalError("boom")):
                res = w.run(P, sel, rec, adopter=self._adopter(calls))
            self.assertEqual(calls, [])
            self.assertEqual(res["items"]["M-001"]["status"], "executed")
            self.assertEqual(res["status"], "partially_applied")
            self.assertIn(w.pages[0], res["adoption"]["needs_manual"])
            self.assertEqual(res["w_checks"]["W6"]["status"], "fail")
            self.assertIn("无法确认", res["w_checks"]["W6"]["detail"])
        finally:
            w.close()

    def test_real_publish_keeps_the_held_back_note_local(self):
        """End to end (real refetch / exporter / verifier / publisher): Home is adopted; Kid, whose note still has a
        local change the plan does not write, is held from the adoption publish (SKIP_HELD) and keeps its local file."""
        e = E2EWorld([para("首页正文")], kid=True, mode="full")
        try:
            home_rel, kid_rel = e.rel[e.home], e.rel[e.kid]
            home_l = e.write(home_rel, e.read(home_rel).replace("首页正文", "首页正文（已修改）"))
            kid_text = e.read(kid_rel).replace("kid body", "kid body 改") + "\n本地另一处改动，计划不写回\n"
            kid_l = e.write(kid_rel, kid_text)

            def pub(rel):
                con = sqlite3.connect(f"file:{os.path.join(e.state, 'export.db')}?mode=ro", uri=True)
                try:
                    return con.execute("SELECT status, notion_id, sha256 FROM published_files WHERE rel_path=?",
                                       (rel,)).fetchone()
                finally:
                    con.close()
            pub_kid_before = pub(kid_rel)
            hb, kb = e.rc.list_children(e.home)[0], e.rc.list_children(e.kid)[0]
            items = [
                {"item_id": "M-001", "category": "M", "notion_id": e.home, "rel_path": home_rel, "l_sha": home_l,
                 "binds": [], "ops": [{"op": "UPDATE", "target_id": hb["id"], "before_norm_sha": wb_norm.block_json_sha(hb),
                                       "request": {"paragraph": {"rich_text": rt("首页正文（已修改）")}}}]},
                {"item_id": "M-002", "category": "M", "notion_id": e.kid, "rel_path": kid_rel, "l_sha": kid_l,
                 "binds": [], "ops": [{"op": "UPDATE", "target_id": kb["id"], "before_norm_sha": wb_norm.block_json_sha(kb),
                                       "request": {"paragraph": {"rich_text": rt("kid body 改")}}}]}]
            refused = [{"rel_path": kid_rel, "notion_id": e.kid, "class": "BLANK_LINES", "reason": "x", "hint": ""}]
            res = e.apply(items, "wb-e2e-holdback", extra={"refused": refused})
            self.assertEqual(sorted((r["method"], r["path"]) for r in e.wb_writes()),
                             sorted([("PATCH", f"blocks/{hb['id']}"), ("PATCH", f"blocks/{kb['id']}")]))
            self.assertEqual(res["status"], "partially_applied", res.get("reason"))
            self.assertEqual([h["notion_id"] for h in res["adoption"]["held_back"]], [e.kid])
            self.assertEqual(e.read(kid_rel), kid_text)                          # the local change survives
            self.assertEqual(pub(kid_rel), pub_kid_before)                       # not published over
            lb = wb_state.latest_base(e.db, e.kid)
            self.assertNotEqual((lb or {}).get("origin"), "WRITEBACK")
            ad = res["adoption"]
            self.assertEqual(ad["steps"]["publish_mode"], "full", ad.get("reason"))
            self.assertEqual(ad["steps"]["publish_apply"], 0)
            self.assertIn("首页正文（已修改）", e.read(home_rel))
            self.assertEqual(wb_state.latest_base(e.db, e.home)["origin"], "WRITEBACK")
            pp = os.path.join(e.work, "reports", "writeback", "wb-e2e-holdback", "adopt", "publish", "publish_plan.json")
            with open(pp, encoding="utf-8") as fh:
                acts = [a for a in json.load(fh)["actions"] if a.get("nid") == e.kid]
            self.assertTrue(acts and all(a["op"] == "SKIP_HELD" and "writeback_not_adopted" in a.get("reason", "")
                                         for a in acts), acts)
            self.assertNotIn("校验未通过", res["w_checks"]["W6"]["detail"])
            self.assertEqual(res["w_checks"]["W1"]["status"], "pass")
        finally:
            e.close()


# =============================================================================================== safety / static
class StaticSafetyTests(unittest.TestCase):
    def test_no_bypass_or_env_switch(self):
        import ast
        with open(os.path.join(TOOLS, "wb_apply.py"), encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertNotIn("environ", names)
        self.assertNotIn("getenv", names)
        for bad in ("bypass", "skip_gate", "force", "yes", "no_confirm"):
            for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
                self.assertNotIn(bad, [a.arg for a in fn.args.args + fn.args.kwonlyargs], fn.name)

    def test_writeback_env_only_loaded_in_make_clients(self):
        with open(os.path.join(TOOLS, "wb_apply.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(src.count("load_writeback_token("), 2)    # make_clients + the permission re-check


@unittest.skipUnless(os.path.isfile(os.path.join(TOOLS, "writeback.py")), "writeback.py (I1) not present")
class CliGateRefusalTests(unittest.TestCase):
    """The human gate of `writeback.py apply` (owned by I1) REFUSES for agent markers, without a TTY and with piped
    stdin -- against a temp WORK (--work), the fake server and fake token files. Refusal only (contract rule 3):
    nothing is confirmed, the plan stays draft and the fake sees no request with the write-back token."""

    def setUp(self):
        self.sandbox_state = sorted(os.listdir(os.path.join(os.path.dirname(TOOLS), "state")))
        self.w = World(1)
        w = self.w
        os.makedirs(os.path.join(w.work, "config"))
        cfg = dict(w.cfg, rate_limit_total_rps=2.5)
        with open(os.path.join(w.work, "config", "writeback.json"), "w") as fh:
            json.dump(cfg, fh)
        d = os.path.join(w.tmp, "tok")
        os.mkdir(d, 0o700)
        self.tok = os.path.join(d, "writeback.env")
        with open(self.tok, "w") as fh:
            fh.write(f"NOTION_WRITEBACK_TOKEN={w.fake.secret('wb')}\n")
        os.chmod(self.tok, 0o600)
        self.ro = os.path.join(d, "read.env")
        with open(self.ro, "w") as fh:
            fh.write(f"NOTION_TARGET_TOKEN={w.fake.secret('ro')}\n")
        it = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "must never be written")])
        self.P = "wb-20261118T0101-" + os.urandom(2).hex()
        _p, self.sha, _s, _r = w.plan([it], status="draft", confirm=[], plan_id=self.P)
        w.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        w.fake.clear_requests()
        self.state_before = self._state_digest()

    def _state_digest(self):
        """{relative path: sha256} of every file under the temp WORK's state/ (byte identity, not just names)."""
        out = {}
        root = os.path.join(self.w.work, "state")
        for d, _dirs, files in os.walk(root):
            for f in files:
                p = os.path.join(d, f)
                out[os.path.relpath(p, root)] = wb_common.sha256_file(p)
        return out

    def tearDown(self):
        self.w.close()
        self.assertEqual(sorted(os.listdir(os.path.join(os.path.dirname(TOOLS), "state"))), self.sandbox_state)

    def _env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDECODE", "AI_AGENT", "CI", "GITHUB_ACTIONS") and not k.startswith("CLAUDE_CODE_")}
        env.update(extra)
        return env

    def _argv(self):
        w = self.w
        return [sys.executable, "-I", "-B", os.path.join(TOOLS, "writeback.py"), "apply", "--work", w.work, "--vault",
                w.vault, "--base-url", w.base, "--token-file", self.tok, "--read-token-file", self.ro,
                "--plan", self.P, "--sha", self.sha[:12], "--items", "M-001"]

    def _assert_untouched(self):
        w = self.w
        # the human gate runs before anything under state/ is opened read-write: state/ stays byte-identical
        self.assertEqual(self._state_digest(), self.state_before)
        self.assertEqual(w.wb_requests(), [])
        self.assertEqual(w.plan_status(self.P), "draft")
        self.assertEqual(w.db.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0], 0)
        self.assertEqual(plain(w.kids(w.pages[0])[0]), "p0 line 0")

    def test_agent_markers_and_no_tty_refused(self):
        for extra in ({"CLAUDECODE": "1"}, {"AI_AGENT": "x"}, {"CI": "true"}, {"GITHUB_ACTIONS": "true"},
                      {"CLAUDE_CODE_ENTRYPOINT": "cli"}, {}):
            r = subprocess.run(self._argv(), input=b"", capture_output=True, env=self._env(**extra), timeout=120,
                               cwd=self.w.tmp)
            self.assertEqual(r.returncode, 3, (extra, r.stdout[-500:], r.stderr[-500:]))
            if extra:
                self.assertIn(next(iter(extra)), (r.stdout + r.stderr).decode("utf-8", "replace"))
        self._assert_untouched()

    def test_pty_with_agent_marker_refused_without_prompt(self):
        import pexpect
        argv = self._argv()
        child = pexpect.spawn(argv[0], argv[1:], env=self._env(CLAUDECODE="1"), cwd=self.w.tmp, timeout=120,
                              encoding="utf-8")
        child.expect(pexpect.EOF)
        out = child.before or ""
        child.close()
        self.assertEqual(child.exitstatus, 3, out[-500:])
        self.assertNotIn(f"确认 {self.P[-4:]}", out)           # the challenge phrase was never shown
        self._assert_untouched()

    def test_pty_clean_env_but_piped_stdin_refused(self):
        import pexpect
        import shlex
        cmd = "echo 确认 | " + " ".join(shlex.quote(a) for a in self._argv())
        child = pexpect.spawn("/bin/bash", ["-c", cmd], env=self._env(), cwd=self.w.tmp, timeout=120, encoding="utf-8")
        child.expect(pexpect.EOF)
        out = child.before or ""
        child.close()
        self.assertEqual(child.exitstatus, 3, out[-500:])
        self._assert_untouched()



# =============================================================================================== wave-4 fix round 1
def _forge_done_update(w, P, item_id, target_pid, text, op_index=0):
    """A forged `done` UPDATE journal row (finding r1-never-unapproved-02): after_json copied from the live block,
    before_json carrying `text` -- exactly what someone editing writeback.db by hand could insert."""
    live = w.kids(target_pid)[0]
    before = json.loads(json.dumps(live))
    before["paragraph"]["rich_text"] = rt(text)
    with wb_state.transaction(w.db):
        w.db.execute("INSERT INTO journal(plan_id, item_id, op, target_id, state, before_json, request_json, "
                     "expected_after_json, after_json, committed_ids, ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (P, item_id, "UPDATE", live["id"], "done", json.dumps(before),
                      json.dumps({"body": {"paragraph": {"rich_text": rt(text)}}, "op_index": op_index}),
                      json.dumps({}), json.dumps(live), json.dumps({"outcome": "ok"}), wb_common.now_iso()))
    return live["id"]


class W4RollbackProvenanceTests(unittest.TestCase):
    """r1-never-unapproved-02: rollback restores only what THIS plan wrote and needs the original apply confirmation."""

    def setUp(self):
        self.w = World(2)

    def tearDown(self):
        self.w.close()

    def _applied(self):
        w = self.w
        m = w.item("M-001", 0, [w.op_update(w.pages[0], 0, "U1")])
        P, sha, sel, rec = w.plan([m])
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res)
        return P, sha

    def _rb(self, P, sha, items):
        w = self.w
        w.confirm(P, sha, items, "rollback")
        return wb_apply.rollback_plan(P, None, w.record(P, sha, "rollback"), client=w.wb, read_client=w.rc,
                                      cfg=w.cfg, db=w.db, work=w.work, vault=w.vault)

    def _assert_page1_untouched(self):
        self.assertEqual([plain(b) for b in self.w.kids(self.w.pages[1])], ["p1 line 0", "p1 line 1", "p1 line 2"])

    def test_forged_row_under_a_planned_item_refuses_whole_rollback(self):
        w = self.w
        P, sha = self._applied()
        _forge_done_update(w, P, "M-001", w.pages[1], "FORGED VIA ROLLBACK")
        prev = wb_apply.rollback_preview(P, None, read_client=w.rc, db=w.db, work=w.work)
        forged = [s for s in prev["steps"] if s["target"] == w.kids(w.pages[1])[0]["id"]]
        self.assertEqual([s["action"] for s in forged], ["manual"], prev["steps"])
        self.assertIn("不符", forged[0]["reason"])
        w.fake.clear_requests()
        res = self._rb(P, sha, ["M-001"])
        self.assertEqual(res["status"], "refused", res)
        self.assertIn("不符", res["reason"])
        self.assertEqual(w.wb_writes(), [])
        self._assert_page1_untouched()
        self.assertEqual(plain(w.kids(w.pages[0])[0]), "U1")

    def test_forged_item_not_in_plan_refused(self):
        w = self.w
        P, sha = self._applied()
        _forge_done_update(w, P, "M-777", w.pages[1], "FORGED VIA ROLLBACK")
        w.fake.clear_requests()
        res = self._rb(P, sha, ["M-777", "M-001"])
        self.assertEqual(res["status"], "refused", res)
        self.assertEqual(w.wb_writes(), [])
        self._assert_page1_untouched()

    def test_before_image_not_matching_plan_sha_refused(self):
        """A done row of the planned block whose before_json was replaced (restore content not the plan's)."""
        w = self.w
        P, sha = self._applied()
        row = [r for r in w.journal(P) if r["op"] == "UPDATE"][0]
        before = json.loads(row["before_json"])
        before["paragraph"]["rich_text"] = rt("INJECTED")
        with wb_state.transaction(w.db):
            w.db.execute("UPDATE journal SET before_json=? WHERE seq=?", (json.dumps(before), row["seq"]))
        w.fake.clear_requests()
        res = self._rb(P, sha, ["M-001"])
        self.assertEqual(res["status"], "refused", res)
        self.assertEqual(w.wb_writes(), [])
        self.assertEqual(plain(w.kids(w.pages[0])[0]), "U1")

    def test_missing_apply_confirmation_refused(self):
        w = self.w
        P, sha = self._applied()
        with wb_state.transaction(w.db):
            w.db.execute("DELETE FROM confirmations WHERE plan_id=? AND phrase_scope='apply'", (P,))
        w.fake.clear_requests()
        res = self._rb(P, sha, ["M-001"])
        self.assertEqual(res["status"], "refused", res)
        self.assertIn("apply", res["reason"])
        self.assertEqual(w.wb_writes(), [])

    def test_untampered_rollback_still_works(self):
        w = self.w
        P, sha = self._applied()
        w.fake.clear_requests()
        res = self._rb(P, sha, ["M-001"])
        self.assertEqual(res["status"], "applied", res)
        self.assertEqual(plain(w.kids(w.pages[0])[0]), "p0 line 0")
        self.assertEqual(len(w.wb_writes()), 1)



class W4AuditScopeProvenanceTests(WorldCase):
    """r1-user-journey-01 (a page this tool trashed is not 'unreachable') and r1-never-unapproved-06 (a created page
    counts only with a committed journal CREATE_PAGE of a confirmed item, under a parent that is still in scope)."""
    N_PAGES = 2

    def _delete_child(self):
        w = self.w
        child = w.fake.add_page("Doomed", parent=w.pages[0], children=[para("doomed body")])
        w.fake.grant("wb", child)
        w.cfg["whitelist_page_ids"] = list(w.pages) + [child]
        with wb_state.transaction(w.db):
            w.db.execute("INSERT INTO pending_deletes(notion_id, rel_path, subtree_json, detected_run, detected_at, "
                         "status, ttl_until) VALUES(?,?,?,?,?,?,?)", (child, "General/Page 0/Doomed.md", "[]", "r",
                                                                      "x", "hold", "2099-01-01"))
        it = {"item_id": "D-001", "category": "D", "notion_id": child, "rel_path": "General/Page 0/Doomed.md",
              "l_sha": None, "ops": [{"op": "TRASH_PAGE", "subtree_ids": []}], "binds": []}
        P, sha, sel, rec = w.plan([it])
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res)
        self.assertTrue(w.fake.page(child)["in_trash"])
        return child, P, sha

    def test_page_trashed_by_writeback_does_not_block_later_applies(self):
        w = self.w
        child, P, _sha = self._delete_child()
        audit = wb_apply.audit_scope(client=w.wb, cfg=w.cfg, db=w.db, record=False)
        self.assertTrue(audit["ok"], audit)
        self.assertEqual([t["id"] for t in audit["trashed_via_writeback"]], [child])
        self.assertEqual(audit["trashed_via_writeback"][0]["plan_id"], P)
        it = w.item("M-001", 1, [w.op_update(w.pages[1], 0, "after delete")])
        P2, sha2, sel2, rec2 = w.plan([it])
        w.fake.clear_requests()
        res = w.run(P2, sel2, rec2)
        self.assertEqual(res["status"], "applied", res)
        self.assertEqual(len(w.wb_writes()), 1)
        self.assertIsNone(wb_state.get_meta(w.db, "forced_mode"))

    def test_page_trashed_by_someone_else_is_still_unreachable_with_recovery_hint(self):
        w = self.w
        other = w.fake.add_page("Trashed by a person", parent=w.pages[0])
        w.fake.grant("wb", other)
        w.cfg["whitelist_page_ids"] = list(w.pages) + [other]
        w.admin.update_page(other, {"in_trash": True})
        it = w.item("M-001", 1, [w.op_update(w.pages[1], 0, "x")])
        P, sha, sel, rec = w.plan([it])
        w.fake.clear_requests()
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "refused", res)
        self.assertNoWrites()
        self.assertIn(other, res["reason"])
        self.assertIn("whitelist_page_ids", res["reason"])
        self.assertIn("audit-scope", res["reason"])
        self.assertEqual(wb_state.get_meta(w.db, "forced_mode"), "plan")

    def test_rolled_back_delete_then_trashed_by_person_counts_again(self):
        w = self.w
        child, P, sha = self._delete_child()
        w.confirm(P, sha, ["D-001"], "rollback")
        out = wb_apply.rollback_plan(P, None, w.record(P, sha, "rollback"), client=w.wb, read_client=w.rc, cfg=w.cfg,
                                     db=w.db, work=w.work, vault=w.vault)
        self.assertEqual(out["status"], "applied", out)
        self.assertFalse(w.fake.page(child)["in_trash"])
        w.admin.update_page(child, {"in_trash": True})     # later trashed by a person: a real drift again
        audit = wb_apply.audit_scope(client=w.wb, cfg=w.cfg, db=w.db, record=False)
        self.assertFalse(audit["ok"], audit)
        self.assertEqual([u["id"] for u in audit["unreachable"]], [child])

    def test_forged_created_page_mapping_does_not_hide_extra_access(self):
        w = self.w
        x = w.fake.add_page("Workspace page, not whitelisted")
        w.fake.grant("wb", x)
        wb_state.set_meta(w.db, "created_page:General/whatever.md", {"page_id": x, "state": "complete"})
        audit = wb_apply.audit_scope(client=w.wb, cfg=w.cfg, db=w.db, record=False)
        self.assertFalse(audit["ok"], audit)
        self.assertIn(wb_common.norm_notion_id(x), [e["id"] for e in audit["extra"]])
        ctx = mock.Mock(cfg=w.cfg, db=w.db)
        self.assertFalse(wb_apply._in_scope_page(ctx, w.fake.page(x)))

    def test_created_page_under_parent_removed_from_whitelist_is_extra(self):
        w = self.w
        rel = "General/Page 0/Fresh.md"
        nsha = w.write_note(rel, "# Fresh\n")
        it = {"item_id": "N-001", "category": "N", "notion_id": None, "rel_path": rel, "l_sha": nsha, "binds": [],
              "ops": [{"op": "CREATE_PAGE", "parent": {"page_id": w.pages[0]}, "title": "Fresh",
                       "children": [req_para("c")]}]}
        P, sha, sel, rec = w.plan([it])
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res)
        new = res["created"]["N-001"]
        self.assertTrue(wb_apply.audit_scope(client=w.wb, cfg=w.cfg, db=w.db, record=False)["ok"])
        ctx = mock.Mock(cfg=w.cfg, db=w.db)
        self.assertTrue(wb_apply._in_scope_page(ctx, w.fake.page(new)))
        w.cfg["whitelist_page_ids"] = [w.pages[1]]          # the parent is no longer declared
        w.fake.revoke("wb", w.pages[0])
        w.fake.grant("wb", new)
        audit = wb_apply.audit_scope(client=w.wb, cfg=w.cfg, db=w.db, record=False)
        self.assertFalse(audit["ok"], audit)
        self.assertIn(wb_common.norm_notion_id(new), [e["id"] for e in audit["extra"]])
        self.assertFalse(wb_apply._in_scope_page(ctx, w.fake.page(new)))



class W4RollbackPropertiesTests(unittest.TestCase):
    """r1-spec-coverage-13: rollback of a row-property write (WB §13: only while the property still equals the
    write-time read-back)."""

    def setUp(self):
        self.w = World(1)

    def tearDown(self):
        self.w.close()

    def _applied(self):
        w = self.w
        db, ds = w.fake.add_database(w.pages[0], "Tasks", {"Name": "title",
                                                           "Tag": {"type": "select", "options": ["a", "b", "c"]}})
        row = w.fake.add_row(ds, "Row 1", {"Tag": {"select": {"name": "a"}}})
        w.cfg["whitelist_database_ids"] = [db]
        w.cfg["automation_free_database_ids"] = [db]
        rel = "General/Tasks/Row 1.md"
        w.write_note(rel, f"---\nnotion_id: {row}\n---\nrow\n")
        live = w.rc.retrieve_page(row)
        it = {"item_id": "M-001", "category": "M", "notion_id": row, "rel_path": rel, "l_sha": w.vsha(rel), "binds": [],
              "ops": [{"op": "UPDATE_PROPERTIES", "properties": {"Tag": {"select": {"name": "b"}}},
                       "before": {"Tag": live["properties"]["Tag"]}}]}
        P, sha, sel, rec = w.plan([it])
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res)
        self.assertEqual(w.fake.page(row)["properties"]["Tag"]["select"]["name"], "b")
        return row, P, sha

    def _rb(self, P, sha):
        w = self.w
        w.confirm(P, sha, ["M-001"], "rollback")
        return wb_apply.rollback_plan(P, None, w.record(P, sha, "rollback"), client=w.wb, read_client=w.rc,
                                      cfg=w.cfg, db=w.db, work=w.work, vault=w.vault)

    def test_rollback_restores_the_before_value(self):
        w = self.w
        row, P, sha = self._applied()
        prev = wb_apply.rollback_preview(P, None, read_client=w.rc, db=w.db, work=w.work)
        self.assertEqual([(s["op"], s["action"]) for s in prev["steps"]], [("RB_PROPERTIES", "do")])
        w.fake.clear_requests()
        out = self._rb(P, sha)
        self.assertEqual(out["status"], "applied", out)
        self.assertEqual(w.fake.page(row)["properties"]["Tag"]["select"]["name"], "a")
        self.assertEqual([(r["method"], r["path"]) for r in w.wb_writes()], [("PATCH", f"pages/{row}")])

    def test_property_edited_after_the_write_is_skipped_with_zero_writes(self):
        w = self.w
        row, P, sha = self._applied()
        w.admin.update_page(row, {"properties": {"Tag": {"select": {"name": "c"}}}})
        prev = wb_apply.rollback_preview(P, None, read_client=w.rc, db=w.db, work=w.work)
        self.assertEqual([s["action"] for s in prev["steps"]], ["skip"])
        w.fake.clear_requests()
        self._rb(P, sha)
        self.assertEqual(w.wb_writes(), [])
        self.assertEqual(w.fake.page(row)["properties"]["Tag"]["select"]["name"], "c")


class W4ReconcileLockTests(WorldCase):
    """r1-data-safety-03: reconcile (which can rewrite a vault note's frontmatter) is mutually exclusive with refresh."""
    _unknown_append = ReconcileTests._unknown_append

    def test_reconcile_refused_while_refresh_lock_is_held(self):
        import wb_locks
        w = self.w
        seq = self._unknown_append(True)
        with wb_locks.lock("refresh", state_dir=w.state):
            r = wb_apply.reconcile_journal(seq, "committed", read_client=w.rc, cfg=w.cfg, db=w.db, work=w.work,
                                           vault=w.vault)
        self.assertFalse(r["ok"], r)
        self.assertIn("refresh", r["reason"])
        self.assertEqual(w.db.execute("SELECT state FROM journal WHERE seq=?", (seq,)).fetchone()[0], "unknown")
        r = wb_apply.reconcile_journal(seq, "committed", read_client=w.rc, cfg=w.cfg, db=w.db, work=w.work)
        self.assertTrue(r["ok"], r)          # free again: works as before


class W4FrontmatterAndBomTests(WorldCase):
    """r1-data-safety-02 (BOM note: notion_id goes into the real frontmatter) and r1-data-safety-07 (a crash after the
    frontmatter replace is settled from the live file on resume)."""

    def test_with_notion_id_keeps_the_real_frontmatter_of_a_bom_note(self):
        pid = "12345678-1234-4234-8234-123456789abc"
        data = "﻿---\ntags: [project]\naliases: [BN]\n---\n# Brand New\n\nnew text\n".encode("utf-8")
        out = wb_apply._with_notion_id(data, pid).decode("utf-8")
        self.assertFalse(out.startswith("﻿"), out[:40])
        fm, body, _ = wb_common.split_note(out)
        meta = wb_common.parse_frontmatter(fm)
        self.assertEqual(meta.get("notion_id"), pid)
        self.assertIn("tags", meta)
        self.assertIn("aliases", meta)
        self.assertNotIn("---", body)
        self.assertEqual(wb_apply._diff_outside_ok_keys(data, out.encode("utf-8"), body_only=True), [])

    def test_crash_after_frontmatter_replace_is_settled_on_resume(self):
        w = self.w
        rel = "General/Page 0/Brand New.md"
        nsha = w.write_note(rel, "# Brand New\n\nnew text\n")
        it = {"item_id": "N-001", "category": "N", "notion_id": None, "rel_path": rel, "l_sha": nsha, "binds": [],
              "ops": [{"op": "CREATE_PAGE", "parent": {"page_id": w.pages[0]}, "title": "Brand New",
                       "children": [req_para("new text")]}]}
        P, sha, sel, rec = w.plan([it])
        real = wb_apply._j_update

        def crash(ctx, seq, **cols):
            row = wb_apply._j_row(ctx.db, seq)
            if row and row["op"] == "VAULT_FRONTMATTER" and cols.get("state") == "done":
                raise _Crash("after the replace")
            return real(ctx, seq, **cols)
        with mock.patch.object(wb_apply, "_j_update", crash):
            with self.assertRaises(_Crash):
                w.run(P, sel, rec)
        self.assertEqual(w.plan_status(P), "applying")
        self.assertNotEqual(w.vsha(rel), nsha)                 # the file was rewritten before the crash
        seen = {}

        def adopter(ctx, written, **kw):
            seen["mapping"] = wb_apply._mapping(ctx.db, rel)
            return ok_adopter(ctx, written)
        out = w.resume(P, w.record(P, sha, "resume"), adopter=adopter)
        self.assertEqual(out["status"], "applied", out)
        fm_rows = [r for r in w.journal(P) if r["op"] == "VAULT_FRONTMATTER"]
        self.assertEqual([r["state"] for r in fm_rows], ["done"])
        self.assertEqual(seen["mapping"].get("vault_sha"), w.vsha(rel))
        self.assertEqual(len([r for r in w.wb_writes() if r["method"] == "POST"]), 1)   # never re-created

    def test_frontmatter_guarded_replace_stops_on_race(self):
        w = self.w
        rel = "General/Page 0/Brand New.md"
        nsha = w.write_note(rel, "# Brand New\n\nnew text\n")
        it = {"item_id": "N-001", "category": "N", "notion_id": None, "rel_path": rel, "l_sha": nsha, "binds": [],
              "ops": [{"op": "CREATE_PAGE", "parent": {"page_id": w.pages[0]}, "title": "Brand New",
                       "children": [req_para("new text")]}]}
        P, sha, sel, rec = w.plan([it])
        with mock.patch.object(wb_apply.publish_vault, "guarded_replace",
                               return_value={"vault_sha": "f" * 64, "kept": None}):
            out = w.run(P, sel, rec)
        self.assertEqual(out["status"], "partially_applied", out)
        self.assertIn("frontmatter", " ".join(out.get("notes") or []))
        rows = [r for r in w.journal(P) if r["op"] == "VAULT_FRONTMATTER"]
        self.assertEqual([r["state"] for r in rows], ["failed"])
        with open(os.path.join(w.vault, *rel.split("/")), encoding="utf-8") as fh:
            self.assertNotIn("notion_id", fh.read())


class W4HoldHintAndAdoptionGateTests(WorldCase):
    """r1-contract-consistency-06 (override_rejected hold hint = the planner's) and r1-contract-consistency-08
    (the adoption stops on the exporter's block_lines self-check, like a refresh round)."""
    _base = AdoptionPipelineTests._base
    _refetch = AdoptionPipelineTests._refetch
    _runner = AdoptionPipelineTests._runner
    _ctx_after_apply = AdoptionPipelineTests._ctx_after_apply

    def test_override_rejected_hint_matches_the_planner(self):
        import wb_plan
        h = {"reason": "override_rejected:unsafe_name", "source": "export", "subtree": True, "paths": []}
        self.assertEqual(wb_apply._hold_release_hint(h, "12345678-1234-4234-8234-123456789abc"),
                         wb_plan.OVERRIDE_REJECTED_HINT)
        self.assertIn("refresh.py --release-hold", wb_apply._hold_release_hint(
            {"reason": "sync_conflict_copy", "source": "scan"}, "x"))

    def test_block_lines_selfcheck_failure_stops_the_adoption(self):
        w = self.w
        self._base()
        ctx, written = self._ctx_after_apply()
        calls = []
        inner = self._runner(calls)

        def run(script, args, timeout=None):
            rc = inner(script, args, timeout)
            if script == "export_obsidian.py":
                a = dict(zip(args[::1], args[1::1]))
                with open(os.path.join(a["--out"], "export_report.json"), "w") as fh:
                    json.dump({"block_lines": {"untagged_lines": 3, "contiguity_violations": 1,
                                               "own_noncontiguous": 0}}, fh)
            return rc
        out = wb_apply._adopt_pipeline(ctx, written, runner=run, refetch=self._refetch([]), blobs_dir=w.tmp,
                                       sidebar=os.path.join(w.tmp, "sb.json"))
        scripts = [c[0] for c in calls]
        self.assertEqual(scripts, ["export_obsidian.py"], scripts)      # no verify, no publish
        self.assertFalse(out["ok"], out)
        self.assertIn("untagged_lines", json.dumps(out, ensure_ascii=False))

    def test_clean_block_lines_report_still_publishes(self):
        w = self.w
        self._base()
        ctx, written = self._ctx_after_apply()
        calls = []
        inner = self._runner(calls)

        def run(script, args, timeout=None):
            rc = inner(script, args, timeout)
            if script == "export_obsidian.py":
                a = dict(zip(args[::1], args[1::1]))
                with open(os.path.join(a["--out"], "export_report.json"), "w") as fh:
                    json.dump({"block_lines": {"untagged_lines": 0, "contiguity_violations": 0,
                                               "own_noncontiguous": 0}}, fh)
            return rc
        wb_apply._adopt_pipeline(ctx, written, runner=run, refetch=self._refetch([]), blobs_dir=w.tmp,
                                 sidebar=os.path.join(w.tmp, "sb.json"))
        self.assertIn("publish_vault.py", [c[0] for c in calls])

    def test_unregistered_local_deletions_are_held_note_only(self):
        """r1-contract-consistency-01: a note deleted locally with no pending_deletes row is held (note only) for the
        adoption publish and its verify; a registered one (pending_deletes row) and the adopted ids are not."""
        w = self.w
        ctx, written = self._ctx_after_apply()
        a, b_, c = (str(__import__("uuid").uuid4()) for _ in range(3))
        with wb_state.transaction(w.db):
            w.db.execute("INSERT INTO pending_deletes(notion_id, rel_path, status) VALUES(?,?,?)", (b_, "General/B.md",
                                                                                                    "hold"))
        missing = [{"rel": "General/A.md", "notion_id": a}, {"rel": "General/B.md", "notion_id": b_},
                   {"rel": "General/C.md", "notion_id": c, "pending": True},
                   {"rel": "General/Page 0.md", "notion_id": w.pages[0]}, {"rel": "General/img.png", "notion_id": a}]
        got = wb_apply._unregistered_deletions(ctx, missing, exclude=[w.pages[0]])
        self.assertEqual(got, {a: ["General/A.md"]})
        import wb_scan
        with mock.patch.object(wb_scan, "scan", return_value={"missing": missing}):
            p = wb_apply._adopt_holds(ctx, w.tmp, [w.pages[0]])
        with open(p) as fh:
            holds = json.load(fh)
        self.assertEqual(holds[a]["subtree"], False)
        self.assertEqual(holds[a]["paths"], ["General/A.md"])
        self.assertNotIn(w.pages[0], holds)


def _mk_export_db(w, pub=(), objs=()):
    """A minimal state/export.db: published_files (rel, notion_id, status) + objects_map (notion_id, status, path)."""
    p = os.path.join(w.state, "export.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE IF NOT EXISTS published_files(rel_path TEXT PRIMARY KEY, sha256 TEXT, notion_id TEXT, "
                "kind TEXT, run_id TEXT, status TEXT, staged_sha256 TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS objects_map(notion_id TEXT PRIMARY KEY, kind TEXT, rel_path TEXT, "
                "name TEXT, parent_id TEXT, teamspace TEXT, ord INT, title TEXT, rendered_sha256 TEXT, "
                "published_sha256 TEXT, published_path TEXT, published_run TEXT, status TEXT, first_run TEXT, "
                "last_run TEXT)")
    for rel, nid, st in pub:
        con.execute("INSERT OR REPLACE INTO published_files(rel_path, sha256, notion_id, kind, run_id, status) "
                    "VALUES(?,?,?,?,?,?)", (rel, "ab" * 32, nid, "page", "R1", st))
    for nid, st, path in objs:
        con.execute("INSERT OR REPLACE INTO objects_map(notion_id, kind, rel_path, published_path, status) "
                    "VALUES(?,?,?,?,?)", (nid, "page", path, path, st))
    con.commit()
    con.close()
    return p


class W4NextRoundDeleteVerificationTests(WorldCase):
    """r1-spec-coverage-01 / r1-user-journey-06 (R-183, WB §6.6): pending_next_round -> verified / needs_manual at the
    next plan after the next refresh publish."""
    N_PAGES = 1
    _child = DeleteTests._child

    def _deleted(self):
        w = self.w
        child = self._child()
        it = {"item_id": "D-001", "category": "D", "notion_id": child, "rel_path": "General/Page 0/Doomed.md",
              "l_sha": None, "ops": [{"op": "TRASH_PAGE", "subtree_ids": []}], "binds": []}
        P, sha, sel, rec = w.plan([it])
        res = w.run(P, sel, rec)
        self.assertEqual(res["status"], "applied", res)
        _mk_export_db(w, pub=[("General/Page 0/Doomed.md", child, "trashed_via_writeback"),
                              (w.rels[0], w.pages[0], "active")],
                      objs=[(child, "trashed_via_writeback", "General/Page 0/Doomed.md"),
                            (w.pages[0], "active", w.rels[0])])
        return child, P

    def _published_after(self, minutes=1):
        w = self.w
        at = (datetime.datetime.now().astimezone() + datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")
        with open(os.path.join(w.state, "last_published.json"), "w") as fh:
            json.dump({"run_id": "R9", "snapshot": "x", "at": at}, fh)

    def _verify(self):
        w = self.w
        return wb_apply.verify_pending_deletes(w.db, read_client=w.rc, work=w.work, vault=w.vault, nid_index={})

    def _vs(self, P):
        return self.w.db.execute("SELECT verify_status, reason FROM plan_items WHERE plan_id=? AND item_id='D-001'",
                                 (P,)).fetchone()

    def test_verified_after_the_next_refresh_publish(self):
        w = self.w
        child, P = self._deleted()
        self.assertEqual(self._verify(), [])                     # no refresh has published since: not yet
        self.assertEqual(self._vs(P)[0], "pending_next_round")
        self._published_after()
        w.fake.clear_requests()
        out = self._verify()
        self.assertEqual([(x["item_id"], x["status"]) for x in out], [("D-001", "verified")], out)
        self.assertEqual(self._vs(P)[0], "verified")
        self.assertEqual(w.wb_writes(), [])
        self.assertTrue(all(r["token"] == "ro" for r in w.fake.requests))
        self.assertEqual(self._verify(), [])                     # decided once

    def test_restored_page_or_leftover_link_needs_manual(self):
        w = self.w
        child, P = self._deleted()
        self._published_after()
        w.admin.update_page(child, {"in_trash": False})        # someone restored it in Notion
        w.write_note(w.rels[0], f"---\nnotion_id: {w.pages[0]}\n---\n# Page 0\n\n[[General/Page 0/Doomed|Doomed]]\n")
        out = self._verify()
        self.assertEqual([x["status"] for x in out], ["needs_manual"], out)
        why = "；".join(out[0]["reasons"])
        self.assertIn("in_trash", why)
        self.assertIn("链接", why)
        vs, reason = self._vs(P)
        self.assertEqual(vs, "needs_manual")
        self.assertIn("W4", reason)

    def test_vault_file_with_the_id_needs_manual(self):
        w = self.w
        child, P = self._deleted()
        self._published_after()
        out = wb_apply.verify_pending_deletes(w.db, read_client=w.rc, work=w.work, vault=w.vault,
                                              nid_index={child: ["General/Page 0/Doomed.md"]})
        self.assertEqual([x["status"] for x in out], ["needs_manual"])
        self.assertIn("库内仍有", out[0]["reasons"][0])


class W4W8ConsistencyTests(WorldCase):
    """r1-spec-coverage-11: W8 also checks objects_map and plan_items (no dangling rows)."""
    _ctx_after_apply = AdoptionPipelineTests._ctx_after_apply

    def test_objects_map_and_plan_items_checked(self):
        w = self.w
        ctx, written = self._ctx_after_apply()
        _mk_export_db(w, objs=[(w.pages[0], "active", w.rels[0])])
        self.assertEqual(wb_apply._w8_objects_and_items(ctx, written), [])
        _mk_export_db(w, objs=[(w.pages[0], "trashed_via_writeback", "General/elsewhere.md")])
        probs = wb_apply._w8_objects_and_items(ctx, written)
        self.assertTrue(any("trashed_via_writeback" in p for p in probs), probs)
        self.assertTrue(any("路径" in p for p in probs), probs)
        con = sqlite3.connect(os.path.join(w.state, "export.db"))
        con.execute("DELETE FROM objects_map")
        con.commit()
        con.close()
        self.assertTrue(any("无记录" in p for p in wb_apply._w8_objects_and_items(ctx, written)))
        _mk_export_db(w, objs=[(w.pages[0], "active", w.rels[0])])
        with wb_state.transaction(w.db):
            w.db.execute("INSERT INTO plan_items(plan_id, item_id, category, decision) VALUES(?,?,?,?)",
                         (ctx.plan_id, "P-GHOST", "M", "pending"))
            w.db.execute("UPDATE plan_items SET decision='pending' WHERE plan_id=? AND item_id='M-001'", (ctx.plan_id,))
        probs = wb_apply._w8_objects_and_items(ctx, written)
        self.assertTrue(any("P-GHOST" in p for p in probs), probs)
        self.assertTrue(any("M-001" in p and "executed" in p for p in probs), probs)


if __name__ == "__main__":
    unittest.main()
