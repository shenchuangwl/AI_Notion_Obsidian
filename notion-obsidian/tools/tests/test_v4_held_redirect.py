"""Real refreshes against a loopback fake source, with a note-only hold surviving remote moves."""
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_e2e_writeback as e2e
import test_n2o_pipeline_w4 as pipeline
from test_redirected_staging_v2 import rename_source


def read_report(w, run, name):
    return json.loads(Path(w.P.report(run, name)).read_text())


def make_sync_copy(note):
    copy = note.with_name(note.stem + " (Conflicted copy 2026-11-21 0901).md")
    shutil.copy2(note, copy)
    return copy


class HeldRedirect(pipeline.N2OBase):
    def exercise_folder(self, cross=False, strict=False, corrupt=False, optional_leaf=False, post_fault=False):
        w = e2e.World(self, self._testMethodName)
        w.write_cfg(mode="plan")
        folder = w.fake.add_page("中间页", parent=w.page)
        children = {name: w.fake.add_page(name, parent=folder, children=[e2e.blk("paragraph", e2e.rt(name))])
                    for name in ("孙页", "孙页二")}
        other = self.add_root(w, "独立页面", "独立正文")
        destination = w.fake.add_page("量化根页")
        w.fake.add_page("量化子页", parent=destination)
        w.roots_in.append({"id": destination, "title": "量化根页", "teamspace": "Quantitative Investing",
                           "teamspace_order": 2, "root_order": 1})
        Path(w.sidebar).write_text(json.dumps({"teamspace_order_required": list(e2e.vv.SPEC_TEAMSPACES),
                                               "roots": w.roots_in, "order_match": True}))
        w.fake.grant("wb", other, destination)
        self.assert_not_paused(w, *w.refresh("R2"))
        old_dir = Path(w.path("General/写回测试页/中间页"))
        folder_note = old_dir / "中间页.md"
        original = folder_note.read_bytes()
        sync_copy = make_sync_copy(folder_note)
        if optional_leaf:
            make_sync_copy(Path(w.path(e2e.DOOMED)))
        self.assert_not_paused(w, *w.refresh("R3"))
        if cross:
            with w.fake._lock:
                w.fake.kids[w.fake.up[folder]].remove(folder)
                w.fake._insert_child(destination, folder)
                w.fake._touch(folder, w.fake._tok("wb"))
            new_dir = "Quantitative Investing/量化根页/中间页"
            new_note = new_dir + "/中间页.md"
        else:
            rename_source(w, folder, "中间页改名")
            new_dir = "General/写回测试页/中间页改名"
            new_note = new_dir + "/中间页改名.md"
        rename_source(w, other, "独立页面已改名")
        if strict:
            w.edit(e2e.CHILD, "子页面正文", "必须保留的本地正文")
            rename_source(w, w.child, "远端子页")
        if optional_leaf:
            rename_source(w, w.doomed, "保留叶新名")
            w.write("General/写回测试页/保留叶新名.md", "个人文件\n")
        original_run = e2e.refresh.Runner.__call__

        def run(runner, script, args, **kw):
            code = original_run(runner, script, args, **kw)
            if corrupt and script == "export_obsidian.py" and "--link-targets" in args:
                index = Path(args[args.index("--out") + 1]) / "vault/Notion_Obsidian/Godcreate 目录索引.md"
                with index.open("a", encoding="utf-8") as out:
                    out.write("\n[[General/不存在的笔记]]\n")
            return code

        real_post = e2e.refresh.Round.post_verify

        def post(rnd, *args, **kwargs):
            if post_fault:
                Path(w.path(new_dir + "/孙页.md")).rename(old_dir / "孙页.md")
                spec = "Notion_Obsidian/sortspec.md"
                w.write(spec, w.read(spec).replace("target-folder: " + new_dir + "\n",
                                                   "target-folder: General/缺失目录\n"))
            return real_post(rnd, *args, **kwargs)

        for rid in (("R4",) if post_fault else ("R4", "R5")):
            with mock.patch.object(e2e.refresh.Runner, "__call__", run), mock.patch.object(e2e.refresh.Round, "post_verify", post):
                rc, st = w.refresh(rid)
            with self.subTest(run=rid):
                if post_fault:
                    self.assertEqual((rc, st["result"]), (1, "published_post_verify_failed"))
                    verification = read_report(w, rid, "post_publish_verify/verify.json")["checks"]
                    self.assertEqual(verification["C03"]["status"], "FAIL")
                    self.assertTrue(any(i.get("id") == children["孙页"] and i.get("issue") == "wrong_location"
                                        for i in verification["C03"]["details"]["issues"]))
                    self.assertEqual(verification["C05"]["status"], "FAIL")
                    self.assertTrue(any(i.get("folder") == new_dir and i.get("issue") == "section_missing"
                                        for i in verification["C05"]["details"]["problems"]))
                    continue
                if corrupt:
                    self.assertEqual((rc, st["result"]), (1, "failed"))
                    refused = read_report(w, rid, "verify_link_targets/verify.json")
                    self.assertEqual(refused["status_by_check"]["C06"], "FAIL")
                    self.assertEqual(st["redirect_failed_targets"], {w.child: e2e.CHILD})
                    self.assertFalse(Path(w.P.report(rid, "publish_journal.jsonl")).exists())
                    self.assertTrue(Path(w.path("General/独立页面.md")).is_file())
                    continue
                self.assert_not_paused(w, rc, st)
                self.assertEqual(st["result"], "published_with_holds")
                for phase in ("verify", "post_publish_verify") + (("link_targets_verify",) if strict or optional_leaf else ()):
                    self.assertEqual({c: st[phase].get(c) for c in e2e.refresh.BLOCKING},
                                     dict.fromkeys(e2e.refresh.BLOCKING, "PASS"), phase)
                expected_targets = {w.child: e2e.CHILD} if strict else {}
                if optional_leaf:
                    expected_targets[w.doomed] = e2e.DOOMED
                    self.assertIn("[[" + e2e.DOOMED[:-3] + "|保留叶新名]]", w.read(e2e.MAIN))
                    self.assertEqual(w.read("General/写回测试页/保留叶新名.md"), "个人文件\n")
                self.assertEqual(st.get("link_targets", {}), expected_targets)
                actions = read_report(w, rid, "publish_plan.json")["actions"]
                hold = read_report(w, rid, "holds.json")[folder]
                self.assertEqual((hold["reason"], hold["subtree"]), ("sync_conflict_copy", False))
                self.assertTrue(any(a["op"] in e2e.refresh.HELD_OPS and a.get("nid") == folder for a in actions))
                if rid == "R4":
                    self.assertTrue(any(a["op"] == "MOVE" and a.get("nid") == other for a in actions))
                other_note = w.read("General/独立页面已改名.md")
                self.assertIn(other, other_note)
                self.assertIn("独立正文", other_note)
                self.assertFalse(Path(w.path("General/独立页面.md")).exists())
                self.assertEqual(folder_note.read_bytes(), original)
                self.assertEqual(sync_copy.read_bytes(), original)
                self.assertIn(folder, original.decode())
                for name, nid in children.items():
                    self.assertFalse((old_dir / (name + ".md")).exists())
                    self.assertIn(nid, w.read(new_dir + "/" + name + ".md"))
                self.assertIn("[[" + new_note[:-3] + "|", w.read("Notion_Obsidian/Godcreate 目录索引.md"))
                if strict:
                    self.assertIn("必须保留的本地正文", w.read(e2e.CHILD))
                    self.assertIn("[[" + e2e.CHILD[:-3] + "|远端子页]]", w.read(e2e.MAIN))
        self.assertEqual(w.writes(), [])

    def test_note_only_hold_renamed_folder(self):
        self.exercise_folder()

    def test_note_only_hold_moved_across_teamspaces(self):
        self.exercise_folder(cross=True)

    def test_strict_redirect_survives_held_folder_fallback(self):
        self.exercise_folder(strict=True)

    def test_valid_held_leaf_keeps_identity_when_held_folder_redirect_fails(self):
        self.exercise_folder(optional_leaf=True)

    def test_wrong_child_and_missing_canonical_sort_section_still_fail_post_verify(self):
        self.exercise_folder(post_fault=True)

    def test_strict_redirect_is_reverified_after_fallback(self):
        self.exercise_folder(strict=True, corrupt=True)


class OldNameReuse(pipeline.N2OBase):
    def exercise(self, sync):
        w = e2e.World(self, self._testMethodName)
        other = self.add_root(w, "独立页面", "独立正文")
        w.fake.grant("wb", other)
        self.assert_not_paused(w, *w.refresh("R2"))
        note = Path(w.path(e2e.CHILD))
        if sync:
            copy = make_sync_copy(note)
        else:
            w.edit(e2e.CHILD, "子页面正文", "本地独立修改")
        original = note.read_bytes()
        self.assert_not_paused(w, *w.refresh("R3"))
        rename_source(w, w.child, "子页面新名")
        new = w.fake.add_page("子页面", parent=w.page, children=[e2e.blk("paragraph", e2e.rt("新同级页正文"))])
        rename_source(w, other, "独立页面已改名")
        for rid in ("R4", "R5"):
            rc, st = w.refresh(rid)
            with self.subTest(run=rid):
                self.assertEqual((rc, st["result"]), (2, "paused_for_review"))
                text = w.read(f"Notion_Obsidian/运行记录/{rid}-已暂停.md")
                for detail in (w.child, new, e2e.CHILD, "其他页面也会暂停", "库外备份", "唯一名称"):
                    self.assertIn(detail, text)
                self.assertNotIn("扫描器缺陷", text)
                self.assertNotIn("L-06", text)
                self.assertNotIn("--approve-run", text)
                self.assertFalse(Path(w.P.report(rid, "publish_journal.jsonl")).exists())
                self.assertTrue(Path(w.path("General/独立页面.md")).is_file())
                self.assertEqual(note.read_bytes(), original)
                if sync:
                    self.assertEqual(copy.read_bytes(), original)
        import wb_gate
        with mock.patch.object(wb_gate, "require_human", return_value="/dev/pts/test"), \
                mock.patch.object(wb_gate, "confirm_yes_no", side_effect=AssertionError("must refuse before prompt")):
            self.assertEqual(e2e.refresh.approve_run(w.P, "R5"), e2e.refresh.EXIT_FAIL)
        rename_source(w, new, "同级页面唯一名称")
        self.assert_not_paused(w, *w.refresh("R6"))
        self.assertIn(new, w.read("General/写回测试页/同级页面唯一名称.md"))
        self.assertIn(other, w.read("General/独立页面已改名.md"))
        self.assertEqual(note.read_bytes(), original)
        self.assertEqual(w.writes(), [])

    def test_sync_hold_and_new_sibling_reusing_old_name(self):
        self.exercise(sync=True)

    def test_local_edit_and_new_sibling_reusing_old_name(self):
        self.exercise(sync=False)


class ApplyRefusal(pipeline.N2OBase):
    def exercise(self, missing):
        w = e2e.World(self, self._testMethodName)
        kept = Path(w.path(e2e.CHILD))
        copy = make_sync_copy(kept)
        original = kept.read_bytes()
        self.assert_not_paused(w, *w.refresh("R2"))
        rename_source(w, w.child, "子页面新名")
        real_apply = e2e.refresh.Round.apply

        def concurrent_save(rnd, *args, **kwargs):
            if missing:
                kept.unlink()
            else:
                kept.write_text("个人笔记\n")
            return real_apply(rnd, *args, **kwargs)

        with mock.patch.object(e2e.refresh.Round, "apply", concurrent_save):
            rc, st = w.refresh("R3")
        self.assertEqual((rc, st["result"]), (1, "failed"))
        reason, step = e2e.refresh.explain_failure(st)
        self.assertIn(e2e.CHILD, reason)
        self.assertIn(w.child, reason)
        self.assertIn("发布前复核", reason)
        self.assertIn("重新扫描", step)
        self.assertIn("6.3", step)
        self.assertIn("库外备份", step)
        for text in (w.read("Notion_Obsidian/运行记录/R3-未完成.md"), Path(w.P.report("R3", "run_record.md")).read_text()):
            self.assertIn(reason, text)
            self.assertNotIn("意外错误", text)
        self.assertFalse(Path(w.P.report("R3", "publish_journal.jsonl")).exists())
        self.assertEqual(copy.read_bytes(), original)
        if missing:
            self.assertFalse(kept.exists())
        else:
            self.assertEqual(kept.read_text(), "个人笔记\n")
        self.assertEqual(w.writes(), [])

    def test_missing_retained_note_before_apply(self):
        self.exercise(missing=True)

    def test_new_identity_at_retained_path_before_apply(self):
        self.exercise(missing=False)


class LocalFolderMove(pipeline.N2OBase):
    def exercise(self, across):
        w = e2e.World(self, self._testMethodName)
        other = self.add_root(w, "独立页面", "独立正文")
        w.fake.grant("wb", other)
        self.assert_not_paused(w, *w.refresh("R2"))
        if across:
            retained = Path(w.path("Quantitative Investing/写回测试页.md"))
            Path(w.path(e2e.MAIN)).rename(retained)
        else:
            retained = Path(w.path("General/写回测试页 本地/写回测试页.md"))
            pipeline.TestFolderRenameNeverPausesN2O.rename_dir(self, w, "General/写回测试页", "General/写回测试页 本地")
        self.assert_not_paused(w, *w.refresh("R3"))
        original = retained.read_bytes()
        rename_source(w, w.page, "目录页远端改名")
        rename_source(w, other, "独立页面已改名")
        for rid in ("R4", "R5"):
            rc, st = w.refresh(rid)
            with self.subTest(run=rid):
                self.assert_not_paused(w, rc, st)
                self.assertEqual(st["result"], "published_with_holds")
                self.assertEqual(retained.read_bytes(), original)
                self.assertIn(other, w.read("General/独立页面已改名.md"))
                self.assertFalse(Path(w.path("General/独立页面.md")).exists())
        self.assertEqual(w.writes(), [])

    def test_locally_renamed_folder_then_remote_title_change(self):
        self.exercise(across=False)

    def test_locally_moved_folder_note_then_remote_title_change(self):
        self.exercise(across=True)

class RendererGuidance(pipeline.N2OBase):
    def test_new_renderer_pause_uses_user_facing_reason(self):
        w = e2e.World(self, "renderer-guidance")
        prior = Path(w.P.last_published_json)
        baseline = json.loads(prior.read_text())
        baseline["renderer_version"] = ["old-test-renderer"]
        prior.write_text(json.dumps(baseline))
        rc, st = w.refresh("R2")
        self.assertEqual((rc, st["result"]), (2, "paused_for_review"))
        text = w.read("Notion_Obsidian/运行记录/R2-已暂停.md")
        self.assertIn("old-test-renderer", text)
        self.assertIn("人工核对", text)
        self.assertNotIn("D-15", text)
        self.assertEqual(w.writes(), [])
