"""Actual planning output with a loopback-only fixture, plus pure mode/refusal and contract checks."""
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
for directory in (HERE, HERE.parent):
    sys.path.insert(0, str(directory))
import test_wb_plan as fixtures
import wb_config
import wb_state
import writeback
from test_writeback_cli import run_main


class ModeHelp(unittest.TestCase):
    def test_audit_downgrade_plan_cli_explains_real_remedy(self):
        env = fixtures.Env(self)
        wb_config.record_downgrade(env.db, "plan", "audit-scope：范围多出一页")
        env.edit(fixtures.LEAF, "second para", "second para preview")
        argv = ["--work", env.work, "--vault", env.vault, "plan", "--scope", "whitelist"]
        with mock.patch.object(writeback, "_read_client", return_value=env.ro):
            rc, output, err = run_main(argv)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("把 mode 改为 whitelist", output)
        for fragment in ("audit-scope：范围多出一页", "共享", "writeback.py audit-scope", "用户本人", "writeback.py plan"):
            self.assertIn(fragment, output)

    def test_user_plan_and_missing_read_confirmation_have_different_recoveries(self):
        cfg = dict(wb_config.DEFAULTS, mode="whitelist")
        with self.assertRaises(writeback._Fail) as caught:
            writeback._precheck_write_mode(cfg, "写入")
        msg = caught.exception.msg
        self.assertEqual(caught.exception.code, 2)
        self.assertNotIn("把 mode 改为 whitelist", msg)
        for term in ("read_integration_readonly_confirmed_at", "只读", "记录", "写请求", "writeback.py status"):
            self.assertIn(term, msg)
        with self.assertRaises(writeback._Fail) as wanted:
            writeback._precheck_write_mode(dict(cfg, mode="plan"), "写入")
        self.assertIn("把 mode 改为 whitelist", wanted.exception.msg)
        self.assertIn("whitelist-check", wanted.exception.msg)

    def test_all_applicable_caps_report_their_remedies_without_raising_mode(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE wb_meta(key TEXT PRIMARY KEY, value TEXT)")
        wb_config.record_downgrade(db, "plan", "audit-scope mismatch")
        cfg = dict(wb_config.DEFAULTS, mode="full", whitelist_scope={"sample": "subtree"})
        with self.assertRaises(writeback._Fail) as caught:
            writeback._require_write_mode(cfg, db, "写入")
        self.assertEqual(caught.exception.code, 2)
        msg = caught.exception.msg
        for term in ("subtree", "叶子", "read_integration_readonly_confirmed_at", "audit-scope mismatch",
                     "writeback.py audit-scope", "writeback.py status"):
            self.assertIn(term, msg)
        self.assertNotIn("把 mode 改为 whitelist", msg)
        self.assertEqual(wb_state.get_meta(db, "forced_mode"), "plan")

    def test_interrupted_plan_with_forced_downgrade_preserves_record_and_offers_original_id(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE wb_meta(key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO wb_meta VALUES ('forced_mode', 'plan')")
        db.execute("INSERT INTO wb_meta VALUES ('forced_mode_reason', 'audit-scope mismatch')")
        cfg = dict(wb_config.DEFAULTS, mode="whitelist", read_integration_readonly_confirmed_at="historical record")
        with self.assertRaises(writeback._Fail) as caught:
            writeback._refuse_resume_mode(cfg, db, {"mode": "whitelist", "plan_id": "wb-original"})
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("writeback.py audit-scope", caught.exception.msg)
        self.assertIn("apply --resume wb-original", caught.exception.msg)
        self.assertNotIn("把模式改回 whitelist", caught.exception.msg)
        self.assertEqual(wb_state.get_meta(db, "forced_mode"), "plan")

    def test_binding_contract_records_both_decisions(self):
        doc = (HERE.parents[1] / "_resume_20261118/impl/IMPL_CONTRACT.md").read_text()
        l22 = doc.split("- L-22 (", 1)[1].split("- L-23 (", 1)[0]
        l23 = doc.split("- L-23 (", 1)[1].split("- L-20a (", 1)[0]
        self.assertNotIn("NOT implemented", l22)
        for entry in (l22, l23):
            self.assertIn("2026-11-21", entry)
        self.assertIn("refused[]", l23)
        self.assertIn("applying", l22)
