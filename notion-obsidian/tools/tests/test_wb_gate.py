#!/usr/bin/env python3
"""Tests for wb_gate (IMPL_CONTRACT §2 D-05, §5.4).

Refusal: stdin/stdout not TTYs, no controlling terminal, and the agent/CI environment markers
(CLAUDECODE, AI_AGENT, CI, GITHUB_ACTIONS, CLAUDE_CODE_*) even inside a real pty (pexpect).
Mechanics: confirm_phrase / confirm_yes_no on the BARE wb_gate functions with a dummy phrase in a
pty with a cleaned environment. No write path, plan, token or Notion call is involved anywhere.

Run from the task root:
  python3 -I -B -m unittest discover -s tools/tests -p 'test_wb_gate.py' -v
"""
import ast
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import wb_gate  # noqa: E402

try:
    import pexpect
except ImportError:  # pragma: no cover
    pexpect = None

MARKERS = ["CLAUDECODE", "AI_AGENT", "CI", "GITHUB_ACTIONS", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID"]
DUMMY_PHRASE = "确认 TEST 修改0 新增0 ABC123"

# Child program: calls ONE bare wb_gate function and writes the outcome as JSON to argv[3].
CHILD = r'''
import json, sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import wb_gate
mode, out = sys.argv[2], sys.argv[3]
res = {}
try:
    if mode == "require":
        res["tty"] = wb_gate.require_human("测试")
    elif mode == "phrase":
        lines = json.loads(sys.argv[5]) if len(sys.argv) > 5 else ["dummy line 1", "dummy line 2"]
        res["ok"] = wb_gate.confirm_phrase(sys.argv[4], lines)
    elif mode == "yesno":
        res["ok"] = wb_gate.confirm_yes_no("dummy question?")
    code = 0
except wb_gate.GateRefused as e:
    res["refused"] = e.reason
    code = 3
with open(out, "w", encoding="utf-8") as fh:
    json.dump(res, fh, ensure_ascii=False)
print("CHILD-DONE", flush=True)
sys.exit(code)
'''


def clean_env(**extra):
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb"}
    env.update(extra)
    return env


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_wb_gate_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.script = os.path.join(self.tmp, "gate_child.py")
        with open(self.script, "w", encoding="utf-8") as fh:
            fh.write(CHILD)
        self.n = 0

    def out_path(self):
        self.n += 1
        return os.path.join(self.tmp, f"out{self.n}.json")

    def argv(self, mode, out, *extra):
        return ["-I", "-B", self.script, TOOLS, mode, out, *extra]

    @staticmethod
    def result(out):
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)


# ---------------------------------------------------------------- in-process unit tests
class UnitTests(unittest.TestCase):
    def test_agent_env_markers(self):
        self.assertEqual(wb_gate.agent_env_markers({}), [])
        for m in MARKERS + ["CLAUDE_CODE_ANYTHING_NEW"]:
            self.assertEqual(wb_gate.agent_env_markers({m: "1", "HOME": "/x"}), [m], m)
        self.assertEqual(wb_gate.agent_env_markers({"CI": ""}), ["CI"])  # presence, not value
        # D-05 clarification: these must NOT cause a refusal
        self.assertEqual(wb_gate.agent_env_markers({"INVOCATION_ID": "x", "JOURNAL_STREAM": "8:1"}), [])
        self.assertEqual(wb_gate.agent_env_markers({"CLAUDE": "1", "XCI": "1", "MY_CLAUDE_CODE_X": "1"}), [])

    def test_env_marker_refuses_before_any_tty_access(self):
        for m in MARKERS:
            with mock.patch.dict(os.environ, {m: "1"}, clear=True), \
                    mock.patch("os.open", side_effect=AssertionError("tty touched")), \
                    mock.patch("builtins.open", side_effect=AssertionError("tty touched")):
                with self.assertRaises(wb_gate.GateRefused) as cm:
                    wb_gate.require_human("apply")
                self.assertIn(m, cm.exception.reason)
                self.assertIn("apply", cm.exception.reason)
                with self.assertRaises(wb_gate.GateRefused):
                    wb_gate.confirm_phrase(DUMMY_PHRASE, ["x"])
                with self.assertRaises(wb_gate.GateRefused):
                    wb_gate.confirm_yes_no("q?")

    def test_non_tty_streams_refused(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(sys, "stdin", io.StringIO("y\n")), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            with self.assertRaises(wb_gate.GateRefused) as cm:
                wb_gate.require_human("rollback")
            self.assertIn("交互终端", cm.exception.reason)
            with self.assertRaises(wb_gate.GateRefused):
                wb_gate.confirm_yes_no("q?")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(sys, "stdin", None):
            with self.assertRaises(wb_gate.GateRefused):
                wb_gate.require_human("apply")

    def test_challenge_code(self):
        codes = {wb_gate.challenge_code() for _ in range(200)}
        for c in codes:
            self.assertRegex(c, r"^[0-9A-F]{6}$")
        self.assertGreater(len(codes), 150)

    def test_phrase_normalisation(self):
        n = wb_gate._norm
        self.assertEqual(n("  确认　ab12  修改3 ABC123 \n"), "确认 ab12 修改3 ABC123")
        self.assertEqual(n("ＡＢＣ１２３"), "ABC123")  # full-width IME input
        self.assertNotEqual(n("abc123"), n("ABC123"))  # case still matters

    def test_display_safe(self):
        ds = wb_gate.display_safe
        self.assertEqual(ds("M-001 General/计划 A.md\t🙂"), "M-001 General/计划 A.md\t🙂")  # unchanged
        evil = "M-002 General/\x1b[1A\x1b[2KSecret.md\x1b[2K\rM-002 General/Harmless.md"
        out = ds(evil)
        self.assertEqual(out, "M-002 General/\\x1b[1A\\x1b[2KSecret.md\\x1b[2K\\x0dM-002 General/Harmless.md")
        for ch in ("\x00", "\x07", "\x08", "\n", "\r", "\x1b", "\x7f", "\x85", "\x9b", "\u2028", "\u2029",
                   "\u202e", "\u2066", "\u200f"):
            got = ds(f"a{ch}b")
            self.assertNotIn(ch, got, repr(ch))
            self.assertTrue(got.startswith("a\\") and got.endswith("b"), got)
        self.assertEqual(ds(5), "5")

    def test_unsafe_phrase_refused_before_tty(self):
        # an expected phrase that could not be typed back (or that moves the cursor) is a caller bug
        with mock.patch.object(wb_gate, "require_human", return_value="/dev/pts/0"), \
                mock.patch("builtins.open", side_effect=AssertionError("tty touched")):
            for bad in ("确认 X\x1b[2K ABC123", "确认\rX", "确认\u202eX"):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    wb_gate.confirm_phrase(bad, ["x"])

    def test_no_bypass_surface(self):
        self.assertEqual(list(inspect.signature(wb_gate.require_human).parameters), ["action"])
        self.assertEqual(list(inspect.signature(wb_gate.confirm_phrase).parameters), ["expected_phrase", "lines_to_show"])
        self.assertEqual(list(inspect.signature(wb_gate.confirm_yes_no).parameters), ["question"])
        src = inspect.getsource(wb_gate)
        tree = ast.parse(src)
        env_users = set()
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(fn):
                if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv", "environb"):
                    env_users.add(fn.name)
        self.assertEqual(env_users, {"agent_env_markers"})  # the environment is only read for refusal markers
        self.assertNotIn("getenv", src)
        self.assertIsNone(re.search(r"(?i)bypass\s*=|skip_gate|--yes|force\s*=", src))


# ---------------------------------------------------------------- subprocess (no pty / partial tty)
class SubprocessRefusalTests(Base):
    def test_pipes_refused(self):
        out = self.out_path()
        r = subprocess.run([sys.executable] + self.argv("require", out), env=clean_env(), input=b"",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, start_new_session=True)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("标准输入和标准输出必须都是交互终端", self.result(out)["refused"])

    def test_phrase_and_yesno_refused_over_pipes(self):
        for mode in ("phrase", "yesno"):
            out = self.out_path()
            r = subprocess.run([sys.executable] + self.argv(mode, out, DUMMY_PHRASE), env=clean_env(),
                               input=(DUMMY_PHRASE + "\n").encode(), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=30, start_new_session=True)
            self.assertEqual(r.returncode, 3, r.stderr)
            self.assertIn("refused", self.result(out))

    def test_stdin_tty_stdout_pipe_refused(self):
        master, slave = os.openpty()
        try:
            out = self.out_path()
            p = subprocess.Popen([sys.executable] + self.argv("require", out), env=clean_env(), stdin=slave,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            os.close(slave)
            slave = None
            _, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 3, err)
            self.assertIn("交互终端", self.result(out)["refused"])
        finally:
            if slave is not None:
                os.close(slave)
            os.close(master)

    def test_tty_streams_without_controlling_terminal_refused(self):
        master, slave = os.openpty()
        try:
            out = self.out_path()
            p = subprocess.Popen([sys.executable] + self.argv("require", out), env=clean_env(), stdin=slave,
                                 stdout=slave, stderr=subprocess.PIPE, start_new_session=True)
            os.close(slave)
            slave = None
            _, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 3, err)
            self.assertIn("/dev/tty", self.result(out)["refused"])
        finally:
            if slave is not None:
                os.close(slave)
            os.close(master)


# ---------------------------------------------------------------- pexpect (real pty + controlling tty)
@unittest.skipIf(pexpect is None, "pexpect not installed")
class PtyTests(Base):
    def spawn(self, mode, out, env, *extra):
        child = pexpect.spawn(sys.executable, self.argv(mode, out, *extra), env=env, encoding="utf-8",
                              timeout=30, dimensions=(40, 200))
        child.logfile_read = io.StringIO()  # full transcript of what the child printed on the pty
        self.addCleanup(lambda: child.close(force=True) if child.isalive() else None)
        return child

    def finish(self, child):
        child.expect("CHILD-DONE")
        child.expect(pexpect.EOF)
        child.close()
        return child.exitstatus

    def test_markers_refused_inside_pty(self):
        for m in MARKERS:
            for mode in ("require", "phrase", "yesno"):
                with self.subTest(var=m, mode=mode):
                    out = self.out_path()
                    child = self.spawn(mode, out, clean_env(**{m: "1"}), DUMMY_PHRASE)
                    status = self.finish(child)
                    self.assertEqual(status, 3)
                    res = self.result(out)
                    self.assertIn(m, res["refused"])
                    transcript = child.logfile_read.getvalue()
                    self.assertNotIn("请原样输入", transcript)
                    self.assertNotIn("[y/N]", transcript)

    def test_empty_marker_value_refused_inside_pty(self):
        out = self.out_path()
        child = self.spawn("require", out, clean_env(CI=""))
        self.assertEqual(self.finish(child), 3)
        self.assertIn("CI", self.result(out)["refused"])

    def test_clean_env_pty_passes_require_human(self):
        # INVOCATION_ID / JOURNAL_STREAM must not cause a refusal (D-05 clarification)
        out = self.out_path()
        child = self.spawn("require", out, clean_env(INVOCATION_ID="abc", JOURNAL_STREAM="8:123"))
        self.assertEqual(self.finish(child), 0)
        self.assertRegex(self.result(out)["tty"], r"^/dev/(pts/\d+|tty\w*)$")

    def phrase_round(self, answer):
        out = self.out_path()
        child = self.spawn("phrase", out, clean_env(), DUMMY_PHRASE)
        child.expect("dummy line 2")
        child.expect("请原样输入")
        child.expect("> ")
        if answer is None:
            child.sendeof()
        else:
            child.sendline(answer)
        self.assertEqual(self.finish(child), 0)
        return self.result(out)["ok"]

    def test_confirm_phrase_mechanics(self):
        self.assertTrue(self.phrase_round(DUMMY_PHRASE))
        self.assertTrue(self.phrase_round("  确认　TEST 修改0  新增0 ABC123  "))  # IME spacing
        self.assertFalse(self.phrase_round("确认 TEST 修改0 新增0 ABC124"))
        self.assertFalse(self.phrase_round("确认 TEST 修改0 新增0 abc123"))
        self.assertFalse(self.phrase_round("y"))
        self.assertFalse(self.phrase_round(""))
        self.assertFalse(self.phrase_round(None))  # EOF

    def test_confirm_phrase_escapes_control_sequences(self):
        # review finding: ESC/CSI and a bare CR in an item line could erase or overwrite approval lines
        evil = ["M-001 General/A.md", "M-002 General/\x1b[1A\x1b[2KSecret.md\x1b[2K\rM-002 General/Harmless.md",
                "M-003 a\nM-004 injected line", "M-005 \u202eevil\u202c.md"]
        out = self.out_path()
        child = self.spawn("phrase", out, clean_env(), DUMMY_PHRASE, json.dumps(evil))
        child.expect("请原样输入")
        child.expect("> ")
        child.sendline(DUMMY_PHRASE)
        self.assertEqual(self.finish(child), 0)
        self.assertTrue(self.result(out)["ok"])
        transcript = child.logfile_read.getvalue()
        self.assertNotIn("\x1b", transcript)
        self.assertIsNone(re.search(r"\r(?!\n)", transcript))  # every CR is the pty's CRLF
        self.assertNotIn("\u202e", transcript)
        self.assertIn("M-002 General/\\x1b[1A\\x1b[2KSecret.md\\x1b[2K\\x0dM-002 General/Harmless.md", transcript)
        self.assertIn("M-003 a\\x0aM-004 injected line", transcript)  # one item = one displayed line
        self.assertNotIn("\nM-004", transcript.replace("\r\n", "\n"))

    def yes_no_round(self, answer):
        out = self.out_path()
        child = self.spawn("yesno", out, clean_env())
        child.expect(r"dummy question\? \[y/N\] ")
        if answer is None:
            child.sendeof()
        else:
            child.sendline(answer)
        self.assertEqual(self.finish(child), 0)
        return self.result(out)["ok"]

    def test_confirm_yes_no_mechanics(self):
        for a in ("y", "Y", "yes", "YES", " 是 "):
            self.assertTrue(self.yes_no_round(a), a)
        for a in ("", "n", "no", "否", "yy", "确认", None):
            self.assertFalse(self.yes_no_round(a), a)


if __name__ == "__main__":
    unittest.main()
