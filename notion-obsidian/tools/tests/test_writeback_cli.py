#!/usr/bin/env python3
"""Tests for tools/writeback.py (I1 CLI: approval UX of speccheck §4, R-140..R-153, R-160..R-163, R-192/R-193,
R-220..R-222).

* Read-only commands run in-process (main([...])) against the fake Notion server of tools/tests/fake_notion.py with
  fake tokens; notion_tokens.load_writeback_token is patched to raise, proving they never load the write token.
* The human gate is exercised for REFUSAL (agent env markers, no TTY, piped stdin) with subprocesses, and the
  decision commands (reject / unreject / discard / restore-deleted / unblock / ack-* / resolve --take notion /
  hold / rebase) are driven through a pty with pexpect in a child environment scrubbed of CLAUDECODE / AI_AGENT /
  CI / GITHUB_ACTIONS / CLAUDE_CODE_* — only here, only against temp work dirs and the fake server. apply is
  driven through the pty up to the challenge phrase and must refuse a wrong phrase with 0 writes; the confirmed
  path is tested through the internal confirm_and_execute() with an injected executor (contract §1.3).

Run (from the sandbox root):
  python3 -I -B -m unittest discover -s tools/tests -p 'test_writeback_cli.py' -v
"""
import contextlib
import functools
import io
import json
import os
import re
import subprocess
import tempfile
import sys
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
for _p in (HERE, TOOLS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_wb_plan as TP  # noqa: E402  (fixture helpers only; its TestCases stay in their own module)
import wb_common  # noqa: E402
import wb_locks  # noqa: E402
import wb_plan  # noqa: E402
import wb_state  # noqa: E402
import writeback  # noqa: E402

try:
    import pexpect  # noqa: E402
except ImportError:  # pragma: no cover
    pexpect = None

WB = os.path.join(TOOLS, "writeback.py")
LEAF, TASK, KID = TP.LEAF, TP.TASK, TP.KID
AGENT_PREFIX = "CLAUDE_CODE_"
AGENT_VARS = ("CLAUDECODE", "AI_AGENT", "CI", "GITHUB_ACTIONS")


def setUpModule():
    TP.setUpModule()
    # the fake server's threads live in this process; pexpect's forkpty child execs Python immediately
    import warnings
    warnings.filterwarnings("ignore", r"This process .* is multi-threaded, use of forkpty", DeprecationWarning)


def tearDownModule():
    TP.tearDownModule()


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in AGENT_VARS and not k.startswith(AGENT_PREFIX)}
    env.update(extra)
    return env


def state_digest(e):
    """{relative path: sha256} of every file under the work dir's state/ (byte-identity checks)."""
    out = {}
    root = os.path.join(e.work, "state")
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = wb_common.sha256_bytes(fh.read())
    return out


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = writeback.main(argv)
    return rc, out.getvalue(), err.getvalue()


class _Base(unittest.TestCase):
    def setUp(self):
        # Exercise the default credential path and its real permission checks, including in pty children.
        home = tempfile.TemporaryDirectory(prefix="wb-cli-home-")
        self.addCleanup(home.cleanup)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": home.name}))
        token_dir = os.path.join(home.name, ".config", "notion-obsidian")
        os.makedirs(token_dir, mode=0o700)
        os.close(os.open(os.path.join(token_dir, "writeback.env"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))

    def env(self, **kw):
        e = TP.Env(self, **kw)
        e.argv = ["--work", e.work, "--vault", e.vault, "--base-url", e.base, "--read-token-file", e.read_token_file]
        return e

    def main(self, e, *args):
        with mock.patch.object(wb_plan, "obsidian_status", return_value=TP.OBSIDIAN_RUNNING):
            return run_main(e.argv + list(args))

    def make_plan(self, e, *args):
        rc, out, err = self.main(e, "plan", *args)
        self.assertEqual(rc, 0, out + err)
        pid = re.search(r"plan_id：(\S+)", out).group(1)
        sha12 = re.search(r"前 12 位）：([0-9a-f]{12})", out).group(1)
        return pid, sha12, out

    def spawn(self, e, *args, env=None):
        child = pexpect.spawn(sys.executable, ["-I", "-B", WB] + e.argv + list(args), env=env or clean_env(),
                              encoding="utf-8", timeout=60, dimensions=(50, 250))
        self.addCleanup(lambda: child.terminate(force=True) if child.isalive() else None)
        return child

    def answer(self, child, ans="y"):
        child.expect(r"\[y/N\]")
        child.sendline(ans)
        child.expect(pexpect.EOF)
        child.close()
        return child.exitstatus, child.before


# ============================================================================================ help / modes
class TestHelpAndModes(_Base):
    def test_help_chinese_english_no_yes_flag(self):
        out = subprocess.run([sys.executable, "-I", "-B", WB, "--help"], capture_output=True, text=True, env=clean_env())
        self.assertEqual(out.returncode, 0)
        for s in ("查看 VIEW", "确认 APPROVE", "拒绝 REJECT", "再次执行 RE-RUN", "status", "plan", "show",
                  "whitelist-check", "apply", "reject", "unreject", "discard", "restore-deleted", "resolve",
                  "reconcile", "unblock", "rollback", "ack-anomaly", "ack-backlog", "hold", "rebase", "audit-scope",
                  "Manual, approval-gated write-back"):
            self.assertIn(s, out.stdout)
        ap = writeback.build_parser()
        flags = set()
        for action in ap._actions:
            flags.update(action.option_strings)
            if hasattr(action, "choices") and isinstance(action.choices, dict):
                for sp in action.choices.values():
                    for a in sp._actions:
                        flags.update(a.option_strings)
        self.assertFalse({f for f in flags if re.search(r"yes|force|confirm|no-?gate|skip", f)}, flags)
        a = subprocess.run([sys.executable, "-I", "-B", WB, "apply", "--help"], capture_output=True, text=True,
                           env=clean_env())
        for f in ("--items", "--all-safe", "--delete", "--delete-all-in-plan", "--delete-blocks", "--rename", "--move",
                  "--resume", "--sha"):
            self.assertIn(f, a.stdout)

    def test_mode_off_plan_explains_and_creates_nothing_apply_refused(self):
        e = self.env(cfg={"mode": "off"})
        e.edit(LEAF, "second para", "second para off")
        rc, out, err = self.main(e, "plan")
        self.assertEqual(rc, 0, err)
        self.assertIn("off", out)
        self.assertFalse(os.path.exists(os.path.join(e.work, "reports", "writeback")))
        rc, out, err = self.main(e, "apply", "--plan", "wb-20261118T000000-abcd", "--sha", "0" * 12, "--all-safe")
        self.assertEqual(rc, writeback.EXIT_REFUSED)
        self.assertIn("off", err)
        rc, out, err = self.main(e, "status")
        self.assertEqual(rc, 0, err)
        self.assertIn("模式 mode：off", out)

    def test_mode_plan_allows_plan_show_refuses_apply(self):
        e = self.env(cfg={"mode": "plan"})
        e.edit(LEAF, "second para", "second para P")
        pid, sha12, _ = self.make_plan(e)
        rc, out, err = self.main(e, "show", "--plan", pid)
        self.assertEqual(rc, 0, err)
        rc, out, err = self.main(e, "apply", "--plan", pid, "--sha", sha12, "--all-safe")
        self.assertEqual(rc, writeback.EXIT_REFUSED)
        self.assertIn("mode=plan 只允许生成预览", err)

    def test_effective_mode_without_readonly_confirmation_is_plan(self):
        e = self.env()
        c = dict(e.cfg_dict)
        c.pop("read_integration_readonly_confirmed_at")
        e.write_cfg(c)
        rc, out, err = self.main(e, "apply", "--plan", "wb-20261118T000000-abcd", "--sha", "0" * 12, "--all-safe")
        self.assertEqual(rc, writeback.EXIT_REFUSED)
        self.assertIn("有效模式为 plan", err)

    def test_plan_takes_refresh_lock_non_blocking(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para L")
        with wb_locks.lock("refresh", state_dir=os.path.join(e.work, "state")):
            rc, out, err = self.main(e, "plan")
        self.assertEqual(rc, writeback.EXIT_REFUSED)
        self.assertIn("refresh 正在运行", err)
        self.assertFalse(os.path.exists(os.path.join(e.work, "reports", "writeback")))

    def test_requires_explicit_vault_outside_production(self):
        e = self.env()
        rc, out, err = run_main(["--work", e.work, "--base-url", e.base, "--read-token-file", e.read_token_file, "plan"])
        self.assertEqual(rc, writeback.EXIT_REFUSED)
        self.assertIn("--vault", err)


# ============================================================================================ VIEW
class TestViewCommands(_Base):
    def test_read_only_commands_never_load_write_token_and_send_no_write(self):
        import notion_tokens
        e = self.env()
        e.edit(LEAF, "second para", "second para RO")
        e.fake.clear_requests()
        with mock.patch.object(notion_tokens, "load_writeback_token", side_effect=AssertionError("write token")):
            pid, _sha12, _ = self.make_plan(e)
            for argv in (["status"], ["status", "--json"], ["show", "--plan", pid], ["show", "--plan", pid, "--item", "M-001"],
                         ["whitelist-check"], ["whitelist-check", "--json"]):
                rc, out, err = self.main(e, *argv)
                self.assertEqual(rc, 0, (argv, err))
        self.assertEqual(e.writes(), [])
        self.assertTrue(e.fake.requests and all(r["token"] == "ro" for r in e.fake.requests))

    def test_r152_status_all_sections(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para S")
        pid, _sha12, _ = self.make_plan(e)
        os.unlink(e.path("General/Home/Doomed.md"))
        e.hold_delete("del", "General/Home/Doomed.md")
        with wb_state.transaction(e.db):
            e.db.execute("INSERT INTO name_overrides(notion_id, old_rel, local_rel, since, status) VALUES(?,?,?,?, 'open')",
                         (e.ids["ren"], "General/Home/Rename me.md", "General/Home/Renamed.md", wb_common.now_iso()))
            e.db.execute("INSERT INTO page_state(notion_id, state, reason, since) VALUES(?,?,?,?)",
                         (e.ids["kid"], "BLOCKED", "readback mismatch", wb_common.now_iso()))
            e.db.execute("INSERT INTO rejections(notion_id, rel_path, l_sha, item_category, reason, created_at, plan_id, "
                         "active) VALUES(?,?,?,?,?,?,?,1)", (e.ids["leaf"], LEAF, "x", "M", "not now",
                                                             wb_common.now_iso(), pid))
            e.db.execute("INSERT INTO creation_state(ts, members, plan, state, source) VALUES(?,?,?,?,?)",
                         (wb_common.now_iso(), 1, "free", "unlimited", "refresh"))
        rc, out, err = self.main(e, "status")
        self.assertEqual(rc, 0, err)
        for s in ("模式 mode：whitelist", "成员数 members：1", "creation_state：unlimited", "-- 计划 plans --", pid,
                  "待确认的本地改动（按类）", "待写回的本地修改 local_modified：1", LEAF, "本地删除 missing：1",
                  "删除 hold：General/Home/Doomed.md", "命名差异", "General/Home/Renamed.md", "BLOCKED", "unblock --page",
                  "已拒绝 rejected", "not now", "上次计划"):
            self.assertIn(s, out)
        rc, out, err = self.main(e, "status", "--json")
        st = json.loads(out)
        for k in ("mode", "members", "plans", "pending_local_changes", "holds", "name_overrides", "blocked_pages",
                  "rejected", "initial_backlog", "last_plan"):
            self.assertIn(k, st)

    def test_r153_show_item_unified_diff_and_ops(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para SHOW")
        pid, sha12, _ = self.make_plan(e)
        rc, out, err = self.main(e, "show", "--plan", pid, "--item", "M-001")
        self.assertEqual(rc, 0, err)
        self.assertIn("@@ ", out)
        self.assertIn("-second para", out)
        self.assertIn("+second para SHOW", out)
        self.assertIn("操作列表", out)
        self.assertIn('"op": "UPDATE"', out)
        rc, out, err = self.main(e, "show", "--plan", pid)
        self.assertIn("## 修改内容（M）", out)
        self.assertIn(sha12, out)
        e.edit(LEAF, "second para SHOW", "second para AGAIN")
        rc, out, err = self.main(e, "show", "--plan", pid, "--item", "M-001")
        self.assertIn("库内文件在计划后又被修改", out)

    def test_r140_cli_plan_outputs(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para OUT")
        pid, sha12, out = self.make_plan(e)
        d = os.path.join(e.work, "reports", "writeback", pid)
        self.assertTrue(os.path.isfile(os.path.join(d, "plan.json")))
        self.assertTrue(os.path.isfile(os.path.join(d, "preview.md")))
        self.assertTrue(os.path.isfile(e.path(f"Notion_Obsidian/写回预览/{pid}.md")))
        with open(os.path.join(d, "plan.json"), "rb") as fh:
            self.assertTrue(wb_common.sha256_bytes(fh.read()).startswith(sha12))
        self.assertIn("库内只读副本（不能作为确认依据）", out)

    def test_r151_whitelist_check(self):
        e = self.env()
        rc, out, err = self.main(e, "whitelist-check", "--json")
        self.assertEqual(rc, 0, err)
        rows = {r["id"]: r for r in json.loads(out)}
        self.assertEqual(rows[e.ids["folder"]]["descendant_pages"], 1)
        self.assertFalse(rows[e.ids["folder"]]["leaf"])
        self.assertTrue(rows[e.ids["leaf"]]["leaf"])
        self.assertEqual(rows[e.ids["db"]]["rows"], 1)
        self.assertEqual(rows[e.ids["leaf"]]["live"], "是")


# ============================================================================================ gate refusals
@unittest.skipIf(pexpect is None, "pexpect not installed")
class TestGateRefusals(_Base):
    def _apply_argv(self, pid, sha12):
        return ["apply", "--plan", pid, "--sha", sha12, "--items", "M-001"]

    def test_r160_refused_with_agent_markers_no_tty_and_piped_stdin(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para G")
        e.edit(KID, "kid body", "kid body LOCAL")
        e.remote_text("kid", 0, "kid body REMOTE")          # a real conflict item for resolve --take obsidian
        pid, sha12, _ = self.make_plan(e)
        p, _ = wb_plan.load_plan(pid, work=e.paths, db=e.db)
        cid = TP.items_of(p, "C")[0]["item_id"]
        mid = TP.items_of(p, "M")[0]["item_id"]
        e.close_db()
        e.fake.clear_requests()
        cmds = [["apply", "--plan", pid, "--sha", sha12, "--items", mid], ["rollback", "--plan", pid],
                ["resolve", "--plan", pid, "--item", cid, "--take", "obsidian"], ["apply", "--resume", pid]]
        before = state_digest(e)
        # 1. agent / CI markers in a real pty: refused before any prompt, before any state file is opened
        for var in ("CLAUDECODE", "AI_AGENT", "CI", "GITHUB_ACTIONS", "CLAUDE_CODE_ENTRYPOINT"):
            for argv in cmds:
                with self.subTest(var=var, cmd=argv[0] + (" " + argv[1] if argv[0] == "apply" else "")):
                    child = self.spawn(e, *argv, env=clean_env(**{var: "1"}))
                    child.expect(pexpect.EOF)
                    child.close()
                    self.assertEqual(child.exitstatus, writeback.EXIT_GATE, child.before)
                    self.assertIn(var, child.before)
                    self.assertNotIn("请原样输入", child.before)
        # 2. no TTY at all (pipes)
        for argv in cmds:
            with self.subTest(no_tty=argv[0]):
                r = subprocess.run([sys.executable, "-I", "-B", WB] + e.argv + argv, capture_output=True, text=True,
                                   env=clean_env(), stdin=subprocess.DEVNULL)
                self.assertEqual(r.returncode, writeback.EXIT_GATE, r.stderr)
        # 3. piped stdin inside a terminal: echo … | writeback.py apply|resolve …
        for argv in (cmds[0], cmds[2]):
            shell_cmd = "echo 确认 | " + " ".join([sys.executable, "-I", "-B", WB] + e.argv + argv)
            child = pexpect.spawn("/bin/sh", ["-c", shell_cmd], env=clean_env(), encoding="utf-8", timeout=60)
            child.expect(pexpect.EOF)
            child.close()
            self.assertEqual(child.exitstatus, writeback.EXIT_GATE, child.before)
            self.assertIn("管道", child.before)
        self.assertEqual(state_digest(e), before, "a refused command leaves state/ byte-identical")
        self.assertEqual(e.writes(), [])
        self.assertEqual(e.db.execute("SELECT count(*) FROM confirmations").fetchone()[0], 0)
        self.assertEqual(wb_plan.plan_row(e.db, pid)["status"], "draft")
        self.assertEqual(e.db.execute("SELECT count(*) FROM plans").fetchone()[0], 1, "resolve created no plan")

    def test_refused_gate_commands_never_create_or_touch_state(self):
        """I2 request: apply / resume / rollback / resolve --take obsidian run the human gate before any state file
        is opened: without a TTY nothing under state/ appears, changes or is created (no writeback.db)."""
        e = self.env()
        e.close_db()
        for s in ("", "-wal", "-shm"):
            p = os.path.join(e.work, "state", "writeback.db" + s)
            if os.path.exists(p):
                os.unlink(p)
        before = state_digest(e)
        for argv in (["apply", "--plan", "wb-20261118T000000-abcd", "--sha", "0" * 12, "--items", "M-001"],
                     ["apply", "--resume", "wb-20261118T000000-abcd"], ["rollback", "--plan", "wb-20261118T000000-abcd"],
                     ["resolve", "--plan", "wb-20261118T000000-abcd", "--item", "C-001", "--take", "obsidian"],
                     ["reject", "--plan", "wb-20261118T000000-abcd", "--items", "M-001"]):
            with self.subTest(cmd=argv[0]):
                r = subprocess.run([sys.executable, "-I", "-B", WB] + e.argv + argv, capture_output=True, text=True,
                                   env=clean_env(), stdin=subprocess.DEVNULL)
                self.assertEqual(r.returncode, writeback.EXIT_GATE, r.stderr)
                self.assertEqual(state_digest(e), before, argv)
        self.assertFalse(os.path.exists(os.path.join(e.work, "state", "writeback.db")))

    def test_r161_wrong_phrase_exits_with_zero_writes_and_code_not_in_files(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para W")
        pid, sha12, _ = self.make_plan(e)
        e.fake.clear_requests()
        child = self.spawn(e, *self._apply_argv(pid, sha12))
        child.expect("请原样输入下面这一行以确认")
        shown = child.before
        child.expect(r"> ")
        shown += child.before
        m = re.search(r"(确认 \S{4} 修改1 新增0 删除0 改名0 ([0-9A-F]{6}))", shown)
        self.assertIsNotNone(m, shown)
        phrase, code = m.group(1), m.group(2)
        self.assertIn("M-001", shown)
        self.assertIn(wb_plan.RESIDUAL_RISK, shown)
        child.sendline(phrase.replace("修改1", "修改2"))
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, writeback.EXIT_FAIL, child.before)
        self.assertIn("短语不符", child.before)
        self.assertEqual(e.writes(), [])
        self.assertEqual(e.fake.requests, [], "nothing at all is sent before the confirmation")
        self.assertEqual(e.db.execute("SELECT count(*) FROM confirmations").fetchone()[0], 0)
        self.assertEqual(wb_plan.plan_row(e.db, pid)["status"], "draft")
        self.assertFalse(os.path.exists(os.path.join(e.work, "approvals", "writeback")))
        d = os.path.join(e.work, "reports", "writeback", pid)
        for f in os.listdir(d):
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                self.assertNotIn(code, fh.read())

    def test_r162_wrong_sha_or_non_draft_refused_before_challenge(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para H")
        pid, sha12, _ = self.make_plan(e)
        bad = ("0" if sha12[0] != "0" else "1") + sha12[1:]
        child = self.spawn(e, "apply", "--plan", pid, "--sha", bad, "--items", "M-001")
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, child.before)
        self.assertIn("--sha", child.before)
        self.assertNotIn("请原样输入", child.before)
        wb_state.cas_plan_status(e.db, pid, "draft", "superseded", reason="discarded_by_user")
        child = self.spawn(e, "apply", "--plan", pid, "--sha", sha12, "--items", "M-001")
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, child.before)
        self.assertIn("superseded", child.before)

    def test_r163_selection_errors_refused_before_challenge(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para T")
        os.rename(e.path("General/Home/Rename me.md"), e.path("General/Home/Renamed.md"))
        pid, sha12, _ = self.make_plan(e)
        for argv, msg in ((["--items", "T-001"], "--rename"), (["--items", "C-001"], "resolve"),
                          (["--rename", "M-001"], "--items"), ([], "没有选择任何条目")):
            with self.subTest(argv=argv):
                child = self.spawn(e, "apply", "--plan", pid, "--sha", sha12, *argv)
                child.expect(pexpect.EOF)
                child.close()
                self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, child.before)
                self.assertIn(msg, child.before)
                self.assertNotIn("请原样输入", child.before)

    def test_decision_commands_refuse_without_tty(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para N")
        pid, _sha12, _ = self.make_plan(e)
        for argv in (["reject", "--plan", pid, "--items", "M-001"], ["unreject", "--path", LEAF],
                     ["discard", "--plan", pid], ["restore-deleted", "--id", e.ids["del"]],
                     ["unblock", "--page", e.ids["kid"]], ["ack-anomaly", "--run", pid],
                     ["ack-backlog", "--path", LEAF], ["hold", "--run", "R9"], ["rebase", "--from-notion"],
                     ["reconcile", "--journal", "1", "--decision", "committed"], ["audit-scope"],
                     ["resolve", "--plan", pid, "--item", "M-001", "--take", "notion"]):
            with self.subTest(cmd=argv[0]):
                r = subprocess.run([sys.executable, "-I", "-B", WB] + e.argv + argv, capture_output=True, text=True,
                                   env=clean_env(), stdin=subprocess.DEVNULL)
                self.assertIn(r.returncode, (writeback.EXIT_GATE, writeback.EXIT_REFUSED), (argv, r.stderr))
        self.assertEqual(wb_plan.item_decisions(e.db, pid), {"M-001": "pending"})
        self.assertEqual(wb_plan.plan_row(e.db, pid)["status"], "draft")


# ============================================================================================ confirmed path (internal)
class TestConfirmedPathInternal(_Base):
    def test_r162_confirmations_approvals_cas_and_executor_call(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para C")
        e.write("General/Home/Folder/Brand new.md", "new body\n")
        pid, sha12, _ = self.make_plan(e)
        plan, sha = wb_plan.load_plan(pid, work=e.paths, db=e.db)
        sel = wb_plan.selection_from_args(plan, TP._Args(items=["M-001"]))
        items = wb_plan.selected_items(plan, sel)
        calls = []

        def executor(plan_id, selection, record, **kw):
            calls.append((plan_id, selection, record, kw))
            return {"status": "applied", "executed": ["M-001"]}
        res = writeback.confirm_and_execute(e.paths, e.cfg, e.db, plan, sha, items, sel, tty="/dev/pts/9",
                                            os_user="tester", client_factory=lambda: ("WC", "RC"), executor=executor)
        self.assertEqual(res["status"], "applied")
        rows = [dict(r) for r in e.db.execute("SELECT * FROM confirmations")]
        self.assertEqual([(r["plan_id"], r["item_id"], r["plan_sha256"], r["challenge_ok"], r["phrase_scope"])
                          for r in rows], [(pid, "M-001", sha, 1, "apply")])
        self.assertEqual(wb_plan.plan_row(e.db, pid)["status"], "confirmed")
        self.assertEqual(wb_plan.item_decisions(e.db, pid), {"M-001": "confirmed", "N-001": "pending"})
        ap = os.path.join(e.work, "approvals", "writeback", f"{pid}.json")
        with open(ap, encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertEqual(set(rec) >= {"plan_id", "plan_sha256", "os_user", "tty", "confirmed_at", "selection",
                                      "challenge_ok"}, True)
        self.assertEqual(rec["selection"], {"items": ["M-001"], "delete": [], "delete_blocks": [], "rename": [],
                                            "move": []})
        self.assertTrue(rec["challenge_ok"])
        with open(ap, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("tok-", raw)
        (cpid, csel, crec, ckw), = calls
        self.assertEqual((cpid, csel.items), (pid, ["M-001"]))
        self.assertEqual((crec["plan_sha256"], crec["phrase_scope"], crec["challenge_ok"]), (sha, "apply", True))
        self.assertEqual((ckw["client"], ckw["read_client"]), ("WC", "RC"))
        with self.assertRaises(writeback._Fail):
            writeback.confirm_and_execute(e.paths, e.cfg, e.db, plan, sha, items, sel, tty="t", os_user="u",
                                          client_factory=lambda: (None, None), executor=executor)
        self.assertEqual(len(calls), 1, "a plan can be confirmed only once (CAS draft->confirmed)")

    def test_real_executor_writes_only_the_confirmed_item(self):
        try:
            import wb_apply
        except Exception as exc:  # pragma: no cover - I2 module missing
            self.skipTest(f"wb_apply not importable: {exc}")
        e = self.env()
        e.fake.add_token("wb2", caps=("read", "update", "insert"), roots=(e.ids["leaf"],), secret="tok-wb2-i1-test")
        e.set_cfg(whitelist_page_ids=[e.ids["leaf"]], whitelist_database_ids=[], automation_free_database_ids=[])
        e.edit(LEAF, "second para", "second para REAL")
        e.edit(LEAF, "last para", "last para ALSO")
        pid, sha12, _ = self.make_plan(e)
        plan, sha = wb_plan.load_plan(pid, work=e.paths, db=e.db)
        sel = wb_plan.selection_from_args(plan, TP._Args(items=["M-001"]))
        items = wb_plan.selected_items(plan, sel)
        tokdir = os.path.join(e.tmp, "tok")
        os.makedirs(tokdir, mode=0o700)
        os.chmod(tokdir, 0o700)
        tokfile = os.path.join(tokdir, "writeback.env")
        with open(tokfile, "w", encoding="utf-8") as fh:
            fh.write("NOTION_WRITEBACK_TOKEN=" + e.fake.secret("wb2") + "\n")
        os.chmod(tokfile, 0o600)
        args = type("A", (), {"token_file": tokfile, "base_url": e.base, "read_token_file": e.read_token_file})()
        executor = functools.partial(wb_apply.execute_confirmed_plan,
                                     adopter=lambda ctx, written, **kw: {"ok": True, "note": "adoption skipped in test"})
        e.fake.clear_requests()
        res = writeback.confirm_and_execute(e.paths, e.cfg, e.db, plan, sha, items, sel, tty="/dev/pts/9", os_user="t",
                                            client_factory=lambda: writeback._make_clients(args, e.paths, e.cfg, "apply"),
                                            executor=executor)
        writes = e.writes()
        targets = {w["path"] for w in writes}
        allowed = {f"blocks/{o.get('target_id')}" for o in items[0]["ops"]} | \
            {f"blocks/{o.get('parent_id')}/children" for o in items[0]["ops"]}
        self.assertTrue(all(any(t.endswith(a) for a in allowed) for t in targets), (targets, allowed, res))
        self.assertTrue(all(w["token"] == "wb2" for w in writes))
        # the executor really ran the confirmed item (W6-W8 fail only because the adopter is a no-op stub)
        self.assertIn(res.get("status"), ("applied", "partially_applied"), res)
        self.assertEqual(res.get("executed"), ["M-001"], res)
        self.assertTrue(writes)
        texts = [b["paragraph"]["rich_text"] for b in e.fake.children(e.ids["leaf"]) if b["type"] == "paragraph"]
        joined = " | ".join("".join(x.get("plain_text", "") for x in r) for r in texts)
        self.assertIn("second para REAL", joined)
        self.assertIn("last para ALSO", joined)


# ============================================================================================ decisions through a pty
@unittest.skipIf(pexpect is None, "pexpect not installed")
class TestDecisionCommands(_Base):
    def test_r220_reject_then_replan_lists_rejected_and_zero_requests(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para REJ")
        pid, _sha12, _ = self.make_plan(e)
        e.fake.clear_requests()
        child = self.spawn(e, "reject", "--plan", pid, "--items", "M-001", "--reason", "not wanted")
        rc, out = self.answer(child)
        self.assertEqual(rc, 0, out)
        self.assertEqual(e.fake.requests, [], "reject sends no Notion request")
        self.assertEqual(wb_plan.item_decisions(e.db, pid)["M-001"], "rejected")
        r = dict(e.db.execute("SELECT * FROM rejections").fetchone())
        self.assertEqual((r["notion_id"], r["rel_path"], r["item_category"], r["reason"], r["active"], r["restore_notion"]),
                         (e.ids["leaf"], LEAF, "M", "not wanted", 1, 0))
        self.assertTrue(r["l_sha"] and r["os_user"] and r["created_at"] and r["plan_id"] == pid)
        pid2, _s, out2 = self.make_plan(e)
        p2, _ = wb_plan.load_plan(pid2, work=e.paths, db=e.db)
        self.assertEqual(TP.items_of(p2, "M"), [])
        self.assertEqual([x["rel_path"] for x in p2["rejected"]], [LEAF])
        # apply of a rejected item is refused
        with self.assertRaises(wb_plan.WbError):
            plan1, _ = wb_plan.load_plan(pid, work=e.paths, db=e.db)
            wb_plan.selected_items(plan1, wb_plan.Selection(items=["M-001"]), wb_plan.item_decisions(e.db, pid))

    def test_r220_reject_restore_notion_m_and_t(self):
        e = self.env()
        original = e.read(LEAF)
        e.edit(LEAF, "second para", "second para RESTORE")
        os.rename(e.path("General/Home/Rename me.md"), e.path("General/Home/Renamed.md"))
        pid, _sha12, _ = self.make_plan(e)
        e.fake.clear_requests()
        child = self.spawn(e, "reject", "--plan", pid, "--items", "M-001,T-001", "--restore-notion")
        rc, out = self.answer(child)
        self.assertEqual(rc, 0, out)
        self.assertEqual(e.fake.requests, [])
        self.assertEqual(e.read(LEAF), original, "local file restored to the last published (Notion) version")
        bk = os.path.join(e.work, "backup", "writeback-restore")
        found = [os.path.join(d, f) for d, _, fs in os.walk(bk) for f in fs]
        self.assertEqual(len(found), 1)
        with open(found[0], encoding="utf-8") as fh:
            self.assertIn("second para RESTORE", fh.read())
        self.assertTrue(os.path.isfile(e.path("General/Home/Rename me.md")))
        self.assertFalse(os.path.exists(e.path("General/Home/Renamed.md")))

    def test_r221_unreject_reenables(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para UN")
        pid, _sha12, _ = self.make_plan(e)
        writeback.reject_items(e.paths, e.db, wb_plan.load_plan(pid, work=e.paths, db=e.db)[0],
                               [wb_plan.load_plan(pid, work=e.paths, db=e.db)[0]["items"][0]], reason="x", os_user="u")
        child = self.spawn(e, "unreject", "--path", LEAF)
        rc, out = self.answer(child)
        self.assertEqual(rc, 0, out)
        self.assertEqual(e.db.execute("SELECT count(*) FROM rejections WHERE active=1").fetchone()[0], 0)
        pid2, _s, _ = self.make_plan(e)
        p2, _ = wb_plan.load_plan(pid2, work=e.paths, db=e.db)
        self.assertEqual(len(TP.items_of(p2, "M")), 1)

    def test_r222_discard_then_apply_refused(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para DIS")
        pid, sha12, _ = self.make_plan(e)
        child = self.spawn(e, "discard", "--plan", pid)
        rc, out = self.answer(child)
        self.assertEqual(rc, 0, out)
        row = wb_plan.plan_row(e.db, pid)
        self.assertEqual((row["status"], row["status_reason"]), ("superseded", "discarded_by_user"))
        child = self.spawn(e, "apply", "--plan", pid, "--sha", sha12, "--items", "M-001")
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, child.before)
        self.assertNotIn("请原样输入", child.before)

    def test_answer_no_changes_nothing(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para NO")
        pid, _sha12, _ = self.make_plan(e)
        child = self.spawn(e, "reject", "--plan", pid, "--items", "M-001")
        rc, out = self.answer(child, "n")
        self.assertEqual(rc, writeback.EXIT_FAIL)
        self.assertEqual(wb_plan.item_decisions(e.db, pid)["M-001"], "pending")
        self.assertEqual(e.db.execute("SELECT count(*) FROM rejections").fetchone()[0], 0)

    def test_r193_restore_deleted_unblock_ack_anomaly_ack_backlog(self):
        e = self.env()
        os.unlink(e.path("General/Home/Doomed.md"))
        e.hold_delete("del", "General/Home/Doomed.md")
        pid, _sha12, _ = self.make_plan(e)
        p, _ = wb_plan.load_plan(pid, work=e.paths, db=e.db)
        did = TP.items_of(p, "D")[0]["item_id"]
        rc, out = self.answer(self.spawn(e, "restore-deleted", "--plan", pid, "--item", did))
        self.assertEqual(rc, 0, out)
        self.assertEqual(e.db.execute("SELECT status FROM pending_deletes WHERE notion_id=?", (e.ids["del"],)).fetchone()[0],
                         "expired")
        self.assertEqual(wb_plan.item_decisions(e.db, pid)[did], "rejected")
        # unblock
        with wb_state.transaction(e.db):
            e.db.execute("INSERT INTO page_state(notion_id, state, reason, since) VALUES(?,?,?,?)",
                         (e.ids["kid"], "BLOCKED", "x", wb_common.now_iso()))
        rc, out = self.answer(self.spawn(e, "unblock", "--page", e.ids["kid"]))
        self.assertEqual(rc, 0, out)
        self.assertEqual(e.db.execute("SELECT state FROM page_state WHERE notion_id=?", (e.ids["kid"],)).fetchone()[0],
                         "normal")
        # ack-anomaly
        e.set_cfg(max_modified_notes=0)
        e.edit(LEAF, "second para", "second para ACK")
        pid2, _s, _ = self.make_plan(e)
        rc, out = self.answer(self.spawn(e, "ack-anomaly", "--run", pid2))
        self.assertEqual(rc, 0, out)
        ack = wb_state.get_meta_json(e.db, "anomaly_ack")
        self.assertEqual(ack["plan_id"], pid2)
        pid3, _s, _ = self.make_plan(e)
        p3, _ = wb_plan.load_plan(pid3, work=e.paths, db=e.db)
        self.assertTrue(TP.items_of(p3, "M")[0]["executable"])
        # ack-backlog
        bl = {e.ids["leaf"]: {"l_sha": "x", "detected_at": wb_common.now_iso()}}
        wb_state.set_meta(e.db, "initial_backlog", bl)
        rc, out = self.answer(self.spawn(e, "ack-backlog", "--path", LEAF))
        self.assertEqual(rc, 0, out)
        self.assertTrue(wb_state.get_meta_json(e.db, "initial_backlog")[e.ids["leaf"]]["acked_at"])

    def test_r192_resolve_take_notion_and_obsidian_gate(self):
        e = self.env()
        published = e.read(KID)
        e.edit(KID, "kid body", "kid body LOCAL")
        e.remote_text("kid", 0, "kid body REMOTE")
        pid, _sha12, _ = self.make_plan(e)
        p, _ = wb_plan.load_plan(pid, work=e.paths, db=e.db)
        cid = TP.items_of(p, "C")[0]["item_id"]
        e.fake.clear_requests()
        rc, out = self.answer(self.spawn(e, "resolve", "--plan", pid, "--item", cid, "--take", "notion"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(e.fake.requests, [])
        self.assertEqual(e.read(KID), published, "restored to the last published version; next refresh REPLACEs")
        self.assertEqual(wb_plan.item_decisions(e.db, pid)[cid], "resolved_notion")
        # take obsidian needs the challenge phrase: a wrong phrase creates no plan
        e.edit(KID, "kid body", "kid body LOCAL2")
        pid2, _s, _ = self.make_plan(e)
        p2, _ = wb_plan.load_plan(pid2, work=e.paths, db=e.db)
        cid2 = TP.items_of(p2, "C")[0]["item_id"]
        child = self.spawn(e, "resolve", "--plan", pid2, "--item", cid2, "--take", "obsidian")
        child.expect("请原样输入下面这一行以确认")
        self.assertIn("本项将覆盖 Notion 自", child.before)
        child.expect(r"> ")
        self.assertRegex(child.before, r"确认 \S{4} 以本地为准 C-\d{3} [0-9A-F]{6}")
        child.sendline("确认 wrong")
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, writeback.EXIT_FAIL)
        self.assertEqual(e.db.execute("SELECT count(*) FROM plans").fetchone()[0], 2)

    def test_r193_hold_converts_paused_round_deletions(self):
        e = self.env()
        os.unlink(e.path("General/Home/Doomed.md"))
        import wb_scan
        scan = wb_scan.scan(e.vault, os.path.join(e.work, "state", "export.db"),
                            os.path.join(e.work, "state", "writeback.db"), e.cfg)
        rep = os.path.join(e.work, "reports", "R9")
        os.makedirs(rep)
        with open(os.path.join(rep, "wb_scan.json"), "w", encoding="utf-8") as fh:
            json.dump(scan, fh)
        with open(os.path.join(e.work, "state", "paused_runs.json"), "w", encoding="utf-8") as fh:
            json.dump({"latest": "R9", "runs": {"R9": {"status": "paused", "reasons": ["local_mass_delete: …"],
                                                       "snapshot": e.snap, "sidebar": os.path.join(e.work, "sidebar.json"),
                                                       "staging": os.path.join(e.work, "staging", "R1")}}}, fh)
        rc, out = self.answer(self.spawn(e, "hold", "--run", "R9"))
        self.assertEqual(rc, 0, out)
        row = e.db.execute("SELECT status, rel_path FROM pending_deletes WHERE notion_id=?", (e.ids["del"],)).fetchone()
        self.assertEqual(tuple(row), ("hold", "General/Home/Doomed.md"))
        with open(os.path.join(e.work, "state", "paused_runs.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["runs"]["R9"]["status"], "discarded")

    def _token_file(self, e, name="wb"):
        d = os.path.join(e.tmp, "tokdir")
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        f = os.path.join(d, "writeback.env")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("NOTION_WRITEBACK_TOKEN=" + e.fake.secret(name) + "\n")
        os.chmod(f, 0o600)
        return f

    def test_audit_scope_wired_to_executor_and_clears_downgrade_after_gate(self):
        try:
            import wb_apply  # noqa: F401
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"wb_apply not importable: {exc}")
        e = self.env()
        e.fake.add_token("wb2", caps=("read", "update", "insert"), roots=(e.ids["leaf"],), secret="tok-wb2-i1-audit")
        e.set_cfg(whitelist_page_ids=[e.ids["leaf"]], whitelist_database_ids=[], automation_free_database_ids=[])
        import wb_config
        wb_config.record_downgrade(e.db, "plan", "test downgrade")
        tok = self._token_file(e, "wb2")
        e.fake.clear_requests()
        child = self.spawn(e, "--token-file", tok, "audit-scope")
        child.expect(r"\[y/N\]")
        self.assertIn('"ok": true', child.before)
        self.assertIn("forced_mode=plan", child.before)
        child.sendline("y")
        child.expect(pexpect.EOF)
        child.close()
        rc, out = child.exitstatus, child.before
        self.assertEqual(rc, 0, out)
        self.assertIn("已解除自动降级", out)
        self.assertIsNone(wb_state.get_meta(e.db, "forced_mode"))
        self.assertEqual(e.writes(), [], "audit-scope only reads (POST search is a read)")
        # an extra reachable object -> not ok -> downgraded again, nothing cleared
        e.fake.grant("wb2", e.ids["other"])
        child = self.spawn(e, "--token-file", tok, "audit-scope")
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, writeback.EXIT_FAIL, child.before)
        self.assertEqual(wb_state.get_meta(e.db, "forced_mode"), "plan")

    def test_rollback_and_resume_refused_by_state_after_gate(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para RB")
        pid, _sha12, _ = self.make_plan(e)
        e.fake.clear_requests()
        child = self.spawn(e, "rollback", "--plan", pid)
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, child.before)
        self.assertNotIn("请原样输入", child.before)
        child = self.spawn(e, "apply", "--resume", pid)
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, child.before)
        self.assertIn("applying", child.before)
        self.assertEqual(e.writes(), [])
        self.assertEqual(e.db.execute("SELECT count(*) FROM confirmations").fetchone()[0], 0)

    def test_r193_rebase_from_notion(self):
        e = self.env(cfg={"mode": "plan"})
        published = e.read(LEAF)
        e.edit(LEAF, "second para", "second para OLD VERSION FROM SYNC")
        rc, out = self.answer(self.spawn(e, "rebase", "--from-notion"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(e.read(LEAF), published)
        self.assertIsNotNone(wb_state.get_meta(e.db, "rebased_at"))


def _real_run(tc, e, pid, sel, roots, name="wb2"):
    """The internal confirmed path (confirm_and_execute) with the REAL wb_apply executor against the fake server;
    the write token reaches only `roots`; adoption is a no-op stub (W6-W8 then fail by design)."""
    import wb_apply
    if name not in [t["name"] for t in e.fake.tokens.values()]:
        e.fake.add_token(name, caps=("read", "update", "insert"), roots=tuple(roots), secret=f"tok-{name}-i1-fix")
    plan, sha = wb_plan.load_plan(pid, work=e.paths, db=e.db)
    items = wb_plan.selected_items(plan, sel)
    tokdir = os.path.join(e.tmp, "tok-" + name)
    os.makedirs(tokdir, mode=0o700, exist_ok=True)
    os.chmod(tokdir, 0o700)
    tokfile = os.path.join(tokdir, "writeback.env")
    with open(tokfile, "w", encoding="utf-8") as fh:
        fh.write("NOTION_WRITEBACK_TOKEN=" + e.fake.secret(name) + "\n")
    os.chmod(tokfile, 0o600)
    args = type("A", (), {"token_file": tokfile, "base_url": e.base, "read_token_file": e.read_token_file})()
    executor = functools.partial(wb_apply.execute_confirmed_plan,
                                 adopter=lambda ctx, written, **kw: {"ok": True, "note": "adoption skipped in test"})
    with mock.patch.object(wb_plan, "obsidian_status", return_value=TP.OBSIDIAN_RUNNING):
        return plan, items, writeback.confirm_and_execute(
            e.paths, e.cfg, e.db, plan, sha, items, sel, tty="/dev/pts/9", os_user="t",
            client_factory=lambda: writeback._make_clients(args, e.paths, e.cfg, "apply"), executor=executor)


class TestFixRoundCli(_Base):
    """Review findings of wave 3 on the CLI side + the I2 requests (each test names what it covers)."""

    def test_plan_built_in_another_mode_is_refused_f1(self):
        e = self.env(cfg={"mode": "plan"})
        e.edit(LEAF, "second para", "second para MODE")
        pid, sha12, _ = self.make_plan(e)
        e.set_cfg(mode="whitelist")
        plan, sha = wb_plan.load_plan(pid, work=e.paths, db=e.db)
        self.assertEqual(plan["mode"], "plan")
        sel = wb_plan.Selection(items=["M-001"])
        items = wb_plan.selected_items(plan, sel)
        with self.assertRaises(writeback._Fail) as cm:
            writeback.confirm_and_execute(e.paths, e.cfg, e.db, plan, sha, items, sel, tty="t", os_user="u",
                                          client_factory=lambda: (None, None), executor=lambda *a, **k: {})
        self.assertIn("生成时的模式", cm.exception.msg)
        self.assertEqual(e.db.execute("SELECT count(*) FROM confirmations").fetchone()[0], 0)
        self.assertEqual(wb_plan.plan_row(e.db, pid)["status"], "draft")
        if pexpect is not None:
            child = self.spawn(e, "apply", "--plan", pid, "--sha", sha12, "--items", "M-001")
            child.expect(pexpect.EOF)
            child.close()
            self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, child.before)
            self.assertIn("生成时的模式", child.before)
            self.assertNotIn("请原样输入", child.before)

    def test_mode_off_every_subcommand_only_explains_f19(self):
        e = self.env(cfg={"mode": "off"})
        e.edit(LEAF, "second para", "second para OFF")
        e.close_db()
        before = state_digest(e)
        vault_before = e.read(LEAF)
        P = "wb-20261118T000000-abcd"
        for argv in (["reject", "--plan", P, "--items", "M-001"], ["unreject", "--path", LEAF], ["discard", "--plan", P],
                     ["restore-deleted", "--id", e.ids["del"]], ["resolve", "--plan", P, "--item", "C-001", "--take",
                                                                    "notion"],
                     ["resolve", "--plan", P, "--item", "C-001", "--take", "obsidian"],
                     ["reconcile", "--journal", "1", "--decision", "committed"], ["unblock", "--page", e.ids["kid"]],
                     ["rollback", "--plan", P], ["ack-anomaly", "--run", P], ["ack-backlog", "--path", LEAF],
                     ["hold", "--run", "R9"], ["rebase", "--from-notion"], ["audit-scope"],
                     ["apply", "--plan", P, "--sha", "0" * 12, "--all-safe"], ["apply", "--resume", P]):
            with self.subTest(cmd=argv[0]):
                rc, out, err = self.main(e, *argv)
                self.assertEqual(rc, writeback.EXIT_REFUSED, (argv, out, err))
                self.assertIn("off", err)
        rc, out, err = self.main(e, "plan")
        self.assertEqual(rc, 0, err)
        self.assertIn("off", out)
        self.assertEqual(state_digest(e), before, "mode off: nothing under state/ changed")
        self.assertEqual(e.read(LEAF), vault_before)

    def test_reject_only_pending_items_f12(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para REJ2")
        pid, _sha12, _ = self.make_plan(e)
        plan, _ = wb_plan.load_plan(pid, work=e.paths, db=e.db)
        with wb_state.transaction(e.db):
            e.db.execute("UPDATE plans SET status='applied' WHERE plan_id=?", (pid,))
            e.db.execute("UPDATE plan_items SET decision='executed' WHERE plan_id=?", (pid,))
        with self.assertRaises(wb_plan.WbError):
            writeback.reject_items(e.paths, e.db, plan, [plan["items"][0]], reason="late", os_user="u")
        self.assertEqual(wb_plan.item_decisions(e.db, pid), {"M-001": "executed"})
        self.assertEqual(e.db.execute("SELECT count(*) FROM rejections").fetchone()[0], 0)
        with wb_state.transaction(e.db):
            e.db.execute("UPDATE plans SET status='confirmed' WHERE plan_id=?", (pid,))
            e.db.execute("UPDATE plan_items SET decision='pending' WHERE plan_id=?", (pid,))
        with self.assertRaises(wb_plan.WbError):
            writeback.reject_items(e.paths, e.db, plan, [plan["items"][0]], reason="late", os_user="u")
        if pexpect is not None:
            with wb_state.transaction(e.db):
                e.db.execute("UPDATE plans SET status='applied' WHERE plan_id=?", (pid,))
                e.db.execute("UPDATE plan_items SET decision='executed' WHERE plan_id=?", (pid,))
            child = self.spawn(e, "reject", "--plan", pid, "--items", "M-001")
            child.expect(pexpect.EOF)
            child.close()
            self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, child.before)
            self.assertIn("executed", child.before)
            self.assertNotIn("[y/N]", child.before)
            self.assertEqual(wb_plan.item_decisions(e.db, pid), {"M-001": "executed"})

    @unittest.skipIf(pexpect is None, "pexpect not installed")
    def test_resume_closes_an_applying_plan_that_never_sent_anything_f20(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para CLOSE")
        pid, _sha12, _ = self.make_plan(e)
        wb_state.cas_plan_status(e.db, pid, "draft", "confirmed")
        wb_state.cas_plan_status(e.db, pid, "confirmed", "applying")
        e.fake.clear_requests()
        child = self.spawn(e, "apply", "--resume", pid)
        child.expect(r"\[y/N\]")
        self.assertIn("没有任何已发出或待发出的操作", child.before)
        child.sendline("y")
        child.expect(pexpect.EOF)
        child.close()
        rc, out = child.exitstatus, child.before
        self.assertEqual(rc, 0, out)
        self.assertIn("已关闭为 aborted", out)
        row = wb_plan.plan_row(e.db, pid)
        self.assertEqual((row["status"], row["status_reason"]), ("aborted", "closed_by_user:no_journal_writes"))
        self.assertEqual(e.fake.requests, [])
        # a plan with a sent / done journal row but no intent row is not closed this way: apply --resume offers the
        # executor's closing run (0 write requests) behind the challenge phrase (I2 request); a wrong phrase changes
        # nothing
        e.edit(LEAF, "second para CLOSE", "second para CLOSE2")
        pid2, _s, _ = self.make_plan(e)
        wb_state.cas_plan_status(e.db, pid2, "draft", "confirmed")
        wb_state.cas_plan_status(e.db, pid2, "confirmed", "applying")
        with wb_state.transaction(e.db):
            e.db.execute("INSERT INTO journal(plan_id, item_id, op, target_id, state, ts) VALUES(?,?,?,?,?,?)",
                         (pid2, "M-001", "UPDATE", "x", "sent", wb_common.now_iso()))
        e.fake.clear_requests()
        child = self.spawn(e, "apply", "--resume", pid2)
        child.expect("请原样输入下面这一行以确认")
        shown = child.before
        child.expect(r"> ")
        shown += child.before
        self.assertIn("本次不会发送任何写入请求（0 个）", shown)
        self.assertRegex(shown, r"确认 \S{4} 续执行0\[\] [0-9A-F]{6}")
        child.sendline("确认 wrong")
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, writeback.EXIT_FAIL, child.before)
        self.assertEqual(wb_plan.plan_row(e.db, pid2)["status"], "applying")
        self.assertEqual(e.fake.requests, [])
        self.assertEqual(e.db.execute("SELECT count(*) FROM confirmations").fetchone()[0], 0)

    @unittest.skipIf(pexpect is None, "pexpect not installed")
    def test_gate_list_shows_delete_subtree_and_phrase_counts_children_f4_f13(self):
        e = self.env(cfg={"mode": "full"})
        e.set_cfg(whitelist_page_ids=list(e.cfg_dict["whitelist_page_ids"]) + [e.ids["del2c"]])
        os.unlink(e.path("General/Home/Doomed parent/Doomed parent.md"))
        os.unlink(e.path("General/Home/Doomed parent/Doomed child.md"))
        e.hold_delete("del2", "General/Home/Doomed parent/Doomed parent.md",
                      subtree=[{"notion_id": e.ids["del2c"], "rel": "General/Home/Doomed parent/Doomed child.md"}])
        pid, sha12, _ = self.make_plan(e)
        p, _ = wb_plan.load_plan(pid, work=e.paths, db=e.db)
        did = TP.items_of(p, "D")[0]["item_id"]
        e.fake.clear_requests()
        child = self.spawn(e, "apply", "--plan", pid, "--sha", sha12, "--delete", did)
        child.expect("请原样输入下面这一行以确认")
        shown = child.before
        child.expect(r"> ")
        shown += child.before
        self.assertIn("连带子页面 1 个", shown)
        self.assertIn("子页面：Doomed child", shown)
        self.assertIn("风险 high", shown)
        self.assertRegex(shown, rf"删除1\[{did}\]（含子页面1个）")
        child.sendline("确认 wrong")
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, writeback.EXIT_FAIL)
        self.assertEqual(e.writes(), [])

    def test_r223_unapproved_items_are_never_written(self):
        """R-223: with several executable items in a plan, only the confirmed one reaches Notion (fake write log vs
        the confirmed item's ops), the confirmations rows name only it, unselected pages stay byte-identical."""
        e = self.env()
        e.set_cfg(whitelist_page_ids=[e.ids["leaf"], e.ids["kid"], e.ids["kid2"]], whitelist_database_ids=[],
                  automation_free_database_ids=[])
        e.edit(LEAF, "second para", "second para NOT CONFIRMED")
        e.edit(KID, "kid body", "kid body CONFIRMED")
        e.edit("General/Home/Folder2/Kid2.md", "kid2 body", "kid2 body NOT CONFIRMED")
        pid, _sha12, _ = self.make_plan(e)
        p, _ = wb_plan.load_plan(pid, work=e.paths, db=e.db)
        m = {it["rel_path"]: it for it in TP.items_of(p, "M")}
        self.assertTrue(all(x["executable"] for x in m.values()), [x["blocked_by"] for x in m.values()])
        before_leaf = json.dumps(e.fake.children(e.ids["leaf"]), sort_keys=True)
        before_kid2 = json.dumps(e.fake.children(e.ids["kid2"]), sort_keys=True)
        e.fake.clear_requests()
        kid_item = m[KID]["item_id"]
        plan, items, res = _real_run(self, e, pid, wb_plan.Selection(items=[kid_item]),
                                     roots=(e.ids["leaf"], e.ids["kid"], e.ids["kid2"]))
        self.assertEqual(res.get("executed"), [kid_item], res)
        writes = e.writes()
        self.assertTrue(writes)
        allowed = {f"blocks/{o.get('target_id')}" for o in items[0]["ops"]} | \
            {f"blocks/{o.get('parent_id')}/children" for o in items[0]["ops"]}
        for w in writes:
            self.assertTrue(any(w["path"].endswith(a) for a in allowed), (w["method"], w["path"], allowed))
        self.assertEqual(json.dumps(e.fake.children(e.ids["leaf"]), sort_keys=True), before_leaf)
        self.assertEqual(json.dumps(e.fake.children(e.ids["kid2"]), sort_keys=True), before_kid2)
        rows = [tuple(r) for r in e.db.execute("SELECT item_id, phrase_scope FROM confirmations")]
        self.assertEqual(rows, [(kid_item, "apply")])
        dec = wb_plan.item_decisions(e.db, pid)
        self.assertEqual(dec[kid_item], "executed")
        self.assertTrue(all(v == "pending" for k, v in dec.items() if k != kid_item), dec)

    def test_planner_and_real_executor_nested_block_delete_and_last_child_f11_f15(self):
        """End to end planner -> I2: a parent block deleted together with its child (post-order B items) and the only
        child of a quote deleted (the parent's has_children flips): executed, W1-W3 pass, Notion equals L."""
        def seed(e):
            e.ids["nest"] = e.fake.add_page("Nest", parent=e.ids["home"], children=[
                TP.para("head"), TP.bullet("parent item", children=[TP.bullet("kid item")]), TP.para("tail")])
            e.ids["quo"] = e.fake.add_page("Quo", parent=e.ids["home"], children=[
                TP.para("intro"), {"type": "quote", "quote": {"rich_text": [TP.rt("quoted")],
                                                              "children": [TP.para("only child")]}}])
        e = self.env(seed=seed, cfg={"allow_block_trash": True})
        e.set_cfg(whitelist_page_ids=[e.ids["nest"], e.ids["quo"]], whitelist_database_ids=[],
                  automation_free_database_ids=[])
        nest, quo = "General/Home/Nest.md", "General/Home/Quo.md"
        e.edit(nest, "- parent item\n    - kid item\n\n", "")
        e.edit(quo, "> quoted\n>\n> only child\n", "> quoted\n")
        pid, _sha12, _ = self.make_plan(e)
        p, _ = wb_plan.load_plan(pid, work=e.paths, db=e.db)
        bs = {rel: [it for it in TP.items_of(p, "B") if it["rel_path"] == rel] for rel in (nest, quo)}
        self.assertEqual({k: len(v) for k, v in bs.items()}, {nest: 2, quo: 1}, (p["items"], p["refused"]))
        ids = [it["item_id"] for v in bs.values() for it in v]
        self.assertTrue(all(it["executable"] for v in bs.values() for it in v))
        plan, items, res = _real_run(self, e, pid, wb_plan.Selection(delete_blocks=ids),
                                     roots=(e.ids["nest"], e.ids["quo"]))
        self.assertEqual(sorted(res.get("executed") or []), sorted(ids), res)
        self.assertIn(res.get("status"), ("applied", "partially_applied"), res)
        w = res.get("w_checks") or {}
        for k in ("W1", "W2", "W3"):
            self.assertEqual((w.get(k) or {}).get("status"), "pass", (k, w.get(k)))
        self.assertEqual([wb_plan._plain((b.get(b["type"]) or {}).get("rich_text"))
                          for b in e.fake.children(e.ids["nest"]) if not b.get("in_trash")], ["head", "tail"])
        quote = [b for b in e.fake.children(e.ids["quo"]) if b["type"] == "quote"][0]
        self.assertEqual([b for b in e.fake.children(quote["id"]) if not b.get("in_trash")], [])

    def test_every_option_has_help_text_f18(self):
        ap = writeback.build_parser()
        missing = []
        for action in ap._actions:
            if hasattr(action, "choices") and isinstance(action.choices, dict):
                for name, sp in action.choices.items():
                    for a in sp._actions:
                        if a.option_strings and a.option_strings != ["-h", "--help"] and not a.help:
                            missing.append((name, a.option_strings))
        self.assertEqual(missing, [])
        r = subprocess.run([sys.executable, "-I", "-B", WB, "reject", "--help"], capture_output=True, text=True,
                           env=clean_env())
        self.assertIn("拒绝原因", r.stdout)

    @unittest.skipIf(pexpect is None, "pexpect not installed")
    def test_ack_backlog_for_a_new_file_f7(self):
        e = self.env(enabled=False)
        rel = "General/Home/Folder/Brand new.md"
        e.write(rel, "pre-existing new file\n")
        self.make_plan(e)
        self.assertIn("new:" + rel, wb_state.get_meta_json(e.db, "initial_backlog", {}))
        rc, out = self.answer(self.spawn(e, "ack-backlog", "--path", rel))
        self.assertEqual(rc, 0, out)
        self.assertIn("new:" + rel, wb_state.get_meta_json(e.db, "backlog_acked", {}))


# ============================================================================================ L-17 (wave 3.5)
class TestUnregisteredRenameCliL17(_Base):
    """L-17 on the CLI: `status` and `plan` show the notice while the scan has a rename / move no refresh has
    registered yet; apply of a plan built in that state is refused at the selection (before the challenge, 0 writes,
    0 confirmations); a plan built BEFORE the rename is refused by confirm_and_execute: ONE fresh read-only vault
    scan per apply, after the phrase (a rename made while the challenge is on screen counts too) and before any
    confirmation row, client or executor (fix loop 1: there was a second scan before the challenge)."""

    H = wb_plan.UNREGISTERED_RENAME_HINT

    def counts(self, e, pid):
        return (e.db.execute("SELECT count(*) FROM confirmations WHERE plan_id=?", (pid,)).fetchone()[0],
                wb_plan.plan_row(e.db, pid)["status"])

    @unittest.skipIf(pexpect is None, "pexpect not installed")
    def test_status_and_plan_show_the_notice_and_apply_is_refused_at_selection(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para L17")
        os.rename(e.path(TP.REN), e.path(TP.REN_NEW))
        rc, out, err = self.main(e, "status")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"注意 UNREGISTERED_RENAME：{self.H}", out)
        self.assertIn(f"同文件夹改名：{TP.REN} → {TP.REN_NEW}", out)
        rc, out, err = self.main(e, "status", "--json")
        self.assertEqual([(x["kind"], x["new_rel"]) for x in json.loads(out)["unregistered_renames"]],
                         [("renamed_same_folder", TP.REN_NEW)])
        pid, sha12, out = self.make_plan(e)
        self.assertIn(f"注意 UNREGISTERED_RENAME：{self.H}", out)
        self.assertIn("可执行 0", out)
        plan, sha = wb_plan.load_plan(pid, work=e.paths, db=e.db)
        self.assertEqual(sorted(it["category"] for it in plan["items"]), ["M", "T"])
        self.assertTrue(all(wb_plan.UNREGISTERED_RENAME in it["blocked_by"] and not it["executable"]
                            for it in plan["items"]))
        rc, show, err = self.main(e, "show", "--plan", pid)
        self.assertIn(self.H, show)
        e.fake.clear_requests()
        for argv in (["--items", "M-001"], ["--all-safe"], ["--items", "M-001", "--rename", "T-001"]):
            with self.subTest(argv=argv):
                child = self.spawn(e, "apply", "--plan", pid, "--sha", sha12, *argv)
                child.expect(pexpect.EOF)
                child.close()
                self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, child.before)
                self.assertIn("UNREGISTERED_RENAME", child.before)
                self.assertIn(self.H, child.before)
                self.assertNotIn("请原样输入", child.before)
        self.assertEqual(e.writes(), [])
        self.assertEqual(self.counts(e, pid), (0, "draft"))
        # the internal confirmed path refuses the same plan before any confirmation row / client / executor
        called = []
        with self.assertRaises(writeback._Fail) as cm:
            writeback.confirm_and_execute(e.paths, e.cfg, e.db, plan, sha, plan["items"][:1],
                                          wb_plan.Selection(items=[plan["items"][0]["item_id"]]), tty="t", os_user="u",
                                          client_factory=lambda: called.append("clients"),
                                          executor=lambda *a, **k: called.append("executor"))
        self.assertEqual(cm.exception.code, writeback.EXIT_REFUSED)
        self.assertIn(self.H, cm.exception.msg)
        self.assertEqual(called, [])
        self.assertEqual(self.counts(e, pid), (0, "draft"))
        # after the refresh registered the rename the notice is gone
        e.register_rename("ren", TP.REN, TP.REN_NEW)
        rc, out, err = self.main(e, "status")
        self.assertNotIn("UNREGISTERED_RENAME", out)
        self.assertIn("命名差异", out)

    @unittest.skipIf(pexpect is None, "pexpect not installed")
    def test_plan_built_before_the_move_is_refused_at_apply(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para EARLY")
        pid, sha12, out = self.make_plan(e)
        self.assertNotIn("UNREGISTERED_RENAME", out)
        plan, sha = wb_plan.load_plan(pid, work=e.paths, db=e.db)
        sel = wb_plan.Selection(items=["M-001"])
        items = wb_plan.selected_items(plan, sel)                  # the plan itself is executable
        os.rename(e.path(TP.KID2), e.path(TP.KID2_MOVED))           # moved after the plan, no refresh since
        e.fake.clear_requests()
        child = self.spawn(e, "apply", "--plan", pid, "--sha", sha12, "--items", "M-001")
        child.expect("请原样输入下面这一行以确认")
        shown = child.before
        child.expect(r"> ")
        shown += child.before
        m = re.search(r"(确认 \S{4} 修改1 新增0 删除0 改名0 [0-9A-F]{6})", shown)
        self.assertIsNotNone(m, shown)
        child.sendline(m.group(1))                                  # the one vault scan runs after the phrase
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, child.before)
        self.assertIn(self.H, child.before)
        self.assertIn(f"跨文件夹移动：{TP.KID2} → {TP.KID2_MOVED}", child.before)
        self.assertEqual(e.writes(), [])
        self.assertEqual(self.counts(e, pid), (0, "draft"))
        called = []
        with self.assertRaises(writeback._Fail) as cm:
            writeback.confirm_and_execute(e.paths, e.cfg, e.db, plan, sha, items, sel, tty="t", os_user="u",
                                          client_factory=lambda: called.append("clients"),
                                          executor=lambda *a, **k: called.append("executor"))
        self.assertIn(self.H, cm.exception.msg)
        # a scan that cannot run refuses too (fail closed)
        import wb_scan
        with mock.patch.object(wb_scan, "scan", side_effect=OSError("disk")):
            with self.assertRaises(writeback._Fail) as cm2:
                writeback.confirm_and_execute(e.paths, e.cfg, e.db, plan, sha, items, sel, tty="t", os_user="u",
                                              client_factory=lambda: called.append("clients"),
                                              executor=lambda *a, **k: called.append("executor"))
        self.assertIn("库扫描失败", cm2.exception.msg)
        self.assertEqual(called, [])
        self.assertEqual(e.writes(), [])
        self.assertEqual(self.counts(e, pid), (0, "draft"))
        # once a refresh holds the moved object with its current path, the move is registered: the check passes
        e.hold_like_refresh("kid2", [TP.KID2, TP.KID2_MOVED])
        res = writeback.confirm_and_execute(e.paths, e.cfg, e.db, plan, sha, items, sel, tty="t", os_user="u",
                                            client_factory=lambda: (None, None),
                                            executor=lambda *a, **k: {"status": "applied", "executed": ["M-001"]})
        self.assertEqual(res["status"], "applied")
        self.assertEqual(self.counts(e, pid), (1, "confirmed"))


    def test_unpublished_override_still_blocks_and_held_ancestor_registers_fix2(self):
        """Fix loop 2 (W3.5 L17 review, requirement lens): a refresh round that opened the name override and then
        paused for review published nothing, so `status` keeps the notice (with what is missing), `plan` stays
        blocked and the fresh scan of confirm_and_execute refuses a plan built before the rename (before: the open
        override counted as registered, the notice vanished and the apply ended partially_applied, W6 C05). After
        the round that publishes it the notice is gone. A child renamed below a folder note held with its subtree is
        registered by that ancestor hold in `status`, `status --json` and the apply check (export parent chain)."""
        e = self.env()
        e.edit(LEAF, "second para", "second para FIX2")
        pid0, sha0, _ = self.make_plan(e)                                  # built before the rename
        plan0, s0 = wb_plan.load_plan(pid0, work=e.paths, db=e.db)
        sel0 = wb_plan.Selection(items=["M-001"])
        items0 = wb_plan.selected_items(plan0, sel0)
        os.rename(e.path(TP.REN), e.path(TP.REN_NEW))
        e.register_rename("ren", TP.REN, TP.REN_NEW, published=False)      # opened by a round that then paused
        rc, out, err = self.main(e, "status")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"注意 UNREGISTERED_RENAME：{self.H}", out)
        self.assertIn(f"同文件夹改名：{TP.REN} → {TP.REN_NEW}（{wb_plan.OVERRIDE_UNPUBLISHED_TEXT}）", out)
        rc, out, err = self.main(e, "status", "--json")
        self.assertEqual([(x["new_rel"], x.get("override_open")) for x in json.loads(out)["unregistered_renames"]],
                         [(TP.REN_NEW, True)])
        called = []
        with self.assertRaises(writeback._Fail) as cm:
            writeback.confirm_and_execute(e.paths, e.cfg, e.db, plan0, s0, items0, sel0, tty="t", os_user="u",
                                          client_factory=lambda: called.append("clients"),
                                          executor=lambda *a, **k: called.append("executor"))
        self.assertEqual(cm.exception.code, writeback.EXIT_REFUSED)
        self.assertIn(self.H, cm.exception.msg)
        self.assertEqual(called, [])
        self.assertEqual(self.counts(e, pid0), (0, "draft"))
        pid, sha12, out = self.make_plan(e)
        self.assertIn("可执行 0", out)
        self.assertIn(wb_plan.OVERRIDE_UNPUBLISHED_TEXT, out)
        # the round that publishes it (ADOPT_RENAME moves the published row): registered
        e.register_rename("ren", TP.REN, TP.REN_NEW)
        rc, out, err = self.main(e, "status")
        self.assertNotIn("UNREGISTERED_RENAME", out)
        # a child renamed below a folder note held WITH its subtree: registered by the ancestor hold
        os.rename(e.path(TP.KID), e.path("General/Home/Folder/Kid renamed.md"))
        e.hold_like_refresh("folder", [], "post_verify_failed:C04", source="post_verify", auto_release=False)
        rc, out, err = self.main(e, "status")
        self.assertNotIn("UNREGISTERED_RENAME", out)
        rc, out, err = self.main(e, "status", "--json")
        self.assertEqual(json.loads(out)["unregistered_renames"], [])
        pid2, sha2, out = self.make_plan(e)
        self.assertNotIn("UNREGISTERED_RENAME", out)
        plan2, s2 = wb_plan.load_plan(pid2, work=e.paths, db=e.db)
        m = [it["item_id"] for it in plan2["items"] if it["category"] == "M" and it["rel_path"] == LEAF]
        sel2 = wb_plan.Selection(items=m)
        res = writeback.confirm_and_execute(e.paths, e.cfg, e.db, plan2, s2, wb_plan.selected_items(plan2, sel2), sel2,
                                            tty="t", os_user="u", client_factory=lambda: (None, None),
                                            executor=lambda *a, **k: {"status": "applied", "executed": m})
        self.assertEqual(res["status"], "applied")
        self.assertEqual(self.counts(e, pid2), (1, "confirmed"))

    def test_apply_runs_one_vault_scan_after_the_phrase(self):
        """Fix loop 1 (W3.5 L17 review): every apply ran two read-only full-vault scans (before the challenge and
        in confirm_and_execute; about 5.6 s each on the production vault). Now one, after the phrase: a clean plan
        scans once and reaches the executor; a plan built before a move is refused after the phrase by that scan,
        before any confirmation row, client or executor."""
        import types
        import wb_scan
        real_scan = wb_scan.scan
        e = self.env()
        e.edit(LEAF, "second para", "second para ONE SCAN")
        pid, sha12, _ = self.make_plan(e)
        events = []

        def scan(*a, **k):
            events.append("scan")
            return real_scan(*a, **k)

        def phrase(ph, lines):
            events.append("phrase")
            return True

        def execute(*a, **k):
            events.append("executor")
            return {"plan_id": pid, "status": "applied", "executed": ["M-001"]}

        def clients(*a, **k):
            events.append("clients")
            return None, None
        argv = ["apply", "--plan", pid, "--sha", sha12, "--items", "M-001"]
        with mock.patch.object(wb_scan, "scan", side_effect=scan), \
                mock.patch.object(writeback, "_human", return_value="pts/test"), \
                mock.patch.object(writeback, "_phrase", side_effect=phrase), \
                mock.patch.object(writeback, "_make_clients", side_effect=clients), \
                mock.patch.object(writeback, "_apply_module",
                                  return_value=types.SimpleNamespace(execute_confirmed_plan=execute)):
            rc, out, err = self.main(e, *argv)
            self.assertEqual(rc, writeback.EXIT_OK, out + err)
            self.assertEqual(events, ["phrase", "scan", "clients", "executor"])
            self.assertEqual(self.counts(e, pid), (1, "confirmed"))
            # a plan built before a move: refused by that one scan, after the phrase, before any confirmation
            e.edit(LEAF, "second para ONE SCAN", "second para ONE SCAN 2")
            pid2, sha2, _ = self.make_plan(e)
            os.rename(e.path(TP.KID2), e.path(TP.KID2_MOVED))
            del events[:]
            rc, out, err = self.main(e, "apply", "--plan", pid2, "--sha", sha2, "--items", "M-001")
            self.assertEqual(rc, writeback.EXIT_REFUSED, out + err)
            self.assertIn(self.H, out + err)
            self.assertEqual(events, ["phrase", "scan"])
            self.assertEqual(self.counts(e, pid2), (0, "draft"))
        self.assertEqual(e.writes(), [])


# ============================================================================================ wave 4 prep: L-22, L-20a
def approvals_listing(e):
    root = os.path.join(e.work, "approvals")
    return sorted(os.path.relpath(os.path.join(d, f), root) for d, _dirs, files in os.walk(root) for f in files)


class TestWave4PrepCli(_Base):
    """L-22: `apply --resume` refuses a plan-mode mismatch BEFORE the challenge (and before the y/N of an unstarted
    plan) and leaves state/ byte-identical; `status` lists such a plan with "把模式改回 <mode> 后再 --resume".
    L-20a (ii) on the CLI: `status`, `status --json` and the apply-time scan register a rename with the publisher's
    hold set (pending_deletes on hold, page_state BLOCKED / PARTIAL_CREATE) and (i) any own hold."""

    def make_applying(self, e, *, intent=True):
        """A plan left `applying` by a crashed apply: built in the current mode, confirmed, CAS'd to applying,
        optionally with an unsent `intent` journal row (exactly what the executor writes before a send)."""
        pid, sha12, _ = self.make_plan(e)
        plan, sha = wb_plan.load_plan(pid, work=e.paths, db=e.db)
        m = TP.items_of(plan, "M")[0]
        with wb_state.transaction(e.db):
            e.db.execute("INSERT INTO confirmations(plan_id, item_id, confirmed_at, os_user, tty, challenge_ok, "
                         "phrase_scope, plan_sha256) VALUES(?,?,?,?,?,?,?,?)",
                         (pid, m["item_id"], wb_common.now_iso(), "u", "t", 1, "apply", sha))
        self.assertTrue(wb_state.cas_plan_status(e.db, pid, "draft", "confirmed"))
        self.assertTrue(wb_state.cas_plan_status(e.db, pid, "confirmed", "applying"))
        if intent:
            with wb_state.transaction(e.db):
                e.db.execute("INSERT INTO journal(plan_id, item_id, op, target_id, state, ts) VALUES(?,?,?,?,?,?)",
                             (pid, m["item_id"], "UPDATE", m["ops"][0].get("target_id"), "intent",
                              wb_common.now_iso()))
        return pid, plan

    def refused_resume(self, e, pid, plan_mode):
        """apply --resume on the pty: refused before any prompt; state/ byte-identical, no request, no approval."""
        forced_before = wb_state.get_meta(e.db, "forced_mode")
        e.close_db()
        before, appr = state_digest(e), approvals_listing(e)
        e.fake.clear_requests()
        child = self.spawn(e, "apply", "--resume", pid)
        idx = child.expect(["请原样输入", r"\[y/N\]", pexpect.EOF])
        out = child.before
        if idx != 2:
            child.sendline("确认 wrong")
            child.expect(pexpect.EOF)
            out += child.before
        child.close()
        self.assertEqual(idx, 2, "refused before the challenge / y-N prompt: " + out)
        self.assertEqual(child.exitstatus, writeback.EXIT_REFUSED, out)
        self.assertIn(f"生成时的模式为 {plan_mode}", out)
        if forced_before:
            self.assertIn(f"有效模式已恢复为此计划原来的 {plan_mode}", out)
            self.assertNotIn(f"把模式改回 {plan_mode}", out)
        else:
            self.assertIn(f"用户本人核对后把模式改回 {plan_mode}", out)
        self.assertIn(f"writeback.py apply --resume {pid}", out)
        self.assertEqual(state_digest(e), before, "a refused resume leaves state/ byte-identical")
        self.assertEqual(approvals_listing(e), appr, "no approvals file")
        self.assertEqual(e.fake.requests, [], "no client was built: not a single request")
        self.assertEqual(e.db.execute("SELECT count(*) FROM confirmations WHERE plan_id=? AND phrase_scope='resume'",
                                      (pid,)).fetchone()[0], 0)
        self.assertEqual(wb_plan.plan_row(e.db, pid)["status"], "applying")
        return out

    @unittest.skipIf(pexpect is None, "pexpect not installed")
    def test_resume_refuses_a_mode_change_before_the_challenge_l22(self):
        e = self.env()                                             # mode whitelist
        e.edit(LEAF, "second para", "second para L22")
        pid, plan = self.make_applying(e)
        self.assertEqual(plan["mode"], "whitelist")
        journal = [tuple(r) for r in e.db.execute("SELECT seq, state FROM journal WHERE plan_id=?", (pid,))]
        e.set_cfg(mode="full")                                     # the mode changed since planning
        self.refused_resume(e, pid, "whitelist")
        self.assertEqual([tuple(r) for r in e.db.execute("SELECT seq, state FROM journal WHERE plan_id=?", (pid,))],
                         journal, "the intent row is kept for a later resume")
        # status lists the plan with the hint (text and --json)
        rc, out, err = self.main(e, "status")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"中断的计划 {pid}（applying）生成时的模式为 whitelist，当前有效模式为 full", out)
        self.assertIn(f"writeback.py apply --resume {pid}", out)
        self.assertRegex(out, rf"{pid}  applying .*续执行被拒绝：用户本人核对后把模式改回 whitelist")
        rc, js, err = self.main(e, "status", "--json")
        refusal, = json.loads(js)["resume_refused"]
        self.assertEqual({k: refusal[k] for k in ("plan_id", "plan_mode", "mode")},
                         {"plan_id": pid, "plan_mode": "whitelist", "mode": "full"})
        self.assertIn(f"cd {e.work} && python3 -I ", refusal["hint"])
        self.assertIn(f"writeback.py apply --resume {pid}", refusal["hint"])
        # an applying plan with no journal row at all (crash before the first intent) is not offered the y/N close
        e.set_cfg(mode="whitelist")
        e.edit(LEAF, "second para L22", "second para L22 b")
        pid2, _plan2 = self.make_applying(e, intent=False)
        e.set_cfg(mode="full")
        self.refused_resume(e, pid2, "whitelist")
        # the hint is right: back in mode whitelist the resume reaches its challenge (a wrong phrase changes nothing)
        e.set_cfg(mode="whitelist")
        rc, out, err = self.main(e, "status")
        self.assertNotIn("续执行被拒绝", out)
        self.assertEqual(json.loads(self.main(e, "status", "--json")[1])["resume_refused"], [])
        e.close_db()
        child = self.spawn(e, "apply", "--resume", pid)
        child.expect("请原样输入下面这一行以确认")
        child.expect(r"> ")
        child.sendline("确认 wrong")
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, writeback.EXIT_FAIL, child.before)
        self.assertEqual(e.writes(), [])
        self.assertEqual(wb_plan.plan_row(e.db, pid)["status"], "applying")

    @unittest.skipIf(pexpect is None, "pexpect not installed")
    def test_resume_refuses_full_to_whitelist_and_a_forced_downgrade_l22(self):
        """The other direction (built in mode full, config now whitelist) and an automatic downgrade recorded in
        writeback.db (forced_mode=plan: the effective mode is plan) are refused the same way, before the challenge."""
        e = self.env(cfg={"mode": "full"})
        e.edit(LEAF, "second para", "second para L22 full")
        pid, plan = self.make_applying(e)
        self.assertEqual(plan["mode"], "full")
        e.set_cfg(mode="whitelist")
        self.refused_resume(e, pid, "full")
        e.set_cfg(mode="full")
        wb_state.set_meta(e.db, "forced_mode", "plan")
        wb_state.set_meta(e.db, "forced_mode_reason", "audit-scope: test")
        out = self.refused_resume(e, pid, "full")
        self.assertIn("当前有效模式为 plan", out)
        st = writeback.status_data(e.paths, e.cfg, e.db, scan=False)
        refusal, = st["resume_refused"]
        self.assertEqual(refusal["plan_id"], pid)
        self.assertIn("自动降级记录", refusal["hint"])
        self.assertIn("writeback.py audit-scope", refusal["hint"])
        self.assertIn(f"writeback.py apply --resume {pid}", refusal["hint"])
        self.assertEqual(writeback.resume_mode_mismatch({"mode": "full"}, "full"), None)
        self.assertEqual(writeback.resume_mode_mismatch({}, "full"),
                         "计划没有记录可写入的生成模式，不能续执行（计划保持 applying；需要时联系维护者）")

    def test_status_and_apply_scan_use_the_publisher_hold_set_l20a(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para L20A CLI")
        pid, sha12, _ = self.make_plan(e)                          # built BEFORE the rename: executable
        plan, sha = wb_plan.load_plan(pid, work=e.paths, db=e.db)
        sel = wb_plan.Selection(items=["M-001"])
        items = wb_plan.selected_items(plan, sel)
        os.rename(e.path(TP.REN), e.path(TP.REN_NEW))

        def views():
            rc, out, err = self.main(e, "status")
            self.assertEqual(rc, 0, err)
            js = json.loads(self.main(e, "status", "--json")[1])
            try:
                writeback._refuse_unregistered_renames(e.paths, e.cfg, plan, items, db=e.db)   # the apply-time scan
                refused = None
            except writeback._Fail as exc:
                refused = exc.msg
            return "UNREGISTERED_RENAME" in out, [x["new_rel"] for x in js["unregistered_renames"]], refused
        notice, unreg, refused = views()
        self.assertEqual((notice, unreg), (True, [TP.REN_NEW]))
        self.assertIn(wb_plan.UNREGISTERED_RENAME_HINT, refused)
        copy_old = TP.SYNC_COPY_OLD_NAME

        def sync_copy():
            with open(e.path(TP.REN_NEW), "rb") as fh:
                wb_common.atomic_write(e.path(copy_old), fh.read())
            e.hold_like_refresh("ren", [TP.REN], "sync_conflict_copy", subtree=False)
        sources = {"pending_deletes": (lambda: e.hold_delete("ren", TP.REN)),
                   "BLOCKED": (lambda: TP.set_page_state(e, "ren", "BLOCKED")),
                   "PARTIAL_CREATE": (lambda: TP.set_page_state(e, "ren", "PARTIAL_CREATE")),
                   "sync copy named after the old name": sync_copy}
        for label, setup in sources.items():
            with self.subTest(label):
                setup()
                self.assertEqual(views(), (False, [], None))
                TP.clear_pending(e)
                TP.set_page_state(e, "ren", None)
                TP.clear_auto_holds(e)
                if os.path.exists(e.path(copy_old)):
                    os.remove(e.path(copy_old))
                self.assertEqual(views()[:2], (True, [TP.REN_NEW]))
        # the confirmed path: refused before any confirmation / client / executor while unregistered, reaches the
        # executor once the publisher holds the object (page_state BLOCKED)
        called = []
        with self.assertRaises(writeback._Fail):
            writeback.confirm_and_execute(e.paths, e.cfg, e.db, plan, sha, items, sel, tty="t", os_user="u",
                                          client_factory=lambda: called.append("clients"),
                                          executor=lambda *a, **k: called.append("executor"))
        self.assertEqual(called, [])
        self.assertEqual(e.db.execute("SELECT count(*) FROM confirmations").fetchone()[0], 0)
        TP.set_page_state(e, "ren", "BLOCKED")

        def clients():
            called.append("clients")
            return None, None

        def execute(*a, **k):
            called.append("executor")
            return {"status": "applied", "executed": ["M-001"]}
        res = writeback.confirm_and_execute(e.paths, e.cfg, e.db, plan, sha, items, sel, tty="t", os_user="u",
                                            client_factory=clients, executor=execute)
        self.assertEqual((res["status"], called), ("applied", ["clients", "executor"]))
        self.assertEqual(e.writes(), [])


# ============================================================================================ wave-4 fix round 1
class TestW4FixRound1Cli(_Base):
    def counts(self, e, pid):
        return (e.db.execute("SELECT count(*) FROM confirmations WHERE plan_id=?", (pid,)).fetchone()[0],
                wb_plan.plan_row(e.db, pid)["status"])

    def token(self, e, file_mode=0o600, dir_mode=0o700, name="tok"):
        d = os.path.join(e.tmp, name)
        os.makedirs(d, exist_ok=True)
        os.chmod(d, dir_mode)
        p = os.path.join(d, "writeback.env")
        with open(p, "w") as fh:
            fh.write("NOTION_WRITEBACK_TOKEN=fake-secret-for-the-test\n")
        os.chmod(p, file_mode)
        return p

    def gated(self, e, *args):
        """main() with the human gate passed and the phrase step recorded (never typed: it returns False, so nothing
        is ever confirmed); clients are never built."""
        ev = []

        def phrase(ph, lines):
            ev.append(("phrase", ph))
            return False

        def clients(*a, **k):
            ev.append(("clients",))
            raise AssertionError("no client may be built in these tests")
        with mock.patch.object(writeback, "_human", return_value="pts/test"), \
                mock.patch.object(writeback, "_phrase", side_effect=phrase), \
                mock.patch.object(writeback, "_make_clients", side_effect=clients):
            rc, out, err = self.main(e, *args)
        return rc, out, err, ev

    # ---------------------------------------------------------------- r1-operability-01
    def test_token_file_mode_is_refused_before_the_challenge(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para TOKEN")
        pid, sha12, _ = self.make_plan(e)
        for fm, dm in ((0o644, 0o700), (0o600, 0o755)):
            tok = self.token(e, fm, dm, name=f"tok{fm:o}{dm:o}")
            rc, out, err, ev = self.gated(e, "--token-file", tok, "apply", "--plan", pid, "--sha", sha12, "--items",
                                          "M-001")
            self.assertEqual(rc, writeback.EXIT_REFUSED, out + err)
            self.assertIn("chmod", err)
            self.assertNotIn("Traceback", err)
            self.assertEqual(ev, [])                                   # no challenge shown
            self.assertEqual(self.counts(e, pid), (0, "draft"))        # nothing confirmed, plan unchanged
        ok = self.token(e, name="tok_ok")
        rc, out, err, ev = self.gated(e, "--token-file", ok, "apply", "--plan", pid, "--sha", sha12, "--items",
                                      "M-001")
        self.assertEqual([x[0] for x in ev], ["phrase"], out + err)     # after chmod the same command proceeds
        self.assertEqual(self.counts(e, pid), (0, "draft"))
        self.assertEqual(e.writes(), [])

    def test_client_factory_failure_closes_the_confirmed_plan(self):
        import notion_tokens
        e = self.env()
        e.edit(LEAF, "second para", "second para FACTORY")
        pid, sha12, _ = self.make_plan(e)
        plan, sha = wb_plan.load_plan(pid, work=e.paths, db=e.db)
        sel = wb_plan.selection_from_args(plan, TP._Args(items=["M-001"]))

        def boom():
            raise notion_tokens.TokenError("凭据文件权限为 0o644，必须为 0o600")
        with self.assertRaises(writeback._Fail) as cm:
            writeback.confirm_and_execute(e.paths, e.cfg, e.db, plan, sha, wb_plan.selected_items(plan, sel), sel,
                                          tty="t", os_user="u", client_factory=boom)
        self.assertIn("aborted", cm.exception.msg)
        self.assertEqual(wb_plan.plan_row(e.db, pid)["status"], "aborted")

    # ---------------------------------------------------------------- r1-operability-10
    def test_errors_are_chinese_lines_with_distinct_exit_codes(self):
        e = self.env()
        rc, out, err = run_main(["--work", e.work, "--vault", e.vault, "--base-url", e.base,
                                 "--read-token-file", os.path.join(e.tmp, "missing.env"), "plan"])
        self.assertEqual(rc, writeback.EXIT_REFUSED, err)
        self.assertIn("凭据问题", err)
        self.assertNotIn("Traceback", err)
        e.db.close()
        e._db = None
        with open(os.path.join(e.work, "state", "writeback.db"), "wb") as fh:
            fh.write(b"this is not a database" * 100)
        for suffix in ("-wal", "-shm"):
            try:
                os.unlink(os.path.join(e.work, "state", "writeback.db" + suffix))
            except FileNotFoundError:
                pass
        rc, out, err = self.main(e, "status", "--no-scan")
        self.assertEqual(rc, writeback.EXIT_FAIL, err)
        self.assertIn("状态库错误", err)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), self.assertRaises(SystemExit) as cm:
            writeback.main(["plan", "--bogus"])
        self.assertEqual(cm.exception.code, writeback.EXIT_USAGE)
        self.assertIn("参数错误", buf.getvalue())

    def test_unreachable_members_read_is_named(self):
        e = self.env()
        with mock.patch.object(wb_plan.LiveReader, "members", return_value=(None, "NotionAPIError: 503")):
            rc, out, err = self.main(e, "plan")
        self.assertEqual(rc, 0, err)
        self.assertIn("成员数读取失败（NotionAPIError: 503）", out)

    # ---------------------------------------------------------------- r1-never-unapproved-05
    def test_vault_is_required_and_must_be_the_plans_vault(self):
        import wb_apply
        e = self.env()
        self.assertIsNone(wb_apply.Paths(e.work, None).vault)
        e.edit(LEAF, "second para", "second para VAULT")
        pid, sha12, _ = self.make_plan(e)
        tok = self.token(e)
        no_vault = ["--work", e.work, "--base-url", e.base, "--read-token-file", e.read_token_file, "--token-file", tok]
        with mock.patch.object(writeback, "_human", return_value="pts/test"), \
                mock.patch.object(writeback, "_phrase", side_effect=AssertionError("no phrase")):
            for cmd in (["apply", "--plan", pid, "--sha", sha12, "--items", "M-001"], ["rollback", "--plan", pid]):
                rc, out, err = run_main(no_vault + cmd)
                self.assertEqual(rc, writeback.EXIT_REFUSED, (cmd, out, err))
                self.assertIn("--vault", err)
            other = os.path.join(e.tmp, "other-vault")
            os.makedirs(other)
            rc, out, err = run_main(no_vault + ["--vault", other, "apply", "--plan", pid, "--sha", sha12, "--items",
                                                "M-001"])
            self.assertEqual(rc, writeback.EXIT_REFUSED, out + err)
            self.assertIn("生成的", err)
        self.assertEqual(self.counts(e, pid), (0, "draft"))
        self.assertEqual(e.writes(), [])

    # ---------------------------------------------------------------- r1-contract-consistency-02
    def test_resume_refused_before_the_challenge_while_a_rename_is_unregistered(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para RESUME")
        pid, sha12, _ = self.make_plan(e)
        with wb_state.transaction(e.db):
            e.db.execute("UPDATE plans SET status='applying' WHERE plan_id=?", (pid,))
            e.db.execute("INSERT INTO journal(plan_id, item_id, op, target_id, state, ts) VALUES(?,?,?,?,?,?)",
                         (pid, "M-001", "UPDATE", e.ids["leaf"], "intent", wb_common.now_iso()))
        os.rename(e.path(TP.KID2), e.path(TP.KID2_MOVED))
        tok = self.token(e)
        rc, out, err, ev = self.gated(e, "--token-file", tok, "apply", "--resume", pid)
        self.assertEqual(rc, writeback.EXIT_REFUSED, out + err)
        self.assertIn(wb_plan.UNREGISTERED_RENAME, err)
        self.assertIn(f"apply --resume {pid}", err)
        self.assertEqual(ev, [])
        self.assertEqual(self.counts(e, pid), (0, "applying"))
        os.rename(e.path(TP.KID2_MOVED), e.path(TP.KID2))       # no rename: the resume reaches the challenge
        rc, out, err, ev = self.gated(e, "--token-file", tok, "apply", "--resume", pid)
        self.assertEqual([x[0] for x in ev], ["phrase"], out + err)

    # ---------------------------------------------------------------- r1-user-journey-02 / r1-operability-06
    def test_hold_refusal_shows_affected_note_reason_and_recovery_in_plan_and_show(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para LOCAL")
        e.hold_like_refresh("leaf", [LEAF], "sync_conflict_copy", subtree=False)
        pid, _, plan_output = self.make_plan(e)
        rc, preview, err = self.main(e, "show", "--plan", pid)
        self.assertEqual(rc, 0, err)
        with open(os.path.join(e.work, "reports", "writeback", pid, "plan.json"), encoding="utf-8") as fh:
            plan = json.load(fh)
        self.assertFalse(any(item.get("rel_path") == LEAF for item in plan["items"]))
        self.assertEqual([r["class"] for r in plan["refused"] if r["rel_path"] == LEAF], ["AUTO_SYNC_HOLD"])
        for name, output in (("plan", plan_output), ("show", preview)):
            with self.subTest(command=name):
                self.assertIn(LEAF, output)
                self.assertIn("因自动同步暂停而拒绝", output)
                self.assertIn("自动同步已暂停", output)
                self.assertIn("sync_conflict_copy", output)
                self.assertIn("下一轮", output)

    def test_plan_prints_reasons_and_a_fitting_next_step(self):
        e = self.env()
        rc, out, err = self.main(e, "plan")                         # nothing to do
        self.assertIn("没有可执行项", out)
        self.assertNotIn("apply --plan", out)
        e.edit(LEAF, "second para", "second para NEXT")
        e.set_cfg(mode="plan")
        rc, out, err = self.main(e, "plan")
        self.assertIn("本计划只是预览（mode=plan）", out)
        self.assertIn("mode 改为 whitelist", out)
        self.assertNotIn("apply --plan", out)
        e.set_cfg(mode="whitelist")
        settling = {"running": True, "started_at": wb_common.now_iso()}
        with mock.patch.object(wb_plan, "obsidian_status", return_value=settling):
            rc, out, err = run_main(e.argv + ["plan"])
        self.assertIn("启动不足", out)
        self.assertIn(f"重新运行 cd {e.work} && python3 -I ", out)
        self.assertIn("writeback.py plan", out)
        self.assertNotIn("apply --plan", out)
        rc, out, err = self.main(e, "plan")
        self.assertRegex(out, r"apply --plan \S+ --sha [0-9a-f]{12} --items M-001")

    def test_plan_names_delete_and_resolve_selectors(self):
        e = self.env()
        os.unlink(e.path("General/Home/Doomed.md"))
        e.hold_delete("del", "General/Home/Doomed.md")
        e.edit(KID, "kid body", "kid body LOCAL")
        e.remote_text("kid", 0, "kid body REMOTE")
        rc, out, err = self.main(e, "plan")
        self.assertIn("--delete D-001", out)
        self.assertNotIn("--items D-001", out)
        self.assertRegex(out, r"resolve --plan \S+ --item C-001 --take notion")

    # ---------------------------------------------------------------- r1-user-journey-04 / -15 / -01
    def test_partial_publish_pause_in_status_and_plan_never_invites_same_round_approval(self):
        e = self.env()
        with open(os.path.join(e.work, "state", "paused_runs.json"), "w", encoding="utf-8") as fh:
            json.dump({"latest": "R4", "runs": {"R4": {"status": "paused", "reasons": ["apply_race: SKIP_MOVE_RACE"]}}}, fh)
        for command in ("status", "plan"):
            rc, text, err = self.main(e, command)
            with self.subTest(command=command):
                self.assertEqual(rc, 0, err)
                self.assertNotIn("--approve-run R4", text)
                self.assertIn("reports/R4/publish_journal.jsonl", text)

    def test_partial_publish_pause_reads_persisted_flag_and_all_reasons(self):
        e = self.env()
        for fields in ({"partially_applied": True, "reasons": ["其他原因"]},
                       {"reasons": ["其他原因"] * 5 + ["apply_race: late"]}):
            entry = dict(status="paused", **fields)
            with open(os.path.join(e.work, "state", "paused_runs.json"), "w", encoding="utf-8") as fh:
                json.dump({"runs": {"R4": entry}}, fh)
            rc, text, err = self.main(e, "status")
            with self.subTest(fields=fields):
                self.assertEqual(rc, 0, err)
                self.assertNotIn("--approve-run R4", text)
                self.assertIn("reports/R4/publish_journal.jsonl", text)

    def test_status_shows_paused_rounds_and_reads_well(self):
        e = self.env()
        with open(os.path.join(e.work, "state", "paused_runs.json"), "w", encoding="utf-8") as fh:
            json.dump({"latest": "R4", "runs": {"R4": {"status": "paused", "paused_at": "2026-11-20T10:00:00+07:00",
                                                        "reasons": ["source_mass_change: 1/6 = 16.7% ≥ 10%"]}}}, fh)
        e.edit(LEAF, "second para", "second para S")
        for _ in range(8):
            pid, _sha12, out = self.make_plan(e)
        self.assertIn("refresh.py --approve-run R4", out)            # plan warns too
        with wb_state.transaction(e.db):
            e.db.execute("INSERT INTO rejections(notion_id, rel_path, l_sha, item_category, reason, created_at, plan_id, "
                         "active) VALUES(?,?,?,?,?,?,?,1)", (e.ids["leaf"], LEAF, "0" * 64, "M", "not now",
                                                             wb_common.now_iso(), pid))
            wb_state.set_meta(e.db, "forced_mode", "plan")
            wb_state.set_meta(e.db, "forced_mode_reason", "audit-scope：多出 0 个对象，不可达 1 个")
        rc, out, err = self.main(e, "status")
        self.assertEqual(rc, 0, err)
        self.assertIn("R4", out)
        self.assertIn("--approve-run R4", out)
        self.assertIn("--discard-run R4", out)
        plan_lines = [ln for ln in out.split("\n") if re.match(r"^wb-\d", ln)]
        self.assertLessEqual(len(plan_lines), 1 + writeback.STATUS_DECIDED_PLANS)
        self.assertIn("更早的已结束计划未列出", out)
        last = next(ln for ln in out.split("\n") if ln.startswith("上次计划"))
        self.assertNotIn("{", last)
        self.assertIn("当前不生效", out)                              # stale rejection marked, still listed
        self.assertIn("成员数 members：1", out)                       # from the latest plan's live read
        self.assertIn("whitelist_page_ids", out)                      # audit-scope downgrade: how to recover
        rc, out, err = self.main(e, "status", "--json")
        st = json.loads(out)
        self.assertEqual([r["run_id"] for r in st["paused_rounds"]], ["R4"])

    # ---------------------------------------------------------------- r1-never-unapproved-07
    def test_show_renders_the_verified_plan_not_preview_md(self):
        e = self.env()
        e.edit(LEAF, "second para", "second para REAL")
        pid, _sha12, _ = self.make_plan(e)
        p = os.path.join(e.work, "reports", "writeback", pid, "preview.md")
        os.chmod(p, 0o660)
        with open(p, encoding="utf-8") as fh:
            t = fh.read()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(t.replace("second para REAL", "second para HARMLESS"))
        rc, out, err = self.main(e, "show", "--plan", pid)
        self.assertEqual(rc, 0, err)
        self.assertIn("second para REAL", out)
        self.assertNotIn("second para HARMLESS", out)
        self.assertIn("不一致", out)

    # ---------------------------------------------------------------- r1-user-journey-03
    def test_every_result_status_gets_a_chinese_verdict(self):
        def show(res, kind="apply"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = writeback._print_result(res, kind)
            return rc, buf.getvalue()
        rc, out = show({"plan_id": "wb-x", "status": "aborted", "reason": "预检不一致", "requests": 0,
                        "skipped": [{"item_id": "M-001", "status": "skipped", "reason": "块 72280c8f 自计划后已改变"}],
                        "result_md": "/r/result.md"})
        self.assertEqual(rc, writeback.EXIT_FAIL)
        self.assertIn("已中止（aborted）", out)
        self.assertIn("块 72280c8f 自计划后已改变", out)
        self.assertIn("重新运行", out)
        self.assertIn("完整结果：/r/result.md", out)
        rc, out = show({"plan_id": "wb-x", "status": "refused", "reason": "audit-scope 不通过"})
        self.assertEqual(rc, writeback.EXIT_REFUSED)
        self.assertIn("已拒绝", out)
        rc, out = show({"plan_id": "wb-x", "status": "applied", "needs_manual": [{"seq": 3}]}, "rollback")
        self.assertEqual(rc, writeback.EXIT_OK)
        self.assertIn("回退完成", out)
        self.assertIn("1 项需人工检查", out)
        self.assertNotIn("完成，可验收", out)
        rc, out = show({"plan_id": "wb-x", "status": "applied"})
        self.assertIn("完成，可验收（applied）", out)

    # ---------------------------------------------------------------- r1-user-journey-12
    def test_help_shows_runnable_commands(self):
        out = subprocess.run([sys.executable, "-I", "-B", WB, "--help"], capture_output=True, text=True, env=clean_env())
        self.assertIn("python3 -I tools/writeback.py", out.stdout)
        self.assertIn("python3 -I tools/refresh.py --mode incremental", out.stdout)
        self.assertIn('--approve-run "${RUN_ID:?', out.stdout)
        self.assertIn('--discard-run "${RUN_ID:?', out.stdout)
        self.assertIn('--release-hold "${NOTION_ID:?', out.stdout)


if __name__ == "__main__":
    unittest.main()
