#!/usr/bin/env python3
"""Tests for wb_common, wb_config, wb_state and wb_locks (IMPL_CONTRACT §5.1-§5.3, §5.5).

Run from the task root:
  python3 -I -B -m unittest discover -s tools/tests -p 'test_wb_state.py' -v
All fixtures live in tempfile.mkdtemp(); the sandbox export.db is only read (immutable=1) and copied.
"""
import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import wb_common  # noqa: E402
import wb_config  # noqa: E402
import wb_locks  # noqa: E402
import wb_state  # noqa: E402

PY = [sys.executable, "-I", "-B"]
NID = "87aa3350-70da-4998-a141-9474377ca858"
NID2 = "f3d821bd-e228-4f28-a395-a8d89d17eb9d"

# Golden column lists: WRITEBACK_SPEC §3 + IMPL_CONTRACT §5.3 additions (+ documented extras).
GOLDEN = {
    "base_history": ["notion_id", "seq", "rel_path", "sha256", "page_json_sha", "renderer_version", "run_id",
                     "origin", "seed"],
    "block_lines": ["notion_id", "base_sha", "seq", "renderer_version", "block_id", "owner_page_id",
                    "parent_block_id", "ord", "depth", "type", "own_start", "own_end", "line_start", "line_end",
                    "prefix", "zero_line", "klass", "x_reason", "json_sha"],
    "plans": ["plan_id", "created_at", "expires_at", "plan_sha256", "status", "scope", "counts_json",
              "status_reason"],
    "plan_items": ["plan_id", "item_id", "notion_id", "rel_path", "category", "ops_json", "risk", "decision",
                   "reason", "l_sha", "r_json_sha", "verify_status", "binds"],
    "confirmations": ["id", "plan_id", "item_id", "confirmed_at", "os_user", "tty", "challenge_ok", "phrase_scope",
                      "plan_sha256"],
    "journal": ["seq", "plan_id", "item_id", "op", "target_id", "state", "before_json", "request_json",
                "expected_after_json", "response_status", "committed_ids", "after_json", "ts"],
    "pending_deletes": ["notion_id", "rel_path", "subtree_json", "detected_run", "detected_at", "status",
                        "ttl_until"],
    "name_overrides": ["notion_id", "old_rel", "local_rel", "notion_title_at_detect", "since", "status"],
    "page_state": ["notion_id", "state", "reason", "since"],
    "creation_state": ["ts", "members", "plan", "state", "source"],
    "block_budget": ["ts", "plan_id", "item_id", "created_blocks"],
    "trash_archive": ["notion_id", "archived_at", "reason", "last_snapshot", "object_json", "blocks_json",
                      "inbound_refs_json"],
    "rejections": ["id", "notion_id", "rel_path", "l_sha", "item_category", "reason", "created_at", "plan_id",
                   "active", "os_user", "restore_notion"],
    "wb_meta": ["key", "value"],
}


def sha(b):
    return hashlib.sha256(b).hexdigest()


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_wb_state_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def p(self, *parts):
        return os.path.join(self.tmp, *parts)


# ---------------------------------------------------------------- wb_common
class CommonTests(TmpCase):
    def test_paths_and_constants(self):
        self.assertEqual(wb_common.WORK, os.path.dirname(TOOLS))
        self.assertEqual(wb_common.PROD_VAULT, "/data120/Obsidian/Godcreate")
        for k in ("STATE", "REPORTS", "CONFIG_DIR", "STAGING", "SNAPSHOTS"):
            self.assertTrue(getattr(wb_common, k).startswith(wb_common.WORK + os.sep), k)
        self.assertEqual(wb_common.CONFIG_DIR, os.path.join(wb_common.WORK, "config"))

    def test_hash_json_time(self):
        self.assertEqual(wb_common.sha256_bytes(b"abc"), sha(b"abc"))
        f = self.p("f.bin")
        with open(f, "wb") as fh:
            fh.write(b"x" * (3 << 20))
        self.assertEqual(wb_common.sha256_file(f), sha(b"x" * (3 << 20)))
        self.assertEqual(wb_common.canon_json({"b": 1, "a": ["é", None]}), '{"a":["é",null],"b":1}')
        t = wb_common.now_iso()
        self.assertRegex(t, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")

    def test_norm_notion_id(self):
        self.assertEqual(wb_common.norm_notion_id(NID.replace("-", "").upper()), NID)
        self.assertEqual(wb_common.norm_notion_id(NID), NID)
        for bad in ("", "xyz", NID + "0", None, 5):
            with self.assertRaises(ValueError):
                wb_common.norm_notion_id(bad)

    def test_split_note_exporter_shape(self):
        text = "---\nnotion_id: abc\nnotion_title: T\n---\n\nFirst line\n\n- item\n"
        fm, body, first = wb_common.split_note(text)
        self.assertEqual(fm, "notion_id: abc\nnotion_title: T")
        self.assertEqual(body, "\nFirst line\n\n- item\n")
        self.assertEqual(first, 5)
        self.assertEqual(text.split("\n")[first], "First line")  # line 6 (1-based) = index 5
        self.assertEqual(wb_common.parse_frontmatter(fm), {"notion_id": "abc", "notion_title": "T"})

    def test_split_note_variants(self):
        self.assertEqual(wb_common.split_note("no fm\n---\n"), (None, "no fm\n---\n", 1))
        self.assertEqual(wb_common.split_note(""), (None, "", 1))
        fm, body, first = wb_common.split_note("---\r\nk: v\r\n...\r\nbody")
        self.assertEqual((fm, body, first), ("k: v", "body", 4))
        fm, body, first = wb_common.split_note("---\n---\n")
        self.assertEqual((fm, body, first), ("", "", 3))

    def test_split_note_unterminated_is_an_error_not_no_frontmatter(self):
        # review finding: a note whose closing '---' was deleted must not look like "no frontmatter"
        for text in ("---\nnotion_id: %s\nnotion_title: T\n\nBody text\n" % NID,
                     "---\r\nnotion_id: %s\r\n" % NID, "---\nk: v\nnever closed\n", "---", "---\n"):
            with self.assertRaises(wb_common.FrontmatterError, msg=repr(text)) as cm:
                wb_common.split_note(text)
            self.assertIn("unterminated frontmatter", str(cm.exception))  # verify_vault's wording
            self.assertIsInstance(cm.exception, ValueError)

    def test_parse_frontmatter(self):
        self.assertEqual(wb_common.parse_frontmatter(None), {})
        self.assertEqual(wb_common.parse_frontmatter("  \n"), {})
        with self.assertRaises(wb_common.FrontmatterError):
            wb_common.parse_frontmatter("a: [unclosed")
        with self.assertRaises(wb_common.FrontmatterError):
            wb_common.parse_frontmatter("- just\n- a list")
        self.assertIsInstance(wb_common.FrontmatterError("x"), ValueError)

    def test_atomic_write(self):
        f = self.p("d", "sub", "note.md")
        wb_common.atomic_write(f, b"one")
        with open(f, "rb") as fh:
            self.assertEqual(fh.read(), b"one")
        os.chmod(f, 0o640)
        wb_common.atomic_write(f, b"two")
        with open(f, "rb") as fh:
            self.assertEqual(fh.read(), b"two")
        self.assertEqual(os.stat(f).st_mode & 0o777, 0o640)
        self.assertEqual(sorted(os.listdir(self.p("d", "sub"))), ["note.md"])  # no tmp left behind
        with self.assertRaises(TypeError):
            wb_common.atomic_write(f, "text")

    def test_safe_join(self):
        root = self.p("vault")
        os.makedirs(os.path.join(root, "a"))
        self.assertEqual(wb_common.safe_join(root, "a/b.md"), os.path.join(root, "a", "b.md"))
        self.assertEqual(wb_common.safe_join(root + "/", "a/./b.md"), os.path.join(root, "a", "b.md"))
        for bad in ("../x.md", "a/../../x.md", "/etc/passwd", "", "a/\x00b"):
            with self.assertRaises(wb_common.UnsafePath, msg=bad):
                wb_common.safe_join(root, bad)
        outside = self.p("outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(root, "link"))
        with self.assertRaises(wb_common.UnsafePath):
            wb_common.safe_join(root, "link/x.md")
        os.symlink(os.path.join(outside, "f"), os.path.join(root, "a", "fl.md"))
        self.assertTrue(wb_common.safe_join(root, "a/fl.md").endswith("fl.md"))  # publish_vault semantics
        with self.assertRaises(wb_common.UnsafePath):
            wb_common.safe_join(root, "a/fl.md", allow_final_symlink=False)
        self.assertIsInstance(wb_common.UnsafePath("x"), ValueError)

    def test_backup_copy_never_overwrites(self):
        src = self.p("v", "n.md")
        os.makedirs(os.path.dirname(src))
        with open(src, "wb") as fh:
            fh.write(b"v1")
        broot = self.p("backup")
        b1 = wb_common.backup_copy(src, broot, "General/n.md")
        self.assertEqual(b1, os.path.join(broot, "General", "n.md"))
        self.assertEqual(wb_common.backup_copy(src, broot, "General/n.md"), b1)  # same content: reuse
        with open(src, "wb") as fh:
            fh.write(b"v2")
        b2 = wb_common.backup_copy(src, broot, "General/n.md")
        self.assertEqual(b2, b1 + ".1")
        with open(b1, "rb") as fh:
            self.assertEqual(fh.read(), b"v1")
        with open(b2, "rb") as fh:
            self.assertEqual(fh.read(), b"v2")
        with self.assertRaises(wb_common.UnsafePath):
            wb_common.backup_copy(src, broot, "../escape.md")

    def test_backup_copy_refuses_root_itself(self):
        src = self.p("vault", "n.md")
        os.makedirs(os.path.dirname(src))
        with open(src, "wb") as fh:
            fh.write(b"x")
        broot = self.p("backup")
        os.makedirs(broot)
        for rel in (".", "a/..", "General/../.", "./"):
            with self.assertRaises(wb_common.UnsafePath, msg=rel):
                wb_common.backup_copy(src, broot, rel)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["backup", "vault"])  # no '<backup_root>.1' sibling
        self.assertEqual(os.listdir(broot), [])

    def test_sync_conflict_re(self):
        r = wb_common.SYNC_CONFLICT_RE
        self.assertTrue(r.search("General/Note (Conflicted copy 2026-11-17 101233).md"))
        self.assertTrue(r.search("A (Conflicted copy MacBook 202611171012).md"))
        self.assertFalse(r.search("A (Conflicted copy).md"))  # contract regex needs text after "copy "
        self.assertFalse(r.search("General/Note.md"))
        self.assertFalse(r.search("Note (Conflicted copy 1).md.bak"))
        self.assertEqual(r.pattern, r" \(Conflicted copy [^)]*\)\.md$")

    def test_wb_error_defined_in_wb_common(self):
        # IMPL_CONTRACT §5.13.1 (binding): load_plan "raises `wb_common.WbError`" (r1-contract-consistency-07).
        self.assertTrue(hasattr(wb_common, "WbError"), "wb_common.WbError missing")
        E = wb_common.WbError
        self.assertTrue(issubclass(E, Exception))
        # Same catch semantics as wb_plan's former private fallback (a plain Exception): existing
        # `except ValueError` / `except OSError` handlers must not start swallowing write-back errors.
        self.assertFalse(issubclass(E, (ValueError, OSError, LookupError)))
        self.assertEqual(str(E("计划 x 未登记")), "计划 x 未登记")

    def test_load_plan_raises_wb_common_wb_error(self):
        # A caller that follows §5.13.1 and catches wb_common.WbError must catch every load_plan refusal.
        import wb_plan
        self.assertIs(wb_plan.WbError, wb_common.WbError)
        work = self.p("work")
        db = wb_state.connect(os.path.join(work, "state", "writeback.db"))
        self.addCleanup(db.close)
        pid = "wb-20261121T1200-abcd"
        with self.assertRaises(wb_common.WbError):  # invalid plan id
            wb_plan.load_plan("bogus", work=work, db=db)
        with self.assertRaises(wb_common.WbError):  # not registered in writeback.db
            wb_plan.load_plan(pid, work=work, db=db)
        db.execute("INSERT INTO plans(plan_id, plan_sha256, status) VALUES (?,?,?)", (pid, sha(b"other"), "draft"))
        with self.assertRaises(wb_common.WbError):  # plan.json missing
            wb_plan.load_plan(pid, work=work, db=db)
        pj = os.path.join(work, "reports", "writeback", pid, "plan.json")
        os.makedirs(os.path.dirname(pj))
        data = json.dumps({"plan_id": pid}).encode()
        with open(pj, "wb") as fh:
            fh.write(data)
        with self.assertRaises(wb_common.WbError):  # bytes do not hash to plans.plan_sha256
            wb_plan.load_plan(pid, work=work, db=db)
        db.execute("UPDATE plans SET plan_sha256=? WHERE plan_id=?", (sha(data), pid))
        self.assertEqual(wb_plan.load_plan(pid, work=work, db=db), ({"plan_id": pid}, sha(data)))


# ---------------------------------------------------------------- wb_config
class ConfigTests(TmpCase):
    def write(self, obj):
        f = self.p("writeback.json")
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        return f

    def test_missing_file_is_off(self):
        cfg = wb_config.load(self.p("missing.json"))
        self.assertEqual(cfg["mode"], "off")
        self.assertEqual(cfg, wb_config.defaults())
        for k, v in {"send_silent_notifications": True, "test_page_id": None, "whitelist_page_ids": [],
                     "whitelist_database_ids": [], "automation_free_database_ids": [], "allow_block_trash": False,
                     "allow_page_move": False, "max_pages_per_plan": 50, "rate_limit_total_rps": 2.5,
                     "plan_ttl_hours": 24, "whitelist_scope": {}}.items():
            self.assertEqual(cfg[k], v, k)

    def test_repo_default_file(self):
        f = os.path.join(wb_common.CONFIG_DIR, "writeback.json")
        self.assertTrue(os.path.exists(f))
        self.assertEqual(wb_config.load(f), wb_config.defaults())
        self.assertEqual(wb_config.load(f)["mode"], "off")

    def test_merge_and_normalise(self):
        cfg = wb_config.load(self.write({"mode": "plan", "whitelist_page_ids": [NID.replace("-", "").upper(), NID],
                                         "whitelist_scope": {NID2.replace("-", ""): "page"}, "test_page_id": NID}))
        self.assertEqual(cfg["mode"], "plan")
        self.assertEqual(cfg["whitelist_page_ids"], [NID])
        self.assertEqual(cfg["whitelist_scope"], {NID2: "page"})
        self.assertEqual(cfg["max_ops_per_page"], 50)

    def test_invalid(self):
        for bad in ({"mode": "fulll"}, {"unknown_key": 1}, {"mode": 1}, {"workspace_members": 0},
                    {"workspace_members": True}, {"max_ops_per_page": "50"}, {"max_ops_per_page": -1},
                    {"allow_block_trash": 1}, {"whitelist_page_ids": "x"}, {"whitelist_page_ids": ["nope"]},
                    {"whitelist_scope": {NID: "all"}}, {"whitelist_scope": []}, {"test_page_id": "bad"},
                    {"rate_limit_total_rps": 0}, {"source_change_pause_ratio": 1.5},
                    {"rate_limit_writeback_during_refresh_rps": 3.0}, {"send_silent_notifications": "yes"},
                    {"read_integration_readonly_confirmed_at": 5},
                    # review finding: non-finite / unbounded numbers
                    {"rate_limit_total_rps": float("inf"), "rate_limit_writeback_during_refresh_rps": float("inf"),
                     "plan_ttl_hours": float("inf")},
                    {"plan_ttl_hours": float("inf")}, {"plan_ttl_hours": float("nan")},
                    {"source_change_pause_ratio": float("nan")}, {"rate_limit_total_rps": 3.5},
                    {"plan_ttl_hours": 169}, {"plan_ttl_hours": 10 ** 400},
                    # review finding: the same page id in two spellings with different scopes
                    {"whitelist_scope": {NID.replace("-", "").upper(): "subtree", NID: "page"}},
                    {"whitelist_scope": {NID: "page", NID.upper(): "subtree"}}):
            with self.assertRaises(wb_config.ConfigError, msg=repr(bad)):
                wb_config.load(self.write(bad))
        # literal JSON constants and overflowing literals
        for text in ('{"plan_ttl_hours": Infinity}', '{"rate_limit_total_rps": NaN}', '{"plan_ttl_hours": -Infinity}',
                     '{"plan_ttl_hours": 1e999}',
                     # duplicate keys: json.load would silently keep the last one
                     '{"mode": "off", "mode": "full"}',
                     '{"whitelist_scope": {"%s": "subtree", "%s": "page"}}' % (NID, NID)):
            f = self.p("raw.json")
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(text)
            with self.assertRaises(wb_config.ConfigError, msg=text):
                wb_config.load(f)
        # boundaries still accepted; one id in two spellings with the SAME scope is fine
        cfg = wb_config.load(self.write({"rate_limit_total_rps": 3, "rate_limit_writeback_during_refresh_rps": 3.0,
                                         "plan_ttl_hours": 168,
                                         "whitelist_scope": {NID.replace("-", ""): "subtree", NID: "subtree"}}))
        self.assertEqual(cfg["whitelist_scope"], {NID: "subtree"})
        self.assertEqual(cfg["plan_ttl_hours"], 168)
        f = self.p("broken.json")
        with open(f, "w") as fh:
            fh.write("{not json")
        with self.assertRaises(wb_config.ConfigError):
            wb_config.load(f)
        with open(f, "w") as fh:
            fh.write("[]")
        with self.assertRaises(wb_config.ConfigError):
            wb_config.load(f)

    def test_effective_mode(self):
        ok = "2026-11-17T18:30:00+07:00"
        db = wb_state.connect(self.p("st", "writeback.db"))
        self.addCleanup(db.close)
        cfg = wb_config.load(self.write({"mode": "whitelist", "read_integration_readonly_confirmed_at": ok}))
        self.assertEqual(wb_config.effective_mode(cfg, db), "whitelist")
        # D-21: subtree scope caps at plan
        cfg2 = wb_config.load(self.write({"mode": "full", "read_integration_readonly_confirmed_at": ok,
                                          "whitelist_scope": {NID: "subtree"}}))
        self.assertEqual(wb_config.effective_mode(cfg2, db), "plan")
        # WB §2.2: read integration not confirmed read-only -> plan
        cfg3 = wb_config.load(self.write({"mode": "whitelist"}))
        info = wb_config.effective_mode_info(cfg3, db)
        self.assertEqual(info["mode"], "plan")
        self.assertEqual(info["configured"], "whitelist")
        self.assertTrue(info["reasons"])
        # off/plan are never raised
        self.assertEqual(wb_config.effective_mode(wb_config.load(self.write({"mode": "off"})), db), "off")
        self.assertTrue(wb_config.record_downgrade(db, "plan", "audit-scope mismatch"))
        self.assertEqual(wb_state.get_meta(db, "forced_mode"), "plan")
        self.assertEqual(wb_config.effective_mode(cfg, db), "plan")
        self.assertIn("audit-scope mismatch", " ".join(wb_config.effective_mode_info(cfg, db)["reasons"]))
        # a later "downgrade" to a higher mode never raises the recorded one
        self.assertFalse(wb_config.record_downgrade(db, "full", "x"))
        self.assertEqual(wb_state.get_meta(db, "forced_mode"), "plan")
        self.assertTrue(wb_config.record_downgrade(db, "off", "stop"))
        self.assertEqual(wb_config.effective_mode(cfg, db), "off")
        with self.assertRaises(ValueError):
            wb_config.record_downgrade(db, "max", "x")

    def test_effective_mode_fails_closed(self):
        # review finding: an unreadable downgrade state must never count as "no downgrade"
        ok = "2026-11-17T18:30:00+07:00"
        cfg = wb_config.load(self.write({"mode": "whitelist", "read_integration_readonly_confirmed_at": ok}))
        full = wb_config.load(self.write({"mode": "full", "read_integration_readonly_confirmed_at": ok}))
        off = wb_config.load(self.write({"mode": "off"}))
        # db is a required argument (contract: effective_mode(cfg, db))
        for fn in (wb_config.effective_mode, wb_config.effective_mode_info):
            self.assertIs(inspect.signature(fn).parameters["db"].default, inspect.Parameter.empty)
        with self.assertRaises(TypeError):
            wb_config.effective_mode(cfg)  # pylint: disable=no-value-for-parameter
        # the reviewer's repro: downgrade recorded, then the connection is closed
        db = wb_state.connect(self.p("st", "writeback.db"))
        wb_config.record_downgrade(db, "plan", "audit-scope mismatch")
        self.assertEqual(wb_config.effective_mode(cfg, db), "plan")
        db.close()
        raw = sqlite3.connect(":memory:")  # no wb_meta table
        self.addCleanup(raw.close)
        notdb = self.p("not-a-db.db")
        with open(notdb, "wb") as fh:
            fh.write(b"this is not an sqlite database" * 100)
        corrupt = sqlite3.connect(notdb)
        self.addCleanup(corrupt.close)
        bad_val = wb_state.connect(self.p("st2", "writeback.db"))
        self.addCleanup(bad_val.close)
        wb_state.set_meta(bad_val, "forced_mode", "PLAN?")
        for label, conn in (("closed", db), ("None", None), ("no wb_meta", raw), ("corrupt", corrupt),
                            ("not a connection", object()), ("invalid forced_mode", bad_val)):
            with self.subTest(label):
                info = wb_config.effective_mode_info(cfg, conn)
                self.assertEqual(info["mode"], "plan")
                self.assertIn("按 plan 处理", " ".join(info["reasons"]))
                self.assertEqual(wb_config.effective_mode(full, conn), "plan")
                self.assertEqual(wb_config.effective_mode(off, conn), "off")  # never raised
        # a readable db without a downgrade record really is "no downgrade"
        clean = wb_state.connect(self.p("st3", "writeback.db"))
        self.addCleanup(clean.close)
        self.assertEqual(wb_config.effective_mode(full, clean), "full")
        ro = wb_state.connect_readonly(self.p("st3", "writeback.db"))
        self.addCleanup(ro.close)
        self.assertEqual(wb_config.effective_mode(full, ro), "full")

    def test_config_sha_stable(self):
        a = wb_config.load(self.p("missing.json"))
        b = wb_config.defaults()
        self.assertEqual(wb_config.config_sha(a), wb_config.config_sha(b))
        b["mode"] = "plan"
        self.assertNotEqual(wb_config.config_sha(a), wb_config.config_sha(b))


# ---------------------------------------------------------------- wb_state: schema
def table_cols(db, name):
    return [r[1] for r in db.execute(f"PRAGMA table_info({name})")]


class SchemaTests(TmpCase):
    def test_fresh_create(self):
        path = self.p("state", "writeback.db")
        db = wb_state.connect(path)
        self.addCleanup(db.close)
        self.assertTrue(os.path.exists(path))
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} - {"sqlite_sequence"}
        self.assertEqual(names, set(GOLDEN))
        for t, cols in GOLDEN.items():
            self.assertEqual(table_cols(db, t), cols, t)
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], wb_state.SCHEMA_VERSION)
        pk = [r[1] for r in sorted(db.execute("PRAGMA table_info(block_lines)"), key=lambda r: r[5]) if r[5]]
        self.assertEqual(pk, ["notion_id", "base_sha", "seq"])
        pk = [r[1] for r in sorted(db.execute("PRAGMA table_info(base_history)"), key=lambda r: r[5]) if r[5]]
        self.assertEqual(pk, ["notion_id", "seq"])
        self.assertIsNone(db.isolation_level)

    def test_idempotent_reopen(self):
        path = self.p("writeback.db")
        db = wb_state.connect(path)
        wb_state.set_meta(db, "enabled_at", "2026-11-17T20:00:00+07:00")
        db.execute("INSERT INTO plans(plan_id, status) VALUES('wb-1', 'draft')")
        schema1 = db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        db.close()
        for _ in range(2):
            db = wb_state.connect(path)
            self.assertEqual(db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall(), schema1)
            self.assertEqual(wb_state.get_meta(db, "enabled_at"), "2026-11-17T20:00:00+07:00")
            self.assertEqual(db.execute("SELECT count(*) FROM plans").fetchone()[0], 1)
            db.close()

    def test_migration_from_older_db(self):
        path = self.p("old.db")
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE base_history(notion_id TEXT, seq INTEGER, rel_path TEXT, sha256 TEXT, page_json_sha TEXT,
                renderer_version TEXT, run_id TEXT, origin TEXT, PRIMARY KEY(notion_id, seq));
            CREATE TABLE plans(plan_id TEXT PRIMARY KEY, created_at TEXT, expires_at TEXT, plan_sha256 TEXT,
                status TEXT, scope TEXT, counts_json TEXT);
            CREATE TABLE plan_items(plan_id TEXT, item_id TEXT, notion_id TEXT, rel_path TEXT, category TEXT,
                ops_json TEXT, risk TEXT, reason TEXT, l_sha TEXT, r_json_sha TEXT, verify_status TEXT,
                PRIMARY KEY(plan_id, item_id));
            CREATE TABLE legacy_extra(x TEXT);
            INSERT INTO base_history VALUES('n1', 1, 'a.md', 'aa', 'pj', '1.2.0', 'r1', 'REMOTE');
            INSERT INTO plan_items(plan_id, item_id, notion_id) VALUES('p', 'M-001', 'n1');
            INSERT INTO plans(plan_id, status) VALUES('p', 'draft');
        """)
        old.commit()
        old.close()
        db = wb_state.connect(path)
        self.addCleanup(db.close)
        for t, cols in GOLDEN.items():
            self.assertEqual(sorted(table_cols(db, t)), sorted(cols), t)
        self.assertEqual(table_cols(db, "base_history")[-1], "seed")
        self.assertEqual(dict(wb_state.latest_base(db, "n1"))["seed"], 0)
        row = db.execute("SELECT decision, binds FROM plan_items WHERE item_id='M-001'").fetchone()
        self.assertEqual((row["decision"], row["binds"]), ("pending", None))
        self.assertEqual(table_cols(db, "legacy_extra"), ["x"])  # unknown tables untouched
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], wb_state.SCHEMA_VERSION)
        # the new tables work
        wb_state.set_meta(db, "k", "v")
        self.assertEqual(wb_state.get_meta(db, "k"), "v")
        self.assertTrue(wb_state.cas_plan_status(db, "p", "draft", "superseded", "discarded_by_user"))

    def test_migration_refuses_missing_pk_column(self):
        path = self.p("old2.db")
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE block_lines(notion_id TEXT, base_sha TEXT, block_id TEXT)")
        old.commit()
        old.close()
        with self.assertRaises(wb_state.SchemaError):
            wb_state.connect(path)
        chk = sqlite3.connect(path)
        self.addCleanup(chk.close)
        self.assertEqual(table_cols(chk, "block_lines"), ["notion_id", "base_sha", "block_id"])
        # the failed migration was rolled back as a whole
        self.assertIsNone(chk.execute("SELECT name FROM sqlite_master WHERE name='base_history'").fetchone())

    def test_newer_schema_refused(self):
        path = self.p("new.db")
        db = wb_state.connect(path)
        db.execute("PRAGMA user_version=99")
        db.close()
        with self.assertRaises(wb_state.SchemaError):
            wb_state.connect(path)

    def test_connect_readonly(self):
        self.assertIsNone(wb_state.connect_readonly(self.p("absent.db")))
        self.assertFalse(os.path.exists(self.p("absent.db")))
        path = self.p("wb.db")
        db = wb_state.connect(path)
        wb_state.set_meta(db, "a", "1")
        db.close()
        self.assertEqual(sorted(os.listdir(self.tmp)), ["wb.db"])
        sha0 = wb_common.sha256_file(path)
        ro = wb_state.connect_readonly(path)
        self.assertEqual(wb_state.get_meta(ro, "a"), "1")
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("INSERT INTO wb_meta VALUES('b', '2')")
        ro.close()
        # documented side effect (review finding): only the empty WAL companions may appear;
        # the database file itself is byte-identical
        self.assertEqual(wb_common.sha256_file(path), sha0)
        extra = sorted(set(os.listdir(self.tmp)) - {"wb.db"})
        self.assertTrue(set(extra) <= {"wb.db-wal", "wb.db-shm"}, extra)
        if "wb.db-wal" in extra:
            self.assertEqual(os.path.getsize(self.p("wb.db-wal")), 0)
        db = wb_state.connect(path)  # a later read-write connection is unaffected
        self.assertEqual(wb_state.get_meta(db, "a"), "1")
        db.close()

    def test_connect_readonly_unwritable_dir_fails_at_open(self):
        d = self.p("locked")
        os.makedirs(d)
        path = os.path.join(d, "writeback.db")
        wb_state.connect(path).close()
        os.chmod(d, 0o555)
        self.addCleanup(os.chmod, d, 0o755)
        if os.access(d, os.W_OK):
            self.skipTest("directory still writable (running as root?)")
        with self.assertRaises(sqlite3.OperationalError):
            wb_state.connect_readonly(path)
        self.assertEqual(os.listdir(d), ["writeback.db"])

    def test_connect_refuses_non_file_paths(self):
        # review finding: ':memory:' became a real file named ':memory:' in the cwd, and '' meant
        # the default (deployed) state/writeback.db
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        for bad in (":memory:", "", "   ", "file:x.db", "file::memory:?cache=shared"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    wb_state.connect(bad)
                with self.assertRaises(ValueError):
                    wb_state.connect_readonly(bad)
        self.assertEqual(os.listdir(self.tmp), [])
        self.assertFalse(os.path.exists(wb_state.DB_PATH) and os.path.getmtime(wb_state.DB_PATH) > time.time() - 60)

    def test_confirmations_one_row_per_event(self):
        # review finding: apply, apply --resume and rollback confirm the same plan item; each event
        # must keep its own audit row
        db = wb_state.connect(self.p("wb.db"))
        self.addCleanup(db.close)
        pk = [r[1] for r in db.execute("PRAGMA table_info(confirmations)") if r[5]]
        self.assertEqual(pk, ["id"])
        for scope in ("apply", "resume", "rollback"):
            db.execute("INSERT INTO confirmations(plan_id, item_id, confirmed_at, challenge_ok, phrase_scope)"
                       " VALUES('P', 'M-001', '2026-11-17T21:00:00+07:00', 1, ?)", (scope,))
        rows = db.execute("SELECT id, phrase_scope FROM confirmations WHERE plan_id='P' AND item_id='M-001'"
                          " ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(1, "apply"), (2, "resume"), (3, "rollback")])
        plan = db.execute("EXPLAIN QUERY PLAN SELECT * FROM confirmations WHERE plan_id='P' AND item_id='M-001'")
        self.assertIn("conf_item", " ".join(str(tuple(r)) for r in plan))

    def test_migration_v1_confirmations_rebuilt(self):
        path = self.p("v1.db")
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE confirmations(plan_id TEXT NOT NULL, item_id TEXT NOT NULL, confirmed_at TEXT, os_user TEXT,
                tty TEXT, challenge_ok INTEGER, phrase_scope TEXT, plan_sha256 TEXT, PRIMARY KEY(plan_id, item_id));
            INSERT INTO confirmations VALUES('P', 'M-001', 't1', 'u', '/dev/pts/1', 1, 'apply', 'ps');
            INSERT INTO confirmations VALUES('P', 'N-001', 't1', 'u', '/dev/pts/1', 1, 'apply', 'ps');
            PRAGMA user_version=1;
        """)
        old.commit()
        old.close()
        db = wb_state.connect(path)
        self.addCleanup(db.close)
        self.assertEqual(table_cols(db, "confirmations"), GOLDEN["confirmations"])
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], wb_state.SCHEMA_VERSION)
        rows = [tuple(r) for r in db.execute("SELECT * FROM confirmations ORDER BY id")]
        self.assertEqual(rows, [(1, "P", "M-001", "t1", "u", "/dev/pts/1", 1, "apply", "ps"),
                                (2, "P", "N-001", "t1", "u", "/dev/pts/1", 1, "apply", "ps")])
        db.execute("INSERT INTO confirmations(plan_id, item_id, phrase_scope) VALUES('P', 'M-001', 'rollback')")
        self.assertEqual(db.execute("SELECT max(id) FROM confirmations").fetchone()[0], 3)
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        self.assertFalse([n for n in names if "__pre_v" in n])
        db.close()
        db2 = wb_state.connect(path)  # idempotent afterwards
        self.assertEqual(db2.execute("SELECT count(*) FROM confirmations").fetchone()[0], 3)
        db2.close()

    def test_rebuild_never_drops_columns(self):
        path = self.p("v1x.db")
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE confirmations(plan_id TEXT NOT NULL, item_id TEXT NOT NULL, confirmed_at TEXT,
                local_note TEXT, PRIMARY KEY(plan_id, item_id));
            INSERT INTO confirmations VALUES('P', 'M-001', 't1', 'keep me');
        """)
        old.commit()
        old.close()
        with self.assertRaises(wb_state.SchemaError):
            wb_state.connect(path)
        chk = sqlite3.connect(path)
        self.addCleanup(chk.close)
        self.assertEqual(table_cols(chk, "confirmations"), ["plan_id", "item_id", "confirmed_at", "local_note"])
        self.assertEqual(chk.execute("SELECT local_note FROM confirmations").fetchall(), [("keep me",)])


# ---------------------------------------------------------------- wb_state: helpers
class StateHelperTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.db = wb_state.connect(self.p("state", "writeback.db"))
        self.addCleanup(self.db.close)
        self.base = self.p("state", "base")

    def test_base_dir_follows_db(self):
        self.assertEqual(wb_state.base_dir_for(self.db), self.base)
        self.assertEqual(wb_state.base_dir_for(None), os.path.join(wb_common.STATE, "base"))

    def test_fileless_db_never_falls_back_to_state_base(self):
        # review finding: an in-memory connection silently used <WORK>/state/base
        real = wb_state.BASE_DIR
        before = sorted(os.listdir(real)) if os.path.isdir(real) else None
        raw = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(raw.close)
        wb_state._migrate(raw)
        with self.assertRaises(ValueError):
            wb_state.base_dir_for(raw)
        c = b"in-memory content"
        with self.assertRaises(ValueError):
            wb_state.append_base(raw, notion_id=NID, rel_path="a.md", sha=sha(c), page_json_sha="p",
                                 renderer_version="1.2.1", run_id="r", origin="REMOTE", content=c)
        self.assertEqual(raw.execute("SELECT count(*) FROM base_history").fetchone()[0], 0)
        self.assertEqual(sorted(os.listdir(real)) if os.path.isdir(real) else None, before)
        # an explicit base_dir still works for such connections
        self.assertEqual(wb_state.store_content(c, base_dir=self.p("mem_base")), sha(c))

    def test_append_base_dedupe(self):
        c1, c2 = b"---\nnotion_id: x\n---\n\nv1\n", b"---\nnotion_id: x\n---\n\nv2\n"
        s1, s2 = sha(c1), sha(c2)
        kw = dict(notion_id=NID, rel_path="General/A.md", page_json_sha="pj1", renderer_version="1.2.1",
                  run_id="r1", origin="REMOTE")
        self.assertIsNone(wb_state.latest_base(self.db, NID))
        self.assertEqual(wb_state.append_base(self.db, sha=s1, content=c1, seed=1, **kw), 1)
        # identical (rel_path, sha, page_json_sha, renderer_version) -> nothing appended
        self.assertIsNone(wb_state.append_base(self.db, sha=s1, content=c1, **dict(kw, run_id="r2")))
        self.assertIsNone(wb_state.append_base(self.db, sha=s1, content=c1, **dict(kw, origin="WRITEBACK")))
        # each of the four fields alone triggers an append
        self.assertEqual(wb_state.append_base(self.db, sha=s2, content=c2, **kw), 2)
        self.assertEqual(wb_state.append_base(self.db, sha=s2, content=c2, **dict(kw, renderer_version="1.2.2")), 3)
        self.assertEqual(wb_state.append_base(self.db, sha=s2, content=c2,
                                              **dict(kw, renderer_version="1.2.2", page_json_sha="pj2")), 4)
        self.assertEqual(wb_state.append_base(self.db, sha=s2, content=c2,
                                              **dict(kw, renderer_version="1.2.2", page_json_sha="pj2",
                                                     rel_path="General/B.md", origin="WRITEBACK")), 5)
        # back to an older version is a new row too (STALE_REVERT needs the chain)
        self.assertEqual(wb_state.append_base(self.db, sha=s1, content=c1, **kw), 6)
        last = wb_state.latest_base(self.db, NID)
        self.assertEqual((last["seq"], last["sha256"], last["origin"], last["seed"]), (6, s1, "REMOTE", 0))
        hist = wb_state.base_history_for(self.db, NID)
        self.assertEqual([h["seq"] for h in hist], [1, 2, 3, 4, 5, 6])
        self.assertEqual(hist[0]["seed"], 1)
        self.assertEqual(hist[4]["origin"], "WRITEBACK")
        # other notion ids have their own chain
        self.assertEqual(wb_state.append_base(self.db, sha=s1, content=c1, **dict(kw, notion_id=NID2)), 1)
        # content stored once per sha
        self.assertEqual(sorted(os.listdir(self.base)), sorted([s1, s2]))
        self.assertEqual(wb_state.base_content(s1, db=self.db), c1)

    def test_append_base_validation(self):
        c = b"body"
        kw = dict(notion_id=NID, rel_path="a.md", page_json_sha="p", renderer_version="1.2.1", run_id="r")
        with self.assertRaises(ValueError):
            wb_state.append_base(self.db, sha=sha(b"other"), content=c, origin="REMOTE", **kw)
        with self.assertRaises(ValueError):
            wb_state.append_base(self.db, sha=sha(c), content=c, origin="LOCAL", **kw)
        with self.assertRaises(ValueError):
            wb_state.append_base(self.db, sha="../../etc", content=c, origin="REMOTE", **kw)
        self.assertEqual(self.db.execute("SELECT count(*) FROM base_history").fetchone()[0], 0)
        self.assertFalse(os.path.exists(self.base) and os.listdir(self.base))

    def test_append_base_joins_outer_transaction(self):
        c = b"x"
        kw = dict(notion_id=NID, rel_path="a.md", sha=sha(c), page_json_sha="p", renderer_version="1.2.1",
                  run_id="r", origin="REMOTE", content=c)
        with self.assertRaises(RuntimeError):
            with wb_state.transaction(self.db):
                self.assertEqual(wb_state.append_base(self.db, **kw), 1)
                wb_state.import_block_lines(self.db, NID, sha(c), [{"type": "paragraph"}])
                raise RuntimeError("publisher failed after the rows")
        self.assertEqual(self.db.execute("SELECT count(*) FROM base_history").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM block_lines").fetchone()[0], 0)
        self.assertFalse(self.db.in_transaction)
        with wb_state.transaction(self.db):
            self.assertEqual(wb_state.append_base(self.db, **kw), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM base_history").fetchone()[0], 1)

    def test_content_store(self):
        data = "正文 v1\n".encode()
        s = wb_state.store_content(data, base_dir=self.base)
        self.assertEqual(s, sha(data))
        f = os.path.join(self.base, s)
        ino = os.stat(f).st_ino
        self.assertEqual(wb_state.store_content(data, base_dir=self.base), s)
        self.assertEqual(os.stat(f).st_ino, ino)  # re-storing is a no-op
        self.assertEqual(wb_state.base_content(s, base_dir=self.base), data)
        self.assertIsNone(wb_state.base_content(sha(b"absent"), base_dir=self.base))
        with open(f, "r+b") as fh:  # corrupt one byte
            fh.write(b"X")
        with self.assertRaises(wb_state.BaseCorrupt):
            wb_state.base_content(s, base_dir=self.base)
        self.assertEqual(wb_state.store_content(data, base_dir=self.base), s)  # heals a corrupt copy
        self.assertEqual(wb_state.base_content(s, base_dir=self.base), data)
        for bad in ("../x", s.upper(), s[:-1], None):
            with self.assertRaises(ValueError):
                wb_state.base_content(bad, base_dir=self.base)
        self.assertEqual([x for x in os.listdir(self.base) if x.startswith(".")], [])

    def rows(self, base_sha, n=3, **extra):
        out = []
        for i in range(n):
            r = {"notion_id": NID, "base_sha": base_sha, "renderer_version": "1.2.1",
                 "block_id": None if i == 1 else f"b{i}", "owner_page_id": NID, "parent_block_id": None,
                 "ord": i, "depth": 0, "type": "gap" if i == 1 else "paragraph", "own_start": 10 + i,
                 "own_end": 10 + i, "line_start": 10 + i, "line_end": 10 + i, "prefix": "", "zero_line": False,
                 "klass": "X" if i == 1 else "R?", "x_reason": None, "json_sha": f"j{i}", "seq": i}
            r.update(extra)
            out.append(r)
        return out

    def test_block_lines_import_idempotent(self):
        bs = sha(b"note")
        rows = self.rows(bs)
        wb_state.import_block_lines(self.db, NID, bs, list(reversed(rows)))
        got = wb_state.block_lines_for(self.db, NID, bs)
        self.assertEqual([r["seq"] for r in got], [0, 1, 2])
        self.assertEqual(got[1]["type"], "gap")
        self.assertIsNone(got[1]["block_id"])
        self.assertEqual(got[0]["zero_line"], 0)
        self.assertEqual(set(got[0]), set(GOLDEN["block_lines"]))
        for k in GOLDEN["block_lines"]:
            self.assertEqual(got[2][k], int(rows[2][k]) if k == "zero_line" else rows[2][k], k)
        before = self.db.execute("SELECT * FROM block_lines ORDER BY seq").fetchall()
        wb_state.import_block_lines(self.db, NID, bs, rows)  # same rows again
        self.assertEqual([tuple(r) for r in self.db.execute("SELECT * FROM block_lines ORDER BY seq")],
                         [tuple(r) for r in before])
        # re-import of a different row set replaces (never accumulates)
        wb_state.import_block_lines(self.db, NID, bs, self.rows(bs, n=2, renderer_version="1.2.2"))
        got = wb_state.block_lines_for(self.db, NID, bs)
        self.assertEqual(len(got), 2)
        self.assertEqual({r["renderer_version"] for r in got}, {"1.2.2"})
        # other (notion_id, base_sha) pairs are untouched
        bs2 = sha(b"note2")
        wb_state.import_block_lines(self.db, NID, bs2, self.rows(bs2))
        self.assertEqual(len(wb_state.block_lines_for(self.db, NID, bs)), 2)
        self.assertEqual(len(wb_state.block_lines_for(self.db, NID, bs2)), 3)

    def test_block_lines_without_seq_and_errors(self):
        bs = sha(b"n")
        rows = [{k: v for k, v in r.items() if k != "seq"} for r in self.rows(bs)]
        rows[0]["unknown_future_key"] = 1
        wb_state.import_block_lines(self.db, NID, bs, rows)
        self.assertEqual([r["ord"] for r in wb_state.block_lines_for(self.db, NID, bs)], [0, 1, 2])
        snapshot = [tuple(r) for r in self.db.execute("SELECT * FROM block_lines ORDER BY seq")]
        bad_sets = [
            self.rows(bs)[:1] + self.rows(sha(b"other"))[1:],              # foreign base_sha
            [dict(self.rows(bs)[0], notion_id=NID2)],                       # foreign notion_id
            [self.rows(bs)[0], {k: v for k, v in self.rows(bs)[1].items() if k != "seq"}],  # mixed seq
            [self.rows(bs)[0], dict(self.rows(bs)[1], seq=0)],              # duplicate seq
        ]
        for bad in bad_sets:
            with self.assertRaises(ValueError):
                wb_state.import_block_lines(self.db, NID, bs, bad)
        self.assertEqual([tuple(r) for r in self.db.execute("SELECT * FROM block_lines ORDER BY seq")], snapshot)
        with self.assertRaises(ValueError):
            wb_state.import_block_lines(self.db, NID, "not-a-sha", [])

    def test_meta(self):
        self.assertIsNone(wb_state.get_meta(self.db, "x"))
        self.assertEqual(wb_state.get_meta(self.db, "x", "d"), "d")
        wb_state.set_meta(self.db, "x", "1")
        wb_state.set_meta(self.db, "x", "2")
        self.assertEqual(wb_state.get_meta(self.db, "x"), "2")
        wb_state.set_meta(self.db, "backlog", ["a.md", "b.md"])
        self.assertEqual(wb_state.get_meta_json(self.db, "backlog"), ["a.md", "b.md"])
        wb_state.set_meta(self.db, "x", None)
        self.assertIsNone(wb_state.get_meta(self.db, "x"))

    def test_plan_cas(self):
        self.db.execute("INSERT INTO plans(plan_id, status) VALUES('P', 'draft')")
        self.assertTrue(wb_state.cas_plan_status(self.db, "P", "draft", "confirmed"))
        self.assertFalse(wb_state.cas_plan_status(self.db, "P", "draft", "confirmed"))  # lost the race
        self.assertTrue(wb_state.cas_plan_status(self.db, "P", "confirmed", "applying"))
        self.assertFalse(wb_state.cas_plan_status(self.db, "P", "confirmed", "applying"))
        self.assertTrue(wb_state.cas_plan_status(self.db, "P", "applying", "aborted", "WRITEBACK_STOP"))
        row = self.db.execute("SELECT status, status_reason FROM plans WHERE plan_id='P'").fetchone()
        self.assertEqual(tuple(row), ("aborted", "WRITEBACK_STOP"))
        for frm, to in (("aborted", "applying"), ("applied", "applying"), ("expired", "confirmed"),
                        ("draft", "applying"), ("draft", "applied")):
            with self.assertRaises(ValueError):
                wb_state.cas_plan_status(self.db, "P", frm, to)
        self.assertFalse(wb_state.cas_plan_status(self.db, "missing", "draft", "expired"))

    def test_journal_seq_never_reused(self):
        self.db.execute("INSERT INTO journal(plan_id, state) VALUES('P', 'intent')")
        self.db.execute("INSERT INTO journal(plan_id, state) VALUES('P', 'intent')")
        self.db.execute("DELETE FROM journal WHERE seq=2")
        self.db.execute("INSERT INTO journal(plan_id, state) VALUES('P', 'intent')")
        self.assertEqual([r[0] for r in self.db.execute("SELECT seq FROM journal ORDER BY seq")], [1, 3])


# ---------------------------------------------------------------- ensure_export_db
class ExportDbTests(TmpCase):
    SRC = os.path.join(wb_common.STATE, "export.db")

    @staticmethod
    def backup_copy_db(src_path, dst):
        src = sqlite3.connect(f"file:{src_path}?immutable=1", uri=True)
        out = sqlite3.connect(dst)
        src.backup(out)
        src.close()
        out.close()

    def check_on_copy(self, source, dst):
        """Copy `source` (read with immutable=1), make the copy pre-D-07, ensure_export_db twice."""
        st0 = os.stat(source)
        sha0 = wb_common.sha256_file(source)
        self.backup_copy_db(source, dst)
        con = sqlite3.connect(dst)
        if "staged_sha256" in table_cols(con, "published_files"):
            # the deployed export.db may already be migrated (review finding): make the COPY pre-D-07
            con.execute("ALTER TABLE published_files DROP COLUMN staged_sha256")
            con.commit()
        n0 = con.execute("SELECT count(*) FROM published_files").fetchone()[0]
        cols0 = table_cols(con, "published_files")
        con.close()
        self.assertNotIn("staged_sha256", cols0)
        wb_state.ensure_export_db(dst)
        wb_state.ensure_export_db(dst)  # idempotent
        con = sqlite3.connect(dst)
        try:
            cols = table_cols(con, "published_files")
            self.assertEqual(cols, cols0 + ["staged_sha256"])  # added exactly once
            self.assertEqual(con.execute("SELECT count(*) FROM published_files").fetchone()[0], n0)
            self.assertEqual(con.execute("SELECT count(*) FROM published_files WHERE staged_sha256 IS NOT NULL")
                             .fetchone()[0], 0)
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            con.close()
        # the source was only read
        st1 = os.stat(source)
        self.assertEqual((st1.st_size, st1.st_mtime_ns), (st0.st_size, st0.st_mtime_ns))
        self.assertEqual(wb_common.sha256_file(source), sha0)

    def test_on_copy_of_sandbox_export_db(self):
        if not os.path.exists(self.SRC):
            self.skipTest(f"{self.SRC} not present")
        self.check_on_copy(self.SRC, self.p("export.db"))

    def test_on_copy_of_already_migrated_export_db(self):
        # review finding: once the deployed export.db has staged_sha256 the copy test must still pass
        if not os.path.exists(self.SRC):
            self.skipTest(f"{self.SRC} not present")
        migrated = self.p("migrated_source.db")
        self.backup_copy_db(self.SRC, migrated)
        wb_state.ensure_export_db(migrated)
        con = sqlite3.connect(migrated)
        con.execute("UPDATE published_files SET staged_sha256=sha256")
        con.commit()
        self.assertIn("staged_sha256", table_cols(con, "published_files"))
        con.close()
        self.check_on_copy(migrated, self.p("export.db"))

    def test_pre_d07_fixture_and_named_insert(self):
        # independent of the sandbox export.db: the publisher's current PUB_SCHEMA
        dst = self.p("fixture_export.db")
        con = sqlite3.connect(dst)
        con.executescript("""
            CREATE TABLE published_files(rel_path TEXT PRIMARY KEY, sha256 TEXT, notion_id TEXT, kind TEXT,
              run_id TEXT, status TEXT);
            CREATE INDEX pf_obj ON published_files(notion_id, kind);
            INSERT INTO published_files VALUES('General/A.md', 'aa', 'n1', 'page', 'r1', 'active');
        """)
        con.commit()
        con.close()
        for _ in range(3):
            wb_state.ensure_export_db(dst)
        con = sqlite3.connect(dst)
        self.addCleanup(con.close)
        self.assertEqual(table_cols(con, "published_files"),
                         ["rel_path", "sha256", "notion_id", "kind", "run_id", "status", "staged_sha256"])
        self.assertEqual(con.execute("SELECT staged_sha256 FROM published_files").fetchall(), [(None,)])
        # the D-07 publisher must write with named columns (documented deployment-order constraint) ...
        con.execute("INSERT OR REPLACE INTO published_files(rel_path, sha256, notion_id, kind, run_id, status,"
                    " staged_sha256) VALUES('General/B.md', 'bb', 'n2', 'page', 'r2', 'active', 'ss')")
        # ... because the pre-D-07 positional 6-value INSERT no longer fits
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("INSERT OR REPLACE INTO published_files VALUES(?,?,?,?,?, 'active')",
                        ("General/C.md", "cc", "n3", "page", "r3"))
        self.assertIn("DEPLOYMENT ORDER", wb_state.ensure_export_db.__doc__)

    def test_missing_file_or_table(self):
        with self.assertRaises(FileNotFoundError):
            wb_state.ensure_export_db(self.p("nope.db"))
        self.assertFalse(os.path.exists(self.p("nope.db")))
        empty = self.p("empty.db")
        sqlite3.connect(empty).close()
        wb_state.ensure_export_db(empty)  # no published_files: nothing created
        con = sqlite3.connect(empty)
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)


# ---------------------------------------------------------------- wb_locks
REFRESH_STYLE = """
import fcntl, sys, time
f = open(sys.argv[1], "w")
try:
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("BUSY", flush=True)
    sys.exit(0)
print("GOT", flush=True)
if len(sys.argv) > 2:
    time.sleep(float(sys.argv[2]))
"""


class LockTests(TmpCase):
    def run_refresh_style(self, path):
        r = subprocess.run(PY + ["-c", REFRESH_STYLE, path], capture_output=True, text=True, timeout=30)
        return r.stdout.strip()

    def test_lock_excludes_refresh_style_flock(self):
        with wb_locks.lock("refresh", state_dir=self.tmp) as h:
            self.assertEqual(h.path, os.path.join(self.tmp, "refresh.lock"))
            self.assertEqual(self.run_refresh_style(h.path), "BUSY")
            with self.assertRaises(wb_locks.LockBusy):  # same process, second open file
                with wb_locks.lock("refresh", state_dir=self.tmp):
                    pass
        self.assertEqual(self.run_refresh_style(os.path.join(self.tmp, "refresh.lock")), "GOT")

    def test_refresh_style_holder_blocks_lock(self):
        path = os.path.join(self.tmp, "refresh.lock")
        proc = subprocess.Popen(PY + ["-c", REFRESH_STYLE, path, "30"], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "GOT")
            with self.assertRaises(wb_locks.LockBusy) as cm:
                with wb_locks.lock("refresh", state_dir=self.tmp):
                    pass
            self.assertIn("refresh 正在运行", str(cm.exception))
            with wb_locks.lock("writeback", state_dir=self.tmp):  # other names are independent
                pass
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()
        with wb_locks.lock("refresh", state_dir=self.tmp):
            pass

    def test_blocking_waits(self):
        path = os.path.join(self.tmp, "writeback.lock")
        proc = subprocess.Popen(PY + ["-c", REFRESH_STYLE, path, "0.7"], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "GOT")
            t0 = time.monotonic()
            with wb_locks.lock("writeback", blocking=True, state_dir=self.tmp):
                self.assertGreater(time.monotonic() - t0, 0.2)
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()

    def test_lock_all_and_names(self):
        with wb_locks.lock_all(["writeback", "refresh"], state_dir=self.tmp) as held:
            self.assertEqual([h.name for h in held], ["refresh", "writeback"])
        with wb_locks.lock("refresh", state_dir=self.tmp):
            with self.assertRaises(wb_locks.LockBusy):
                with wb_locks.lock_all(["writeback", "refresh"], state_dir=self.tmp):
                    pass
        with wb_locks.lock("writeback", state_dir=self.tmp):  # all-or-none: nothing left held
            pass
        for bad in ("../x", "", "A", "refresh/x", None):
            with self.assertRaises(ValueError):
                wb_locks.lock_path(bad, self.tmp)
        self.assertEqual(wb_locks.lock_path("refresh"), os.path.join(wb_common.STATE, "refresh.lock"))


if __name__ == "__main__":
    unittest.main()
