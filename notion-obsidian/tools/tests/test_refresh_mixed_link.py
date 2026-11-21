"""A user saves independent parent text while Obsidian rewrites links to a renamed child."""
from pathlib import Path
import shlex
import unittest

import test_e2e_writeback as fixture


class MixedParentEdit(unittest.TestCase):
    def prepare(self):
        w = fixture.World(self, "parent-before-refresh")
        w.make_plan()
        new = fixture.CHILD.replace("子页面.md", "子页面改名.md")
        Path(w.path(fixture.CHILD)).rename(w.path(new))
        w.write(fixture.MAIN, w.read(fixture.MAIN).replace("[[" + fixture.CHILD[:-3] + "|", "[[" + new[:-3] + "|"))
        w.edit(fixture.MAIN, "第一段：普通文字。", "第一段：独立正文。")
        return w

    def test_independent_text_before_refresh_survives_and_is_plannable(self):
        w = self.prepare()
        before = w.read(fixture.MAIN)
        rc, st = w.refresh("R2")
        self.assertEqual(rc, 0, st.get("pause_reasons") or st.get("error"))
        self.assertEqual(st["result"], "published")
        self.assertEqual(w.read(fixture.MAIN), before)
        pid, _, _ = w.make_plan()
        plan, _ = w.plan_obj(pid)
        parent = [i for i in plan["items"] if i["rel_path"] == fixture.MAIN]
        self.assertEqual(len(parent), 1, plan["refused"])
        self.assertTrue(parent[0]["executable"], parent[0]["blocked_by"])
        self.assertEqual([op['op'] for op in parent[0]['ops']], ['UPDATE'])

    def test_parent_update_is_blocked_along_with_every_item_before_first_refresh(self):
        w = self.prepare()
        pid, _, _ = w.make_plan()
        plan, _ = w.plan_obj(pid)
        parents = [item for item in plan["items"] if item["rel_path"] == fixture.MAIN]
        self.assertEqual([[op["op"] for op in item["ops"]] for item in parents], [["UPDATE"]])
        self.assertTrue(any(item["category"] == "T" for item in plan["items"]))
        self.assertEqual([change["kind"] for change in plan["unregistered_renames"]], ["renamed_same_folder"])
        for item in plan["items"]:
            self.assertIn("UNREGISTERED_RENAME", item["blocked_by"], item["item_id"])
            self.assertFalse(item["executable"], item)

    def test_apply_suggested_steps_finishes_parent_body_and_child_title(self):
        w = self.prepare()
        w.edit(fixture.DOOMED, "待删页面正文", "待删页面正文（编辑）")
        rc, state = w.refresh("R2")
        self.assertEqual((rc, state["result"]), (0, "published"))
        for step in (1, 2):
            pid, sha, output = w.make_plan("--scope", "白名单")
            plan, _ = w.plan_obj(pid)
            line, = [line for line in output.splitlines() if " apply --plan " in line]
            hint_end = line.index("（需本人")
            hint_start = line.rindex("cd ", 0, line.index(" apply --plan "))
            command = line[hint_start:hint_end]
            self.assertTrue(command.startswith(f"cd {w.work} && python3 -I tools/writeback.py apply "), command)
            args = shlex.split(command.split("tools/writeback.py ", 1)[1])
            selectors = args[:args.index("--vault")] if "--vault" in args else args
            self.assertEqual(selectors[:5], ["apply", "--plan", pid, "--sha", sha])
            if step == 1:
                self.assertTrue(any(it["category"] == "T" and it["executable"] for it in plan["items"]))
                self.assertIn("--items", selectors)
            else:
                self.assertIn("--rename", selectors)
            env = fixture.clean_env(WB_BASE_URL=w.base, WB_READ_TOKEN_FILE=w.read_token_file, WB_TOKEN_FILE=w.token_file)
            with fixture.pexpect.spawn("bash", ["-c", command], env=env, encoding="utf-8", timeout=90) as child:
                w.challenge(child)
                child.expect(fixture.pexpect.EOF)
                output_apply = child.before
                child.close()
                rc = child.exitstatus
            result = w.result(pid)
            self.assertEqual((rc, result["status"]), (0, "applied"), (output_apply, result["w_checks"]))
            expected = {f"W{i}": "pass" for i in range(1, 11)}
            expected.update(W4="n/a", W9="n/a")  # Both groups contain zero deletes and create zero blocks.
            if step == 1:
                expected["W5"] = "n/a"  # The first group only updates existing paragraphs.
            self.assertEqual({k:v["status"] for k,v in result["w_checks"].items()}, expected)
            self.assertIn("无删除项", result["w_checks"]["W4"]["detail"])
            self.assertIn("新建块 0", result["w_checks"]["W9"]["detail"])

            if step == 1:
                self.assertNotIn("--rename", selectors)
                self.assertIn("无改名项", result["w_checks"]["W5"]["detail"])
                self.assertIn("plan --scope " + shlex.quote("白名单"), output)
                self.assertEqual(w.title(w.child), "子页面")
                self.assertIn(("paragraph", "第一段：独立正文。", []), w.tree(w.page))
        self.assertEqual(w.title(w.child), "子页面改名")
        self.assertIn(("paragraph", "第一段：独立正文。", []), w.tree(w.page))
        self.assertIn("第一段：独立正文。", w.read(fixture.MAIN))
        self.assertIn("[[" + fixture.CHILD_NEW[:-3] + "|", w.read(fixture.MAIN))
        self.assertTrue(Path(w.path(fixture.CHILD_NEW)).is_file())
        pid, _, _ = w.make_plan("--scope", "白名单")
        self.assertFalse(w.plan_obj(pid)[0]["items"])

    def test_real_remote_text_and_independent_local_edit_pause(self):
        w = self.prepare()
        before = w.read(fixture.MAIN)
        w.fake.add_blocks(w.page, [fixture.blk('paragraph', fixture.rt('远端独立内容'))])
        rc, st = w.refresh('R2')
        self.assertEqual(rc, 2, st.get('error'))
        self.assertTrue(any('conflict_on_page_with_children' in s for s in st['pause_reasons']))
        self.assertEqual(w.read(fixture.MAIN), before)
