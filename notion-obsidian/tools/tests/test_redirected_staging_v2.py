"""Publish gates and stable links when a remotely moved note remains at its local path."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_conflict_link_identity as kept_cases
import test_e2e_writeback as e2e
import test_export_block_lines as export_cases
import test_n2o_pipeline_w4 as n2o


def rename_source(w, nid, name):
    with w.fake._lock:
        w.fake._patch_page(nid, {"properties": {"title": {"title": [{"text": {"content": name}}]}}},
                           w.fake._tok("wb"))


class TeamspaceRoot(n2o.N2OBase):
    def test_root_leaf_keeps_one_sort_section_and_three_rounds_publish(self):
        w = e2e.World(self, "root-leaf")
        title = "另一个根页"
        nid = self.add_root(w, title, "另一个根页的正文")
        w.fake.grant("wb", nid)
        kept = f"General/{title}.md"
        self.assert_not_paused(w, *w.refresh("R2"))
        local = w.read(kept) + "\n自己保存的一行\n"
        w.write(kept, local)
        rename_source(w, nid, "远端改名")
        for run in ("R3", "R4", "R5"):
            rc, st = w.refresh(run)
            with self.subTest(round=run):
                self.assert_not_paused(w, rc, st)
                self.assertEqual(st["result"], "published_with_holds")
                self.assertEqual(st["link_targets"].get(nid), kept)
                self.assertEqual(w.read(kept), local)
                for phase in ("verify", "link_targets_verify", "post_publish_verify"):
                    self.assertTrue(all(st[phase][c] == "PASS" for c in e2e.refresh.BLOCKING), st[phase])
                spec = w.read("Notion_Obsidian/sortspec.md")
                sections = [s.splitlines() for s in spec.split("  target-folder: ")[1:]]
                self.assertEqual(len(sections), len({s[0] for s in sections}))
                top = next(s for s in sections if s[0] == "/")
                self.assertEqual([line.strip()[9:] for line in top if line.strip().startswith("/folders ")][:5],
                                 list(e2e.vv.SPEC_TEAMSPACES))
                general = next(s for s in sections if s[0] == "General")
                self.assertEqual(general.count(f"  /:files. {title}.md"), 1)
                self.assertNotIn("  /:files. 远端改名.md", general)
                self.assertIn(f"[[{kept[:-3]}|远端改名]]", w.read("Notion_Obsidian/Godcreate 目录索引.md"))
                if run == "R3":
                    log = self.runlog(w, "R3.md")
                    self.assertIn(kept, log)
                    self.assertNotIn("请不要点击", log)
                    self.assertIn("目录索引", log)


    def test_root_folder_edit_refuses_a_split_tree_and_preserves_all_children(self):
        w = e2e.World(self, "root-folder-edit")
        personal = "\n本地目录页备注\n"
        local = w.read(e2e.MAIN) + personal
        seed_time = kept_cases.page_edit_time(w, w.page)
        children = {rel: w.read(rel) for rel in (e2e.CHILD, e2e.DOOMED)}
        w.write(e2e.MAIN, local)
        kept_cases.rename_at(w, w.page, "远端根目录改名", seed_time)
        rc, st = w.refresh("split-root")
        self.assertEqual(rc, e2e.refresh.EXIT_FAIL, st)
        self.assertIn("先备份本地修改", st.get("error", ""))
        for report in (Path(w.P.report("split-root", "run_record.md")),
                       Path(w.path("Notion_Obsidian/运行记录/split-root-未完成.md"))):
            why = report.read_text()
            for detail in (w.page, "每一轮都会停下", "6.3", "备份本地修改"):
                self.assertIn(detail, why)
            self.assertNotIn("下一轮会自动重试", why)
        report = e2e.refresh.read_json(w.P.report("split-root", "verify_link_targets", "verify.json"))
        self.assertEqual(report["status_by_check"]["C03"], "FAIL")
        self.assertFalse(Path(w.P.report("split-root", "publish_journal.jsonl")).exists())
        self.assertEqual(w.read(e2e.MAIN), local)
        self.assertEqual({rel: w.read(rel) for rel in children}, children)
        kept_cases.check_folder_recovery(self, w, w.page, e2e.MAIN, "写回测试页", "远端根目录改名",
                                         seed_time, personal.encode())
        self.assertEqual({rel: w.read(rel) for rel in children}, children)


class RedirectedExportGate(unittest.TestCase):
    def test_faulty_block_line_report_stops_the_second_export_before_publication(self):
        w = e2e.World(self, "bad-link-line-map")
        w.write(e2e.CHILD, w.read(e2e.CHILD) + "\n保留正文\n")
        rename_source(w, w.child, "新路径")
        original = e2e.refresh.Runner.__call__
        before = w.read(e2e.MAIN)

        def inject_failed_selfcheck(runner, script, args, **kwargs):
            code = original(runner, script, args, **kwargs)
            if script == "export_obsidian.py" and "--link-targets" in args:
                report = Path(args[args.index("--out") + 1]) / "export_report.json"
                data = json.loads(report.read_text())
                data["block_lines"]["own_noncontiguous"] = 1
                report.write_text(json.dumps(data))
            return code

        with mock.patch.object(e2e.refresh.Runner, "__call__", inject_failed_selfcheck):
            rc, st = w.refresh("bad-map")
        self.assertNotEqual(rc, 0)
        self.assertIn("own_noncontiguous", st.get("error", ""))
        self.assertEqual(w.read(e2e.MAIN), before)
        self.assertFalse(Path(w.P.report("bad-map", "publish_journal.jsonl")).exists())

    def test_legacy_redirected_pause_requires_fresh_validation_before_confirmation(self):
        import wb_gate
        w = e2e.World(self, "legacy-paused-redirect")
        w.write_cfg(conflict_pause_threshold=1)
        w.write(e2e.CHILD, w.read(e2e.CHILD) + "\n个人修改\n")
        rename_source(w, w.child, "远端新名")
        rc, st = w.refresh("old-paused")
        self.assertEqual(rc, e2e.refresh.EXIT_PAUSED, st)
        self.assertTrue(st.get("link_targets"))
        report = Path(w.P.report("old-paused", "refresh_status.json"))
        for missing in ("link_targets_block_lines", "link_targets_verify", "link_targets"):
            with self.subTest(missing=missing):
                legacy = dict(st)
                legacy.pop(missing, None)
                report.write_text(json.dumps(legacy))
                stderr = io.StringIO()
                with mock.patch.object(wb_gate, "require_human", return_value="/dev/pts/test"), \
                        mock.patch.object(wb_gate, "confirm_yes_no", return_value=False) as ask, \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                    rc = e2e.refresh.approve_run(w.P, "old-paused")
                self.assertEqual(rc, e2e.refresh.EXIT_FAIL)
                ask.assert_not_called()
                self.assertIn("重新刷新", stderr.getvalue())
                self.assertFalse(Path(w.P.report("old-paused", "approve", "publish_journal.jsonl")).exists())
        report.write_text(json.dumps(st))
        with mock.patch.object(wb_gate, "require_human", return_value="/dev/pts/test"), \
                mock.patch.object(wb_gate, "confirm_yes_no", return_value=False) as ask, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            e2e.refresh.approve_run(w.P, "old-paused")
            ask.assert_called_once()

    def test_redirected_runlog_names_a_working_index_link(self):
        kept = "General/保留名.md"
        plan = {"actions": [{"op": "CONFLICT_MOVED_EDITED", "rel": "General/新名.md", "frm": kept,
                             "nid": export_cases.LEAF, "kind": "page"}]}
        [note] = e2e.refresh.runlog_notes({"link_targets": {export_cases.LEAF: kept}}, plan, {})
        self.assertIn("请从目录索引打开", "\n".join(note["lines"]))
        self.assertNotIn("请不要点击", "\n".join(note["lines"]))

    def test_unredirected_conflict_runlog_names_the_retained_file_without_promising_a_link(self):
        kept = "General/保留名.md"
        plan = {"actions": [{"op": "CONFLICT_MOVED_EDITED", "rel": "General/新名.md", "frm": kept,
                             "nid": export_cases.LEAF, "kind": "page"}]}
        [note] = e2e.refresh.runlog_notes({}, plan, {})
        self.assertIn(kept, "\n".join(note["lines"]))
        self.assertNotIn("请从目录索引打开", "\n".join(note["lines"]))


class DuplicateFilename(export_cases._Base):
    def test_same_folder_sortspec_emits_one_entry_for_the_concrete_filename(self):
        _, _, ex, _, _ = export_cases.export(self.fx, self.tmp, "ordinary")
        kept = ex.nodes[export_cases.LEAF2]["note"]
        out, _, _, _, _ = export_cases.export(self.fx, self.tmp, "same-file", link_targets={export_cases.LEAF: kept})
        sort = (Path(out)/"vault/Notion_Obsidian/sortspec.md").read_text()
        section = sort.split(f"  target-folder: {Path(kept).parent}\n", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertEqual(section.count(f"  /:files. {Path(kept).name}"), 1)
