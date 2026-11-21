"""Read the release's actual pause notes, backlog preview and show output."""
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_e2e_writeback as e2e
import test_refresh_pause as pause


class PauseNotes(pause.Harness):
    def test_renderer_change_reason(self):
        self.seed_all_base(rv="1.2.0")
        rc, status = self.run_round()
        note = self.assert_paused(rc, status, "renderer_version_changed")
        self.assertNotIn("D-15", note)

    def test_rejected_deviation_reason(self):
        self.verify_override["staging"] = {"rejected": [
            {"category": "local_new", "path": "x.md", "reason": "has_notion_id"}]}
        rc, status = self.run_round()
        note = self.assert_paused(rc, status, "expected_deviations_rejected_entries")
        self.assertIn("x.md", note)
        self.assertNotIn("L-06", note)


class FirstPreview(unittest.TestCase):
    def test_initial_backlog_in_preview_and_show(self):
        world = e2e.World(self, name="backlog-help")
        world.edit(e2e.DOOMED, "待删页面正文", "待删页面正文（启用前的本地编辑）")
        rc, output, err = world.main("plan")
        self.assertEqual(rc, 0, err)
        plan_id = re.search(r"plan_id：(\S+)", output)[1]
        preview = world.read(f"Notion_Obsidian/写回预览/{plan_id}.md")
        rc, show, err = world.main("show", "--plan", plan_id)
        self.assertEqual(rc, 0, err)
        for text in (preview, show):
            self.assertIn("INITIAL_BACKLOG", text)
            self.assertNotIn("D-20", text)
