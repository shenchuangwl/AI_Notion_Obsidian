"""An object hold must not stop other Notion pages from publishing. Link rewrites must retain a proven base."""
import json
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_e2e_writeback as e2e
import test_n2o_pipeline_w4 as pipeline
from test_redirected_staging_v2 import rename_source


class ObjectHolds(pipeline.N2OBase):
    def exercise(self, duplicate=False, rename_parent=False):
        w = e2e.World(self, self._testMethodName)
        w.write_cfg(mode="plan")
        other = self.add_root(w, "独立页面", "正文保留")
        w.fake.grant("wb", other)
        self.assert_not_paused(w, *w.refresh("R2"))
        kept = Path(w.path(e2e.CHILD))
        duplicate_path = kept.with_name("子页面副本.md")
        if duplicate:
            duplicate_path.write_bytes(kept.read_bytes())
        else:
            kept.unlink()
        self.assert_not_paused(w, *w.refresh("R3"))
        rename_source(w, w.page if rename_parent else w.child, "远端改名")
        rename_source(w, other, "独立页面已改名")
        for run in ("R4", "R5"):
            rc, st = w.refresh(run)
            self.assert_not_paused(w, rc, st)
            self.assertEqual(st["result"], "published_with_holds")
            self.assertTrue(all(st["post_publish_verify"][c] == "PASS" for c in e2e.refresh.BLOCKING))
            self.assertNotIn(w.child, st.get("link_targets", {}))
            actions = json.loads(Path(w.P.report(run, "publish_plan.json")).read_text())["actions"]
            self.assertTrue(any(a["op"] in e2e.refresh.HELD_OPS and a.get("nid") == w.child for a in actions))
            self.assertIn("正文保留", w.read("General/独立页面已改名.md"))
            self.assertFalse(Path(w.path("General/独立页面.md")).exists())
            self.assertEqual(kept.exists(), duplicate)
            if duplicate:
                self.assertEqual(kept.read_bytes(), duplicate_path.read_bytes())
        self.assertEqual(w.writes(), [])

    def test_local_delete_then_remote_rename(self):
        self.exercise()

    def test_local_delete_then_remote_ancestor_rename(self):
        self.exercise(rename_parent=True)

    def test_duplicate_then_remote_rename(self):
        self.exercise(duplicate=True)


class LocalBody(pipeline.N2OBase):
    def test_local_rename_and_body_edit_do_not_seed_unpublished_bytes(self):
        w = e2e.World(self, "local-link-base")
        w.make_plan()
        old = Path(w.path(e2e.CHILD))
        new = old.with_name("本地改名.md")
        old.rename(new)
        rel = new.relative_to(w.vault).as_posix()
        w.write(e2e.MAIN, w.read(e2e.MAIN).replace("[[" + e2e.CHILD[:-3] + "|", "[[" + rel[:-3] + "|"))
        w.edit(e2e.MAIN, "第一段：普通文字。", "第一段：独立正文。")
        saved = w.read(e2e.MAIN)
        sql = "SELECT seq,sha256 FROM base_history WHERE notion_id=? ORDER BY seq"
        before = [(row["seq"], row["sha256"]) for row in w.q(sql, (w.page,))]
        self.assertTrue(before)
        self.assert_not_paused(w, *w.refresh("R2"))
        actions = json.loads(Path(w.P.report("R2", "publish_plan.json")).read_text())["actions"]
        a, = [a for a in actions if a.get("nid") == w.page]
        self.assertEqual((a["op"], a["variant"]), ("LOCAL_ONLY", "body"))
        self.assertEqual(w.read(e2e.MAIN), saved)
        self.assertEqual([(row["seq"], row["sha256"]) for row in w.q(sql, (w.page,))], before)
        pid, _, _ = w.make_plan()
        item, = [i for i in w.plan_obj(pid)[0]["items"] if i["rel_path"] == e2e.MAIN]
        self.assertTrue(item["executable"])
        self.assertEqual([op["op"] for op in item["ops"]], ["UPDATE"])

    def test_remote_target_move_keeps_conflict_and_last_real_base(self):
        w = e2e.World(self, "unpublished-link")
        nid = self.add_root(w, "链接页", "独立段落")
        url = "https://www.notion.so/" + w.child.replace("-", "")
        w.fake.add_blocks(nid, [e2e.blk("paragraph", {"type":"text", "text":{"content":"自定义文字", "link":{"url":url}}})])
        w.fake.grant("wb", nid)
        w.write_cfg(whitelist_page_ids=w.cfg["whitelist_page_ids"] + [nid])
        self.assert_not_paused(w, *w.refresh("R2"))
        w.make_plan()
        rel = "General/链接页.md"
        w.edit(rel, "独立段落", "本地独立改动")
        local = w.read(rel)
        self.assertIn(e2e.CHILD[:-3] + "|自定义文字", local)
        def history():
            return [(row["seq"], row["sha256"]) for row in w.q("SELECT seq,sha256 FROM base_history WHERE notion_id=? ORDER BY seq", (nid,))]
        last = history()
        self.assertTrue(last)
        rename_source(w, w.child, "远端改名")
        for run in ("R3", "R4"):
            self.assert_not_paused(w, *w.refresh(run))
            actions = json.loads(Path(w.P.report(run, "publish_plan.json")).read_text())["actions"]
            a, = [a for a in actions if a.get("nid") == nid]
            self.assertEqual(a["op"], "CONFLICT_BOTH")
            conflict_files = list((Path(w.vault) / "Notion_Obsidian/conflicts").rglob("*.md"))
            self.assertTrue(any("远端改名|自定义文字" in f.read_text() for f in conflict_files))
            self.assertEqual(w.read(rel), local)
            self.assertEqual(history(), last)
            plan_id, _, _ = w.make_plan()
            preview, _ = w.plan_obj(plan_id)
            self.assertFalse(any(r.get("rel_path") == rel and r.get("class") == "STALE_REVERT" for r in preview["refused"]))
        self.assertEqual(w.writes(), [])


class FailureGuidance(unittest.TestCase):
    def test_refused_redirect_explains_object_repeat_and_handbook(self):
        nid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        for err, targets in [(f"无法确认移动后笔记的唯一位置，请核对 notion_id {nid} 和库内文件 []", {}),
                             ("移动后笔记链接的暂存校验失败。", {nid:"General/保留.md"}),
                             ("重定向导出缺少 manifest.json", {nid:"General/保留.md"})]:
            with self.subTest(error=err):
                reason, step = e2e.refresh.explain_failure(dict(run_id="R3", error=err, redirect_failed_targets=targets))
                self.assertIn(nid, reason)
                self.assertIn("每一轮都会停下", step)
                self.assertIn("6.3", step)
                self.assertIn("备份", step)
                self.assertNotIn("意外错误", reason)
