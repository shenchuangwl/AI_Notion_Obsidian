"""A parent with a retained child from the last published refresh can still preview a separate paragraph edit."""
import unittest

import test_e2e_writeback as fixture


class RetainedLinkPlanning(unittest.TestCase):
    def test_edit_after_published_remote_rename_has_an_adoption_safe_preview(self):
        w = fixture.World(self, "kept-plan-parent")
        w.make_plan()  # enable planning on the clean R1 vault before any local modification
        w.write(fixture.CHILD, w.read(fixture.CHILD) + "\n保留的本地编辑\n")
        title = "远端改名"
        with w.fake._lock:
            w.fake._patch_page(w.child, {"properties": {"title": {"title": [{"text": {"content": title}}]}}},
                               w.fake._tok("wb"))
        rc, status = w.refresh("R2")
        self.assertEqual(rc, 0, status)
        self.assertEqual(status["link_targets"][w.child], fixture.CHILD)
        w.edit(fixture.MAIN, "第一段：普通文字。", "第一段：正文编辑。")

        def plan():
            pid, _, _ = w.make_plan()
            return w.plan_obj(pid)[0]

        p = plan()
        candidates = [i for i in p["items"] if i["rel_path"] == fixture.MAIN]
        self.assertEqual(len(candidates), 1, p["refused"])
        item = candidates[0]
        self.assertEqual([op["op"] for op in item["ops"]], ["UPDATE"])
        self.assertFalse(item["executable"], item)
        block = " ".join(item["blocked_by"])
        for detail in (fixture.CHILD, title, "写回", "保留", "刷新", "重新生成计划",
                       f"cd {w.work} && python3 -I ", "writeback.py plan"):
            self.assertIn(detail, block)
        # An unrelated note without retained links can still be planned in the same run.
        w.edit(fixture.DOOMED, "待删页面正文", "另一篇独立正文")
        unrelated = [i for i in plan()["items"] if i["rel_path"] == fixture.DOOMED]
        self.assertEqual(len(unrelated), 1)
        self.assertTrue(unrelated[0]["executable"], unrelated[0]["blocked_by"])

        w.write("General/写回测试页/重复身份.md", w.read(fixture.CHILD))
        p = plan()
        self.assertFalse(any(i["rel_path"] == fixture.MAIN for i in p["items"]), p)
        self.assertTrue(any(r["rel_path"] == fixture.MAIN for r in p["refused"]), p)
