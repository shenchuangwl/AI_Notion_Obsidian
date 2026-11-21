"""Printed next steps only. No confirmation, token loading, HTTP or write-back execution."""
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import refresh
import wb_apply
import wb_common
import wb_config
import writeback


class TestOperability06(unittest.TestCase):
    def test_effective_plan_refusal_guides_only_the_user_to_a_fresh_whitelist_plan(self):
        cfg = dict(wb_config.DEFAULTS, mode="plan")
        with self.assertRaises(writeback._Fail) as refusal:
            writeback._precheck_write_mode(cfg, "apply")
        self.assertEqual(refusal.exception.code, 2)
        msg = refusal.exception.msg
        for fragment in ('用户本人', 'config/writeback.json', 'whitelist_page_ids', 'whitelist',
                         'whitelist-check', "plan --scope '白名单'", '旧计划'):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, msg)
        self.assertIn(f"cd {shlex.quote(str(TOOLS.parent))} && python3 -I tools/writeback.py", msg)


class TestUserJourney12(unittest.TestCase):
    def test_plan_and_interrupted_plan_provide_commands_with_required_ids(self):
        pid = 'wb-20261121T0600-abcd'
        cmd = f"cd {shlex.quote(str(TOOLS.parent))} && python3 -I tools/writeback.py"
        plan = {'mode': 'whitelist', 'plan_id': pid, 'items': []}
        self.assertIn(f"{cmd} show --plan {pid}", '\n'.join(writeback.plan_next_steps(plan, pid, 'abcdef123456')))
        self.assertIn(f"{cmd} apply --resume {pid}", writeback.resume_mode_mismatch(plan, 'full'))

    def test_recovery_summary_has_a_work_directory_without_changing_user_content(self):
        ctx = SimpleNamespace(items={})
        text = wb_apply._next_step(ctx, 'M-001', {'status': 'failed'})
        self.assertIn(f"cd {shlex.quote(str(TOOLS.parent))} && python3 -I tools/writeback.py plan", text)
        literal = '我的标题含 writeback.py plan，请原样保留'
        result = '\n'.join(writeback.paused_round_lines([dict(run_id='R9', paused_at='today', reasons=[literal])]))
        self.assertIn(literal, result)

    def test_pause_note_preserves_work_and_nonproduction_vault(self):
        with tempfile.TemporaryDirectory(prefix="cli's isolated work ") as tmp:
            work, vault = Path(tmp), Path(tmp)/'another vault'
            vault.mkdir()
            (work / "tools").mkdir()
            (work / "tools/refresh.py").touch()
            p = refresh.Paths(str(work), str(vault))
            message = '自定义文件名 writeback.py plan'
            out = refresh.write_paused_note(p, 'R9', {'pause_reasons': [message]})
            text = Path(out).read_text()
            self.assertIn(message, text)
            commands = re.findall(r'`(cd [^\n`]+)`', text)
            parsed = []
            for command in commands:
                fields = shlex.split(command)
                self.assertEqual(fields[:3], ['cd', str(work), '&&'])
                self.assertEqual(fields[3:6], ['python3', '-I', 'tools/refresh.py'])
                args = refresh.build_parser().parse_args(fields[6:])
                self.assertEqual(args.vault, str(vault))
                self.assertTrue(args.allow_other_vault)
                parsed.append((args.approve_run, args.discard_run))
            self.assertIn(('R9', None), parsed)
            self.assertIn((None, 'R9'), parsed)

    def test_top_level_help_gives_a_full_command_for_each_tool(self):
        text = writeback.build_parser().format_help()
        for tool in ('writeback', 'refresh'):
            self.assertIn(f"cd {shlex.quote(str(TOOLS.parent))} && python3 -I tools/{tool}.py", text)


class TestCommandIsolation(unittest.TestCase):
    def test_override_values_remain_private_and_original_targets_are_explicit(self):
        with tempfile.TemporaryDirectory(prefix="work's path ") as directory:
            vault = str(Path(directory) / "vault $HOME; archive")
            args = SimpleNamespace(work=directory, vault=vault,
                                   base_url="http://example.invalid/user?fake-secret=V11PRIVATE",
                                   token_file="/private/V11PRIVATE", read_token_file="/private/read-V11PRIVATE")
            item_path = "a; $(touch X) ' and spaces.md"
            with wb_common.command_context(args):
                command = wb_common.cli_command("writeback", "ack-backlog", "--path", item_path)
                self.assertNotIn("V11PRIVATE", command)
                fields = shlex.split(command)
                self.assertEqual(fields[:3], ["cd", directory, "&&"])
                parsed = writeback.build_parser().parse_args(fields[6:])
                self.assertEqual(parsed.work, directory)
                self.assertEqual(parsed.vault, vault)
                self.assertEqual(parsed.path, item_path)
                self.assertIn("WB_BASE_URL:?", parsed.base_url)
                self.assertIn("WB_READ_TOKEN_FILE:?", parsed.read_token_file)
                self.assertIn("WB_TOKEN_FILE:?", parsed.token_file)
                offline = wb_common.cli_command("refresh", "--mode", "incremental")
                self.assertIn("WB_REFRESH_SNAPSHOT:?", offline)
                self.assertNotIn("V11PRIVATE", offline)
            self.assertNotIn(directory, wb_common.cli_command("writeback", "plan"))

    def test_data_only_work_checks_its_own_tools_and_exits_without_launching_refresh(self):
        with tempfile.TemporaryDirectory(prefix="data-only ' work ") as directory:
            root = Path(directory)
            vault = root / "private vault"
            vault.mkdir()
            note = refresh.write_paused_note(refresh.Paths(root, vault), "R9", {"pause_reasons": ["manual"]})
            text = Path(note).read_text()
            controls = re.findall(r"`(cd [^\n`]+)`", text)
            self.assertEqual(len(controls), 2)
            # A sentinel python3 detects an accidental launch from the incomplete work directory.
            fakebin = root / "bin"
            fakebin.mkdir()
            sentinel = fakebin / "python3"
            sentinel.write_text("#!/bin/sh\n: > unexpected-python\n")
            sentinel.chmod(0o700)
            for command in controls:
                with self.subTest(command=command):
                    out = subprocess.run(["/bin/sh", "-c", command], cwd=root, text=True,
                                         capture_output=True, env={"PATH": str(fakebin)}, check=False)
                    self.assertNotEqual(out.returncode, 0)
                    self.assertIn("此工作目录缺少 tools/refresh.py", out.stderr)
                    self.assertIn("完整 tools/", out.stderr)
                    self.assertFalse((root / "unexpected-python").exists())
                    self.assertFalse((root / "state").exists())
