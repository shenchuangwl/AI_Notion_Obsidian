"""Public guidance must give a runnable hold example and usable explanations."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import refresh
import wb_apply
import wb_scan
import writeback


class PublicHelp(unittest.TestCase):
    def test_hold_command_in_refresh_help_can_be_pasted(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            refresh.main(["--help"])
        line, = [s for s in output.getvalue().splitlines() if "tools/writeback.py hold --run " in s]
        cmd = line[line.index("python3 -I "):]
        self.assertNotIn("<run>", cmd)
        syntax = subprocess.run(["bash", "-n", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        prefix = "python3 -I tools/writeback.py "
        self.assertTrue(cmd.startswith(prefix), cmd)
        expanded = subprocess.check_output(["bash", "-c", "printf '%s\\n' " + cmd[len(prefix):]],
                                           env={**os.environ, "RUN_ID": "R2"}, text=True).splitlines()
        self.assertEqual(expanded, ["hold", "--run", "R2"])

    def test_operator_text_is_free_of_internal_references(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            writeback.main(["--help"])
        with mock.patch.object(wb_scan, "_scan_once", side_effect=wb_scan._Vanished("retry")):
            with self.assertRaises(wb_scan.ScanUnsettled) as error:
                wb_scan.scan("unused", "unused", None)
        page = "00000000-0000-4000-8000-000000000001"
        group = SimpleNamespace(page_id=page, snap={"tree":{"kids":{}, "by_id":{}}})
        op = {"parent_id":page, "position":{"type":"wrong"},
              "children":[{"object":"block","type":"paragraph","paragraph":{"rich_text":[]}}]}
        with self.assertRaisesRegex(wb_apply._PageStop, "APPEND position") as append_error:
            wb_apply._precheck_append(SimpleNamespace(ours=set()), group, op)
        for txt in (output.getvalue(), writeback.MODE_OFF_TEXT, str(error.exception), str(append_error.exception)):
            self.assertNotRegex(txt, r"WRITEBACK_SPEC|IMPL_CONTRACT|WB §")
