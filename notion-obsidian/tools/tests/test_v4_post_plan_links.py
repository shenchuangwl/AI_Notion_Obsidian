"""Mixed parent edits use each link's post-publication destination on real fake-Notion refresh rounds."""
import json
import posixpath
import re
from pathlib import Path

import test_e2e_writeback as e2e
from test_n2o_pipeline_w4 import N2OBase
from test_redirected_staging_v2 import rename_source


def linkpath(setting, src, dest):
    target = dest.removesuffix(".md")
    if setting == "shortest":
        return posixpath.basename(target)
    if setting == "relative":
        return "./" + posixpath.relpath(target, posixpath.dirname(src))
    return target


def action(w, run, rel):
    plan = json.loads(Path(w.P.report(run, "publish_plan.json")).read_text())
    entry, = [a for a in plan["actions"] if a["rel"] == rel]
    return entry


def history(w, nid):
    sql = "SELECT seq,sha256 FROM base_history WHERE notion_id=? ORDER BY seq"
    return [(r["seq"], r["sha256"]) for r in w.q(sql, (nid,))]


class MixedLinkFormats(N2OBase):
    def check_local_rename(self, setting):
        w = e2e.World(self, "a3-" + setting)
        w.write(".obsidian/app.json", json.dumps({"newLinkFormat": setting}))
        w.make_plan()
        Path(w.path(e2e.CHILD)).rename(w.path(e2e.CHILD_NEW))
        spelling = linkpath(setting, e2e.MAIN, e2e.CHILD_NEW)
        # Obsidian's page mention and child link both follow the moved file. Keep one alias and omit the other.
        links = iter(["[[" + spelling + "|子页面]]", "[[" + spelling + "]]"])
        old = r"\[\[" + re.escape(e2e.CHILD[:-3]) + r"\|[^\]]*\]\]"
        body, count = re.subn(old, lambda _: next(links), w.read(e2e.MAIN))
        self.assertEqual(count, 2)
        w.write(e2e.MAIN, body)
        w.edit(e2e.MAIN, "第一段：普通文字。", "第一段：独立修改。")
        saved = w.read(e2e.MAIN)
        base = history(w, w.page)
        self.assertTrue(base)
        # Before scan/registration, the pending local rename blocks every item, including its own.
        pid, _, _ = w.make_plan()
        preview, _ = w.plan_obj(pid)
        self.assertTrue(preview["unregistered_renames"])
        self.assertTrue(any(i["category"] == "T" for i in preview["items"]))
        for item in preview["items"]:
            self.assertFalse(item["executable"])
            self.assertIn("UNREGISTERED_RENAME", item["blocked_by"])
        for run in ("R2", "R3"):
            marker = "无关 Notion 修改 " + run
            w.fake.add_blocks(w.doomed, [e2e.blk("paragraph", e2e.rt(marker))])
            rc, st = w.refresh(run)
            self.assert_not_paused(w, rc, st)
            self.assertEqual(st["result"], "published")
            a = action(w, run, e2e.MAIN)
            self.assertEqual((a["op"], a["variant"]), ("LOCAL_ONLY", "body"))
            self.assertIn(marker, w.read(e2e.DOOMED))
            self.assertEqual(w.read(e2e.MAIN), saved)
            self.assertEqual(history(w, w.page), base)
        self.assertFalse(list((Path(w.vault) / "Notion_Obsidian/conflicts").rglob("*.md")))
        self.assertEqual(w.writes(), [])

    def test_a3_shortest_with_and_without_alias(self):
        self.check_local_rename("shortest")

    def test_a3_relative_with_and_without_alias(self):
        self.check_local_rename("relative")

    def test_a3_absolute_with_and_without_alias(self):
        self.check_local_rename("absolute")

    def make_link_page(self, w):
        nid = self.add_root(w, "链接页", "独立段落")
        url = "https://www.notion.so/" + w.child.replace("-", "")
        rich = {"type": "text", "text": {"content": "自定义文字", "link": {"url": url}}}
        w.fake.add_blocks(nid, [e2e.blk("paragraph", rich)])
        w.fake.grant("wb", nid)
        w.write_cfg(whitelist_page_ids=w.cfg["whitelist_page_ids"] + [nid])
        self.assert_not_paused(w, *w.refresh("R2"))
        w.make_plan()
        rel = "General/链接页.md"
        w.edit(rel, "独立段落", "本地独立修改")
        return nid, rel

    def check_remote_rename(self, setting):
        w = e2e.World(self, "b2-" + setting)
        nid, rel = self.make_link_page(w)
        old_path = linkpath(setting, rel, e2e.CHILD)
        w.edit(rel, "[[" + e2e.CHILD[:-3] + "|自定义文字]]", "[[" + old_path + "|自定义文字]]")
        saved, base = w.read(rel), history(w, nid)
        self.assertTrue(base)
        rename_source(w, w.child, "远端改名")
        for run in ("R3", "R4"):
            self.assert_not_paused(w, *w.refresh(run))
            self.assertEqual(action(w, run, rel)["op"], "CONFLICT_BOTH")
            self.assertEqual(w.read(rel), saved)
            self.assertEqual(history(w, nid), base)
            conflicts = (Path(w.vault) / "Notion_Obsidian/conflicts").rglob("*.md")
            self.assertTrue(any("远端改名|自定义文字" in p.read_text() for p in conflicts))
            plan_id, _, _ = w.make_plan()
            preview, _ = w.plan_obj(plan_id)
            self.assertFalse(any(r.get("rel_path") == rel and r["class"] == "STALE_REVERT"
                                 for r in preview["refused"]))
        self.assertEqual(w.writes(), [])

    def test_b2_remote_rename_invalidates_shortest_link(self):
        self.check_remote_rename("shortest")

    def test_b2_remote_rename_invalidates_relative_link(self):
        self.check_remote_rename("relative")

    def test_b2_remote_rename_invalidates_absolute_link(self):
        self.check_remote_rename("absolute")

    def check_unproven_target(self, ambiguous):
        w = e2e.World(self, "ambiguous" if ambiguous else "wrong-note")
        nid, rel = self.make_link_page(w)
        Path(w.path(e2e.CHILD)).rename(w.path(e2e.CHILD_NEW))
        # From the link page in General, neither of these two identically named notes is a sibling.
        if ambiguous:
            w.write("General/其他文件夹/子页面改名.md", "无关本地笔记")
        spelling = "子页面改名" if ambiguous else "待删页面"
        w.edit(rel, "[[" + e2e.CHILD[:-3] + "|自定义文字]]", "[[" + spelling + "|自定义文字]]")
        saved, base = w.read(rel), history(w, nid)
        for run in ("R3", "R4"):
            self.assert_not_paused(w, *w.refresh(run))
            self.assertEqual(action(w, run, rel)["op"], "CONFLICT_BOTH")
            self.assertEqual(w.read(rel), saved)
            self.assertEqual(history(w, nid), base)
        self.assertEqual(w.writes(), [])

    def test_ambiguous_shortest_link_is_protected(self):
        self.check_unproven_target(True)

    def test_link_to_a_different_managed_note_is_protected(self):
        self.check_unproven_target(False)

    def test_anchored_paths_and_case_collisions_require_a_unique_destination(self):
        from publish_vault import Resolver
        notes = {"Other/General/Old.md": "moved", "General/Same.md": "one", "General/same.md": "two"}
        resolver = Resolver(notes, lambda path: ("id", notes[path]))
        for spelling in ("./Old", "/General/Old", "General/Same.md", "./same"):
            with self.subTest(spelling=spelling):
                self.assertIsNone(resolver.resolve(spelling, "General/Parent.md"))
