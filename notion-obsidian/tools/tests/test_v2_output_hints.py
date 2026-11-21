"""User-visible help only. No live service, protected credential, or write-back execution."""
from pathlib import Path
import re
import shlex
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
for directory in (HERE, HERE.parent):
    sys.path.insert(0, str(directory))
import refresh
import wb_common
import wb_plan
import writeback
from test_writeback_cli import run_main


class ActionMessages(unittest.TestCase):
    def test_show_missing_state_provides_plan_command_in_same_workspace(self):
        with tempfile.TemporaryDirectory(prefix="data with spaces ") as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            rc, out, err = run_main(["--work", tmp, "--vault", str(vault), "show", "--plan", "wb-example"])
            self.assertEqual(rc, 2)
            self.assertIn("没有任何计划", err)
            self.assertIn("cd " + shlex.quote(tmp) + " && python3 -I ", err)
            self.assertIn("writeback.py plan", err)
            self.assertFalse((Path(tmp) / "state").exists())

    def test_pause_guidance_and_viewed_commands_are_human_readable(self):
        detail = refresh.pause_details(["local_mass_delete: 6"], new_missing_rels=["我的 writeback.py plan.md"],
                                       run_id="R111")
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            p = refresh.Paths(root, vault)
            paused = refresh.write_paused_note(p, "R111", {"pause_reasons": ["local_mass_delete: 6"],
                                                          "pause_details": detail})
            note = Path(paused).read_text()
        self.assertIn("我的 writeback.py plan.md", note)
        self.assertNotRegex(note, r"WRITEBACK_SPEC|\bWB §?[0-9]|[（(]7\.4\.3[）)]")
        self.assertRegex(note, r"cd .*python3 -I [^\n]*refresh\.py --discard-run R111")
        msg = wb_plan.blocked_text("SYNC_SETTLING：文件仍在同步")
        self.assertIn("cd " + shlex.quote(wb_common.WORK) + " && python3 -I tools/writeback.py plan", msg)
