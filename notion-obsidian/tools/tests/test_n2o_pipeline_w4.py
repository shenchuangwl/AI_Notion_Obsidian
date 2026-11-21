#!/usr/bin/env python3
"""Wave-4 fix round 1 (owner group n2o-pipeline: F publish_vault, G refresh / wb_scan): end-to-end regressions with
REAL refresh rounds on the fake Notion (tools/tests/test_e2e_writeback.World: snapshot crawl through the read
client, real export_obsidian / verify_vault / publish_vault from a temp WORK copy of tools/*.py, temp vault).

Each test reproduces one confirmed finding; on the code before the fix it fails:
* r1-n2o-completeness-01: a local rename of a root FOLDER PAGE's folder is held (UNSUPPORTED_RENAME, subtree) and
  the rest keeps publishing; the post-publish verify item about the held object (C05 not_listed under its computed
  file name) no longer arms refresh_pause_next.json, so the following rounds are not paused;
* r1-n2o-completeness-02: a user file at a path a NEW Notion page gets (same name) is an object-level hold, not a
  whole-round pause (staging verify rejected the local_new entry: has_notion_id); once the user renames the file,
  the hold is released and the page is published;
* r1-n2o-completeness-03: a leaf with a local name override gets a Notion child -> the rejected override holds the
  subtree; the index link to the held, unpublished child is explained by the hold (no pause next round);
* r1-n2o-completeness-05 (mitigation): CONFLICT_MOVED_EDITED names the temporarily dangling links in the vault run
  log, and the empty note Obsidian creates when such a link is clicked does not pause the round;
* r1-data-safety-04: a vault file whose name is not valid UTF-8 no longer fails every round at the scan stage.

Run (from the sandbox root):
  python3 -I -B -m unittest discover -s tools/tests -p 'test_n2o_pipeline_w4.py' -v
"""
import copy
import json
import os
import sys
import unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
for _p in (HERE, TOOLS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import refresh  # noqa: E402
import test_e2e_writeback as e2e  # noqa: E402

World = e2e.World
RUNLOG = "Notion_Obsidian/运行记录"


def setUpModule():
    e2e.setUpModule()


def tearDownModule():
    e2e.tearDownModule()


class N2OBase(unittest.TestCase):
    def runlog(self, w, name):
        p = w.path(f"{RUNLOG}/{name}")
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as fh:
            return fh.read()

    def assert_not_paused(self, w, rc, st):
        self.assertIn(st.get("result"), ("published", "published_with_holds"),
                      json.dumps({k: st.get(k) for k in ("result", "error", "pause_reasons", "post_verify_unlocatable",
                                                         "staging_rejected_entries")}, ensure_ascii=False))
        self.assertEqual(rc, refresh.EXIT_OK)
        self.assertFalse(os.path.exists(w.P.pause_next), refresh.read_json(w.P.pause_next))
        self.assertFalse(os.path.exists(w.path(f"{RUNLOG}/{st['run_id']}-已暂停.md")))
        self.assertFalse(os.path.exists(w.path(f"{RUNLOG}/{st['run_id']}-未完成.md")))

    def add_root(self, w, title, text):
        """A second sidebar root (General) outside the test page's subtree."""
        pid = w.fake.add_page(title, children=[e2e.blk("paragraph", e2e.rt(text))])
        w.roots_in.append({"id": pid, "title": title, "teamspace": "General", "teamspace_order": 1, "root_order": 2})
        with open(w.sidebar, "w", encoding="utf-8") as fh:
            json.dump({"teamspace_order_required": list(e2e.vv.SPEC_TEAMSPACES), "roots": w.roots_in,
                       "order_match": True}, fh)
        return pid


class TestFolderRenameNeverPausesN2O(N2OBase):
    def rename_dir(self, w, old, new):
        os.rename(w.path(old), w.path(new))
        for root, _ds, fs in os.walk(w.vault):     # Obsidian "update internal links"
            for f in fs:
                if f.endswith(".md"):
                    p = os.path.join(root, f)
                    with open(p, encoding="utf-8") as fh:
                        t = fh.read()
                    t2 = t.replace(f"[[{old}/", f"[[{new}/")
                    if t2 != t:
                        with open(p, "w", encoding="utf-8", newline="\n") as fh:
                            fh.write(t2)

    def test_root_folder_page_rename_holds_its_subtree_and_two_rounds_never_pause(self):
        w = World(self, "fold")
        other = self.add_root(w, "另一页", "另一页正文")
        rc, st = w.refresh("R2")
        self.assert_not_paused(w, rc, st)
        self.rename_dir(w, "General/写回测试页", "General/写回测试页 本地")
        w.fake.add_blocks(other, [e2e.blk("paragraph", e2e.rt("Notion 新增的一段"))])  # the rest must publish
        rc, st = w.refresh("B1")
        self.assert_not_paused(w, rc, st)
        self.assertEqual(st["result"], "published_with_holds")
        held = st["holds"]["this_round"]
        self.assertIn(w.page, held)
        self.assertTrue(str(held[w.page]["reason"]).startswith("unsupported_rename"), held[w.page])
        plan = refresh.read_json(w.P.report("B1", "publish_plan.json"))
        by_rel = {a["rel"]: a["op"] for a in plan["actions"]}
        self.assertEqual(by_rel.get("General/另一页.md"), "REPLACE")              # outside the subtree: published
        self.assertIn("Notion 新增的一段", w.read("General/另一页.md"))
        self.assertEqual(by_rel.get("General/写回测试页/写回测试页.md"), "SKIP_HELD")
        # the C05 item the verifier cannot map (computed file name of the held folder page) is explained by the hold
        expl = st.get("post_verify_explained_by_holds") or []
        self.assertTrue(any(x["check"] == "C05" and w.page in x["held_ids"] for x in expl), expl)
        self.assertNotIn("post_verify_unlocatable", st)
        for rid in ("B2", "B3"):                     # rounds after it: never paused while the rename stays
            rc, st = w.refresh(rid)
            self.assert_not_paused(w, rc, st)
            self.assertEqual(st["result"], "published_with_holds")
        self.assertTrue(os.path.isfile(w.path("General/写回测试页 本地/写回测试页.md")))
        self.rename_dir(w, "General/写回测试页 本地", "General/写回测试页")   # renamed back: holds released
        rc, st = w.refresh("B4")
        self.assert_not_paused(w, rc, st)
        self.assertEqual(st["result"], "published", st.get("holds"))
        self.assertEqual(refresh.load_holds(w.P), {})

    def test_non_root_folder_rename_with_a_nested_folder_note_never_pauses(self):
        """Skeptic scope extension of r1-n2o-completeness-01: C05 section_missing of the nested folder note."""
        w = World(self, "fold2")
        g = w.fake.add_page("孙页面", parent=w.child, children=[e2e.blk("paragraph", e2e.rt("孙"))])
        w.fake.add_page("曾孙页面", parent=g, children=[e2e.blk("paragraph", e2e.rt("曾孙"))])
        rc, st = w.refresh("N1")
        self.assert_not_paused(w, rc, st)
        self.assertTrue(os.path.isfile(w.path("General/写回测试页/子页面/孙页面/孙页面.md")))
        self.rename_dir(w, "General/写回测试页/子页面", "General/写回测试页/子页面 本地")
        for rid in ("C1", "C2"):
            rc, st = w.refresh(rid)
            self.assert_not_paused(w, rc, st)
            self.assertEqual(st["result"], "published_with_holds")
            self.assertIn(w.child, st["holds"]["this_round"])


class TestLocalFileAtNotionPath(N2OBase):
    COLL = "General/写回测试页/撞名.md"

    def test_user_file_at_a_new_pages_path_is_object_level_and_released_after_rename(self):
        w = World(self, "coll")
        w.write(self.COLL, "我自己的笔记，没有 frontmatter\n")
        rc, st = w.refresh("E0")
        self.assert_not_paused(w, rc, st)
        new_id = w.fake.add_page("撞名", parent=w.page, children=[e2e.blk("paragraph", e2e.rt("Notion 新页面正文"))])
        w.fake.add_blocks(w.page, [e2e.blk("paragraph", e2e.rt("同一轮的另一处 Notion 改动"))])
        rc, st = w.refresh("E1")
        self.assert_not_paused(w, rc, st)
        self.assertEqual(st.get("local_new_collisions"), [self.COLL])
        self.assertNotIn("staging_rejected_entries", st)
        self.assertEqual(w.read(self.COLL), "我自己的笔记，没有 frontmatter\n")      # never overwritten
        self.assertIn("同一轮的另一处 Notion 改动", w.read(e2e.MAIN))                  # the rest is published
        hs = refresh.load_holds(w.P)
        self.assertEqual(hs[new_id]["reason"], "plan_conflict_untracked")
        self.assertEqual(hs[new_id]["release_when_absent"], self.COLL)
        log = self.runlog(w, "E1.md")
        self.assertIn("本地文件占用了 Notion 页面的路径", log)
        self.assertIn(f"`{self.COLL}`", log)
        rc, st = w.refresh("E2")                     # next round: still object-level, still not paused
        self.assert_not_paused(w, rc, st)
        os.rename(w.path(self.COLL), w.path("General/写回测试页/我的笔记.md"))
        rc, st = w.refresh("E3")
        self.assert_not_paused(w, rc, st)
        self.assertEqual(st["result"], "published", st.get("holds"))
        self.assertEqual([h["notion_id"] for h in st["holds_released"]], [new_id])
        self.assertIn("Notion 新页面正文", w.read(self.COLL))
        self.assertEqual(w.read("General/写回测试页/我的笔记.md"), "我自己的笔记，没有 frontmatter\n")


class TestRejectedOverrideNewChild(N2OBase):
    LOCAL = "General/写回测试页/子页面本地改名.md"

    def test_index_link_to_the_held_new_child_does_not_pause_the_next_round(self):
        w = World(self, "ovr")
        os.rename(w.path(e2e.CHILD), w.path(self.LOCAL))
        w.write(e2e.MAIN, w.read(e2e.MAIN).replace(f"[[{e2e.CHILD[:-3]}|", f"[[{self.LOCAL[:-3]}|"))
        rc, st = w.refresh("I0")
        self.assert_not_paused(w, rc, st)
        w.fake.add_page("孙页面", parent=w.child, children=[e2e.blk("paragraph", e2e.rt("孙页面正文"))])
        rc, st = w.refresh("I1")
        self.assert_not_paused(w, rc, st)
        self.assertEqual(st["result"], "published_with_holds")
        self.assertEqual([r["reason"] for r in st["overrides"]["rejected"]], ["folder_note"])
        expl = st.get("post_verify_explained_by_holds") or []
        self.assertTrue(any(x["check"] == "C06" for x in expl), expl)
        rc, st = w.refresh("I2")
        self.assert_not_paused(w, rc, st)


class TestConflictMovedEditedDanglingLinks(N2OBase):
    NEW = "General/写回测试页/子页面Notion改名.md"

    def test_dangling_link_targets_are_named_and_a_clicked_empty_note_does_not_pause(self):
        w = World(self, "cme")
        w.write(e2e.CHILD, w.read(e2e.CHILD) + "\n本地新增的一行\n")
        with w.fake._lock:
            w.fake._patch_page(w.child, {"properties": {"title": {"title": [{"text": {"content": "子页面Notion改名"}}]}}},
                               w.fake._tok("wb"))
        rc, st = w.refresh("R2")
        self.assert_not_paused(w, rc, st)
        self.assertEqual([c["op"] for c in st["conflicts"]], ["CONFLICT_MOVED_EDITED"])
        self.assertEqual(st.get("link_targets"), {w.child: e2e.CHILD})
        index = w.read("Notion_Obsidian/Godcreate 目录索引.md")
        self.assertIn(f"[[{e2e.CHILD[:-3]}|", index)
        self.assertNotIn(f"[[{self.NEW[:-3]}|", index)
        log = self.runlog(w, "R2.md")
        self.assertIn("冲突期间保留原笔记的路径", log)
        self.assertIn(f"`{self.NEW}`", log)
        self.assertIn(f"`{e2e.CHILD}`", log)
        w.write(self.NEW, "")                        # Obsidian creates an empty note when the link is clicked
        rc, st = w.refresh("R3")
        self.assert_not_paused(w, rc, st)
        self.assertEqual(w.read(self.NEW), "")
        self.assertIn("本地新增的一行", w.read(e2e.CHILD))


class TestUndecodableNames(N2OBase):
    def test_a_name_that_is_not_utf8_never_fails_the_round(self):
        w = World(self, "gbk")
        raw = os.fsencode(w.path("General/写回测试页")) + b"/\xc4\xe3\xba\xc3.md"
        with open(raw, "wb") as fh:
            fh.write(b"user text\n")
        for rid in ("U1", "U2"):
            rc, st = w.refresh(rid)
            self.assert_not_paused(w, rc, st)
            self.assertIn("undecodable_names", st.get("warnings") or [])
        log = self.runlog(w, "U1.md")
        self.assertIn("文件名不是有效的 UTF-8", log)
        self.assertIn("\\xc4\\xe3\\xba\\xc3.md", log)
        with open(raw, "rb") as fh:
            self.assertEqual(fh.read(), b"user text\n")


if __name__ == "__main__":
    unittest.main()
