"""Rehearse Obsidian link spelling changes around child-page renames (UJ-08)."""
import json
import os
import unittest

import test_wb_plan as fixture
import writeback


class TestParentLinkEdits(unittest.TestCase):
    parent = "General/Home/Folder/Folder.md"
    kid = "General/Home/Folder/Kid.md"
    new = "General/Home/Folder/Kid new.md"
    sib = "General/Home/Folder/Sib.md"

    def world(self):
        def add_sibling(e):
            e.ids["sib"] = e.fake.add_page("Sib", parent=e.ids["folder"], children=[fixture.para("sibling")])
        return fixture.Env(self, seed=add_sibling)

    def rename(self, e):
        os.rename(e.path(self.kid), e.path(self.new))
        e.edit(self.parent, "[[" + self.kid[:-3] + "|", "[[" + self.new[:-3] + "|")

    def pending(self, e):
        status = writeback.status_data(e.paths, e.cfg, e.db)
        return status["pending_local_changes"]["待写回的本地修改 local_modified"]

    def test_b_and_c_only_link_spelling_is_absent_from_pending_even_with_deleted_sibling(self):
        e = self.world()
        self.rename(e)
        os.unlink(e.path(self.sib))
        for registered in (False, True):
            with self.subTest(registered=registered):
                if registered:
                    e.register_rename("kid", self.kid, self.new)
                p, _, _ = e.plan()
                self.assertTrue(any(r["rel_path"] == self.parent and r["class"] == "LINK_REWRITE_NOOP"
                                    for r in p["refused"]))
                self.assertNotIn(self.parent, self.pending(e))

    def test_d_rename_and_body_edit_plan_only_the_paragraph(self):
        e = self.world()
        self.rename(e)
        e.edit(self.parent, "folder body", "folder body EDITED")
        for registered in (False, True):
            with self.subTest(registered=registered):
                if registered:
                    e.register_rename("kid", self.kid, self.new)
                self.assertIn(self.parent, self.pending(e))
                p, _, _ = e.plan()
                items = [it for it in p["items"] if it["rel_path"] == self.parent]
                self.assertEqual([it["category"] for it in items], ["M"], p["refused"])
                ops = items[0]["ops"]
                self.assertEqual([op["op"] for op in ops], ["UPDATE"])
                self.assertEqual(ops[0]["block_type"], "paragraph")
                rich = ops[0]["request"]["paragraph"]["rich_text"]
                self.assertEqual("".join(part["text"]["content"] for part in rich), "folder body EDITED")
                self.assertFalse(items[0]["executable"])  # this stage still protects non-leaf pages

    def test_d_when_registered_render_matches_new_path_full_mode_allows_body_update(self):
        e = self.world()
        self.rename(e)
        e.register_rename("kid", self.kid, self.new)
        e.edit(self.parent, "folder body", "folder body EDITED")
        e.set_cfg(mode="full")
        # A successful refresh records the effective spelling for the next normal planner call.
        with open(os.path.join(e.work, "reports", "R1", "overrides.json"), "w", encoding="utf-8") as f:
            json.dump({e.ids["kid"]: self.new}, f)
        p, _, _ = e.plan()
        items = [it for it in p["items"] if it["rel_path"] == self.parent]
        self.assertEqual(len(items), 1, p["refused"])
        item = items[0]
        self.assertEqual(item["category"], "M")
        self.assertTrue(item["executable"], item["blocked_by"])
        self.assertEqual([op["op"] for op in item["ops"]], ["UPDATE"])

    def test_mixed_noncanonical_spelling_keeps_body_op_but_protects_adoption(self):
        e = self.world()
        e.set_cfg(mode="full")
        e.edit(self.parent, "[[" + self.kid[:-3] + "|", "[[Kid|")
        e.edit(self.parent, "folder body", "folder body EDITED")
        p, _, _ = e.plan()
        items = [it for it in p["items"] if it["rel_path"] == self.parent]
        self.assertEqual(len(items), 1, p["refused"])
        item = items[0]
        self.assertEqual(item["category"], "M")
        self.assertEqual([op["op"] for op in item["ops"]], ["UPDATE"])
        self.assertFalse(item["executable"])
        block = " ".join(item["blocked_by"])
        self.assertIn("恢复为导出写法", block)
        self.assertIn(f"cd {e.work} && python3 -I ", block)
        self.assertIn("writeback.py plan", block)

    def test_e_deleted_generated_sibling_link_identified_separately_from_renamed_link(self):
        e = self.world()
        self.rename(e)
        os.unlink(e.path(self.sib))
        e.edit(self.parent, "\n[[General/Home/Folder/Sib|Sib]]\n", "\n")
        self.assertIn(self.parent, self.pending(e))
        p, _, _ = e.plan()
        refusal, = [r for r in p["refused"] if r["rel_path"] == self.parent]
        self.assertEqual(refusal["class"], "UNSUPPORTED_REGION")
        self.assertIn("Sib", " ".join(refusal["where"]["b_text"]), refusal)
        self.assertIn("还原", refusal["hint"])
        self.assertIn("重新", refusal["hint"])
        self.assertIn(f"cd {e.work} && python3 -I ", refusal["hint"])
        self.assertIn("writeback.py plan", refusal["hint"])
        self.assertFalse(any(it["rel_path"] == self.parent for it in p["items"]))

    def test_a_different_existing_target_is_not_a_spelling_change(self):
        e = self.world()
        os.rename(e.path(self.kid), e.path(self.new))
        e.edit(self.parent, "[[" + self.kid[:-3] + "|", "[[" + self.sib[:-3] + "|")
        self.assertIn(self.parent, self.pending(e))
        p, _, _ = e.plan()
        self.assertFalse(any(it["rel_path"] == self.parent for it in p["items"]))
        self.assertTrue(any(r["rel_path"] == self.parent and r["class"] == "UNSUPPORTED_REGION" for r in p["refused"]))

    def test_personal_target_replacing_old_link_remains_visible(self):
        e = self.world()
        os.rename(e.path(self.kid), e.path(self.new))
        e.write("General/Home/Folder/Personal.md", "local personal note")
        e.edit(self.parent, "[[" + self.kid[:-3] + "|", "[[Personal|")
        self.assertIn(self.parent, self.pending(e))
        p, _, _ = e.plan()
        self.assertFalse(any(it["rel_path"] == self.parent for it in p["items"]))
        self.assertTrue(any(r["rel_path"] == self.parent and r["class"] == "UNSUPPORTED_REGION" for r in p["refused"]))

    def test_changed_alias_is_not_an_automatic_spelling_change(self):
        e = self.world()
        self.rename(e)
        e.edit(self.parent, "Kid new|Kid]]", "Kid new|Changed title]]")
        self.assertIn(self.parent, self.pending(e))
        p, _, _ = e.plan()
        self.assertFalse(any(it["rel_path"] == self.parent for it in p["items"]))
        self.assertTrue(any(r["rel_path"] == self.parent and r["class"] == "UNSUPPORTED_REGION" for r in p["refused"]))
