#!/usr/bin/env python3
"""Small two-round N->O sandbox rehearsal required by wave 4 acceptance.

The fixture uses a fake read connection and a copied SQLite snapshot. It never reaches a real Notion endpoint.
"""
import contextlib
import io
import json
import os
import sqlite3
import shutil
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in __import__("sys").path:
    __import__("sys").path.insert(0, HERE)

import test_e2e_writeback as e2e  # noqa: E402


World = e2e.World
CHILD = e2e.CHILD
MAIN = e2e.MAIN


def _copy_snapshot(src, dst):
    source = sqlite3.connect(src)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _mutate_snapshot(path, page_id):
    con = sqlite3.connect(path)
    try:
        row = con.execute("SELECT id, data FROM blocks WHERE page_id=? AND type='paragraph' ORDER BY ord LIMIT 1",
                          (page_id,)).fetchone()
        if row:
            data = json.loads(row[1])
            rich = ((data.get("paragraph") or {}).get("rich_text") or [])
            if rich and isinstance(rich[0], dict):
                rich[0].setdefault("text", {})["content"] = rich[0].get("text", {}).get("content", "") + "（快照改动）"
                rich[0]["plain_text"] = rich[0].get("text", {}).get("content", "")
                con.execute("UPDATE blocks SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), row[0]))
        con.commit()
    finally:
        con.close()


class TestSandboxTwoRound(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        e2e.setUpModule()

    @classmethod
    def tearDownClass(cls):
        e2e.tearDownModule()

    def test_mutated_snapshot_two_round_rehearsal(self):
        w = World(self, "accept-sandbox")
        child_text = w.read(CHILD)
        renamed = "General/写回测试页/子页面改名.md"
        sync_copy = "General/写回测试页/子页面 (Conflicted copy 2026-11-20 1200).md"
        user_file = "General/本地用户笔记.md"
        os.rename(w.path(CHILD), w.path(renamed))
        w.write(MAIN, w.read(MAIN).replace(f"[[{CHILD[:-3]}|", f"[[{renamed[:-3]}|"))
        w.write(sync_copy, child_text.replace("子页面正文", "子页面正文（另一台设备）"))
        w.write(user_file, "用户自己的文件\n")
        with w.fake._lock:
            w.fake._patch_page(w.child, {"properties": {"title": {"title": [{"text": {"content": "子页面改名"}}]}}},
                               w.fake._tok("wb"))
            w.fake.add_blocks(w.child, [e2e.blk("paragraph", e2e.rt("Notion 新增段落"))])
        source = w.crawl()
        mutated = os.path.join(w.P.snapshots, "accept-mutated.db")
        _copy_snapshot(source, mutated)
        shutil.copy2(source + ".roots.json", mutated + ".roots.json")
        _mutate_snapshot(mutated, w.child)

        with contextlib.redirect_stdout(io.StringIO()):
            rc1, st1 = w.refresh("S1", snap=mutated)
        self.assertIn(st1.get("result"), ("published", "published_with_holds"), st1)
        self.assertTrue(os.path.exists(w.path("Notion_Obsidian/运行记录/S1.md")))
        self.assertTrue(os.path.exists(w.path(sync_copy)))

        os.unlink(w.path(sync_copy))
        with contextlib.redirect_stdout(io.StringIO()):
            rc2, st2 = w.refresh("S2", snap=mutated)
        self.assertIn(st2.get("result"), ("published", "published_with_holds"), st2)
        self.assertEqual(w.published(renamed)[2], "active")
        self.assertTrue(os.path.exists(w.path("Notion_Obsidian/运行记录/S2.md")))
        self.assertTrue(os.path.exists(w.path(user_file)))

        con = sqlite3.connect(w.P.export_db)
        try:
            dup = con.execute("SELECT notion_id, COUNT(*) FROM published_files WHERE status='active' AND notion_id IS NOT NULL "
                              "GROUP BY notion_id HAVING COUNT(*) > 1").fetchall()
        finally:
            con.close()
        self.assertEqual(dup, [])
        conflicts = []
        root = w.path("Notion_Obsidian/conflicts")
        if os.path.isdir(root):
            conflicts = [os.path.join(dp, f) for dp, _ds, fs in os.walk(root) for f in fs]
        self.assertLessEqual(len(conflicts), 1)
        verify = os.path.join(w.P.reports, "S2", "post_publish_verify", "verify.json")
        with open(verify, encoding="utf-8") as fh:
            checks = json.load(fh).get("status_by_check", {})
        self.assertTrue(checks)
        self.assertTrue(all(checks.get(f"C{i:02d}") == "PASS" for i in range(1, 11)), checks)


if __name__ == "__main__":
    unittest.main()
